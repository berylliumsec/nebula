"""Core-backed runtime/catalog administration; host and vendor I/O stays inert."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    HarnessProfile,
    ProviderProfile,
    WebSearchSettings,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from ui.tests.fixtures.butler_runtime_settings_core import (
    InertSearchRuntime,
    MetadataHarnessAdapter,
    metadata_local_services,
    public_directory_transport,
)


@pytest.fixture
async def admin(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = await store.create(Engagement(name="Selected metadata project"))
    foreign = await store.create(Engagement(name="Other metadata project"))
    platform = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path
    )
    search = InertSearchRuntime(store=store, data_root=tmp_path)
    platform.search_runtime = search
    monkeypatch.setattr(
        "nebula.v3.api._discover_local_provider_services", metadata_local_services
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        tool_platform=platform,
        auth_token="fixture-token",
        openrouter_directory_transport=public_directory_transport(),
    )
    app.state.harness_runtime_service.adapter_factory = lambda kind: (
        MetadataHarnessAdapter()
    )
    return (
        store,
        project,
        foreign,
        platform,
        search,
        app,
        SettingsAssistantApi(app, "fixture-token"),
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_search_lifecycle_records_canonical_fields_for_new_and_existing_settings(
    admin, existing
):
    store, _, _, _, runtime, _, _ = admin
    if existing:
        (await runtime.record(image_digest="sha256:" + "b" * 64))

    async def journey():
        port = await runtime.ensure_ready()
        ready = await store.get(WebSearchSettings, "web-search")
        assert ready.image_digest == "sha256:" + ("b" if existing else "a") * 64
        assert ready.container_state == "ready" and ready.port == port
        await runtime.stop()
        assert (
            await store.get(WebSearchSettings, ready.id)
        ).container_state == "stopped"
        await runtime.remove()
        assert (
            await store.get(WebSearchSettings, ready.id)
        ).container_state == "absent"

    (await journey())


@pytest.mark.parametrize("operation", ["stop", "remove"])
async def test_search_command_can_record_first_known_container_state(admin, operation):
    store, _, _, _, runtime, _, _ = admin
    runtime.phase = "ready"
    (await getattr(runtime, operation)())
    assert (await store.get(WebSearchSettings, "web-search")).container_state == (
        "stopped" if operation == "stop" else "absent"
    )


async def test_butler_web_search_actions_read_current_state_and_refresh_the_real_owner(
    admin,
):
    store, project, _, _, runtime, _, gateway = admin

    async def journey():
        for method, suffix, body, state in [
            ("POST", "/install", None, "absent"),
            ("POST", "/start", None, "ready"),
            ("POST", "/test", None, "ready"),
            ("PUT", "/engines", {"engines": ["wikipedia"]}, "ready"),
            ("POST", "/stop", None, "stopped"),
            ("DELETE", "", None, "absent"),
        ]:
            saved = await gateway.request(
                project.id,
                method=method,
                path="integrations/web-search" + suffix,
                body=body,
            )
            assert saved["read_back"] and saved["result"]["container_state"] == state
            assert saved["client_action"] == {
                "kind": "runtime_settings_refresh",
                "project_id": project.id,
                "resource": "web-search",
            }
            assert saved["operation_accepted"] is False
        final = await store.get(WebSearchSettings, "web-search")
        assert final.image_digest == "sha256:" + "a" * 64
        assert final.engines == ["wikipedia"]
        assert final.last_test.ok is True
        assert any(call[0] == "run" for call in runtime.calls)

    (await journey())


@pytest.mark.parametrize("action,command", [("stop", "stop"), ("remove", "rm")])
async def test_butler_search_failure_keeps_observed_state_and_retry_is_explicit(
    admin, action, command
):
    store, project, _, _, runtime, _, gateway = admin

    async def journey():
        await runtime.ensure_ready()
        before = await store.get(WebSearchSettings, "web-search")
        runtime.exit_codes[command] = 1
        method, path = (
            ("DELETE", "integrations/web-search")
            if action == "remove"
            else ("POST", "integrations/web-search/stop")
        )
        with pytest.raises(ValueError, match="503"):
            await gateway.request(project.id, method=method, path=path)
        assert (await store.get(WebSearchSettings, before.id)) == before
        observed = await gateway.request(
            project.id, method="GET", path="integrations/web-search"
        )
        assert observed["result"]["container_state"] == "ready"
        runtime.exit_codes.clear()
        retried = await gateway.request(project.id, method=method, path=path)
        assert retried["read_back"] and retried["result"]["container_state"] == (
            "absent" if action == "remove" else "stopped"
        )

    (await journey())


async def test_butler_operator_profile_lifecycle_uses_exact_readback_and_current_revisions(
    admin,
):
    _, project, _, _, _, _, gateway = admin

    async def journey():
        first = await gateway.request(
            project.id,
            method="POST",
            path="operator-profiles",
            body={"display_name": "First operator"},
        )
        assert first["read_back"] and first["result"]["active"] is True
        created = await gateway.request(
            project.id,
            method="POST",
            path="operator-profiles",
            body={"display_name": "Metadata operator"},
        )
        profile = created["result"]
        assert created["read_back"] and profile["active"] is False
        path = "operator-profiles/" + profile["id"]
        with pytest.raises(ValueError, match="body.expected_revision"):
            await gateway.request(
                project.id, method="PATCH", path=path, body={"display_name": "Unfenced"}
            )
        saved = await gateway.request(
            project.id,
            method="PATCH",
            path=path,
            body={
                "display_name": "Renamed operator",
                "expected_revision": profile["revision"],
            },
        )
        assert (
            saved["read_back"] and saved["result"]["display_name"] == "Renamed operator"
        )
        assert saved["client_action"]["resource"] == "operator-profiles"
        with pytest.raises(ValueError, match="409"):
            await gateway.request(
                project.id,
                method="POST",
                path=path + "/activate",
                body={"expected_revision": profile["revision"]},
            )
        removed = await gateway.request(
            project.id,
            method="DELETE",
            path=path,
            expected_revision=saved["result"]["revision"],
        )
        assert removed["read_back"] and removed["result"] == {"deleted": True}
        created = await gateway.request(
            project.id,
            method="POST",
            path="operator-profiles",
            body={"display_name": "Selected operator"},
        )
        selected = await gateway.request(
            project.id,
            method="POST",
            path="operator-profiles/" + created["result"]["id"] + "/activate",
            body={"expected_revision": created["result"]["revision"]},
        )
        assert (
            selected["read_back"]
            and selected["result"]["id"] == created["result"]["id"]
        )
        assert selected["result"]["active"] is True

    (await journey())


async def test_settings_catalog_queries_are_pinned_to_the_selected_project(admin):
    _, project, foreign, platform, _, _, gateway = admin
    for identity, name in [
        (project.id, "fixture-selected-skill"),
        (foreign.id, "fixture-foreign-skill"),
    ]:
        root = await platform.workspace_for(identity)
        skill = root / ".agents" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Synthetic fixture\n---\nInert metadata.\n"
        )
        (root / "AGENTS.md").write_text(
            "Selected" if identity == project.id else "Foreign instructions"
        )

    async def journey():
        for path in [
            "skills",
            "skills/catalog",
            "hooks",
            "project-instructions",
            "provider-catalog",
            "providers/discover-local",
            "providers/openrouter/upstream-providers",
            "runtime-usage",
        ]:
            result = await gateway.request(project.id, method="GET", path=path)
            assert result["read_back"]
            assert "fixture-foreign-skill" not in json.dumps(result)
        skills = await gateway.request(project.id, method="GET", path="skills")
        assert "fixture-selected-skill" in json.dumps(skills)
        instructions = await gateway.request(
            project.id, method="GET", path="project-instructions"
        )
        assert instructions["result"]["size_bytes"] == len("Selected")
        with pytest.raises(ValueError, match="outside"):
            await gateway.request(
                project.id, method="GET", path=f"skills?engagement_id={foreign.id}"
            )

    (await journey())


async def test_settings_health_and_account_reads_keep_the_observation_and_exact_profile(
    admin,
):
    store, project, _, _, _, _, gateway = admin
    harness = await store.create(
        HarnessProfile(
            name="Trusted fixture",
            kind="codex_app_server",
            executable="/fixture/not-executed",
            enabled=False,
        )
    )
    provider = await store.create(
        ProviderProfile(name="Disabled fixture", provider_type="vllm", enabled=False)
    )
    credits = await store.create(
        ProviderProfile(
            name="Disabled credits", provider_type="openrouter", enabled=False
        )
    )

    async def journey():
        for family, profile, healthy in [
            ("harnesses", harness, True),
            ("providers", provider, False),
        ]:
            result = await gateway.request(
                project.id, method="POST", path=f"{family}/{profile.id}/health"
            )
            assert (
                result["read_back"] and result["observed_settings"]["id"] == profile.id
            )
            assert result["result"]["healthy"] is healthy
            assert result["client_action"]["resource"] == family
        result = await gateway.request(
            project.id,
            method="POST",
            path=f"providers/{credits.id}/openrouter/credits/refresh",
        )
        assert result["read_back"] and result["result"]["status"] == "unavailable"
        assert result["result"]["reason_code"] == "provider_disabled"
        assert "client_action" not in result

    (await journey())


pytestmark = pytest.mark.anyio
