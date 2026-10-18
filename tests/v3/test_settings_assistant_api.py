from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import BrowserSessionStatus, Engagement, ProviderProfile
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore


def test_settings_api_discovers_existing_routes_and_rejects_other_core_workflows(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")
    operations = gateway.schema()["operations"]
    assert any(
        item["method"] == "PUT"
        and item["path"] == "/api/v1/engagements/{project}/scope"
        for item in operations
    )
    assert any(item["path"] == "/api/v1/providers/{entity_id}" for item in operations)
    assert any(
        item["path"] == "/api/v1/voice/settings" and item["method"] == "PATCH"
        for item in operations
    )
    assert not any(item["path"] == "/api/v1/voice/session" for item in operations)
    assert not any(
        "/chat/" in item["path"] or "/test-turn" in item["path"] for item in operations
    )
    schema = gateway.schema(path="providers/{entity_id}", method="PATCH")
    assert schema["path"] == "/api/v1/providers/{entity_id}"
    assert schema["request_body"]
    for path in (
        "chat/sessions/another/messages",
        "engagements/another/scope",
        "engagements/{project}/../another/scope",
        "providers/%2e%2e/chat",
        "auth/pairings/redeem",
    ):
        with pytest.raises(ValueError):
            gateway.schema(path=path, method="GET")


def test_settings_api_saves_only_selected_project_and_reads_back(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        path = "engagements/{project}/scope"
        current = (await gateway.request(selected.id, method="GET", path=path))[
            "result"
        ]
        saved = await gateway.request(
            selected.id,
            method="PUT",
            path=path,
            body={
                "allow_all_targets": True,
                "bypass_permissions": False,
                "expected_revision": current["revision"],
            },
        )
        untouched = (await gateway.request(other.id, method="GET", path=path))["result"]
        assert saved["status"] == 200
        assert saved["read_back"] is True
        assert saved["result"]["allow_all_targets"] is True
        assert saved["result"]["bypass_permissions"] is False
        assert saved["result"]["revision"] >= current["revision"]
        assert untouched["allow_all_targets"] is False
        with pytest.raises(ValueError):
            await gateway.request(
                selected.id,
                method="PUT",
                path=f"engagements/{other.id}/scope",
                body={"allow_all_targets": True},
            )

    asyncio.run(journey())


def test_settings_api_discovers_and_reads_back_core_wide_voice_timeout(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    schema = gateway.schema(path="voice/settings", method="PATCH")
    assert schema["path"] == "/api/v1/voice/settings"
    assert schema["request_body"]

    async def journey():
        current = await gateway.request(project.id, method="GET", path="voice/settings")
        assert current["result"] == {"auto_pause_seconds": 20, "revision": 0}
        saved = await gateway.request(
            project.id,
            method="PATCH",
            path="voice/settings",
            body={"expected_revision": 0, "auto_pause_seconds": 10},
        )
        assert saved["read_back"] is True
        assert saved["result"] == {"auto_pause_seconds": 10, "revision": 1}

    asyncio.run(journey())


def test_settings_api_discovers_and_reads_back_nebula_wide_butler_file(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token", execution_data_root=tmp_path),
        "test-token",
    )
    schema = gateway.schema(path="butler/instructions", method="PUT")
    assert schema["path"] == "/api/v1/butler/instructions"
    assert schema["request_body"]

    async def journey():
        current = await gateway.request(
            project.id, method="GET", path="butler/instructions"
        )
        assert current["result"]["status"] == "missing"
        saved = await gateway.request(
            project.id,
            method="PUT",
            path="butler/instructions",
            body={"content": "Check Act projects.", "expected_sha256": None},
        )
        assert saved["read_back"] is True
        assert saved["result"]["status"] == "ready"
        assert saved["result"]["content"] == "Check Act projects."

    asyncio.run(journey())


def test_settings_api_redacts_pairing_secret(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token", allow_insecure_device_pairing=True)
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        created = await gateway.request(
            project.id,
            method="POST",
            path="auth/pairings",
            body={"name": "Test device"},
        )
        assert created["status"] in (200, 201)
        assert created["result"]["secret"] == "[redacted]"

    asyncio.run(journey())


def test_settings_api_confirms_provider_deletion_by_readback(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(name="Disposable", provider_type="ollama", is_local=True)
    )
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        result = await gateway.request(
            project.id, method="DELETE", path=f"providers/{provider.id}"
        )
        assert result["status"] in (200, 204)
        assert result["read_back"] is True
        assert result["result"] == {"deleted": True}

    asyncio.run(journey())


def test_headless_session_start_and_status_use_selected_project(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_ONE_BROWSER_CUTOVER", raising=False)
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token")
    calls: list[str] = []

    async def ensure(project_id: str):
        calls.append(project_id)
        return SimpleNamespace(id="headless", status=BrowserSessionStatus.ACTIVE)

    async def status(project_id: str, name: str, arguments: dict):
        assert name == "status" and arguments == {}
        calls.append(project_id)
        return {"structuredContent": {"session_id": "headless", "state": "active"}}

    monkeypatch.setattr(app.state.agent_security_sessions, "ensure", ensure)
    monkeypatch.setattr(app.state.agent_security_tools, "call_tool", status)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        started = client.post(
            f"/api/v1/engagements/{project.id}/agent-security/start", headers=headers
        )
        observed = client.get(
            f"/api/v1/engagements/{project.id}/agent-security/status", headers=headers
        )
        assert started.status_code == 200
        assert started.json() == {"session_id": "headless", "state": "active"}
        assert observed.json() == {"session_id": "headless", "state": "active"}
        assert calls == [project.id, project.id]
