"""Core-owned fixed runtime setup through Butler's authenticated gateway."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json

import httpx
import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_administration import ButlerAdministration
from nebula.v3.container_terminal import (
    ContainerTerminalService,
    ContainerTerminalPreflightRequest,
    ContainerTerminalStartRequest,
)
from nebula.v3.tools import GuidedToolError
from ui.tests.fixtures.terminal_core import SyntheticTerminalPlatform
from nebula.v3.domain import Engagement, AutomationProjectPolicy
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.sandbox import ContainerSandboxRunner
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from ui.tests.fixtures.butler_setup_core import configure_setup_io


@pytest.fixture
async def setup_core(tmp_path, monkeypatch):
    for name in ("_trusted_runtime_paths", "available", "_capture"):
        monkeypatch.setattr(
            ContainerSandboxRunner, name, getattr(ContainerSandboxRunner, name)
        )
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    selected = await store.create(Engagement(name="Selected setup project"))
    other = await store.create(Engagement(name="Other setup project"))
    platform = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path
    )
    boundary = configure_setup_io(store, platform, tmp_path)
    app = create_app(
        store,
        artifact_store=artifacts,
        tool_platform=platform,
        auth_token="setup-fixture-token",
    )
    gateway = SettingsAssistantApi(app, "setup-fixture-token")
    return store, selected, other, platform, boundary, app, gateway


async def core_request(app, method, path, body=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://nebula-core",
        headers={"Authorization": "Bearer setup-fixture-token"},
    ) as client:
        response = await client.request(method, "/api/v1/" + path, json=body)
        assert response.is_success, response.text
        return response.json()


async def select_fixed(app, gateway, project_id):
    # Core starts runner detection at startup, but ASGITransport never runs the
    # lifespan. The first status read starts it, so the refresh below awaits it
    # instead of a later status read starting a detection that clears candidates.
    await core_request(app, "GET", "setup/status")
    await gateway.request(project_id, method="POST", path="setup/runtime/refresh")
    status = await core_request(app, "GET", "setup/status")
    candidate = next(
        item for item in status["terminal"]["candidates"] if item["runtime"] == "podman"
    )
    saved = await gateway.request(
        project_id,
        method="POST",
        path="setup/runtime/select",
        body={"candidate_id": candidate["candidate_id"]},
    )
    assert saved["read_back"] is True
    assert saved["client_action"]["resource"] == "setup"
    return candidate


async def test_butler_selects_only_core_detected_fixed_candidates(setup_core):
    _, selected, _, _, _, app, gateway = setup_core

    async def journey():
        candidate = await select_fixed(app, gateway, selected.id)
        repeated = await gateway.request(
            selected.id,
            method="POST",
            path="setup/runtime/select",
            body={"candidate_id": candidate["candidate_id"]},
        )
        assert (
            repeated["read_back"] is True and repeated["result"]["idempotent"] is True
        )
        with pytest.raises(ValueError, match="422"):
            await gateway.request(
                selected.id,
                method="POST",
                path="setup/runtime/select",
                body={
                    "candidate_id": candidate["candidate_id"],
                    "executable": "/tmp/untrusted",
                },
            )

    (await journey())


@pytest.mark.parametrize("action", ["prepare", "retry"])
async def test_butler_image_preparation_cannot_be_rebound_to_another_project(
    setup_core, action
):
    _, selected, other, _, boundary, app, gateway = setup_core

    async def journey():
        await select_fixed(app, gateway, selected.id)
        with pytest.raises(ValueError, match="selected project"):
            await gateway.request(
                selected.id,
                method="POST",
                path="setup/image/" + action,
                body={"project_id": other.id},
            )
        assert boundary.prepared_projects == []

    (await journey())


async def phase(app, expected):
    for _ in range(100):
        current = await core_request(app, "GET", "setup/status")
        if current["terminal"]["image_preparation"]["phase"] == expected:
            return current
        await asyncio.sleep(0.005)
    pytest.fail(f"Core setup did not report {expected}")


@pytest.mark.parametrize("body", [None, {"project_id": None}])
async def test_butler_preparation_binds_omitted_and_null_project_to_selection(
    setup_core, body
):
    _, selected, _, _, boundary, app, gateway = setup_core

    async def journey():
        await select_fixed(app, gateway, selected.id)
        result = await gateway.request(
            selected.id, method="POST", path="setup/image/prepare", body=body
        )
        assert result["operation_accepted"] is True and result["read_back"] is True
        observed = await phase(app, "error")
        assert observed["terminal"]["image_preparation"]["project_id"] == selected.id
        assert boundary.prepared_projects == [selected.id]

    (await journey())


async def test_butler_setup_failure_retry_exact_cancel_and_readiness_are_authoritative(
    setup_core,
):
    _, selected, _, _, boundary, app, gateway = setup_core

    async def journey():
        await select_fixed(app, gateway, selected.id)
        first = await gateway.request(
            selected.id, method="POST", path="setup/image/prepare"
        )
        assert first["operation_accepted"] is True
        # Acceptance is distinct from successful completion.
        await phase(app, "error")
        retry = await gateway.request(
            selected.id, method="POST", path="setup/image/retry"
        )
        operation_id = retry["result"]["operation_id"]
        pending = await phase(app, "preparing_image")
        assert pending["terminal"]["image_preparation"]["project_id"] == selected.id
        with pytest.raises(ValueError, match="404"):
            await gateway.request(
                selected.id,
                method="POST",
                path="setup/image/cancel",
                body={"operation_id": first["result"]["operation_id"]},
            )
        unchanged = await core_request(app, "GET", "setup/status")
        assert (
            unchanged["terminal"]["image_preparation"]["operation_id"] == operation_id
        )
        assert unchanged["terminal"]["image_preparation"]["phase"] == "preparing_image"
        cancelled = await gateway.request(
            selected.id,
            method="POST",
            path="setup/image/cancel",
            body={"operation_id": operation_id},
        )
        assert cancelled["read_back"] is True
        await phase(app, "cancelled")
        await gateway.request(selected.id, method="POST", path="setup/image/retry")
        ready = await phase(app, "ready")
        assert ready["terminal"]["status"] == "ready"
        assert (
            ready["terminal"]["image_preparation"]["image_digest"]
            == "sha256:" + "b" * 64
        )
        refreshed = await gateway.request(
            selected.id, method="POST", path="setup/runtime/refresh"
        )
        assert refreshed["read_back"] is True
        assert (await core_request(app, "GET", "setup/status"))["terminal"][
            "status"
        ] == "ready"
        assert boundary.prepared_projects == [selected.id] * 3

    (await journey())


async def test_butler_retry_does_not_inherit_foreign_project_and_cannot_cancel_it(
    setup_core,
):
    _, selected, other, _, boundary, app, gateway = setup_core

    async def journey():
        await select_fixed(app, gateway, selected.id)
        await core_request(app, "POST", "setup/image/prepare", {"project_id": other.id})
        await phase(app, "error")
        foreign = await core_request(
            app, "POST", "setup/image/retry", {"project_id": other.id}
        )
        await phase(app, "preparing_image")
        with pytest.raises(ValueError, match="selected project"):
            await gateway.request(
                selected.id,
                method="POST",
                path="setup/image/cancel",
                body={"operation_id": foreign["operation_id"]},
            )
        assert (await core_request(app, "GET", "setup/status"))["terminal"][
            "image_preparation"
        ]["phase"] == "preparing_image"
        await core_request(
            app, "POST", "setup/image/cancel", {"operation_id": foreign["operation_id"]}
        )
        await phase(app, "cancelled")
        await gateway.request(selected.id, method="POST", path="setup/image/retry")
        current = await phase(app, "ready")
        assert current["terminal"]["image_preparation"]["project_id"] == selected.id
        assert boundary.prepared_projects == [other.id, other.id, selected.id]

    (await journey())


async def test_butler_shared_automation_prepare_reads_actual_cached_metadata(
    setup_core,
):
    _, selected, _, platform, _, app, gateway = setup_core

    async def journey():
        await select_fixed(app, gateway, selected.id)
        result = await gateway.request(
            selected.id, method="POST", path="automation/runtime/prepare"
        )
        assert result["read_back"] is True and result["result"]["ready"] is True
        assert result["client_action"]["resource"] == "automation-runtime"
        observed = await core_request(
            app, "GET", f"automation/runtime?engagement_id={selected.id}"
        )
        assert observed["ready"] is True and observed["digest"] == "sha256:" + "b" * 64
        assert (
            platform.last_automation_runtime_metadata()["inventory"][0]["version"]
            == "Synthetic fixture"
        )

    (await journey())


async def test_butler_setup_and_project_automation_reads_remain_closed(setup_core):
    store, selected, _, _, _, app, gateway = setup_core

    async def journey():
        observer = ButlerAdministration(store, selected.id, gateway)
        setup = await observer.system({"kind": "setup_status"})
        assert setup["read_back"] is True
        policy = await app.state.automation_runtime.project_policy(selected.id)
        (
            await store.update(
                AutomationProjectPolicy,
                policy.id,
                {"execution_mode": "host"},
                expected_revision=policy.revision,
            )
        )
        observed = await observer.system({"kind": "automation_runtime"})
        assert json.loads(observed["text"])["runner_profile_id"] == "host"
        assert json.loads(observed["text"])["ready"] is False

    (await journey())


async def test_butler_reads_only_existing_selected_project_terminal_egress(
    setup_core, tmp_path
):
    store, selected, other, platform, _, _, _ = setup_core
    terminal_platform = SyntheticTerminalPlatform(tmp_path / "synthetic-terminal")
    service = ContainerTerminalService(
        store=store, tool_platform=terminal_platform, operator_id=lambda: "fixture"
    )
    app = create_app(
        store,
        artifact_store=platform.artifact_store,
        tool_platform=platform,
        container_terminal_service=service,
        auth_token="setup-fixture-token",
    )
    gateway = SettingsAssistantApi(app, "setup-fixture-token")

    async def journey():
        request = ContainerTerminalPreflightRequest(engagement_id=selected.id)
        preview = await service.preflight(request)
        started = await service.start(
            ContainerTerminalStartRequest(
                **request.model_dump(),
                preview_token=preview.preview_token,
                preview_fingerprint=preview.preview_fingerprint,
                client_idempotency_key="synthetic-egress",
            )
        )
        await service.claim(started.session_id, started.websocket_ticket)
        await service.launch(started.session_id)
        try:
            observer = ButlerAdministration(store, selected.id, gateway)
            result = await observer.system({"kind": "terminal_public_ip"})
            assert json.loads(result["text"])["address"] == "203.0.113.42"
            assert "websocket_ticket" not in json.dumps(result)
            with pytest.raises(GuidedToolError):
                await ButlerAdministration(store, other.id, gateway).system(
                    {"kind": "terminal_public_ip"}
                )
        finally:
            await service.close(started.session_id)
        with pytest.raises(GuidedToolError):
            await observer.system({"kind": "terminal_public_ip"})

    (await journey())


pytestmark = pytest.mark.anyio
