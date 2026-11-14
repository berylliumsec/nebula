"""Management envelopes keep auth, replay and post-response shutdown on the host."""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.database import DeploymentOperationRow
from nebula.v3.process_identity import same_process
from nebula.v3.runtime_coordination import CoordinationConflict
from tests.v3.test_release_store import bundle
from tests.v3.test_runtime_bootstrap import AUTH, invite, redeem
from tests.v3.test_runtime_bootstrap import recovery as recovery
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_processes import _API_WORKER, _WORKER
from tests.v3.test_runtime_memory_control import reserve_policy

pytestmark = pytest.mark.anyio
PATH = "/api/v1/deployment/operations"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_management_validates_auth_body_and_parameters_before_accepting(recovery):
    client = recovery.client
    body = {"operation_id": "attempt", "action": "shutdown", "parameters": {}}
    assert (await client.post(PATH, json=body)).status_code == 401
    assert (await client.post(PATH, headers=AUTH, json=body)).status_code == 422
    body["parameters"] = {"expected_revision": 1, "extra": "not an operation field"}
    assert (await client.post(PATH, headers=AUTH, json=body)).status_code == 422
    assert (
        await client.post(PATH, headers=AUTH, content=b"x" * (128 * 1024 + 1))
    ).status_code == 413
    async with recovery.managed.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(DeploymentOperationRow)
            )
            == 0
        )


async def test_stage_and_recovery_selection_replay_authoritative_result(
    recovery, tmp_path
):
    source = tmp_path / "managed-source"
    digest = bundle(source, version="managed")
    body = {
        "operation_id": "stage-one",
        "action": "stage",
        "parameters": {"source": str(source), "manifest_sha256": digest},
    }
    response = await recovery.client.post(PATH, headers=AUTH, json=body)
    assert response.status_code == 200 and response.json()["state"] == "succeeded"
    assert response.json()["result"] == {
        "version": "managed",
        "manifest_sha256": digest,
    }
    assert (
        await recovery.client.get(PATH + "/stage-one", headers=AUTH)
    ).json() == response.json()
    assert (
        await recovery.client.post(PATH, headers=AUTH, json=body)
    ).json() == response.json()
    changed = {**body, "parameters": {**body["parameters"], "source": str(tmp_path)}}
    assert (
        await recovery.client.post(PATH, headers=AUTH, json=changed)
    ).status_code == 409
    selected = await recovery.client.post(
        PATH,
        headers=AUTH,
        json={
            "operation_id": "select-one",
            "action": "select_recovery_release",
            "parameters": {"version": "managed"},
        },
    )
    assert selected.json()["state"] == "succeeded"
    assert (await recovery.client.get("/settings")).status_code == 200
    offer = await invite(recovery.client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recovery.root.app), base_url="https://fixture"
    ) as other:
        assert (
            await other.post("/api/v1/auth/pairings/redeem", json=redeem(offer))
        ).status_code == 200
        assert (await other.get(PATH + "/stage-one")).status_code == 409


async def test_operation_finishes_its_receipt_when_a_stopping_host_cancels_the_request(
    recovery, tmp_path
):
    # A supervisor generation that is being replaced stops relaying requests
    # after its drain window. An accepted operation still completes, records
    # a definite receipt, and is joined before host authority is released.
    source = tmp_path / "slow-source"
    digest = bundle(source, version="slow")
    original = recovery.root.stage
    entered = asyncio.Event()

    async def slow_stage(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.5)
        return await original(*args, **kwargs)

    recovery.root.stage = slow_stage
    body = {
        "operation_id": "stage-during-drain",
        "action": "stage",
        "parameters": {"source": str(source), "manifest_sha256": digest},
    }
    request = asyncio.create_task(recovery.client.post(PATH, headers=AUTH, json=body))
    await asyncio.wait_for(entered.wait(), 10)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    pending = set(recovery.root.management.inflight)
    assert pending
    await recovery.root.close()
    assert not recovery.root.management.inflight
    async with recovery.managed.database.session() as session:
        row = await session.get(DeploymentOperationRow, "stage-during-drain")
    assert row is not None and row.state == "succeeded"


@pytest.mark.parametrize("refused", [False, True])
async def test_shutdown_callback_waits_for_success_and_never_repeats_saved_operation(
    recovery, monkeypatch, refused
):
    calls = []

    async def shutdown(*, expected_revision):
        calls.append(("shutdown", expected_revision))
        if refused:
            raise CoordinationConflict("Synthetic work is still active")
        return {"ready": True}

    async def callback():
        calls.append(("callback", None))

    monkeypatch.setattr(recovery.root, "shutdown", shutdown)
    recovery.root._shutdown_requested = callback
    body = {
        "operation_id": "stop-once",
        "action": "shutdown",
        "parameters": {"expected_revision": 1},
    }
    response = await recovery.client.post(PATH, headers=AUTH, json=body)
    assert response.status_code == 200
    assert response.json()["state"] == ("uncertain" if refused else "succeeded")
    assert calls == [("shutdown", 1)] + ([] if refused else [("callback", None)])
    assert (
        await recovery.client.post(PATH, headers=AUTH, json=body)
    ).json() == response.json()
    assert calls == [("shutdown", 1)] + ([] if refused else [("callback", None)])


async def allow_runtime(recovery, program):
    policy = (await recovery.client.get("/api/v1/memory-budgets", headers=AUTH)).json()
    saved = await recovery.client.put(
        "/api/v1/memory-budgets",
        headers=AUTH,
        json={
            "expected_revision": policy["revision"],
            "policy": reserve_policy(policy),
        },
    )
    assert saved.status_code == 200
    await recovery.managed.stage("v1", program)


async def test_uncertain_operation_never_notifies_shutdown_launcher(
    recovery, monkeypatch
):
    notified = []

    async def shutdown(**_values):
        return {"state": "stopped"}

    async def callback():
        notified.append(True)

    class UnconfirmedLedger:
        async def execute(
            self, *, dispatch, action, parameters, operation_id, **_values
        ):
            await dispatch(action, parameters)
            return {
                "operation_id": operation_id,
                "action": action,
                "state": "uncertain",
                "result": None,
            }

    monkeypatch.setattr(recovery.root, "shutdown", shutdown)
    recovery.root._shutdown_requested = callback
    recovery.root.management.operations = UnconfirmedLedger()
    response = await recovery.client.post(
        PATH,
        headers=AUTH,
        json={
            "operation_id": "unconfirmed-stop",
            "action": "shutdown",
            "parameters": {"expected_revision": 1},
        },
    )
    assert response.json()["state"] == "uncertain" and notified == []


async def test_real_idle_api_shutdown_notifies_only_after_verified_process_retirement(
    recovery,
):
    await allow_runtime(recovery, _API_WORKER)
    root = recovery.root
    api = await root.prepare_api("v1")
    await root.promote_api(
        api.runtime_id, expected_revision=(await root.coordinator.status()).revision
    )
    observed = []

    async def stopped():
        observed.append(same_process(api.pid, api.process_birth))

    root._shutdown_requested = stopped
    body = {
        "operation_id": "actual-stop",
        "action": "shutdown",
        "parameters": {
            "expected_revision": (await root.coordinator.status()).revision,
        },
    }
    response = await recovery.client.post(PATH, headers=AUTH, json=body)
    assert response.status_code == 200 and response.json()["state"] == "succeeded"
    assert response.json()["result"] == {
        "state": "stopped",
        "retired_runtime_ids": [api.runtime_id],
    }
    assert observed == [False]
    assert (
        await recovery.client.post(PATH, headers=AUTH, json=body)
    ).json() == response.json()
    assert observed == [False]


async def test_owned_project_refuses_real_shutdown_without_notifying_launcher(recovery):
    await allow_runtime(recovery, _WORKER)
    root = recovery.root
    owner = await root.prepare_execution("v1")
    await root.coordinator.coordination.claim_project(
        "admitted-project", owner.runtime_id
    )
    observed = []

    async def stopped():
        observed.append(True)

    root._shutdown_requested = stopped
    body = {
        "operation_id": "blocked-stop",
        "action": "shutdown",
        "parameters": {
            "expected_revision": (await root.coordinator.status()).revision,
        },
    }
    response = await recovery.client.post(PATH, headers=AUTH, json=body)
    assert response.status_code == 200 and response.json()["state"] == "uncertain"
    assert observed == [] and same_process(owner.pid, owner.process_birth)
