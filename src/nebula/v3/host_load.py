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
            try:
                # Host pressure is normalized by host CPUs, not container quota.
                average = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
                ratio = average if ratio is None else max(ratio, average)
            except (AttributeError, OSError):
                # diagnostic-expected: load average unavailable on this platform.
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
            self._busy = cpu_busy or memory_busy
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
                else None,
                ratio is not None or memory is not None,
            )
            return self._cached


class HostLoadGate:
    """Shared refresh and start budget; cancellation never owns active work."""

    def __init__(self, monitor: HostLoadMonitor | None = None) -> None:
        self.monitor = monitor or HostLoadMonitor()
        self._refresh: asyncio.Task[HostLoadSnapshot] | None = None
        self._epoch: float | None = None
        self._starts = 0
        self._waiters: deque[object] = deque()

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
        self, on_deferred: Callable[[str | None], Awaitable[None]] | None = None
    ) -> None:
        ticket = object()
        self._waiters.append(ticket)
        try:
            await self._wait(ticket, on_deferred)
        finally:
            self._waiters.remove(ticket)

    async def _wait(
        self,
        ticket: object,
        on_deferred: Callable[[str | None], Awaitable[None]] | None,
    ) -> None:
        reason: str | None = None
        while True:
            snapshot = await self.snapshot()
            if snapshot.sampled_at != self._epoch:
                self._epoch, self._starts = snapshot.sampled_at, 0
            budget = max(1, min(16, math.ceil(snapshot.cpu_capacity * 2)))
            if (
                not snapshot.busy
                and self._starts < budget
                and self._waiters[0] is ticket
            ):
                self._starts += 1
                if reason is not None and on_deferred is not None:
                    await on_deferred(None)
                return
            next_reason = snapshot.reason or "Waiting for host start capacity"
            if next_reason != reason and on_deferred is not None:
                await on_deferred(next_reason)
            reason = next_reason
            await asyncio.sleep(self.monitor.interval)
