"""Supervisor-owned recovery of compute reservations after an execution loss."""

from __future__ import annotations

import asyncio

from .async_utils import finish_cleanup
from .diagnostics import record_caught_exception
from .domain import RunnerProfile
from .memory_budget import MemoryBudgetService
from .memory_policy import MemoryEnforcementUnavailable
from .memory_recovery import MemoryReconciler
from .runner_profiles import container_runner_profile
from .sandbox import ContainerSandboxRunner
from .storage import NebulaStore


class HostMemoryRecovery:
    def __init__(self, host, *, budgets=None, runner_factory=None, interval=5):
        if not 0.01 <= interval <= 60:
            raise ValueError("Host memory recovery requires bounded polling")
        self.host = host
        self.budgets = budgets or MemoryBudgetService(host.coordinator.database)
        if self.budgets.database is not host.coordinator.database:
            raise ValueError("Host memory recovery must use the supervisor database")
        self.store = NebulaStore(host.coordinator.database)
        self.runner_factory, self.interval = runner_factory, interval
        self.reconciler = MemoryReconciler(self.budgets, self.resolve)
        self._stop = asyncio.Event()
        self.task = None
        self._close_task = None
        self.closed = False
        self.state = "not_started"

    async def resolve(self, job):
        profile_id = job.snapshot.profile_id
        if profile_id is None:
            raise MemoryEnforcementUnavailable(
                "Reconnect this job's original execution profile before verifying cleanup"
            )
        profile = await self.store.get(RunnerProfile, profile_id)
        return self.runner_for_profile(profile)

    def runner_for_profile(self, profile):
        # Disabled profiles still describe an old job's cleanup authority. No
        # workload is launched here. The reconciler additionally checks the
        # original process namespace, fingerprint, daemon and immutable groups.
        if self.runner_factory is not None:
            return self.runner_factory(profile)
        return ContainerSandboxRunner(
            profile=container_runner_profile(profile),
            workspace_roots=[self.host.data_root],
            memory_profile_id=profile.id,
        )

    async def start(self):
        if self.closed:
            raise RuntimeError("Create new host memory recovery after detachment")
        owner = self.host.shared_services
        if (
            self.host.closed
            or self.host.lock.descriptor is None
            or owner is None
            or owner.recovery is not self
        ):
            raise RuntimeError(
                "Memory recovery requires the exclusive host-services owner"
            )
        if self.task is None:
            self.task = asyncio.create_task(
                self._loop(), name="nebula-host-memory-recovery"
            )

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.reconciler.once()
                self.state = "ready"
            except Exception as error:
                self.state = "degraded"
                record_caught_exception(
                    "runtime",
                    "memory.host_recovery_waiting",
                    "Lost jobs remain charged while their cleanup authority is unavailable.",
                    error,
                    stage="memory-reconciliation",
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
        self.state = "closed"

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._stop.set()

            async def cleanup():
                # Do not cancel an accepted daemon removal or SQL outcome.
                # Join it before another supervisor receives the host lock.
                if self.task is not None:
                    await self.task
                await self.store.event_history.close()
                self.state = "closed"

            self._close_task = asyncio.create_task(
                cleanup(), name="nebula-host-memory-recovery-close"
            )

        async def join():
            await self._close_task

        await finish_cleanup(join())
