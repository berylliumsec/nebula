"""Supervisor memory recovery uses persisted profiles and verified group proof."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import RunnerIsolation, RunnerProfile, RunnerRuntime
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryEnforcementEvidence,
    MemoryLaunchGroups,
    MemoryLaunchOwner,
)
from nebula.v3.memory_recovery import runner_fingerprint
from nebula.v3.process_identity import process_birth, process_origin
from nebula.v3.runner_profiles import container_runner_profile
from nebula.v3.runtime_host_services import HostSharedServices
from nebula.v3.runtime_memory import RuntimeMemoryAdmission
from nebula.v3.runtime_memory_recovery import HostMemoryRecovery
from nebula.v3.runtime_processes import RuntimeProcessHost
from nebula.v3.storage import NebulaStore
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_memory_budget import capability
from tests.v3.test_memory_enforcement import kernel as kernel
from tests.v3.test_memory_recovery import _WORKER
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


def stored(**changes):
    return RunnerProfile(
        id="fixture",
        name="Isolated cleanup fixture",
        runtime=RunnerRuntime.DOCKER,
        executable="/usr/bin/docker",
        socket="unix:///isolated/fixture.sock",
        platform="linux/amd64",
        isolation=RunnerIsolation.ROOTLESS,
        **changes,
    )


class Runner:
    def __init__(self, profile):
        self.profile = container_runner_profile(profile)
        self.identity = "original-synthetic-daemon"
        self.removed = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def available(self):
        return True, "Nonexecuting cleanup fixture"

    async def _capture(self, operation, *_args):
        if operation == "info":
            return json.dumps({"ID": self.identity}), "", 0
        assert operation == "inspect"
        return (
            json.dumps(
                {
                    "Id": "a" * 64,
                    "Config": {"Labels": {"nebula.memory.job": "lost"}},
                    "HostConfig": {"Memory": 2 * GIB, "MemorySwap": 2 * GIB},
                    "State": {"OOMKilled": False},
                }
            ),
            "",
            0,
        )

    async def _force_remove(self, group_id, *, require_removed):
        assert require_removed
        self.entered.set()
        await self.release.wait()
        self.removed.append(group_id)


@pytest.fixture
async def recovery(managed):
    administrator = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
    )
    budgets = MemoryBudgetService(managed.database, administrator=lambda: administrator)
    profile = await managed.store.create(stored())
    runner = Runner(profile)
    calls = []

    def resolve(observed):
        calls.append(observed.id)
        runner.profile = container_runner_profile(observed)
        return runner

    services = HostSharedServices(
        managed.host, memory_budgets=budgets, cleanup_runner_factory=resolve
    )
    services.recovery.interval = 0.02
    try:
        yield managed, budgets, profile, runner, services, calls
    finally:
        runner.release.set()
        await services.close()


async def lost_job(budgets, runner, *, alive=False):
    job = await budgets.create_job(
        "lost",
        project_id="admitted-project",
        resource_kind="fixture",
        resource_id="worker",
        capabilities=capability(),
    )
    job = await budgets.reserve(job.id, capability())
    job = await budgets.claim_start(
        job.id,
        job.revision,
        owner=MemoryLaunchOwner(
            process_id=os.getpid() if alive else 2_000_000_000,
            process_birth=process_birth(os.getpid())
            if alive
            else "absent-synthetic-owner",
            process_origin=process_origin(),
            runner_fingerprint=runner_fingerprint(runner),
            runner_identity="docker:" + runner.identity,
        ),
    )
    job = await budgets.groups_created(
        job.id,
        job.revision,
        MemoryLaunchGroups(group_id="a" * 64, auxiliary_group_ids=["b" * 64]),
    )
    return await budgets.started(
        job.id,
        job.revision,
        MemoryEnforcementEvidence(
            job_id=job.id,
            group_id="a" * 64,
            auxiliary_group_ids=["b" * 64],
            applied=job.snapshot.limits,
            protected_process_tree=True,
            termination_reason="running",
            detail="Synthetic enforcement receipt; no workload launched",
        ),
    )


async def test_host_reclaims_dead_owner_workload_and_helper_from_saved_profile(
    recovery,
):
    _, budgets, _, runner, services, calls = recovery
    await lost_job(budgets, runner)
    await services.start()
    await until(lambda: len(runner.removed) == 2)
    async with asyncio.timeout(8):
        while (result := await budgets.get_job("lost")).state != "finished":
            await asyncio.sleep(0.01)
    assert calls == ["fixture"]
    assert runner.removed == ["a" * 64, "b" * 64]
    assert result.reserved_bytes == 0 and result.outcome == "worker_lost"
    assert result.evidence.group_empty and result.evidence.auxiliary_groups_empty


async def test_host_does_not_probe_or_release_a_live_controller(recovery):
    _, budgets, _, runner, services, calls = recovery
    await lost_job(budgets, runner, alive=True)
    await services.start()
    await until(lambda: services.recovery.state == "ready")
    assert calls == [] and runner.removed == []
    assert (await budgets.status()).reserved_bytes == 2 * GIB


@pytest.mark.parametrize(
    "changed", ["missing_profile", "profile_fingerprint", "daemon_identity"]
)
async def test_unverifiable_original_authority_remains_charged(recovery, changed):
    fixture, budgets, profile, runner, services, _ = recovery
    await lost_job(budgets, runner)
    if changed == "missing_profile":
        await fixture.store.delete(RunnerProfile, profile.id)
    elif changed == "profile_fingerprint":
        await fixture.store.update(
            RunnerProfile, profile.id, {"socket": "unix:///replacement/fixture.sock"}
        )
    else:
        runner.identity = "replacement-synthetic-daemon"
    await services.start()
    await until(lambda: services.recovery.state == "ready")
    assert runner.removed == []
    assert (await budgets.status()).reserved_bytes == 2 * GIB


async def test_host_close_joins_accepted_cleanup_before_releasing_lock(recovery):
    fixture, budgets, _, runner, services, _ = recovery
    await lost_job(budgets, runner)
    runner.release.clear()
    await services.start()
    await asyncio.wait_for(runner.entered.wait(), 5)
    closing = asyncio.create_task(fixture.host.close())
    await until(lambda: services.recovery.closed)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    assert not closing.done() and fixture.host.lock.descriptor is not None
    runner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert fixture.host.lock.descriptor is None
    assert (await budgets.get_job("lost")).reserved_bytes == 0


async def test_recovery_cannot_start_outside_its_exclusive_service_owner(managed):
    rogue = HostMemoryRecovery(managed.host)
    try:
        with pytest.raises(RuntimeError, match="exclusive"):
            await rogue.start()
        assert rogue.task is None
    finally:
        await rogue.close()


async def test_supervisor_reuses_its_memory_admission_authority(recovery):
    fixture, budgets, _, _, _, _ = recovery
    root = RuntimeSupervisor(
        fixture.host,
        operator_token=_TOKEN,
        worker_admission=RuntimeMemoryAdmission(budgets),
    )
    try:
        assert root.shared_services.recovery.budgets is budgets
        await root.start()
        assert root.shared_services.recovery.task is not None
    finally:
        await root.close()


@pytest.mark.parametrize(
    "isolation,runtime,expected_platform,expected_isolation,machine",
    [
        ("rootless", "docker", "linux", "linux_rootless", None),
        ("podman_machine", "podman", "macos", "podman_machine", "fixture-vm"),
        ("docker_desktop_vm", "docker", "macos", "docker_desktop_vm", None),
    ],
)
async def test_launch_and_recovery_use_identical_profile_conversion(
    tmp_path, isolation, runtime, expected_platform, expected_isolation, machine
):
    from nebula.v3.automation_runtime import AutomationRuntimeManager
    from nebula.v3.artifacts import ArtifactStore

    profile = RunnerProfile(
        name="Fixture",
        runtime=runtime,
        executable=f"/usr/bin/{runtime}",
        socket="unix:///isolated/fixture.sock",
        context="fixture-vm" if isolation != "rootless" else None,
        platform="linux/amd64",
        isolation=isolation,
        seccomp_profile="/isolated/seccomp.json",
    )
    converted = container_runner_profile(profile)
    assert converted.platform.value == expected_platform
    assert converted.isolation_mode.value == expected_isolation
    assert converted.machine_name == machine
    assert converted.socket == (profile.socket if isolation == "rootless" else None)
    store = NebulaStore(tmp_path / "conversion.db")
    try:
        manager = AutomationRuntimeManager(
            store=store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            data_root=tmp_path,
            workspace_resolver=lambda _: tmp_path,
            runtime_image="registry.invalid/fixture@sha256:" + "a" * 64,
        )
        assert manager._runner(profile).profile == converted
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def test_host_automatically_releases_real_group_after_controller_crash(
    kernel, tmp_path
):
    runner, budgets, _, workspace, _ = kernel
    from tests.v3.test_memory_enforcement import request

    store = NebulaStore(budgets.database)
    profile = await store.create(
        RunnerProfile(
            id="fixture",
            name="Disposable OCI fixture",
            runtime="docker",
            executable=str(runner.profile.executable),
            socket=runner.profile.socket,
            platform="linux/amd64",
            isolation="rootless",
        )
    )
    host = RuntimeProcessHost(
        DeploymentCoordinator(budgets.database),
        tmp_path / "host",
        operator_token=_TOKEN,
    )
    await host.start()
    services = HostSharedServices(
        host,
        memory_budgets=budgets,
        cleanup_runner_factory=lambda observed: (
            runner if observed.id == profile.id else None
        ),
    )
    services.recovery.interval = 0.02
    ready = tmp_path / "controller-ready"
    config = tmp_path / "controller.json"
    config.write_text(
        json.dumps(
            {
                "profile": runner.profile.model_dump(mode="json"),
                "workspace": str(workspace),
                "database": str(tmp_path / "fixture.db"),
                "root": str(tmp_path),
                "administrator": budgets.administrator().model_dump(mode="json"),
                "request": request(kernel, 4, hold=60).model_dump(mode="json"),
                "name": "nebula-host-recovery-" + uuid4().hex,
                "ready": str(ready),
            }
        )
    )
    script = tmp_path / "controller.py"
    script.write_text(
        _WORKER.replace(
            "allow_unpinned_images=True,",
            "allow_unpinned_images=True, memory_profile_id='fixture',",
        )
    )
    child = None
    with (tmp_path / "controller.log").open("wb") as output:
        try:
            child = subprocess.Popen(
                [sys.executable, str(script), str(config)],
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2])
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                },
            )
            async with asyncio.timeout(20):
                while not ready.exists():
                    assert child.poll() is None, (
                        tmp_path / "controller.log"
                    ).read_text()
                    await asyncio.sleep(0.05)
            before = await budgets.get_job("crashed-owner")
            assert before.snapshot.profile_id == "fixture"
            await services.start()
            await until(lambda: services.recovery.state == "ready")
            assert (await budgets.status()).reserved_bytes == 32 * MIB
            assert (await runner._capture("inspect", before.launch_groups.group_id))[
                2
            ] == 0
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)
            async with asyncio.timeout(15):
                while (
                    result := await budgets.get_job("crashed-owner")
                ).state != "finished":
                    await asyncio.sleep(0.05)
            assert result.outcome == "worker_lost" and result.reserved_bytes == 0
            assert result.evidence.group_empty
            assert (await budgets.status()).reserved_bytes == 0
            assert (await runner._capture("inspect", before.launch_groups.group_id))[
                2
            ] != 0
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, timeout=5)
            await services.close()
            for job in await budgets.list_jobs():
                if job.launch_groups:
                    await runner._force_remove(
                        job.launch_groups.group_id, require_removed=True
                    )
            await host.close()
            await store.event_history.close()
