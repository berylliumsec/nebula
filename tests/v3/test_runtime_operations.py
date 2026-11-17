"""Management retries retain receipts without repeating host effects."""

import asyncio
import pytest
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_operations import DeploymentOperations
from nebula.v3.database import DeploymentOperationRow
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_operation_retry_retains_immutable_receipt(host):
    ledger, calls = DeploymentOperations(host[0]), []

    async def dispatch(action, parameters):
        calls.append(action)
        return {"version": "v1"}

    args = dict(
        operation_id="op",
        actor_id="operator",
        action="stage",
        parameters={"version": "v1"},
        dispatch=dispatch,
    )
    first = await ledger.execute(**args)
    assert first == await ledger.execute(**args)
    assert first["state"] == "succeeded" and calls == ["stage"]
    assert (
        await DeploymentOperations(host[0]).status("op", actor_id="operator") == first
    )
    with pytest.raises(CoordinationConflict):
        await ledger.execute(**{**args, "parameters": {"version": "v2"}})


async def test_in_flight_retry_does_not_dispatch_twice(host):
    ledger = DeploymentOperations(host[0])
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(action, parameters):
        entered.set()
        await release.wait()
        return {"done": True}

    args = dict(
        operation_id="op",
        actor_id="operator",
        action="stage",
        parameters={},
        dispatch=dispatch,
    )
    task = asyncio.create_task(ledger.execute(**args))
    await entered.wait()
    assert (await ledger.execute(**args))["state"] == "running"
    release.set()
    assert (await task)["state"] == "succeeded"


async def test_failed_dispatch_remains_uncertain_after_replacement(host):
    calls = []

    async def dispatch(action, parameters):
        calls.append(action)
        raise OSError("lost response after a possible effect")

    args = dict(
        operation_id="op",
        actor_id="operator",
        action="promote_api",
        parameters={},
        dispatch=dispatch,
    )
    assert (await DeploymentOperations(host[0]).execute(**args))["state"] == "uncertain"
    assert (await DeploymentOperations(host[0]).execute(**args))["state"] == "uncertain"
    assert calls == ["promote_api"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeIpcError(
            'Project "Synthetic" (project): Project cleanup is unverified; '
            "its ownership is retained"
        ),
        CoordinationConflict("deployment changed before shutdown"),
    ],
)
async def test_uncertain_receipt_keeps_a_refusal_reason(host, error):
    async def dispatch(_action, _parameters):
        raise error

    receipt = await DeploymentOperations(host[0]).execute(
        operation_id="refused",
        actor_id="operator",
        action="shutdown",
        parameters={"expected_revision": 1},
        dispatch=dispatch,
    )
    assert receipt["state"] == "uncertain"
    assert receipt["result"] == {
        "detail": "The operation did not report a confirmed result. Read deployment status before reconciliation.",
        "error_type": type(error).__name__,
        "reason": str(error),
    }


async def test_uncertain_receipt_never_copies_unclassified_error_text(host):
    async def dispatch(_action, _parameters):
        raise OSError("synthetic-private-path")

    receipt = await DeploymentOperations(host[0]).execute(
        operation_id="lost",
        actor_id="operator",
        action="shutdown",
        parameters={"expected_revision": 1},
        dispatch=dispatch,
    )
    assert receipt["state"] == "uncertain"
    assert "reason" not in receipt["result"]
    assert "synthetic-private-path" not in str(receipt)


async def test_get_after_host_replacement_classifies_abandoned_operation_without_dispatch(
    host,
):
    coordinator = host[0]
    async with coordinator.database.session() as session:
        session.add(
            DeploymentOperationRow(
                operation_id="abandoned",
                actor_id="operator",
                action="stage",
                request_hash="a" * 64,
                parameters={},
                state="running",
                claim_owner="old-host",
                result=None,
                created_at_ms=1,
                updated_at_ms=1,
            )
        )
    old = DeploymentOperations(coordinator, owner_id="old-host")
    new = DeploymentOperations(coordinator, owner_id="new-host")
    assert (await old.status("abandoned", actor_id="operator"))["state"] == "running"
    with pytest.raises(CoordinationConflict):
        await new.status("abandoned", actor_id="unrelated")
    assert (await old.status("abandoned", actor_id="operator"))["state"] == "running"
    first = await new.status("abandoned", actor_id="operator")
    assert first["state"] == "uncertain"
    assert await new.status("abandoned", actor_id="operator") == first


async def test_unconfirmed_operation_is_diagnosed_without_its_text(
    host, tmp_path, monkeypatch
):
    import json

    from nebula.v3 import diagnostics
    from nebula.v3.diagnostics import DiagnosticManager

    manager = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    monkeypatch.setattr(diagnostics, "_manager", manager)
    try:

        async def dispatch(_action, _parameters):
            raise OSError("synthetic-private-path")

        await DeploymentOperations(host[0]).execute(
            operation_id="lost",
            actor_id="operator",
            action="shutdown",
            parameters={"expected_revision": 1},
            dispatch=dispatch,
        )
        assert manager.flush()
        encoded = (manager.log_dir / "errors.log").read_text()
    finally:
        manager.close()
    assert "synthetic-private-path" not in encoded
    events = [json.loads(line) for line in encoded.splitlines()]
    assert [event["event_code"] for event in events] == [
        "runtime.deployment_operation.unconfirmed"
    ]


async def test_rejected_operation_is_asked_again_only_when_opted_in(host):
    from sqlalchemy import select

    from nebula.v3.database import DeploymentReceiptRow
    from nebula.v3.runtime_coordination import OperationRejected

    calls = []

    async def dispatch(action, parameters):
        calls.append((action, parameters))
        if len(calls) == 1:
            raise OperationRejected("handoff changed; read fresh recovery state")
        return {"recovered": True}

    args = dict(
        operation_id="auto-recovery:synthetic",
        actor_id="automatic-recovery",
        action="recover_project",
        parameters={"project_id": "one", "expected_handoff_revision": 1},
        dispatch=dispatch,
    )
    ledger = DeploymentOperations(host[0])
    first = await ledger.execute(**args)
    assert first["state"] == "failed"
    # An ordinary replay keeps the saved receipt, as deployment journals expect.
    assert await ledger.execute(**args) == first and len(calls) == 1
    # Automatic recovery asks again with fresh parameters under the same ID.
    fresh = {
        **args,
        "parameters": {"project_id": "one", "expected_handoff_revision": 2},
    }
    second = await DeploymentOperations(host[0]).execute(**fresh, retry_rejected=True)
    assert second["state"] == "succeeded"
    assert calls[1] == ("recover_project", fresh["parameters"])
    assert await ledger.execute(**fresh, retry_rejected=True) == second
    assert len(calls) == 2
    async with host[0].database.session() as session:
        [audit] = list(
            await session.scalars(
                select(DeploymentReceiptRow).where(
                    DeploymentReceiptRow.action == "operation.retried"
                )
            )
        )
    assert audit.result["operation_id"] == "auto-recovery:synthetic"
    assert audit.result["previous_result"]["detail"] == (
        "handoff changed; read fresh recovery state"
    )


async def test_uncertain_operation_is_never_asked_again(host):
    calls = []

    async def dispatch(action, _parameters):
        calls.append(action)
        raise OSError("lost reply")

    args = dict(
        operation_id="auto-recovery:lost",
        actor_id="automatic-recovery",
        action="prepare_execution",
        parameters={"version": "v1"},
        dispatch=dispatch,
        retry_rejected=True,
    )
    assert (await DeploymentOperations(host[0]).execute(**args))["state"] == "uncertain"
    assert (await DeploymentOperations(host[0]).execute(**args))["state"] == "uncertain"
    assert calls == ["prepare_execution"]


async def test_stale_deployment_revisions_are_failed_before_any_effect(host):
    from nebula.v3.runtime_coordination import DeploymentChanged

    coordinator = host[0]
    revision = (await coordinator.status()).revision
    stale = revision - 1
    with pytest.raises(DeploymentChanged):
        await coordinator.promote_api("absent-api", expected_revision=stale)
    with pytest.raises(DeploymentChanged):
        await coordinator.rollback_code("v1", expected_revision=stale)
    with pytest.raises(DeploymentChanged):
        await coordinator.migration_preflight("v2", expected_revision=stale)
    assert (await coordinator.status()).revision == revision


async def test_execution_prepare_without_reservation_is_a_failed_receipt():
    from types import SimpleNamespace
    from nebula.v3.supervisor import RuntimeSupervisor
    from nebula.v3.runtime_coordination import OperationRejected

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            from tests.v3.test_deployment_lifecycle import manifest

            return SimpleNamespace(manifest=manifest("v2").model_dump(mode="json"))

    async def admit(*args):
        pass

    async def spawn(*args):
        return None

    root = RuntimeSupervisor.__new__(RuntimeSupervisor)
    root.started, root.closed, root._shutdown_committed = True, False, False
    root._operations = asyncio.Lock()

    async def blocker(version):
        return f"Version v1 is kept resident by 1 project(s): Apple ({version})"

    root.coordinator = SimpleNamespace(
        database=SimpleNamespace(session=Session),
        execution_capacity_blocker=blocker,
    )
    root.worker_admission = admit
    root.host = SimpleNamespace(spawn=spawn)
    with pytest.raises(OperationRejected, match="remains staged") as refused:
        await root.prepare_execution("v2")
    # The per-project switch refusal names what keeps the slot.
    assert "kept resident by 1 project(s): Apple (v2)" in str(refused.value)


async def test_code_rollback_partial_effect_is_not_a_definite_refusal():
    from types import SimpleNamespace
    from nebula.v3.supervisor import RuntimeSupervisor
    from nebula.v3.runtime_coordination import OperationRejected

    changed = []

    async def change_target(*args, **kwargs):
        changed.append("target")

    async def refuse(*args):
        raise OperationRejected("candidate refused")

    root = RuntimeSupervisor.__new__(RuntimeSupervisor)
    root.started, root.closed, root._shutdown_committed = True, False, False
    root._operations = asyncio.Lock()
    root.coordinator = SimpleNamespace(rollback_code=change_target)
    root.prepare_api = refuse
    with pytest.raises(CoordinationConflict) as error:
        await root.rollback_code("v1", expected_revision=1)
    assert not isinstance(error.value, OperationRejected)
    assert changed == ["target"]


async def test_process_inventory_distinguishes_missing_protocol_support(host):
    from nebula.v3.deployment_protocol import DeploymentStatus

    result = (await host[0].status()).model_dump(mode="json")
    assert result["processes"] == []
    del result["processes"]
    assert DeploymentStatus.model_validate(result).processes is None


async def test_incompatible_manifest_is_a_definite_refusal_without_revision_change(
    host,
):
    from types import SimpleNamespace
    from nebula.v3.runtime_coordination import OperationRejected

    coordinator = host[0]
    revision = (await coordinator.status()).revision
    async with coordinator.database.session() as session:
        with pytest.raises(OperationRejected, match="SQL schema"):
            await coordinator._compatible(
                session, SimpleNamespace(schema_min=999), role="api"
            )
    assert (await coordinator.status()).revision == revision


async def test_reconciliation_rejects_active_invocation_and_preserves_original_effect(
    host,
):
    ledger = DeploymentOperations(host[0])
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(_action, _parameters):
        entered.set()
        await release.wait()
        raise OSError("synthetic uncertain completion")

    task = asyncio.create_task(
        ledger.execute(
            operation_id="active-prepare",
            actor_id="operator",
            action="prepare_execution",
            parameters={"version": "v2"},
            dispatch=dispatch,
        )
    )
    await entered.wait()
    with pytest.raises(CoordinationConflict, match="still active"):
        await ledger.reconcile("active-prepare", actor_id="operator")
    release.set()
    assert (await task)["state"] == "uncertain"
    assert (await ledger.reconcile("active-prepare", actor_id="operator"))[
        "state"
    ] == "failed"


async def test_reconciliation_keeps_legacy_unknown_and_live_migration_child(host):
    import os
    from nebula.v3.deployment_recovery import checkpoint
    from nebula.v3.process_identity import process_birth, process_origin

    ledger = DeploymentOperations(host[0])

    async def dispatch(_action, _parameters):
        await checkpoint(
            phase="migration_admitted",
            children=[
                {
                    "kind": "migration",
                    "pid": os.getpid(),
                    "birth": process_birth(os.getpid()),
                    "process_origin": process_origin(),
                }
            ],
        )
        raise OSError("synthetic migration uncertainty")

    await ledger.execute(
        operation_id="migration-child",
        actor_id="operator",
        action="migrate",
        parameters={"version": "v2"},
        dispatch=dispatch,
    )
    with pytest.raises(CoordinationConflict, match="still running"):
        await ledger.reconcile("migration-child", actor_id="operator")
    async with host[0].coordination._write() as session:
        row = await session.get(DeploymentOperationRow, "migration-child")
        row.result = {"detail": "legacy unknown"}
    with pytest.raises(CoordinationConflict, match="Legacy"):
        await ledger.reconcile("migration-child", actor_id="operator")


async def test_reconciliation_requires_exact_linked_ready_worker(host):
    from nebula.v3.deployment_recovery import checkpoint
    from tests.v3.test_deployment_lifecycle import worker

    # worker() spawns only an inert OS sleep process, requested separately by
    # process-level selection; it establishes kernel identity for this guard.
    identity = await worker(host, "v2", role="api")
    ledger = DeploymentOperations(host[0])

    async def dispatch(_action, _parameters):
        async with host[0].database.session() as session:
            from nebula.v3.database import RuntimeProcessRow

            process = await session.get(RuntimeProcessRow, identity)
            from nebula.v3.process_identity import process_origin

            child = {
                "kind": "worker",
                "pid": process.pid,
                "birth": process.process_birth,
                "process_origin": process_origin(),
            }
        await checkpoint(
            phase="worker_spawned",
            runtime_id=identity,
            children=[child],
            endpoint=process.endpoint,
        )
        raise OSError("lost worker receipt")

    await ledger.execute(
        operation_id="linked",
        actor_id="operator",
        action="prepare_api",
        parameters={"version": "v2"},
        dispatch=dispatch,
    )
    result = await ledger.reconcile("linked", actor_id="operator")
    assert result["state"] == "succeeded"
    assert result["result"]["runtime_id"] == identity
    assert "recovery" not in result["result"]
    assert await ledger.reconcile("linked", actor_id="operator") == result


async def test_dead_owner_before_bootstrap_can_reconcile_without_replaying(
    host, monkeypatch
):
    from nebula.v3.deployment_recovery import checkpoint
    import nebula.v3.runtime_operations as operations

    old = DeploymentOperations(host[0], owner_id="old-host")

    async def dispatch(_action, _parameters):
        await checkpoint(phase="migration_spawn_intent", version="v2")
        raise OSError("synthetic owner exit before bootstrap")

    await old.execute(
        operation_id="prebootstrap",
        actor_id="operator",
        action="migrate",
        parameters={"version": "v2"},
        dispatch=dispatch,
    )
    fresh = DeploymentOperations(host[0], owner_id="new-host")
    monkeypatch.setattr(operations, "same_process", lambda *args: False)
    result = await fresh.reconcile("prebootstrap", actor_id="operator")
    assert result["state"] == "failed"
    assert result["result"]["reconciled"] is True


async def test_reconcile_holds_foreign_same_boot_namespace(host, monkeypatch):
    from nebula.v3.deployment_recovery import checkpoint
    import nebula.v3.runtime_operations as operations

    ledger = DeploymentOperations(host[0])

    async def dispatch(_action, _parameters):
        await checkpoint(phase="migration_spawn_intent")
        raise OSError("inert prebootstrap uncertainty")

    await ledger.execute(
        operation_id="foreign",
        actor_id="operator",
        action="migrate",
        parameters={"version": "v2"},
        dispatch=dispatch,
    )
    recorded = operations.process_origin()
    assert recorded is not None
    foreign = recorded.rsplit(":", 1)[0] + ":999999999"
    monkeypatch.setattr(operations, "process_origin", lambda: foreign)
    monkeypatch.setattr(operations, "same_process", lambda *args: False)
    with pytest.raises(CoordinationConflict, match="namespace"):
        await ledger.reconcile("foreign", actor_id="operator")


async def _saved_retirement(host):
    from nebula.v3.database import (
        RuntimeInstanceRow,
        RuntimeProcessRow,
        DeploymentReceiptRow,
    )
    from nebula.v3.process_identity import process_birth
    import os

    coordinator = host[0]
    runtime_id = await coordinator.reserve_worker(
        "v1",
        role="execution",
        endpoint="unix:///isolated/retired.sock",
        credential_reference="fake-reference",
    )
    async with coordinator.coordination._write() as session:
        row = await session.get(RuntimeProcessRow, runtime_id)
        row.pid = 999999999
        row.process_birth = process_birth(os.getpid())
        row.state = "retired"
        identity = dict(
            runtime_id=runtime_id,
            version=row.version,
            role=row.role,
            pid=row.pid,
            process_birth=row.process_birth,
            endpoint=row.endpoint,
        )
        instance = await session.get(RuntimeInstanceRow, runtime_id)
        instance.lease_expires_at_ms = 0
        session.add(
            DeploymentReceiptRow(
                id="retired-proof",
                action="worker.retired",
                result=identity,
                occurred_at_ms=1,
            )
        )
    return identity


@pytest.mark.parametrize(
    "guard",
    [
        None,
        "missing_process",
        "missing_instance",
        "live_lease",
        "wrong_version",
        "wrong_role",
        "identity_mismatch",
        "missing_receipt",
        "live_process",
        "owner",
        "resource",
        "command",
        "capacity",
        "foreign_namespace",
        "legacy_receipt",
        "legacy_wrong_version",
        "legacy_wrong_role",
    ],
)
async def test_retirement_reconciles_only_exact_durable_terminal_proof(
    host, monkeypatch, guard
):
    from nebula.v3 import runtime_operations as operations
    from nebula.v3.database import (
        RuntimeInstanceRow,
        RuntimeProcessRow,
        DeploymentReceiptRow,
        ProjectOwnershipRow,
        RuntimeResourceRow,
        RuntimeCommandRow,
        RuntimeCapacityReservationRow,
    )
    from nebula.v3.process_identity import process_birth
    import os

    identity = await _saved_retirement(host)
    old = DeploymentOperations(host[0], owner_id="old-host")
    calls = []

    async def dispatch(action, parameters):
        calls.append(action)
        raise CoordinationConflict("there is no live process incarnation to retire")

    args = dict(
        operation_id="old-retirement",
        actor_id="operator",
        action="retire",
        parameters={"runtime_id": identity["runtime_id"]},
        dispatch=dispatch,
    )
    assert (await old.execute(**args))["state"] == "uncertain"
    async with host[0].coordination._write() as session:
        row = await session.get(RuntimeProcessRow, identity["runtime_id"])
        instance = await session.get(RuntimeInstanceRow, identity["runtime_id"])
        receipt = await session.get(DeploymentReceiptRow, "retired-proof")
        if guard in {"legacy_receipt", "legacy_wrong_version", "legacy_wrong_role"}:
            receipt.result = {
                key: receipt.result[key] for key in ("runtime_id", "version", "role")
            }
            if guard == "legacy_wrong_version":
                receipt.result = {**receipt.result, "version": "v2"}
            if guard == "legacy_wrong_role":
                receipt.result = {**receipt.result, "role": "api"}
        elif guard == "missing_process":
            await session.delete(row)
        elif guard == "missing_instance":
            await session.delete(instance)
        elif guard == "live_lease":
            instance.lease_expires_at_ms = 9999999999999
        elif guard == "wrong_version":
            instance.version = "v2"
        elif guard == "wrong_role":
            instance.role = "api"
        elif guard == "identity_mismatch":
            receipt.result = {**receipt.result, "pid": 123}
        elif guard == "missing_receipt":
            await session.delete(receipt)
        elif guard == "live_process":
            row.pid, row.process_birth = os.getpid(), process_birth(os.getpid())
            receipt.result = {
                **receipt.result,
                "pid": row.pid,
                "process_birth": row.process_birth,
            }
        elif guard == "owner":
            session.add(
                ProjectOwnershipRow(
                    project_id="one",
                    runtime_id=identity["runtime_id"],
                    fencing_token=1,
                    lease_expires_at_ms=0,
                    command_sequence=0,
                )
            )
        elif guard == "resource":
            session.add(
                RuntimeResourceRow(
                    id="held-resource",
                    project_id="one",
                    runtime_id=identity["runtime_id"],
                    fencing_token=1,
                    kind="vendor_reference",
                    state="open",
                    restoration="restorable",
                    detail="",
                    checkpoint={},
                    updated_at_ms=1,
                )
            )
        elif guard == "command":
            session.add(
                RuntimeCommandRow(
                    id="held-command",
                    project_id="one",
                    runtime_id=identity["runtime_id"],
                    fencing_token=1,
                    sequence=1,
                    name="test",
                    state="uncertain",
                    actor_id="operator",
                    idempotency_key="test",
                    payload={},
                    payload_hash="a" * 64,
                    result=None,
                    accepted_at_ms=1,
                )
            )
        elif guard == "capacity":
            session.add(
                RuntimeCapacityReservationRow(
                    id="held-capacity",
                    runtime_id=identity["runtime_id"],
                    project_id="one",
                    fencing_token=1,
                    request_key="test",
                    demands={},
                    state="held",
                    created_at_ms=1,
                )
            )
    fresh = DeploymentOperations(host[0], owner_id="new-host")
    monkeypatch.setattr(operations, "same_process", lambda *_: False)
    if guard == "foreign_namespace":
        current = operations.process_origin()
        monkeypatch.setattr(
            operations,
            "process_origin",
            lambda: current.rsplit(":", 1)[0] + ":999999999",
        )
    if guard not in {None, "legacy_receipt"}:
        with pytest.raises(CoordinationConflict):
            await fresh.reconcile("old-retirement", actor_id="operator")
        assert (await fresh.status("old-retirement", actor_id="operator"))[
            "state"
        ] == "uncertain"
    else:
        result = await fresh.reconcile("old-retirement", actor_id="operator")
        assert (
            result["state"] == "succeeded"
            and result["result"]["runtime_id"] == identity["runtime_id"]
        )
        assert await fresh.reconcile("old-retirement", actor_id="operator") == result
        assert await fresh.execute(**args) == result
    assert calls == ["retire"]


async def test_redundant_retirement_reads_proof_without_repeating_process_effect(host):
    from types import SimpleNamespace
    from nebula.v3.runtime_processes import RuntimeProcessHost
    from nebula.v3.process_identity import process_origin

    identity = await _saved_retirement(host)
    process_host = SimpleNamespace(
        _launch=asyncio.Lock(),
        closed=False,
        lock=SimpleNamespace(descriptor=1),
        launch_origin=process_origin(),
        coordinator=host[0],
    )
    # No RPC/process methods are supplied: a repeated completed retirement may
    # only read its durable proof and return the same exact incarnation.
    result = await RuntimeProcessHost.retire(process_host, identity["runtime_id"])
    assert result.model_dump(mode="json") == {
        "protocol": "nebula.runtime/v1",
        **identity,
    }


async def test_missing_incarnation_retirement_is_a_definite_refusal(host):
    async def dispatch(_action, parameters):
        await host[0].begin_retirement(parameters["runtime_id"])
        raise AssertionError("unreachable")

    receipt = await DeploymentOperations(host[0]).execute(
        operation_id="missing-retire",
        actor_id="operator",
        action="retire",
        parameters={"runtime_id": "missing-incarnation"},
        dispatch=dispatch,
    )
    assert receipt["state"] == "failed"
    assert receipt["result"]["error_type"] == "OperationRejected"


async def _record_durable_effect(coordinator, action, operation_id, parameters):
    """Commit exactly the record an action's effect commits, in its own transaction."""
    from nebula.v3.database import DeploymentReceiptRow, RuntimeRecoveryRow
    from nebula.v3.runtime_coordination import ProjectLease
    from nebula.v3.runtime_orphan_recovery import orphan_receipt_key

    async with coordinator.coordination._write() as session:
        if action == "recover_project":
            session.add(
                RuntimeRecoveryRow(
                    operation_id=operation_id,
                    actor_id="operator",
                    project_id=parameters["project_id"],
                    revision=1,
                    source_runtime_id="dead-source",
                    source_fencing_token=4,
                    target_runtime_id=parameters["target_runtime_id"],
                    target_fencing_token=5,
                    expected_handoff_revision=None,
                    phase="held",
                    checkpoint={},
                    blockers=[],
                    created_at_ms=1,
                    updated_at_ms=1,
                )
            )
        elif action == "reconcile_project":
            session.add(
                DeploymentReceiptRow(
                    id=orphan_receipt_key(ProjectLease(**parameters["expected_owner"])),
                    action="project.orphans_reconciled",
                    result={"project_id": "one", "effects_replayed": False},
                    occurred_at_ms=1,
                )
            )
        else:
            await coordinator._receipt(
                session,
                "api.promoted",
                {
                    "runtime_id": parameters["runtime_id"],
                    "version": "v2",
                    "revision": parameters["expected_revision"] + 1,
                },
            )


_OWNER = {"project_id": "one", "runtime_id": "dead-source", "fencing_token": 4}
_DURABLE_CASES = {
    "recover_project": {
        "project_id": "one",
        "target_runtime_id": "target",
        "expected_owner": _OWNER,
        "expected_handoff_revision": None,
    },
    "reconcile_project": {"expected_owner": _OWNER},
    "promote_api": {"runtime_id": "api-new", "expected_revision": 7},
}


@pytest.mark.parametrize("action", sorted(_DURABLE_CASES))
async def test_lost_reply_after_commit_reports_the_durable_record(host, action):
    calls = []

    async def dispatch(name, values):
        calls.append(name)
        await _record_durable_effect(host[0], name, "committed", values)
        raise OSError("connection reset after the commit")

    args = dict(
        operation_id="committed",
        actor_id="operator",
        action=action,
        parameters=_DURABLE_CASES[action],
        dispatch=dispatch,
    )
    receipt = await DeploymentOperations(host[0]).execute(**args)
    assert receipt["state"] == "succeeded"
    assert receipt["result"]["dispatch_error_type"] == "OSError"
    assert receipt["result"]["confirmed_by"] in {
        "runtime_recovery",
        "orphan_reconciliation_receipt",
        "api_promoted_receipt",
    }
    # The same ID returns the settled receipt; nothing is dispatched again.
    assert await DeploymentOperations(host[0]).execute(**args) == receipt
    assert calls == [action]


@pytest.mark.parametrize("action", sorted(_DURABLE_CASES))
async def test_lost_reply_without_commit_names_the_record_to_read(host, action):
    async def dispatch(_name, _values):
        raise OSError("connection reset before any commit")

    receipt = await DeploymentOperations(host[0]).execute(
        operation_id="absent",
        actor_id="operator",
        action=action,
        parameters=_DURABLE_CASES[action],
        dispatch=dispatch,
    )
    assert receipt["state"] == "uncertain"
    assert receipt["result"]["durable_record"]


@pytest.mark.parametrize("action", sorted(_DURABLE_CASES))
async def test_uncertain_receipt_reconciles_from_its_durable_record(
    host, monkeypatch, action
):
    import nebula.v3.runtime_operations as operations

    parameters = _DURABLE_CASES[action]

    async def dispatch(_name, _values):
        raise OSError("connection reset; the commit outcome is unknown")

    old = DeploymentOperations(host[0], owner_id="old-host")
    args = dict(
        operation_id="late",
        actor_id="operator",
        action=action,
        parameters=parameters,
        dispatch=dispatch,
    )
    assert (await old.execute(**args))["state"] == "uncertain"
    # The original host's commit became visible after it reported the error.
    await _record_durable_effect(host[0], action, "late", parameters)
    fresh = DeploymentOperations(host[0], owner_id="new-host")
    monkeypatch.setattr(operations, "same_process", lambda *args: False)
    settled = await fresh.reconcile("late", actor_id="operator")
    assert settled["state"] == "succeeded"
    assert settled["result"]["reconciled"] is True
    assert await fresh.status("late", actor_id="operator") == settled


@pytest.mark.parametrize("action", sorted(_DURABLE_CASES))
async def test_uncertain_receipt_without_effect_reconciles_as_failed(
    host, monkeypatch, action
):
    import nebula.v3.runtime_operations as operations

    async def dispatch(_name, _values):
        raise OSError("connection reset before any commit")

    old = DeploymentOperations(host[0], owner_id="old-host")
    await old.execute(
        operation_id="never",
        actor_id="operator",
        action=action,
        parameters=_DURABLE_CASES[action],
        dispatch=dispatch,
    )
    fresh = DeploymentOperations(host[0], owner_id="new-host")
    # While the original host may still be running, nothing is settled.
    monkeypatch.setattr(operations, "same_process", lambda *args: True)
    with pytest.raises(CoordinationConflict, match="not confirmed gone"):
        await fresh.reconcile("never", actor_id="operator")
    monkeypatch.setattr(operations, "same_process", lambda *args: False)
    assert (await fresh.reconcile("never", actor_id="operator"))["state"] == "failed"


async def test_promotion_receipt_of_another_revision_is_not_this_request(host):
    async def dispatch(_name, values):
        await _record_durable_effect(
            host[0],
            "promote_api",
            "other",
            {**values, "expected_revision": values["expected_revision"] + 3},
        )
        raise OSError("reply lost")

    receipt = await DeploymentOperations(host[0]).execute(
        operation_id="other-revision",
        actor_id="operator",
        action="promote_api",
        parameters=_DURABLE_CASES["promote_api"],
        dispatch=dispatch,
    )
    assert receipt["state"] == "uncertain"
