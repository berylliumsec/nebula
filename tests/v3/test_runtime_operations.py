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
