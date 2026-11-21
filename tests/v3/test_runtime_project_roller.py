"""In-Core project roller on SQLite and PostgreSQL.

The roller acts through injected steps that perform the same coordinator
calls the supervisor's ``prepare_execution``, ``handoff`` and ``retire``
operations make; the source worker's checkpoint and the supervisor's
transfer and verification are driven explicitly, as a real roll would.
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    Database,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    ProjectHandoffRow,
    RuntimeProcessRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import HandoffPhase, ResourceSnapshot
from nebula.v3.deployments import DeploymentCoordinator, checkpoint_digest
from nebula.v3.domain import Engagement
from nebula.v3.runtime_project_roller import (
    ROLLER_ACTOR,
    RuntimeProjectRoller,
    TARGET_PREPARED,
    roll_projects_at_once,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_deployment_lifecycle import manifest, stop_worker, worker
from tests.v3.test_runtime_bootstrap import recovery as recovery
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio
PROJECTS = ("one", "three", "two")
FEATURES = ["project.handoff/v1"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def host(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    store = NebulaStore(database)
    for identity in PROJECTS:
        await store.create(Engagement(id=identity, name="Synthetic " + identity))
    coordinator = DeploymentCoordinator(database)
    coordinator.test_children = {}
    for version in ("v1", "v2", "v3"):
        await coordinator.stage(
            manifest(version, features=FEATURES),
            Path("/isolated/releases") / version,
            "a" * 64,
        )
    try:
        yield coordinator, store
    finally:
        for child in coordinator.test_children.values():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        await database.dispose()


class Crash(BaseException):
    """The host stopped mid-pass."""


class Steps:
    """The coordinator effects of the supervisor's roll operations."""

    def __init__(self, host, *, crash_on_handoff: bool = False):
        self.host = host
        self.calls: list[tuple] = []
        self.crash_on_handoff = crash_on_handoff
        self.prepare_failures = 0
        self.before_withdraw = None

    async def prepare(self, version):
        if self.prepare_failures:
            self.prepare_failures -= 1
            self.calls.append(("prepare-refused",))
            raise RuntimeError("Release cannot start an execution worker")
        runtime_id = await worker(self.host, version)
        assert runtime_id is not None
        self.calls.append(("prepare", runtime_id))
        return runtime_id

    async def handoff(self, project_id, target_runtime_id):
        if self.crash_on_handoff:
            raise Crash()
        self.calls.append(("handoff", project_id, target_runtime_id))
        await self.host[0].request_handoff(project_id, target_runtime_id)

    async def retire(self, runtime_id):
        self.calls.append(("retire", runtime_id))
        coordinator = self.host[0]
        proof = await coordinator.begin_retirement(runtime_id)
        stop_worker(self.host, runtime_id)
        await coordinator.retired(proof)

    async def withdraw(self, project_id, target_runtime_id, revision):
        if self.before_withdraw is not None:
            await self.before_withdraw()
        self.calls.append(("withdraw", project_id, target_runtime_id))
        return await self.host[0].withdraw_handoff(
            project_id, target_runtime_id=target_runtime_id, revision=revision
        )

    def named(self, kind):
        return [call for call in self.calls if call[0] == kind]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def resident(host, projects=PROJECTS):
    """Each project on its own v1 worker, with v2 as the execution target."""
    coordinator = host[0]
    leases = {}
    for project in projects:
        runtime_id = await worker(host, "v1")
        leases[project] = await coordinator.coordination.claim_project(
            project, runtime_id
        )
    await coordinator.target_execution("v2")
    return leases


def roller(host, steps, clock=None, **options):
    values = dict(
        projects_at_once=2,
        pinned_retry_seconds=60,
        held_limit_seconds=300,
        operator_quiet_seconds=0,
    )
    values.update(options)
    built = RuntimeProjectRoller(host[0], steps, clock=clock or Clock(), **values)
    host[0].roller = built
    return built


async def transfer(coordinator, lease):
    """The source checkpoints, the supervisor transfers and verifies."""
    assert (
        await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
        == HandoffPhase.TRANSFERABLE
    )
    moved = await coordinator.transfer(lease.project_id)
    await coordinator.verified(moved, restored_checkpoint_digest=checkpoint_digest({}))
    return moved


async def process_state(coordinator, runtime_id):
    async with coordinator.database.session() as session:
        return (await session.get(RuntimeProcessRow, runtime_id)).state


async def handoff_row(coordinator, project_id):
    async with coordinator.database.session() as session:
        return await session.get(ProjectHandoffRow, project_id)


def pin(lease, resource_id="terminal", state="active"):
    return ResourceSnapshot(
        id=resource_id,
        project_id=lease.project_id,
        runtime_id=lease.runtime_id,
        fencing_token=lease.fencing_token,
        kind="terminal",
        state=state,
        restoration="pinned",
        detail="An open terminal cannot be restored",
    )


async def test_rolls_k_projects_at_once_and_retires_each_source_after_verification(
    host,
):
    coordinator = host[0]
    leases = await resident(host)
    steps = Steps(host)
    roll = roller(host, steps)

    status = await roll.reconcile()
    assert status.state == "rolling" and status.remaining == 3
    # Projects roll in a stable order: "one" and "three" first.
    assert [call[1] for call in steps.named("handoff")] == ["one", "three"]
    assert len(steps.named("prepare")) == 2
    assert {item.project_id for item in status.moving} == {"one", "three"}
    # No new slot opens while both handoffs are unfinished.
    await roll.reconcile()
    assert len(steps.named("handoff")) == 2 and not steps.named("retire")

    await transfer(coordinator, leases["one"])
    status = await roll.reconcile()
    # The verified source retires; the freed slot takes the third project.
    assert steps.named("retire") == [("retire", leases["one"].runtime_id)]
    assert await process_state(coordinator, leases["one"].runtime_id) == "retired"
    assert [call[1] for call in steps.named("handoff")] == ["one", "three", "two"]
    assert status.retired_sources == 1

    for project in ("three", "two"):
        await transfer(coordinator, leases[project])
    status = await roll.reconcile()
    assert status.state == "complete" and status.remaining == 0
    assert status.retired_sources == 3 and not status.moving
    for lease in leases.values():
        assert await process_state(coordinator, lease.runtime_id) == "retired"
    deployment = await coordinator.status()
    assert deployment.roll == status
    assert {project.runtime_version for project in deployment.projects} == {"v2"}
    async with coordinator.database.session() as session:
        actions = [
            row.action
            for row in await session.scalars(
                select(DeploymentReceiptRow).where(
                    DeploymentReceiptRow.action.startswith("roll.")
                )
            )
        ]
    assert actions.count("roll.target_prepared") == 3
    assert actions.count("roll.handoff_requested") == 3
    assert actions.count("roll.source_retired") == 3


async def test_pinned_project_is_never_held_and_is_retried_after_its_pin_clears(
    host,
):
    coordinator = host[0]
    leases = await resident(host, ("one", "two"))
    await coordinator.resource(leases["one"], pin(leases["one"]))
    steps = Steps(host)
    clock = Clock()
    roll = roller(host, steps, clock)

    status = await roll.reconcile()
    # The pinned project is skipped from SQL; it never sees a handoff hold.
    assert [call[1] for call in steps.named("handoff")] == ["two"]
    assert await handoff_row(coordinator, "one") is None
    assert [item.project_id for item in status.pinned] == ["one"]
    assert status.pinned[0].blockers == ["An open terminal cannot be restored"]
    assert status.pinned[0].retry_in_seconds == 60

    await transfer(coordinator, leases["two"])
    status = await roll.reconcile()
    assert status.state == "waiting" and status.remaining == 1
    assert len(steps.named("handoff")) == 1

    # The pin clears; the retry needs no external deployer run.
    await coordinator.resource(leases["one"], pin(leases["one"], state="closed"))
    clock.now += 61
    await roll.reconcile()
    assert [call[1] for call in steps.named("handoff")] == ["two", "one"]
    await transfer(coordinator, leases["one"])
    status = await roll.reconcile()
    assert status.state == "complete" and not status.pinned
    assert await process_state(coordinator, leases["one"].runtime_id) == "retired"


async def test_pinned_handoff_is_withdrawn_and_its_target_retargeted(host):
    coordinator = host[0]
    leases = await resident(host, ("one", "two"))
    steps = Steps(host)
    roll = roller(host, steps, projects_at_once=1)

    await roll.reconcile()
    [(_kind, project, target)] = steps.named("handoff")
    assert project == "one"
    # A terminal opens after the request; the source reports the pin.
    await coordinator.resource(leases["one"], pin(leases["one"]))
    assert (
        await coordinator.checkpoint(leases["one"], checkpoint={}, blockers=[])
        == HandoffPhase.FINISHING
    )
    status = await roll.reconcile()
    withdrawn = await handoff_row(coordinator, "one")
    assert withdrawn.phase == HandoffPhase.VERIFIED
    assert withdrawn.checkpoint == {"cancelled_before_transfer": True}
    assert not withdrawn.admission_held
    # The same prepared target now serves the next project.
    assert steps.named("withdraw") == [("withdraw", "one", target)]
    assert steps.named("handoff")[-1] == ("handoff", "two", target)
    assert len(steps.named("prepare")) == 1 and not steps.named("retire")
    assert [item.project_id for item in status.pinned] == ["one"]
    assert [item.project_id for item in status.moving] == ["two"]
    assert await process_state(coordinator, leases["one"].runtime_id) == "ready"


async def test_held_handoff_is_released_after_the_held_limit(host):
    coordinator = host[0]
    leases = await resident(host, ("one",))
    steps = Steps(host)
    clock = Clock()
    roll = roller(host, steps, clock, held_limit_seconds=300)

    await roll.reconcile()
    [(_kind, _project, target)] = steps.named("handoff")
    assert (
        await coordinator.checkpoint(
            leases["one"], checkpoint={}, blockers=["A provider turn is running"]
        )
        == HandoffPhase.FINISHING
    )
    assert (await handoff_row(coordinator, "one")).admission_held
    clock.now += 299
    status = await roll.reconcile()
    assert status.moving[0].admission_held and len(steps.named("handoff")) == 1
    clock.now += 2
    status = await roll.reconcile()
    row = await handoff_row(coordinator, "one")
    assert row.phase == HandoffPhase.VERIFIED and not row.admission_held
    assert status.state == "waiting" and status.pinned[0].project_id == "one"
    # The unused target is retired instead of kept as a spare.
    assert steps.named("retire") == [("retire", target)]
    assert await process_state(coordinator, target) == "retired"


async def test_restart_mid_roll_resumes_from_sql(host):
    coordinator = host[0]
    leases = await resident(host, ("one", "two"))
    crashing = Steps(host, crash_on_handoff=True)
    with pytest.raises(Crash):
        await roller(host, crashing).reconcile()
    [(_kind, prepared)] = crashing.named("prepare")

    # A new host's roller recognises the target from its saved receipt.
    steps = Steps(host)
    restarted = roller(host, steps)
    await restarted.reconcile()
    assert ("handoff", "one", prepared) in steps.calls
    assert len(steps.named("prepare")) == 1  # only for the second project
    await transfer(coordinator, leases["one"])

    # Another restart after the verified transfer still retires the source.
    again = Steps(host)
    status = await roller(host, again).reconcile()
    assert again.named("retire") == [("retire", leases["one"].runtime_id)]
    assert not again.named("prepare") and not again.named("handoff")
    assert status.state == "rolling" and status.remaining == 1
    async with coordinator.database.session() as session:
        saved = list(
            await session.scalars(
                select(DeploymentReceiptRow).where(
                    DeploymentReceiptRow.action == TARGET_PREPARED
                )
            )
        )
    assert prepared in {row.result["runtime_id"] for row in saved}


async def test_roller_defers_while_an_operator_deployment_runs(host):
    coordinator = host[0]
    await resident(host, ("one",))
    async with coordinator.database.session() as session:
        now = await coordinator.coordination._clock_ms(session)
    async with coordinator.coordination._write() as session:
        for operation_id, actor, age in (
            ("deployer-handoff", "operator", 0),
            ("own-step", ROLLER_ACTOR, 0),
        ):
            session.add(
                DeploymentOperationRow(
                    operation_id=operation_id,
                    actor_id=actor,
                    action="handoff",
                    request_hash="0" * 64,
                    parameters={},
                    state="succeeded",
                    claim_owner="test",
                    result={},
                    created_at_ms=now - age,
                    updated_at_ms=now - age,
                )
            )
    steps = Steps(host)
    status = await roller(host, steps, operator_quiet_seconds=600).reconcile()
    assert status.state == "deferred" and status.remaining == 1
    assert "handoff" in status.detail and not steps.calls
    # Once the deployer has been quiet for the window, the roller acts.
    async with coordinator.coordination._write() as session:
        row = await session.get(DeploymentOperationRow, "deployer-handoff")
        row.updated_at_ms = now - 601_000
    status = await roller(host, steps, operator_quiet_seconds=600).reconcile()
    assert status.state == "rolling" and steps.named("handoff")


async def test_disabled_roller_takes_no_step(host, monkeypatch):
    await resident(host, ("one",))
    monkeypatch.setenv("NEBULA_ROLL_PROJECTS_AT_ONCE", "0")
    assert roll_projects_at_once() == 0
    steps = Steps(host)
    status = await roller(host, steps, projects_at_once=None).reconcile()
    assert status.state == "disabled" and not steps.calls
    monkeypatch.setenv("NEBULA_ROLL_PROJECTS_AT_ONCE", "99")
    assert roll_projects_at_once() == 16


async def test_ledger_steps_are_roller_receipts_and_status_is_authenticated(
    recovery, monkeypatch
):
    """Steps run as ordinary deployment operations under the roller's actor."""
    from nebula.v3.runtime_coordination import OperationRejected
    from nebula.v3.runtime_project_roller import LedgerRollActions, RollStepFailed
    from tests.v3.test_runtime_bootstrap import AUTH

    root = recovery.root
    requested = []

    async def handoff(project_id, target_runtime_id):
        requested.append((project_id, target_runtime_id))

    async def refused(version):
        raise OperationRejected(f"Release {version} cannot start an execution worker")

    monkeypatch.setattr(root, "request_project_handoff", handoff)
    monkeypatch.setattr(root, "prepare_execution", refused)
    steps = LedgerRollActions(root)
    await steps.handoff("synthetic-project", "runtime-target")
    assert requested == [("synthetic-project", "runtime-target")]
    with pytest.raises(RollStepFailed, match="cannot start an execution worker"):
        await steps.prepare("v2")
    async with root.coordinator.database.session() as session:
        rows = {
            row.action: row
            for row in await session.scalars(select(DeploymentOperationRow))
        }
    assert {row.actor_id for row in rows.values()} == {ROLLER_ACTOR}
    assert rows["handoff"].state == "succeeded"
    assert rows["prepare_execution"].state == "failed"

    # The supervisor's roller reports through authenticated status.
    roll = RuntimeProjectRoller.for_supervisor(
        root, projects_at_once=2, operator_quiet_seconds=600
    )
    try:
        status = await roll.reconcile()
        assert status.state == "idle" and status.detail == "No execution target is set."
        assert (
            await recovery.client.get("/api/v1/deployment/status")
        ).status_code == 401
        body = (
            await recovery.client.get("/api/v1/deployment/status", headers=AUTH)
        ).json()
        assert body["roll"]["state"] == "idle"
        assert body["roll"]["projects_at_once"] == 2
    finally:
        await roll.close()
    assert root.coordinator.roller is None
    assert (await root.coordinator.status()).roll is None


async def test_withdraw_never_sends_a_transferred_project_back(host):
    """A handoff that transfers after the pass read it is left to finish."""
    coordinator = host[0]
    leases = await resident(host, ("one",))
    steps = Steps(host)
    clock = Clock()
    roll = roller(host, steps, clock, held_limit_seconds=300)
    await roll.reconcile()
    [(_kind, _project, target)] = steps.named("handoff")
    await coordinator.checkpoint(
        leases["one"], checkpoint={}, blockers=["A provider turn is running"]
    )
    clock.now += 301

    async def transfer_first():
        # The turn ends and the supervisor transfers between read and withdraw.
        await transfer(coordinator, leases["one"])

    steps.before_withdraw = transfer_first
    await roll.reconcile()
    row = await handoff_row(coordinator, "one")
    assert row.phase == HandoffPhase.VERIFIED and row.target_runtime_id == target
    assert row.source_runtime_id == leases["one"].runtime_id
    assert not row.admission_held and row.checkpoint != {
        "cancelled_before_transfer": True
    }
    owners = {item.project_id: item for item in (await coordinator.status()).projects}
    assert owners["one"].runtime_id == target
    steps.before_withdraw = None
    status = await roll.reconcile()
    assert steps.named("retire") == [("retire", leases["one"].runtime_id)]
    assert status.state == "complete" and not status.pinned


async def test_refused_preparation_backs_off_instead_of_retrying_every_pass(host):
    leases = await resident(host, ("one",))
    steps = Steps(host)
    steps.prepare_failures = 2
    clock = Clock()
    roll = roller(host, steps, clock)
    status = await roll.reconcile()
    assert status.state == "blocked"
    assert "cannot start an execution worker" in status.blockers[0]
    for _ in range(3):
        await roll.reconcile()
    assert len(steps.named("prepare-refused")) == 1
    clock.now += 5
    await roll.reconcile()
    assert len(steps.named("prepare-refused")) == 2
    clock.now += 9
    await roll.reconcile()
    assert len(steps.named("prepare-refused")) == 2
    clock.now += 1
    status = await roll.reconcile()
    assert [call[1] for call in steps.named("handoff")] == ["one"]
    assert status.state == "rolling" and not status.blockers
    assert leases


async def test_a_previous_rolls_target_retires_only_after_its_next_handoff_verifies(
    host,
):
    coordinator = host[0]
    leases = await resident(host, ("one",))
    steps = Steps(host)
    roll = roller(host, steps)
    await roll.reconcile()
    second = await transfer(coordinator, leases["one"])
    assert (await roll.reconcile()).state == "complete"

    # The next release: the roller's own v2 target is now a source.
    await coordinator.target_execution("v3")
    await roll.reconcile()
    [_first, (_kind, project, third)] = steps.named("handoff")
    assert project == "one"
    assert (
        await coordinator.checkpoint(second, checkpoint={}, blockers=[])
        == HandoffPhase.TRANSFERABLE
    )
    moved = await coordinator.transfer("one")
    # Transferred but not verified: the source is ownerless, yet kept.
    await roll.reconcile()
    assert ("retire", second.runtime_id) not in steps.calls
    assert await process_state(coordinator, second.runtime_id) == "ready"
    await coordinator.verified(moved, restored_checkpoint_digest=checkpoint_digest({}))
    status = await roll.reconcile()
    assert ("retire", second.runtime_id) in steps.calls
    assert status.state == "complete" and status.retired_sources == 2
    assert third


async def test_deferred_roller_still_releases_its_own_held_handoff(host):
    coordinator = host[0]
    leases = await resident(host, ("one",))
    steps = Steps(host)
    clock = Clock()
    roll = roller(host, steps, clock, operator_quiet_seconds=600)
    await roll.reconcile()
    assert steps.named("handoff")
    await coordinator.checkpoint(
        leases["one"], checkpoint={}, blockers=["A provider turn is running"]
    )
    async with coordinator.database.session() as session:
        now = await coordinator.coordination._clock_ms(session)
    async with coordinator.coordination._write() as session:
        for operation_id, state, age in (
            ("deployer-stage", "succeeded", 0),
            # Left running by a host that stopped two hours ago.
            ("stale-running", "running", 7_200_000),
        ):
            session.add(
                DeploymentOperationRow(
                    operation_id=operation_id,
                    actor_id="operator",
                    action="stage",
                    request_hash="0" * 64,
                    parameters={},
                    state=state,
                    claim_owner="gone",
                    result={},
                    created_at_ms=now - age,
                    updated_at_ms=now - age,
                )
            )
    clock.now += 301
    status = await roll.reconcile()
    assert status.state == "deferred"
    row = await handoff_row(coordinator, "one")
    assert row.phase == HandoffPhase.VERIFIED and not row.admission_held
    assert [item.project_id for item in status.pinned] == ["one"]
    assert steps.named("withdraw")
    # Only the stale running row remains: it no longer defers the roller.
    async with coordinator.coordination._write() as session:
        await session.delete(
            await session.get(DeploymentOperationRow, "deployer-stage")
        )
    assert (await roll.reconcile()).state != "deferred"
