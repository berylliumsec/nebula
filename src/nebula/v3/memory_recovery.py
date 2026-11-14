"""Reconcile dead launch owners without treating a missing heartbeat as cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
from typing import Any

from .diagnostics import create_diagnostic_task, record_caught_exception
from .domain import utc_now
from .memory_budget import MemoryBudgetService, MemoryJob
from .memory_policy import (
    MemoryBudgetConflict,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
)
from .process_identity import (
    launch_owner_ended,
    origin_boot,
    origin_from_previous_boot,
    process_origin,
)


def runner_fingerprint(runner) -> str:
    if runner.profile is None:
        raise MemoryEnforcementUnavailable(
            "the cleanup runner has no explicit identity"
        )
    return hashlib.sha256(
        json.dumps(
            runner.profile.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def runner_identity(runner) -> str | None:
    """Docker supplies a persistent daemon ID; other agents must supply one."""
    if runner.profile.runtime_type.value != "docker":
        return None
    output, error, code = await runner._capture("info", "--format", "{{json .}}")
    if code:
        raise MemoryEnforcementUnavailable(
            "the original runner authority cannot be verified"
        )
    identity = json.loads(output).get("ID")
    if not isinstance(identity, str) or not 8 <= len(identity) <= 200:
        return None
    return "docker:" + identity


class MemoryReconciler:
    def __init__(
        self,
        service: MemoryBudgetService,
        resolve_runner: Callable[[MemoryJob], Awaitable[Any]],
        *,
        previous_boot_ended: Callable[[str], Awaitable[bool]] | None = None,
    ):
        """`previous_boot_ended(boot)` must confirm that no runtime born on that
        boot still renews a lease; without it, owners from an earlier boot are
        left to a reconciler that can check (the supervisor's)."""
        self.service, self.resolve_runner = service, resolve_runner
        self.previous_boot_ended = previous_boot_ended
        self.task: asyncio.Task | None = None
        self.cursor = ""

    async def start(self):
        if self.task is None:
            self.task = create_diagnostic_task(
                self._loop(),
                feature="runtime",
                event_code="memory.reconciliation.failed",
                failure_message="Lost-worker cleanup stopped; unknown groups remain charged.",
                name="memory-reconciliation",
            )

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _loop(self):
        while True:
            await self.once()
            await asyncio.sleep(5)

    async def once(self) -> list[str]:
        jobs = await self.service.reconciliation_jobs(after=self.cursor)
        self.cursor = jobs[-1].id if len(jobs) == 64 else ""
        released = []
        for job in jobs:
            try:
                if await self._reconcile(job):
                    released.append(job.id)
            except MemoryBudgetConflict:
                # diagnostic-expected: a concurrent reconciler committed first.
                continue
            except Exception as error:
                # A failed probe never proves that the group is empty. Keep the
                # durable charge and retry independently of other lost jobs.
                record_caught_exception(
                    "runtime",
                    "memory.reconciliation.held",
                    "A lost job remains charged because cleanup could not be verified.",
                    error,
                    stage="reconciliation",
                )
        return released

    async def _reconcile(self, job: MemoryJob) -> bool:
        owner, groups = job.launch_owner, job.launch_groups
        if owner is None or not launch_owner_ended(
            owner.process_origin, owner.process_id, owner.process_birth
        ):
            return False
        if origin_from_previous_boot(owner.process_origin, process_origin()):
            # Its kernel is gone on this host, but a live lease from that boot
            # means another host sharing this database may still run it.
            boot = origin_boot(owner.process_origin)
            if (
                boot is None
                or self.previous_boot_ended is None
                or not await self.previous_boot_ended(boot)
            ):
                return False
        if job.state == "reserved" and groups is None and job.evidence is None:
            # No external create can precede the committed STARTING transition.
            # Recheck the exact owner and revision inside the release transaction.
            await self.service.release_prelaunch(
                job.id, job.revision, owner=owner, owner_dead=True
            )
            return True
        # Before group creation is acknowledged the daemon may still accept an
        # in-flight create request. Absence alone cannot free that reservation.
        if groups is None:
            if job.state != "uncertain":
                await self.service.worker_lost(job.id, job.revision)
            return False
        runner = await self.resolve_runner(job)
        if runner_fingerprint(runner) != owner.runner_fingerprint:
            raise MemoryEnforcementUnavailable(
                "the owning runner configuration changed; retain the reservation"
            )
        healthy, detail = await runner.available()
        if not healthy:
            raise MemoryEnforcementUnavailable(detail)
        if (
            owner.runner_identity is None
            or await runner_identity(runner) != owner.runner_identity
        ):
            raise MemoryEnforcementUnavailable(
                "the owning daemon identity is unavailable or changed; retain the reservation"
            )
        job = await self.service.worker_lost(job.id, job.revision)
        raw, error, code = await runner._capture(
            "inspect", groups.group_id, "--format", "{{json .}}"
        )
        document = None
        if code:
            if "no such" not in (raw + error).lower():
                raise MemoryEnforcementUnavailable(
                    "the owning group cannot be inspected"
                )
        else:
            document = json.loads(raw)
            if (
                document.get("Id", document.get("ID")) != groups.group_id
                or document.get("Config", {}).get("Labels", {}).get("nebula.memory.job")
                != job.id
            ):
                raise MemoryEnforcementUnavailable(
                    "the group does not belong to this lost job"
                )
        old = job.evidence
        limits = old.applied if old else job.snapshot.limits
        drift = False
        if document is not None:
            config = document.get("HostConfig", {})
            maximum, combined = config.get("Memory"), config.get("MemorySwap")
            if (
                type(maximum) is not int
                or type(combined) is not int
                or combined < maximum
            ):
                raise MemoryEnforcementUnavailable(
                    "the lost group's limits cannot be verified"
                )
            observed = MemoryLimits(max_bytes=maximum, swap_bytes=combined - maximum)
            if observed not in (
                old.applied if old else job.snapshot.limits,
                job.pending_limits,
            ):
                drift = True
            else:
                limits = observed
        await runner._force_remove(groups.group_id, require_removed=True)
        for helper_id in groups.auxiliary_group_ids:
            await runner._force_remove(helper_id, require_removed=True)
        if await runner_identity(runner) != owner.runner_identity:
            raise MemoryEnforcementUnavailable(
                "runner authority changed during cleanup; retain the reservation"
            )
        current = await self.service.get_job(job.id)
        if current.state == "finished":
            return True
        if old is None:
            await self.service.unstarted_cleanup(
                job.id,
                current.revision,
                group_id=groups.group_id,
                detail="Dead launch owner; unreleased group and helpers verified removed.",
                verified_empty=True,
            )
        else:
            oom = (
                document is not None
                and document.get("State", {}).get("OOMKilled") is True
            )
            await self.service.finish(
                job.id,
                current.revision,
                MemoryEnforcementEvidence(
                    application_uncertain=drift,
                    job_id=job.id,
                    group_id=groups.group_id,
                    applied=limits,
                    protected_process_tree=True,
                    group_empty=True,
                    auxiliary_group_ids=groups.auxiliary_group_ids,
                    auxiliary_groups_empty=True,
                    oom_kill_count=1 if oom else 0,
                    oom_victim_confirmed=oom,
                    peak_bytes=old.peak_bytes,
                    current_bytes=0,
                    observed_at=utc_now().isoformat(),
                    termination_reason="memory_limit_exceeded"
                    if oom
                    else "worker_lost",
                    detail=(
                        "Applied limits were uncertain; last verified limits retained. "
                        if drift
                        else ""
                    )
                    + "Dead launch owner; immutable workload and helper groups verified removed. Explicit retry required.",
                ),
            )
        return True
