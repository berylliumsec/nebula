"""Global read parity through the authenticated public API, without an owner."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import Database, ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    ChatQueue,
    ChatSession,
    Engagement,
    GuideProgress,
    StorageAction,
    StorageDevice,
    StorageDispatch,
    StorageIncident,
    StorageSource,
    StorageVolume,
    utc_now,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.runtime_global_reads import mount_global_reads
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
TOKEN = "isolated-global-read-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def reads(location, tmp_path, monkeypatch):
    database = Database(location, pool_settings=PoolSettings(5, 12, 5, 2))
    await database.initialize()
    database._bootstrap = False
    store = NebulaStore(database)
    coordination = RuntimeCoordination(database)
    routing = RuntimeRouting(
        coordination, tmp_path / "credentials", request_admission=True
    )
    delivery = RuntimeDelivery(coordination, RuntimeVault(tmp_path / "payloads"))
    global_api = RuntimeGlobalApi(database)
    mount_global_reads(global_api.app, global_api.store, global_api.actor)

    async def forbidden_owner(_scope):
        pytest.fail("A contextless read attempted to contact an execution owner")

    async def forbidden_connection():
        pytest.fail("A global guide mutation opened an independent SQL connection")

    monkeypatch.setattr(database, "connect", forbidden_connection)
    app = create_api_worker(
        store,
        delivery,
        operator_token=TOKEN,
        project_for=routing.project_for,
        upstream_for=forbidden_owner,
        global_api=global_api,
        command_start_timeout=0.03,
        poll_interval=0.01,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
    )
    try:
        yield SimpleNamespace(
            client=client,
            store=store,
            database=database,
            app=app,
            global_api=global_api,
        )
    finally:
        await client.aclose()
        await app.state.viewer_proxy.close()
        await global_api.close()
        await store.event_history.close()
        await database.dispose()


async def test_contextless_reads_require_auth_and_leave_no_execution_or_fake_project(
    reads,
):
    for path in ("/api/v1/guides/progress", "/api/v1/storage"):
        assert (await reads.client.get(path)).status_code == 401
        response = await reads.client.get(path, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
    assert (
        await reads.client.get("/api/v1/guides/progress", headers=AUTH)
    ).json() == []
    assert (await reads.client.get("/api/v1/storage", headers=AUTH)).json() == {
        "sources": [],
        "devices": [],
        "volumes": [],
        "incidents": [],
        "actions": [],
        "dispatches": [],
    }
    assert await reads.store.count(Engagement) == 0
    async with reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
    for path, method in [
        ("/api/v1/project-instructions", "GET"),
        ("/api/v1/guides/starter-files", "POST"),
        ("/api/v1/ssh-environments", "GET"),
        ("/api/v1/desktop-browser-mode", "GET"),
    ]:
        assert not reads.global_api.supports(
            {"type": "http", "path": path, "method": method}
        )


async def test_guide_lifecycle_uses_one_atomic_receipt_and_preserves_completion_time(
    reads,
):
    path = "/api/v1/guides/progress/shortcuts"
    headers = {**AUTH, "Idempotency-Key": "start-guide"}
    body = {
        "status": "in_progress",
        "step_index": 1,
        "expected_revision": 0,
        "expected_operator_key": "local",
    }
    first = await reads.client.put(path, headers=headers, json=body)
    replay = await reads.client.put(path, headers=headers, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["x-nebula-command-id"] == replay.headers["x-nebula-command-id"]
    assert await reads.store.count(GuideProgress) == 1
    completed = await reads.client.put(
        path,
        headers={**AUTH, "Idempotency-Key": "complete"},
        json={"status": "completed", "step_index": 3, "expected_revision": 1},
    )
    replayed_guide = await reads.client.put(
        path,
        headers={**AUTH, "Idempotency-Key": "replay"},
        json={"status": "completed", "step_index": 3, "expected_revision": 2},
    )
    assert replayed_guide.json()["completed_at"] == completed.json()["completed_at"]
    stale = await reads.client.put(
        path, headers={**AUTH, "Idempotency-Key": "stale"}, json=body
    )
    assert stale.status_code == 409
    assert (
        await reads.client.delete(path, headers={**AUTH, "Idempotency-Key": "reset"})
    ).status_code == 204
    assert (
        await reads.client.get("/api/v1/guides/progress", headers=AUTH)
    ).json() == []


async def test_paired_device_progress_follows_active_operator_and_revocation(reads):
    client = reads.client
    first = (
        await client.post(
            "/api/v1/operator-profiles",
            headers={**AUTH, "Idempotency-Key": "operator-a"},
            json={"display_name": "First"},
        )
    ).json()
    body = {
        "status": "in_progress",
        "step_index": 1,
        "expected_revision": 0,
        "expected_operator_key": first["id"],
    }
    path = "/api/v1/guides/progress/agents-md"
    assert (
        await client.put(
            path, headers={**AUTH, "Idempotency-Key": "first-guide"}, json=body
        )
    ).status_code == 200
    offer = (
        await client.post(
            "/api/v1/auth/pairings", headers=AUTH, json={"name": "Read fixture"}
        )
    ).json()
    phone = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reads.app), base_url="https://synthetic.test"
    )
    try:
        paired = await phone.post(
            "/api/v1/auth/pairings/redeem",
            json={
                "secret": offer["secret"],
                "confirmation_code": offer["confirmation_code"],
                "name": "Phone",
            },
        )
        assert paired.status_code == 200, paired.text
        assert (await phone.get("/api/v1/guides/progress")).json()[0][
            "operator_key"
        ] == first["id"]
        body.update(expected_revision=1, step_index=2)
        assert (
            await phone.put(
                path, headers={"Idempotency-Key": "without-csrf"}, json=body
            )
        ).status_code == 403
        headers = {
            "X-Nebula-CSRF": paired.json()["csrf_token"],
            "Origin": "https://synthetic.test",
            "Idempotency-Key": "phone-progress",
        }
        saved = await phone.put(path, headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        async with reads.database.session() as session:
            command = await session.get(
                RuntimeCommandRow, saved.headers["x-nebula-command-id"]
            )
            assert command.actor_id == "device:" + paired.json()["device"]["id"]
        second = (
            await client.post(
                "/api/v1/operator-profiles",
                headers={**AUTH, "Idempotency-Key": "operator-b"},
                json={"display_name": "Second"},
            )
        ).json()
        await client.post(
            f"/api/v1/operator-profiles/{second['id']}/activate",
            headers={**AUTH, "Idempotency-Key": "activate-b"},
            json={"expected_revision": 1},
        )
        body["expected_revision"] = 2
        changed = await phone.put(
            path, headers={**headers, "Idempotency-Key": "old-operator"}, json=body
        )
        assert changed.status_code == 409 and "operator changed" in changed.text
        assert (await phone.get("/api/v1/guides/progress")).json() == []
        revoked = await client.delete(
            "/api/v1/auth/devices/" + paired.json()["device"]["id"], headers=AUTH
        )
        assert revoked.status_code == 204
        assert (await phone.get("/api/v1/guides/progress")).status_code in {401, 403}
    finally:
        await phone.aclose()


async def test_interruption_before_receipt_rolls_back_guide_progress(
    reads, monkeypatch
):
    original = GlobalSqlDelivery._response

    async def interrupted(self, intent, actor):
        result = await original(self, intent, actor)
        assert result[0] == 200
        raise RuntimeError("Interrupted before receipt")

    monkeypatch.setattr(GlobalSqlDelivery, "_response", interrupted)
    with pytest.raises(RuntimeError, match="before receipt"):
        await reads.client.put(
            "/api/v1/guides/progress/shortcuts",
            headers={**AUTH, "Idempotency-Key": "interrupted"},
            json={"status": "in_progress", "expected_revision": 0},
        )
    assert await reads.store.count(GuideProgress) == 0
    async with reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )


async def seed_reports(reads, *, project="project"):
    now = utc_now()
    source = await reads.store.create(
        StorageSource(
            id="source",
            name="Synthetic storage",
            engagement_id=project,
            profile_id="inert",
            model="synthetic",
            token_sha256="a" * 64,
            reporting_interval_seconds=60,
        )
    )
    await reads.store.create(
        StorageDevice(id="device", source_id=source.id, device_id="disk", label="Disk")
    )
    await reads.store.create(
        StorageVolume(
            id="volume",
            source_id=source.id,
            device_id="disk",
            volume_id="data",
            label="Data",
            state="warning",
            observed_at=now,
            received_at=now - timedelta(seconds=90),
        )
    )
    chat = await reads.store.create(
        ChatSession(
            id="chat",
            title="Synthetic storage chat",
            engagement_id=project,
            provider_profile_id="inert",
            model="synthetic",
        )
    )
    incident = await reads.store.create(
        StorageIncident(
            id="incident",
            source_id=source.id,
            volume_key="volume",
            state="warning",
            opened_at=now,
            last_observed_at=now,
            chat_session_id=chat.id,
        )
    )
    dispatch = await reads.store.create(
        StorageDispatch(
            id="dispatch",
            source_id=source.id,
            incident_id=incident.id,
            event_id="event",
            prompt="PRIVATE DISPATCH PROMPT",
            status="queued",
        )
    )
    await reads.store.create(
        ChatQueue(
            id="chat-queue-chat",
            engagement_id=project,
            session_id=chat.id,
            items=[
                {
                    "key": dispatch.id,
                    "status": "needs_review",
                    "detail": "Review saved work",
                    "request": {"secret": "DO NOT EXPOSE"},
                }
            ],
        )
    )
    await reads.store.create(
        StorageAction(
            id="action",
            source_id=source.id,
            incident_id=incident.id,
            offer_id="offer",
            title="Review action",
        )
    )
    return source, chat


async def test_storage_snapshot_reports_saved_freshness_and_queue_state_without_secrets(
    reads,
):
    await seed_reports(reads)
    response = await reads.client.get("/api/v1/storage", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["volumes"][0]["stale"] and data["volumes"][0]["state"] == "warning"
    assert data["dispatches"][0]["queue_status"] == "needs_review"
    assert data["dispatches"][0]["queue_detail"] == "Review saved work"
    assert (
        "token_sha256" not in response.text
        and "PRIVATE DISPATCH PROMPT" not in response.text
    )
    assert "DO NOT EXPOSE" not in response.text
    assert data["actions"][0]["title"] == "Review action"


async def test_storage_paging_keeps_source_freshness_outside_the_current_page(reads):
    source, _ = await seed_reports(reads)
    now = utc_now()
    for index in range(1, 3):
        await reads.store.create(
            StorageVolume(
                id=f"volume-{index}",
                source_id=source.id,
                device_id="disk",
                volume_id=str(index),
                label=str(index),
                state="healthy",
                observed_at=now,
                received_at=now,
            )
        )
    first = await reads.client.get("/api/v1/storage?limit=1", headers=AUTH)
    assert first.status_code == 200 and first.json()["partial"]
    assert first.headers["x-nebula-next-offset"] == "1"
    second = await reads.client.get("/api/v1/storage?limit=1&offset=1", headers=AUTH)
    assert second.status_code == 200 and second.json()["sources"] == []
    assert not second.json()["volumes"][0]["stale"]
    final = (
        await reads.client.get("/api/v1/storage?limit=1&offset=2", headers=AUTH)
    ).json()
    assert not final["partial"] and final["next_offset"] is None
    assert (
        len(
            {
                first.json()["volumes"][0]["id"],
                second.json()["volumes"][0]["id"],
                final["volumes"][0]["id"],
            }
        )
        == 3
    )


async def test_storage_queue_observation_never_crosses_its_project(reads):
    _, chat = await seed_reports(reads)
    await reads.store.update(ChatSession, chat.id, {"engagement_id": "other-project"})
    data = (await reads.client.get("/api/v1/storage", headers=AUTH)).json()
    assert data["dispatches"][0]["queue_status"] == "unavailable"
    assert "another project" in data["dispatches"][0]["queue_detail"]
    assert "Review saved work" not in str(data)


async def test_read_bounds_and_guide_pagination_are_explicit(reads, monkeypatch):
    for identity in ("guide-a", "guide-b"):
        await reads.client.put(
            "/api/v1/guides/progress/" + identity,
            headers={**AUTH, "Idempotency-Key": identity},
            json={"status": "completed", "expected_revision": 0},
        )
    page = await reads.client.get("/api/v1/guides/progress?limit=1", headers=AUTH)
    assert len(page.json()) == 1 and page.headers["x-nebula-next-offset"] == "1"
    assert (
        len(
            (
                await reads.client.get(
                    "/api/v1/guides/progress?limit=1&offset=1", headers=AUTH
                )
            ).json()
        )
        == 1
    )
    assert (
        await reads.client.get("/api/v1/storage?limit=1001", headers=AUTH)
    ).status_code == 422
    await seed_reports(reads)
    monkeypatch.setattr("nebula.v3.runtime_global_reads.MAX_DASHBOARD_BYTES", 32)
    oversized = await reads.client.get("/api/v1/storage", headers=AUTH)
    assert oversized.status_code == 413 and "smaller catalog page" in oversized.text
