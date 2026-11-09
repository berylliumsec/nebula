"""Global SQL metadata stays usable without granting a worker all projects."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement, ProviderProfile, ScopePolicy
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
TOKEN = "isolated-global-sql-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def global_api(location, tmp_path):
    database = Database(location, pool_settings=PoolSettings(5, 12, 5, 2))
    await database.initialize()
    database._bootstrap = False
    store = NebulaStore(database)
    coordination = RuntimeCoordination(database)
    routing = RuntimeRouting(
        coordination, tmp_path / "credentials", request_admission=True
    )
    delivery = RuntimeDelivery(coordination, RuntimeVault(tmp_path / "payloads"))

    async def unavailable(_scope):
        pytest.fail(
            "Global SQL operations must not create or contact an execution worker"
        )

    app = create_api_worker(
        store,
        delivery,
        operator_token=TOKEN,
        project_for=routing.project_for,
        upstream_for=unavailable,
        command_start_timeout=0.03,
        poll_interval=0.01,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
    )
    try:
        yield SimpleNamespace(
            client=client, store=store, routing=routing, app=app, database=database
        )
    finally:
        await client.aclose()
        await app.state.viewer_proxy.close()
        await store.event_history.close()
        await database.dispose()


async def test_project_creation_and_global_catalogs_need_no_execution_owner(global_api):
    client = global_api.client
    assert (await client.get("/api/v1/engagements")).status_code == 401
    response = await client.post(
        "/api/v1/engagements",
        headers={**AUTH, "Idempotency-Key": "project"},
        json={"name": "A synthetic project"},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    assert (
        await global_api.store.get(ScopePolicy, project["scope_policy_id"])
    ).engagement_id == project["id"]
    for path in (
        "engagements",
        "providers",
        "harnesses",
        "operator-profiles",
        "provider-catalog",
        "library/items",
        "runner-profiles",
    ):
        # Runtime setup is a host observation, covered through the supervisor
        # by the dedicated host-operator cases rather than a disabled SQL stub.
        assert (await client.get("/api/v1/" + path, headers=AUTH)).status_code == 200, (
            path
        )
    assert [
        item["id"]
        for item in (await client.get("/api/v1/engagements", headers=AUTH)).json()
    ] == [project["id"]]
    async with global_api.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
    command = response.headers["x-nebula-command-id"]
    status = await client.get(f"/api/v1/runtime/requests/{command}", headers=AUTH)
    assert status.json()["state"] == "succeeded"


async def test_concurrent_global_retry_reuses_sql_effect_and_response(global_api):
    request = {"name": "Exact once project"}
    headers = {**AUTH, "Idempotency-Key": "same-project"}
    first, second = await asyncio.gather(
        *(
            global_api.client.post("/api/v1/engagements", headers=headers, json=request)
            for _ in range(2)
        )
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["x-nebula-command-id"] == second.headers["x-nebula-command-id"]
    assert await global_api.store.count(Engagement) == 1
    changed = await global_api.client.post(
        "/api/v1/engagements", headers=headers, json={"name": "Different"}
    )
    assert changed.status_code == 409


async def test_provider_mutations_retain_validation_and_are_replayable(global_api):
    client = global_api.client
    created = await client.post(
        "/api/v1/providers",
        headers={**AUTH, "Idempotency-Key": "provider"},
        json={
            "name": "Synthetic provider",
            "provider_type": "openai",
            "endpoint": "https://api.openai.com/v1",
            "secret_ref": "env:SYNTHETIC_UNUSED",
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    assert not profile["capabilities"]["tool_calling"]
    patch = {
        "changes": {"name": "Saved name"},
        "expected_revision": profile["revision"],
    }
    headers = {**AUTH, "Idempotency-Key": "update-provider"}
    changed = await client.patch(
        f"/api/v1/providers/{profile['id']}", headers=headers, json=patch
    )
    replay = await client.patch(
        f"/api/v1/providers/{profile['id']}", headers=headers, json=patch
    )
    assert changed.status_code == 200, changed.text
    assert replay.json() == changed.json()
    assert (
        await global_api.store.get(ProviderProfile, profile["id"])
    ).name == "Saved name"


async def test_invalid_global_mutation_has_receipt_without_partial_entities(global_api):
    result = await global_api.client.post(
        "/api/v1/engagements",
        headers={**AUTH, "Idempotency-Key": "invalid"},
        json={"id": "forged", "name": "No"},
    )
    assert result.status_code == 422
    assert await global_api.store.count(Engagement) == 0
    async with global_api.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 1
        )
    replay = await global_api.client.get(
        f"/api/v1/runtime/requests/{result.headers['x-nebula-command-id']}/response",
        headers=AUTH,
    )
    assert replay.status_code == 422


async def test_shutdown_fence_blocks_new_global_effect_but_keeps_receipt(global_api):
    headers = {**AUTH, "Idempotency-Key": "before-stop"}
    client = global_api.client
    original = await client.post(
        "/api/v1/engagements", headers=headers, json={"name": "Before stop"}
    )
    async with global_api.database.session() as session:
        session.add(
            DeploymentHostRow(
                key="host", revision=1, shutdown_requested=True, detail="stopping"
            )
        )
    retry = await client.post(
        "/api/v1/engagements", headers=headers, json={"name": "Before stop"}
    )
    blocked = await client.post(
        "/api/v1/engagements",
        headers={**AUTH, "Idempotency-Key": "after-stop"},
        json={"name": "After stop"},
    )
    assert retry.json() == original.json()
    assert blocked.status_code == 409
    assert await global_api.store.count(Engagement) == 1


async def test_first_project_reference_records_only_new_admission_demand(global_api):
    from nebula.v3.runtime_delivery import HttpIntent

    project = await global_api.store.create(Engagement(name="First use"))
    intent = HttpIntent.capture(
        "POST",
        "/api/v1/chat/completions",
        b"",
        [(b"content-type", b"application/json")],
        ('{"engagement_id":"' + project.id + '"}').encode(),
    )
    assert await global_api.routing.project_for(intent) == project.id
    async with global_api.database.session() as session:
        row = await session.get(ProjectOwnershipRow, project.id)
        assert row.runtime_id is None and row.fencing_token == 0
