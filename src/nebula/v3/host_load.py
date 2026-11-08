"""Cached, cooperative host admission using bounded standard-library reads."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import threading
import time

from .diagnostics import create_diagnostic_task


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
        cgroup: Path = Path("/sys/fs/cgroup"),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("host sampling interval must be positive")
        self.interval, self.proc, self.cgroup, self.clock = (
            interval,
            proc,
            cgroup,
            clock,
        )
        self._cached: HostLoadSnapshot | None = None
        self._lock = threading.Lock()
        self._previous_cpu: tuple[float, float] | None = None
        self._busy = False
        self._previous_host_cpu: tuple[float, float] | None = None

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
            # Linux load includes uninterruptible I/O waits; never call it CPU use.
            average = None
            try:
                average = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
            except (AttributeError, OSError):
                # diagnostic-expected: load average unavailable on this platform.
                pass
            stat = self._text(self.proc / "stat")
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
            io_ratio = None
            for path in (self.proc / "pressure/io", self.cgroup / "io.pressure"):
                pressure = self._text(path)
                if pressure:
                    try:
                        fields = dict(
                            item.split("=")
                            for item in pressure.splitlines()[0].split()[1:]
                        )
                        value = float(fields["avg10"]) / 100
                        if math.isfinite(value) and value >= 0:
                            io_ratio = (
                                value if io_ratio is None else max(io_ratio, value)
                            )
                    except (ValueError, KeyError, IndexError):
                        # diagnostic-expected: optional I/O pressure unavailable.
                        pass
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
            cpu_busy = ratio is not None and ratio >= (0.75 if self._busy else 0.90)
            memory_busy = memory is not None and memory < (0.15 if self._busy else 0.08)
            load_busy = average is not None and average >= (
                0.75 if self._busy else 0.90
            )
            io_busy = io_ratio is not None and io_ratio >= (
                0.10 if self._busy else 0.20
            )
            self._busy = cpu_busy or memory_busy or load_busy or io_busy
            self._cached = HostLoadSnapshot(
                now,
                capacity,
                ratio,
                memory,
                self._busy,
                "Waiting for host memory capacity"
                if memory_busy
                else "Waiting for host CPU capacity"
                if cpu_busy
                else "Waiting for host I/O capacity"
                if io_busy
                else "Waiting for host load capacity"
                if load_busy
                else None,
                any(value is not None for value in (ratio, memory, average, io_ratio)),
                average,
                io_ratio,
            )
            return self._cached


class HostLoadGate:
    """Shared refresh and start budget; cancellation never owns active work."""

    def __init__(self, monitor: HostLoadMonitor | None = None) -> None:
        self.monitor = monitor or HostLoadMonitor()
        self._refresh: asyncio.Task[HostLoadSnapshot] | None = None
        self._epoch: float | None = None
        self._starts = 0
        self._waiters: deque[tuple[object, bool]] = deque()
        self._last_pressure_start = float("-inf")

    async def snapshot(self) -> HostLoadSnapshot:
        cached = self.monitor.cached()
        if cached is not None:
            return cached
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
        return await asyncio.shield(self._refresh)

    async def wait(
        self,
        on_deferred: Callable[[str | None], Awaitable[None]] | None = None,
        *,
        direct: bool = False,
    ) -> None:
        ticket = object()
        self._waiters.append((ticket, direct))
        try:
            await self._wait(ticket, on_deferred, direct)
        finally:
            self._waiters.remove((ticket, direct))

    async def _wait(
        self,
        ticket: object,
        on_deferred: Callable[[str | None], Awaitable[None]] | None,
        direct: bool,
    ) -> None:
        reason: str | None = None
        while True:
            snapshot = await self.snapshot()
            if snapshot.sampled_at != self._epoch:
                self._epoch, self._starts = snapshot.sampled_at, 0
            budget = max(1, min(16, math.ceil(snapshot.cpu_capacity * 2)))
            headroom = (
                snapshot.cpu_ratio is not None
                and snapshot.cpu_ratio < 0.75
                and snapshot.memory_available_ratio is not None
                and snapshot.memory_available_ratio >= 0.15
                and snapshot.reason
                in {"Waiting for host I/O capacity", "Waiting for host load capacity"}
            )
            # Skip ineligible background waiters only during pressure. FIFO resumes
            # across all lanes after recovery; no waiter is removed or recreated.
            eligible = next(
                (
                    item
                    for item, lane in self._waiters
                    if not snapshot.busy or (lane and headroom)
                ),
                None,
            )
            now = self.monitor.clock()
            pressure_ready = now - self._last_pressure_start >= 1.0
            if (
                (not snapshot.busy or (direct and headroom and pressure_ready))
                and self._starts < budget
                and eligible is ticket
            ):
                self._starts += 1
                if snapshot.busy:
                    self._last_pressure_start = now
                if reason is not None and on_deferred is not None:
                    await on_deferred(None)
                return
            next_reason = snapshot.reason or "Waiting for host start capacity"
            if next_reason != reason and on_deferred is not None:
                await on_deferred(next_reason)
            reason = next_reason
            await asyncio.sleep(self.monitor.interval)
