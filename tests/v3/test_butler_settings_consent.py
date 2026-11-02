"""Program trust and project cloud consent remain with the requesting operator."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Engagement, HarnessProfile
from nebula.v3.execution_ai import ExecutionAIService, PostToolAssistantConfig
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.settings_assistant_tools import set_post_tool_preference
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.tools import GuidedToolError


@pytest.fixture
def consent_settings(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = store.create(Engagement(name="Consent fixture"))
    app = create_app(store, artifact_store=artifacts, auth_token="fixture-token")
    return store, artifacts, project, app, SettingsAssistantApi(app, "fixture-token")


@pytest.mark.parametrize("enabled", [None, False, True])
def test_butler_cannot_create_spawned_harness_even_when_disabled(
    consent_settings, enabled
):
    store, _, project, _, gateway = consent_settings
    body = {
        "name": "Untrusted program",
        "kind": "codex_app_server",
        "executable": "/fixture/not-executed",
    }
    if enabled is not None:
        body["enabled"] = enabled
    with pytest.raises(GuidedToolError) as refusal:
        asyncio.run(
            gateway.request(project.id, method="POST", path="harnesses", body=body)
        )
    assert refusal.value.code == "harness_trust_required"
    assert "setting_id=harnesses" in refusal.value.next_action
    assert store.list_entities(HarnessProfile) == []


@pytest.mark.parametrize(
    "method,changes",
    [
        ("PATCH", {"executable": "/other/not-executed"}),
        ("PATCH", {"home_directory": "/other/account"}),
        (
            "PATCH",
            {
                "connection_mode": "endpoint",
                "transport": "websocket",
                "endpoint": "ws://127.0.0.1:9999",
                "executable": None,
                "home_directory": None,
            },
        ),
        ("PUT", {"home_directory": None}),
    ],
)
def test_butler_preserves_existing_harness_process_configuration(
    consent_settings, method, changes
):
    store, _, project, _, gateway = consent_settings
    profile = store.create(
        HarnessProfile(
            name="Trusted program",
            kind="codex_app_server",
            executable="/fixture/not-executed",
            home_directory="/fixture/account",
            enabled=False,
        )
    )
    if method == "PATCH":
        body = {"changes": changes, "expected_revision": profile.revision}
    else:
        body = {**profile.model_dump(mode="json"), **changes}
    with pytest.raises(GuidedToolError) as refusal:
        asyncio.run(
            gateway.request(
                project.id,
                method=method,
                path=f"harnesses/{profile.id}",
                body=body,
                expected_revision=profile.revision if method == "PUT" else None,
            )
        )
    assert refusal.value.code == "harness_trust_required"
    assert store.get(HarnessProfile, profile.id) == profile


def test_butler_can_rename_an_existing_spawned_harness_without_changing_trust(
    consent_settings,
):
    store, _, project, _, gateway = consent_settings
    profile = store.create(
        HarnessProfile(
            name="Trusted program",
            kind="codex_app_server",
            executable="/fixture/not-executed",
            home_directory="/fixture/account",
            enabled=False,
        )
    )
    saved = asyncio.run(
        gateway.request(
            project.id,
            method="PATCH",
            path=f"harnesses/{profile.id}",
            body={
                "expected_revision": profile.revision,
                "changes": {"name": "Renamed trusted program"},
            },
        )
    )
    assert saved["read_back"] and saved["result"]["name"] == "Renamed trusted program"
    assert saved["result"]["executable"] == profile.executable
    with pytest.raises(ValueError, match="requesting browser"):
        asyncio.run(
            gateway.request(
                project.id,
                method="PATCH",
                path=f"harnesses/{profile.id}",
                body={
                    "expected_revision": saved["result"]["revision"],
                    "changes": {"permission_mode": "unrestricted"},
                },
            )
        )


@pytest.mark.parametrize(
    "previous,next_config",
    [
        ({}, {"cloud_confirmed": True}),
        ({}, {"cloud_confirmed": "yes"}),
        (
            {"cloud_confirmed": True, "provider_id": "first"},
            {"cloud_confirmed": True, "provider_id": "second"},
        ),
        (
            {"cloud_confirmed": True, "provider_id": "first"},
            {
                "cloud_confirmed": True,
                "backend_kind": "harness",
                "harness_profile_id": "first",
            },
        ),
        (
            {
                "cloud_confirmed": True,
                "backend_kind": "harness",
                "harness_profile_id": "first",
            },
            {
                "cloud_confirmed": True,
                "backend_kind": "harness",
                "harness_profile_id": "second",
            },
        ),
    ],
)
def test_butler_cannot_grant_or_transfer_post_tool_cloud_consent(
    consent_settings, previous, next_config
):
    _, _, project, app, gateway = consent_settings
    service = app.state.execution_ai_service
    service.set_config(project.id, PostToolAssistantConfig(**previous))
    before = service.read_config(project.id)
    body = {**next_config, "expected_revision": before.revision}
    with pytest.raises(GuidedToolError) as refusal:
        asyncio.run(
            gateway.request(
                project.id,
                method="PUT",
                path="engagements/{project}/post-tool-assistant",
                body=body,
            )
        )
    assert refusal.value.code == "post_tool_consent_required"
    assert "setting_id=follow-up" in refusal.value.next_action
    assert service.read_config(project.id) == before


@pytest.mark.parametrize("consent", [False, True])
def test_butler_preserves_or_revokes_existing_post_tool_consent_at_observed_revision(
    consent_settings, consent
):
    _, _, project, app, gateway = consent_settings
    service = app.state.execution_ai_service
    config = PostToolAssistantConfig(
        cloud_confirmed=True, provider_id="first", model="fixture-model"
    )
    service.set_config(project.id, config)
    before = service.read_config(project.id)
    body = {
        **config.model_dump(mode="json"),
        "cloud_confirmed": consent,
        "model": "another-model",
        "expected_revision": before.revision,
    }
    saved = asyncio.run(
        gateway.request(
            project.id,
            method="PUT",
            path="engagements/{project}/post-tool-assistant",
            body=body,
        )
    )
    assert saved["read_back"] and saved["state"] == "core_settings_saved"
    assert saved["result"]["cloud_confirmed"] is consent
    assert saved["result"]["model"] == "another-model"
    assert saved["client_action"] == {
        "kind": "post_tool_refresh",
        "project_id": project.id,
    }
    with pytest.raises(ValueError, match="409"):
        asyncio.run(
            gateway.request(
                project.id,
                method="PUT",
                path="engagements/{project}/post-tool-assistant",
                body=body,
            )
        )
    body.pop("expected_revision")
    with pytest.raises(ValueError, match="body.expected_revision"):
        asyncio.run(
            gateway.request(
                project.id,
                method="PUT",
                path="engagements/{project}/post-tool-assistant",
                body=body,
            )
        )


def test_core_post_tool_update_cannot_restore_consent_at_a_stale_revision(
    consent_settings,
):
    _, _, project, app, _ = consent_settings
    service = app.state.execution_ai_service
    config = PostToolAssistantConfig(provider_id="first", cloud_confirmed=True)
    before = service.read_config(project.id)
    with TestClient(app) as client:
        granted = client.put(
            f"/api/v1/engagements/{project.id}/post-tool-assistant",
            json={
                **config.model_dump(mode="json"),
                "expected_revision": before.revision,
            },
            headers={"Authorization": "Bearer fixture-token"},
        )
        assert granted.status_code == 200
        assert service.get_config(project.id).cloud_confirmed is True
        before = service.read_config(project.id)
        service.set_config(
            project.id, config.model_copy(update={"cloud_confirmed": False})
        )
        response = client.put(
            f"/api/v1/engagements/{project.id}/post-tool-assistant",
            json={
                **config.model_dump(mode="json"),
                "expected_revision": before.revision,
            },
            headers={"Authorization": "Bearer fixture-token"},
        )
    assert response.status_code == 409
    assert service.get_config(project.id).cloud_confirmed is False


def test_direct_post_tool_preference_preserves_operator_consent_during_a_race(
    consent_settings, monkeypatch
):
    store, artifacts, project, app, _ = consent_settings
    service = app.state.execution_ai_service
    config = PostToolAssistantConfig(
        provider_id="first", model="fixture-model", cloud_confirmed=True
    )
    service.set_config(project.id, config)
    get_config = ExecutionAIService.get_config

    def revoke_after_read(instance, identity):
        observed = get_config(instance, identity)
        service.set_config(
            identity, observed.model_copy(update={"cloud_confirmed": False})
        )
        return observed

    monkeypatch.setattr(ExecutionAIService, "get_config", revoke_after_read)
    with pytest.raises(ConflictError):
        set_post_tool_preference(
            store, artifacts, project.id, {"setting": "take_notes", "enabled": True}
        )
    assert service.read_config(project.id).cloud_confirmed is False
