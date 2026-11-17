"""A project worker never runs the global event-history prune.

Every transaction on a worker's project-scoped database locks that runtime's
``project_ownership`` rows. The prune scans the whole event ledger in one
transaction, so inside a worker it held the ownership row past the 30 s lease
while the heartbeat waited, and the project stayed 503 ``starting``. The
unscoped supervisor host runs it instead (see ``test_supervisor.py``).
"""

import asyncio

import pytest

from nebula.v3 import execution_kernel
from nebula.v3.execution_scope import execution_context
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_worker_read_quiescence import worker as worker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_project_worker_startup_runs_no_event_history_prune(worker, monkeypatch):
    started = []

    async def prune(database, **_kwargs):
        started.append(database.execution_runtime_id)

    monkeypatch.setattr(execution_kernel, "prune_orphaned_event_history", prune)
    with execution_context(worker.lease):
        async with worker.kernel.router.lifespan_context(worker.kernel):
            await asyncio.sleep(0.2)
            # The worker's own heartbeat statement is never queued behind it.
            await asyncio.wait_for(
                worker.coordinator.coordination.heartbeat("worker"), 5
            )
    assert started == []
