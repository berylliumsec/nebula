"""The shutdown fence serializes with new durable command acceptance."""

import asyncio
import pytest
from sqlalchemy import delete

from nebula.v3.database import DeploymentHostRow, RuntimeCommandRow
from nebula.v3.runtime_coordination import CoordinationConflict
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("existing_host", [False, True])
async def test_shutdown_waits_for_in_flight_acceptance_before_auditing(
    host, monkeypatch, existing_host
):
    coordinator = host[0]
    if existing_host:
        revision = (await coordinator.status()).revision
    else:
        async with coordinator.database.session() as session:
            await session.execute(delete(DeploymentHostRow))
        revision = 0
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.coordination._ensure_project

    async def barrier(session, project_id):
        entered.set()
        await release.wait()
        await original(session, project_id)

    monkeypatch.setattr(coordinator.coordination, "_ensure_project", barrier)
    submission = asyncio.create_task(
        coordinator.coordination.submit(
            project_id="one",
            actor_id="operator",
            idempotency_key="new",
            name="work",
            payload={},
        )
    )
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        shutdown = asyncio.create_task(
            coordinator.begin_shutdown(expected_revision=revision)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(shutdown), 0.15)
        release.set()
        assert await submission
        with pytest.raises(CoordinationConflict):
            await shutdown
    finally:
        release.set()
        await asyncio.gather(
            submission, *([shutdown] if shutdown else []), return_exceptions=True
        )


async def test_shutdown_replays_existing_receipt_but_rejects_new_intent(host):
    coordinator = host[0]
    arguments = dict(
        project_id="one",
        actor_id="operator",
        idempotency_key="saved",
        name="work",
        payload={},
    )
    receipt = await coordinator.coordination.submit(**arguments)
    async with coordinator.database.session() as session:
        (await session.get(RuntimeCommandRow, receipt.command_id)).state = "succeeded"
    await coordinator.begin_shutdown(
        expected_revision=(await coordinator.status()).revision
    )
    assert await coordinator.coordination.submit(**arguments) == receipt
    with pytest.raises(CoordinationConflict, match="shutdown"):
        await coordinator.coordination.submit(**{**arguments, "idempotency_key": "new"})


async def test_revision_change_before_shutdown_is_rejected_without_effect(host):
    from nebula.v3.runtime_operations import DeploymentOperations

    coordinator = host[0]
    stale = (await coordinator.status()).revision
    # A worker starting while the request waits for the host moves the revision.
    await coordinator.abort_shutdown(detail="A worker became ready.")

    async def dispatch(_action, values):
        return {"leases": len(await coordinator.prepare_shutdown(**values))}

    receipt = await DeploymentOperations(coordinator).execute(
        operation_id="stale-stop",
        actor_id="operator",
        action="shutdown",
        parameters={"expected_revision": stale},
        dispatch=dispatch,
    )
    assert receipt["state"] == "failed"
    assert receipt["result"] == {
        "detail": "deployment changed before shutdown",
        "error_type": "DeploymentChanged",
    }
    async with coordinator.database.session() as session:
        saved = await session.get(DeploymentHostRow, "host")
        assert not saved.shutdown_requested
        assert saved.revision == stale + 1


async def test_project_label_is_one_bounded_line(host):
    from nebula.v3.domain import Engagement

    coordinator, store = host
    await store.create(
        Engagement(id="shaped", name='Red "Team"\n\tShutdown blocked: ' + "x" * 200)
    )
    label = await coordinator.project_label("shaped")
    assert label == (
        "Project \"Red 'Team' Shutdown blocked: " + "x" * 51 + '" (shaped)'
    )
    assert await coordinator.project_label("missing") == "Project missing"


async def test_shutdown_names_a_project_whose_owner_is_no_longer_live(host):
    from sqlalchemy import update

    from nebula.v3.database import RuntimeInstanceRow
    from nebula.v3.runtime_coordination import LeaseLost

    coordinator = host[0]
    await coordinator.coordination.register_runtime("lost", "v1")
    await coordinator.coordination.claim_project("one", "lost")
    async with coordinator.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == "lost")
            .values(lease_expires_at_ms=0)
        )
    with pytest.raises(LeaseLost) as refused:
        await coordinator.prepare_shutdown(
            expected_revision=(await coordinator.status()).revision
        )
    assert str(refused.value) == (
        "Project one is owned by execution worker lost, which is no longer live "
        "(a live execution runtime is required); its recovery must finish before "
        "shutdown"
    )
    async with coordinator.database.session() as session:
        assert not (await session.get(DeploymentHostRow, "host")).shutdown_requested
