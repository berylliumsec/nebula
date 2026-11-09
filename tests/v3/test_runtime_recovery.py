"""Recovery preserves held work and requires positive restoration evidence."""

from pathlib import Path
import pytest
from sqlalchemy import update
from nebula.v3.database import ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_project_handoff import activation_allowed, restored_checkpoint
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def pair(host):
    coordinator, _ = host
    first = await worker(host, "v1")
    await coordinator.stage(
        manifest("recovery", features=["project.handoff/v1"]),
        Path("/isolated/recovery"),
        "c" * 64,
    )
    target = await worker(host, "recovery")
    old = await coordinator.coordination.claim_project("one", first)
    return coordinator, target, old


async def expire(coordinator):
    async with coordinator.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "one")
            .values(lease_expires_at_ms=0)
        )


async def begin(coordinator, target, old):
    return await coordinator.begin_recovery(
        "one",
        target,
        expected_owner=old,
        expected_handoff_revision=None,
        operation_id="recovery-one",
        actor_id="operator",
    )


async def test_recovery_grant_is_held_idempotent_and_fenced(host):
    coordinator, target, old = await pair(host)
    receipt = await coordinator.coordination.submit(
        actor_id="operator",
        idempotency_key="saved",
        project_id="one",
        name="work",
        payload={},
    )
    await expire(coordinator)
    current = await begin(coordinator, target, old)
    assert await begin(coordinator, target, old) == current
    assert current.fencing_token == old.fencing_token + 1
    with pytest.raises(LeaseLost):
        async with coordinator.coordination.fenced(old):
            pass
    assert await coordinator.coordination.claim_next(current, lane="work") is None
    digest = await restored_checkpoint(coordinator, current)
    with pytest.raises(CoordinationConflict, match="not been verified"):
        await activation_allowed(coordinator, current)
    await coordinator.verify_recovery(
        current, operation_id="recovery-one", restored_checkpoint_digest=digest
    )
    await activation_allowed(coordinator, current)
    assert (
        await coordinator.coordination.claim_next(current)
    ).receipt.command_id == receipt.command_id


async def test_recovery_rejects_live_owner(host):
    coordinator, target, old = await pair(host)
    with pytest.raises(CoordinationConflict, match="still live"):
        await begin(coordinator, target, old)


async def test_recovery_never_replays_an_abandoned_command(host):
    coordinator, target, old = await pair(host)
    receipt = await coordinator.coordination.submit(
        actor_id="operator",
        idempotency_key="unknown",
        project_id="one",
        name="work",
        payload={},
    )
    await coordinator.coordination.claim_next(old)
    await expire(coordinator)
    current = await begin(coordinator, target, old)
    async with coordinator.database.session() as session:
        row = await session.get(RuntimeCommandRow, receipt.command_id)
        assert row.state == "uncertain" and row.runtime_id == old.runtime_id
    with pytest.raises(CoordinationConflict, match="explicit outcome"):
        await restored_checkpoint(coordinator, current)
    await coordinator.resolve_uncertain(
        current,
        receipt.command_id,
        actor_id="operator",
        result={"receipt": "reviewed-effect"},
        succeeded=True,
    )
    digest = await restored_checkpoint(coordinator, current)
    await coordinator.verify_recovery(
        current, operation_id="recovery-one", restored_checkpoint_digest=digest
    )
    assert await coordinator.coordination.claim_next(current) is None
