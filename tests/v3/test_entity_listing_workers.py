"""Slow entity reads use bounded admission and task-owned AsyncSessions."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
    wait_for_database_gate,
)

import asyncio
from contextlib import suppress

import anyio.to_thread
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, ChatSession, RiskClass, ToolCall
from nebula.v3.storage import NebulaStore


@pytest.mark.parametrize("worker_tokens", [2, 40])
@pytest.mark.parametrize("resource", ["artifacts", "tool-artifacts", "chat-sessions"])
async def test_slow_listings_use_bounded_admission_and_task_owned_sessions(
    tmp_path, resource, worker_tokens
):
    store = NebulaStore(tmp_path / "workers.db")
    (
        await store.create(
            ToolCall(
                id="call",
                engagement_id="project",
                run_id="run",
                tool_name="synthetic.noop",
                risk_class=RiskClass.LOCAL_READ,
            )
        )
    )
    (
        await store.create(
            Artifact(
                id="artifact",
                engagement_id="project",
                sha256="0" * 64,
                size=1,
                storage_path="synthetic/blob",
                metadata={"tool_call_id": "call"},
            )
        )
    )
    (
        await store.create(
            ChatSession(
                id="chat",
                engagement_id="project",
                title="Synthetic",
                provider_profile_id="provider",
                model="model",
            )
        )
    )
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="synthetic-token",
        execution_data_root=tmp_path / "runtime",
    )
    path = {
        "artifacts": "/api/v1/artifacts?engagement_id=project",
        "tool-artifacts": "/api/v1/tool-calls/call/artifacts",
        "chat-sessions": "/api/v1/chat-sessions?engagement_id=project",
    }[resource]
    release = asyncio.Event()
    active = 0
    maximum = 0
    entered = 0
    sessions = []
    loop_task = asyncio.current_task()

    class TracedSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = asyncio.current_task()
            self.finished = False
            sessions.append(self)

        async def scalars(self, *args, **kwargs):
            assert asyncio.current_task() is self.owner
            return await super().scalars(*args, **kwargs)

        async def get(self, *args, **kwargs):
            assert asyncio.current_task() is self.owner
            return await super().get(*args, **kwargs)

        async def commit(self):
            assert asyncio.current_task() is self.owner
            return await super().commit()

        async def close(self):
            await super().close()
            self.finished = True

    store.database._session_factory = async_sessionmaker(
        bind=store.database.engine, class_=TracedSession, expire_on_commit=False
    )

    def slow_read(conn, cursor, statement, parameters, context, executemany):
        nonlocal active, maximum, entered
        if not statement.startswith("SELECT") or "FROM entities" not in statement:
            return
        if parameters and parameters[0] == "paired_device_sessions":
            return
        entered += 1
        active += 1
        maximum = max(maximum, active)
        try:
            wait_for_database_gate(conn, release)
        finally:
            active -= 1

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", slow_read)

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = worker_tokens
        expected_capacity = min(4, max(1, store.database.pool_size - 1))
        tasks = []
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://core.test"
            ) as client:
                tasks = [
                    asyncio.create_task(
                        client.get(
                            path, headers={"Authorization": "Bearer synthetic-token"}
                        )
                    )
                    for _ in range(20 if worker_tokens == 40 else 6)
                ]
                for _ in range(200):
                    if entered >= expected_capacity:
                        break
                    if any(task.done() for task in tasks):
                        await next(task for task in tasks if task.done())
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.05)  # let any incorrectly admitted work start
                assert entered == expected_capacity
                assert maximum == expected_capacity
                # A burst of slow lists must leave DB connections for ordinary
                # authenticated reads, even with the default 40 worker tokens.
                response = await asyncio.wait_for(
                    client.get(
                        "/api/v1/auth/devices",
                        headers={"Authorization": "Bearer synthetic-token"},
                    ),
                    timeout=0.5,
                )
                assert response.status_code == 200
                unauthorized = await asyncio.wait_for(client.get(path), timeout=0.5)
                assert unauthorized.status_code == 401
                assert not any(task.done() for task in tasks)
                release.set()
                responses = await asyncio.gather(*tasks)
                assert all(response.status_code == 200 for response in responses)
                assert all(len(response.json()) == 1 for response in responses)
                assert maximum == expected_capacity
                assert any(session.owner is not loop_task for session in sessions)
                assert len({id(session) for session in sessions}) == len(sessions)
                assert all(session.finished for session in sessions)
        finally:
            release.set()
            for task in tasks:
                with suppress(Exception):
                    await task
            limiter.total_tokens = previous

    try:
        (await exercise())
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", slow_read
        )
        (await store.database.dispose())


pytestmark = pytest.mark.anyio
