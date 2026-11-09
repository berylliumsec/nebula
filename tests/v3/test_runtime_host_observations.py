"""Real host Settings routes with inert host observations and durable saves."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from nebula.v3.database import RuntimeCommandRow
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.domain import (
    RunnerProfile,
    ToolSuggestionSettings,
    VpnProfile,
    WebSearchRuntimeState,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.host_load import HostLoadGate
from nebula.v3.host_pressure import HostPressureSettings
from nebula.v3.runtime_coordination import ProjectLease
from nebula.v3.runtime_host_pressure import HOST_PRESSURE, RemoteHostLoadGate
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_prepared_catalog import automation_runtime_metadata
from nebula.v3.sandbox import PreparedContainerImage
from sqlalchemy import func, select

from tests.v3.test_host_load import ManualMonitor
from tests.v3.test_runtime_host_services import peer
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_cpu_save_readback_replay_and_honest_inactive_executor(settings):
    observations = settings.host.observations
    observations.gate.monitor = ManualMonitor(busy=False)
    before = (await settings.client.get("/api/v1/host-cpu-budget")).json()
    assert before["apply_state"] == "inactive" and not before["activation_supported"]
    body = {
        "host_id": before["host_id"],
        "expected_revision": before["revision"],
        "cpu_limit": 2.0,
    }
    changed = await settings.mutate(
        "host-cpu-budget", method="PUT", key="cpu", body=body
    )
    assert changed.status_code == 200, changed.text
    assert (
        changed.json()["cpu_limit"] == 2.0
        and changed.json()["apply_state"] == "inactive"
    )
    assert (
        await settings.mutate("host-cpu-budget", method="PUT", key="cpu", body=body)
    ).json() == changed.json()
    assert (
        await settings.mutate(
            "host-cpu-budget", method="PUT", key="cpu-stale", body=body
        )
    ).status_code == 409
    assert (await settings.client.get("/api/v1/host-cpu-budget")).json()[
        "revision"
    ] == 2
    refused = await settings.mutate(
        "host-cpu-budget",
        method="PUT",
        key="cpu-enable",
        body={**body, "expected_revision": 2, "enabled": True},
    )
    assert refused.status_code == 422 and "unavailable" in refused.text


async def test_saved_pressure_reaches_exact_worker_grants_and_cache_expires(settings):
    observations = settings.host.observations
    observations.gate.monitor = ManualMonitor(busy=False)
    await settings.managed.stage("pressure-v1")
    identity = await settings.managed.host.spawn("pressure-v1", "execution")
    lease = await settings.root.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    client = (await peer(settings.managed, identity)).client
    remote = RemoteHostLoadGate(client)
    try:
        before = (await settings.client.get("/api/v1/host-pressure")).json()
        body = {
            "host_id": before["host_id"],
            "expected_revision": before["revision"],
            "thresholds": {**before["saved_thresholds"], "cpu": 0.05},
        }
        saved = await settings.mutate(
            "host-pressure", method="PUT", key="pressure", body=body
        )
        assert saved.status_code == 200 and saved.json()["waiting"], saved.text
        assert saved.json()["saved_thresholds"] == saved.json()["effective_thresholds"]
        with execution_context(lease):
            assert (await remote.snapshot()).busy
            updated = await settings.mutate(
                "host-pressure",
                method="PUT",
                key="pressure-relax",
                body={
                    **body,
                    "expected_revision": 2,
                    "thresholds": before["saved_thresholds"],
                },
            )
            assert updated.status_code == 200 and not updated.json()["waiting"]
            assert (await remote.snapshot()).busy
            remote._cached_at = 0
            assert not (await remote.snapshot()).busy
        stale = ProjectLease(
            lease.project_id, lease.runtime_id, lease.fencing_token + 1
        )
        with pytest.raises(RuntimeIpcError):
            await client.call(HOST_PRESSURE, asdict(stale))
        restored = HostPressureSettings(
            observations.pressure.path, HostLoadGate(observations.gate.monitor)
        )
        await restored.startup()
        assert (await restored.status())["revision"] == 3
    finally:
        await client.close()


async def test_butler_file_and_interval_roundtrip_revision_and_replay(settings):
    before = (await settings.client.get("/api/v1/butler/instructions")).json()
    assert before["status"] == "missing"
    body = {"content": "Synthetic shared Butler mandate.\n", "expected_sha256": None}
    saved = await settings.mutate(
        "butler/instructions", method="PUT", key="mandate", body=body
    )
    assert saved.status_code == 200 and saved.json()["status"] == "ready", saved.text
    assert (settings.managed.data / ".butler" / "BUTLER.md").read_text() == body[
        "content"
    ]
    assert (
        await settings.mutate(
            "butler/instructions", method="PUT", key="mandate", body=body
        )
    ).json() == saved.json()
    assert (
        await settings.mutate(
            "butler/instructions", method="PUT", key="mandate-stale", body=body
        )
    ).status_code == 409
    current = await settings.client.get("/api/v1/butler/check-interval")
    interval = {
        "check_interval_minutes": 17,
        "expected_revision": current.json()["revision"],
    }
    changed = await settings.mutate(
        "butler/check-interval", method="PUT", key="interval", body=interval
    )
    assert changed.status_code == 200 and changed.json()["check_interval_minutes"] == 17
    assert (
        await settings.client.get("/api/v1/butler/check-interval")
    ).json() == changed.json()


async def test_diagnostic_settings_files_errors_and_direct_bounded_telemetry(settings):
    observations = settings.host.observations
    manager = DiagnosticManager(settings.managed.data, watch_settings=False)
    observations.diagnostics = manager
    try:
        batch = {
            "events": [
                {
                    "level": "error",
                    "event_code": "interface.synthetic.error",
                    "message": "Synthetic browser error",
                    "metadata": {"synthetic": True},
                }
            ]
        }
        path = "/api/v1/diagnostics/events"
        assert (await settings.client.post(path, json=batch)).status_code == 403
        observations.allow_browser_diagnostics = True
        async with settings.managed.database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(RuntimeCommandRow)
            )
        accepted = await settings.client.post(path, json=batch)
        assert accepted.status_code == 200 and accepted.json()["accepted"] == 1, (
            accepted.text
        )
        async with settings.managed.database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(RuntimeCommandRow)
                )
                == count
            )
        errors = await settings.client.get(
            "/api/v1/diagnostics/errors", params={"feature": "interface", "limit": 1}
        )
        assert (
            errors.status_code == 200
            and errors.json()["errors"][0]["error_id"]
            == accepted.json()["error_ids"][0]
        )
        assert (await settings.client.get("/api/v1/diagnostics/files")).json()["files"]
        previous = (await settings.client.get("/api/v1/diagnostics/settings")).json()
        saved = await settings.mutate(
            "diagnostics/settings",
            method="PUT",
            key="diagnostics",
            body={**previous, "global_level": "debug"},
        )
        assert saved.status_code == 200 and saved.json()["global_level"] == "debug", (
            saved.text
        )
        assert (
            await settings.client.get("/api/v1/diagnostics/settings")
        ).json() == saved.json()
        assert (
            await settings.client.post(path, content=b"x" * (128 * 1024 + 1))
        ).status_code == 413
        assert (
            await settings.client.get("/api/v1/diagnostics/errors?limit=501")
        ).status_code == 422
    finally:
        await asyncio.to_thread(manager.close)


async def test_shared_automation_image_prepare_replay_and_changed_runner(settings):
    runtime = settings.host.observations.automation
    assert not (await settings.client.get("/api/v1/automation/runtime")).json()["ready"]
    profile = await settings.store.create(
        RunnerProfile(
            id="local",
            name="Inert runner",
            runtime="podman",
            executable="/inert/podman",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )
    )
    calls = []

    async def available():
        assert settings.managed.database.task_session() is None
        return True, "Synthetic verified runner"

    runtime.runner_factory = lambda _: SimpleNamespace(available=available)
    digest = "sha256:" + "a" * 64

    class Preparer:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def prepare(self):
            return PreparedContainerImage(
                source_reference="docker.io/kalilinux/kali-rolling:latest",
                base_resolved_reference="base@" + digest,
                base_digest=digest,
                resolved_reference="derived@" + digest,
                digest=digest,
                platform="linux/amd64",
                configured_user="65532",
                installed_packages=(),
                refreshed=False,
                detail="Synthetic image",
                binary_inventory=(("bash", "/bin/bash", "5.2"),),
            )

    runtime.preparer_factory = Preparer
    prepared = await settings.mutate("automation/runtime/prepare", key="prepare")
    assert prepared.status_code == 200 and prepared.json()["ready"], prepared.text
    assert (
        await settings.mutate("automation/runtime/prepare", key="prepare")
    ).json() == prepared.json() and len(calls) == 1
    assert automation_runtime_metadata(settings.managed.data)["digest"] == digest
    assert (await settings.client.get("/api/v1/automation/runtime")).json()["ready"]
    await settings.store.update(
        RunnerProfile,
        profile.id,
        {"executable": "/inert/changed/podman"},
        expected_revision=profile.revision,
    )
    assert not (await settings.client.get("/api/v1/automation/runtime")).json()["ready"]


async def test_host_integration_reads_observe_canonical_credentials_and_search(
    settings,
):
    saved = await settings.mutate(
        "credentials",
        key="integration-secret",
        body={"secret": "synthetic-integrations-secret", "persistence": "session"},
    )
    reference = saved.json()["reference"]
    await settings.store.create(
        ToolSuggestionSettings(id="typesafe", secret_ref=reference)
    )
    status = await settings.client.get("/api/v1/integrations/typesafe")
    assert (
        status.status_code == 200
        and status.json()["source"] == "session"
        and status.json()["available"]
    ), status.text
    assert reference not in status.text
    for index in range(2):
        await settings.store.create(
            VpnProfile(
                id=f"vpn-{index}",
                name=f"VPN{index}",
                filename="synthetic.ovpn",
                remote_host="vpn.invalid",
                remote_port=1194,
                protocol="udp",
                fingerprint=str(index) * 64,
                secret_ref=reference,
            )
        )
    vpns = await settings.client.get("/api/v1/vpn-profiles?limit=1")
    assert vpns.status_code == 200 and vpns.headers["X-Nebula-Next-Offset"] == "1", (
        vpns.text
    )
    assert vpns.json()[0]["available"] and reference not in vpns.text

    async def state():
        return WebSearchRuntimeState.READY, "Synthetic search observed ready"

    settings.host.observations.search = SimpleNamespace(state=state)
    search = await settings.client.get("/api/v1/integrations/web-search")
    assert search.status_code == 200 and search.json()["container_state"] == "ready", (
        search.text
    )
