from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
import httpx
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.automation_runtime import AutomationRuntimeManager
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.diagnostics import DiagnosticManager, SETTINGS_SCHEMA
from nebula.v3.domain import (
    BrowserSessionStatus,
    AutomationProjectPolicy,
    Engagement,
    HarnessProfile,
    McpServerProfile,
    ProviderProfile,
)
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import GuidedToolError


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


@pytest.mark.parametrize(
    ("method", "path", "read_path", "destination"),
    [
        ("PUT", "integrations/typesafe", "integrations/typesafe", "typesafe"),
        ("DELETE", "integrations/typesafe", "integrations/typesafe", "typesafe"),
        ("POST", "vpn-profiles", "vpn-profiles", "vpn-egress"),
        ("POST", "mcp-servers/import", "mcp-servers", "mcp"),
        ("DELETE", "vpn-profiles/example", "vpn-profiles", "vpn-egress"),
    ],
)
async def test_settings_api_keeps_private_integration_operations_in_browser(
    tmp_path, method, path, read_path, destination
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    operations = gateway.schema()["operations"]
    template = path.replace("/example", "/{profile_id}")
    assert not any(
        item["method"] == method and item["path"] == f"/api/v1/{template}"
        for item in operations
    )
    for candidate in {path, template}:
        with pytest.raises(ValueError, match=f"setting_id={destination}"):
            gateway.schema(path=candidate, method=method)
    assert gateway.schema(path=read_path, method="GET")["method"] == "GET"

    async def journey():
        before = await gateway.request(project.id, method="GET", path=read_path)
        with pytest.raises(ValueError, match="requesting browser"):
            await gateway.request(project.id, method=method, path=path)
        current = await gateway.request(project.id, method="GET", path=read_path)
        assert current["read_back"] is True
        assert current["result"] == before["result"]

    (await journey())


async def test_settings_api_saves_only_selected_project_and_reads_back(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
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

    (await journey())


async def test_settings_api_discovers_and_reads_back_core_wide_voice_timeout(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
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

    (await journey())


async def test_settings_api_discovers_and_reads_back_nebula_wide_butler_file(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
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

    (await journey())


async def test_settings_api_saves_and_reads_back_diagnostic_settings(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    manager = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    gateway = SettingsAssistantApi(
        create_app(
            store,
            auth_token="test-token",
            diagnostic_manager=manager,
        ),
        "test-token",
    )
    try:
        schema = gateway.schema(path="diagnostics/settings", method="PUT")
        assert schema["path"] == "/api/v1/diagnostics/settings"
        assert schema["request_body"]

        async def journey():
            current = await gateway.request(
                project.id, method="GET", path="diagnostics/settings"
            )
            assert current["result"]["schema"] == SETTINGS_SCHEMA
            saved = await gateway.request(
                project.id,
                method="PUT",
                path="diagnostics/settings",
                body={
                    "schema": SETTINGS_SCHEMA,
                    "global_level": "debug",
                    "feature_levels": {"storage": "debug"},
                    "sensitive_detail_capture": False,
                },
            )
            assert saved["read_back"] is True
            assert saved["result"]["global_level"] == "debug"
            assert saved["result"]["feature_levels"] == {"storage": "debug"}
            observed = await gateway.request(
                project.id, method="GET", path="diagnostics/settings"
            )
            assert observed["result"] == saved["result"]

        (await journey())
    finally:
        manager.close()


async def test_butler_discovers_and_revision_saves_its_check_interval(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    capabilities = await ButlerCapabilities.create(
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
        assert saved["state"] == "core_settings_saved"
        assert saved["client_action"] == {
            "kind": "butler_interval_refresh",
            "project_id": project.id,
        }
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

    (await journey())


async def test_settings_api_cannot_create_unusable_pairing_offer(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
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

    (await journey())


async def test_settings_api_keeps_credential_status_but_blocks_secret_mutations(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
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

    (await journey())


async def test_settings_api_confirms_provider_deletion_by_readback(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    provider = await store.create(
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

    (await journey())


@pytest.mark.parametrize(
    ("family", "model", "payload"),
    [
        ("providers", ProviderProfile, {"provider_type": "ollama", "is_local": True}),
        (
            "harnesses",
            HarnessProfile,
            {
                "kind": "codex_app_server",
                "connection_mode": "endpoint",
                "transport": "websocket",
                "endpoint": "ws://127.0.0.1:9999",
            },
        ),
        (
            "mcp-servers",
            McpServerProfile,
            {"transport": "streamable_http", "url": "https://example.test/mcp"},
        ),
    ],
)
async def test_settings_api_profile_lifecycle_reads_exact_created_identity(
    tmp_path, family, model, payload
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    existing = await store.create(
        model(name="existing-profile", enabled=False, **payload)
    )
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )
    assert gateway.schema(path=family, method="POST")["request_body"]
    capabilities = await ButlerCapabilities.create(
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
        assert (await store.get(model, profile["id"])).name == "new-profile"
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
        assert (await store.get(model, profile["id"])).name == "renamed-profile"
        with pytest.raises(ValueError, match="expected_revision"):
            await request(method="DELETE", path=path)
        with pytest.raises(ValueError, match="409"):
            await request(
                method="DELETE", path=path, expected_revision=profile["revision"]
            )
        assert (await store.get(model, profile["id"])).name == "renamed-profile"
        deleted = await request(
            method="DELETE", path=path, expected_revision=saved["result"]["revision"]
        )
        assert deleted["read_back"] is True
        assert deleted["result"] == {"deleted": True}
        assert (await store.get(model, existing.id)).name == "existing-profile"

    (await journey())


async def test_settings_api_create_keeps_identity_when_exact_readback_fails(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    gateway = SettingsAssistantApi(
        create_app(store, auth_token="test-token"), "test-token"
    )

    original_request = httpx.AsyncClient.request
    failed_paths: list[str] = []

    async def unavailable(client, method, path, **kwargs):
        if method == "GET" and path.startswith("/api/v1/providers/"):
            failed_paths.append(path)
            return httpx.Response(
                503, request=httpx.Request("GET", f"http://127.0.0.1{path}")
            )
        return await original_request(client, method, path, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", unavailable)

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
        assert failed_paths == [f"/api/v1/providers/{created['result']['id']}"]
        assert (
            await store.get(ProviderProfile, created["result"]["id"])
        ).name == "saved-unverified"

    (await journey())


async def test_headless_session_start_and_status_use_selected_project(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NEBULA_ONE_BROWSER_CUTOVER", raising=False)
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
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
    async with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        started = await client.post(
            f"/api/v1/engagements/{project.id}/agent-security/start", headers=headers
        )
        observed = await client.get(
            f"/api/v1/engagements/{project.id}/agent-security/status", headers=headers
        )
        assert started.status_code == 200
        assert started.json() == {"session_id": "headless", "state": "active"}
        assert observed.json() == {"session_id": "headless", "state": "active"}
        assert calls == [project.id, project.id]


@pytest.mark.parametrize(
    ("family", "payload", "private", "cleared"),
    [
        (
            "providers",
            {"provider_type": "ollama", "is_local": True},
            {"secret_ref": "session:" + "a" * 32},
            {"secret_ref": None},
        ),
        (
            "harnesses",
            {"kind": "codex_app_server", "executable": "/bin/true"},
            {"auth_mode": "secret_ref", "secret_ref": "session:" + "a" * 32},
            {"auth_mode": "existing_session", "secret_ref": None},
        ),
        (
            "mcp-servers",
            {"transport": "stdio", "command": "/bin/true"},
            {
                "trusted_stdio": True,
                "environment_secret_refs": {"TOKEN": "session:" + "a" * 32},
            },
            {"trusted_stdio": False, "environment_secret_refs": {}},
        ),
    ],
)
async def test_settings_profile_edits_preserve_browser_owned_authentication(
    tmp_path, family, payload, private, cleared
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    body = {"name": "operator-profile", "enabled": False, **payload, **private}
    response = await client.post(f"/api/v1/{family}", json=body)
    assert response.status_code == 201, response.text
    original = response.json()
    path = f"{family}/{original['id']}"

    async def journey():
        with pytest.raises(ValueError, match="requesting browser"):
            await gateway.request(project.id, method="POST", path=family, body=body)
        with pytest.raises(ValueError, match="requesting browser"):
            await gateway.request(
                project.id,
                method="PATCH",
                path=path,
                body={"expected_revision": original["revision"], "changes": cleared},
            )
        with pytest.raises(ValueError, match="requesting browser"):
            await gateway.request(
                project.id,
                method="PUT",
                path=path,
                expected_revision=original["revision"],
                body={
                    "id": original["id"],
                    "name": "implicit-reset",
                    "enabled": False,
                    **payload,
                },
            )
        assert (await client.get(f"/api/v1/{path}")).json() == original
        for method in ("PATCH", "PUT"):
            current = (await client.get(f"/api/v1/{path}")).json()
            updated = await gateway.request(
                project.id,
                method=method,
                path=path,
                body={
                    "expected_revision": current["revision"],
                    "changes": {"name": f"renamed-{method}"},
                }
                if method == "PATCH"
                else {**current, "name": f"renamed-{method}"},
            )
            assert updated["read_back"] is True
            assert updated["result"]["name"] == f"renamed-{method}"
            observed = (await client.get(f"/api/v1/{path}")).json()
            for key, value in private.items():
                assert observed[key] == value
            if family == "mcp-servers":
                assert updated["result"]["environment_secret_refs"] == {
                    "TOKEN": "[redacted]"
                }
        current = (await client.get(f"/api/v1/{path}")).json()
        with pytest.raises(ValueError, match="current revision"):
            await gateway.request(
                project.id,
                method="PATCH",
                path=path,
                body={"changes": {"name": "unguarded"}},
            )
        with pytest.raises(ValueError, match="profile changed"):
            await gateway.request(
                project.id,
                method="PATCH",
                path=path,
                body={
                    "expected_revision": original["revision"],
                    "changes": {"name": "stale"},
                },
            )
        assert (await client.get(f"/api/v1/{path}")).json() == current

    (await journey())


@pytest.mark.parametrize(
    ("family", "payload"),
    [
        ("providers", {"provider_type": "ollama", "is_local": True}),
        ("harnesses", {"kind": "codex_app_server", "executable": "/bin/true"}),
    ],
)
async def test_settings_profile_privacy_cannot_grant_or_silently_clear_consent(
    tmp_path, family, payload
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    privacy = {"permits_sensitive_data": True, "auto_share_tool_results": True}
    body = {"name": "operator-consent", "enabled": False, **payload, "privacy": privacy}
    created = await client.post(f"/api/v1/{family}", json=body)
    assert created.status_code == 201, created.text
    current = created.json()
    path = f"{family}/{current['id']}"

    async def journey():
        for flags in (privacy, {"permits_sensitive_data": True}):
            with pytest.raises(ValueError, match="requesting browser"):
                await gateway.request(
                    project.id,
                    method="POST",
                    path=family,
                    body={**body, "privacy": flags},
                )
        for flags in (
            {},
            {"permits_sensitive_data": True},
            {"permits_sensitive_data": False},
        ):
            with pytest.raises(ValueError, match="requesting browser"):
                await gateway.request(
                    project.id,
                    method="PATCH",
                    path=path,
                    body={
                        "expected_revision": current["revision"],
                        "changes": {"privacy": flags},
                    },
                )
        assert (await client.get(f"/api/v1/{path}")).json() == current
        saved = await gateway.request(
            project.id,
            method="PATCH",
            path=path,
            body={
                "expected_revision": current["revision"],
                "changes": {
                    "name": "retained consent",
                    "privacy": {
                        **current["privacy"],
                        "retention": "unchanged permission",
                    },
                },
            },
        )
        assert saved["read_back"] is True
        assert saved["result"]["privacy"]["auto_share_tool_results"] is True
        assert saved["result"]["privacy"]["permits_sensitive_data"] is True

    (await journey())


async def test_settings_profile_trust_stays_bound_to_the_operator_program(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        McpServerProfile(
            name="Trusted",
            transport="stdio",
            command="/bin/true",
            trusted_stdio=True,
            enabled=False,
        )
    )
    app = create_app(store, auth_token="test-token")
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        for changes in (
            {"command": "/bin/false"},
            {"arguments": ["--version"]},
            {"environment": {"MODE": "changed"}},
            {"cwd_policy": "fixed", "cwd": str(tmp_path)},
            {
                "transport": "streamable_http",
                "command": None,
                "url": "https://example.test/mcp",
            },
        ):
            with pytest.raises(ValueError, match="local-program trust"):
                await gateway.request(
                    project.id,
                    method="PATCH",
                    path=f"mcp-servers/{profile.id}",
                    body={"expected_revision": profile.revision, "changes": changes},
                )
        assert (await store.get(McpServerProfile, profile.id)) == profile

    (await journey())


async def test_settings_profile_replace_fences_a_concurrent_browser_credential_edit(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        ProviderProfile(name="Concurrent", provider_type="ollama", is_local=True)
    )
    app = create_app(store, auth_token="test-token")
    observed_headers = []

    @app.middleware("http")
    async def concurrent_operator(request, call_next):
        if (
            request.method == "PUT"
            and request.url.path == f"/api/v1/providers/{profile.id}"
        ):
            observed_headers.append(request.headers.get("If-Match"))
            (
                await store.update(
                    ProviderProfile,
                    profile.id,
                    {"secret_ref": "session:" + "b" * 32},
                    expected_revision=profile.revision,
                )
            )
        return await call_next(request)

    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        with pytest.raises(ValueError, match="revision conflict"):
            await gateway.request(
                project.id,
                method="PUT",
                path=f"providers/{profile.id}",
                body={**profile.model_dump(mode="json"), "name": "stale rename"},
            )
        assert observed_headers == [str(profile.revision)]
        current = await store.get(ProviderProfile, profile.id)
        assert current.name == profile.name
        assert current.secret_ref == "session:" + "b" * 32

    (await journey())


async def test_settings_mcp_collection_and_export_filter_before_serialization(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    for index in range(103):
        (
            await store.create(
                McpServerProfile(
                    name=f"foreign-private-{index}",
                    transport="streamable_http",
                    url="https://example.test/mcp",
                    enabled=False,
                    metadata={"engagement_id": other.id},
                )
            )
        )
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        empty = await gateway.request(
            selected.id, method="GET", path="mcp-servers/export"
        )
        assert empty["result"] == {"config": {"mcpServers": {}}, "warnings": []}
        visible = [
            (
                await store.create(
                    McpServerProfile(
                        name=name,
                        transport="streamable_http",
                        url="https://example.test/mcp",
                        enabled=False,
                        metadata=metadata,
                    )
                )
            )
            for name, metadata in (
                ("selected-profile", {"engagement_id": selected.id}),
                ("shared-profile", {}),
            )
        ]
        listed = await gateway.request(selected.id, method="GET", path="mcp-servers")
        assert {row["id"] for row in listed["result"]} == {row.id for row in visible}
        exported = await gateway.request(
            selected.id, method="GET", path="mcp-servers/export"
        )
        assert set(exported["result"]["config"]["mcpServers"]) == {
            row.name for row in visible
        }
        assert "foreign-private" not in str(listed) + str(exported)
        for path in ("mcp-servers", "mcp-servers/export"):
            with pytest.raises(ValueError, match="401"):
                await SettingsAssistantApi(app, "invalid").request(
                    selected.id, method="GET", path=path
                )

    (await journey())


async def test_settings_mcp_exact_operations_refuse_another_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    foreign = await store.create(
        McpServerProfile(
            name="other-project-profile",
            transport="streamable_http",
            url="https://example.test/mcp",
            enabled=False,
            metadata={"engagement_id": other.id},
        )
    )
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        for method, body, revision, suffix in (
            ("GET", None, None, ""),
            (
                "PATCH",
                {"expected_revision": foreign.revision, "changes": {"name": "changed"}},
                None,
                "",
            ),
            ("PUT", foreign.model_dump(mode="json"), foreign.revision, ""),
            ("DELETE", None, foreign.revision, ""),
            ("POST", {}, None, "/probe"),
        ):
            with pytest.raises(
                GuidedToolError, match="not in the selected project"
            ) as refusal:
                await gateway.request(
                    selected.id,
                    method=method,
                    path=f"mcp-servers/{foreign.id}{suffix}",
                    body=body,
                    expected_revision=revision,
                )
            assert refusal.value.code == "mcp_project_scope"
            assert getattr(refusal.value, "_nebula_before_execution", False) is True
            assert (await store.get(McpServerProfile, foreign.id)) == foreign

    (await journey())


async def test_settings_mcp_edits_preserve_project_ownership(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        body = {
            "name": "selected-profile",
            "transport": "streamable_http",
            "url": "https://example.test/mcp",
            "enabled": False,
            "metadata": {"engagement_id": selected.id},
        }
        with pytest.raises(GuidedToolError, match="not in the selected project"):
            await gateway.request(
                selected.id,
                method="POST",
                path="mcp-servers",
                body={**body, "metadata": {"engagement_id": other.id}},
            )
        created = await gateway.request(
            selected.id, method="POST", path="mcp-servers", body=body
        )
        current = created["result"]
        assert created["read_back"] is True
        assert current["metadata"]["engagement_id"] == selected.id
        path = f"mcp-servers/{current['id']}"
        for method, replacement in (
            (
                "PATCH",
                {"expected_revision": current["revision"], "changes": {"metadata": {}}},
            ),
            ("PUT", {**current, "metadata": {}}),
        ):
            with pytest.raises(ValueError, match="preserve.*project ownership"):
                await gateway.request(
                    selected.id, method=method, path=path, body=replacement
                )
        saved = await gateway.request(
            selected.id,
            method="PATCH",
            path=path,
            body={
                "expected_revision": current["revision"],
                "changes": {"name": "renamed"},
            },
        )
        assert saved["read_back"] is True
        assert saved["result"]["metadata"] == current["metadata"]
        deleted = await gateway.request(
            selected.id,
            method="DELETE",
            path=path,
            expected_revision=saved["result"]["revision"],
        )
        assert deleted["result"] == {"deleted": True}

    (await journey())


async def test_settings_mcp_probe_pins_the_selected_project(tmp_path, monkeypatch):
    from nebula.v3.mcp import McpProbeService

    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    selected = await store.create(Engagement(name="Selected"))
    profile = await store.create(
        McpServerProfile(
            name="shared",
            transport="streamable_http",
            url="https://example.test/mcp",
            enabled=False,
        )
    )
    observed = []

    async def probe(self, profile_id, *, engagement_id=None):
        observed.append((profile_id, engagement_id))
        return {
            "profile_id": profile_id,
            "compatible": True,
            "capabilities": {"tools": []},
        }

    monkeypatch.setattr(McpProbeService, "probe", probe)
    gateway = SettingsAssistantApi(app, "test-token")

    async def journey():
        path = f"mcp-servers/{profile.id}/probe"
        for owner in ("another-project", [selected.id]):
            with pytest.raises(ValueError, match="only in the selected project"):
                await gateway.request(
                    selected.id, method="POST", path=path, body={"engagement_id": owner}
                )
        assert observed == []
        await gateway.request(selected.id, method="POST", path=path, body={})
        assert observed == [(profile.id, selected.id)]

    (await journey())


async def test_settings_mcp_changed_readback_preserves_uncertain_effects(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    profile = await store.create(
        McpServerProfile(
            name="selected-profile",
            transport="streamable_http",
            url="https://example.test/mcp",
            enabled=False,
            metadata={"engagement_id": selected.id},
        )
    )

    @app.middleware("http")
    async def concurrent_owner_change(request, call_next):
        response = await call_next(request)
        if (
            request.method == "PATCH"
            and request.url.path == f"/api/v1/mcp-servers/{profile.id}"
        ):
            current = await store.get(McpServerProfile, profile.id)
            (
                await store.update(
                    McpServerProfile,
                    profile.id,
                    {"metadata": {"engagement_id": other.id}},
                    expected_revision=current.revision,
                )
            )
        return response

    async def journey():
        with pytest.raises(
            GuidedToolError, match="not in the selected project"
        ) as refusal:
            await SettingsAssistantApi(app, "test-token").request(
                selected.id,
                method="PATCH",
                path=f"mcp-servers/{profile.id}",
                body={
                    "expected_revision": profile.revision,
                    "changes": {"name": "saved-before-owner-change"},
                },
            )
        assert getattr(refusal.value, "_nebula_before_execution", False) is False
        assert (
            await store.get(McpServerProfile, profile.id)
        ).name == "saved-before-owner-change"

    (await journey())


async def _policy_gateway(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    policy = await store.create(
        AutomationProjectPolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:automation-policy:{project.id}")),
            engagement_id=project.id,
            execution_mode="docker",
            host_access_acknowledged=False,
        )
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")

    async def no_execution(_launch):
        raise AssertionError("policy metadata must not start a command session")

    runtime = AutomationRuntimeManager(
        store=store,
        artifact_store=artifacts,
        data_root=tmp_path / "runtime",
        workspace_resolver=lambda _: tmp_path,
        runtime_image="registry.invalid/nebula-automation@sha256:" + "a" * 64,
        session_factory=no_execution,
    )
    app = create_app(
        store,
        auth_token="test-token",
        artifact_store=artifacts,
        automation_runtime=runtime,
    )
    return store, project, policy, app, SettingsAssistantApi(app, "test-token")


@pytest.mark.parametrize("acknowledgment", [True, "true", 1, "yes"])
async def test_settings_policy_cannot_create_first_host_consent(
    tmp_path, acknowledgment
):
    store, project, policy, app, gateway = await _policy_gateway(tmp_path)
    preparations = []

    @app.middleware("http")
    async def observe_dispatch(request, call_next):
        if request.method == "PUT":
            preparations.append(request.url.path)
        return await call_next(request)

    async def journey():
        for mode in ("docker", "host", None):
            with pytest.raises(GuidedToolError) as refusal:
                await gateway.request(
                    project.id,
                    method="PUT",
                    path="engagements/{project}/automation-policy",
                    body={
                        "host_access_acknowledged": acknowledgment,
                        "execution_mode": mode,
                        "expected_revision": policy.revision,
                    },
                )
            assert refusal.value.code == "host_consent_required"
            assert "setting_id=network-scope" in refusal.value.next_action
            assert getattr(refusal.value, "_nebula_before_execution", False)
            assert (await store.get(AutomationProjectPolicy, policy.id)) == policy
        assert preparations == []

    (await journey())


async def test_settings_policy_preserves_prior_consent_and_requires_body_revision(
    tmp_path,
):
    store, project, policy, app, gateway = await _policy_gateway(tmp_path)
    path = "engagements/{project}/automation-policy"

    async def journey():
        for arguments in ({}, {"expected_revision": policy.revision}):
            with pytest.raises(GuidedToolError) as refusal:
                await gateway.request(
                    project.id,
                    method="PUT",
                    path=path,
                    body={"execution_mode": "docker", "network_enabled": False},
                    **arguments,
                )
            assert refusal.value.code == "settings_revision_required"
        saved = await gateway.request(
            project.id,
            method="PUT",
            path=path,
            body={
                "execution_mode": "docker",
                "network_enabled": False,
                "host_access_acknowledged": "false",
                "expected_revision": policy.revision,
            },
        )
        assert (
            saved["read_back"] is True and saved["result"]["network_enabled"] is False
        )
        assert saved["result"]["host_access_acknowledged"] is False
        # The direct browser API retains its separate consent authority. This
        # metadata-only Docker request launches no Host program or session.
        async with TestClient(
            app, headers={"Authorization": "Bearer test-token"}
        ) as client:
            response = await client.put(
                f"/api/v1/engagements/{project.id}/automation-policy",
                json={
                    "execution_mode": "docker",
                    "host_access_acknowledged": True,
                    "expected_revision": saved["result"]["revision"],
                    "network_enabled": False,
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()
        preserved = await gateway.request(
            project.id,
            method="PUT",
            path=path,
            body={
                "execution_mode": "docker",
                "host_access_acknowledged": False,
                "expected_revision": current["revision"],
                "max_timeout_ms": 120_000,
            },
        )
        assert preserved["read_back"] is True
        assert preserved["result"]["host_access_acknowledged"] is True
        with pytest.raises(GuidedToolError) as refusal:
            await gateway.request(
                project.id,
                method="PUT",
                path=path,
                body={"execution_mode": "docker", "expected_revision": policy.revision},
            )
        assert refusal.value.code == "settings_revision_conflict"

    (await journey())


async def test_settings_policy_refuses_a_concurrent_operator_edit(tmp_path):
    store, project, policy, app, gateway = await _policy_gateway(tmp_path)

    @app.middleware("http")
    async def concurrent_edit(request, call_next):
        if request.method == "PUT" and request.url.path.endswith("/automation-policy"):
            (
                await store.update(
                    AutomationProjectPolicy,
                    policy.id,
                    {"network_enabled": False},
                    expected_revision=policy.revision,
                )
            )
        return await call_next(request)

    async def journey():
        with pytest.raises(ValueError, match="409"):
            await gateway.request(
                project.id,
                method="PUT",
                path="engagements/{project}/automation-policy",
                body={
                    "execution_mode": "docker",
                    "host_access_acknowledged": False,
                    "max_timeout_ms": 120_000,
                    "expected_revision": policy.revision,
                },
            )
        current = await store.get(AutomationProjectPolicy, policy.id)
        assert current.network_enabled is False
        assert current.max_timeout_ms == policy.max_timeout_ms
        assert current.host_access_acknowledged is False

    (await journey())


pytestmark = pytest.mark.anyio
