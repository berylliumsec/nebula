"""Auxiliary runtime SQLite stores must also yield during database waits."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import aiosqlite
import httpx
import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.browser_engine import BrowserEngineReceipt
from nebula.v3.browserd import ActionLedger
from nebula.v3.butler_memory import ButlerMemoryService, MemoryConflictError
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore
from tests.v3.test_async_core import (
    reject_synchronous_database_waits as reject_synchronous_database_waits,
)
from tests.v3.test_browserd import action


async def test_memory_api_keeps_project_scope_and_revision_after_restart(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "core.db")
        projects = await store.create_many(
            [
                Engagement(id="project-a", name="Synthetic A"),
                Engagement(id="project-b", name="Synthetic B"),
            ]
        )
        memory = ButlerMemoryService(tmp_path / "memory.db")
        local = await memory.capture_verified_lesson(
            projects[0].id,
            "Use reviewed evidence for the project report.",
            source_ref="synthetic-report",
            verification_state="verified",
        )
        global_item = await memory.capture_operator_preference(
            projects[0].id, "Always keep evidence summaries concise."
        )
        assert global_item is not None
        app = create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            butler_memory_service=memory,
            auth_token="synthetic-token",
            execution_data_root=tmp_path,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
            ) as client:
                route = "/api/v1/butler/memory"
                assert (
                    await client.get(route, params={"engagement_id": projects[0].id})
                ).status_code == 401
                headers = {"Authorization": "Bearer synthetic-token"}
                result = await client.get(
                    route, params={"engagement_id": projects[1].id}, headers=headers
                )
                assert result.status_code == 200, result.text
                assert [item["id"] for item in result.json()["items"]] == [
                    global_item.id
                ]
                assert result.json()["items"][0]["source_ref"] is None
                edited = await client.patch(
                    route + "/" + global_item.id,
                    headers=headers,
                    json={
                        "engagement_id": projects[1].id,
                        "content": "Always use concise evidence summaries.",
                        "expected_revision": global_item.revision,
                    },
                )
                assert edited.status_code == 200, edited.text
                assert edited.json()["revision"] == global_item.revision + 1
                restarted = ButlerMemoryService(tmp_path / "memory.db")
                assert (await restarted.list(projects[0].id)).items[
                    0
                ].content == edited.json()["content"]
                with pytest.raises(MemoryConflictError):
                    await restarted.delete(
                        projects[1].id,
                        global_item.id,
                        expected_revision=global_item.revision,
                    )
                await restarted.delete(
                    projects[1].id,
                    global_item.id,
                    expected_revision=global_item.revision + 1,
                )
                assert [
                    item.id for item in (await restarted.list(projects[0].id)).items
                ] == [local.id]
        assert store.database.pool_snapshot()["checked_out"] == 0

    (await exercise())


@pytest.mark.parametrize("kind", ["memory", "browser_actions"])
async def test_auxiliary_writer_wait_yields_and_cancellation_cleans_up(tmp_path, kind):
    async def exercise():
        path = tmp_path / (kind + ".db")
        if kind == "memory":
            memory = ButlerMemoryService(path)
            item = await memory.capture_verified_lesson(
                "project",
                "Use reviewed evidence for the project report.",
                source_ref="synthetic-report",
                verification_state="verified",
            )

            def write():
                return memory.update(
                    "project",
                    item.id,
                    "Retain reviewed evidence in the report.",
                    expected_revision=item.revision,
                )
        else:
            ledger = ActionLedger(path)
            await ledger.initialize()

            async def write():
                return await ledger.claim(action())

        async with aiosqlite.connect(path) as blocker:
            await blocker.execute("BEGIN IMMEDIATE")
            waiting = asyncio.create_task(write())
            ticks = 0
            for _ in range(10):
                await asyncio.sleep(0.005)
                ticks += 1
            assert ticks == 10 and not waiting.done()
            waiting.cancel()
            await blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 3)
        if kind == "memory":
            assert (await memory.list("project")).items[0].revision == item.revision
            updated = await write()
            assert updated.revision == item.revision + 1
        else:
            assert await write() is None
            complete = BrowserEngineReceipt(
                action_token="action-1",
                state="complete",
                page_url="https://app.example.test/",
            )
            await ledger.finish(complete)
            assert await ActionLedger(path).claim(action()) == complete

    (await exercise())


pytestmark = pytest.mark.anyio
