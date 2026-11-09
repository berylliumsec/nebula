"""Host-managed jobs apply through their exact controller and durable host receipts."""

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select
from starlette.responses import JSONResponse

from nebula.v3.database import MemoryBudgetAuditRow, RuntimeCommandRow
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    MIB,
    MemoryBudgetConflict,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
)
from nebula.v3.memory_runtime import MemoryComputeController, managed_compute
from nebula.v3.runtime_host_services import HostSharedServices
from nebula.v3.runtime_host_settings_bridge import RemoteHostSettings
from nebula.v3.runtime_processes import RuntimeProcessHost
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_managed_search_runtime import make_runtime
from tests.v3.test_memory_enforcement import kernel as kernel
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_integration_credentials import (
    credential_peer as credential_peer,
)
from tests.v3.test_runtime_processes import _TOKEN, managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def live(settings, monkeypatch):
    controller = settings.host.observations.memory_compute
    service = settings.root.shared_services.recovery.budgets
    assert controller.service is service
    capability = MemoryRunnerCapabilities(
        environment="container",
        kernel_enforced=True,
        protected_process_tree=True,
        explicit_swap=True,
        live_update=True,
        capacity_bytes=service.administrator().capacity_bytes,
        detail="Synthetic exact-owner enforcement fixture",
    )

    async def probe(runner, *, profile_id=None):
        assert runner in runners.values()
        return capability.model_copy(update={"profile_id": profile_id})

    async def observe(runner, job_id, group):
        assert runners[job_id] is runner and group == "inert-" + job_id
        return MemoryEnforcementEvidence(
            job_id=job_id,
            group_id=group,
            applied=runner.limits,
            protected_process_tree=True,
            current_bytes=4 * MIB,
            termination_reason="running",
            detail="Synthetic current kernel readback",
        )

    monkeypatch.setattr("nebula.v3.memory_runtime.probe_memory_runner", probe)
    monkeypatch.setattr("nebula.v3.memory_runtime.observe_memory_group", observe)
    runners = {}

    async def create(identity="host-service", *, project_id=None):
        current = await service.create_job(
            identity,
            project_id=project_id,
            resource_kind="web_search_service",
            resource_id=identity,
            capabilities=capability,
            requested=MemoryLimits(max_bytes=32 * MIB),
        )
        current = await service.reserve(identity, capability)
        current = await service.claim_start(identity, current.revision)
        entered, release = asyncio.Event(), asyncio.Event()
        release.set()

        class Runner:
            limits = MemoryLimits(max_bytes=32 * MIB)
            updates = 0
            lost = False

            async def _capture(self, operation, memory, swap, group):
                assert settings.managed.database.task_session() is None
                assert operation == "update" and group == "inert-" + identity
                self.updates += 1
                entered.set()
                await release.wait()
                self.limits = MemoryLimits(
                    max_bytes=int(memory.split("=", 1)[1]),
                    swap_bytes=int(swap.split("=", 1)[1])
                    - int(memory.split("=", 1)[1]),
                )
                if self.lost:
                    raise MemoryEnforcementUnavailable(
                        "Synthetic lost kernel acknowledgement"
                    )
                return "", "", 0

        runner = Runner()
        runners[identity] = runner
        controller.job_runners[identity] = runner
        current = await service.started(
            identity,
            current.revision,
            await observe(runner, identity, "inert-" + identity),
        )
        return SimpleNamespace(
            job=current, runner=runner, entered=entered, release=release
        )

    yield SimpleNamespace(
        settings=settings,
        controller=controller,
        service=service,
        create=create,
        runners=runners,
    )


def update(job, *, acknowledge=True, revision=None):
    return {
        "expected_revision": job.revision if revision is None else revision,
        "limits": {"max_bytes": 16 * MIB},
        "acknowledge_possible_termination": acknowledge,
    }


async def apply(live, active, *, key="host-apply", body=None):
    return await live.settings.mutate(
        f"memory-budgets/jobs/{active.job.id}/apply",
        body=body or update(active.job),
        key=key,
    )


async def test_host_apply_guards_and_exact_controller_receipt_replay(live):
    active = await live.create()
    path = f"/api/v1/memory-budgets/jobs/{active.job.id}/apply"
    assert (
        await live.settings.client.post(
            path, json=update(active.job), headers={"Authorization": ""}
        )
    ).status_code == 401
    assert (
        await live.settings.client.post(path, json=update(active.job))
    ).status_code == 428
    assert (
        await apply(
            live, active, key="no-ack", body=update(active.job, acknowledge=False)
        )
    ).status_code == 422
    assert (
        await apply(live, active, key="stale", body=update(active.job, revision=1))
    ).status_code == 409
    assert active.runner.updates == 0
    # Editing a profile/default runner cannot substitute for the admitted owner.
    live.controller.runners[None] = object()
    first = await apply(live, active)
    assert first.status_code == 200, first.text
    assert first.json()["application_state"] == "verified"
    assert first.json()["evidence"]["applied"]["max_bytes"] == 16 * MIB
    assert first.json()["snapshot"]["limits"]["max_bytes"] == 32 * MIB
    replay = await apply(live, active)
    assert replay.json() == first.json() and active.runner.updates == 1
    command = first.headers["X-Nebula-Command-Id"]
    assert replay.headers["X-Nebula-Command-Id"] == command
    assert (
        await apply(live, active, body=update(active.job, revision=2))
    ).status_code == 409
    lookup = await live.settings.client.get(
        "/api/v1/runtime/requests/lookup", params={"idempotency_key": "host-apply"}
    )
    assert lookup.json() == {
        "command_id": command,
        "project_id": "__nebula_host_work__",
        "state": "succeeded",
    }
    assert (
        await live.settings.client.get(f"/api/v1/runtime/requests/{command}/response")
    ).json() == first.json()
    async with live.settings.managed.database.session() as session:
        audits = list(await session.scalars(select(MemoryBudgetAuditRow)))
    assert [(row.actor_id, row.action) for row in audits] == [
        ("operator", "job.apply_requested"),
        ("operator", "job.applied"),
    ]


async def test_host_apply_requires_owning_map_and_preserves_project_proxy(
    live, monkeypatch
):
    active = await live.create()
    live.controller.job_runners.pop(active.job.id)
    live.controller.runners[None] = active.runner
    refused = await apply(live, active, key="missing-owner")
    assert refused.status_code == 503 and active.runner.updates == 0
    live.controller.job_runners[active.job.id] = active.runner
    wrong = MemoryComputeController(
        MemoryBudgetService(live.service.database), live.settings.managed.data
    )
    wrong.job_runners[active.job.id] = active.runner
    live.settings.host.observations.memory_compute = wrong
    try:
        refused = await apply(live, active, key="different-budget-owner")
        assert refused.status_code == 503 and active.runner.updates == 0
    finally:
        live.settings.host.observations.memory_compute = live.controller
    project = await live.create("project-job", project_id="admitted-project")
    with pytest.raises(MemoryBudgetConflict, match="owning project"):
        await live.settings.root.shared_services.memory_control.apply_host_job(
            project.job.id, MemoryRunningUpdate(**update(project.job)), "operator"
        )
    forwarded = []

    async def proxy(_self, scope, receive, send):
        forwarded.append((scope["path"], (await receive())["body"]))
        await JSONResponse({"project_forwarded": True}, status_code=207)(
            scope, receive, send
        )

    monkeypatch.setattr(type(live.settings.root.proxy), "__call__", proxy)
    response = await apply(live, project, key="unchanged-project-key")
    assert response.status_code == 207 and response.json()["project_forwarded"]
    assert json.loads(forwarded[0][1]) == update(project.job)
    assert project.runner.updates == active.runner.updates == 0
    assert (
        await live.settings.client.post(
            "/api/v1/memory-budgets/jobs/absent/apply", json=update(active.job)
        )
    ).status_code == 404
    async with live.settings.managed.database.session() as session:
        commands = list(await session.scalars(select(RuntimeCommandRow)))
    assert {row.idempotency_key for row in commands} == {
        "missing-owner",
        "different-budget-owner",
    }


async def test_disconnected_apply_finishes_once_and_lost_kernel_reply_stays_charged(
    live,
):
    active = await live.create()
    active.release.clear()
    pending = asyncio.create_task(apply(live, active, key="lost-viewer-response"))
    try:
        await asyncio.wait_for(active.entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        active.release.set()
        replay = await apply(live, active, key="lost-viewer-response")
        assert replay.status_code == 200 and active.runner.updates == 1
        uncertain = await live.create("unknown-kernel-result")
        uncertain.runner.lost = True
        failed = await apply(live, uncertain, key="unknown-kernel-effect")
        assert failed.status_code == 503, failed.text
        assert (
            await apply(live, uncertain, key="unknown-kernel-effect")
        ).json() == failed.json()
        assert uncertain.runner.updates == 1
        held = await live.service.get_job(uncertain.job.id)
        assert held.state == "uncertain" and held.reserved_bytes == 32 * MIB
        assert held.evidence.applied.max_bytes == 32 * MIB
        assert uncertain.runner.limits.max_bytes == 16 * MIB
        refused = await apply(
            live, uncertain, key="new-unknown-retry", body=update(held)
        )
        assert refused.status_code == 503 and "applied limits changed" in refused.text
        assert uncertain.runner.updates == 1
    finally:
        active.release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_settings_assistant_applies_same_host_job_and_reads_canonical_result(
    live, credential_peer
):
    before = (await live.settings.client.get("/api/v1/memory-budgets")).json()
    reserve = before["minimum_core_reserve_bytes"]
    saved = await live.settings.client.put(
        "/api/v1/memory-budgets",
        json={
            "expected_revision": before["revision"],
            "policy": {
                **before["policy"],
                "reserve_bytes": reserve,
                "aggregate_bytes": min(
                    before["policy"]["aggregate_bytes"],
                    before["administrator_limits"]["maximum_aggregate_bytes"],
                    before["administrator_limits"]["capacity_bytes"] - reserve,
                ),
            },
        },
    )
    assert (
        saved.status_code == 200 and saved.json()["policy"]["reserve_bytes"] == reserve
    )
    active = await live.create()
    capabilities = await credential_peer.client.call("host.services/v1", {})
    assistant = SettingsAssistantApi(
        FastAPI(),
        "unused",
        host_request=RemoteHostSettings(
            credential_peer.client, capabilities["host_settings"]
        ),
    )
    with (
        execution_context(credential_peer.lease),
        managed_compute(
            credential_peer.lease.project_id, "tool", "explicit-host-memory-apply"
        ),
    ):
        result = await assistant.request(
            credential_peer.lease.project_id,
            method="POST",
            path=f"memory-budgets/jobs/{active.job.id}/apply",
            body=update(active.job),
        )
    assert result["read_back"] and result["result"]["application_state"] == "verified"
    assert result["result"]["evidence"]["applied"]["max_bytes"] == 16 * MIB
    assert active.runner.updates == 1
    observed = await live.settings.client.get(
        f"/api/v1/memory-budgets/jobs/{active.job.id}"
    )
    public, assistant_result = observed.json(), dict(result["result"])
    for value in (public, assistant_result):
        for field in ("created_at", "updated_at"):
            timestamp = datetime.fromisoformat(value[field])
            # SQLite reloads these UTC-origin columns without timezone metadata;
            # compare the exact instant and every field, retaining microseconds.
            value[field] = timestamp.replace(tzinfo=timestamp.tzinfo or UTC).astimezone(
                UTC
            )
    assert public == assistant_result
    async with live.settings.managed.database.session() as session:
        audits = list(
            await session.scalars(
                select(MemoryBudgetAuditRow).where(
                    MemoryBudgetAuditRow.scope_key == active.job.id
                )
            )
        )
        policy_audit = await session.scalar(
            select(MemoryBudgetAuditRow).where(
                MemoryBudgetAuditRow.action == "policy.save"
            )
        )
    assert len(audits) == 2 and {row.actor_id for row in audits} == {
        "butler:admitted-project"
    }
    assert policy_audit.actor_id == "operator"


async def test_actual_host_managed_service_live_lowering_has_kernel_readback(
    kernel, tmp_path
):
    _runner, service, controller, _workspace, _image = kernel
    host = RuntimeProcessHost(
        DeploymentCoordinator(service.database),
        tmp_path / "core",
        operator_token=_TOKEN,
    )
    shared = HostSharedServices(host, memory_budgets=service)
    root = RuntimeSupervisor(host, operator_token=_TOKEN, shared_services=shared)
    runtime = await make_runtime(kernel, tmp_path / "search")
    observations = shared.host_settings.observations
    observations.search, observations.memory_compute = runtime, controller
    observations.automation.memory_compute = controller
    await root.start()
    try:
        await runtime.ensure_ready()
        job = await service.get_job(runtime._job.job_id)
        assert job.project_id is None and job.resource_kind == "web_search_service"
        assert job.evidence.applied.max_bytes == 32 * MIB
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root.app),
            base_url="https://fixture",
            headers=AUTH,
        ) as client:
            changed = await client.post(
                f"/api/v1/memory-budgets/jobs/{job.id}/apply",
                json=update(job),
                headers={"Idempotency-Key": "physical-owned-service-apply"},
            )
            assert changed.status_code == 200, changed.text
            observed = await service.get_job(job.id)
            assert (
                observed.application_state == "verified"
                and observed.reserved_bytes == 16 * MIB
            )
            group = observed.evidence.group_id
            raw, error, code = await runtime._runner_instance._capture(
                "exec",
                group,
                "/bin/sh",
                "-c",
                "cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/memory.swap.max",
            )
            assert code == 0, error
            assert raw.split() == [str(16 * MIB), "0"]
        await runtime.stop()
        finished = await service.get_job(job.id)
        assert finished.reserved_bytes == 0 and finished.evidence.group_empty
        assert (await _runner._capture("inspect", group))[2] != 0
        print(
            json.dumps(
                {
                    "job_id": job.id,
                    "group_id": group,
                    "before_bytes": 32 * MIB,
                    "after_bytes": observed.evidence.applied.max_bytes,
                    "kernel_readback": raw.split(),
                    "cleanup_state": finished.state,
                    "reserved_bytes": finished.reserved_bytes,
                    "group_empty": finished.evidence.group_empty,
                }
            )
        )
    finally:
        await runtime.close()
        await root.close()
