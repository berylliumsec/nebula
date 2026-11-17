"""Expired-owner initiation uses SQL receipts, never a speculative spawn retry."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    ProjectAdmissionHoldRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
    RuntimeResourceRow,
)
from nebula.v3.domain import ChatSession, Engagement
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
        self.abort_starts = 0
        self.reject_recover = 0
        # Runtime IDs successive preparations return, then "prepared-target".
        self.prepare_ids = []
        # (pid, birth) the prepared worker process row and spawn checkpoint
        # record; the default names no live process.
        self.worker_process = (12345, "synthetic-birth")
        # "after_commit" or "before_commit": lose the reconciliation reply.
        self.lose_reconciliation = None

    def ledger(self):
        return self.operations

    async def dispatch(self, action, values, operation_id, actor_id):
        self.calls.append((action, values, operation_id, actor_id))
        if action == "reconcile_project":
            if self.reject_reconciliation:
                raise OperationRejected("OS absence could not be confirmed")
            lost, self.lose_reconciliation = self.lose_reconciliation, None
            if lost == "before_commit":
                raise OSError("connection reset before the commit")
            if lost == "after_commit":
                from nebula.v3.database import DeploymentReceiptRow
                from nebula.v3.runtime_coordination import ProjectLease
                from nebula.v3.runtime_orphan_recovery import orphan_receipt_key

                async with self.coordinator.coordination._write() as session:
                    session.add(
                        DeploymentReceiptRow(
                            id=orphan_receipt_key(
                                ProjectLease(**values["expected_owner"])
                            ),
                            action="project.orphans_reconciled",
                            result={"reconciled": True},
                            occurred_at_ms=1,
                        )
                    )
                raise OSError("connection reset after the commit")
            return {"reconciled": True}
        if action == "prepare_execution":
            if self.abort_starts:
                from nebula.v3.runtime_coordination import StartAborted

                self.abort_starts -= 1
                raise StartAborted("The worker did not become ready; nothing started")
            from nebula.v3.deployment_recovery import checkpoint
            from nebula.v3.process_identity import process_origin

            runtime_id = (
                self.prepare_ids.pop(0) if self.prepare_ids else "prepared-target"
            )
            # As the host's spawn does: the reservation, then the bound child.
            await checkpoint(
                phase="worker_spawn_intent",
                runtime_id=runtime_id,
                version=values["version"],
                role="execution",
            )
            await self.coordinator.coordination.register_runtime(
                runtime_id, values["version"]
            )
            pid, birth = self.worker_process
            async with self.coordinator.database.session() as session:
                row = process(runtime_id, values["version"])
                row.pid, row.process_birth = pid, birth
                session.add(row)
                if self.change_handoff:
                    handoff = await session.get(ProjectHandoffRow, "one")
                    handoff.revision += 1
            await checkpoint(
                phase="worker_spawned",
                children=[
                    {
                        "pid": pid,
                        "birth": birth,
                        "kind": "worker",
                        "process_origin": process_origin(),
                    }
                ],
                runtime_id=runtime_id,
                endpoint="unix:///synthetic/" + runtime_id + ".sock",
            )
            if self.lose_prepare:
                raise RuntimeError("The spawn result was lost")
            return {"runtime_id": runtime_id}
        assert action == "recover_project"
        if self.reject_recover:
            self.reject_recover -= 1
            raise OperationRejected("The recovery target did not answer (OSError)")
        return {
            "project_id": values["project_id"],
            "runtime_id": values["target_runtime_id"],
            "fencing_token": values["expected_owner"]["fencing_token"] + 1,
        }


@pytest.fixture
async def recovery(host, monkeypatch):
    coordinator, store = host
    clock = [1_000_000]
    await store.create(
        ChatSession(
            engagement_id="one",
            title="Open recovery fixture",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )

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


async def test_expired_positive_dead_owner_initiates_promoted_target_version(recovery):
    await recovery.automatic.reconcile()
    calls = recovery.management.calls
    assert [call[0] for call in calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    assert calls[0][1] == {"expected_owner": expected_owner(recovery)}
    assert calls[1][1] == {"version": "v2"}
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


async def test_expired_owner_with_only_archived_chat_is_not_recovered(recovery):
    (chat,) = await recovery.store.find_entities(
        ChatSession, {}, engagement_id="one", limit=1
    )
    await recovery.store.update(
        ChatSession,
        chat.id,
        {"metadata": {"archived_at": "2026-11-13T00:00:00+00:00"}},
        expected_revision=chat.revision,
    )

    await recovery.automatic._recover_expired()

    async with recovery.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.runtime_id == recovery.lease.runtime_id
        assert owner.fencing_token == recovery.lease.fencing_token
    assert recovery.management.calls == []


async def archive_open_chat(recovery):
    (chat,) = await recovery.store.find_entities(
        ChatSession, {}, engagement_id="one", limit=1
    )
    await recovery.store.update(
        ChatSession,
        chat.id,
        {"metadata": {"archived_at": "2026-11-13T00:00:00+00:00"}},
        expected_revision=chat.revision,
    )


async def project_command(recovery, state):
    """A project-routed write, such as a note or rename, with no chat start."""
    receipt = await recovery.coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="synthetic-project-write",
        name="http.invoke/v1",
        payload={"format": 1, "request_reference": "synthetic-reference"},
    )
    if state != "queued":
        async with recovery.coordinator.database.session() as session:
            row = await session.get(RuntimeCommandRow, receipt.command_id)
            row.state = state
            row.runtime_id = recovery.lease.runtime_id
            row.fencing_token = recovery.lease.fencing_token
    return receipt


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("project_status", ["active", "archived"])
async def test_expired_owner_with_only_project_work_is_recovered(
    recovery, state, project_status
):
    # A project with no open chat still has a worker admitted for its queued
    # work. If that worker dies, the work must not wait forever for a worker
    # that only a chat would bring back.
    await archive_open_chat(recovery)
    if project_status == "archived":
        # An archived project's delete or unarchive is still delivered.
        await recovery.store.update(Engagement, "one", {"status": "archived"})
    await project_command(recovery, state)

    await recovery.automatic.reconcile()

    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    assert recovery.management.calls[2][1]["expected_owner"] == expected_owner(recovery)


@pytest.mark.parametrize("state", ["uncertain", "succeeded"])
async def test_expired_owner_without_unfinished_project_work_is_not_recovered(
    recovery, state
):
    await archive_open_chat(recovery)
    await project_command(recovery, state)

    await recovery.automatic.reconcile()

    assert recovery.management.calls == []


async def test_recovery_keeps_source_version_without_a_promoted_target(recovery):
    async with recovery.coordinator.database.session() as session:
        host = await session.get(DeploymentHostRow, "host")
        host.target_version = None
    await recovery.automatic.reconcile()
    prepared = next(
        call for call in recovery.management.calls if call[0] == "prepare_execution"
    )
    assert prepared[1] == {"version": "v1"}


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


async def test_refused_recovery_is_asked_again_later_with_the_same_operation(
    recovery,
):
    recovery.management.reject_reconciliation = True
    await recovery.automatic.reconcile()
    [refused] = await operations(recovery)
    assert refused.state == "failed"
    # Within the retry interval the refusal is not dispatched again.
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == ["reconcile_project"]
    # Later, the same deterministic operation is asked again and can succeed.
    recovery.management.reject_reconciliation = False
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    calls = [call[0] for call in recovery.management.calls]
    assert calls[:2] == ["reconcile_project", "reconcile_project"]
    assert recovery.management.calls[1][2] == refused.operation_id
    rows = {row.operation_id: row for row in await operations(recovery)}
    assert rows[refused.operation_id].state == "succeeded"


async def test_reconciliation_reply_lost_after_commit_continues_recovery(recovery):
    recovery.management.lose_reconciliation = "after_commit"
    await recovery.automatic.reconcile()
    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["reconcile_project"].state == "succeeded"
    assert rows["reconcile_project"].result["dispatch_error_type"] == "OSError"
    assert not recovery.diagnostics


async def test_uncertain_reconciliation_without_effect_is_asked_again(recovery):
    recovery.management.lose_reconciliation = "before_commit"
    await recovery.automatic.reconcile()
    [lost] = await operations(recovery)
    assert lost.state == "uncertain"
    # The next tick settles it from its absent durable receipt, never by
    # dispatching it again; the refused-operation backoff then applies.
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == ["reconcile_project"]
    [settled] = await operations(recovery)
    assert settled.state == "failed" and settled.result["reconciled"] is True
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["reconcile_project"].state == "succeeded"


async def test_idle_unassigned_execution_neither_blocks_nor_is_reused(recovery):
    """A stray unowned worker holding nothing (an abandoned deployment target,
    a source not yet retired) cannot block recovery of an unrelated project;
    recovery prepares and attaches only its own ledgered target."""
    async with recovery.coordinator.database.session() as session:
        session.add(process("unknown-target"))
    await recovery.automatic.reconcile()
    calls = recovery.management.calls
    assert [call[0] for call in calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    assert calls[2][1]["target_runtime_id"] == "prepared-target"
    async with recovery.coordinator.database.session() as session:
        stray = await session.get(RuntimeProcessRow, "unknown-target")
        assert stray is not None and stray.state == "ready"


def stray_terminal(project_id):
    return RuntimeResourceRow(
        id="terminal-" + project_id,
        project_id=project_id,
        runtime_id="unknown-target",
        fencing_token=1,
        kind="terminal",
        state="open",
        restoration="none",
        detail="A terminal the stray worker still holds",
        checkpoint={},
        updated_at_ms=1_000_000,
    )


async def test_unassigned_execution_holding_project_effects_blocks_recovery(
    recovery,
):
    async with recovery.coordinator.database.session() as session:
        session.add(process("unknown-target"))
        session.add(stray_terminal("one"))
    await recovery.automatic.reconcile()
    assert [call[0] for call in recovery.management.calls] == ["reconcile_project"]
    assert len(await operations(recovery)) == 1
    assert "unknown-target still holds project resources" in str(
        recovery.diagnostics[-1]
    )


async def test_another_projects_leftover_does_not_block_recovery(recovery):
    """A dead former owner of another project that still holds that
    project's effects is that project's reconciliation, not this one's."""
    async with recovery.coordinator.database.session() as session:
        session.add(process("unknown-target"))
        session.add(stray_terminal("two"))
    await recovery.automatic.reconcile()
    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]


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


async def admission_hold(recovery):
    async with recovery.coordinator.database.session() as session:
        row = await session.get(ProjectAdmissionHoldRow, "one")
        return row.reason if row is not None else None


async def test_repeated_recoveries_back_off_and_never_stop(recovery, monkeypatch):
    # Three recoveries just now: the next waits 60 s, then runs. It is never
    # held for an operator, and the wait is visible and reported once.
    noted = []
    monkeypatch.setattr(
        "nebula.v3.runtime_auto_admission.record_diagnostic",
        lambda *args, **kwargs: noted.append((args, kwargs)),
    )
    await history(recovery, count=3)
    await recovery.automatic.reconcile()
    await recovery.automatic.reconcile()
    await recovery.controller().reconcile()
    assert not recovery.management.calls
    assert not recovery.diagnostics
    assert [args[2] for args, _ in noted] == ["runtime.project_recovery.backoff"] * 2
    reason = await admission_hold(recovery)
    assert "stopped 3 times" in reason and "about 60 s" in reason
    # A fresh controller (a Core restart) reads the same backoff from SQL.
    recovery.clock[0] += 59_000
    await recovery.controller().reconcile()
    assert not recovery.management.calls
    recovery.clock[0] += 1_001
    await recovery.controller().reconcile()
    assert [call[0] for call in recovery.management.calls] == [
        "reconcile_project",
        "prepare_execution",
        "recover_project",
    ]
    assert await admission_hold(recovery) is None


async def test_crash_loop_backoff_is_capped_at_five_minutes(recovery):
    # However often the project failed, the next recovery is at most five
    # minutes after the last one, so a failure that stops is soon recovered.
    await history(recovery, count=40)
    await recovery.automatic.reconcile()
    recovery.clock[0] += 5 * 60_000 - 1
    await recovery.controller().reconcile()
    assert not recovery.management.calls
    assert "stopped 40 times" in await admission_hold(recovery)
    recovery.clock[0] += 2
    await recovery.controller().reconcile()
    assert len(recovery.management.calls) == 3


async def test_recovery_backoff_runs_from_the_latest_recovery(recovery):
    # Only recent recoveries count, and the wait runs from the newest one.
    await history(recovery, count=1)
    recovery.clock[0] += 14_000
    await recovery.controller().reconcile()
    assert not recovery.management.calls
    recovery.clock[0] += 1_000
    await recovery.controller().reconcile()
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
        await recovery.store.create(
            ChatSession(
                engagement_id=project_id,
                title="Open recovery fixture " + project_id,
                provider_profile_id="synthetic-provider",
                model="synthetic-model",
            )
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
            async with recovery.coordinator.database.session() as session:
                session.add(process(operation_id))
            return {"runtime_id": operation_id}
        return {}

    recovery.management.dispatch = dispatch
    await recovery.automatic.reconcile()
    assert sum(call[0] == "recover_project" for call in recovery.management.calls) == 16
    await recovery.automatic.reconcile()
    assert sum(call[0] == "recover_project" for call in recovery.management.calls) == 17


async def test_aborted_recovery_spawn_is_retried_with_backoff(recovery):
    recovery.management.abort_starts = 2
    await recovery.automatic.reconcile()
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "failed"

    async def tick(advance_ms):
        recovery.clock[0] += advance_ms
        controller = recovery.controller()
        await controller.reconcile()

    def prepares():
        return [c for c in recovery.management.calls if c[0] == "prepare_execution"]

    await tick(31_000)
    assert len(prepares()) == 2  # first retry after 30 s, aborted again
    await tick(31_000)
    assert len(prepares()) == 2  # the second retry waits 60 s
    await tick(31_000)
    assert len(prepares()) == 3
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "succeeded"
    assert rows["recover_project"].state == "succeeded"


@pytest.mark.parametrize("retired", [True, False])
async def test_unresponsive_live_owner_is_retired_then_recovered(recovery, retired):
    # A frozen or hung owner: its process lives but its control channel fails.
    worker = recovery.observed[0]
    worker.process_state, worker.reachable = "alive", False
    worker.identity = SimpleNamespace(version="v1", role="execution")
    asked = []

    async def retire_unresponsive(identity):
        asked.append(identity)
        return retired

    recovery.automatic.supervisor.host.retire_unresponsive = retire_unresponsive

    await recovery.automatic.reconcile()

    assert asked == [worker.identity]
    calls = [call[0] for call in recovery.management.calls]
    if retired:
        assert calls == ["reconcile_project", "prepare_execution", "recover_project"]
    else:
        assert calls == []


@pytest.mark.parametrize("variant", ["reachable", "api", "unknown"])
async def test_reachable_api_or_unverified_owner_is_never_force_retired(
    recovery, variant
):
    worker = recovery.observed[0]
    worker.process_state = "unknown" if variant == "unknown" else "alive"
    worker.reachable = variant == "reachable"
    worker.identity = SimpleNamespace(
        version="v1", role="api" if variant == "api" else "execution"
    )
    asked = []

    async def retire_unresponsive(identity):
        asked.append(identity)
        return True

    recovery.automatic.supervisor.host.retire_unresponsive = retire_unresponsive

    await recovery.automatic.reconcile()

    assert asked == []
    assert recovery.management.calls == []


def prepares(recovery):
    return [
        call for call in recovery.management.calls if call[0] == "prepare_execution"
    ]


def recovers(recovery):
    return [call for call in recovery.management.calls if call[0] == "recover_project"]


async def test_dead_prepared_target_is_replaced_by_a_fresh_preparation(recovery):
    # The prepared target died before recover_project claimed it. Asking
    # again with the saved target would be refused forever; a fresh
    # preparation under its own operation identity takes its place.
    recovery.management.reject_recover = 1
    recovery.management.prepare_ids = ["prepared-target", "fresh-target"]
    await recovery.automatic.reconcile()
    assert [call[1]["target_runtime_id"] for call in recovers(recovery)] == [
        "prepared-target"
    ]
    recovery.observed.append(
        SimpleNamespace(
            runtime_id="prepared-target",
            process_state="gone",
            identity=SimpleNamespace(version="v2"),
        )
    )
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert len(prepares(recovery)) == 2
    assert len({call[2] for call in prepares(recovery)}) == 2
    assert [call[1]["target_runtime_id"] for call in recovers(recovery)] == [
        "prepared-target",
        "fresh-target",
    ]
    assert recovers(recovery)[0][2] != recovers(recovery)[1][2]
    # Replayed passes never dispatch either preparation again.
    await recovery.controller().reconcile()
    assert len(prepares(recovery)) == 2


async def test_retired_prepared_target_is_replaced(recovery):
    recovery.management.reject_recover = 1
    recovery.management.prepare_ids = ["prepared-target", "fresh-target"]
    await recovery.automatic.reconcile()
    async with recovery.coordinator.database.session() as session:
        (await session.get(RuntimeProcessRow, "prepared-target")).state = "retired"
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert recovers(recovery)[-1][1]["target_runtime_id"] == "fresh-target"


async def test_live_prepared_target_is_kept_after_a_refused_claim(recovery):
    recovery.management.reject_recover = 1
    await recovery.automatic.reconcile()
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert len(prepares(recovery)) == 1
    assert [call[1]["target_runtime_id"] for call in recovers(recovery)] == [
        "prepared-target",
        "prepared-target",
    ]


async def host_died(recovery):
    """The host that dispatched the saved operations is gone (a restart)."""
    async with recovery.coordinator.database.session() as session:
        for row in await session.scalars(select(DeploymentOperationRow)):
            origin = dict((row.result or {}).get("recovery") or {})
            if origin:
                origin["pid"] = 2**22 + 12345
                row.result = {**row.result, "recovery": origin}
    recovery.management.operations = DeploymentOperations(recovery.coordinator)


async def test_uncertain_prepare_of_a_verified_worker_settles_and_recovers(
    recovery,
):
    # The host lost the preparation reply after its worker became ready (for
    # example it died). The spawn checkpoint and process row prove the
    # worker, so the receipt settles and recovery claims that same worker.
    import os

    from nebula.v3.database import RuntimeInstanceRow
    from nebula.v3.process_identity import process_birth

    recovery.management.worker_process = (os.getpid(), process_birth(os.getpid()))
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "uncertain"
    async with recovery.coordinator.database.session() as session:
        instance = await session.get(RuntimeInstanceRow, "prepared-target")
        instance.lease_expires_at_ms = recovery.clock[0] + 60_000
    await host_died(recovery)
    await recovery.controller().reconcile()
    assert len(prepares(recovery)) == 1
    assert [call[1]["target_runtime_id"] for call in recovers(recovery)] == [
        "prepared-target"
    ]
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "succeeded"


async def test_uncertain_prepare_of_a_retired_reservation_is_prepared_again(
    recovery,
):
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    async with recovery.coordinator.database.session() as session:
        (await session.get(RuntimeProcessRow, "prepared-target")).state = "retired"
    await host_died(recovery)
    recovery.management.lose_prepare = False
    recovery.management.prepare_ids = ["fresh-target"]
    await recovery.controller().reconcile()
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "failed"
    recovery.clock[0] += 31_000
    await recovery.controller().reconcile()
    assert len(prepares(recovery)) == 2
    assert recovers(recovery)[-1][1]["target_runtime_id"] == "fresh-target"


async def test_uncertain_prepare_of_an_unconfirmed_started_worker_settles(recovery):
    # The host died after the bound-child checkpoint and before it recorded
    # the worker ready: the worker runs unowned in "starting". Its exact
    # incarnation answers the identity probe, so readiness is recorded as
    # the spawn would have, the receipt settles and recovery claims it.
    import os

    from nebula.v3.database import RuntimeInstanceRow
    from nebula.v3.process_identity import process_birth

    pid, birth = os.getpid(), process_birth(os.getpid())
    recovery.management.worker_process = (pid, birth)
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    async with recovery.coordinator.database.session() as session:
        (await session.get(RuntimeProcessRow, "prepared-target")).state = "starting"
        instance = await session.get(RuntimeInstanceRow, "prepared-target")
        instance.lease_expires_at_ms = recovery.clock[0] + 60_000
    await host_died(recovery)
    probes = []

    class Client:
        async def call(self, name, payload, timeout):
            probes.append(name)
            return {
                "runtime_id": "prepared-target",
                "version": "v2",
                "role": "execution",
                "pid": pid,
                "process_birth": birth,
                "endpoint": "unix:///synthetic/prepared-target.sock",
            }

        async def close(self):
            pass

    async def rpc(runtime_id):
        assert runtime_id == "prepared-target"
        return Client()

    recovery.automatic.supervisor.host.rpc = rpc
    await recovery.controller().reconcile()
    assert probes == ["worker.identity"]
    assert len(prepares(recovery)) == 1
    assert [call[1]["target_runtime_id"] for call in recovers(recovery)] == [
        "prepared-target"
    ]
    async with recovery.coordinator.database.session() as session:
        assert (
            await session.get(RuntimeProcessRow, "prepared-target")
        ).state == "ready"


async def test_uncertain_prepare_of_an_unanswering_started_worker_waits(recovery):
    import os

    from nebula.v3.process_identity import process_birth

    recovery.management.worker_process = (os.getpid(), process_birth(os.getpid()))
    recovery.management.lose_prepare = True
    await recovery.automatic.reconcile()
    async with recovery.coordinator.database.session() as session:
        (await session.get(RuntimeProcessRow, "prepared-target")).state = "starting"
    await host_died(recovery)

    async def rpc(runtime_id):
        raise OSError("synthetic worker socket is not answering")

    recovery.automatic.supervisor.host.rpc = rpc
    await recovery.controller().reconcile()
    # Nothing is re-dispatched and nothing is claimed while it is unproven.
    assert len(prepares(recovery)) == 1 and not recovers(recovery)
    rows = {row.action: row for row in await operations(recovery)}
    assert rows["prepare_execution"].state == "uncertain"
