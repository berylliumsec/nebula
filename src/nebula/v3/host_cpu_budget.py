"""Host-local CPU policy with revision checks and observed executor application."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import math
import os
from pathlib import Path
import socket
import tempfile
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .diagnostics import record_caught_exception
from .domain import NebulaModel
from .report_cpu_executor import ReportCpuExecutor

ApplyState = Literal["inactive", "applied", "pending", "failed", "unavailable"]


class HostCpuPolicy(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    enabled: bool | None = Field(default=None, strict=True)
    cpu_limit: float | None = Field(
        default=None, ge=0.01, allow_inf_nan=False, strict=True
    )


class HostCpuUpdate(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    enabled: bool | None = Field(default=None, strict=True)
    cpu_limit: float | None = Field(ge=0.01, allow_inf_nan=False, strict=True)


class HostCpuStatus(HostCpuPolicy):
    host_name: str
    cpu_capacity: float
    automatic_cpu_limit: float
    effective_cpu_limit: float
    applied_cpu_limit: float | None
    apply_state: ApplyState
    capacity_clamped: bool
    detail: str
    activation_supported: bool = False
    activation_unavailable_reason: str | None = (
        "This executor is managed outside Core Settings."
    )
    executor_scope: str = "external"
    executor_state: str = "unmanaged"
    worker_count: int | None = None
    active_jobs: int | None = None
    queued_jobs: int | None = None
    worker_pids: list[int] | None = None
    worker_affinities: list[list[int]] | None = None
    maximum_worker_cpus: int | None = None


class HostCpuConflict(ValueError):
    pass


class HostCpuBudget:
    """One Core coordinator owns this local file and optional executor boundary."""

    def __init__(
        self,
        path: Path,
        capacity: Callable[[], Awaitable[float]],
        *,
        apply: Callable[[float], Awaitable[None]] | None = None,
        observe: Callable[[], Awaitable[float]] | None = None,
        executor: ReportCpuExecutor | None = None,
    ):
        if (apply is None) != (observe is None):
            raise ValueError("CPU budget application requires an observer")
        if executor is not None and apply is not None:
            raise ValueError("Choose Core-managed workers or external quota callbacks")
        self.path = path
        self.capacity = capacity
        self.apply = apply
        self.observe = observe
        self.executor = executor
        self._lock = asyncio.Lock()
        self._application_failed = False

    def _write(self, policy: HostCpuPolicy) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".host-cpu-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(policy.model_dump_json() + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self) -> HostCpuPolicy:
        if not self.path.exists():
            policy = HostCpuPolicy(
                host_id=uuid4().hex,
                revision=1,
                enabled=False if self.executor is not None else None,
            )
            self._write(policy)
            return policy
        policy = HostCpuPolicy.model_validate(
            json.loads(self.path.read_text(encoding="utf-8"))
        )
        if self.executor is not None and policy.enabled is None:
            policy = policy.model_copy(update={"enabled": False})
        return policy

    async def _managed_status(
        self, policy: HostCpuPolicy, capacity: float
    ) -> HostCpuStatus:
        assert self.executor is not None
        automatic, target, maximum = self.executor.limits(capacity, policy.cpu_limit)
        reason = self.executor.support_reason()
        if maximum < 1:
            reason = "Background PDF workers need at least one available CPU."
        observed = None
        state: ApplyState
        facts = {}
        try:
            facts = await self.executor.observe()
            observed = facts["applied_cpu_limit"]
            executor_state = facts["executor_state"]
            if self._application_failed or executor_state == "failed":
                state, detail = (
                    "failed",
                    facts.get("error")
                    or "Background PDF workers could not apply the setting. Save again to retry.",
                )
            elif not policy.enabled and executor_state == "disabled":
                state, detail = "inactive", "Background PDF workers are disabled."
            elif policy.enabled and executor_state == "running" and observed == target:
                state, detail = (
                    "applied",
                    f"Running {facts['worker_count']} PDF workers on {observed:g} CPUs.",
                )
            else:
                state, detail = (
                    "pending",
                    "Draining accepted PDF work before applying the worker setting."
                    if executor_state == "draining"
                    else "Starting background PDF workers…",
                )
        except Exception as exc:
            record_caught_exception(
                "runtime",
                "host_cpu.workers_read_failed",
                "Background PDF workers could not be observed.",
                exc,
                stage="readback",
            )
            state, detail, executor_state = (
                "unavailable",
                "Worker status could not be checked. Reload to retry.",
                "unavailable",
            )
        return HostCpuStatus(
            **policy.model_dump(),
            host_name=socket.gethostname(),
            cpu_capacity=capacity,
            automatic_cpu_limit=automatic,
            effective_cpu_limit=target,
            applied_cpu_limit=observed,
            apply_state=state,
            capacity_clamped=policy.cpu_limit is not None
            and policy.cpu_limit != target,
            detail=detail,
            activation_supported=reason is None,
            activation_unavailable_reason=reason,
            executor_scope="report-pdfs",
            executor_state=executor_state,
            maximum_worker_cpus=maximum,
            worker_count=facts.get("worker_count"),
            active_jobs=facts.get("active_jobs"),
            queued_jobs=facts.get("queued_jobs"),
            worker_pids=facts.get("worker_pids"),
            worker_affinities=facts.get("worker_affinities"),
        )

    async def _status(self, policy: HostCpuPolicy, capacity: float) -> HostCpuStatus:
        if self.executor is not None:
            return await self._managed_status(policy, capacity)
        automatic = max(0.01, capacity / 2)
        target = min(policy.cpu_limit or automatic, capacity)
        observed: float | None = None
        state: ApplyState
        if self.observe is None:
            state = "inactive"
            detail = (
                "Saved on this Core host. The shared background executor is inactive."
            )
        else:
            try:
                observed = await self.observe()
                if not math.isfinite(observed) or observed <= 0:
                    raise ValueError("Invalid observed CPU quota")
            except Exception as exc:
                record_caught_exception(
                    "runtime",
                    "host_cpu.observe_failed",
                    "The background CPU quota could not be observed.",
                    exc,
                    stage="readback",
                )
                state = "unavailable"
                detail = "The executor quota could not be checked. Reload to retry."
            else:
                if math.isclose(observed, target, rel_tol=1e-4, abs_tol=1e-4):
                    state, detail = (
                        "applied",
                        "Applied to this host's shared background executor.",
                    )
                elif self._application_failed:
                    state, detail = (
                        "failed",
                        "Saved, but the executor did not apply this CPU budget. Retry saving.",
                    )
                else:
                    state, detail = (
                        "pending",
                        "Saved. The executor has a different CPU limit; save again to apply.",
                    )
        return HostCpuStatus(
            **policy.model_dump(),
            host_name=socket.gethostname(),
            cpu_capacity=capacity,
            automatic_cpu_limit=automatic,
            effective_cpu_limit=target,
            applied_cpu_limit=observed,
            apply_state=state,
            capacity_clamped=policy.cpu_limit is not None
            and policy.cpu_limit > capacity,
            detail=detail,
        )

    async def _capacity(self) -> float:
        value = await self.capacity()
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Host CPU capacity is unavailable")
        return value

    async def status(self) -> HostCpuStatus:
        async with self._lock:
            policy = await asyncio.to_thread(self._read)
            return await self._status(policy, await self._capacity())

    async def _apply(self, policy: HostCpuPolicy, capacity: float) -> None:
        self._application_failed = False
        if self.executor is not None or self.apply is not None:
            try:
                if self.executor is not None:
                    _, target, _ = self.executor.limits(capacity, policy.cpu_limit)
                    await self.executor.request(policy.enabled is True, target)
                else:
                    assert self.apply is not None
                    await self.apply(
                        min(policy.cpu_limit or max(0.01, capacity / 2), capacity)
                    )
            except Exception as exc:
                self._application_failed = True
                record_caught_exception(
                    "runtime",
                    "host_cpu.apply_failed",
                    "The saved CPU budget could not be applied to the background executor.",
                    exc,
                    stage="apply",
                )

    async def startup(self) -> None:
        async with self._lock:
            policy = await asyncio.to_thread(self._read)
            await self._apply(policy, await self._capacity())

    async def update(self, request: HostCpuUpdate) -> HostCpuStatus:
        async with self._lock:
            policy = await asyncio.to_thread(self._read)
            if (
                policy.host_id != request.host_id
                or policy.revision != request.expected_revision
            ):
                raise HostCpuConflict(
                    "This Core host or its CPU budget changed. Reload before saving."
                )
            capacity = await self._capacity()
            if request.cpu_limit is not None and request.cpu_limit > capacity:
                raise ValueError(
                    f"CPU limit exceeds this host's {capacity:g} available CPUs"
                )
            enabled = policy.enabled if request.enabled is None else request.enabled
            if self.executor is None and request.enabled is not None:
                raise ValueError(
                    "Activation is unavailable for this Core's externally managed executor."
                )
            if self.executor is not None:
                _, target, _ = self.executor.limits(capacity, request.cpu_limit)
                self.executor.validate(enabled is True, target)
            saved = HostCpuPolicy(
                host_id=policy.host_id,
                revision=policy.revision + 1,
                cpu_limit=request.cpu_limit,
                enabled=enabled,
            )
            await asyncio.to_thread(self._write, saved)
            await self._apply(saved, capacity)
            return await self._status(saved, capacity)
