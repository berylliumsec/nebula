"""Authenticated Core and typed Butler use one durable memory-policy service."""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.database import MemoryBudgetAuditRow
from nebula.v3.domain import Engagement
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import GIB, MemoryAdministratorLimits
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio
TOKEN = "synthetic-memory-budget"
PATH = "/api/v1/memory-budgets"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def core(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    await store.create(Engagement(id="project", name="Synthetic project"))
    administrator = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    service = MemoryBudgetService(store.database, administrator=lambda: administrator)
    app = create_app(
        store,
        auth_token=TOKEN,
        memory_budget_service=service,
        execution_data_root=tmp_path / "execution",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            yield store, service, app, client
    finally:
        await store.database.dispose()


async def test_configuration_reads_back_and_survives_new_core_factory(core, tmp_path):
    _store, service, _app, client = core
    response = await client.get(PATH)
    assert response.headers["cache-control"] == "private, no-store"
    before = response.json()
    policy = before["policy"]
    policy["default_job"]["max_bytes"] = 4 * GIB
    policy["aggregate_bytes"] = 12 * GIB
    saved = await client.put(
        PATH, json={"expected_revision": before["revision"], "policy": policy}
    )
    assert saved.status_code == 200
    assert saved.json()["application_state"] == "unavailable"
    assert saved.json()["revision"] == 2
    assert (await client.get(PATH)).json() == saved.json()
    restored = NebulaStore(tmp_path / "core.db")
    try:
        fresh = create_app(
            restored,
            auth_token=TOKEN,
            memory_budget_service=MemoryBudgetService(
                restored.database, administrator=service.administrator
            ),
            execution_data_root=tmp_path / "restored",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fresh),
            base_url="http://fixture",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as other:
            assert (await other.get(PATH)).json() == saved.json()
    finally:
        await restored.database.dispose()


@pytest.mark.parametrize("value", [0, -1, True, "4294967296", 20 * GIB])
async def test_invalid_memory_values_are_rejected_without_losing_policy(core, value):
    _store, _service, _app, client = core
    before = (await client.get(PATH)).json()
    policy = {
        **before["policy"],
        "default_job": {**before["policy"]["default_job"], "max_bytes": value},
    }
    response = await client.put(
        PATH, json={"expected_revision": before["revision"], "policy": policy}
    )
    assert response.status_code == 422
    assert (await client.get(PATH)).json() == before


async def test_concurrent_browser_and_butler_saves_have_one_winner(core):
    _store, _service, app, client = core
    before = (await client.get(PATH)).json()
    body = {"expected_revision": before["revision"], "policy": before["policy"]}
    responses = await asyncio.gather(
        client.put(PATH, json=body), client.put(PATH, json=body)
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (await client.get(PATH)).json()["revision"] == 2
    gateway = SettingsAssistantApi(app, TOKEN)
    with pytest.raises(ValueError, match="409"):
        await gateway.request("project", method="PUT", path="memory-budgets", body=body)


async def test_memory_routes_require_normal_core_authorization(core):
    _store, _service, app, client = core
    before = (await client.get(PATH)).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as anonymous:
        for method, path, body in [
            ("GET", PATH, None),
            ("GET", PATH + "/jobs", None),
            ("PUT", PATH, {"expected_revision": 1, "policy": before["policy"]}),
            (
                "POST",
                PATH + "/jobs/unknown/apply",
                {"expected_revision": 1, "limits": before["policy"]["default_job"]},
            ),
        ]:
            assert (await anonymous.request(method, path, json=body)).status_code == 401
    assert (await client.get(PATH)).json() == before


async def test_butler_discovers_typed_actions_saves_and_verifies_with_actor_audit(
    core, tmp_path
):
    store, _service, app, client = core
    butler = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        "project",
        settings_api=SettingsAssistantApi(app, TOKEN),
    )
    matches = await butler.search({"query": "memory budget worker limits"})
    assert any(
        item["operation"] == "nebula.settings.memory_budget"
        for item in matches["matches"]
    )
    description = await butler.describe(
        {"operation": "nebula.settings.set_memory_budget"}
    )
    assert "expected_revision" in description["input_schema"]["properties"]
    current = await butler.invoke(
        {"operation": "nebula.settings.memory_budget", "arguments": {}}, change=False
    )
    policy = current["result"]["policy"]
    policy["default_job"]["max_bytes"] = 4 * GIB
    policy["aggregate_bytes"] = 12 * GIB
    saved = await butler.invoke(
        {
            "operation": "nebula.settings.set_memory_budget",
            "arguments": {
                "expected_revision": current["result"]["revision"],
                "policy": policy,
            },
        },
        change=True,
    )
    assert saved["read_back"] is True
    assert saved["result"]["policy"] == (await client.get(PATH)).json()["policy"]
    assert saved["result"]["application_state"] == "unavailable"
    usage = await butler.invoke(
        {"operation": "nebula.settings.memory_usage", "arguments": {}}, change=False
    )
    assert usage["jobs"] == []
    async with store.database.session() as session:
        audit = await session.scalar(
            select(MemoryBudgetAuditRow).where(
                MemoryBudgetAuditRow.action == "policy.save"
            )
        )
        assert audit.actor_id == "butler:project"
        assert audit.previous["default_job"]["max_bytes"] == 2 * GIB
        assert audit.proposed["default_job"]["max_bytes"] == 4 * GIB
        assert audit.result["application"] == "new_jobs"


async def test_public_actor_header_does_not_impersonate_butler(core):
    store, _service, _app, client = core
    current = (await client.get(PATH)).json()
    response = await client.put(
        PATH,
        headers={"X-Nebula-Actor": "butler:forged"},
        json={"expected_revision": current["revision"], "policy": current["policy"]},
    )
    assert response.status_code == 200
    async with store.database.session() as session:
        audit = await session.scalar(
            select(MemoryBudgetAuditRow).where(
                MemoryBudgetAuditRow.action == "policy.save"
            )
        )
        assert audit.actor_id == "operator"
