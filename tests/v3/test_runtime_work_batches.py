"""Real SQL/API receipts and host-owned Work progress, without worker processes.

Runtime registrations and the private capability response are synthetic. No
provider, harness, OCI runtime or external service is invoked.
"""

import asyncio
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeDeliveryRow,
    RuntimeProcessRow,
    RuntimeWorkBatchProjectRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    PairedDeviceSession,
    ScopePolicy,
    WorkItem,
    WorkUpdate,
    utc_now,
)
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import (
    CommandReceipt,
    CoordinationConflict,
    LEASE_MS,
    LeaseLost,
)
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery, _json
from nebula.v3.runtime_processes import RuntimeProcessHost
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.runtime_work_batches import (
    RuntimeWorkBatches,
    WORK_BATCH_BARRIER,
    WORK_BATCH_COMMAND,
    WORK_BATCH_FEATURE,
    _import_id,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from nebula.v3.work import WorkImportBatch, WorkProjectBatchUpdate
from tests.v3.test_deployment_lifecycle import manifest
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
TOKEN = "synthetic-work-batch-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
SETTINGS = PoolSettings(5, 12, 5, 2)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def runtime(value, identity, *, supported=True):
    version = "supported" if supported else "older"
    await value.coordinator.stage(
        manifest(version, features=[WORK_BATCH_FEATURE] if supported else []),
        Path("/synthetic/releases") / version,
        "a" * 64,
    )
    await value.coordination.register_runtime(identity, version)
    async with value.database.session() as session:
        session.add(
            RuntimeProcessRow(
                runtime_id=identity,
                version=version,
                role="execution",
                pid=0,
                process_birth="synthetic-no-process",
                endpoint="unix:///synthetic/" + identity,
                credential_reference=uuid4().hex,
                state="ready",
                updated_at_ms=value.clock[0],
            )
        )


def intent(method, body):
    return HttpIntent.capture(
        method,
        "/api/v1/work/import" if method == "POST" else "/api/v1/work/projects",
        b"",
        [(b"content-type", b"application/json")],
        json.dumps(body).encode(),
    )


async def accepted(value, method="PATCH", body=None, key=None, actor="operator"):
    return await value.acceptor.accept(
        intent(method, body or {"project_ids": ["one", "two"], "work_enabled": True}),
        actor_id=actor,
        idempotency_key=key or uuid4().hex,
    )


async def page(value, receipt):
    result = await value.delivery.page("operator", receipt.command_id, limit=64)
    head = next(
        (json.loads(frame.content) for frame in result.frames if frame.kind == "head"),
        None,
    )
    body = b"".join(frame.content for frame in result.frames if frame.kind == "body")
    return result, head["status"] if head else None, json.loads(body) if body else None


@pytest.fixture
async def batch(location, tmp_path):
    database = Database(location, pool_settings=SETTINGS)
    await database.initialize()
    database._bootstrap = False
    store = NebulaStore(database)
    coordinator = DeploymentCoordinator(database)
    coordination, clock = coordinator.coordination, [1_000_000]

    async def now(_session):
        return clock[0]

    coordination._clock_ms = now
    host = RuntimeProcessHost(coordinator, tmp_path / "core", pool_settings=SETTINGS)
    await host.start()
    delivery = RuntimeDelivery(coordination, RuntimeVault(host.root / "payloads"))
    service = RuntimeWorkBatches(delivery, host=host)
    available = [True]

    async def capability():
        # Manually stepped tests expose each SQL boundary. The owned-loop test
        # below uses a restarted real host and the actual service capability.
        return {
            "protocol": "nebula.work-batches/v1",
            "feature": WORK_BATCH_FEATURE,
            "ready": available[0],
        }

    acceptor = RuntimeWorkBatches(delivery, availability=capability)
    value = SimpleNamespace(
        database=database,
        location=location,
        store=store,
        coordinator=coordinator,
        coordination=coordination,
        clock=clock,
        host=host,
        service=service,
        delivery=delivery,
        acceptor=acceptor,
        available=available,
        clients=[],
        apps=[],
    )
    for identity in ("one", "two", "parent"):
        await store.create(
            Engagement(id=identity, name="Synthetic " + identity, work_enabled=False)
        )

    async def routing(_intent):
        pytest.fail("A Work batch must not choose one project's execution owner")

    async def upstream(_scope):
        pytest.fail("SQL Work batches must not invoke an execution worker")

    async def client():
        app = create_api_worker(
            store,
            delivery,
            operator_token=TOKEN,
            project_for=routing,
            upstream_for=upstream,
            work_batches=acceptor,
            command_start_timeout=0.02,
            poll_interval=0.01,
            dispose_database=False,
        )
        value.apps.append(app)
        result = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
        )
        value.clients.append(result)
        return result

    value.client = client
    try:
        yield value
    finally:
        for opened in value.clients:
            await opened.aclose()
        for app in value.apps:
            await app.state.viewer_proxy.close()
        await acceptor.close()
        await service.close()
        await host.close()
        await store.event_history.close()
        await database.dispose()


async def test_public_new_project_import_replays_after_api_replacement(batch):
    client = await batch.client()
    body = {
        "source": "synthetic",
        "projects": [
            {
                "external_id": "new",
                "name": "Imported child",
                "parent_engagement_id": "parent",
                "items": [
                    {
                        "external_id": "task",
                        "title": "A task",
                        "update": {"summary": "Imported check-in"},
                    }
                ],
            }
        ],
    }
    headers = {**AUTH, "Idempotency-Key": "import-new"}
    assert (
        await client.post(
            "/api/v1/work/import", headers={"Idempotency-Key": "unauth"}, json=body
        )
    ).status_code == 401
    pending = await client.post("/api/v1/work/import", headers=headers, json=body)
    assert pending.status_code == 202, pending.text
    command = pending.headers["x-nebula-command-id"]
    assert await batch.store.count(Engagement) == 3
    async with batch.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
    await batch.service.reconcile()
    replacement = await batch.client()
    response = await replacement.get(
        f"/api/v1/runtime/requests/{command}/response", headers=AUTH
    )
    assert response.status_code == 200, response.text
    imported = response.json()["projects"][0]
    project = await batch.store.get(Engagement, imported["engagement_id"])
    assert project.parent_engagement_id == "parent"
    assert (
        await batch.store.get(ScopePolicy, project.scope_policy_id)
    ).engagement_id == project.id
    assert await batch.store.count(WorkItem) == await batch.store.count(WorkUpdate) == 1
    lookup = await replacement.get(
        "/api/v1/runtime/requests/lookup",
        headers=AUTH,
        params={"idempotency_key": "import-new"},
    )
    assert (
        lookup.json()["command_id"] == command and lookup.json()["state"] == "succeeded"
    )
    retry = await replacement.post("/api/v1/work/import", headers=headers, json=body)
    assert retry.status_code == 200 and retry.json() == response.json()
    assert retry.headers["x-nebula-command-id"] == command


async def test_natural_import_retry_preserves_ids_and_operator_edits(batch):
    body = {
        "source": "stable",
        "projects": [
            {
                "external_id": "project",
                "name": "Original",
                "items": [{"external_id": "item", "title": "Original item"}],
            }
        ],
    }
    receipt = await accepted(batch, "POST", body)
    await batch.service.reconcile()
    _, status, result = await page(batch, receipt)
    assert status == 200
    imported = result["projects"][0]
    await batch.store.update(
        Engagement, imported["engagement_id"], {"name": "Operator name"}
    )
    await batch.store.update(
        WorkItem, imported["items"][0]["item_id"], {"title": "Operator item"}
    )
    again = await accepted(batch, "POST", body)
    await batch.service.reconcile()
    assert (await page(batch, again))[2] == result
    assert (
        await batch.store.get(Engagement, imported["engagement_id"])
    ).name == "Operator name"
    assert (
        await batch.store.get(WorkItem, imported["items"][0]["item_id"])
    ).title == "Operator item"
    assert await batch.store.count(WorkItem) == 1


async def test_multi_project_metadata_is_atomic_and_revision_checked(batch):
    await runtime(batch, "worker")
    leases = [
        await batch.coordination.claim_project(identity, "worker")
        for identity in ("one", "two")
    ]
    before = [await batch.store.get(Engagement, lease.project_id) for lease in leases]
    body = {
        "project_ids": ["two", "one"],
        "parent_engagement_id": "parent",
        "work_enabled": True,
    }
    receipt = await accepted(batch, body=body)
    await batch.service.reconcile()
    assert (await page(batch, receipt))[1:] == (200, {"updated": 2})
    for original in before:
        current = await batch.store.get(Engagement, original.id)
        assert current.revision == original.revision + 1
        assert current.work_enabled and current.parent_engagement_id == "parent"
    retry = await accepted(batch, body=body)
    await batch.service.reconcile()
    assert (await page(batch, retry))[2] == {"updated": 0}
    for original in before:
        assert (
            await batch.store.get(Engagement, original.id)
        ).revision == original.revision + 1


@pytest.mark.parametrize("invalid", ["missing-later", "cycle", "schema"])
async def test_invalid_batch_has_error_receipt_and_no_partial_entities(batch, invalid):
    body = {
        "source": "invalid",
        "projects": [
            {"external_id": "first", "name": "Must not appear"},
            {"external_id": "second", "name": "Missing", "engagement_id": "absent"},
        ],
    }
    method, expected = "POST", 404
    if invalid == "cycle":
        body, method, expected = (
            {"project_ids": ["one", "two"], "parent_engagement_id": "one"},
            "PATCH",
            409,
        )
    elif invalid == "schema":
        body, expected = {"source": "invalid", "projects": []}, 422
    receipt = await accepted(batch, method, body, key="invalid")
    response, status, _ = await page(batch, receipt)
    assert response.state == "failed" and status == expected
    assert await batch.store.count(Engagement) == 3
    assert (
        await batch.store.count(WorkItem) == await batch.store.count(ScopePolicy) == 0
    )
    async with batch.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeWorkBatchProjectRow)
            )
            == 0
        )
    assert await accepted(batch, method, body, key="invalid") == receipt


async def test_sql_failure_rolls_back_every_project_scope_item_and_response(
    batch, monkeypatch
):
    body = {
        "source": "atomic",
        "projects": [
            {
                "external_id": "first",
                "name": "First",
                "items": [{"external_id": "task", "title": "Task"}],
            },
            {"external_id": "second", "name": "Second"},
        ],
    }
    original = batch.service._apply

    async def reject_after_writes(value):
        await original(value)
        raise ConflictError("Synthetic late SQL conflict")

    monkeypatch.setattr(batch.service, "_apply", reject_after_writes)
    receipt = await accepted(batch, "POST", body)
    await batch.service.reconcile()
    response, status, result = await page(batch, receipt)
    assert (
        response.state == "failed"
        and status == 409
        and "late SQL conflict" in result["detail"]
    )
    assert await batch.store.count(Engagement) == 3
    assert (
        await batch.store.count(ScopePolicy) == await batch.store.count(WorkItem) == 0
    )
    async with batch.database.session() as session:
        participants = list(await session.scalars(select(RuntimeWorkBatchProjectRow)))
        assert len(participants) == 2 and all(
            row.state == "failed" for row in participants
        )
        barriers = list(
            await session.scalars(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.name == WORK_BATCH_BARRIER
                )
            )
        )
        assert all(row.state == "failed" for row in barriers)


async def test_normal_work_lane_cannot_pass_queued_batch_barrier(batch):
    await runtime(batch, "worker")
    lease = await batch.coordination.claim_project("one", "worker")
    prior = await batch.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="prior",
        name="inert",
        payload={},
    )
    receipt = await accepted(batch, body={"project_ids": ["one"], "work_enabled": True})
    await batch.service.reconcile()
    later = await batch.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="later",
        name="inert",
        payload={},
    )
    claimed = await batch.coordination.claim_next(lease, lane="work")
    assert claimed.receipt == prior
    await batch.coordination.finish_command(lease, prior.command_id, result={})
    assert await batch.coordination.claim_next(lease, lane="work") is None
    assert (await page(batch, receipt))[0].state == "queued"
    await batch.service.reconcile()
    assert (await page(batch, receipt))[1] == 200
    assert (await batch.coordination.claim_next(lease, lane="work")).receipt == later


async def test_uncertain_work_never_resets_or_replays_through_batch(batch):
    await runtime(batch, "worker")
    lease = await batch.coordination.claim_project("one", "worker")
    prior = await batch.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="unknown",
        name="inert",
        payload={},
    )
    await batch.coordination.claim_next(lease)
    await batch.coordination.finish_command(
        lease,
        prior.command_id,
        state="uncertain",
        result={"detail": "Unknown fixture effect"},
    )
    receipt = await accepted(batch, body={"project_ids": ["one"], "work_enabled": True})
    for _ in range(2):
        await batch.service.reconcile()
    assert (await page(batch, receipt))[0].state == "queued"
    assert not (await batch.store.get(Engagement, "one")).work_enabled
    async with batch.database.session() as session:
        assert (
            await session.get(RuntimeCommandRow, prior.command_id)
        ).state == "uncertain"
    assert await batch.coordination.claim_next(lease, lane="work") is None


async def test_old_owner_receives_no_barrier_then_progresses_on_compatible_lease(batch):
    await runtime(batch, "old", supported=False)
    old = await batch.coordination.claim_project("one", "old")
    receipt = await accepted(batch, body={"project_ids": ["one"], "work_enabled": True})
    await batch.service.reconcile()
    async with batch.database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RuntimeCommandRow)
                .where(RuntimeCommandRow.name == WORK_BATCH_BARRIER)
            )
            == 0
        )
        assert (
            await session.get(RuntimeWorkBatchProjectRow, (receipt.command_id, "one"))
        ).state == "waiting"
    ordinary = await batch.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="old-work",
        name="inert",
        payload={},
    )
    assert (await batch.coordination.claim_next(old)).receipt == ordinary
    await batch.coordination.finish_command(old, ordinary.command_id, result={})
    batch.clock[0] += LEASE_MS + 1
    await runtime(batch, "replacement")
    new = await batch.coordination.claim_project("one", "replacement")
    assert new.fencing_token == old.fencing_token + 1
    await batch.service.reconcile()
    assert (await page(batch, receipt))[1] == 200
    with pytest.raises(LeaseLost):
        async with batch.coordination.fenced(old):
            pytest.fail("Old owner entered a batch-updated project")


async def test_expiry_after_sql_writes_rolls_back_then_new_fence_completes(
    batch, monkeypatch
):
    await runtime(batch, "worker")
    old = await batch.coordination.claim_project("one", "worker")
    receipt = await accepted(batch)
    original = batch.service._apply

    async def expire_after_writes(body):
        result = await original(body)
        batch.clock[0] += LEASE_MS + 1
        return result

    monkeypatch.setattr(batch.service, "_apply", expire_after_writes)
    await batch.service.reconcile()
    assert (await page(batch, receipt))[0].state == "queued"
    for identity in ("one", "two"):
        assert not (await batch.store.get(Engagement, identity)).work_enabled
    async with batch.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeDeliveryRow))
            == 0
        )
    monkeypatch.setattr(batch.service, "_apply", original)
    await runtime(batch, "replacement")
    new = await batch.coordination.claim_project("one", "replacement")
    await batch.service.reconcile()
    assert new.fencing_token == old.fencing_token + 1
    assert (await page(batch, receipt))[1] == 200


async def test_ownerless_nonzero_counter_stays_monotonic(batch):
    async with batch.database.session() as session:
        session.add(
            ProjectOwnershipRow(
                project_id="one",
                runtime_id=None,
                fencing_token=7,
                lease_expires_at_ms=0,
                command_sequence=0,
            )
        )
    receipt = await accepted(batch, body={"project_ids": ["one"], "work_enabled": True})
    await batch.service.reconcile()
    assert (await page(batch, receipt))[1] == 200
    async with batch.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.runtime_id is None and owner.fencing_token == 7
    await runtime(batch, "next")
    assert (await batch.coordination.claim_project("one", "next")).fencing_token == 8


async def test_handoff_and_shutdown_holds_keep_saved_batch_queued(batch):
    receipt = await accepted(batch)
    async with batch.database.session() as session:
        session.add(
            ProjectHandoffRow(
                project_id="one",
                revision=1,
                source_runtime_id="old",
                source_fencing_token=1,
                target_runtime_id="new",
                target_version="supported",
                phase="draining",
                admission_held=True,
                blockers=[],
                checkpoint={},
                updated_at_ms=batch.clock[0],
            )
        )
    await batch.service.reconcile()
    assert (await page(batch, receipt))[0].state == "queued"
    async with batch.database.session() as session:
        handoff = await session.get(ProjectHandoffRow, "one")
        handoff.phase, handoff.admission_held = "verified", False
        (await session.get(DeploymentHostRow, "host")).shutdown_requested = True
    await batch.service.reconcile()
    assert (await page(batch, receipt))[0].state == "queued"
    with pytest.raises(CoordinationConflict, match="shutdown"):
        await accepted(batch)
    async with batch.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).shutdown_requested = False
    await batch.service.reconcile()
    assert (await page(batch, receipt))[1] == 200


async def test_capability_refusal_creates_nothing_but_existing_receipt_remains(batch):
    batch.available[0] = False
    client = await batch.client()
    body = {"project_ids": ["one", "two"], "work_enabled": True}
    response = await client.patch(
        "/api/v1/work/projects", headers={**AUTH, "Idempotency-Key": "held"}, json=body
    )
    assert (
        response.status_code == 503
        and "supporting coordinated Work batches" in response.text
    )
    async with batch.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeWorkBatchProjectRow)
            )
            == 0
        )
    batch.available[0] = True
    receipt = await accepted(batch, key="saved")
    batch.available[0] = False
    assert await accepted(batch, key="saved") == receipt
    with pytest.raises(CoordinationConflict):
        await accepted(batch, key="saved", body={**body, "work_enabled": False})


async def test_overlapping_batch_order_and_concurrent_acceptance_are_immutable(batch):
    body = {"project_ids": ["one", "two"], "work_enabled": True}
    first, replay = await asyncio.gather(
        *(accepted(batch, body=body, key="same") for _ in range(2))
    )
    assert first == replay
    second = await accepted(batch, body={**body, "work_enabled": False})
    # Try the later parent first: it cannot acquire a lane ahead of the first.
    await batch.service._advance(
        second.command_id,
        "operator",
        WorkProjectBatchUpdate.model_validate({**body, "work_enabled": False}),
    )
    assert (await page(batch, second))[0].state == "queued"
    await batch.service.reconcile()
    assert (await page(batch, first))[1] == (await page(batch, second))[1] == 200
    assert not (await batch.store.get(Engagement, "one")).work_enabled
    async with batch.database.session() as session:
        parents = list(
            await session.scalars(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.name == WORK_BATCH_COMMAND
                )
            )
        )
        assert len(parents) == 2
        assert all(row.project_id == GLOBAL_EXECUTION_PROJECT for row in parents)


async def test_new_host_loop_resumes_saved_sql_only_batch_without_browser(batch):
    receipt = await accepted(batch)
    await batch.service.close()
    await batch.host.close()
    database = Database(batch.location, pool_settings=SETTINGS)
    coordinator = DeploymentCoordinator(database)
    host = RuntimeProcessHost(coordinator, batch.host.data_root, pool_settings=SETTINGS)
    await host.start()
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(host.root / "payloads")
    )
    resumed = RuntimeWorkBatches(delivery, host=host)
    try:
        assert not resumed.capability()["ready"]
        await resumed.start()
        assert resumed.capability()["ready"]
        deadline = asyncio.get_running_loop().time() + 5
        while (await delivery.page("operator", receipt.command_id)).state == "queued":
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert (await page(batch, receipt))[1] == 200
        for identity in ("one", "two"):
            assert (await batch.store.get(Engagement, identity)).work_enabled
    finally:
        await resumed.close()
        assert not resumed.capability()["ready"]
        await host.close()
        await database.dispose()


async def test_metadata_batch_and_project_reads_never_admit_workers(batch):
    await batch.coordinator.stage(
        manifest("supported", features=[WORK_BATCH_FEATURE]),
        Path("/synthetic/releases/supported"),
        "a" * 64,
    )
    async with batch.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "synthetic-api"
    calls = []

    async def prepare(_version):
        identity = "automatic-" + str(len(calls))
        calls.append(identity)
        await runtime(batch, identity)
        return SimpleNamespace(runtime_id=identity)

    async def attach(project_id, runtime_id):
        return await batch.coordination.claim_project(project_id, runtime_id)

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=batch.coordinator,
            store=batch.store,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="supported",
    )
    receipt = await accepted(batch)
    await batch.service.start()
    deadline = asyncio.get_running_loop().time() + 5
    while (await page(batch, receipt))[0].state == "queued":
        assert asyncio.get_running_loop().time() < deadline
        await automatic.reconcile()
        await asyncio.sleep(0.01)
    await automatic.reconcile()
    assert calls == []
    async with batch.database.session() as session:
        owners = list(await session.scalars(select(ProjectOwnershipRow)))
        assert len(owners) == 2 and all(not row.admission_requested for row in owners)
    routing = RuntimeRouting(
        batch.coordination, batch.host.credentials.directory, request_admission=True
    )
    with pytest.raises(RuntimeIpcError, match="no live execution owner"):
        await routing.upstream_for(
            {
                "path": "/api/v1/work/items",
                "query_string": b"engagement_id=one",
                "method": "GET",
            }
        )
    await automatic.reconcile()
    assert calls == []
    async with batch.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "one")).runtime_id is None
        assert (await session.get(ProjectOwnershipRow, "two")).runtime_id is None


async def test_open_chat_created_without_routing_gets_its_exact_project_worker(batch):
    project = await batch.store.create(Engagement(name="Direct chat project"))
    await batch.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Created directly by Core",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    async with batch.database.session() as session:
        api_host = await session.get(DeploymentHostRow, "host")
        api_host.api_runtime_id = "synthetic-api"

    prepared = []

    async def prepare(_version):
        runtime_id = f"direct-chat-worker-{len(prepared)}"
        prepared.append(runtime_id)
        await runtime(batch, runtime_id)
        return SimpleNamespace(runtime_id=runtime_id)

    async def attach(project_id, runtime_id):
        return await batch.coordination.claim_project(project_id, runtime_id)

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=batch.coordinator,
            store=batch.store,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="supported",
    )
    await automatic.reconcile()

    async with batch.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, project.id)
        assert owner.runtime_id == prepared[0]
    assert len(prepared) == 1


async def test_legacy_queued_goal_creates_admit_their_exact_five_projects(batch):
    children = [
        await batch.store.create(Engagement(name=f"Legacy goal lane {index}"))
        for index in range(5)
    ]
    for child in children:
        intent = HttpIntent.capture(
            "POST",
            "/api/v1/chat/goal-conversations",
            b"",
            [(b"content-type", b"application/json")],
            json.dumps(
                {
                    "engagement_id": child.id,
                    "objective": f"Start lane for {child.id}",
                    "completion_criteria": ["The lane conversation is created"],
                }
            ).encode(),
        )
        receipt = await batch.delivery.accept(
            intent,
            project_id=child.id,
            actor_id="operator",
            idempotency_key=f"legacy-goal-create-{child.id}",
        )
        async with batch.database.session() as session:
            command = await session.get(RuntimeCommandRow, receipt.command_id)
            assert command.chat_start is False
            owner = await session.get(ProjectOwnershipRow, child.id)
            owner.retirement_requested = True
    async with batch.database.session() as session:
        api_host = await session.get(DeploymentHostRow, "host")
        api_host.api_runtime_id = "synthetic-api"

    prepared = []

    async def prepare(_version):
        runtime_id = f"legacy-goal-worker-{len(prepared)}"
        prepared.append(runtime_id)
        await runtime(batch, runtime_id)
        return SimpleNamespace(runtime_id=runtime_id)

    async def attach(project_id, runtime_id):
        return await batch.coordination.claim_project(project_id, runtime_id)

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=batch.coordinator,
            store=batch.store,
            host=batch.host,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="supported",
    )
    await automatic.reconcile()
    async with batch.database.session() as session:
        held_commands = list(
            await session.scalars(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.project_id.in_([child.id for child in children])
                )
            )
        )
        assert len(held_commands) == 5
        assert all(not command.chat_start for command in held_commands)
    assert prepared == []
    async with batch.database.session() as session:
        for child in children:
            (
                await session.get(ProjectOwnershipRow, child.id)
            ).retirement_requested = False
    await automatic.reconcile()
    async with batch.database.session() as session:
        for index, child in enumerate(children):
            owner = await session.get(ProjectOwnershipRow, child.id)
            command = await session.scalar(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.project_id == child.id
                )
            )
            assert owner.runtime_id == prepared[index]
            assert command.chat_start is True
    assert len(prepared) == 5


async def test_parent_chat_does_not_admit_children_and_chat_start_admits_exact_child(
    batch,
):
    parent = await batch.store.create(
        Engagement(id="parent-chat-project", name="Parent")
    )
    children = [
        await batch.store.create(
            Engagement(
                id=f"child-chat-project-{index}",
                name=f"Child {index}",
                parent_engagement_id=parent.id,
            )
        )
        for index in range(5)
    ]
    await batch.store.create(
        ChatSession(
            engagement_id=parent.id,
            title="Parent conversation",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    for child in children:
        await batch.store.create(
            ChatSession(
                engagement_id=child.id,
                title="Archived child history",
                provider_profile_id="synthetic-provider",
                model="synthetic-model",
                metadata={"archived_at": "2026-11-13T00:00:00+00:00"},
            )
        )
    async with batch.database.session() as session:
        session.add_all(
            [
                ProjectOwnershipRow(
                    project_id=project_id,
                    runtime_id=None,
                    fencing_token=0,
                    lease_expires_at_ms=0,
                    command_sequence=0,
                    admission_requested=False,
                )
                for project_id in (parent.id, *(child.id for child in children))
            ]
        )
        host = await session.get(DeploymentHostRow, "host")
        if host is None:
            host = DeploymentHostRow(
                key="host",
                revision=1,
                target_version="supported",
                shutdown_requested=False,
                detail="Synthetic admission fixture",
            )
            session.add(host)
        host.api_runtime_id = "synthetic-api"

    calls = []

    async def prepare(_version):
        identity = f"chat-worker-{len(calls)}"
        calls.append(identity)
        await runtime(batch, identity)
        return SimpleNamespace(runtime_id=identity)

    async def attach(project_id, runtime_id):
        return await batch.coordination.claim_project(project_id, runtime_id)

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=batch.coordinator,
            store=batch.store,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="supported",
    )
    await automatic.reconcile()
    async with batch.database.session() as session:
        parent_owner = await session.get(ProjectOwnershipRow, parent.id)
        assert parent_owner.runtime_id == calls[0]
        for child in children:
            child_owner = await session.get(ProjectOwnershipRow, child.id)
            assert child_owner.runtime_id is None
    assert len(calls) == 1

    for child in children:
        await batch.coordination.submit(
            project_id=child.id,
            actor_id="operator",
            idempotency_key=f"child-chat-start-{child.id}",
            name="http.invoke/v1",
            payload={"format": 1, "request_reference": "synthetic-chat"},
            chat_start=True,
        )
    await automatic.reconcile()
    async with batch.database.session() as session:
        for index, child in enumerate(children, start=1):
            child_owner = await session.get(ProjectOwnershipRow, child.id)
            assert child_owner.runtime_id == calls[index]
    assert len(calls) == 6


async def test_auto_admission_retires_worker_after_exact_project_chat_archives(batch):
    await runtime(batch, "open-chat-worker")
    lease = await batch.coordination.claim_project("one", "open-chat-worker")
    chat = await batch.store.create(
        ChatSession(
            engagement_id="one",
            title="Conversation to close",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    async with batch.database.session() as session:
        api_host = await session.get(DeploymentHostRow, "host")
        api_host.api_runtime_id = "synthetic-api"

    retired = []
    reconciled = []

    async def retire_idle_project(project_id):
        retired.append(project_id)
        return True

    async def reconcile_idle_project_retirements():
        reconciled.append(True)
        return []

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=batch.coordinator,
            store=batch.store,
            host=SimpleNamespace(
                retire_idle_project=retire_idle_project,
                reconcile_idle_project_retirements=reconcile_idle_project_retirements,
            ),
        ),
        default_version="supported",
    )
    await automatic.reconcile()
    assert retired == []

    await batch.store.update(
        ChatSession,
        chat.id,
        {"metadata": {"archived_at": "2026-11-13T00:00:00+00:00"}},
        expected_revision=chat.revision,
    )
    await automatic.reconcile()
    assert reconciled and retired == ["one"]
    assert lease.runtime_id == "open-chat-worker"


async def test_revoked_actor_cannot_apply_and_receipt_lookup_stays_actor_scoped(batch):
    device = await batch.store.create(
        PairedDeviceSession(
            name="Synthetic paired browser",
            token_sha256="a" * 64,
            csrf_sha256="b" * 64,
            idle_expires_at=utc_now() + timedelta(hours=1),
            absolute_expires_at=utc_now() + timedelta(days=1),
        )
    )
    actor = "device:" + device.id
    receipt = await accepted(batch, actor=actor, key="actor-key")
    with pytest.raises(NotFoundError):
        await batch.delivery.lookup("operator", "actor-key")
    assert await batch.delivery.lookup(actor, "actor-key") == receipt.command_id
    await batch.store.update(PairedDeviceSession, device.id, {"revoked_at": utc_now()})
    await batch.service.reconcile()
    response = await batch.delivery.page(actor, receipt.command_id, limit=64)
    assert response.state == "failed"
    head = json.loads(
        next(frame.content for frame in response.frames if frame.kind == "head")
    )
    assert head["status"] == 401
    assert not (await batch.store.get(Engagement, "one")).work_enabled
    with pytest.raises(NotFoundError):
        await batch.delivery.page("operator", receipt.command_id)


@pytest.mark.parametrize("workspace", ["", "   "])
async def test_blank_workspace_fails_before_participants_and_corrected_import_progresses(
    batch, workspace, monkeypatch
):
    import nebula.v3.runtime_work_batches as batches

    def reject_cwd_resolution(_value):
        pytest.fail("A blank or omitted workspace must never resolve to Core's cwd")

    monkeypatch.setattr(
        batches, "_resolve_engagement_workspace_path", reject_cwd_resolution
    )
    client = await batch.client()
    body = {
        "source": "blank-workspace",
        "projects": [
            {
                "external_id": "project",
                "name": "Imported project",
                "workspace_path": workspace,
            }
        ],
    }
    headers = {**AUTH, "Idempotency-Key": "blank"}
    invalid = await client.post("/api/v1/work/import", headers=headers, json=body)
    assert invalid.status_code == 422 and "must not be blank" in invalid.text
    failed_id = invalid.headers["x-nebula-command-id"]
    async with batch.database.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeWorkBatchProjectRow)
            )
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
    body["projects"][0]["workspace_path"] = None
    pending = await client.post(
        "/api/v1/work/import",
        headers={**AUTH, "Idempotency-Key": "corrected"},
        json=body,
    )
    assert pending.status_code == 202
    await batch.service.reconcile()
    response = await client.get(
        f"/api/v1/runtime/requests/{pending.headers['x-nebula-command-id']}/response",
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    project = await batch.store.get(
        Engagement, response.json()["projects"][0]["engagement_id"]
    )
    assert project.workspace_path is None
    replay = await client.get(
        f"/api/v1/runtime/requests/{failed_id}/response", headers=AUTH
    )
    assert replay.status_code == 422 and replay.json() == invalid.json()


async def test_legacy_staged_invalid_domain_fails_and_next_batch_advances(batch):
    # Seed exactly the immutable encrypted parent/participant projection left
    # by the old acceptance code, then use real staging to commit its barrier.
    body = {
        "source": "legacy-invalid",
        "projects": [
            {
                "external_id": "bad",
                "name": "Never appears",
                "workspace_path": "",
            }
        ],
    }
    request = intent("POST", body)
    request_reference = batch.delivery.vault.put(
        _json(request.model_dump(mode="json")),
        purpose="intent:operator",
    )
    normalized_reference = batch.delivery.vault.put(
        _json(WorkImportBatch.model_validate(body).model_dump(mode="json")),
        purpose="work-batch-plan:operator",
    )
    original = {"format": 1, "request_reference": request_reference}
    batch_id = str(uuid4())
    project_id = _import_id(body["source"], "bad")
    async with batch.coordination._write() as session:
        session.add(
            RuntimeCommandRow(
                id=batch_id,
                project_id=GLOBAL_EXECUTION_PROJECT,
                actor_id="operator",
                idempotency_key="legacy-invalid",
                sequence=1,
                name=WORK_BATCH_COMMAND,
                lane="work",
                payload={**original, "normalized_reference": normalized_reference},
                payload_hash=sha256(
                    _json(
                        {
                            "project_id": GLOBAL_EXECUTION_PROJECT,
                            "name": WORK_BATCH_COMMAND,
                            "payload": original,
                            "expected_revision": None,
                        }
                    )
                ).hexdigest(),
                accepted_at_ms=batch.clock[0],
                state="queued",
            )
        )
        await session.flush()
        session.add(
            RuntimeWorkBatchProjectRow(
                batch_id=batch_id, project_id=project_id, state="waiting"
            )
        )
    assert await batch.service._stage(batch_id, WorkImportBatch.model_validate(body))
    async with batch.database.session() as session:
        participant = await session.get(
            RuntimeWorkBatchProjectRow, (batch_id, project_id)
        )
        barrier_id = participant.barrier_command_id
        assert participant.state == "barrier"
        assert (await session.get(RuntimeCommandRow, barrier_id)).state == "queued"
    await runtime(batch, "worker")
    lease = await batch.coordination.claim_project("one", "worker")
    unknown = await batch.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="unrelated",
        name="inert",
        payload={},
    )
    await batch.coordination.claim_next(lease)
    await batch.coordination.finish_command(
        lease,
        unknown.command_id,
        state="uncertain",
        result={"detail": "Unknown unrelated fixture effect"},
    )
    valid = await accepted(batch, body={"project_ids": ["two"], "work_enabled": True})
    await batch.service.reconcile()
    failed = CommandReceipt(batch_id, GLOBAL_EXECUTION_PROJECT, 1, batch.clock[0])
    result, status, detail = await page(batch, failed)
    assert (
        result.state == "failed"
        and status == 422
        and "record schema" in detail["detail"]
    )
    assert (await page(batch, valid))[1] == 200
    assert (await batch.store.get(Engagement, "two")).work_enabled
    assert await batch.store.count(Engagement) == 3
    assert await batch.store.count(ScopePolicy) == 0
    async with batch.database.session() as session:
        assert (await session.get(RuntimeCommandRow, barrier_id)).state == "failed"
        assert (
            await session.get(RuntimeWorkBatchProjectRow, (batch_id, project_id))
        ).state == "failed"
        assert (
            await session.get(RuntimeCommandRow, unknown.command_id)
        ).state == "uncertain"
    assert await accepted(batch, "POST", body, key="legacy-invalid") == failed
