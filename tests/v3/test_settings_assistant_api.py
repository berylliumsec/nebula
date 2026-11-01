from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import (
    BrowserSessionStatus,
    Engagement,
    HarnessProfile,
    McpServerProfile,
    ProviderProfile,
)
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


def test_butler_discovers_and_revision_saves_its_check_interval(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id, settings_api=gateway
    )

    async def invoke(operation, arguments, *, change=False):
        return await capabilities.invoke(
            {"operation": operation, "arguments": arguments}, change=change
        )

    async def journey():
        opened = await invoke(
            "nebula.settings.open", {"setting_id": "butler-check-interval"}, change=True
        )
        assert opened["state"] == "pending_browser_apply"
        schema = await invoke(
            "nebula.settings.api_schema",
            {"path": "butler/check-interval", "method": "PUT"},
        )
        assert schema["request_body"]
        current = await invoke(
            "nebula.settings.api_request",
            {"path": "butler/check-interval", "method": "GET"},
            change=True,
        )
        assert current["result"]["check_interval_minutes"] == 5
        saved = await invoke(
            "nebula.settings.api_request",
            {
                "path": "butler/check-interval",
                "method": "PUT",
                "body": {
                    "check_interval_minutes": 2,
                    "expected_revision": current["result"]["revision"],
                },
            },
            change=True,
        )
        assert saved["read_back"] is True
        assert saved["result"]["check_interval_minutes"] == 2
        assert saved["result"]["revision"] > current["result"]["revision"]
        with pytest.raises(ValueError, match="409"):
            await invoke(
                "nebula.settings.api_request",
                {
                    "path": "butler/check-interval",
                    "method": "PUT",
                    "body": {
                        "check_interval_minutes": 10,
                        "expected_revision": current["result"]["revision"],
                    },
                },
                change=True,
            )
        observed = await gateway.request(
            project.id, method="GET", path="butler/check-interval"
        )
        assert observed["result"]["check_interval_minutes"] == 2

    asyncio.run(journey())


def test_settings_api_cannot_create_unusable_pairing_offer(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token", allow_insecure_device_pairing=True)
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        with pytest.raises(ValueError, match="outside the assistant's Settings API"):
            gateway.schema(path="auth/pairings", method="POST")
        with pytest.raises(ValueError, match="outside the assistant's Settings API"):
            await gateway.request(
                project.id,
                method="POST",
                path="auth/pairings",
                body={"name": "Test device"},
            )
        with pytest.raises(ValueError, match="outside the assistant's Settings API"):
            await gateway.request(
                project.id,
                method="DELETE",
                path="auth/devices/unused",
            )
        assert not app.state.pending_pairings

    asyncio.run(journey())


def test_settings_api_keeps_credential_status_but_blocks_secret_mutations(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")
    assert (
        gateway.schema(path="credentials/vault", method="GET")["path"]
        == "/api/v1/credentials/vault"
    )
    reference = "session:" + "a" * 32
    assert gateway.schema(path=f"credentials/{reference}/status", method="GET")[
        "path"
    ].endswith("/status")

    async def journey():
        vault = await gateway.request(
            project.id, method="GET", path="credentials/vault"
        )
        assert "available" in vault["result"]
        status = await gateway.request(
            project.id, method="GET", path=f"credentials/{reference}/status"
        )
        assert status["result"]["state"] == "session_expired"
        for method, path, body in (
            ("POST", "credentials", {"secret": "model-visible"}),
            ("DELETE", f"credentials/{reference}", None),
        ):
            with pytest.raises(
                ValueError, match="outside the assistant's Settings API"
            ):
                gateway.schema(path=path, method=method)
            with pytest.raises(
                ValueError, match="outside the assistant's Settings API"
            ):
                await gateway.request(project.id, method=method, path=path, body=body)

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
            project.id,
            method="DELETE",
            path=f"providers/{provider.id}",
            expected_revision=provider.revision,
        )
        assert result["status"] in (200, 204)
        assert result["read_back"] is True
        assert result["result"] == {"deleted": True}

    asyncio.run(journey())


@pytest.mark.parametrize(
    ("family", "model", "payload"),
    [
        ("providers", ProviderProfile, {"provider_type": "ollama", "is_local": True}),
        (
            "harnesses",
            HarnessProfile,
            {"kind": "codex_app_server", "executable": "/bin/true"},
        ),
        (
            "mcp-servers",
            McpServerProfile,
            {"transport": "streamable_http", "url": "https://example.test/mcp"},
        ),
    ],
)
def test_settings_api_profile_lifecycle_reads_exact_created_identity(
    tmp_path, family, model, payload
):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    existing = store.create(model(name="existing-profile", enabled=False, **payload))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    assert gateway.schema(path=family, method="POST")["request_body"]
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id, settings_api=gateway
    )

    async def request(**arguments):
        return await capabilities.invoke(
            {"operation": "nebula.settings.api_request", "arguments": arguments},
            change=True,
        )

    async def journey():
        created = await request(
            method="POST",
            path=family,
            body={"name": "new-profile", "enabled": False, **payload},
        )
        assert created["read_back"] is True
        assert created["state"] == "core_settings_saved"
        assert created["client_action"] == {
            "kind": "settings_profile_refresh",
            "project_id": project.id,
            "resource": family,
        }
        assert isinstance(created["result"], dict)
        profile = created["result"]
        assert profile["id"] != existing.id
        assert profile["name"] == "new-profile"
        assert store.get(model, profile["id"]).name == "new-profile"
        path = f"{family}/{profile['id']}"
        saved = await request(
            method="PATCH",
            path=path,
            body={
                "expected_revision": profile["revision"],
                "changes": {"name": "renamed-profile"},
            },
        )
        assert saved["read_back"] is True
        assert saved["result"]["id"] == profile["id"]
        assert saved["result"]["name"] == "renamed-profile"
        assert saved["result"]["revision"] > profile["revision"]
        with pytest.raises(ValueError, match="409"):
            await request(
                method="PATCH",
                path=path,
                body={
                    "expected_revision": profile["revision"],
                    "changes": {"name": "stale-profile"},
                },
            )
        assert store.get(model, profile["id"]).name == "renamed-profile"
        with pytest.raises(ValueError, match="expected_revision"):
            await request(method="DELETE", path=path)
        with pytest.raises(ValueError, match="409"):
            await request(
                method="DELETE", path=path, expected_revision=profile["revision"]
            )
        assert store.get(model, profile["id"]).name == "renamed-profile"
        deleted = await request(
            method="DELETE", path=path, expected_revision=saved["result"]["revision"]
        )
        assert deleted["read_back"] is True
        assert deleted["result"] == {"deleted": True}
        assert store.get(model, existing.id).name == "existing-profile"

    asyncio.run(journey())


def test_settings_api_create_keeps_identity_when_exact_readback_fails(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )

    async def unavailable(client, path, **kwargs):
        return httpx.Response(
            503, request=httpx.Request("GET", f"http://127.0.0.1{path}")
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", unavailable)

    async def journey():
        created = await gateway.request(
            project.id,
            method="POST",
            path="providers",
            body={
                "name": "saved-unverified",
                "provider_type": "ollama",
                "is_local": True,
                "enabled": False,
            },
        )
        assert created["read_back"] is False
        assert "client_action" not in created
        assert created["result"]["name"] == "saved-unverified"
        assert (
            store.get(ProviderProfile, created["result"]["id"]).name
            == "saved-unverified"
        )

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
