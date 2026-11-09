"""Pairing/device state is shared SQL authority, independent of execution owners."""

import asyncio
from datetime import timedelta
import hashlib

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.database import DevicePairingOfferRow, RuntimeCommandRow
from nebula.v3.domain import PairedDeviceSession, utc_now
from nebula.v3.storage import StoreTransaction
from tests.v3.test_api_worker import _AUTH
from tests.v3.test_api_worker import boundary as boundary
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def invite(client):
    response = await client.post(
        "/api/v1/auth/pairings", headers=_AUTH, json={"name": "Synthetic invitation"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    return response.json()


def grant(offer, *, wrong=False):
    code = offer["confirmation_code"]
    return {
        "secret": offer["secret"],
        "confirmation_code": f"{(int(code) + 1) % 1_000_000:06d}" if wrong else code,
        "name": "Synthetic paired client",
    }


async def test_pairing_offer_survives_api_replacement_and_remains_single_use(boundary):
    first_app, first = boundary.api()
    offer = await invite(first)
    async with boundary.host.database.session() as session:
        stored = await session.get(
            DevicePairingOfferRow, hashlib.sha256(offer["secret"].encode()).hexdigest()
        )
        assert (
            len(stored.confirmation_mac) == 64
            and stored.confirmation_mac != offer["confirmation_code"]
        )
    await first.aclose()
    await first_app.state.viewer_proxy.close()
    _, replacement = boundary.api()
    response = await replacement.post("/api/v1/auth/pairings/redeem", json=grant(offer))
    assert response.status_code == 200 and response.json()["device"]["current"] is True
    assert len(response.headers.get_list("set-cookie")) == 2
    assert response.headers["cache-control"] == "private, no-store"
    assert (
        await replacement.post("/api/v1/auth/pairings/redeem", json=grant(offer))
    ).status_code == 401
    devices = await replacement.get("/api/v1/auth/devices")
    assert devices.status_code == 200 and len(devices.json()) == 1
    assert devices.headers["cache-control"] == "private, no-store"
    assert devices.json()[0]["current"] is True
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )


async def test_concurrent_api_redeems_create_exactly_one_device(boundary):
    _, first = boundary.api()
    _, second = boundary.api()
    offer = await invite(first)
    results = await asyncio.gather(
        *(
            client.post("/api/v1/auth/pairings/redeem", json=grant(offer))
            for client in (first, second)
        )
    )
    assert sorted(item.status_code for item in results) == [200, 401]
    assert await boundary.host.count(PairedDeviceSession) == 1


@pytest.mark.parametrize("attempts", [4, 5])
async def test_wrong_confirmation_attempts_commit_across_api_processes(
    boundary, attempts
):
    clients = [boundary.api()[1], boundary.api()[1]]
    offer = await invite(clients[0])
    for index in range(attempts):
        response = await clients[index % 2].post(
            "/api/v1/auth/pairings/redeem", json=grant(offer, wrong=True)
        )
        assert (
            response.status_code == 401
            and "confirmation code" in response.json()["detail"]
        )
    final = await clients[1].post("/api/v1/auth/pairings/redeem", json=grant(offer))
    assert final.status_code == (200 if attempts == 4 else 401)


async def test_expiry_and_pending_offer_capacity_are_shared_and_bounded(
    boundary, monkeypatch
):
    monkeypatch.setattr("nebula.v3.authentication_api.MAX_PENDING_OFFERS", 2)
    now = [utc_now()]
    app, first = boundary.api()
    app.state.authentication_api.authentication.clock = lambda: now[0]
    other_app, second = boundary.api()
    other_app.state.authentication_api.authentication.clock = lambda: now[0]
    offers = await asyncio.gather(invite(first), invite(second))
    full = await first.post("/api/v1/auth/pairings", headers=_AUTH, json={})
    assert full.status_code == 429
    now[0] += timedelta(minutes=6)
    assert (
        await second.post("/api/v1/auth/pairings/redeem", json=grant(offers[0]))
    ).status_code == 401
    await invite(first)
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(DevicePairingOfferRow)
            )
            == 1
        )


async def test_pairing_locality_tls_and_control_payload_bounds_are_enforced(boundary):
    app, local = boundary.api()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("172.20.0.2", 1234)),
        base_url="https://synthetic.test",
    ) as remote:
        response = await remote.post(
            "/api/v1/auth/pairings",
            headers={**_AUTH, "X-Forwarded-For": "127.0.0.1"},
            json={},
        )
        assert response.status_code == 403
    offer = await invite(local)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.test"
    ) as insecure:
        assert (
            await insecure.post("/api/v1/auth/pairings/redeem", json=grant(offer))
        ).status_code == 400
        oversized = await insecure.post(
            "/api/v1/auth/pairings/redeem", content=b"x" * (128 * 1024 + 1)
        )
        assert oversized.status_code == 413
    assert (
        await local.post("/api/v1/auth/pairings/redeem", json=grant(offer))
    ).status_code == 200


async def test_device_heartbeat_and_self_revocation_do_not_wait_for_execution(boundary):
    _, client = boundary.api()
    paired = await client.post(
        "/api/v1/auth/pairings/redeem", json=grant(await invite(client))
    )
    assert paired.status_code == 200
    csrf = paired.json()["csrf_token"]
    headers = {"X-Nebula-CSRF": csrf, "Origin": "https://synthetic.test"}
    # No execution runner exists in this test. Device control stays available.
    heartbeat = await client.put(
        "/api/v1/auth/devices/current/capabilities",
        headers=headers,
        json={"platform": "browser", "app_version": "synthetic", "capabilities": []},
    )
    assert heartbeat.status_code == 200 and heartbeat.json()["healthy"] is True
    identity = heartbeat.json()["id"]
    missing_csrf = await client.delete("/api/v1/auth/devices/" + identity)
    assert missing_csrf.status_code == 403
    revoked = await client.delete("/api/v1/auth/devices/" + identity, headers=headers)
    assert (
        revoked.status_code == 204 and len(revoked.headers.get_list("set-cookie")) == 2
    )
    assert (await client.get("/api/v1/auth/devices")).status_code == 401


async def test_pairing_consume_and_device_creation_rollback_as_one_transaction(
    boundary, monkeypatch
):
    _, first = boundary.api()
    _, second = boundary.api()
    offer = await invite(first)
    original = StoreTransaction.add

    async def fail_after_insert(self, entity):
        result = await original(self, entity)
        if isinstance(entity, PairedDeviceSession):
            raise RuntimeError("synthetic interrupted credential grant")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(StoreTransaction, "add", fail_after_insert)
        with pytest.raises(RuntimeError, match="interrupted credential"):
            await first.post("/api/v1/auth/pairings/redeem", json=grant(offer))
    assert await boundary.host.count(PairedDeviceSession) == 0
    assert (
        await second.post("/api/v1/auth/pairings/redeem", json=grant(offer))
    ).status_code == 200
