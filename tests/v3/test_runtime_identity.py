"""Client reconciliation is scoped to durable Core and authenticated principal."""

import asyncio
from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from nebula.v3.database import Database, RuntimeCommandRow, RuntimeCoreIdentityRow
from nebula.v3.domain import PairedDeviceSession, utc_now
from nebula.v3.runtime_identity import core_identity
from tests.v3.test_api_worker import _AUTH, boundary as boundary
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def device(boundary):
    now = utc_now()
    await boundary.host.create(
        PairedDeviceSession(
            id="isolated-browser",
            name="Synthetic viewer",
            token_sha256=hashlib.sha256(b"viewer-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"viewer-csrf").hexdigest(),
            last_used_at=now,
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )


async def test_identity_survives_api_replacement_and_separates_principals(boundary):
    _, first = boundary.api()
    assert (await first.get("/api/v1/runtime/identity")).status_code == 401
    result = await first.get("/api/v1/runtime/identity", headers=_AUTH)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert result.json()["principal_id"] == "operator"
    await device(boundary)
    _, second = boundary.api()
    second.cookies.set("nebula_device", "viewer-token")
    viewer = await second.get("/api/v1/runtime/identity")
    assert viewer.status_code == 200
    assert viewer.json() == {
        "core_id": result.json()["core_id"],
        "principal_id": "device:isolated-browser",
    }
    assert (
        await second.get("/api/v1/runtime/identity", headers=_AUTH)
    ).json() == result.json()
    assert "viewer-token" not in viewer.text


async def test_concurrent_first_identity_has_one_durable_record(boundary):
    results = await asyncio.gather(
        *(core_identity(boundary.api()[0].state.store.database) for _ in range(4))
    )
    assert len(set(results)) == 1
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeCoreIdentityRow)
            )
            == 1
        )


async def test_independent_data_roots_do_not_share_identity(boundary, tmp_path):
    other = Database(tmp_path / "other-core.sqlite3")
    try:
        await other.initialize()
        assert await core_identity(other) != await core_identity(boundary.host.database)
    finally:
        await other.dispose()


async def test_lookup_recovers_lost_acceptance_without_another_submission(boundary):
    _, first = boundary.api()
    original = await first.post(
        "/api/v1/synthetic", json={"text": "inert"}, headers=_AUTH
    )
    command_id = original.json()["receipt"]["command_id"]
    await first.aclose()
    _, second = boundary.api()
    result = await second.get(
        "/api/v1/runtime/requests/lookup",
        params={"idempotency_key": _AUTH["Idempotency-Key"]},
        headers=_AUTH,
    )
    assert result.status_code == 200
    assert result.json() == {
        "command_id": command_id,
        "project_id": "project",
        "state": "queued",
    }
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 1
        )
    assert (
        await second.get(
            "/api/v1/runtime/requests/lookup",
            params={"idempotency_key": "never-accepted"},
            headers=_AUTH,
        )
    ).status_code == 404


async def test_lookup_cannot_disclose_another_principals_acceptance(boundary):
    _, client = boundary.api()
    await client.post("/api/v1/synthetic", json={}, headers=_AUTH)
    await device(boundary)
    client.cookies.set("nebula_device", "viewer-token")
    result = await client.get(
        "/api/v1/runtime/requests/lookup",
        params={"idempotency_key": _AUTH["Idempotency-Key"]},
    )
    assert result.status_code == 404
    assert "command_id" not in result.json()


@pytest.mark.parametrize("key", ["", "invalid key", "x" * 201])
async def test_lookup_rejects_unbounded_or_ambiguous_keys(boundary, key):
    _, client = boundary.api()
    result = await client.get(
        "/api/v1/runtime/requests/lookup",
        params={"idempotency_key": key},
        headers=_AUTH,
    )
    assert result.status_code == 422
