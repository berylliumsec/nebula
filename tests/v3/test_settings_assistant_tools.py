from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import BUTLER_TOOL_NAMES
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatHistoryConflict,
    ChatRequestMessage,
    isolate_settings_assistant_request,
)
from nebula.v3.domain import (
    AutomationProjectPolicy,
    ChatRole,
    ChatSession,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    ProviderProfile,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from nebula.v3.settings_assistant_tools import (
    APP_DESTINATIONS,
    SETTING_DESTINATIONS,
    catalog,
    provider_plugins,
    project_context,
    model_choices,
    set_default_model,
    set_post_tool_preference,
    open_setting,
    open_destination,
    request_device_theme,
    request_project_execution_mode,
    request_project_scope,
    request_headless_session,
)


def test_catalog_covers_settings_destinations_without_secrets():
    result = catalog()
    assert len(result["destinations"]) == len(APP_DESTINATIONS) == 22
    assert {item["id"] for item in result["destinations"]} == {
        item["id"] for item in APP_DESTINATIONS
    }
    assert (
        next(item for item in result["destinations"] if item["id"] == "projects")[
            "path"
        ]
        == "/projects"
    )
    assert len(result["settings"]) == len(SETTING_DESTINATIONS) == 23
    assert (
        next(item for item in result["destinations"] if item["id"] == "storage")["path"]
        == "/storage"
    )
    assert (
        next(item for item in result["destinations"] if item["id"] == "notebook")[
            "path"
        ]
        == "/projects/{project_id}/workbench?view=notebook"
    )
    notebook_family = next(
        item for item in result["capability_families"] if item["id"] == "notebook"
    )
    assert notebook_family["discover_with"] == "nebula.capabilities.search"
    assert "nebula.notebook.edit_cell" in notebook_family["operations"]
    assert (
        "Butler conversations"
        in next(item for item in result["destinations"] if item["id"] == "chat")[
            "aliases"
        ]
    )
    assert (
        "switch from harness to provider"
        in next(item for item in result["destinations"] if item["id"] == "chat")[
            "aliases"
        ]
    )
    assert (
        next(item for item in result["settings"] if item["id"] == "storage")["url"]
        == "/settings#storage-integration-settings"
    )
    assert (
        open_setting({"setting_id": "storage"})["client_action"]["setting_id"]
        == "storage"
    )
    assert {item["id"] for item in result["settings"]} == {
        item[0] for item in SETTING_DESTINATIONS
    }
    assert all(item["url"].startswith("/settings#") for item in result["settings"])
    assert all(
        "credential" not in str(item.get("direct_changes", []))
        for item in result["settings"]
    )
    project_policy = next(
        item for item in result["settings"] if item["id"] == "network-scope"
    )
    assert project_policy["assistant_ui_actions"] == [
        "project_execution_mode",
        "project_scope",
    ]
    assert next(item for item in result["settings"] if item["id"] == "appearance")[
        "assistant_ui_actions"
    ] == ["device_theme"]
    assert (
        next(item for item in result["settings"] if item["id"] == "butler-effort")[
            "url"
        ]
        == "/settings#butler-settings"
    )
    assert (
        next(item for item in result["settings"] if item["id"] == "butler-voice")["url"]
        == "/settings#butler-settings"
    )
    assert (
        next(
            item for item in result["settings"] if item["id"] == "butler-instructions"
        )["url"]
        == "/settings#butler-instructions-settings"
    )
    assert project_policy["direct_changes"] == []
    assert {item["name"] for item in result["actions"]} == {
        "nebula.navigate.open",
        "nebula.settings.open",
        "nebula.settings.set_device_theme",
        "nebula.settings.set_project_mode",
        "nebula.settings.set_project_scope",
        "nebula.settings.headless_session",
        "nebula.settings.api_schema",
        "nebula.settings.api_request",
        "nebula.settings.notebook",
        "nebula.settings.set_notebook",
        "nebula.settings.set_post_tool",
        "nebula.settings.set_default_model",
    }


def test_browser_actions_are_bounded_intents_until_the_requesting_device_applies_them():
    assert open_setting({"setting_id": "butler-effort"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "open_setting", "setting_id": "butler-effort"},
    }
    assert open_setting({"setting_id": "butler-voice"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "open_setting", "setting_id": "butler-voice"},
    }
    assert open_destination({"destination_id": "projects"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "navigate", "destination_id": "projects"},
    }
    assert open_setting({"setting_id": "appearance"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "open_setting", "setting_id": "appearance"},
    }
    assert request_device_theme({"theme": "zero-dark"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "device_theme", "theme": "zero-dark"},
    }
    assert {"glacier", "amber", "zero-classic"} <= set(
        catalog()["device_appearance"]["values"]
    )
    for theme in ("glacier", "amber", "zero-classic"):
        assert request_device_theme({"theme": theme})["client_action"] == {
            "kind": "device_theme",
            "theme": theme,
        }
    assert request_project_execution_mode({"mode": "host", "from": "docker"}) == {
        "state": "pending_browser_apply",
        "client_action": {
            "kind": "project_execution_mode",
            "mode": "host",
            "from": "docker",
        },
    }
    assert request_project_scope({"mode": "all_destinations"}) == {
        "state": "pending_browser_apply",
        "client_action": {
            "kind": "project_scope",
            "changes": {"mode": "all_destinations"},
        },
    }
    assert request_headless_session({"action": "start"}) == {
        "state": "pending_browser_apply",
        "client_action": {"kind": "headless_session", "action": "start"},
    }
    with pytest.raises(ValueError):
        open_setting({"setting_id": "hidden-secret"})
    with pytest.raises(ValueError):
        open_destination({"destination_id": "https://example.org"})
    with pytest.raises(ValueError):
        request_device_theme({"theme": "custom-css"})
    with pytest.raises(ValueError):
        request_project_execution_mode({"mode": "unconfined"})


def test_post_tool_change_reads_back_from_core(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Bounded project"))
    result = set_post_tool_preference(
        store, artifacts, project.id, {"setting": "take_notes", "enabled": False}
    )
    assert result == {
        "setting": "take_notes",
        "enabled": False,
        "project_id": project.id,
        "project_name": "Bounded project",
        "saved": True,
    }
    with pytest.raises(Exception):
        set_post_tool_preference(
            store,
            artifacts,
            project.id,
            {"setting": "take_notes", "enabled": True},
        )
    assert (
        store.get(Engagement, project.id).metadata["post_tool_assistant"]["take_notes"]
        is False
    )


def test_project_context_reads_mode_and_linked_workspace_without_creating_policy(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Bounded project"))
    other = store.create(Engagement(name="Other project"))
    store.create(
        AutomationProjectPolicy(
            engagement_id=other.id, execution_mode="host", host_access_acknowledged=True
        )
    )
    default = project_context(store, project.id)
    assert default["execution_mode"] == "host"
    assert default["policy_source"] == "default"
    assert "No execution policy is stored" in default["policy_description"]
    assert "pending acknowledgment" in default["policy_description"]
    assert default["linked_workspace_path"] is None
    assert not store.find_entities(
        AutomationProjectPolicy, {"engagement_id": project.id}
    )
    auto_default = store.create(Engagement(name="Auto default"))
    store.create(
        AutomationProjectPolicy(engagement_id=auto_default.id, execution_mode="docker")
    )
    stored_docker = project_context(store, auto_default.id)
    assert stored_docker["policy_source"] == "stored"
    assert (
        "may be the automatically created default"
        in stored_docker["policy_description"]
    )

    linked = tmp_path / "linked"
    linked.mkdir()
    project = store.update(
        Engagement,
        project.id,
        {"workspace_path": str(linked)},
        expected_revision=project.revision,
    )
    store.create(
        AutomationProjectPolicy(
            engagement_id=project.id,
            execution_mode="host",
            host_access_acknowledged=True,
        )
    )
    observed = project_context(store, project.id)
    assert observed["execution_mode"] == "host"
    assert observed["policy_source"] == "stored"
    assert "host-access acknowledgment" in observed["policy_description"]
    assert observed["linked_workspace_path"] == str(linked)
    assert observed["workspace_kind"] == "linked_host_folder"
    assert "Existing command sessions" in observed["existing_sessions"]


def test_provider_tools_are_bounded_to_catalog_and_project(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Project A"))
    names = {
        plugin.spec.name for plugin in provider_plugins(store, artifacts, project.id)
    }
    assert names == {
        "nebula.navigate.open",
        "nebula.help.search",
        "nebula.help.project_context",
        "nebula.settings.catalog",
        "nebula.settings.open",
        "nebula.settings.set_device_theme",
        "nebula.settings.set_project_mode",
        "nebula.settings.set_project_scope",
        "nebula.settings.headless_session",
        "nebula.settings.api_schema",
        "nebula.settings.api_request",
        "nebula.settings.notebook",
        "nebula.settings.set_notebook",
        "nebula.settings.set_post_tool",
        "nebula.settings.models",
        "nebula.settings.set_default_model",
    }


def test_provider_runtime_offers_settings_tools_only_to_the_settings_assistant(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Bounded project"))
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    common = dict(
        engagement_id=project.id,
        turn_id="turn",
        provider=None,
        model="small",
        allow_empty=True,
    )
    ordinary = runtime.chat_components(**common)
    settings = runtime.chat_components(**common, settings_assistant=True)
    assert not BUTLER_TOOL_NAMES.intersection(ordinary.specs)
    assert set(settings.specs) == BUTLER_TOOL_NAMES | {
        "workspace.read",
        "workspace.search",
        "tool_output.read",
        "tool_output.search",
    }


def test_control_session_keeps_its_identity_and_accepts_selected_assistant_tools():
    message = ChatRequestMessage(role=ChatRole.USER, content="Show project policy")
    settings = ChatCompletionRequest(
        provider_id="provider",
        model="small",
        control_assistant=True,
        messages=[message],
        include_knowledge=True,
    )
    scoped = isolate_settings_assistant_request(settings)
    assert scoped.include_knowledge is True
    assert scoped.allow_mcp_catalog is True
    assert (
        isolate_settings_assistant_request(
            settings.model_copy(update={"tools_enabled": True})
        ).tools_enabled
        is True
    )
    saved = ChatSession(
        engagement_id="project",
        title="Nebula assistant",
        provider_profile_id="provider",
        model="small",
        metadata={"control_assistant": True},
    )
    with pytest.raises(ChatHistoryConflict):
        isolate_settings_assistant_request(
            settings.model_copy(update={"control_assistant": False}), saved
        )
    with pytest.raises(ChatHistoryConflict):
        isolate_settings_assistant_request(
            settings, saved.model_copy(update={"metadata": {}})
        )


def test_default_model_change_requires_advertised_value_and_reads_back(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    provider = store.create(
        ProviderProfile(
            name="Local model",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["small", "large"],
        )
    )
    harness = store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/usr/bin/true",
            capabilities=HarnessCapabilities(models=["model-a", "model-b"]),
        )
    )
    choices = model_choices(store)
    assert choices["providers"][0]["models"] == ["small", "large"]
    assert choices["harnesses"][0]["models"] == ["model-a", "model-b"]
    assert (
        set_default_model(
            store,
            {
                "kind": "provider",
                "profile_id": provider.id,
                "model": "large",
            },
        )["default_model"]
        == "large"
    )
    assert (
        set_default_model(
            store,
            {
                "kind": "harness",
                "profile_id": harness.id,
                "model": "model-b",
            },
        )["default_model"]
        == "model-b"
    )
    with pytest.raises(ValueError):
        set_default_model(
            store,
            {
                "kind": "provider",
                "profile_id": provider.id,
                "model": "unlisted",
            },
        )
    assert store.get(ProviderProfile, provider.id).metadata["default_model"] == "large"


def test_control_session_lookup_is_authenticated_and_project_scoped(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    provider = store.create(
        ProviderProfile(name="Local", provider_type="ollama", is_local=True)
    )
    ordinary = store.create(
        ChatSession(
            engagement_id=first.id,
            title="Ordinary",
            provider_profile_id=provider.id,
            model="small",
        )
    )
    control = store.create(
        ChatSession(
            engagement_id=first.id,
            title="Nebula assistant",
            provider_profile_id=provider.id,
            model="small",
            metadata={"control_assistant": True},
        )
    )
    store.create(
        ChatSession(
            engagement_id=second.id,
            title="Other project",
            provider_profile_id=provider.id,
            model="small",
            metadata={"control_assistant": True},
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    url = f"/api/v1/chat/control-assistant/session?engagement_id={first.id}"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == control.id
    assert response.json()["id"] != ordinary.id
