"""One admission path for managed OCI compute, with no unrestricted fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .async_utils import finish_cleanup
from .memory_budget import MemoryBudgetService
from .memory_guard import MemoryLaunchGuard, install_memory_guard
from .memory_guard import observe_memory_group
from .diagnostics import create_diagnostic_task, record_caught_exception
from .memory_policy import (
    GIB,
    MemoryBudgetConflict,
    MemoryEnforcementUnavailable,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
    MemoryLimits,
    MemoryLaunchOwner,
    MIB,
    select_memory_policy,
    ExecutionEnvironment,
)


@dataclass(frozen=True)
class ComputeContext:
    project_id: str | None
    resource_kind: str
    resource_id: str
    job_id: str | None = None


@dataclass
class PersistentMemoryJob:
    job_id: str
    guard: MemoryLaunchGuard
    task: asyncio.Task

    async def close(self):
        self.task.cancel()
        result = (await asyncio.gather(self.task, return_exceptions=True))[0]
        if isinstance(result, Exception):
            raise result


compute_context: ContextVar[ComputeContext | None] = ContextVar(
    "nebula_compute_context", default=None
)


@contextmanager
def managed_compute(
    project_id: str | None,
    resource_kind: str,
    resource_id: str,
    *,
    job_id: str | None = None,
):
    token = compute_context.set(
        ComputeContext(project_id, resource_kind, resource_id, job_id)
    )
    try:
        yield
    finally:
        compute_context.reset(token)


async def probe_memory_runner(
    runner, *, profile_id: str | None = None
) -> MemoryRunnerCapabilities:
    healthy, detail = await runner.available()
    if not healthy:
        return MemoryRunnerCapabilities(
            environment="container", profile_id=profile_id, detail=detail
        )
    output, error, code = await runner._capture("info", "--format", "{{json .}}")
    if code:
        return MemoryRunnerCapabilities(
            environment="container",
            profile_id=profile_id,
            detail=error or "Memory controller capabilities could not be read",
        )
    document = json.loads(output)
    host = document.get("host", document.get("Host", {}))
    version = str(
        document.get(
            "CgroupVersion", host.get("cgroupVersion", host.get("CgroupVersion", ""))
        )
    ).lower()
    memory = document.get("MemoryLimit") is True or "memory" in host.get(
        "cgroupControllers", []
    )
    supported = version in {"2", "v2"} and memory
    capacity = document.get("MemTotal", host.get("memTotal", host.get("MemTotal")))
    capacity = capacity if type(capacity) is int and capacity >= 16 * MIB else None
    supported = supported and capacity is not None
    authority = document.get("ID")
    supported = supported and (
        runner.profile is not None
        and runner.profile.runtime_type.value == "docker"
        and isinstance(authority, str)
        and 8 <= len(authority) <= 200
    )
    return MemoryRunnerCapabilities(
        environment="container",
        profile_id=profile_id,
        kernel_enforced=supported,
        protected_process_tree=supported,
        explicit_swap=supported,
        pressure_threshold=False,
        live_update=supported,
        capacity_bytes=capacity,
        detail="Dedicated cgroup-v2 job containers verify memory and swap before release. Explicit running-limit changes require kernel readback; memory.high is unavailable on this adapter."
        if supported
        else "This OCI runner needs a delegated cgroup-v2 memory controller and a stable Docker daemon identity, or a certified memory-runner agent; work will remain held.",
    )


class MemoryComputeController:
    def __init__(self, service: MemoryBudgetService, data_root: Path):
        self.service = service
        self.guard_directory = data_root / "memory-guards"
        self.runners: dict[str | None, Any] = {}
        self.job_runners: dict[str, Any] = {}

    async def run_sandbox(
        self,
        runner,
        request,
        *,
        job_id: str,
        project_id: str | None,
        resource_kind: str,
        resource_id: str,
        profile_id: str | None = None,
        input_bytes: bytes = b"",
        on_chunk=None,
        container_name: str | None = None,
        persistent: bool = False,
        loopback_only: bool = False,
        on_ready=None,
        requested: MemoryLimits | None = None,
        vpn_config: str | None = None,
    ):
        from .sandbox import SandboxContainerUser
        from .memory_recovery import runner_fingerprint, runner_identity
        from .process_identity import process_birth, process_origin

        if request.container_user != SandboxContainerUser.NON_ROOT:
            await self.reject_unsupported(
                "container",
                project_id=project_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                detail="Owner-mapped compute requires a runner that excludes host data and control sockets from exported filesystems. Read-only mounts do not isolate Unix sockets. Use the unprivileged compute identity or configure an isolated runner before retrying.",
            )
        capability = await probe_memory_runner(runner, profile_id=profile_id)
        self.runners[profile_id] = runner
        if requested is None:
            status = await self.service.status()
            configured = select_memory_policy(
                status.policy, status.revision, status.administrator_limits, capability
            ).limits
            maximum = min(configured.max_bytes, request.limits.memory_mb * MIB)
            requested = MemoryLimits(
                max_bytes=maximum,
                swap_bytes=min(
                    configured.swap_bytes, request.limits.memory_swap_mb * MIB
                ),
                high_bytes=min(configured.high_bytes, maximum)
                if configured.high_bytes is not None
                else None,
            )
        overhead = (
            64 * MIB
            if loopback_only or request.network.value in {"scoped", "vpn"}
            else 0
        )
        job = await self.service.create_job(
            job_id,
            project_id=project_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            capabilities=capability,
            requested=requested,
            runner_overhead_bytes=overhead,
        )
        if job.state == "blocked":
            raise MemoryEnforcementUnavailable(job.detail or capability.detail)
        if job.snapshot.limits.max_bytes > 128 * GIB:
            raise MemoryEnforcementUnavailable(
                "this OCI adapter supports at most 128 GiB per job"
            )
        group = (
            container_name
            or "nebula-memory-" + hashlib.sha256(job_id.encode()).hexdigest()[:32]
        )
        guard: MemoryLaunchGuard | None = None
        claimed = False
        monitor = None

        async def sample_usage():
            while True:
                await asyncio.sleep(0.7)
                immutable_group = (
                    guard.container_id if guard and guard.container_id else group
                )
                if persistent:
                    oom, _error, code = await runner._capture(
                        "inspect", immutable_group, "--format", "{{.State.OOMKilled}}"
                    )
                    if code == 0 and oom == "true":
                        # End the entire worker after positive group-level OOM
                        # evidence. Retain the object for final readback.
                        await runner._capture("kill", immutable_group)
                        return
                sample = await observe_memory_group(runner, job_id, immutable_group)
                if sample is not None:
                    try:
                        await self.service.observe(job_id, sample)
                        if guard is not None:
                            guard.last_sample = sample
                    except MemoryBudgetConflict:
                        # diagnostic-expected: application/cleanup can race a usage sample.
                        continue

        async def ready(evidence):
            nonlocal monitor
            current = await self.service.get_job(job_id)
            if (
                current.launch_owner
                and current.launch_owner.runner_identity is not None
                and await runner_identity(runner)
                != current.launch_owner.runner_identity
            ):
                raise MemoryEnforcementUnavailable(
                    "Runner authority changed before workload release; the job remains held for verified cleanup"
                )
            await self.service.started(job_id, current.revision, evidence)
            monitor = create_diagnostic_task(
                sample_usage(),
                feature="runtime",
                event_code="memory.usage_monitor_failed",
                failure_message="Job memory usage could not be observed; its reservation remains charged.",
                name="memory-usage-" + job_id,
            )
            if on_ready is not None:
                await on_ready(guard)

        async def finished(evidence):
            current = await self.service.get_job(job_id)
            await cleanup_authority(current)
            await self.service.finish(job_id, current.revision, evidence)

        async def unstarted(group_id, detail):
            current = await self.service.get_job(job_id)
            await cleanup_authority(current)
            await self.service.unstarted_cleanup(
                job_id,
                current.revision,
                group_id=group_id,
                detail=detail,
                verified_empty=True,
            )

        async def cleanup_authority(current):
            owner = current.launch_owner
            if (
                owner is None
                or owner.runner_identity is None
                or await runner_identity(runner) != owner.runner_identity
            ):
                raise MemoryEnforcementUnavailable(
                    "The owning daemon identity changed or is unavailable during cleanup; capacity remains charged"
                )

        async def created(groups):
            current = await self.service.get_job(job_id)
            if (
                current.launch_owner
                and current.launch_owner.runner_identity is not None
                and await runner_identity(runner)
                != current.launch_owner.runner_identity
            ):
                raise MemoryEnforcementUnavailable(
                    "Runner authority changed during group creation; the reservation remains charged"
                )
            await self.service.groups_created(job_id, current.revision, groups)

        try:
            while job.state in {"queued", "blocked"}:
                job = await self.service.reserve(job_id, capability)
                if job.state == "queued":
                    await asyncio.sleep(0.2)
            if job.state != "reserved":
                raise MemoryBudgetConflict(
                    "job already has an execution owner or a terminal result; explicit retries need a new job identity"
                )
            birth = process_birth(os.getpid())
            if birth is None:
                raise MemoryEnforcementUnavailable(
                    "the launch owner's process identity is unavailable"
                )
            job = await self.service.claim_start(
                job_id,
                job.revision,
                owner=MemoryLaunchOwner(
                    process_id=os.getpid(),
                    process_birth=birth,
                    runner_fingerprint=runner_fingerprint(runner),
                    process_origin=process_origin(),
                    runner_identity=await runner_identity(runner),
                ),
            )
            claimed = True
            self.job_runners[job_id] = runner
            guard = MemoryLaunchGuard(
                job_id,
                job.snapshot,
                install_memory_guard(self.guard_directory),
                ready,
                finished,
                unstarted,
                on_groups_created=created,
            )
            result = await runner.run_stream(
                request,
                input_bytes=input_bytes,
                on_chunk=on_chunk,
                container_name=group,
                memory_guard=guard,
                persistent=persistent,
                loopback_only=loopback_only,
                vpn_config=vpn_config,
            )
            observed = await self.service.get_job(job_id)
            result.memory_job_id, result.memory_outcome = job_id, observed.outcome
            result.memory_enforcement = (
                observed.evidence.model_dump(mode="json") if observed.evidence else None
            )
            return result
        finally:

            async def cleanup():
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                current = await self.service.get_job(job_id)
                if current.state == "finished":
                    self.job_runners.pop(job_id, None)
                if current.state in {"queued", "blocked"}:
                    await self.service.cancel_not_started(
                        job_id, current.revision, actor_id="system"
                    )
                elif current.state == "reserved":
                    # CAS prevents release if another controller claimed launch.
                    await self.service.unstarted_cleanup(
                        job_id,
                        current.revision,
                        group_id=group,
                        detail="Controller stopped before any workload launch.",
                        verified_empty=True,
                    )
                elif claimed and current.state in {"starting", "running", "updating"}:
                    if guard is None:
                        await self.service.unstarted_cleanup(
                            job_id,
                            current.revision,
                            group_id=group,
                            detail="Controller stopped before creating a runner group.",
                            verified_empty=True,
                        )
                    elif not guard.finalized:
                        # Do not release capacity on an unverified cleanup.
                        await self.service.worker_lost(job_id, current.revision)

            await finish_cleanup(cleanup())

    async def jobs(self) -> list[Any]:
        return await self.service.list_jobs()

    async def open_persistent(
        self,
        runner,
        request,
        *,
        job_id: str,
        project_id: str,
        resource_kind: str,
        resource_id: str,
        container_name: str,
        loopback_only: bool = False,
        vpn_config: str | None = None,
    ):
        ready = asyncio.get_running_loop().create_future()

        async def started(guard):
            if not ready.done():
                ready.set_result(guard)

        task = create_diagnostic_task(
            self.run_sandbox(
                runner,
                request,
                job_id=job_id,
                project_id=project_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                profile_id=runner.memory_profile_id,
                container_name=container_name,
                persistent=True,
                loopback_only=loopback_only,
                vpn_config=vpn_config,
                on_ready=started,
            ),
            feature="runtime",
            event_code="memory.persistent_job_ended",
            failure_message="A managed runtime stopped; inspect its durable memory outcome.",
            name="memory-worker-" + job_id,
        )
        try:
            await asyncio.wait((ready, task), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                raise MemoryEnforcementUnavailable(
                    "The managed runtime ended before admission completed"
                )
            return PersistentMemoryJob(job_id, ready.result(), task)
        except BaseException:
            # diagnostic-expected: caller cancellation cannot leave an unattached admission running.
            task.cancel()

            async def cleanup():
                await asyncio.gather(task, return_exceptions=True)

            await finish_cleanup(cleanup())
            raise

    async def reject_unsupported(
        self,
        environment: ExecutionEnvironment,
        *,
        project_id: str | None,
        resource_kind: str,
        resource_id: str,
        detail: str,
    ):
        identity = "held-" + uuid4().hex
        await self.service.create_job(
            identity,
            project_id=project_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            capabilities=MemoryRunnerCapabilities(
                environment=environment, detail=detail
            ),
        )
        raise MemoryEnforcementUnavailable(detail)

    async def apply_running(
        self, job_id: str, request: MemoryRunningUpdate, actor_id: str
    ):
        current = await self.service.get_job(job_id)
        runner = self.job_runners.get(job_id)
        if runner is None:
            raise MemoryEnforcementUnavailable(
                "Reconnect this job's owning runner before applying limits; current reservations remain charged"
            )
        capability = await probe_memory_runner(
            runner, profile_id=current.snapshot.profile_id
        )
        if current.evidence is None:
            raise MemoryBudgetConflict(
                "only a verified running job can receive new limits"
            )
        before = await observe_memory_group(runner, job_id, current.evidence.group_id)
        if before is None or before.applied != current.evidence.applied:
            raise MemoryEnforcementUnavailable(
                "The owning group or its applied limits changed. Read this job before retrying."
            )
        pending = await self.service.request_running_update(
            job_id, request, capability, actor_id=actor_id
        )
        assert pending.evidence is not None
        group = pending.evidence.group_id
        try:
            _output, error, code = await runner._capture(
                "update",
                f"--memory={request.limits.max_bytes}",
                f"--memory-swap={request.limits.reservation_bytes}",
                group,
            )
            observed = await observe_memory_group(runner, job_id, group)
            if observed is not None and observed.applied == request.limits:
                return await self.service.applied_running_update(
                    job_id, pending.revision, observed, actor_id=actor_id
                )
            if observed is not None and observed.applied == pending.evidence.applied:
                return await self.service.failed_running_update(
                    job_id,
                    pending.revision,
                    observed,
                    detail=error[-1000:]
                    if code
                    else "Runner did not apply the requested limits.",
                    actor_id=actor_id,
                )
            raise MemoryEnforcementUnavailable(
                "Application is unverified. Capacity remains reserved; inspect this job before retrying."
            )
        except BaseException as error:
            # A cancellation or lost response cannot roll back a kernel change.
            # Keep the maximum reservation and mark uncertainty for reconciliation.
            record_caught_exception(
                "runtime",
                "memory.live_update_unverified",
                "Running limits could not be verified.",
                error,
                stage="live-memory-application",
            )

            async def lost():
                latest = await self.service.get_job(job_id)
                if latest.state == "updating":
                    await self.service.worker_lost(job_id, latest.revision)

            await finish_cleanup(lost())
            raise
