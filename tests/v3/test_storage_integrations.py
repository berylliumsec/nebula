"""Public sender isolation, durable incidents, and operator action contracts."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from nebula.v3.api import create_app
from nebula.v3.domain import (
    ChatQueue, ChatSession, Engagement, ProviderProfile, StorageAction, StorageDispatch,
    StorageEventReceipt, StorageIncident, StorageVolume, utc_now,
)
from nebula.v3.storage import NebulaStore

AUTH = {"Authorization": "Bearer operator-token"}


def test_published_event_schema_matches_versioned_sender_contract():
    schema = json.loads((Path(__file__).parents[2] / "docs/schemas/storage-event-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    report = event()
    validator.validate(report)
    validator.validate({**report, "state": "unavailable", "free_bytes": None, "total_bytes": None})
    assert list(validator.iter_errors({**report, "state": "warning", "free_bytes": None}))
    assert list(validator.iter_errors({**report, "schema_version": 2}))


def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    store.create(Engagement(id="storage-project", name="Storage project"))
    store.create(ProviderProfile(id="storage-provider", name="Local model", provider_type="openai_compatible", model_allowlist=["test-model"]))
    app = create_app(store, auth_token="operator-token")
    return TestClient(app, base_url="https://testserver"), store


def event(event_id="observation-1", *, state="warning", observed_at=None, free=20):
    return {"schema_version": 1, "event_id": event_id,
            "observed_at": (observed_at or utc_now()).isoformat(), "kind": "snapshot",
            "device": {"id": "nas-1", "label": "NAS One"},
            "volume": {"id": "data", "label": "Data", "path": "/data"},
            "state": state, "free_bytes": free, "total_bytes": 100}


def enroll(client):
    result = client.post("/api/v1/storage/sources", headers=AUTH, json={
        "name": "NAS", "engagement_id": "storage-project", "backend": "provider",
        "profile_id": "storage-provider", "model": "test-model",
        "reporting_interval_seconds": 60,
    })
    assert result.status_code == 201, result.text
    return result.json()


def test_enrollment_test_endpoint_and_sender_authorization(tmp_path):
    client, store = setup(tmp_path)
    created = enroll(client)
    token = created["token"]
    source_id = created["source"]["id"]
    assert token not in str(store.get_by_kind("storage_sources", source_id).model_dump())
    report = event()
    headers = {"Authorization": "Bearer " + token}
    assert client.post("/api/v1/storage/test", headers=headers, json=report).json()["valid"]
    assert store.count(StorageVolume) == 0
    assert client.post("/api/v1/storage/events", headers=headers, json=report).status_code == 403
    assert client.post("/api/v1/storage/events", headers={"Authorization": "Bearer wrong"}, json=report).status_code == 401
    assert client.post("/api/v1/storage/events", json=report).status_code == 401
    enabled = client.patch(f"/api/v1/storage/sources/{source_id}", headers=AUTH,
                           json={"expected_revision": created["source"]["revision"], "enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert client.post("/api/v1/storage/events", headers=headers, json={**report, "free_bytes": 200}).status_code == 422
    assert client.post("/api/v1/storage/events", headers=headers, json=report).status_code == 202
    assert store.count(StorageVolume) == 1
    rotated = client.post(f"/api/v1/storage/sources/{source_id}/rotate", headers=AUTH)
    assert rotated.status_code == 200
    assert client.post("/api/v1/storage/events", headers=headers, json=event("observation-2")).status_code == 401
    fresh = {"Authorization": "Bearer " + rotated.json()["token"]}
    assert client.post("/api/v1/storage/events", headers=fresh, json=event("observation-2")).status_code == 202
    assert client.post(f"/api/v1/storage/sources/{source_id}/revoke", headers=AUTH).status_code == 200
    assert client.post("/api/v1/storage/events", headers=fresh, json=event("observation-3")).status_code == 401


def test_duplicate_late_worsening_resolution_and_restart(tmp_path):
    client, store = setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    client.patch(f"/api/v1/storage/sources/{source['id']}", headers=AUTH,
                 json={"expected_revision": source["revision"], "enabled": True})
    sender = {"Authorization": "Bearer " + created["token"]}
    first_time = utc_now()
    first = event(observed_at=first_time)
    accepted = client.post("/api/v1/storage/events", headers=sender, json=first)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["current"] is True
    assert client.post("/api/v1/storage/events", headers=sender, json=first).json()["duplicate"] is True
    assert client.post("/api/v1/storage/events", headers=sender, json={**first, "free_bytes": 1}).status_code == 409
    late = event("late", state="healthy", observed_at=first_time - timedelta(minutes=1), free=90)
    assert client.post("/api/v1/storage/events", headers=sender, json=late).json()["current"] is False
    assert store.count(StorageIncident) == 1
    assert store.count(StorageEventReceipt) == 2
    incident = store.list_entities(StorageIncident)[0]
    assert store.list_entities(StorageVolume)[0].state == "warning"
    assert store.count(ChatSession) == 1
    assert store.count(StorageDispatch) == 1
    worse = event("worse", state="critical", observed_at=first_time + timedelta(seconds=1), free=5)
    assert client.post("/api/v1/storage/events", headers=sender, json=worse).status_code == 202
    assert store.count(StorageIncident) == 1
    assert store.count(ChatSession) == 1
    assert store.count(StorageDispatch) == 2
    healthy = event("healthy", state="healthy", observed_at=first_time + timedelta(seconds=2), free=80)
    assert client.post("/api/v1/storage/events", headers=sender, json=healthy).status_code == 202
    assert store.get(StorageIncident, incident.id).state == "resolved"
    # A fresh Core sees durable state, including the old linked conversation.
    reloaded = create_app(database=tmp_path / "core.db", auth_token="operator-token")
    with TestClient(reloaded, base_url="https://testserver") as restarted:
        dashboard = restarted.get("/api/v1/storage", headers=AUTH).json()
        assert len(dashboard["incidents"]) == 1
        assert dashboard["incidents"][0]["chat_session_id"] == incident.chat_session_id
    assert store.count(ChatSession) == 1


def test_callback_offer_preview_approval_and_uncertain_lookup(tmp_path):
    client, store = setup(tmp_path)
    service = client.app.state.storage_integrations
    created = enroll(client)
    source = created["source"]
    source_entity = store.get_by_kind("storage_sources", source["id"])
    source_entity = store.update(type(source_entity), source["id"],
        {"enabled": True, "callback_url": "http://127.0.0.1:8766"}, expected_revision=source_entity.revision)
    sender = {"Authorization": "Bearer " + created["token"]}
    calls = []
    preview_mode = {"value": "drift"}

    async def callback(_source, path, body):
        calls.append((path, body))
        if path == "/v1/diagnose":
            return {"diagnostics": "read-only check", "offers": [{"id": "copy", "title": "Copy artifact"}]}
        if path.endswith("/preview"):
            from nebula.v3.storage_integrations import _digest
            effects = [{"operation": "copy", "source": "/fixture", "disposition": "retained"}]
            return {"effects": effects, "digest": "0" * 64 if preview_mode["value"] == "drift" else _digest(effects),
                    "expires_at": (utc_now() + timedelta(minutes=11 if preview_mode["value"] == "long" else 5)).isoformat()}
        if path.endswith("/execute"):
            raise httpx.TimeoutException("lost response")
        return {"status": "complete", "receipt": {"copied": True}}

    import httpx
    service._callback = callback
    assert client.post("/api/v1/storage/events", headers=sender, json=event()).status_code == 202
    action = store.list_entities(StorageAction)[0]
    assert client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH).status_code == 502
    preview_mode["value"] = "long"
    assert client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH).status_code == 502
    preview_mode["value"] = "valid"
    preview = client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH)
    assert preview.status_code == 200, preview.text
    digest = preview.json()["digest"]
    assert client.post(f"/api/v1/storage/actions/{action.id}/approve", headers=AUTH,
        json={"digest": "0" * 64}).status_code == 409
    approval = client.post(f"/api/v1/storage/actions/{action.id}/approve", headers=AUTH,
        json={"digest": digest})
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "complete"
    assert approval.json()["receipt"] == {"copied": True}
    assert sum(path.endswith("/execute") for path, _ in calls) == 1
    assert sum(path.endswith("/status") for path, _ in calls) == 1
    current_source = store.get_by_kind("storage_sources", source["id"])
    store.update(type(current_source), source["id"], {"enabled": False}, expected_revision=current_source.revision)
    assert client.post(f"/api/v1/storage/actions/{action.id}/preview", headers=AUTH).status_code == 409


def test_inventory_staleness_and_callback_failure_keep_alert_visible(tmp_path):
    import httpx

    client, store = setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    source_entity = store.get_by_kind("storage_sources", source["id"])
    store.update(type(source_entity), source["id"],
        {"enabled": True, "callback_url": "http://127.0.0.1:8766"}, expected_revision=source_entity.revision)
    sender = {"Authorization": "Bearer " + created["token"]}
    inventory = client.post("/api/v1/storage/inventory", headers=sender, json={
        "schema_version": 1, "devices": [{"id": "nas-1", "label": "NAS One"}]})
    assert inventory.status_code == 202, inventory.text
    assert store.count(StorageVolume) == 0

    async def unavailable(_source, _path, _body):
        raise httpx.ConnectError("callback offline")

    client.app.state.storage_integrations._callback = unavailable
    assert client.post("/api/v1/storage/events", headers=sender, json=event()).status_code == 202
    dashboard = client.get("/api/v1/storage", headers=AUTH).json()
    assert dashboard["incidents"][0]["callback_error"] == "callback offline"
    assert dashboard["incidents"][0]["dispatch_status"] == "queued"
    assert dashboard["actions"] == []
    assert len(dashboard["devices"]) == 1
    assert dashboard["volumes"][0]["state"] == "warning"
    volume = store.list_entities(StorageVolume)[0]
    store.update(StorageVolume, volume.id, {"received_at": utc_now() - timedelta(seconds=61)},
                 expected_revision=volume.revision)
    stale = client.get("/api/v1/storage", headers=AUTH).json()["volumes"][0]
    assert stale["stale"] is True
    assert stale["state"] == "warning"


def test_configured_runtime_failure_stays_retryable(tmp_path):
    client, store = setup(tmp_path)
    created = enroll(client)
    source = created["source"]
    client.patch(f"/api/v1/storage/sources/{source['id']}", headers=AUTH,
                 json={"expected_revision": source["revision"], "enabled": True})
    profile = store.get(ProviderProfile, "storage-provider")
    store.update(ProviderProfile, profile.id, {"enabled": False}, expected_revision=profile.revision)
    sender = {"Authorization": "Bearer " + created["token"]}
    assert client.post("/api/v1/storage/events", headers=sender, json=event()).status_code == 202
    dashboard = client.get("/api/v1/storage", headers=AUTH).json()
    assert dashboard["incidents"][0]["dispatch_status"] == "failed"
    assert dashboard["dispatches"][0]["status"] == "failed"
    assert store.count(ChatSession) == 0
    disabled = store.get(ProviderProfile, "storage-provider")
    store.update(ProviderProfile, disabled.id, {"enabled": True}, expected_revision=disabled.revision)
    dispatch_id = dashboard["dispatches"][0]["id"]
    retried = client.post(f"/api/v1/storage/dispatches/{dispatch_id}/retry", headers=AUTH)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert store.count(ChatSession) == 1
    chat = store.list_entities(ChatSession)[0]
    queue = store.get(ChatQueue, "chat-queue-" + chat.id)
    items = [dict(item) for item in queue.items]
    items[0].update(status="needs_review", detail="configured runtime failed")
    store.update(ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision)
    surfaced = client.get("/api/v1/storage", headers=AUTH).json()["dispatches"][0]
    assert surfaced["queue_status"] == "needs_review"
    assert surfaced["queue_detail"] == "configured runtime failed"
