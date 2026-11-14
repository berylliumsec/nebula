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


def _supervisor(coordinator, **host):
    from types import SimpleNamespace

    from nebula.v3.supervisor import RuntimeSupervisor

    root = RuntimeSupervisor.__new__(RuntimeSupervisor)
    root.coordinator = coordinator
    root.host = SimpleNamespace(**host)
    root.started, root.closed, root._shutdown_committed = True, False, False
    root._operations = asyncio.Lock()
    root.worker_admission = None
    return root


async def test_stale_revisions_are_definite_refusals_the_deployer_may_ask_again(
    host,
):
    from nebula.v3.runtime_coordination import DeploymentChanged
    from tests.v3.test_deployment_lifecycle import worker

    coordinator = host[0]
    api = await worker(host, "v2", role="api")
    stale = (await coordinator.status()).revision - 1
    with pytest.raises(DeploymentChanged):
        await coordinator.promote_api(api, expected_revision=stale)
    with pytest.raises(DeploymentChanged):
        await coordinator.rollback_code("v1", expected_revision=stale)
    with pytest.raises(DeploymentChanged):
        await coordinator.migration_preflight("v2", expected_revision=stale)

    async def promote(_action, parameters):
        await coordinator.promote_api(
            parameters["runtime_id"], expected_revision=parameters["expected_revision"]
        )
        return {}

    receipt = await DeploymentOperations(coordinator).execute(
        operation_id="deployment:promote-api",
        actor_id="operator",
        action="promote_api",
        parameters={"runtime_id": api, "expected_revision": stale},
        dispatch=promote,
    )
    assert receipt["state"] == "failed"
    assert receipt["result"]["error_type"] == "DeploymentChanged"
    status = await coordinator.status()
    assert status.api_version is None and status.target_execution_version is None


async def test_status_lists_every_unretired_process_for_effect_readback(host):
    from tests.v3.test_deployment_lifecycle import worker

    coordinator = host[0]
    api = await worker(host, "v1", role="api")
    execution = await worker(host, "v2")
    listed = {
        row.runtime_id: (row.version, row.role, row.state)
        for row in (await coordinator.status()).processes
    }
    assert listed == {
        api: ("v1", "api", "ready"),
        execution: ("v2", "execution", "ready"),
    }
    host_children = coordinator.test_children
    host_children[execution].terminate()
    host_children[execution].wait(timeout=5)
    identity = await coordinator.begin_retirement(execution)
    await coordinator.retired(identity)
    assert [row.runtime_id for row in (await coordinator.status()).processes] == [api]


async def test_api_preparation_refused_before_any_start_is_a_definite_failure(host):
    from nebula.v3.memory_policy import MemoryBudgetError
    from nebula.v3.runtime_coordination import OperationRejected

    spawned = []

    async def remains_staged(version, role):
        spawned.append((version, role))
        return None

    root = _supervisor(host[0], spawn=remains_staged)
    with pytest.raises(OperationRejected, match="memory reserve"):
        await root.prepare_api("v2")
    with pytest.raises(OperationRejected, match="Stage and verify"):
        await root.prepare_api("missing")

    async def over_budget(_manifest, _role):
        raise MemoryBudgetError("The memory budget has no room for this worker")

    root.worker_admission = over_budget
    with pytest.raises(OperationRejected, match="no room"):
        await root.prepare_api("v2")
    assert spawned == []

    async def admitted(_manifest, _role):
        return None

    root.worker_admission = admitted
    with pytest.raises(OperationRejected, match="remains staged"):
        await root.prepare_api("v2")
    assert spawned == [("v2", "api")]


async def test_code_rollback_refused_after_its_target_changed_stays_unconfirmed(host):
    from nebula.v3.runtime_coordination import OperationRejected

    coordinator = host[0]

    async def remains_staged(_version, _role):
        return None

    async def admitted(_manifest, _role):
        return None

    root = _supervisor(coordinator, spawn=remains_staged)
    root.worker_admission = admitted
    revision = (await coordinator.status()).revision

    async def dispatch(_action, parameters):
        await root.rollback_code(**parameters)
        return {}

    receipt = await DeploymentOperations(coordinator).execute(
        operation_id="deployment:target",
        actor_id="operator",
        action="rollback_code",
        parameters={"version": "v1", "expected_revision": revision},
        dispatch=dispatch,
    )
    # The target moved before API preparation refused, so the receipt must not
    # claim that nothing happened.
    assert receipt["state"] == "uncertain"
    assert (await coordinator.status()).target_execution_version == "v1"
    with pytest.raises(OperationRejected):
        await root.prepare_api("v1")
