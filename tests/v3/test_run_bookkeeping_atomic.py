"""Run completion bookkeeping shares the durable event stream's write order."""

import asyncio

import pytest

from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import AgentRun, Engagement, RunStatus
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_run_bookkeeping_keeps_concurrent_metadata_and_exactly_once_events(
    location,
):
    stores = [
        NebulaStore(Database(location, pool_settings=PoolSettings(5, 12, 2, 5)))
        for _ in range(2)
    ]
    try:
        await stores[0].create(Engagement(id="project", name="Synthetic"))
        run = await stores[0].create(
            AgentRun(
                id="run",
                engagement_id="project",
                objective="Synthetic bookkeeping",
                status=RunStatus.RUNNING,
            )
        )
        await stores[1].database.initialize()

        async def record(index):
            def plan(current):
                return {"metadata": {**current.metadata, f"writer-{index}": True}}, {
                    "writer": index
                }

            return await stores[index % 2].revise_run_with_event(
                run.id,
                plan,
                event_type="synthetic.progress",
                idempotency_key=f"writer:{index}",
            )

        await asyncio.gather(*(record(index) for index in range(8)))
        await record(0)
        saved = await stores[0].get(AgentRun, run.id)
        assert all(saved.metadata[f"writer-{index}"] for index in range(8))
        assert saved.revision == run.revision + 8
        events = await stores[0].replay_events(run.id)
        assert [event.sequence for event in events] == list(range(1, 9))
        assert len({event.idempotency_key for event in events}) == 8
        await stores[0].update(
            AgentRun,
            run.id,
            {"status": RunStatus.CANCELLED},
            expected_revision=saved.revision,
        )
        stopped, created = await stores[0].revise_run_with_event(
            run.id,
            lambda current: (
                None
                if current.status == RunStatus.CANCELLED
                else ({"status": RunStatus.COMPLETE}, {})
            ),
            event_type="run.completed",
            idempotency_key="run:completed",
        )
        assert stopped.status == RunStatus.CANCELLED
        assert created is False
        assert len(await stores[0].replay_events(run.id)) == 8
    finally:
        for store in stores:
            await store.event_history.close()
            await store.database.dispose()
