"""Runtime metadata changes use revisions without granting host trust or consent."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)


import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, RunnerProfile
from nebula.v3.environments import SshEnvironmentService, SshEnvironmentSettings
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.tools import GuidedToolError


@pytest.fixture
async def runtime_settings(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Runtime metadata fixture"))
    runner = await store.create(
        RunnerProfile(
            id="fixture-runner",
            name="Trusted fixture",
            runtime="podman",
            executable="/fixture/podman",
            context="fixture",
            socket="/fixture/podman.sock",
            platform="linux/amd64",
            isolation="rootless",
            seccomp_profile="/fixture/seccomp.json",
        )
    )
    config = tmp_path / "ssh-config"
    config.write_text(
        "Host fixture-ask\n    HostName example.invalid\nHost fixture-allow\n    HostName example.invalid\nHost fixture-new\n    HostName example.invalid\n"
    )
    service = SshEnvironmentService(store, config_path=config)
    (
        await service.save(
            "fixture-ask", SshEnvironmentSettings(display_name="Ask fixture")
        )
    )
    (
        await service.save(
            "fixture-allow", SshEnvironmentSettings(command_approval="allow")
        )
    )
    app = create_app(store, auth_token="fixture-token", ssh_environment_service=service)
    gateway = SettingsAssistantApi(app, "fixture-token")
    return store, project, runner, service, app, gateway


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime", "docker"),
        ("executable", "/untrusted/podman"),
        ("context", "other"),
        ("socket", "/other/podman.sock"),
        ("platform", "linux/arm64"),
        ("isolation", "docker_desktop_vm"),
        ("seccomp_profile", "/other/seccomp.json"),
        ("create", None),
        ("omit-context", None),
        ("omit-socket", None),
        ("omit-seccomp_profile", None),
    ],
)
async def test_butler_cannot_select_runner_trust(runtime_settings, field, value):
    store, project, runner, _, _, gateway = runtime_settings
    payload = {
        key: runner.model_dump(mode="json")[key]
        for key in (
            "name",
            "runtime",
            "executable",
            "context",
            "socket",
            "platform",
            "isolation",
            "enabled",
            "seccomp_profile",
        )
    }
    payload["expected_revision"] = runner.revision
    identity = runner.id
    if field == "create":
        identity = "new-fixture"
    elif field.startswith("omit-"):
        payload.pop(field.removeprefix("omit-"))
    else:
        payload[field] = value
    with pytest.raises(GuidedToolError) as refusal:
        (
            await gateway.request(
                project.id,
                method="PUT",
                path=f"runner-profiles/{identity}",
                body=payload,
            )
        )
    assert refusal.value.code == "runner_trust_required"
    assert "setting_id=runners" in refusal.value.next_action
    assert (await store.get(RunnerProfile, runner.id)) == runner
    assert len((await store.list_entities(RunnerProfile))) == 1


async def test_butler_renames_trusted_runner_and_reads_exact_saved_profile(
    runtime_settings,
):
    store, project, runner, _, app, gateway = runtime_settings
    payload = {
        key: runner.model_dump(mode="json")[key]
        for key in (
            "name",
            "runtime",
            "executable",
            "context",
            "socket",
            "platform",
            "isolation",
            "enabled",
            "seccomp_profile",
        )
    }
    payload["name"] = "Renamed trusted fixture"
    with pytest.raises(ValueError, match="body.expected_revision"):
        (
            await gateway.request(
                project.id,
                method="PUT",
                path=f"runner-profiles/{runner.id}",
                body=payload,
                expected_revision=runner.revision,
            )
        )
    payload["expected_revision"] = runner.revision
    saved = await gateway.request(
        project.id, method="PUT", path=f"runner-profiles/{runner.id}", body=payload
    )
    assert saved["read_back"] and saved["state"] == "core_settings_saved"
    assert saved["result"]["id"] == runner.id
    assert saved["result"]["name"] == payload["name"]
    assert saved["client_action"]["resource"] == "runner-profiles"
    with pytest.raises(ValueError, match="409"):
        (
            await gateway.request(
                project.id,
                method="PUT",
                path=f"runner-profiles/{runner.id}",
                body=payload,
            )
        )
    (await store.delete(RunnerProfile, runner.id))
    async with TestClient(app) as client:
        response = await client.put(
            f"/api/v1/runner-profiles/{runner.id}",
            json=payload,
            headers={"Authorization": "Bearer fixture-token"},
        )
    assert response.status_code == 409
    assert (await store.list_entities(RunnerProfile)) == []


@pytest.mark.parametrize("alias", ["fixture-ask", "fixture-new"])
async def test_butler_cannot_grant_first_standing_ssh_approval(runtime_settings, alias):
    _, project, _, service, _, gateway = runtime_settings
    before = await service.get(alias)
    payload = {"command_approval": "allow"}
    if before:
        payload["expected_revision"] = before.revision
    with pytest.raises(GuidedToolError) as refusal:
        (
            await gateway.request(
                project.id, method="PUT", path=f"ssh-environments/{alias}", body=payload
            )
        )
    assert refusal.value.code == "ssh_approval_required"
    assert "setting_id=ssh-environments" in refusal.value.next_action
    assert (await service.get(alias)) == before


@pytest.mark.parametrize(
    "alias,approval",
    [
        ("fixture-ask", "ask"),
        ("fixture-allow", "allow"),
        ("fixture-allow", "ask"),
        ("fixture-new", "ask"),
    ],
)
async def test_butler_edits_ssh_metadata_preserving_or_tightening_approval(
    runtime_settings, alias, approval
):
    _, project, _, service, _, gateway = runtime_settings
    before = await service.get(alias)
    payload = {
        "display_name": "Saved metadata",
        "notes": "Synthetic notes",
        "command_approval": approval,
    }
    if before:
        payload["expected_revision"] = before.revision
    saved = await gateway.request(
        project.id, method="PUT", path=f"ssh-environments/{alias}", body=payload
    )
    assert saved["read_back"] and saved["state"] == "core_settings_saved"
    assert saved["result"]["alias"] == alias
    assert saved["result"]["command_approval"] == approval
    assert saved["result"]["display_name"] == "Saved metadata"
    assert saved["client_action"]["resource"] == "ssh-environments"
    revision = saved["result"]["revision"]
    with pytest.raises(ValueError, match="expected_revision"):
        (
            await gateway.request(
                project.id,
                method="PUT",
                path=f"ssh-environments/{alias}",
                body={"notes": "stale"},
            )
        )
    with pytest.raises(ValueError, match="expected_revision"):
        (
            await gateway.request(
                project.id, method="DELETE", path=f"ssh-environments/{alias}"
            )
        )
    with pytest.raises(ValueError, match="409"):
        (
            await gateway.request(
                project.id,
                method="DELETE",
                path=f"ssh-environments/{alias}",
                expected_revision=revision + 1,
            )
        )
    assert (await service.get(alias)).notes == "Synthetic notes"
    removed = await gateway.request(
        project.id,
        method="DELETE",
        path=f"ssh-environments/{alias}",
        expected_revision=revision,
    )
    assert removed["read_back"] and removed["result"] == {"deleted": True}
    assert (await service.get(alias)) is None


async def test_ssh_absence_and_existing_revision_preconditions_are_not_interchangeable(
    runtime_settings,
):
    _, _, _, service, _, _ = runtime_settings
    with pytest.raises(ConflictError):
        (
            await service.save(
                "fixture-ask",
                SshEnvironmentSettings(expected_absent=True, notes="overwrite"),
            )
        )
    (await service.forget("fixture-ask"))
    with pytest.raises(ConflictError):
        (
            await service.save(
                "fixture-ask",
                SshEnvironmentSettings(expected_revision=1, notes="resurrect"),
            )
        )
    assert (await service.get("fixture-ask")) is None


async def test_browser_operator_can_grant_ssh_approval(runtime_settings):
    _, _, _, service, app, _ = runtime_settings
    async with TestClient(app) as client:
        response = await client.put(
            "/api/v1/ssh-environments/fixture-new",
            json={"command_approval": "allow"},
            headers={"Authorization": "Bearer fixture-token"},
        )
    assert response.status_code == 200
    assert (await service.get("fixture-new")).command_approval == "allow"


pytestmark = pytest.mark.anyio
