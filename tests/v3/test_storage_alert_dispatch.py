"""A recorded storage alert keeps (or brings back) its project's worker until it
opens its incident chat, and a deleted project leaves no alert behind."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import update

from nebula.v3 import runtime_auto_admission
from nebula.v3.database import DeploymentHostRow, ProjectOwnershipRow
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import (
    Engagement,
    StorageDevice,
    StorageDispatch,
    StorageIncident,
    StorageSource,
    StorageVolume,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    STORAGE_REPORT_COMMAND,
)
from nebula.v3.runtime_project_handoff import _HANDLES
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.storage import NotFoundError, ProjectWorkerClosing
from nebula.v3.storage_integrations import StorageIntegrationService, storage_router
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
FEATURES = ["project.quiesce/v1", "project.handoff/v1", STORAGE_REPORT_COMMAND]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _source(store, project):
    return await store.create(
        StorageSource(
            name="Synthetic NAS",
            engagement_id=project,
            profile_id="provider",
            model="model",
            enabled=True,
            token_sha256=hashlib.sha256(b"sender-secret").hexdigest(),
        )
    )


async def _alert(store, source, status="pending"):
    incident = await store.create(
        StorageIncident(
            source_id=source.id,
            volume_key="volume",
            state="critical",
            opened_at=utc_now(),
            last_observed_at=utc_now(),
            chat_session_id="incident-chat-" + source.id,
        )
    )
    return await store.create(
        StorageDispatch(
            incident_id=incident.id,
            source_id=source.id,
            event_id="observation-1",
            prompt="A storage incident opened.",
            status=status,
        )
    )


@pytest.fixture
async def alerts(host, monkeypatch):
    coordinator, store = host
    await coordinator.stage(
        manifest("alerts", features=FEATURES), Path("/isolated/alerts"), "e" * 64
    )
    await coordinator.coordination.register_runtime("api", "alerts", role="api")
    await coordinator.target_execution("alerts")
    async with coordinator.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "api"
        await session.commit()
    value = SimpleNamespace(
        coordinator=coordinator, store=store, attached=[], retired=[], recoveries=[]
    )

    async def prepare(_version):
        return SimpleNamespace(runtime_id=await worker(host, "alerts"))

    async def attach(project_id, runtime_id):
        value.attached.append(project_id)
        return await coordinator.coordination.claim_project(project_id, runtime_id)

    async def retire_idle_project(project_id):
        value.retired.append(project_id)
        return False

    async def rediscover():
        return value.gone

    value.gone = []
    monkeypatch.setattr(
        runtime_auto_admission, "record_caught_exception", lambda *a, **k: None
    )
    value.automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=coordinator,
            store=store,
            prepare_execution=prepare,
            attach_execution=attach,
            host=SimpleNamespace(
                retire_idle_project=retire_idle_project, rediscover=rediscover
            ),
            management=object(),
        ),
        default_version="alerts",
    )

    async def recovery(owner, action, parameters):
        value.recoveries.append((owner["project_id"], action))
        return {"runtime_id": "recovery-target"}

    monkeypatch.setattr(value.automatic, "_recovery_operation", recovery)
    return value


async def test_a_pending_alert_brings_back_and_keeps_its_projects_worker(alerts):
    coordinator, store = alerts.coordinator, alerts.store
    source = await _source(store, "one")
    dispatch = await _alert(store, source)
    # Another project's alert never keeps or starts this project's worker.
    await _alert(store, await _source(store, "two"), status="queued")
    # The report that recorded the alert gave the project its fencing row.
    async with coordinator.coordination._write() as session:
        await coordinator.coordination._ensure_project(session, "one")
    # No open chat and no queued command: only the recorded alert demands one.
    await alerts.automatic.reconcile()
    assert alerts.attached == ["one"]
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
    lease = SimpleNamespace(
        project_id="one", runtime_id=owner.runtime_id, fencing_token=owner.fencing_token
    )
    # While it is pending the owner is not idle and refuses retirement.
    await alerts.automatic.reconcile()
    assert alerts.retired == []
    assert await coordinator.request_project_retirement(lease) is False
    # Once the incident chat is queued, the worker may retire as usual.
    current = await store.get(StorageDispatch, dispatch.id)
    await store.update(
        StorageDispatch,
        dispatch.id,
        {"status": "queued"},
        expected_revision=current.revision,
    )
    await alerts.automatic.reconcile()
    assert alerts.retired == ["one"]
    assert await coordinator.request_project_retirement(lease) is True
    # An alert recorded after the hold (it cannot, but a worker could race)
    # still stops the quiescence and the release.
    await _alert(store, source)
    with pytest.raises(CoordinationConflict, match="storage alert"):
        await coordinator.quiesce_checkpoint(lease, local_blockers=[])
    with pytest.raises(CoordinationConflict, match="acquired work"):
        await coordinator.release_quiesced_project(
            lease, checkpoint={"project_id": "one", "paused_sha256": "x"}
        )


async def test_an_expired_owner_with_storage_work_is_recovered(alerts):
    coordinator, store = alerts.coordinator, alerts.store
    source = await _source(store, "one")
    for project in ("one", "two"):
        runtime = await alerts.automatic.supervisor.prepare_execution("alerts")
        await coordinator.coordination.claim_project(project, runtime.runtime_id)
        alerts.gone.append(
            SimpleNamespace(
                runtime_id=runtime.runtime_id,
                process_state="gone",
                identity=SimpleNamespace(version="alerts"),
            )
        )
    await _alert(store, source)
    await coordinator.coordination.submit(
        project_id="two",
        actor_id="storage-source:other:" + "a" * 64,
        idempotency_key="storage-event-two",
        name=STORAGE_REPORT_COMMAND,
        payload={"format": 1, "request_reference": "b" * 64},
        lane="report",
    )
    async with coordinator.database.session() as session:
        await session.execute(update(ProjectOwnershipRow).values(lease_expires_at_ms=0))
        await session.commit()
    # Neither project has an open chat; their storage work alone needs them.
    await alerts.automatic._recover_expired()
    assert sorted(project for project, action in alerts.recoveries) == [
        "one",
        "one",
        "one",
        "two",
        "two",
        "two",
    ]


async def test_a_retiring_worker_leaves_the_alert_for_its_next_worker(host):
    _, store = host
    source = await _source(store, "one")
    dispatch = await _alert(store, source)
    service = StorageIntegrationService(store, None, None, None)
    assert _HANDLES["storage_integrations"] == ("_dispatching",)
    started, release = asyncio.Event(), asyncio.Event()

    async def closing(_dispatch):
        started.set()
        await release.wait()
        raise ProjectWorkerClosing(
            "The project worker is closing; retry chat creation after retirement."
        )

    service._dispatch_now = closing
    run = asyncio.create_task(service.dispatch_pending())
    await started.wait()
    # A dispatch in progress is a resident handle of this worker.
    assert set(service._dispatching) == {dispatch.id}
    release.set()
    await run
    assert service._dispatching == {}
    kept = await store.get(StorageDispatch, dispatch.id)
    assert (kept.status, kept.error) == ("pending", None)


async def test_a_worker_deleting_its_project_removes_its_storage_rows(execution):
    control, runtime, coordinator, _ = execution
    archived = await control.create(
        Engagement(id="archived", name="archived", status="archived")
    )
    gone = await _source(control, "archived")
    kept = await _source(control, "other")
    rows = []
    for source in (gone, kept):
        dispatch = await _alert(control, source)
        await control.create(
            StorageDevice(source_id=source.id, device_id="nas", label="NAS")
        )
        await control.create(
            StorageVolume(
                source_id=source.id,
                device_id="nas",
                volume_id="data",
                label="Data",
                state="critical",
                free_bytes=1,
                total_bytes=100,
                observed_at=utc_now(),
                received_at=utc_now(),
            )
        )
        rows.append(dispatch)
    lease = await coordinator.claim_project("archived", "execution")
    with execution_context(lease):
        await runtime.delete_archived_engagement(
            "archived", expected_revision=archived.revision
        )
    for model in (StorageDispatch, StorageIncident, StorageDevice, StorageVolume):
        remaining = await control.list_entities(model)
        assert {item.source_id for item in remaining} == {kept.id}
    with pytest.raises(NotFoundError):
        await control.get(StorageDispatch, rows[0].id)


async def test_retry_refuses_an_alert_whose_source_is_gone(host):
    _, store = host
    source = await _source(store, "one")
    live = await _alert(store, source, status="failed")
    orphan = await store.create(
        StorageDispatch(
            incident_id="missing-incident",
            source_id="deleted-source",
            event_id="observation-1",
            prompt="A storage incident opened.",
        )
    )
    service = StorageIntegrationService(store, None, None, None)

    async def nothing():
        return None

    service.dispatch_pending = nothing
    app = FastAPI()
    app.include_router(storage_router(service, lambda: "operator"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://core.test"
    ) as client:
        refused = await client.post(f"/api/v1/storage/dispatches/{orphan.id}/retry")
        assert refused.status_code == 409, refused.text
        settled = await store.get(StorageDispatch, orphan.id)
        assert settled.status == "failed" and "no longer exists" in settled.error
        retried = await client.post(f"/api/v1/storage/dispatches/{live.id}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "pending"


async def test_project_less_storage_rows_route_to_their_sources_project(host, tmp_path):
    coordinator, store = host
    source = await _source(store, "two")
    dispatch = await _alert(store, source)
    orphan = await store.create(
        StorageDispatch(
            incident_id="missing-incident",
            source_id="deleted-source",
            event_id="observation-1",
            prompt="A storage incident opened.",
        )
    )
    routing = RuntimeRouting(coordinator.coordination, tmp_path / "credentials")
    for identifier, project in (
        (dispatch.id, "two"),
        (dispatch.incident_id, "two"),
        (orphan.id, GLOBAL_EXECUTION_PROJECT),
    ):
        path = f"/api/v1/storage/dispatches/{identifier}/retry"
        assert await routing._project(path, b"") == project
