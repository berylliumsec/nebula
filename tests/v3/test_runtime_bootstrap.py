"""Stable host recovery works before an API or execution process exists."""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from nebula.v3.database import RuntimeProcessRow
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import GIB, MemoryAdministratorLimits, MemoryBudgetError
from nebula.v3.release_store import ReleaseIntegrityError
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_memory import RuntimeMemoryAdmission
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_memory_control import reserve_policy
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}
MEMORY = "/api/v1/memory-budgets"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def recovery(managed):
    limits = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    budgets = MemoryBudgetService(managed.database, administrator=lambda: limits)
    root = RuntimeSupervisor(
        managed.host,
        operator_token=_TOKEN,
        worker_admission=RuntimeMemoryAdmission(budgets),
    )
    await root.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root.app), base_url="https://fixture"
        ) as client:
            yield SimpleNamespace(root=root, managed=managed, client=client)
    finally:
        await root.close()


async def select_release(recovery, version="v1"):
    _source, installed = await recovery.managed.stage(version, _API_WORKER)
    await recovery.root.select_recovery_release(version)
    return installed


async def invite(client):
    response = await client.post("/api/v1/auth/pairings", headers=AUTH, json={})
    assert response.status_code == 200, response.text
    return response.json()


def redeem(offer):
    return {
        "secret": offer["secret"],
        "confirmation_code": offer["confirmation_code"],
        "name": "Recovery browser",
    }


async def test_health_and_recovery_assets_need_no_worker_and_explicit_selection(
    recovery,
):
    root, client = recovery.root, recovery.client
    assert (await client.get("/api/v1/health")).status_code == 401

    health = await client.get("/api/v1/health", headers=AUTH)
    assert health.status_code == 200
    assert health.headers["cache-control"] == "private, no-store"
    assert health.json()["status"] == "degraded"
    assert health.json()["api_ready"] is False
    assert health.json()["recovery_version"] is None
    assert health.json()["recovery_path"] == "/settings#memory-budget-settings"
    await recovery.managed.stage("v1", _API_WORKER)
    assert (await client.get("/")).status_code == 503
    await root.select_recovery_release("v1")
    index = await client.get("/settings")
    assert index.status_code == 200 and "retained UI" in index.text
    assert (await client.head("/settings")).status_code == 200
    assert (await client.get("/missing.js")).status_code == 404
    assert (await client.get("/api/v1/unsupported", headers=AUTH)).status_code == 503
    assert (await client.get("/api", headers=AUTH)).status_code == 503
    assert (await client.get("/internal")).status_code == 404
    assert (await client.post("/settings")).status_code == 503
    assert (await client.get("/api/v1/health", headers=AUTH)).json()[
        "recovery_version"
    ] == "v1"
    async with recovery.managed.database.session() as session:
        assert list(await session.scalars(select(RuntimeProcessRow))) == []


async def test_pairing_survives_host_restart_with_explicit_recovery_release(recovery):
    await select_release(recovery)
    offer = await invite(recovery.client)
    await recovery.root.close()
    replacement = RuntimeSupervisor(
        recovery.managed.new_host(), operator_token=_TOKEN, recovery_version="v1"
    )
    try:
        await replacement.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replacement.app),
            base_url="https://fixture",
        ) as client:
            assert (await client.get("/settings")).status_code == 200
            assert (
                await client.post("/api/v1/auth/pairings/redeem", json=redeem(offer))
            ).status_code == 200
            assert (
                await client.post("/api/v1/auth/pairings/redeem", json=redeem(offer))
            ).status_code == 401
            assert (await client.get("/api/v1/health")).status_code == 200
    finally:
        await replacement.close()


async def test_host_pairing_keeps_locality_tls_and_body_bounds(recovery):
    app, client = recovery.root.app, recovery.client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 1234)),
        base_url="https://fixture",
    ) as remote:
        assert (
            await remote.post("/api/v1/auth/pairings", headers=AUTH, json={})
        ).status_code == 403
    offer = await invite(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as insecure:
        assert (
            await insecure.post("/api/v1/auth/pairings/redeem", json=redeem(offer))
        ).status_code == 400
    oversized = await client.post(
        "/api/v1/auth/pairings", headers=AUTH, content=b"x" * (128 * 1024 + 1)
    )
    assert oversized.status_code == 413


async def test_recovery_selection_rejects_missing_or_changed_bundle(recovery):
    await select_release(recovery)
    with pytest.raises(CoordinationConflict, match="Stage the recovery"):
        await recovery.root.select_recovery_release("missing")
    _source, changed = await recovery.managed.stage("v2", _API_WORKER)
    index = changed / "ui/index.html"
    index.chmod(0o644)
    index.write_text("changed after retention")
    with pytest.raises(ReleaseIntegrityError, match="checksum"):
        await recovery.root.select_recovery_release("v2")
    response = await recovery.client.get("/settings")
    assert response.status_code == 200 and "retained UI" in response.text
    assert (await recovery.client.get("/api/v1/health", headers=AUTH)).json()[
        "recovery_version"
    ] == "v1"


async def test_paired_browser_repairs_memory_before_real_api_admission(recovery):
    await select_release(recovery)
    root, client = recovery.root, recovery.client
    identity = await client.get("/api/v1/runtime/identity", headers=AUTH)
    assert identity.status_code == 200 and identity.json()["principal_id"] == "operator"
    with pytest.raises(MemoryBudgetError, match="Core reserve"):
        await root.prepare_api("v1")
    offer = await invite(client)
    paired = await client.post("/api/v1/auth/pairings/redeem", json=redeem(offer))
    assert paired.status_code == 200, paired.text
    device_id = paired.json()["device"]["id"]
    device_identity = (await client.get("/api/v1/runtime/identity")).json()
    assert device_identity == {
        "core_id": identity.json()["core_id"],
        "principal_id": "device:" + device_id,
    }
    assert (await client.get("/api/v1/health")).json()["api_ready"] is False
    policy = (await client.get(MEMORY)).json()
    body = {"expected_revision": policy["revision"], "policy": reserve_policy(policy)}
    assert (await client.put(MEMORY, json=body)).status_code == 403
    csrf = {"X-Nebula-CSRF": paired.json()["csrf_token"], "Origin": "https://fixture"}
    saved = await client.put(MEMORY, headers=csrf, json=body)
    assert saved.status_code == 200, saved.text
    assert (await client.get(MEMORY)).json()["policy"] == saved.json()["policy"]
    async with recovery.managed.database.session() as session:
        assert list(await session.scalars(select(RuntimeProcessRow))) == []
    api = await root.prepare_api("v1")
    await root.promote_api(
        api.runtime_id, expected_revision=(await root.coordinator.status()).revision
    )
    health = (await client.get("/api/v1/health")).json()
    assert health["api_ready"] is True and health["status"] == "ok"
    assert (await client.get("/")).headers["x-nebula-api-version"] == "v1"
    revoked = await client.delete("/api/v1/auth/devices/" + device_id, headers=AUTH)
    assert revoked.status_code == 204
    assert (await client.get("/api/v1/health")).status_code == 401
