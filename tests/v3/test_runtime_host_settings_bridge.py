"""The Settings assistant and UI observe the same canonical host authority."""

import asyncio
from dataclasses import asdict

from fastapi import FastAPI
import pytest
from sqlalchemy import select

from nebula.v3.database import ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.domain import ProviderProfile
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_runtime import managed_compute
from nebula.v3.runtime_coordination import LeaseLost, ProjectLease
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.runtime_host_settings_bridge import HOST_SETTINGS, RemoteHostSettings
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from tests.v3.test_host_load import ManualMonitor
from tests.v3.test_runtime_integration_credentials import (
    credential_peer as credential_peer,
)
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def gateway(value):
    capabilities = await value.client.call("host.services/v1", {})
    local = FastAPI()

    @local.get("/api/v1/host-pressure")
    async def wrong_local_owner():
        raise AssertionError("Host Settings reached a project-local owner")

    return SettingsAssistantApi(
        local,
        "unused",
        host_request=RemoteHostSettings(value.client, capabilities["host_settings"]),
    )


async def test_assistant_saves_replays_and_reads_canonical_host_pressure(
    credential_peer,
):
    value, settings = credential_peer, credential_peer.settings
    settings.host.observations.gate.monitor = ManualMonitor(busy=False)
    api = await gateway(value)
    with execution_context(value.lease):
        before = await api.request(
            value.lease.project_id, method="GET", path="host-pressure"
        )
        assert before["read_back"]
        body = {
            "host_id": before["result"]["host_id"],
            "expected_revision": before["result"]["revision"],
            "thresholds": {**before["result"]["saved_thresholds"], "cpu": 0.7},
        }
        with managed_compute(value.lease.project_id, "tool", "stable-settings-tool"):
            changed = await api.request(
                value.lease.project_id, method="PUT", path="host-pressure", body=body
            )
            replay = await api.request(
                value.lease.project_id, method="PUT", path="host-pressure", body=body
            )
        assert (
            changed["read_back"]
            and replay["result"]["revision"] == changed["result"]["revision"] == 2
        )
        observed = (await settings.client.get("/api/v1/host-pressure")).json()
        assert observed["saved_thresholds"] == changed["result"]["saved_thresholds"]
        with (
            managed_compute(value.lease.project_id, "tool", "stable-settings-tool"),
            pytest.raises(ValueError, match="Idempotency"),
        ):
            await api.request(
                value.lease.project_id,
                method="PUT",
                path="host-pressure",
                body={**body, "thresholds": {**body["thresholds"], "cpu": 0.6}},
            )
    async with settings.managed.database.session() as session:
        rows = list(await session.scalars(select(RuntimeCommandRow)))
        assert (
            len(rows) == 1
            and rows[0].actor_id == "operator"
            and rows[0].idempotency_key.startswith("butler-host:")
        )


async def test_assistant_global_sql_create_is_canonical_and_idempotent(credential_peer):
    value, settings = credential_peer, credential_peer.settings
    api = await gateway(value)
    body = {"name": "Synthetic provider", "provider_type": "vllm", "is_local": True}
    with (
        execution_context(value.lease),
        managed_compute(value.lease.project_id, "tool", "profile-settings-tool"),
    ):
        created = await api.request(
            value.lease.project_id, method="POST", path="providers", body=body
        )
        repeated = await api.request(
            value.lease.project_id, method="POST", path="providers", body=body
        )
    assert created["read_back"] and created["result"]["id"] == repeated["result"]["id"]
    assert (
        await settings.store.get(ProviderProfile, created["result"]["id"])
    ).name == "Synthetic provider"
    assert await settings.store.count(ProviderProfile) == 1
    async with settings.managed.database.session() as session:
        rows = list(await session.scalars(select(RuntimeCommandRow)))
        assert len(rows) == 1 and rows[0].name == "global.sql/v1"


async def test_host_bridge_rejects_private_input_and_stale_grants(credential_peer):
    value, settings = credential_peer, credential_peer.settings
    api = await gateway(value)
    from nebula.v3.tools import GuidedToolError

    with execution_context(value.lease), pytest.raises(GuidedToolError) as denied:
        await api.request(
            value.lease.project_id,
            method="PUT",
            path="runner-profiles/new-runner",
            body={
                "name": "Unreviewed runner",
                "runtime": "podman",
                "executable": "/inert/podman",
                "platform": "linux/amd64",
                "isolation": "rootless",
            },
        )
    assert (
        denied.value.code == "runner_trust_required"
        and "setting_id=runners" in denied.value.next_action
    )
    assert getattr(denied.value, "_nebula_before_execution", False)
    with (
        execution_context(value.lease),
        pytest.raises(ValueError, match="requesting browser"),
    ):
        await api.request(
            value.lease.project_id,
            method="PUT",
            path="integrations/typesafe",
            body={"secret": "never-dispatch"},
        )
    payload = {
        "lease": asdict(value.lease),
        "operation_id": "private-op",
        "method": "POST",
        "path": "/api/v1/deployment/operations",
        "body": {"action": "shutdown"},
    }
    rejected = await value.client.call(HOST_SETTINGS, payload)
    assert not rejected["ok"] and rejected["error"] == "invalid"
    stale = ProjectLease(
        value.lease.project_id, value.lease.runtime_id, value.lease.fencing_token + 1
    )
    rejected = await value.client.call(
        HOST_SETTINGS,
        {
            **payload,
            "lease": asdict(stale),
            "method": "GET",
            "path": "/api/v1/host-cpu-budget",
            "body": None,
        },
    )
    assert not rejected["ok"] and rejected["error"] == "lease_lost"
    assert settings.root.started and not settings.root.closed
    async with settings.managed.database.session() as session:
        assert list(await session.scalars(select(RuntimeCommandRow))) == []


async def test_sql_delegate_losing_lease_before_acceptance_cannot_commit(
    credential_peer, monkeypatch
):
    value, settings = credential_peer, credential_peer.settings
    api = await gateway(value)
    original = GlobalSqlDelivery.execute

    async def lose_grant(delivery, intent, **arguments):
        async with settings.root.coordinator.coordination._write() as session:
            row = await session.get(ProjectOwnershipRow, value.lease.project_id)
            row.runtime_id = None
        return await original(delivery, intent, **arguments)

    monkeypatch.setattr(GlobalSqlDelivery, "execute", lose_grant)
    with execution_context(value.lease), pytest.raises(LeaseLost):
        await api.request(
            value.lease.project_id,
            method="POST",
            path="providers",
            body={"name": "Must not commit", "provider_type": "vllm", "is_local": True},
        )
    assert await settings.store.count(ProviderProfile) == 0
    async with settings.managed.database.session() as session:
        assert list(await session.scalars(select(RuntimeCommandRow))) == []


async def test_queued_delegate_losing_lease_does_not_start_probe(credential_peer):
    value, settings = credential_peer, credential_peer.settings
    await settings.store.create(
        ProviderProfile(
            id="held-probe", name="Inert provider", provider_type="vllm", is_local=True
        )
    )
    api, effect = await gateway(value), settings.host.effect
    for _ in range(8):
        await effect.slots.acquire()
    task = None
    try:
        with (
            execution_context(value.lease),
            managed_compute(value.lease.project_id, "tool", "queued-host-probe"),
        ):
            task = asyncio.create_task(
                api.request(
                    value.lease.project_id,
                    method="POST",
                    path="providers/held-probe/health",
                )
            )
        async with asyncio.timeout(3):
            while not effect.tasks:
                await asyncio.sleep(0.01)
        async with settings.root.coordinator.coordination._write() as session:
            row = await session.get(ProjectOwnershipRow, value.lease.project_id)
            row.runtime_id = None
        for _ in range(8):
            effect.slots.release()
        with pytest.raises((LeaseLost, ValueError)):
            await task
        assert settings.calls == []
    finally:
        while effect.slots._value < 8:
            effect.slots.release()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_host_bootstrap_exposes_managed_search_capability_without_api(settings):
    from nebula.v3.managed_search_runtime import ManagedSearchRuntime

    runtime = settings.host.observations.search
    assert isinstance(runtime, ManagedSearchRuntime)
    assert (
        runtime.memory_compute.service is settings.root.shared_services.recovery.budgets
    )
    capability = await runtime.initialization_capability(action="start")
    response = await settings.client.get("/api/v1/integrations/web-search")
    assert response.status_code == 200, response.text
    assert response.json()["initialization_supported"] == capability["supported"]
    assert response.json()["initialization_detail"] == capability["detail"]


async def test_host_image_refusal_reports_audited_memory_block_before_daemon_work(
    settings, monkeypatch
):
    from nebula.v3.database import MemoryJobRow
    from nebula.v3.domain import RunnerProfile
    from nebula.v3.sandbox import ContainerImagePreparer

    await settings.store.create(
        RunnerProfile(
            id="local",
            name="Synthetic managed runner",
            runtime="docker",
            executable="/inert/docker",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )
    )

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("An uncontrolled daemon initializer was spawned")

    class Preparer:
        def __init__(self, **arguments):
            self.preparer = ContainerImagePreparer(**arguments)

        async def prepare(self):
            return await self.preparer._runtime_command(
                "pull", self.preparer.source_reference, timeout_seconds=1
            )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    settings.host.observations.automation.preparer_factory = Preparer
    refused = await settings.mutate(
        "automation/runtime/prepare", key="audited-image-refusal"
    )
    assert (
        refused.status_code == 503 and "memory-managed daemon runner" in refused.text
    ), refused.text
    async with settings.managed.database.session() as session:
        jobs = list(
            await session.scalars(
                select(MemoryJobRow).where(
                    MemoryJobRow.resource_kind == "container_image_pull"
                )
            )
        )
        commands = list(await session.scalars(select(RuntimeCommandRow)))
    assert (
        len(jobs) == 1
        and jobs[0].state == "blocked"
        and jobs[0].reserved_bytes == 0
        and jobs[0].project_id is None
    )
    assert (
        len(commands) == 1
        and commands[0].state == "failed"
        and commands[0].result["http_status"] == 503
    )
