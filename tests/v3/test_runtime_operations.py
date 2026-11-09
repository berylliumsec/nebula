"""Management retries retain receipts without repeating host effects."""

import asyncio
import pytest
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_operations import DeploymentOperations
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
