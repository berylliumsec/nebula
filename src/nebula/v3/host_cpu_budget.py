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

ApplyState = Literal["inactive", "applied", "pending", "failed", "unavailable"]


class HostCpuPolicy(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    cpu_limit: float | None = Field(
        default=None, ge=0.01, allow_inf_nan=False, strict=True
    )


class HostCpuUpdate(NebulaModel):
    host_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
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
    ):
        if (apply is None) != (observe is None):
            raise ValueError("CPU budget application requires an observer")
        self.path = path
        self.capacity = capacity
        self.apply = apply
        self.observe = observe
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
            policy = HostCpuPolicy(host_id=uuid4().hex, revision=1)
            self._write(policy)
            return policy
        return HostCpuPolicy.model_validate(
            json.loads(self.path.read_text(encoding="utf-8"))
        )

    async def _status(self, policy: HostCpuPolicy, capacity: float) -> HostCpuStatus:
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
                    "host_cpu",
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
        if self.apply is not None:
            try:
                await self.apply(
                    min(policy.cpu_limit or max(0.01, capacity / 2), capacity)
                )
            except Exception as exc:
                self._application_failed = True
                record_caught_exception(
                    "host_cpu",
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
            saved = HostCpuPolicy(
                host_id=policy.host_id,
                revision=policy.revision + 1,
                cpu_limit=request.cpu_limit,
            )
            await asyncio.to_thread(self._write, saved)
            await self._apply(saved, capacity)
            return await self._status(saved, capacity)
