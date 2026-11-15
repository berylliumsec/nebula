"""Bookkeeping fallback serializes increments without weakening client revisions."""

import asyncio

import pytest

from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, Engagement
from nebula.v3.storage import NebulaStore, RevisionConflict, revise_latest
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_concurrent_bookkeeping_fallback_preserves_every_increment(location):
    first = NebulaStore(Database(location, pool_settings=PoolSettings(5, 12, 2, 5)))
    second = NebulaStore(Database(location, pool_settings=PoolSettings(5, 12, 2, 5)))
    try:
        await first.create(Engagement(id="bookkeeping-project", name="Synthetic"))
        turn = await first.create(
            ChatTurn(
                id="bookkeeping-turn",
                engagement_id="bookkeeping-project",
                session_id="synthetic-session",
                provider_profile_id="synthetic-provider",
                model="inert",
            )
        )
        await second.database.initialize()
        for store in (first, second):
            original = store.update

            async def conflict(model, entity_id, changes, *, expected_revision):
                raise RevisionConflict("force bounded optimistic fallback")

            store.update = conflict

        async def increment(store):
            return await revise_latest(
                store,
                ChatTurn,
                turn.id,
                lambda current: {"next_step": current.next_step + 1},
                attempts=1,
            )

        await asyncio.gather(
            *(increment((first, second)[index % 2]) for index in range(12))
        )
        saved = await first.get(ChatTurn, turn.id)
        assert saved.next_step == 12
        assert saved.revision == turn.revision + 12
        # Client writes remain strict when their snapshot predates bookkeeping.
        with pytest.raises(RevisionConflict):
            await original(
                ChatTurn,
                turn.id,
                {"content": "stale client"},
                expected_revision=turn.revision,
            )
    finally:
        for store in (first, second):
            await store.event_history.close()
            await store.database.dispose()
