"""Real bounded allocations in a deliberately selected disposable OCI daemon.

The fixture adapter certifies this test daemon only, not a production rootless
runner. Kernel proof, workload allocation, OOM evidence and removal are real.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from nebula.v3.database import Database
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetUpdate,
    MemoryLimits,
    MemoryRunningUpdate,
    MemoryBudgetError,
)
from nebula.v3.memory_runtime import MemoryComputeController
from nebula.v3.memory_guard import install_memory_guard
from nebula.v3.sandbox import (
    ContainerRuntimeType,
    ContainerSandboxRunner,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
    SandboxLimits,
    SandboxRequest,
    SandboxWorkspaceAccess,
)

pytestmark = pytest.mark.anyio

_SOURCE = r"""
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/wait.h>
static void allocate(int megabytes, int hold) {
    size_t size=(size_t)megabytes*1024*1024;
    volatile char *p=malloc(size);
    if (!p) _exit(71);
    for(size_t i=0;i<size;i+=4096) p[i]=1;
    puts("allocated"); fflush(stdout);
    sleep(hold);
    free((void *)p);
}
int main(int argc,char **argv) {
    int amount=atoi(argv[1]), hold=atoi(argv[2]);
    if (argc>3) {
        for(int i=0;i<2;i++) if(fork()==0) { allocate(amount,hold); _exit(0); }
        int status,failed=0;
        for(int i=0;i<2;i++) { wait(&status); if(!WIFEXITED(status)||WEXITSTATUS(status)) failed=1; }
        return failed?72:0;
    }
    allocate(amount,hold);
    return 0;
}
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_guard_installation_survives_private_umask_and_concurrent_generations(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    previous = os.umask(0o077)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            paths = list(executor.map(install_memory_guard, [tmp_path] * 8))
    finally:
        os.umask(previous)
    assert len(set(paths)) == 1
    assert paths[0].stat().st_mode & 0o777 == 0o444
    assert not list(tmp_path.glob(".guard-*"))


class IsolatedKernelFixtureRunner(ContainerSandboxRunner):
    async def available(self):
        output, error, code = await self._capture("info", "--format", "{{json .}}")
        if code:
            return False, error
        info = json.loads(output)
        supported = (
            info.get("OSType") == "linux"
            and str(info.get("CgroupVersion")) == "2"
            and info.get("MemoryLimit") is True
        )
        return (
            supported,
            "Explicit disposable cgroup-v2 kernel fixture; not production isolation certification.",
        )


@pytest.fixture
async def kernel(tmp_path):
    runtime = os.getenv("NEBULA_TEST_MEMORY_RUNTIME")
    image = os.getenv("NEBULA_TEST_MEMORY_IMAGE")
    if not runtime or not image:
        pytest.skip(
            "explicit disposable OCI runtime/image required for actual kernel memory tests"
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o755)
    workspace.chmod(0o755)
    source = tmp_path / "allocator.c"
    source.write_text(_SOURCE)
    subprocess.run(
        ["gcc", "-O0", "-static", str(source), "-o", str(workspace / "allocator")],
        check=True,
        capture_output=True,
        timeout=30,
    )
    (workspace / "allocator").chmod(0o555)
    runner = IsolatedKernelFixtureRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable=Path(runtime),
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            socket=os.getenv(
                "NEBULA_TEST_MEMORY_SOCKET", "unix:///var/run/docker.sock"
            ),
        ),
        allow_unpinned_images=True,
        workspace_roots=[workspace],
    )
    database = Database(tmp_path / "fixture.db")
    administrator = MemoryAdministratorLimits(
        capacity_bytes=256 * MIB,
        maximum_job_bytes=64 * MIB,
        maximum_aggregate_bytes=128 * MIB,
        minimum_reserve_bytes=128 * MIB,
    )
    service = MemoryBudgetService(database, administrator=lambda: administrator)
    initial = await service.status()
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=initial.revision,
            policy=initial.policy.model_copy(
                update={
                    "default_job": MemoryLimits(max_bytes=32 * MIB),
                    "aggregate_bytes": 64 * MIB,
                }
            ),
        ),
        actor_id="fixture",
    )
    controller = MemoryComputeController(service, tmp_path)
    try:
        yield runner, service, controller, workspace, image
    finally:
        await database.dispose()


def request(kernel, amount, hold=0, children=False):
    _, _, _, workspace, image = kernel
    return SandboxRequest(
        image=image,
        command=[
            "/workspace/allocator",
            str(amount),
            str(hold),
            *(["children"] if children else []),
        ],
        workspace=workspace,
        workspace_access=SandboxWorkspaceAccess.READ,
        limits=SandboxLimits(memory_mb=32, timeout_seconds=20),
    )


async def run(kernel, req, identity=None):
    runner, _, controller, _, _ = kernel
    identity = identity or uuid4().hex
    return await controller.run_sandbox(
        runner,
        req,
        job_id=identity,
        project_id="synthetic",
        resource_kind="fixture",
        resource_id=identity,
        profile_id="isolated-kernel-fixture",
    )


async def test_real_job_below_limit_completes_with_kernel_readback(kernel):
    result = await run(kernel, request(kernel, 4))
    assert result.exit_code == 0
    assert result.stdout == "allocated\n"
    assert result.memory_outcome == "completed"
    assert result.memory_enforcement["applied"]["max_bytes"] == 32 * MIB
    assert result.memory_enforcement["group_empty"] is True
    assert "nebula.memory-runner" not in result.stdout
    assert (await kernel[1].status()).reserved_bytes == 0


async def test_real_oversized_job_is_contained_and_has_positive_oom_evidence(kernel):
    result = await run(kernel, request(kernel, 128))
    assert result.exit_code != 0
    assert result.memory_outcome == "memory_limit_exceeded"
    assert result.memory_enforcement["oom_victim_confirmed"] is True
    assert result.memory_enforcement["group_empty"] is True
    assert (await kernel[1].status()).reserved_bytes == 0


async def test_real_children_share_the_job_limit(kernel):
    result = await run(kernel, request(kernel, 24, 2, children=True))
    assert result.exit_code != 0
    assert result.memory_enforcement["applied"]["max_bytes"] == 32 * MIB
    assert result.memory_enforcement["group_empty"] is True
    assert (await kernel[1].status()).reserved_bytes == 0


async def test_real_cancellation_kills_descendants_before_releasing_memory(kernel):
    _, service, _, _, _ = kernel
    identity = uuid4().hex
    task = asyncio.create_task(run(kernel, request(kernel, 4, 10), identity))
    try:
        for _ in range(100):
            jobs = await service.list_jobs()
            if jobs and jobs[0].state == "running":
                break
            await asyncio.sleep(0.05)
        assert (await service.get_job(identity)).state == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        observed = await service.get_job(identity)
        assert observed.state == "finished" and observed.outcome == "cancelled"
        assert observed.evidence.group_empty is True
        assert (await service.status()).reserved_bytes == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_real_concurrent_jobs_queue_at_aggregate_limit(kernel):
    _, service, _, _, _ = kernel
    status = await service.status()
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(update={"aggregate_bytes": 32 * MIB}),
        ),
        actor_id="fixture",
    )
    first, second = uuid4().hex, uuid4().hex
    tasks = [
        asyncio.create_task(run(kernel, request(kernel, 4, 2), identity))
        for identity in (first, second)
    ]
    try:
        saw_waiter = False
        for _ in range(100):
            jobs = await service.list_jobs()
            snapshot = await service.status()
            assert snapshot.reserved_bytes <= 32 * MIB
            if any(job.state == "running" for job in jobs) and any(
                job.state == "queued" for job in jobs
            ):
                saw_waiter = True
                break
            await asyncio.sleep(0.05)
        assert saw_waiter
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
        assert all(result.exit_code == 0 for result in results)
        assert (await service.status()).reserved_bytes == 0
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_running_limit_change_requires_acknowledgement_and_kernel_readback(
    kernel,
):
    _, service, controller, _, _ = kernel
    identity = uuid4().hex
    task = asyncio.create_task(run(kernel, request(kernel, 4, 10), identity))
    try:
        for _ in range(100):
            jobs = await service.list_jobs()
            if (
                jobs
                and jobs[0].state == "running"
                and jobs[0].evidence.current_bytes is not None
            ):
                break
            await asyncio.sleep(0.05)
        current = await service.get_job(identity)
        assert current.evidence.current_bytes >= 4 * MIB
        # A profile may be edited/replaced while this job remains resident.
        # Its owning runner binding must remain the one admitted for this job.
        controller.runners[None] = object()
        update = MemoryRunningUpdate(
            expected_revision=current.revision, limits=MemoryLimits(max_bytes=16 * MIB)
        )
        with pytest.raises(MemoryBudgetError, match="acknowledge"):
            await controller.apply_running(identity, update, "synthetic-operator")
        assert (await service.get_job(identity)).evidence.applied.max_bytes == 32 * MIB
        observed = await controller.apply_running(
            identity,
            update.model_copy(update={"acknowledge_possible_termination": True}),
            "synthetic-operator",
        )
        assert observed.application_state == "verified"
        assert (
            observed.evidence.applied.max_bytes == observed.reserved_bytes == 16 * MIB
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        finished = await service.get_job(identity)
        assert finished.evidence.group_empty
        assert finished.evidence.peak_bytes >= 4 * MIB
        assert (await service.status()).reserved_bytes == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_persistent_worker_is_reserved_until_all_children_are_removed(
    kernel,
):
    runner, service, controller, _, _ = kernel
    identity = uuid4().hex
    group = "nebula-memory-" + identity
    req = request(kernel, 4).model_copy(
        update={"command": ["/bin/sh", "-c", "sleep 3600"]}
    )
    worker = await controller.open_persistent(
        runner,
        req,
        job_id=identity,
        project_id="synthetic",
        resource_kind="automation_session",
        resource_id=identity,
        container_name=group,
    )
    try:
        assert (await service.get_job(identity)).application_state == "verified"
        output, error, code = await runner._capture(
            "exec", group, "/workspace/allocator", "4", "0"
        )
        assert code == 0, error
        assert output == "allocated"
        assert (await service.status()).reserved_bytes == 32 * MIB
    finally:
        await worker.close()
    result = await service.get_job(identity)
    assert result.state == "finished" and result.evidence.group_empty
    assert (await service.status()).reserved_bytes == 0


async def test_real_explicit_job_cap_is_not_raised_to_the_host_default(kernel):
    _, service, _, _, _ = kernel
    current = await service.status()
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=current.revision,
            policy=current.policy.model_copy(
                update={"default_job": MemoryLimits(max_bytes=64 * MIB)}
            ),
        ),
        actor_id="fixture",
    )
    result = await run(kernel, request(kernel, 128))
    job = await service.get_job(result.memory_job_id)
    assert job.snapshot.limits.max_bytes == job.evidence.applied.max_bytes == 32 * MIB
    assert job.outcome == "memory_limit_exceeded"


async def test_real_network_helper_stays_charged_until_verified_removal(kernel):
    from nebula.v3.sandbox import EgressRule, SandboxNetwork, SandboxExecutionKind

    runner, service, _, _, image = kernel
    current = await service.status()
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=current.revision,
            policy=current.policy.model_copy(update={"aggregate_bytes": 128 * MIB}),
        ),
        actor_id="fixture",
    )

    class Lease:
        enabled = False
        closed = False

        async def close(self):
            await runner._force_remove(self.helper_name, require_removed=True)
            self.closed = True

    lease = Lease()

    class IsolatedNoEgressHelper:
        certified = True

        async def acquire(self, *, container_name, **_options):
            lease.helper_name = container_name + "-egress"
            _output, error, code = await runner._capture(
                "run",
                "--detach",
                "--name=" + lease.helper_name,
                "--network=none",
                "--memory=64m",
                "--memory-swap=64m",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--entrypoint=/bin/sh",
                image,
                "-c",
                "sleep 3600",
            )
            assert code == 0, error
            lease.network_mode = "container:" + lease.helper_name
            return lease

    runner.egress_controller = IsolatedNoEgressHelper()
    req = request(kernel, 4).model_copy(
        update={
            "network": SandboxNetwork.SCOPED,
            "execution_kind": SandboxExecutionKind.NETWORK_TOOL,
            "egress_rules": [EgressRule(address="127.0.0.1", all_ports=True)],
        }
    )
    result = await run(kernel, req)
    job = await service.get_job(result.memory_job_id)
    assert job.snapshot.runner_overhead_bytes == 64 * MIB
    assert (
        job.evidence.auxiliary_groups_empty
        and len(job.evidence.auxiliary_group_ids) == 1
    )
    assert lease.closed and job.reserved_bytes == 0
    assert (await service.status()).reserved_bytes == 0
