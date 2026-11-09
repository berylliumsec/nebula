"""Memory settings stay authoritative across host, API and project boundaries."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

from nebula.v3.database import (
    MemoryBudgetAuditRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import GIB, MemoryAdministratorLimits, MemoryBudgetError
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.runtime_host_services import host_service_client
from nebula.v3.runtime_memory import RuntimeMemoryAdmission
from nebula.v3.runtime_memory_control import MEMORY_CONTROL, RemoteMemorySettings
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_memory_budget import capability
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_http import server

pytestmark = pytest.mark.anyio
PATH = "/api/v1/memory-budgets"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def controls(managed):
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
            transport=httpx.ASGITransport(app=root.app),
            base_url="http://fixture",
            headers={"Authorization": "Bearer " + _TOKEN},
        ) as client:
            yield SimpleNamespace(
                root=root, managed=managed, budgets=budgets, client=client
            )
    finally:
        await root.close()


def reserve_policy(status, *, reserve=3 * GIB):
    policy = status["policy"]
    return {**policy, "reserve_bytes": reserve, "aggregate_bytes": 12 * GIB}


async def enable_workers(controls, program=_API_WORKER):
    current = (await controls.client.get(PATH)).json()
    saved = await controls.client.put(
        PATH,
        json={
            "expected_revision": current["revision"],
            "policy": reserve_policy(current),
        },
    )
    assert saved.status_code == 200
    await controls.managed.stage("v1", program)


async def private_peer(controls):
    await enable_workers(controls)
    identity = await controls.managed.host.spawn("v1", "execution")
    lease = await controls.root.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    async with controls.managed.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        secret = controls.managed.host.credentials.read(row.credential_reference)
    client = host_service_client(SimpleNamespace(identity=identity), secret)
    return lease, client, RemoteMemorySettings(client)


async def test_saved_floor_policy_is_repaired_through_host_before_api_admission(
    controls,
):
    await controls.managed.stage("v1", _API_WORKER)
    before = (await controls.client.get(PATH)).json()
    assert before["policy"]["reserve_bytes"] == 2 * GIB
    with pytest.raises(MemoryBudgetError, match="Core reserve"):
        await controls.root.prepare_api("v1")
    async with controls.managed.database.session() as session:
        assert list(await session.scalars(select(RuntimeProcessRow))) == []
    saved = await controls.client.put(
        PATH,
        json={
            "expected_revision": before["revision"],
            "policy": reserve_policy(before),
        },
    )
    assert (
        saved.status_code == 200 and saved.json()["policy"]["reserve_bytes"] == 3 * GIB
    )
    api = await controls.root.prepare_api("v1")
    await controls.root.promote_api(
        api.runtime_id,
        expected_revision=(await controls.root.coordinator.status()).revision,
    )
    observed = (await controls.client.get(PATH)).json()
    assert observed["policy"] == saved.json()["policy"]
    assert observed["runtime_reserved_bytes"] == 256 * 1024**2
    assert observed["application_state"] == "unavailable"


async def test_host_memory_api_retains_auth_revision_and_actor_safeguards(controls):
    assert (
        await controls.client.get(PATH, headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    current = (await controls.client.get(PATH)).json()
    body = {"expected_revision": current["revision"], "policy": reserve_policy(current)}
    first, second = await asyncio.gather(
        controls.client.put(
            PATH, json=body, headers={"X-Nebula-Memory-Actor": "butler:forged"}
        ),
        controls.client.put(PATH, json=body),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    async with controls.managed.database.session() as session:
        rows = list(
            await session.scalars(
                select(MemoryBudgetAuditRow).where(
                    MemoryBudgetAuditRow.action == "policy.save"
                )
            )
        )
    assert len(rows) == 1 and rows[0].actor_id == "operator"
    oversized = await controls.client.put(
        PATH,
        content=b"x" * (128 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


async def test_bound_private_butler_uses_the_same_validated_policy_api_and_audit(
    controls,
):
    lease, client, remote = await private_peer(controls)
    gateway = SettingsAssistantApi(
        controls.root.shared_services.memory_control.app,
        _TOKEN,
        memory_request=remote.request,
    )
    try:
        with execution_context(lease):
            current = await gateway.request(
                lease.project_id, method="GET", path="memory-budgets"
            )
            policy = current["result"]["policy"]
            policy["default_job"]["max_bytes"] = 4 * GIB
            saved = await gateway.request(
                lease.project_id,
                method="PUT",
                path="memory-budgets",
                body={
                    "expected_revision": current["result"]["revision"],
                    "policy": policy,
                },
            )
        assert saved["read_back"] is True
        observed = (await controls.client.get(PATH)).json()
        assert observed["policy"] == saved["result"]["policy"]
        assert observed["application_state"] == "unavailable"
        async with controls.managed.database.session() as session:
            audit = await session.scalar(
                select(MemoryBudgetAuditRow).where(
                    MemoryBudgetAuditRow.actor_id == "butler:admitted-project"
                )
            )
        assert audit.previous["default_job"]["max_bytes"] == 2 * GIB
        assert audit.proposed["default_job"]["max_bytes"] == 4 * GIB
    finally:
        await client.close()


async def test_private_memory_request_cannot_forge_peer_project_or_target_another_api(
    controls,
):
    lease, client, remote = await private_peer(controls)
    try:
        for changed in (
            replace(lease, runtime_id="forged"),
            replace(lease, project_id="synthetic-project"),
        ):
            result = await client.call(
                MEMORY_CONTROL,
                {"lease": asdict(changed), "method": "GET", "path": PATH},
            )
            assert result["ok"] is False and result["error"] == "lease_lost"
        result = await client.call(
            MEMORY_CONTROL,
            {
                "lease": asdict(lease),
                "method": "POST",
                "path": "/api/v1/chat/completions",
                "body": {},
            },
        )
        assert result["ok"] is False
        with execution_context(lease), pytest.raises(LeaseLost, match="bound project"):
            await remote.request("synthetic-project", method="GET", path=PATH)
        async with controls.managed.database.session() as session:
            await session.execute(
                update(ProjectOwnershipRow)
                .where(ProjectOwnershipRow.project_id == lease.project_id)
                .values(lease_expires_at_ms=0)
            )
        with execution_context(lease), pytest.raises(LeaseLost):
            await remote.request(lease.project_id, method="GET", path=PATH)
    finally:
        await client.close()


async def test_durable_memory_job_identity_selects_its_current_project_owner(controls):
    job = await controls.budgets.create_job(
        "compute-synthetic",
        project_id="admitted-project",
        resource_kind="fixture",
        resource_id="allocation",
        capabilities=capability(),
    )
    intent = HttpIntent.capture(
        "POST",
        PATH + "/jobs/" + job.id + "/apply",
        b"",
        [(b"content-type", b"application/json")],
        b'{"expected_revision":1}',
    )
    assert await controls.root.routing.project_for(intent) == "admitted-project"
    conflict = HttpIntent.capture(
        "POST",
        PATH + "/jobs/" + job.id + "/apply",
        b"project_id=synthetic-project",
        [],
        b"",
    )
    with pytest.raises(CoordinationConflict, match="multiple projects"):
        await controls.root.routing.project_for(conflict)


_BUTLER_WORKER_INJECTION = """
        from nebula.v3.butler_capabilities import ButlerCapabilities
        @app.post('/api/v1/engagements/{project_id}/fixture-butler-memory')
        async def change_memory(project_id: str, body: dict):
            assert project_id == lease.project_id
            butler = await ButlerCapabilities.create(app.state.store, app.state.artifact_store, project_id, settings_api=app.state.settings_assistant_api)
            return await butler.invoke({'operation':'nebula.settings.set_memory_budget','arguments':body}, change=True)
        return app
"""


async def test_actual_project_kernel_butler_can_save_and_read_back_host_budgets(
    controls,
):
    from tests.v3.test_execution_worker_process import _HANDOFF_WORKER

    program = _HANDOFF_WORKER.replace("        return app\n", _BUTLER_WORKER_INJECTION)
    await enable_workers(controls, program)
    execution = await controls.root.prepare_execution("v1")
    await controls.root.attach_execution("admitted-project", execution.runtime_id)
    api = await controls.root.prepare_api("v1")
    await controls.root.promote_api(
        api.runtime_id,
        expected_revision=(await controls.root.coordinator.status()).revision,
    )
    current = (await controls.client.get(PATH)).json()
    policy = current["policy"]
    policy["default_job"]["max_bytes"] = 4 * GIB
    async with server(controls.root.app) as endpoint:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
            base_url="http://fixture",
            trust_env=False,
            timeout=20,
        ) as client:
            response = await client.post(
                "/api/v1/engagements/admitted-project/fixture-butler-memory",
                headers={
                    "Authorization": "Bearer " + _TOKEN,
                    "Idempotency-Key": "butler-memory",
                },
                json={"expected_revision": current["revision"], "policy": policy},
            )
    assert response.status_code == 200, response.text
    assert response.json()["read_back"] is True
    assert (await controls.client.get(PATH)).json()["policy"]["default_job"][
        "max_bytes"
    ] == 4 * GIB
    async with controls.managed.database.session() as session:
        assert await session.scalar(
            select(MemoryBudgetAuditRow.id).where(
                MemoryBudgetAuditRow.actor_id == "butler:admitted-project"
            )
        )
