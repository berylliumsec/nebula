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
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn
from uuid import uuid4

from .async_utils import finish_cleanup
from .memory_budget import (
    HOST_COMMAND_KIND,
    QUEUE_HEARTBEAT_SECONDS,
    HostCommandQueuePosition,
    MemoryBudgetService,
)
from .memory_guard import MemoryLaunchGuard, install_memory_guard
from .memory_guard import observe_memory_group
from .diagnostics import create_diagnostic_task, record_caught_exception
from .memory_policy import (
    GIB,
    MemoryAdmissionTimeout,
    MemoryBudgetConflict,
    MemoryBudgetError,
    MemoryEnforcementUnavailable,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
    MemoryLimits,
    MemoryLaunchGroups,
    MemoryLaunchOwner,
    MemoryTerminationReason,
    MIB,
    select_memory_policy,
    ExecutionEnvironment,
)


# A queued Host command waits in the fair queue for up to 30 minutes; the
# operator can cancel it sooner (docs/design/host-command-queue.md).
HOST_COMMAND_QUEUE_SECONDS = 30 * 60.0


class HostCommandCancelled(MemoryBudgetError):
    """The operator cancelled a queued Host command before it started."""


# A queued job never waits forever: the caller gets a clear failure and the
# queued reservation is cancelled, instead of a request that hangs for hours.
QUEUED_ADMISSION_SECONDS = 180.0


def _admission_expired(deadline: float, job_id: str) -> None:
    if asyncio.get_running_loop().time() >= deadline:
        raise MemoryAdmissionTimeout(
            f"Waited {QUEUED_ADMISSION_SECONDS:g} s for the memory budget, but other "
            f"running work holds all of it; {job_id} was not started. Try again shortly."
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


class HostMemoryJob:
    """A persistent Host worker job inside one protected runner memory group.

    Every command of the owning session starts inside the same group and shares
    its frozen budget. Capacity is released only after the runner verifies the
    group is empty and removed.
    """

    def __init__(self, controller: "MemoryComputeController", runner, job_id: str):
        self.controller, self.runner, self.job_id = controller, runner, job_id
        self.monitor: asyncio.Task | None = None
        self.ended = False
        self.outcome: str | None = None
        self._closing: asyncio.Future | None = None
        self._peak: int | None = None

    def start_monitor(self) -> None:
        self.monitor = create_diagnostic_task(
            self._sample(),
            feature="runtime",
            event_code="memory.usage_monitor_failed",
            failure_message="Host job memory usage could not be observed; its reservation remains charged.",
            name="memory-usage-" + self.job_id,
        )

    def _settle_in_background(self, reason: MemoryTerminationReason) -> None:
        self.ended = True
        create_diagnostic_task(
            self.close(reason),
            feature="runtime",
            event_code="memory.host_job_settle_failed",
            failure_message="A host memory group could not be settled; it remains charged for reconciliation.",
            name="memory-host-settle-" + self.job_id,
        )

    async def _sample(self) -> None:
        from .host_memory_client import host_evidence

        while True:
            await asyncio.sleep(0.7)
            report = await self.runner.observe(self.job_id)
            if report is None:
                # The runner restarted or the group vanished; nothing can run in
                # it any more. Verify removal and settle the job. An operator
                # stop removes the group on purpose, so it settles as cancelled.
                current = await self.controller.service.get_job(self.job_id)
                self._settle_in_background(
                    "cancelled"
                    if (current.labels or {}).get("cancel_requested")
                    else "worker_lost"
                )
                return
            if type(report.get("peak_bytes")) is int:
                self._peak = report["peak_bytes"]
            if report.get("oom_kill_count"):
                # Positive kernel evidence for this group ends the whole worker,
                # as the OCI adapter does. The next command starts a new job.
                self._settle_in_background("memory_limit_exceeded")
                return
            try:
                await self.controller.service.observe(
                    self.job_id,
                    host_evidence(
                        report,
                        self.job_id,
                        termination_reason="running",
                        detail="Kernel memory limits and usage observed on the protected host group.",
                    ),
                )
            except MemoryBudgetConflict:
                # diagnostic-expected: application/cleanup can race a usage sample.
                continue

    async def close(self, reason: MemoryTerminationReason = "completed") -> None:
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._close(reason))
        await asyncio.shield(self._closing)

    async def _close(self, reason: MemoryTerminationReason) -> None:
        from .host_memory_client import host_evidence

        self.ended = True
        if self.monitor is not None and not self.monitor.done():
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        service = self.controller.service
        try:
            report = await self.runner.remove(self.job_id)
            current = await service.get_job(self.job_id)
            if current.state == "finished":
                self.outcome = current.outcome
                return
            if current.evidence is None:
                await service.unstarted_cleanup(
                    self.job_id,
                    current.revision,
                    group_id=report["group_id"],
                    detail="Host memory group removed before any process started.",
                    verified_empty=True,
                )
                return
            oom = (
                type(report.get("oom_kill_count")) is int
                and report["oom_kill_count"] > 0
            )
            evidence = host_evidence(
                report,
                self.job_id,
                termination_reason="memory_limit_exceeded" if oom else reason,
                detail="Kernel reported a memory-limit kill in this host group; every descendant was removed."
                if oom
                else "Host memory group and every descendant were removed.",
                fallback_limits=current.evidence.applied,
                peak_bytes=self._peak or current.evidence.peak_bytes,
            )
            finished = await service.finish(self.job_id, current.revision, evidence)
            self.outcome = finished.outcome
        except BaseException:
            # diagnostic-expected: unverified cleanup keeps capacity charged.
            async def lost():
                latest = await service.get_job(self.job_id)
                if latest.state in {"starting", "running", "updating"}:
                    await service.worker_lost(self.job_id, latest.revision)

            await finish_cleanup(lost())
            raise
        finally:
            self.controller.job_runners.pop(self.job_id, None)


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
        reservation_owner = None

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
            birth = process_birth(os.getpid())
            if birth is None:
                raise MemoryEnforcementUnavailable(
                    "the launch owner's process identity is unavailable"
                )
            reservation_owner = MemoryLaunchOwner(
                process_id=os.getpid(),
                process_birth=birth,
                runner_fingerprint=runner_fingerprint(runner),
                process_origin=process_origin(),
            )
            deadline = asyncio.get_running_loop().time() + QUEUED_ADMISSION_SECONDS
            while job.state in {"queued", "blocked"}:
                job = await self.service.reserve(
                    job_id, capability, owner=reservation_owner
                )
                if job.state == "queued":
                    _admission_expired(deadline, job_id)
                    await asyncio.sleep(0.2)
            if job.state != "reserved":
                raise MemoryBudgetConflict(
                    "job already has an execution owner or a terminal result; explicit retries need a new job identity"
                )
            job = await self.service.claim_start(
                job_id,
                job.revision,
                owner=reservation_owner.model_copy(
                    update={"runner_identity": await runner_identity(runner)}
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
                elif current.state == "reserved" and reservation_owner is not None:
                    # Exact owner/revision checks prevent a losing controller
                    # from releasing another owner's pending reservation.
                    await self.service.release_prelaunch(
                        job_id,
                        current.revision,
                        owner=reservation_owner,
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
        project_id: str | None,
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

    async def open_host_job(
        self,
        runner,
        *,
        job_id: str,
        project_id: str | None,
        resource_kind: str,
        resource_id: str,
        labels: dict[str, Any] | None = None,
        on_queued: Callable[[HostCommandQueuePosition], Awaitable[None]] | None = None,
    ) -> HostMemoryJob:
        """Admit a protected Host job through the runner.

        A Host command (``resource_kind="host_command"``) waits in the shared
        fair queue: it checks its place with a read, writes only to claim the
        head or to heartbeat, reports its position through ``on_queued``, and
        stops waiting when the operator cancels it.
        """
        from .host_memory_client import host_evidence, host_group_id
        from .memory_recovery import runner_fingerprint, runner_identity
        from .process_identity import process_birth, process_origin

        capability = await runner.capabilities()
        job = await self.service.create_job(
            job_id,
            project_id=project_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            capabilities=capability,
            labels=labels,
        )
        if job.state == "blocked":
            raise MemoryEnforcementUnavailable(job.detail or capability.detail)
        birth = process_birth(os.getpid())
        if birth is None:
            raise MemoryEnforcementUnavailable(
                "the launch owner's process identity is unavailable"
            )
        owner = MemoryLaunchOwner(
            process_id=os.getpid(),
            process_birth=birth,
            runner_fingerprint=runner_fingerprint(runner),
            process_origin=process_origin(),
        )
        claimed = False
        started = False
        try:
            if resource_kind == HOST_COMMAND_KIND:
                job = await self._wait_in_host_command_queue(
                    job, capability, owner, on_queued
                )
            else:
                deadline = asyncio.get_running_loop().time() + QUEUED_ADMISSION_SECONDS
                while job.state in {"queued", "blocked"}:
                    job = await self.service.reserve(job_id, capability, owner=owner)
                    if job.state == "queued":
                        _admission_expired(deadline, job_id)
                        await asyncio.sleep(0.2)
            if job.state != "reserved":
                raise MemoryBudgetConflict(
                    "job already has an execution owner or a terminal result; explicit retries need a new job identity"
                )
            identity = await runner_identity(runner)
            job = await self.service.claim_start(
                job_id,
                job.revision,
                owner=owner.model_copy(update={"runner_identity": identity}),
            )
            claimed = True
            # The group name derives from the job identity, so recovery can find
            # and remove it even if this process dies while it is being created.
            job = await self.service.groups_created(
                job_id,
                job.revision,
                MemoryLaunchGroups(group_id=host_group_id(job_id)),
            )
            report = await runner.create(job_id, job.snapshot.limits)
            evidence = host_evidence(
                report,
                job_id,
                termination_reason="running",
                detail="Kernel limits verified on the protected host group before any process started.",
            )
            if await runner_identity(runner) != identity:
                raise MemoryEnforcementUnavailable(
                    "Runner authority changed during group creation; the reservation remains charged"
                )
            job = await self.service.started(job_id, job.revision, evidence)
            started = True
            self.job_runners[job_id] = runner
            handle = HostMemoryJob(self, runner, job_id)
            handle.start_monitor()
            return handle
        except BaseException:
            # diagnostic-expected: every failed admission settles or stays charged.
            async def cleanup():
                current = await self.service.get_job(job_id)
                if current.state in {"queued", "blocked"}:
                    await self.service.cancel_not_started(
                        job_id, current.revision, actor_id="system"
                    )
                elif current.state == "reserved":
                    await self.service.release_prelaunch(
                        job_id, current.revision, owner=owner
                    )
                elif claimed and not started:
                    await HostMemoryJob(self, runner, job_id).close("launch_failed")

            await finish_cleanup(cleanup())
            raise

    async def _wait_in_host_command_queue(self, job, capability, owner, on_queued):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HOST_COMMAND_QUEUE_SECONDS
        touched = loop.time()
        reported = None
        while True:
            if job.state == "blocked":
                job = await self.service.reserve(job.id, capability, owner=owner)
            position = await self.service.host_command_position(job.id)
            if position.state == "finished":
                raise HostCommandCancelled(
                    "Cancelled by the operator while queued; it never started."
                )
            if position.state != "queued":
                return await self.service.get_job(job.id)
            if position.ahead == 0 and position.running < position.limit:
                job = await self.service.reserve(job.id, capability, owner=owner)
                touched = loop.time()
                if job.state != "queued":
                    return job
            elif loop.time() - touched >= QUEUE_HEARTBEAT_SECONDS:
                await self.service.touch_queued(job.id)
                touched = loop.time()
            if on_queued is not None and (
                reported is None
                or (reported.ahead, reported.running, reported.limit)
                != (position.ahead, position.running, position.limit)
            ):
                reported = position
                await on_queued(position)
            if loop.time() >= deadline:
                raise MemoryAdmissionTimeout(
                    f"Waited {HOST_COMMAND_QUEUE_SECONDS / 60:g} minutes in the Host "
                    "command queue without a free slot; it was not started. "
                    "Try again, or raise Host commands running at once."
                )
            await asyncio.sleep(0.5)

    async def reject_unsupported(
        self,
        environment: ExecutionEnvironment,
        *,
        project_id: str | None,
        resource_kind: str,
        resource_id: str,
        detail: str,
    ) -> NoReturn:
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
        if hasattr(runner, "memory_fingerprint"):
            return await self._apply_running_host(runner, current, request, actor_id)
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

    async def _apply_running_host(self, runner, current, request, actor_id: str):
        from .host_memory_client import host_evidence, host_limits

        job_id = current.id
        capability = await runner.capabilities()
        if current.evidence is None:
            raise MemoryBudgetConflict(
                "only a verified running job can receive new limits"
            )
        before = await runner.observe(job_id)
        if before is None or host_limits(before) != current.evidence.applied:
            raise MemoryEnforcementUnavailable(
                "The owning group or its applied limits changed. Read this job before retrying."
            )
        pending = await self.service.request_running_update(
            job_id, request, capability, actor_id=actor_id
        )
        try:
            detail = "Runner did not apply the requested limits."
            try:
                await runner.update(job_id, request.limits)
            except MemoryEnforcementUnavailable as error:
                if getattr(error, "code", None) != "rejected":
                    raise
                detail = str(error)
            report = await runner.observe(job_id)
            if report is None:
                raise MemoryEnforcementUnavailable(
                    "Application is unverified. Capacity remains reserved; inspect this job before retrying."
                )
            observed = host_evidence(
                report,
                job_id,
                termination_reason="running",
                detail="Kernel limits read back from the protected host group.",
            )
            if observed.applied == request.limits:
                return await self.service.applied_running_update(
                    job_id, pending.revision, observed, actor_id=actor_id
                )
            if observed.applied == current.evidence.applied:
                return await self.service.failed_running_update(
                    job_id,
                    pending.revision,
                    observed,
                    detail=detail[-1000:],
                    actor_id=actor_id,
                )
            raise MemoryEnforcementUnavailable(
                "Application is unverified. Capacity remains reserved; inspect this job before retrying."
            )
        except BaseException as error:
            # A cancellation or lost response cannot roll back a kernel change.
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
