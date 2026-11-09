"""Queued paired requests retain identity without replaying authentication secrets."""

import asyncio
from datetime import timedelta
import hashlib
import json

from fastapi import Depends, FastAPI, Request
import httpx
from keyring.backends.null import Keyring
import pytest

from nebula.v3.api import create_app
from nebula.v3.authentication import CoreAuthentication
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import PairedDeviceSession, utc_now
from nebula.v3.runtime_delivery import DurableHttpRunner, HttpIntent, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
_TOKEN = "synthetic-principal-core-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def device(store, name="paired"):
    now = utc_now()
    return await store.create(
        PairedDeviceSession(
            id=name,
            name="Synthetic paired device",
            token_sha256=hashlib.sha256((name + "-cookie").encode()).hexdigest(),
            csrf_sha256=hashlib.sha256((name + "-csrf").encode()).hexdigest(),
            last_used_at=now,
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )


async def accept(
    delivery,
    *,
    path,
    method="POST",
    actor="device:paired",
    key="request",
    project="project",
    body=None,
):
    intent = HttpIntent.capture(
        method,
        path,
        b"",
        [
            (b"content-type", b"application/json"),
            (b"cookie", b"nebula_device=paired-cookie; nebula_csrf=paired-csrf"),
            (b"x-nebula-csrf", b"paired-csrf"),
        ],
        json.dumps(body).encode() if body is not None else b"",
    )
    return await delivery.accept(
        intent, actor_id=actor, project_id=project, idempotency_key=key, lane="control"
    )


def body(page):
    return json.loads(
        b"".join(frame.content for frame in page.frames if frame.kind == "body")
    )


def kernel(store, root):
    return create_app(
        store,
        auth_token=_TOKEN,
        execution_data_root=root,
        credential_store=CredentialStore(keyring_backend=Keyring()),
    )


async def test_durable_capability_heartbeat_keeps_the_accepting_device_identity(
    cluster, tmp_path
):
    (first, _), stores, _clock = cluster
    await device(stores[0])
    app = kernel(stores[0], tmp_path)
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    lease = await first.claim_project("project", "old")
    receipt = await accept(
        delivery,
        method="PUT",
        path="/api/v1/auth/devices/current/capabilities",
        body={"platform": "browser", "app_version": "synthetic", "capabilities": []},
    )
    assert await DurableHttpRunner(delivery, app, operator_token=_TOKEN).run_one(
        lease, lane="control"
    )
    result = await delivery.page("device:paired", receipt.command_id)
    assert result.state == "succeeded" and body(result)["current"] is True
    stored = await stores[0].get(PairedDeviceSession, "paired")
    assert stored.heartbeat_at is not None and stored.platform == "browser"
    assert await CoreAuthentication(stores[0], _TOKEN).device_for_token(None) is None


async def test_self_revocation_deletes_both_cookies_without_retaining_their_secrets(
    cluster, tmp_path
):
    (first, _), stores, _clock = cluster
    await device(stores[0])
    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    lease = await first.claim_project("project", "old")
    receipt = await accept(
        delivery, method="DELETE", path="/api/v1/auth/devices/paired"
    )
    assert await DurableHttpRunner(
        delivery, kernel(stores[0], tmp_path), operator_token=_TOKEN
    ).run_one(lease, lane="control")
    result = await delivery.page("device:paired", receipt.command_id)
    head = json.loads(result.frames[0].content)
    assert head["status"] == 204
    cookies = [value for name, value in head["headers"] if name.lower() == "set-cookie"]
    assert len(cookies) == 2 and all("Max-Age=0" in value for value in cookies)
    assert any(value.startswith("nebula_device=") for value in cookies)
    assert any(value.startswith("nebula_csrf=") for value in cookies)
    assert (await stores[0].get(PairedDeviceSession, "paired")).revoked_at is not None
    assert all(
        b"paired-cookie" not in path.read_bytes()
        for path in delivery.vault.root.iterdir()
    )


async def test_internal_transport_bearer_does_not_elevate_a_device_to_bearer_only_authority(
    cluster, tmp_path
):
    (first, _), stores, _clock = cluster
    await device(stores[0])
    auth = CoreAuthentication(stores[0], _TOKEN)
    app, calls = FastAPI(), []

    @app.post("/api/v1/local-only", dependencies=[Depends(auth.require_bearer_auth)])
    async def local_only():
        calls.append(True)
        return {"local": True}

    delivery = RuntimeDelivery(first, RuntimeVault(tmp_path / "private"))
    lease = await first.claim_project("project", "old")
    denied = await accept(delivery, path="/api/v1/local-only")
    runner = DurableHttpRunner(delivery, app, operator_token=_TOKEN)
    assert await runner.run_one(lease, lane="control")
    assert (
        json.loads(
            (await delivery.page("device:paired", denied.command_id)).frames[0].content
        )["status"]
        == 401
    )
    assert not calls
    allowed = await accept(
        delivery, path="/api/v1/local-only", actor="operator", key="operator"
    )
    assert await runner.run_one(lease, lane="control")
    assert (await delivery.page("operator", allowed.command_id)).state == "succeeded"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://public"
    ) as client:
        assert (
            await client.post(
                "/api/v1/local-only",
                headers={"X-Nebula-Runtime-Actor": "device:paired"},
            )
        ).status_code == 401
        response = await client.post(
            "/api/v1/local-only",
            headers={
                "Authorization": "Bearer " + _TOKEN,
                "X-Nebula-Runtime-Actor": "device:paired",
            },
        )
        assert response.status_code == 200
    assert len(calls) == 2


async def test_concurrent_accepted_devices_do_not_share_identity_context(
    cluster, tmp_path
):
    (first, second), stores, _clock = cluster
    await device(stores[0], "one")
    await device(stores[0], "two")
    auth = CoreAuthentication(stores[0], _TOKEN)
    app, both, entered = FastAPI(), asyncio.Event(), []

    @app.post("/api/v1/device-identity", dependencies=[Depends(auth.require_auth)])
    async def identity(request: Request):
        entered.append(request.state.auth_device_id)
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 3)
        current = await auth.device_for_token(None)
        return {"device_id": current.id}

    deliveries = [
        RuntimeDelivery(first, RuntimeVault(tmp_path / "private")),
        RuntimeDelivery(second, RuntimeVault(tmp_path / "private")),
    ]
    receipts = [
        await accept(deliveries[0], path="/api/v1/device-identity", actor="device:one"),
        await accept(
            deliveries[1],
            path="/api/v1/device-identity",
            actor="device:two",
            project="other",
        ),
    ]
    leases = [
        await first.claim_project("project", "old"),
        await second.claim_project("other", "new"),
    ]
    assert all(
        await asyncio.gather(
            *(
                DurableHttpRunner(delivery, app, operator_token=_TOKEN).run_one(
                    lease, lane="control"
                )
                for delivery, lease in zip(deliveries, leases)
            )
        )
    )
    for delivery, receipt, name in zip(deliveries, receipts, ("one", "two")):
        assert body(await delivery.page("device:" + name, receipt.command_id)) == {
            "device_id": name
        }
