"""Public sender isolation, durable incidents, and operator action contracts."""

from __future__ import annotations
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from tests.v3.async_test_support import AsyncTestClient as TestClient
from jsonschema import Draft202012Validator, FormatChecker

from nebula.v3.api import create_app
from nebula.v3.domain import (
    ChatQueue,
    ChatSession,
    Engagement,
    ProviderProfile,
    StorageAction,
    StorageDispatch,
    StorageEventReceipt,
    StorageIncident,
    StorageVolume,
    utc_now,
)
from nebula.v3.storage import NebulaStore

AUTH = {"Authorization": "Bearer operator-token"}


def test_published_event_schema_matches_versioned_sender_contract():
    schema = json.loads(
        (
            Path(__file__).parents[2] / "docs/schemas/storage-event-v1.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    report = event()
    validator.validate(report)
    validator.validate(
        {**report, "state": "unavailable", "free_bytes": None, "total_bytes": None}
    )
    assert list(
        validator.iter_errors({**report, "state": "warning", "free_bytes": None})
    )
    assert list(validator.iter_errors({**report, "schema_version": 2}))


async def test_openapi_exposes_versioned_event_and_inventory_bodies(tmp_path):
    client, _ = await setup(tmp_path)
    paths = (await client.get("/openapi.json")).json()["paths"]
    for path in ("events", "test"):
        body = paths[f"/api/v1/storage/{path}"]["post"]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        assert schema["properties"]["schema_version"]["const"] == 1
        assert {"event_id", "observed_at", "device", "volume", "state"} <= set(
            schema["required"]
        )
    inventory = paths["/api/v1/storage/inventory"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert inventory["properties"]["schema_version"]["const"] == 1
    assert "devices" in inventory["required"]


async def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    (await store.create(Engagement(id="storage-project", name="Storage project")))
    (
        await store.create(
            ProviderProfile(
                id="storage-provider",
                name="Local model",
                provider_type="openai_compatible",
                model_allowlist=["test-model"],
            )
        )
    )
    app = create_app(store, auth_token="operator-token")
    return TestClient(app, base_url="https://testserver"), store


def event(event_id="observation-1", *, state="warning", observed_at=None, free=20):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "observed_at": (observed_at or utc_now()).isoformat(),
        "kind": "snapshot",
        "device": {"id": "nas-1", "label": "NAS One"},
        "volume": {"id": "data", "label": "Data", "path": "/data"},
        "state": state,
        "free_bytes": free,
        "total_bytes": 100,
    }


def enroll(client):
    result = client.post(
        "/api/v1/storage/sources",
        headers=AUTH,
        json={
            "name": "NAS",
            "engagement_id": "storage-project",
            "backend": "provider",
            "profile_id": "storage-provider",
            "model": "test-model",
            "reporting_interval_seconds": 60,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


async def test_enrollment_test_endpoint_and_sender_authorization(tmp_path):
    client, store = await setup(tmp_path)
    created = enroll(client)
    token = created["token"]
    source_id = created["source"]["id"]
    assert token not in str(
        (await store.get_by_kind("storage_sources", source_id)).model_dump()
    )
    report = event()
    headers = {"Authorization": "Bearer " + token}
    assert (
        await client.post("/api/v1/storage/test", headers=headers, json=report)
    ).json()["valid"]
    assert (await store.count(StorageVolume)) == 0
    assert (
        await client.post("/api/v1/storage/events", headers=headers, json=report)
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/storage/events",
            headers={"Authorization": "Bearer wrong"},
            json=report,
        )
    ).status_code == 401
    assert (await client.post("/api/v1/storage/events", json=report)).status_code == 401
    enabled = await client.patch(
        f"/api/v1/storage/sources/{source_id}",
        headers=AUTH,
        json={"expected_revision": created["source"]["revision"], "enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert (
        await client.post(
            "/api/v1/storage/events",
            headers=headers,
            json={**report, "free_bytes": 200},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/storage/events", headers=headers, content=b" " * (64 * 1024 + 1)
        )
    ).status_code == 413
    assert (
        await client.post("/api/v1/storage/events", headers=headers, json=report)
    ).status_code == 202
    assert (await store.count(StorageVolume)) == 1
    rotated = await client.post(
        f"/api/v1/storage/sources/{source_id}/rotate", headers=AUTH
    )
    assert rotated.status_code == 200
    assert (
        await client.post(
            "/api/v1/storage/events", headers=headers, json=event("observation-2")
        )
    ).status_code == 401
    fresh = {"Authorization": "Bearer " + rotated.json()["token"]}
    assert (
        await client.post(
            "/api/v1/storage/events", headers=fresh, json=event("observation-2")
        )
    ).status_code == 202
    assert (
        await client.post(f"/api/v1/storage/sources/{source_id}/revoke", headers=AUTH)
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/storage/events", headers=fresh, json=event("observation-3")
        )
    ).status_code == 401


async def test_source_token_changes_reject_stale_browser_revisions(tmp_path):
    client, _ = await setup(tmp_path)
    created = enroll(client)
    source_id = created["source"]["id"]
    initial_revision = created["source"]["revision"]
    endpoint = f"/api/v1/storage/sources/{source_id}"
    rotated = await client.post(
        f"{endpoint}/rotate",
        headers=AUTH,
        json={"expected_revision": initial_revision},
    )
    assert rotated.status_code == 200, rotated.text
    current_revision = rotated.json()["source"]["revision"]
    assert current_revision > initial_revision
    assert (
        await client.post(
            f"{endpoint}/rotate",
            headers=AUTH,
            json={"expected_revision": initial_revision},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{endpoint}/revoke",
            headers=AUTH,
            json={"expected_revision": initial_revision},
        )
    ).status_code == 409
    fresh = {"Authorization": "Bearer " + rotated.json()["token"]}
    assert (
        await client.post("/api/v1/storage/test", headers=fresh, json=event())
    ).status_code == 200
    revoked = await client.post(
        f"{endpoint}/revoke",
        headers=AUTH,
        json={"expected_revision": current_revision},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["enabled"] is False
    assert (
        await client.post("/api/v1/storage/test", headers=fresh, json=event())
    ).status_code == 401


async def test_callback_auth_is_required_and_source_can_be_disabled_after_runtime_fails(
    tmp_path,
):
    client, store = await setup(tmp_path)
    invalid = await client.post(
        "/api/v1/storage/sources",
        headers=AUTH,
        json={
            "name": "NAS",
            "engagement_id": "storage-project",
            "backend": "provider",
            "profile_id": "storage-provider",
            "model": "test-model",
            "callback_url": "https://storage.example.test",
        },
    )
    assert invalid.status_code == 422
    created = enroll(client)["source"]
    enabled = await client.patch(
        f"/api/v1/storage/sources/{created['id']}",
        headers=AUTH,
        json={"expected_revision": created["revision"], "enabled": True},
    )
    assert enabled.status_code == 200
    profile = await store.get(ProviderProfile, "storage-provider")
    (
        await store.update(
            ProviderProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
    )
    disabled = await client.patch(
        f"/api/v1/storage/sources/{created['id']}",
        headers=AUTH,
        json={"expected_revision": enabled.json()["revision"], "enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


async def test_duplicate_late_worsening_resolution_and_restart(tmp_path):
    client, store = await setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    (
        await client.patch(
            f"/api/v1/storage/sources/{source['id']}",
            headers=AUTH,
            json={"expected_revision": source["revision"], "enabled": True},
        )
    )
    sender = {"Authorization": "Bearer " + created["token"]}
    first_time = utc_now()
    first = event(observed_at=first_time)
    accepted = await client.post("/api/v1/storage/events", headers=sender, json=first)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["current"] is True
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=first)
    ).json()["duplicate"] is True
    assert (
        await client.post(
            "/api/v1/storage/events", headers=sender, json={**first, "free_bytes": 1}
        )
    ).status_code == 409
    late = event(
        "late", state="healthy", observed_at=first_time - timedelta(minutes=1), free=90
    )
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=late)
    ).json()["current"] is False
    assert (await store.count(StorageIncident)) == 1
    assert (await store.count(StorageEventReceipt)) == 2
    incident = (await store.list_entities(StorageIncident))[0]
    assert (await store.list_entities(StorageVolume))[0].state == "warning"
    assert (await store.count(ChatSession)) == 1
    assert (await store.count(StorageDispatch)) == 1
    worse = event(
        "worse", state="critical", observed_at=first_time + timedelta(seconds=1), free=5
    )
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=worse)
    ).status_code == 202
    assert (await store.count(StorageIncident)) == 1
    assert (await store.count(ChatSession)) == 1
    assert (await store.count(StorageDispatch)) == 2
    healthy = event(
        "healthy",
        state="healthy",
        observed_at=first_time + timedelta(seconds=2),
        free=80,
    )
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=healthy)
    ).status_code == 202
    assert (await store.get(StorageIncident, incident.id)).state == "resolved"
    # A fresh Core sees durable state, including the old linked conversation.
    reloaded = create_app(database=tmp_path / "core.db", auth_token="operator-token")
    async with TestClient(reloaded, base_url="https://testserver") as restarted:
        dashboard = (await restarted.get("/api/v1/storage", headers=AUTH)).json()
        assert len(dashboard["incidents"]) == 1
        assert dashboard["incidents"][0]["chat_session_id"] == incident.chat_session_id
    assert (await store.count(ChatSession)) == 1


async def test_callback_offer_preview_approval_and_uncertain_lookup(tmp_path):
    client, store = await setup(tmp_path)
    service = client.app.state.storage_integrations
    created = enroll(client)
    source = created["source"]
    source_entity = await store.get_by_kind("storage_sources", source["id"])
    source_entity = await store.update(
        type(source_entity),
        source["id"],
        {"enabled": True, "callback_url": "http://127.0.0.1:8766"},
        expected_revision=source_entity.revision,
    )
    sender = {"Authorization": "Bearer " + created["token"]}
    calls = []
    preview_mode = {"value": "drift"}

    async def callback(_source, path, body):
        calls.append((path, body))
        if path == "/v1/diagnose":
            return {
                "diagnostics": "read-only check",
                "offers": [{"id": "copy", "title": "Copy artifact"}],
            }
        if path.endswith("/preview"):
            from nebula.v3.storage_integrations import _digest

            effects = [
                {"operation": "copy", "source": "/fixture", "disposition": "retained"}
            ]
            return {
                "effects": effects,
                "digest": "0" * 64
                if preview_mode["value"] == "drift"
                else _digest(effects),
                "expires_at": (
                    utc_now()
                    + timedelta(minutes=11 if preview_mode["value"] == "long" else 5)
                ).isoformat(),
            }
        if path.endswith("/execute"):
            raise httpx.TimeoutException("lost response")
        return {"status": "complete", "receipt": {"copied": True}}

    import httpx

    service._callback = callback
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=event())
    ).status_code == 202
    action = (await store.list_entities(StorageAction))[0]
    assert (
        await client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH)
    ).status_code == 502
    preview_mode["value"] = "long"
    assert (
        await client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH)
    ).status_code == 502
    preview_mode["value"] = "valid"
    preview = await client.post(
        f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH
    )
    assert preview.status_code == 200, preview.text
    digest = preview.json()["digest"]
    assert (
        await client.post(
            f"/api/v1/storage/actions/{action.id}/approve",
            headers=AUTH,
            json={"digest": "0" * 64},
        )
    ).status_code == 409
    approval = await client.post(
        f"/api/v1/storage/actions/{action.id}/approve",
        headers=AUTH,
        json={"digest": digest},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "complete"
    assert approval.json()["receipt"] == {"copied": True}
    assert sum(path.endswith("/execute") for path, _ in calls) == 1
    assert sum(path.endswith("/status") for path, _ in calls) == 1
    current_source = await store.get_by_kind("storage_sources", source["id"])
    (
        await store.update(
            type(current_source),
            source["id"],
            {"enabled": False},
            expected_revision=current_source.revision,
        )
    )
    assert (
        await client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH)
    ).status_code == 409


async def test_source_preview_refusal_is_actionable_conflict(tmp_path, monkeypatch):
    class Callback(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer callback-token"
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/diagnose":
                code = 200
                body = {
                    "diagnostics": "read-only check",
                    "offers": [
                        {
                            "id": "archive-copy:controller:fixture",
                            "title": "Archive copy · /fixture",
                        }
                    ],
                }
            else:
                assert self.path.endswith("/preview")
                code = 409
                body = {"error": "unsupported tree entry: /fixture/backup-link"}
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("NEBULA_TEST_STORAGE_CALLBACK_TOKEN", "callback-token")
        client, store = await setup(tmp_path)
        created = enroll(client)
        source = await store.get_by_kind("storage_sources", created["source"]["id"])
        (
            await store.update(
                type(source),
                source.id,
                {
                    "enabled": True,
                    "callback_url": f"http://127.0.0.1:{server.server_port}",
                    "callback_credential_ref": "env:NEBULA_TEST_STORAGE_CALLBACK_TOKEN",
                },
                expected_revision=source.revision,
            )
        )
        sender = {"Authorization": "Bearer " + created["token"]}
        assert (
            await client.post("/api/v1/storage/events", headers=sender, json=event())
        ).status_code == 202
        action = (await store.list_entities(StorageAction))[0]
        preview = await client.post(
            f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH
        )
        assert preview.status_code == 409, preview.text
        assert preview.json()["detail"] == (
            "Storage source refused preview: unsupported tree entry: /fixture/backup-link"
        )
        saved = await store.get(StorageAction, action.id)
        assert saved.status == "offered" and saved.digest is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_uncertain_action_remains_visible_when_status_callback_is_down(tmp_path):
    import httpx
    from nebula.v3.storage_integrations import _digest

    client, store = await setup(tmp_path)
    created = enroll(client)
    source = await store.get_by_kind("storage_sources", created["source"]["id"])
    (
        await store.update(
            type(source),
            source.id,
            {"enabled": True, "callback_url": "http://127.0.0.1:8766"},
            expected_revision=source.revision,
        )
    )
    calls = []
    effects = [{"operation": "copy", "source": "/fixture", "disposition": "retained"}]

    async def unavailable(_source, path, _body):
        calls.append(path)
        if path == "/v1/diagnose":
            return {
                "diagnostics": "read-only check",
                "offers": [{"id": "copy", "title": "Copy artifact"}],
            }
        if path.endswith("/preview"):
            return {
                "effects": effects,
                "digest": _digest(effects),
                "expires_at": (utc_now() + timedelta(minutes=5)).isoformat(),
            }
        raise httpx.TimeoutException("source reply lost")

    client.app.state.storage_integrations._callback = unavailable
    sender = {"Authorization": "Bearer " + created["token"]}
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=event())
    ).status_code == 202
    action = (await store.list_entities(StorageAction))[0]
    digest = (
        await client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH)
    ).json()["digest"]
    approval = await client.post(
        f"/api/v1/storage/actions/{action.id}/approve",
        headers=AUTH,
        json={"digest": digest},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "uncertain"
    assert "status lookup failed" in approval.json()["error"]
    assert sum(path.endswith("/execute") for path in calls) == 1
    assert sum(path.endswith("/status") for path in calls) == 1

    async def recovered(_source, path, _body):
        assert path.endswith("/status")
        return {"status": "complete", "receipt": {"copied": True}}

    client.app.state.storage_integrations._callback = recovered
    reconciled = await client.post(
        f"/api/v1/storage/actions/{action.id}/reconcile", headers=AUTH
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "complete"
    assert reconciled.json()["receipt"] == {"copied": True}
    assert sum(path.endswith("/execute") for path in calls) == 1


async def test_inventory_staleness_and_callback_failure_keep_alert_visible(tmp_path):
    import httpx

    client, store = await setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    source_entity = await store.get_by_kind("storage_sources", source["id"])
    (
        await store.update(
            type(source_entity),
            source["id"],
            {"enabled": True, "callback_url": "http://127.0.0.1:8766"},
            expected_revision=source_entity.revision,
        )
    )
    sender = {"Authorization": "Bearer " + created["token"]}
    inventory = await client.post(
        "/api/v1/storage/inventory",
        headers=sender,
        json={"schema_version": 1, "devices": [{"id": "nas-1", "label": "NAS One"}]},
    )
    assert inventory.status_code == 202, inventory.text
    assert (await store.count(StorageVolume)) == 0

    async def unavailable(_source, _path, _body):
        raise httpx.ConnectError("callback offline")

    client.app.state.storage_integrations._callback = unavailable
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=event())
    ).status_code == 202
    dashboard = (await client.get("/api/v1/storage", headers=AUTH)).json()
    assert dashboard["incidents"][0]["callback_error"] == "callback offline"
    assert dashboard["incidents"][0]["dispatch_status"] == "queued"
    assert dashboard["actions"] == []
    assert len(dashboard["devices"]) == 1
    assert dashboard["volumes"][0]["state"] == "warning"
    chat_id = dashboard["incidents"][0]["chat_session_id"]
    queue = await store.get(ChatQueue, "chat-queue-" + chat_id)
    prompt = queue.items[0]["request"]["messages"][0]["content"]
    assert "Read-only diagnostics are unavailable" in prompt
    assert "callback offline" not in prompt
    volume = (await store.list_entities(StorageVolume))[0]
    (
        await store.update(
            StorageVolume,
            volume.id,
            {"received_at": utc_now() - timedelta(seconds=61)},
            expected_revision=volume.revision,
        )
    )
    stale = (await client.get("/api/v1/storage", headers=AUTH)).json()["volumes"][0]
    assert stale["stale"] is True
    assert stale["state"] == "warning"


async def test_configured_runtime_failure_stays_retryable(tmp_path):
    client, store = await setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    (
        await client.patch(
            f"/api/v1/storage/sources/{source['id']}",
            headers=AUTH,
            json={"expected_revision": source["revision"], "enabled": True},
        )
    )
    profile = await store.get(ProviderProfile, "storage-provider")
    (
        await store.update(
            ProviderProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
    )
    sender = {"Authorization": "Bearer " + created["token"]}
    assert (
        await client.post("/api/v1/storage/events", headers=sender, json=event())
    ).status_code == 202
    dashboard = (await client.get("/api/v1/storage", headers=AUTH)).json()
    assert dashboard["incidents"][0]["dispatch_status"] == "failed"
    assert dashboard["dispatches"][0]["status"] == "failed"
    assert (await store.count(ChatSession)) == 0
    disabled = await store.get(ProviderProfile, "storage-provider")
    (
        await store.update(
            ProviderProfile,
            disabled.id,
            {"enabled": True},
            expected_revision=disabled.revision,
        )
    )
    dispatch_id = dashboard["dispatches"][0]["id"]
    retried = await client.post(
        f"/api/v1/storage/dispatches/{dispatch_id}/retry", headers=AUTH
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert (await store.count(ChatSession)) == 1
    chat = (await store.list_entities(ChatSession))[0]
    queue = await store.get(ChatQueue, "chat-queue-" + chat.id)
    items = [dict(item) for item in queue.items]
    items[0].update(status="needs_review", detail="configured runtime failed")
    (
        await store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
    )
    surfaced = (await client.get("/api/v1/storage", headers=AUTH)).json()["dispatches"][
        0
    ]
    assert surfaced["queue_status"] == "needs_review"
    assert surfaced["queue_detail"] == "configured runtime failed"


pytestmark = pytest.mark.anyio
