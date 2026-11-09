"""Host-owned PDF workers with observed Linux affinity and draining changes."""

from __future__ import annotations

from .async_utils import MaybeAsync

from .async_utils import async_done_callback, call_async

import asyncio
from collections.abc import Callable
import math
import os
import sys
from typing import TYPE_CHECKING, Any, TypeVar

from .cpu_workers import CpuWorkerBusy, CpuWorkerClosed, CpuWorkerPool
from .diagnostics import create_diagnostic_task, record_caught_exception

_T = TypeVar("_T")
if TYPE_CHECKING:
    from .memory_runtime import MemoryComputeController


class ReportCpuExecutor:
    """One bounded pool for every project's PDFs; never owns application state."""

    def __init__(self, *, pool_factory: Callable[..., CpuWorkerPool] = CpuWorkerPool):
        self.pool_factory = pool_factory
        self.pool: CpuWorkerPool | None = None
        self.enabled = False
        self.limit = 0
        self.state = "disabled"
        self.error: str | None = None
        self._accepting = False
        self._closed = False
        self._transition: asyncio.Task | None = None
        self._legacy: set[asyncio.Task] = set()
        self.memory_compute: MemoryComputeController | None = None

    @staticmethod
    def support_reason() -> str | None:
        if sys.platform != "linux" or not hasattr(os, "sched_setaffinity"):
            return "Background PDF workers require Linux CPU affinity support."
        return None

    @staticmethod
    def limits(capacity: float, requested: float | None) -> tuple[float, float, int]:
        maximum = min(4, math.floor(capacity))
        automatic = min(maximum, max(1, math.floor(capacity / 2)))
        effective = min(maximum, math.floor(requested or automatic))
        return float(automatic), float(effective), maximum

    def validate(self, enabled: bool, limit: float) -> None:
        if enabled:
            reason = self.support_reason()
            if reason:
                raise ValueError(reason)
            if limit < 1:
                raise ValueError(
                    "Background PDF workers need at least one available CPU."
                )

    async def start(self) -> None:
        # HostCpuBudget restores the durable policy; report startup alone must
        # never activate workers or reset an operator's requested state.
        return None

    async def request(self, enabled: bool, limit: float) -> None:
        if self._closed:
            raise CpuWorkerClosed("Background PDF workers are shutting down")
        self.validate(enabled, limit)
        desired = (enabled, int(limit))
        current = (self.enabled, self.limit)
        self.enabled, self.limit = desired
        broken = self.pool is not None and self.pool.snapshot()["broken"]
        if desired == current and not broken and self.state in {"running", "disabled"}:
            return
        self._accepting = False
        self.error = None
        self.state = "draining" if self.pool is not None or self._legacy else "starting"
        if self._transition is None or self._transition.done():
            self._transition = create_diagnostic_task(
                self._reconfigure(),
                feature="reports",
                event_code="reports.cpu_activation.transition_failed",
                failure_message="Background PDF worker activation stopped unexpectedly.",
                name="report-cpu-configuration",
            )

    async def _reconfigure(self) -> None:
        try:
            while not self._closed:
                if self._legacy:
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in self._legacy),
                        return_exceptions=True,
                    )
                if self.pool is not None:
                    if self.enabled and self.limit:
                        cpus = tuple(sorted(os.sched_getaffinity(0)))[: self.limit]
                        await self.pool.set_affinity(cpus)
                    await self.pool.drain()
                    await self._stop_pool()
                if self._closed:
                    return
                if not self.enabled:
                    self.state = "disabled"
                    return
                desired = (self.enabled, self.limit)
                cpus = tuple(sorted(os.sched_getaffinity(0)))[: self.limit]
                if len(cpus) != self.limit:
                    raise ValueError(
                        "The host's available CPUs changed; reload the budget."
                    )
                self.state = "starting"
                self.pool = self.pool_factory(workers=self.limit, cpu_affinity=cpus)
                await self.pool.start()
                if (self.enabled, self.limit) != desired:
                    self.state = "draining"
                    continue
                self.state = "running"
                self._accepting = True
                return
        except asyncio.CancelledError:
            # diagnostic-expected: Core shutdown owns and closes the pool below.
            raise
        except Exception as exc:
            self.state = "failed"
            self._accepting = False
            self.error = "Background PDF workers could not apply the setting. Save again to retry."
            record_caught_exception(
                "reports",
                "reports.cpu_activation.apply_failed",
                self.error,
                exc,
                stage="cpu-activation",
            )

    async def run(
        self,
        function: Callable[..., _T],
        /,
        *arguments: Any,
        input_bytes: int = 0,
        on_start: (MaybeAsync[[], None] | None) = None,
    ) -> _T:
        if self.memory_compute is not None:
            from .memory_runtime import compute_context
            from uuid import uuid4

            context = compute_context.get()
            await self.memory_compute.reject_unsupported(
                "cpu-worker",
                project_id=context.project_id if context else None,
                resource_kind="report_pdf",
                resource_id=context.resource_id if context else uuid4().hex,
                detail="PDF computation requires a memory-managed CPU runner. The host thread and affinity-only worker fallbacks are disabled; configure a supported runner before explicitly retrying this report.",
            )
        if self._closed:
            raise CpuWorkerClosed("Background PDF workers are shutting down")
        if self.state == "disabled" and not self.enabled:
            if on_start is not None:
                await call_async(on_start)
            task = create_diagnostic_task(
                asyncio.to_thread(function, *arguments),
                feature="reports",
                event_code="reports.cpu_activation.legacy_failed",
                failure_message="Standard PDF computation failed after dispatch.",
                name="report-cpu-legacy",
            )
            self._legacy.add(task)
            task.add_done_callback(async_done_callback(self._legacy.discard))
            return await asyncio.shield(task)
        if not self._accepting or self.pool is None:
            raise CpuWorkerBusy(
                "Background PDF workers are changing or unavailable; retry the saved report."
            )
        return await self.pool.run(
            function, *arguments, input_bytes=input_bytes, on_start=on_start
        )

    async def observe(self) -> dict[str, Any]:
        pool = self.pool
        snapshot = pool.snapshot() if pool is not None else {}
        workers = snapshot.get("workers", [])
        state = self.state
        if snapshot.get("broken"):
            state = "failed"
        live = [worker for worker in workers if worker["alive"]]

        def affinity() -> list[list[int]]:
            return [sorted(os.sched_getaffinity(worker["pid"])) for worker in live]

        affinities = await asyncio.to_thread(affinity) if live else []
        observed = len({cpu for group in affinities for cpu in group}) or None
        if state == "running" and (
            len(live) != self.limit or any(len(group) != 1 for group in affinities)
        ):
            state = "starting"
        return {
            "executor_state": state,
            "worker_count": len(live),
            "active_jobs": snapshot.get("active", 0) + len(self._legacy),
            "queued_jobs": snapshot.get("queued", 0),
            "applied_cpu_limit": float(observed) if observed is not None else None,
            "worker_pids": [worker["pid"] for worker in live],
            "worker_affinities": affinities,
            "error": self.error,
        }

    async def _stop_pool(self) -> None:
        if self.pool is not None:
            await self.pool.shutdown()
            if any(worker["alive"] for worker in self.pool.snapshot()["workers"]):
                raise CpuWorkerClosed("Background PDF worker teardown did not finish")
            self.pool = None

    async def shutdown(self) -> None:
        self._closed = True
        self._accepting = False
        if self._transition is not None and not self._transition.done():
            self._transition.cancel()
            await asyncio.gather(self._transition, return_exceptions=True)
        await self._stop_pool()
        if self._legacy:
            await asyncio.gather(*self._legacy, return_exceptions=True)
        self.state = "disabled"
