from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatConfigurationError, ChatHistoryConflict, ChatRequestMessage, isolate_settings_assistant_request
from nebula.v3.domain import AutomationProjectPolicy, ChatRole, ChatSession, Engagement, HarnessCapabilities, HarnessKind, HarnessProfile, ProviderProfile
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from nebula.v3.settings_assistant_tools import (
    SETTING_DESTINATIONS,
    catalog,
    provider_plugins,
    project_context,
    model_choices,
    set_default_model,
    set_post_tool_preference,
)


def test_catalog_covers_settings_destinations_without_secrets():
    result = catalog()
    assert len(result["settings"]) == len(SETTING_DESTINATIONS) == 17
    assert {item["id"] for item in result["settings"]} == {
        item[0] for item in SETTING_DESTINATIONS
    }
    assert all(item["url"].startswith("/settings#") for item in result["settings"])
    assert all("credential" not in str(item.get("direct_changes", [])) for item in result["settings"])
    project_policy = next(item for item in result["settings"] if item["id"] == "network-scope")
    assert project_policy["assistant_ui_actions"] == ["project_execution_mode"]
    assert project_policy["direct_changes"] == []


def test_post_tool_change_reads_back_from_core(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Bounded project"))
    result = set_post_tool_preference(
        store, artifacts, project.id, {"setting": "take_notes", "enabled": False}
    )
    assert result == {
        "setting": "take_notes", "enabled": False, "project_id": project.id,
        "project_name": "Bounded project", "saved": True,
    }
    with pytest.raises(Exception):
        set_post_tool_preference(
            store, artifacts, project.id,
            {"setting": "take_notes", "enabled": True},
        )
    assert store.get(Engagement, project.id).metadata["post_tool_assistant"]["take_notes"] is False


def test_project_context_reads_mode_and_linked_workspace_without_creating_policy(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Bounded project"))
    other = store.create(Engagement(name="Other project"))
    store.create(AutomationProjectPolicy(engagement_id=other.id, execution_mode="host", host_access_acknowledged=True))
    default = project_context(store, project.id)
    assert default["execution_mode"] == "docker"
    assert default["policy_source"] == "default"
    assert "No execution policy is stored" in default["policy_description"]
    assert default["linked_workspace_path"] is None
    assert not store.find_entities(AutomationProjectPolicy, {"engagement_id": project.id})
    auto_default = store.create(Engagement(name="Auto default"))
    store.create(AutomationProjectPolicy(engagement_id=auto_default.id, execution_mode="docker"))
    stored_docker = project_context(store, auto_default.id)
    assert stored_docker["policy_source"] == "stored"
    assert "may be the automatically created default" in stored_docker["policy_description"]

    linked = tmp_path / "linked"
    linked.mkdir()
    project = store.update(Engagement, project.id, {"workspace_path": str(linked)}, expected_revision=project.revision)
    store.create(AutomationProjectPolicy(engagement_id=project.id, execution_mode="host", host_access_acknowledged=True))
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
    names = {plugin.spec.name for plugin in provider_plugins(store, artifacts, project.id)}
    assert names == {
        "nebula.help.search", "nebula.help.project_context",
        "nebula.settings.catalog", "nebula.settings.set_post_tool",
        "nebula.settings.models", "nebula.settings.set_default_model",
    }


def test_provider_runtime_offers_settings_tools_only_to_the_settings_assistant(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Bounded project"))
    runtime = RuntimePlatform(store=store, artifact_store=artifacts, data_root=tmp_path / "runtime")
    common = dict(engagement_id=project.id, turn_id="turn", provider=None, model="small", allow_empty=True)
    ordinary = runtime.chat_components(**common)
    settings = runtime.chat_components(**common, settings_assistant=True)
    assert not any(name.startswith(("nebula.settings.", "nebula.help.")) for name in ordinary.specs)
    assert set(settings.specs) == {
        "nebula.help.search", "nebula.help.project_context",
        "nebula.settings.catalog", "nebula.settings.set_post_tool",
        "nebula.settings.models", "nebula.settings.set_default_model",
    }


def test_settings_session_cannot_be_reused_as_general_chat_or_attach_general_tools():
    message = ChatRequestMessage(role=ChatRole.USER, content="Show project policy")
    settings = ChatCompletionRequest(
        provider_id="provider", model="small", control_assistant=True,
        messages=[message], include_knowledge=True,
    )
    scoped = isolate_settings_assistant_request(settings)
    assert scoped.include_knowledge is False
    assert scoped.allow_mcp_catalog is False
    with pytest.raises(ChatConfigurationError):
        isolate_settings_assistant_request(settings.model_copy(update={"tools_enabled": True}))
    saved = ChatSession(
        engagement_id="project", title="Nebula assistant", provider_profile_id="provider",
        model="small", metadata={"control_assistant": True},
    )
    with pytest.raises(ChatHistoryConflict):
        isolate_settings_assistant_request(settings.model_copy(update={"control_assistant": False}), saved)
    with pytest.raises(ChatHistoryConflict):
        isolate_settings_assistant_request(settings, saved.model_copy(update={"metadata": {}}))


def test_default_model_change_requires_advertised_value_and_reads_back(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    provider = store.create(ProviderProfile(
        name="Local model", provider_type="ollama", is_local=True,
        model_allowlist=["small", "large"],
    ))
    harness = store.create(HarnessProfile(
        name="Codex", kind=HarnessKind.CODEX_APP_SERVER,
        executable="/usr/bin/true",
        capabilities=HarnessCapabilities(models=["model-a", "model-b"]),
    ))
    choices = model_choices(store)
    assert choices["providers"][0]["models"] == ["small", "large"]
    assert choices["harnesses"][0]["models"] == ["model-a", "model-b"]
    assert set_default_model(store, {
        "kind": "provider", "profile_id": provider.id, "model": "large",
    })["default_model"] == "large"
    assert set_default_model(store, {
        "kind": "harness", "profile_id": harness.id, "model": "model-b",
    })["default_model"] == "model-b"
    with pytest.raises(ValueError):
        set_default_model(store, {
            "kind": "provider", "profile_id": provider.id, "model": "unlisted",
        })
    assert store.get(ProviderProfile, provider.id).metadata["default_model"] == "large"


def test_control_session_lookup_is_authenticated_and_project_scoped(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    provider = store.create(ProviderProfile(name="Local", provider_type="ollama", is_local=True))
    ordinary = store.create(ChatSession(
        engagement_id=first.id, title="Ordinary", provider_profile_id=provider.id, model="small",
    ))
    control = store.create(ChatSession(
        engagement_id=first.id, title="Nebula assistant", provider_profile_id=provider.id,
        model="small", metadata={"control_assistant": True},
    ))
    store.create(ChatSession(
        engagement_id=second.id, title="Other project", provider_profile_id=provider.id,
        model="small", metadata={"control_assistant": True},
    ))
    client = TestClient(create_app(store, auth_token="test-token"))
    url = f"/api/v1/chat/control-assistant/session?engagement_id={first.id}"
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == control.id
    assert response.json()["id"] != ordinary.id
