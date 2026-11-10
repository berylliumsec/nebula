"""Expired-owner initiation uses SQL receipts, never a speculative spawn retry."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
)
from nebula.v3.domain import Engagement
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_operations import DeploymentOperations, OperationRejected
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def process(runtime_id, version="v1"):
    return RuntimeProcessRow(
        runtime_id=runtime_id,
        version=version,
        role="execution",
        pid=12345,
        process_birth="synthetic-birth",
        endpoint="unix:///synthetic/" + runtime_id + ".sock",
        credential_reference="synthetic-credential",
        state="ready",
        updated_at_ms=1_000_000,
    )


class Management:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.operations = DeploymentOperations(coordinator)
        self.calls = []
        self.lose_prepare = False
        self.reject_reconciliation = False
        self.change_handoff = False

    def ledger(self):
        return self.operations

    async def dispatch(self, action, values, operation_id, actor_id):
        self.calls.append((action, values, operation_id, actor_id))
        if action == "reconcile_project":
            if self.reject_reconciliation:
                raise OperationRejected("OS absence could not be confirmed")
            return {"reconciled": True}
        if action == "prepare_execution":
            runtime_id = "prepared-target"
            await self.coordinator.coordination.register_runtime(
                runtime_id, values["version"]
            )
            async with self.coordinator.database.session() as session:
                session.add(process(runtime_id, values["version"]))
                if self.change_handoff:
                    handoff = await session.get(ProjectHandoffRow, "one")
                    handoff.revision += 1
            if self.lose_prepare:
                raise RuntimeError("The spawn result was lost")
            return {"runtime_id": runtime_id}
        assert action == "recover_project"
        return {
            "project_id": values["project_id"],
            "runtime_id": values["target_runtime_id"],
            "fencing_token": values["expected_owner"]["fencing_token"] + 1,
        }


@pytest.fixture
async def recovery(host, monkeypatch):
    coordinator, store = host
    clock = [1_000_000]

    async def now(_session):
        return clock[0]

    coordinator.coordination._clock_ms = now
    await coordinator.coordination.register_runtime("source", "v1")
    await coordinator.coordination.register_runtime("api", "v2", role="api")
    lease = await coordinator.coordination.claim_project("one", "source")
    async with coordinator.database.session() as session:
        row = await session.get(DeploymentHostRow, "host")
        row.api_runtime_id, row.target_version = "api", "v2"
        owner = await session.get(ProjectOwnershipRow, "one")
        owner.lease_expires_at_ms = clock[0] - 1
        session.add(process("source"))
    observed = [
        SimpleNamespace(
            runtime_id="source",
            process_state="gone",
            identity=SimpleNamespace(version="v1"),
        )
    ]

    async def rediscover():
        return observed

    management = Management(coordinator)
    supervisor = SimpleNamespace(
        coordinator=coordinator,
        store=store,
        host=SimpleNamespace(rediscover=rediscover),
        management=management,
    )
    diagnostics = []
    monkeypatch.setattr(
        "nebula.v3.runtime_auto_admission.record_caught_exception",
        lambda *args, **kwargs: diagnostics.append(args),
    )

    def controller():
        return RuntimeAutoAdmission(supervisor, default_version="v2")

    return SimpleNamespace(
        coordinator=coordinator,
        store=store,
        lease=lease,
        clock=clock,
        observed=observed,
        management=management,
        controller=controller,
        automatic=controller(),
        diagnostics=diagnostics,
    )


def expected_owner(recovery):
    return {
        "project_id": recovery.lease.project_id,
        "runtime_id": recovery.lease.runtime_id,
        "fencing_token": recovery.lease.fencing_token,
    }


async def operations(recovery):
    async with recovery.coordinator.database.session() as session:
        return list(await session.scalars(select(DeploymentOperationRow)))


async def history(recovery, *, phase="blocked", count=1, same_owner=False):
    async with recovery.coordinator.database.session() as session:
        for index in range(count):
            session.add(
                RuntimeRecoveryRow(
                    operation_id="prior-" + str(index),
                    actor_id="operator",
                    project_id="one",
                    revision=1,
                    source_runtime_id="source" if same_owner else "old-" + str(index),
                    source_fencing_token=recovery.lease.fencing_token,
                    target_runtime_id="previous-target-" + str(index),
                    target_fencing_token=recovery.lease.fencing_token + 1,
                    expected_handoff_revision=None,
                    phase=phase,
                    checkpoint={},
                    blockers=[],
                    created_at_ms=recovery.clock[0],
                    updated_at_ms=recovery.clock[0],
                )
            )


async def test_expired_positive_dead_owner_initiates_exact_source_version(recovery):
    await recovery.automatic.reconcile()
    calls = recovery.management.calls
    assert [call[0] for call in calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    assert calls[0][1] == {"expected_owner": expected_owner(recovery)}
    assert calls[1][1] == {"version": "v1"}
    assert calls[2][1] == {
        "project_id": "one",
        "target_runtime_id": "prepared-target",
        "expected_owner": expected_owner(recovery),
        "expected_handoff_revision": None,
    }
    assert len({call[2] for call in calls}) == 3
    assert all(row.state == "succeeded" for row in await operations(recovery))
    # Initiation delegates authority to management, never clearing/extending it.
    async with recovery.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.runtime_id == "source"
        assert owner.fencing_token == recovery.lease.fencing_token
        assert owner.lease_expires_at_ms == recovery.clock[0] - 1


@pytest.mark.parametrize("state", ["alive", "unknown", "unspawned", "missing"])
async def test_source_without_positive_os_absence_is_preserved(recovery, state):
    recovery.observed[0].process_state = state
    if state == "missing":
        recovery.observed.clear()
    await recovery.automatic.reconcile()
    assert not recovery.management.calls
    assert not await operations(recovery)
    async with recovery.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.runtime_id == "source"
        assert owner.fencing_token == recovery.lease.fencing_token


async def test_unexpired_dead_owner_is_not_reconciled(recovery):
    async with recovery.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        owner.lease_expires_at_ms = recovery.clock[0] + 1
    await recovery.automatic.reconcile()
    assert not recovery.management.calls


async def test_durable_receipts_prevent_duplicate_prepare_in_new_controller(recovery):
    await recovery.automatic.reconcile()
    recovery.management.operations = DeploymentOperations(recovery.coordinator)
    await recovery.controller().reconcile()
    assert len(recovery.management.calls) == 3
    assert len(await operations(recovery)) == 3
    assert not recovery.diagnostics


async def test_uncertain_prepare_retains_process_and_never_replays(recovery):
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    await recovery.automatic.reconcile()
    assert len(recovery.diagnostics) == 1
    recovery.management.operations = DeploymentOperations(recovery.coordinator)
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "prepare_execution",
    ]
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "uncertain"
    async with recovery.coordinator.database.session() as session:
        assert (
            await session.get(RuntimeProcessRow, "prepared-target")
        ).state == "ready"
        assert (await session.get(ProjectOwnershipRow, "one")).runtime_id == "source"


async def test_unconfirmed_running_prepare_becomes_uncertain_without_dispatch(recovery):
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    rows = {row.action: row for row in await operations(recovery)}
    async with recovery.coordinator.database.session() as session:
        row = await session.get(
            DeploymentOperationRow, rows["prepare_execution"].operation_id
        )
        row.state, row.result = "running", None
    recovery.management.operations = DeploymentOperations(recovery.coordinator)
    await recovery.controller().reconcile()
    assert len(recovery.management.calls) == 2
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "uncertain"


async def test_reconciliation_rejection_cannot_prepare(recovery):
    recovery.management.reject_reconciliation = True
    await recovery.automatic.reconcile()
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == ["reconcile_project"]
    assert (await operations(recovery))[0].state == "failed"


async def test_unknown_unassigned_execution_is_not_reused_or_duplicated(recovery):
    async with recovery.coordinator.database.session() as session:
        session.add(process("unknown-target"))
    await recovery.automatic.reconcile()
    assert [call[0] for call in recovery.management.calls] == ["reconcile_project"]
    assert len(await operations(recovery)) == 1


@pytest.mark.parametrize("held", ["shutdown", "archived"])
async def test_shutdown_and_archived_projects_do_not_initiate_recovery(recovery, held):
    if held == "shutdown":
        async with recovery.coordinator.database.session() as session:
            (await session.get(DeploymentHostRow, "host")).shutdown_requested = True
    else:
        await recovery.store.update(Engagement, "one", {"status": "archived"})
    await recovery.automatic.reconcile()
    assert not recovery.management.calls
    assert not await operations(recovery)


@pytest.mark.parametrize("phase", ["held", "prepared", "verified"])
async def test_existing_recovery_is_left_for_supervisor_advancement(recovery, phase):
    await history(recovery, phase=phase, same_owner=True)
    await recovery.automatic.reconcile()
    assert not recovery.management.calls


async def test_persisted_crash_loop_cap_expires_after_ten_minutes(recovery):
    await history(recovery, count=3)
    await recovery.automatic.reconcile()
    await recovery.automatic.reconcile()
    await recovery.controller().reconcile()
    assert not recovery.management.calls
    assert len(recovery.diagnostics) == 2
    recovery.clock[0] += 10 * 60_000 + 1
    await recovery.automatic.reconcile()
    assert len(recovery.management.calls) == 3


async def test_recovery_uses_handoff_revision_read_after_preparation(recovery):
    async with recovery.coordinator.database.session() as session:
        session.add(
            ProjectHandoffRow(
                project_id="one",
                revision=7,
                source_runtime_id="source",
                source_fencing_token=recovery.lease.fencing_token,
                target_runtime_id="old-target",
                target_version="v2",
                phase="finishing",
                admission_held=True,
                blockers=[],
                checkpoint={},
                admitted_turn_ids=[],
                updated_at_ms=recovery.clock[0],
            )
        )
    recovery.management.change_handoff = True
    await recovery.automatic.reconcile()
    assert recovery.management.calls[-1][1]["expected_handoff_revision"] == 8


async def test_recovery_initiation_is_bounded_to_sixteen_projects(recovery):
    for index in range(16):
        project_id = f"batch-{index:02}"
        await recovery.store.create(
            Engagement(id=project_id, name="Synthetic " + project_id)
        )
        async with recovery.coordinator.database.session() as session:
            session.add(
                ProjectOwnershipRow(
                    project_id=project_id,
                    admission_requested=True,
                    runtime_id="source",
                    fencing_token=1,
                    lease_expires_at_ms=recovery.clock[0] - 1,
                    command_sequence=0,
                )
            )

    async def dispatch(action, values, operation_id, actor_id):
        recovery.management.calls.append((action, values, operation_id, actor_id))
        if action == "prepare_execution":
            return {"runtime_id": operation_id}
        return {}

    recovery.management.dispatch = dispatch
    await recovery.automatic.reconcile()
    assert sum(call[0] == "recover_project" for call in recovery.management.calls) == 16
    await recovery.automatic.reconcile()
    assert sum(call[0] == "recover_project" for call in recovery.management.calls) == 17
