"""Storage behavior retained while every unit of work uses the async driver."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from contextlib import asynccontextmanager

import pytest

from nebula.v3.domain import (
    Asset,
    Engagement,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError


@asynccontextmanager
async def open_store(tmp_path):
    store = NebulaStore(tmp_path / "store.db")
    try:
        yield store
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def test_async_crud_revision_checks_and_delete(tmp_path):
    async def exercise():
        async with open_store(tmp_path) as store:
            project = await store.create(Engagement(name="Synthetic project"))
            assert await store.get(Engagement, project.id) == project
            updated = await store.update(
                Engagement, project.id, {"description": "changed"}, expected_revision=1
            )
            assert updated.revision == 2
            with pytest.raises(ConflictError):
                await store.update(
                    Engagement,
                    project.id,
                    {"description": "stale"},
                    expected_revision=1,
                )
            with pytest.raises(ConflictError):
                await store.delete(Engagement, project.id, expected_revision=1)
            await store.delete(Engagement, project.id, expected_revision=2)
            with pytest.raises(NotFoundError):
                await store.get(Engagement, project.id)

    (await exercise())


async def test_async_transaction_rolls_back_every_entity(tmp_path):
    async def exercise():
        async with open_store(tmp_path) as store:
            project = Engagement(name="Rollback")
            with pytest.raises(RuntimeError):
                async with store.transaction() as transaction:
                    await transaction.add(project)
                    await transaction.add(
                        Asset(engagement_id=project.id, name="synthetic asset")
                    )
                    raise RuntimeError("abort")
            assert await store.count(Engagement) == 0
            assert await store.count(Asset) == 0

    (await exercise())


async def test_async_ledger_initializes_first_and_deduplicates(tmp_path):
    async def exercise():
        async with open_store(tmp_path) as store:
            first = await store.append_event(
                "run", "created", {"synthetic": True}, idempotency_key="once"
            )
            assert (
                await store.append_event(
                    "run", "created", {"synthetic": True}, idempotency_key="once"
                )
                == first
            )
            await store.append_event("run", "tick")
            assert [e.sequence for e in await store.replay_events("run")] == [1, 2]
            with pytest.raises(ConflictError):
                await store.append_event(
                    "run", "created", {"changed": True}, idempotency_key="once"
                )

    (await exercise())


async def test_async_ledger_serializes_concurrent_tasks(tmp_path):
    async def exercise():
        async with open_store(tmp_path) as store:
            events = await asyncio.gather(
                *(store.append_event("run", "tick", {"number": n}) for n in range(20))
            )
            assert sorted(e.sequence for e in events) == list(range(1, 21))
            assert store.database.pool_snapshot()["checked_out"] == 0

    (await exercise())


async def test_async_harness_titles_seek_requested_sessions(tmp_path):
    async def exercise():
        async with open_store(tmp_path) as store:
            project = await store.create(Engagement(name="Names"))
            session = await store.create(
                HarnessSession(
                    engagement_id=project.id,
                    harness_profile_id="synthetic",
                    model="fixture",
                )
            )
            first = HarnessTurn(
                engagement_id=project.id,
                harness_session_id=session.id,
                origin=HarnessTurnOrigin.ANALYSIS,
                prompt="First prompt",
            )
            await store.create(first)
            await store.create_many(
                [
                    HarnessTurn(
                        engagement_id=project.id,
                        harness_session_id=session.id,
                        origin=HarnessTurnOrigin.ANALYSIS,
                        prompt="Later prompt",
                    )
                    for _ in range(20)
                ]
            )
            chats, runs, turns = await store.harness_session_title_sources(
                [session.id], engagement_id=project.id
            )
            assert not chats and not runs
            assert turns[session.id].id == first.id
            assert [
                item.id async for item in store.iter_readable_entities(HarnessSession)
            ] == [session.id]

    (await exercise())


pytestmark = pytest.mark.anyio
