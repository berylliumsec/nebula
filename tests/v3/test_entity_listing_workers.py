"""Slow entity reads stay in bounded workers with thread-owned Sessions."""

import asyncio
import threading
from contextlib import suppress

import anyio.to_thread
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, ChatSession, RiskClass, ToolCall
from nebula.v3.storage import NebulaStore


@pytest.mark.parametrize("resource", ["artifacts", "tool-artifacts", "chat-sessions"])
def test_slow_listings_use_bounded_workers_and_thread_owned_sessions(
    tmp_path, resource
):
    store = NebulaStore(tmp_path / "workers.db")
    store.create(
        ToolCall(
            id="call",
            engagement_id="project",
            run_id="run",
            tool_name="synthetic.noop",
            risk_class=RiskClass.LOCAL_READ,
        )
    )
    store.create(
        Artifact(
            id="artifact",
            engagement_id="project",
            sha256="0" * 64,
            size=1,
            storage_path="synthetic/blob",
            metadata={"tool_call_id": "call"},
        )
    )
    store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Synthetic",
            provider_profile_id="provider",
            model="model",
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
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum = 0
    entered = 0
    sessions = []
    loop_thread = threading.get_ident()

    class TracedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner_thread = threading.get_ident()
            self.finished = False
            assert self.owner_thread != loop_thread, (
                "listing Session created on event loop"
            )
            sessions.append(self)

        def _check_owner(self):
            assert threading.get_ident() == self.owner_thread

        def scalars(self, *args, **kwargs):
            self._check_owner()
            return super().scalars(*args, **kwargs)

        def get(self, *args, **kwargs):
            self._check_owner()
            return super().get(*args, **kwargs)

        def commit(self):
            self._check_owner()
            return super().commit()

        def close(self):
            self._check_owner()
            super().close()
            self.finished = True

    store.database._session_factory = sessionmaker(
        bind=store.database.engine, class_=TracedSession, expire_on_commit=False
    )

    def slow_read(conn, cursor, statement, parameters, context, executemany):
        nonlocal active, maximum, entered
        if not statement.startswith("SELECT") or "FROM entities" not in statement:
            return
        assert threading.get_ident() != loop_thread
        with guard:
            entered += 1
            active += 1
            maximum = max(maximum, active)
        try:
            assert release.wait(3), "slow reads blocked the request loop"
        finally:
            with guard:
                active -= 1

    event.listen(store.database.engine, "before_cursor_execute", slow_read)

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 2
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
                    for _ in range(6)
                ]
                for _ in range(200):
                    if entered >= 2:
                        break
                    if any(task.done() for task in tasks):
                        await next(task for task in tasks if task.done())
                    await asyncio.sleep(0.005)
                assert entered >= 2
                assert maximum == 2
                # Authentication still answers while every read worker is held.
                response = await asyncio.wait_for(
                    client.get("/api/v1/auth/devices"), timeout=0.5
                )
                assert response.status_code == 401
                assert not any(task.done() for task in tasks)
                release.set()
                responses = await asyncio.gather(*tasks)
                assert all(response.status_code == 200 for response in responses)
                assert all(len(response.json()) == 1 for response in responses)
                assert maximum == 2
                assert sessions and all(session.finished for session in sessions)
        finally:
            release.set()
            for task in tasks:
                with suppress(Exception):
                    await task
            limiter.total_tokens = previous

    try:
        asyncio.run(exercise())
    finally:
        event.remove(store.database.engine, "before_cursor_execute", slow_read)
        store.database.dispose()
