"""Cached, cooperative host admission using bounded standard-library reads."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
import math
import os
from pathlib import Path
import threading
import time

from pydantic import Field

from .domain import NebulaModel
from .diagnostics import create_diagnostic_task


class HostPressureThresholds(NebulaModel):
    cpu: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False, strict=True)
    memory: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False, strict=True)
    load: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False, strict=True)
    io: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False, strict=True)


@dataclass(frozen=True)
class HostLoadSnapshot:
    sampled_at: float
    cpu_capacity: float
    cpu_ratio: float | None
    memory_available_ratio: float | None
    busy: bool
    reason: str | None
    measurements_available: bool

    load_average_ratio: float | None = None
    io_pressure_ratio: float | None = None

    pressure_metric: str | None = None
    runnable_ratio: float | None = None
    host_io_pressure_ratio: float | None = None

    def readings(self) -> dict[str, float | None]:
        return {
            "cpu": self.cpu_ratio,
            "memory": None
            if self.memory_available_ratio is None
            else 1 - self.memory_available_ratio,
            "load": self.runnable_ratio,
            "io": self.io_pressure_ratio,
        }

    def evaluate(self, thresholds: HostPressureThresholds) -> HostLoadSnapshot:
        labels = {
            "cpu": "CPU use",
            "memory": "memory use",
            "load": "runnable tasks",
            "io": "Core I/O stalls",
        }
        for metric, value in self.readings().items():
            limit = getattr(thresholds, metric)
            # 100% turns a check off. CPU use reads 100% on a saturated host and
            # runnable tasks per CPU has no ceiling, so no lower value would.
            if limit >= 1:
                continue
            if value is not None and math.isfinite(value) and value >= limit:
                # Waiting work saves this text, so it names the metric and the
                # threshold only: a live reading would rewrite every queued
                # turn each time it moved. Readings stay in the sample.
                return replace(
                    self,
                    busy=True,
                    pressure_metric=metric,
                    reason=f"Waiting for host pressure: {labels[metric]} at or above {limit:.0%}",
                )
        return (
            self
            if not self.busy and self.reason is None and self.pressure_metric is None
            else replace(self, busy=False, reason=None, pressure_metric=None)
        )

    def public(self) -> dict[str, object]:
        result = asdict(self)
        result.pop("sampled_at")
        result["sample_age_seconds"] = round(
            max(0, time.monotonic() - self.sampled_at), 3
        )
        return result


class HostLoadMonitor:
    """Cache one sample per second; optional missing counters remain unknown."""

    def __init__(
        self,
        *,
        interval: float = 1.0,
        proc: Path = Path("/proc"),
        cgroup: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("host sampling interval must be positive")
        self.interval, self.proc, self.cgroup, self.clock = (
            interval,
            proc,
            cgroup or Path("/sys/fs/cgroup"),
            clock,
        )
        self._cached: HostLoadSnapshot | None = None
        self._lock = threading.Lock()
        self._previous_cpu: tuple[float, float] | None = None
        self.thresholds = HostPressureThresholds()
        self._previous_host_cpu: tuple[float, float] | None = None
        self._io_cgroup = cgroup if cgroup is not None else self._own_cgroup(proc)

    @classmethod
    def _own_cgroup(cls, proc: Path) -> Path | None:
        membership = cls._text(proc / "self/cgroup")
        if not membership:
            return None
        for line in membership.splitlines():
            if not line.startswith("0::/"):
                continue
            parts = Path(line[3:]).parts
            if not parts or parts == ("/",) or ".." in parts:
                return None
            return Path("/sys/fs/cgroup").joinpath(*parts[1:])
        return None

    def _io_psi(self, path: Path) -> float | None:
        pressure = self._text(path)
        if not pressure:
            return None
        try:
            fields = dict(
                item.split("=") for item in pressure.splitlines()[0].split()[1:]
            )
            value = float(fields["avg10"]) / 100
            return value if math.isfinite(value) and value >= 0 else None
        except (ValueError, KeyError, IndexError):
            # diagnostic-expected: malformed optional cgroup I/O PSI sample.
            return None

    def cached(self) -> HostLoadSnapshot | None:
        value = self._cached
        return (
            value
            if value is not None and self.clock() - value.sampled_at < self.interval
            else None
        )

    @staticmethod
    def _text(path: Path) -> str | None:
        try:
            with path.open() as source:
                return source.read(8192)
        except (OSError, UnicodeError):
            # diagnostic-expected: optional OS/cgroup counter unavailable.
            return None

    def sample(self) -> HostLoadSnapshot:
        value = self.cached()
        if value is not None:
            return value
        with self._lock:
            value = self.cached()
            if value is not None:
                return value
            now = self.clock()
            capacity = float(os.cpu_count() or 1)
            try:
                capacity = min(capacity, float(len(os.sched_getaffinity(0))))
            except (AttributeError, OSError):
                # diagnostic-expected: affinity is optional outside Linux.
                pass
            host_capacity = capacity
            quota = self._text(self.cgroup / "cpu.max")
            if quota:
                parts = quota.split()
                if len(parts) == 2 and parts[0] != "max":
                    try:
                        capacity = min(
                            capacity, max(0.01, float(parts[0]) / float(parts[1]))
                        )
                    except (ValueError, ZeroDivisionError):
                        # diagnostic-expected: optional kernel counter unavailable.
                        pass
            ratio: float | None = None
            usage = self._text(self.cgroup / "cpu.stat")
            if usage:
                counters = dict(
                    line.split()
                    for line in usage.splitlines()
                    if len(line.split()) == 2
                )
                try:
                    used = float(counters["usage_usec"]) / 1_000_000
                    if self._previous_cpu is not None:
                        previous_at, previous_used = self._previous_cpu
                        if now > previous_at and used >= previous_used:
                            ratio = (used - previous_used) / (
                                (now - previous_at) * capacity
                            )
                    self._previous_cpu = (now, used)
                except (KeyError, ValueError):
                    # diagnostic-expected: cgroup CPU accounting unavailable.
                    pass
            # Host load includes unrelated uninterruptible I/O waits. Report it,
            # but admit on runnable tasks instead.
            average = None
            try:
                average = os.getloadavg()[0] / capacity
            except (AttributeError, OSError):
                # diagnostic-expected: load average unavailable on this platform.
                pass
            stat = self._text(self.proc / "stat")
            runnable_ratio = None
            if stat:
                try:
                    cpu_fields = [
                        float(value) for value in stat.splitlines()[0].split()[1:9]
                    ]
                    total = sum(cpu_fields)
                    idle = cpu_fields[3] + cpu_fields[4]  # idle plus iowait
                    if self._previous_host_cpu is not None:
                        old_total, old_idle = self._previous_host_cpu
                        if total > old_total and idle >= old_idle:
                            host_ratio = 1 - (idle - old_idle) / (total - old_total)
                            ratio = (
                                host_ratio if ratio is None else max(ratio, host_ratio)
                            )
                    self._previous_host_cpu = (total, idle)
                except (ValueError, IndexError):
                    # diagnostic-expected: optional host CPU accounting unavailable.
                    pass
                for line in stat.splitlines():
                    if line.startswith("procs_running "):
                        try:
                            runnable_ratio = float(line.split()[1]) / host_capacity
                        except (ValueError, IndexError, ZeroDivisionError):
                            # diagnostic-expected: optional runnable count unavailable.
                            pass
                        break
            io_ratio = (
                self._io_psi(self._io_cgroup / "io.pressure")
                if self._io_cgroup is not None
                else None
            )
            host_io_ratio = self._io_psi(self.proc / "pressure/io")
            memory: float | None = None
            info = self._text(self.proc / "meminfo")
            if info:
                fields = {
                    line.split(":", 1)[0]: line.split(":", 1)[1].strip().split()[0]
                    for line in info.splitlines()
                    if ":" in line and line.split(":", 1)[1].strip()
                }
                try:
                    memory = float(fields["MemAvailable"]) / float(fields["MemTotal"])
                except (KeyError, ValueError, ZeroDivisionError):
                    # diagnostic-expected: optional memory accounting unavailable.
                    pass
            limit = self._text(self.cgroup / "memory.max")
            current = self._text(self.cgroup / "memory.current")
            if limit and current and limit.strip() != "max":
                try:
                    available = max(0.0, 1 - float(current) / float(limit))
                    memory = available if memory is None else min(memory, available)
                except (ValueError, ZeroDivisionError):
                    # diagnostic-expected: optional memory accounting unavailable.
                    pass
            if ratio is not None and not math.isfinite(ratio):
                ratio = None
            if memory is not None and not math.isfinite(memory):
                memory = None
            self._cached = HostLoadSnapshot(
                now,
                capacity,
                ratio,
                memory,
                False,
                None,
                any(
                    value is not None
                    for value in (ratio, memory, runnable_ratio, io_ratio)
                ),
                average,
                io_ratio,
                runnable_ratio=runnable_ratio,
                host_io_pressure_ratio=host_io_ratio,
            ).evaluate(self.thresholds)
            return self._cached


class HostLoadGate:
    """Shared, live pressure policy; FIFO waiters never own runtime slots."""

    def __init__(self, monitor: HostLoadMonitor | None = None) -> None:
        self.monitor = monitor or HostLoadMonitor()
        self._refresh: asyncio.Task[HostLoadSnapshot] | None = None
        self._waiters: deque[object] = deque()
        self.thresholds = self.monitor.thresholds
        self._changed = asyncio.Event()

    def wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def snapshot(self) -> HostLoadSnapshot:
        cached = self.monitor.cached()
        if cached is not None:
            return cached.evaluate(self.thresholds)
        loop = asyncio.get_running_loop()
        if (
            self._refresh is None
            or self._refresh.done()
            or self._refresh.get_loop() is not loop
        ):
            self._refresh = create_diagnostic_task(
                asyncio.to_thread(self.monitor.sample),
                feature="host_load",
                event_code="host_load.refresh",
                failure_message="Host capacity sampling failed.",
                name="nebula-host-load-refresh",
            )
        return (await asyncio.shield(self._refresh)).evaluate(self.thresholds)

    async def wait(
        self,
        on_deferred: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> None:
        ticket = object()
        self._waiters.append(ticket)
        try:
            await self._wait(ticket, on_deferred)
        finally:
            self._waiters.remove(ticket)
            self.wake()

    async def _wait(
        self,
        ticket: object,
        on_deferred: Callable[[str | None], Awaitable[None]] | None,
    ) -> None:
        reason: str | None = None
        while True:
            changed = self._changed
            snapshot = await self.snapshot()
            if not snapshot.busy and self._waiters[0] is ticket:
                if reason is not None and on_deferred is not None:
                    await on_deferred(None)
                return
            next_reason = snapshot.reason or "Waiting for host start capacity"
            if next_reason != reason and on_deferred is not None:
                await on_deferred(next_reason)
            reason = next_reason
            try:
                await asyncio.wait_for(changed.wait(), timeout=self.monitor.interval)
            except TimeoutError:
                # diagnostic-expected: refresh OS readings on the next sampling tick.
                pass
