"""Bounded, process-local performance measurements without payloads or I/O."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import event
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass
class _Timing:
    count: int = 0
    errors: int = 0
    cancelled: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=128))

    def add(self, duration_ms: float, outcome: str) -> None:
        self.count += 1
        self.errors += outcome == "error"
        self.cancelled += outcome == "cancelled"
        self.total_ms += duration_ms
        self.max_ms = max(self.max_ms, duration_ms)
        self.samples.append(duration_ms)


@dataclass
class _Request:
    request_id: str | None
    phases: dict[str, tuple[int, float]] = field(default_factory=dict)
    closed: bool = False


_current: ContextVar[tuple[PerformanceTelemetry, _Request | None] | None] = ContextVar(
    "nebula_performance", default=None
)


class PerformanceTelemetry:
    """All-time counters plus recent bounded samples; owned by one Core DB."""

    def __init__(
        self, *, max_operations: int = 512, max_slow: int = 64, slow_ms: float = 250
    ) -> None:
        self.max_operations = max(1, max_operations)
        self.max_slow = max(1, max_slow)
        self.slow_ms = max(0.0, slow_ms)
        self._started = time.perf_counter()
        self._lock = threading.Lock()
        self._operations: dict[str, _Timing] = {}
        self._slow: deque[dict[str, Any]] = deque(maxlen=self.max_slow)
        self._overflow = 0

    @contextmanager
    def activate(self, request: _Request | None = None) -> Iterator[None]:
        token = _current.set((self, request))
        try:
            yield
        finally:
            _current.reset(token)

    @contextmanager
    def span(self, operation: str) -> Iterator[None]:
        """Measure a code-defined operation, including errors and cancellation."""
        started = time.perf_counter()
        outcome = "ok"
        try:
            yield
        except BaseException as exc:
            outcome = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            )
            raise
        finally:
            self.record(operation, (time.perf_counter() - started) * 1000, outcome)

    def record(
        self,
        operation: str,
        duration_ms: float,
        outcome: str = "ok",
        *,
        include_recent: bool = True,
        http_status: int | None = None,
    ) -> None:
        # Callers supply code-defined labels, never request paths or SQL values.
        operation = operation[:192]
        duration_ms = max(0.0, duration_ms)
        current = _current.get()
        request = current[1] if current is not None and current[0] is self else None
        slow = include_recent and duration_ms >= self.slow_ms
        source = _source_location() if slow and operation.startswith("db.") else None
        with self._lock:
            key = operation
            if (
                key not in self._operations
                and len(self._operations) >= self.max_operations
            ):
                # One additional overflow bucket; never evict hot counters.
                key = "other"
                self._overflow += 1
            timing = self._operations.get(key)
            if timing is None:
                timing = self._operations[key] = _Timing()
            timing.add(duration_ms, outcome)
            if (
                request is not None
                and not request.closed
                and not operation.startswith("http.")
            ):
                phase = (
                    operation
                    if operation in request.phases or len(request.phases) < 32
                    else "other"
                )
                count, total = request.phases.get(phase, (0, 0.0))
                request.phases[phase] = (count + 1, total + duration_ms)
            if slow:
                record: dict[str, Any] = {
                    "operation": operation,
                    "duration_ms": round(duration_ms, 3),
                    "uptime_seconds": round(time.perf_counter() - self._started, 3),
                    "outcome": outcome,
                }
                if request is not None and not request.closed:
                    record["request_id"] = request.request_id
                    if operation.startswith("http."):
                        record["phases"] = [
                            {
                                "operation": name,
                                "count": count,
                                "total_ms": round(total, 3),
                            }
                            for name, (count, total) in sorted(
                                request.phases.items(),
                                key=lambda item: item[1][1],
                                reverse=True,
                            )
                        ]
                if source is not None:
                    record["source"] = source
                if http_status is not None:
                    record["http_status"] = http_status
                self._slow.append(record)

    def close_request(self, request: _Request) -> None:
        # Detached background tasks inherit context but cannot mutate a finished
        # request's breakdown. They still contribute to global operation totals.
        with self._lock:
            request.closed = True
            request.phases.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [
                (
                    name,
                    item.count,
                    item.errors,
                    item.cancelled,
                    item.total_ms,
                    item.max_ms,
                    list(item.samples),
                )
                for name, item in self._operations.items()
            ]
            recent = list(reversed(self._slow))
            overflow = self._overflow
        operations = []
        for name, count, errors, cancelled, total, maximum, samples in rows:
            ordered = sorted(samples)
            operations.append(
                {
                    "operation": name,
                    "count": count,
                    "errors": errors,
                    "cancelled": cancelled,
                    "total_ms": round(total, 3),
                    "mean_ms": round(total / count, 3),
                    "max_ms": round(maximum, 3),
                    "sample_count": len(ordered),
                    "p50_ms": round(ordered[math.ceil(len(ordered) * 0.5) - 1], 3),
                    "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
                }
            )
        return {
            "schema": "nebula.performance/v1",
            "uptime_seconds": round(time.perf_counter() - self._started, 3),
            "limits": {
                "operations": self.max_operations + 1,
                "samples_per_operation": 128,
                "slow_records": self.max_slow,
                "slow_ms": self.slow_ms,
            },
            "overflow_measurements": overflow,
            "operations": sorted(
                operations, key=lambda item: item["total_ms"], reverse=True
            ),
            "recent_slow": recent,
        }


def current_performance() -> PerformanceTelemetry | None:
    current = _current.get()
    return current[0] if current is not None else None


def _source_location() -> str | None:
    frame = sys._getframe(1)
    try:
        for _ in range(48):
            filename = frame.f_code.co_filename.replace("\\", "/")
            if "/nebula/v3/" in filename and not filename.endswith(
                ("/performance.py", "/database.py")
            ):
                return f"nebula/{filename.rsplit('/nebula/', 1)[1]}:{frame.f_lineno}:{frame.f_code.co_name}"
            if frame.f_back is None:
                break
            frame = frame.f_back
        return None
    finally:
        del frame


def install_sql_timing(engine: Any, performance: PerformanceTelemetry) -> None:
    """Use SQLAlchemy's public events; never retain SQL or bind parameters."""

    def before(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        _many: bool,
    ) -> None:
        context._nebula_perf_started = time.perf_counter()
        compiled = context.compiled
        kind = getattr(getattr(compiled, "statement", None), "__visit_name__", "other")
        kind = kind if kind in {"select", "insert", "update", "delete"} else "other"
        # The compiled shape keeps expanding IN-lists in one operation bucket.
        shape = compiled.string if compiled is not None else statement
        context._nebula_perf_operation = (
            f"db.execute.{kind}.{hashlib.sha256(shape.encode()).hexdigest()[:12]}"
        )

    def finish(context: Any, outcome: str) -> None:
        started = getattr(context, "_nebula_perf_started", None)
        if started is not None:
            context._nebula_perf_started = None
            performance.record(
                context._nebula_perf_operation,
                (time.perf_counter() - started) * 1000,
                outcome,
            )

    def after(
        _connection: Any,
        _cursor: Any,
        _statement: Any,
        _parameters: Any,
        context: Any,
        _many: bool,
    ) -> None:
        finish(context, "ok")

    def failed(context: Any) -> None:
        finish(context.execution_context, "error")

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failed)


class EventLoopLagMonitor:
    """One timer per second, with explicit lifespan ownership and no thread."""

    def __init__(self, performance: PerformanceTelemetry) -> None:
        self.performance = performance
        self._handle: asyncio.TimerHandle | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + 1

        def tick() -> None:
            self.performance.record(
                "runtime.event_loop_lag", max(0, loop.time() - due) * 1000
            )
            self.start()

        self._handle = loop.call_at(due, tick)

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


class PerformanceMiddleware:
    """Measure response readiness separately from deliberate streaming lifetime."""

    def __init__(self, app: ASGIApp, *, performance: PerformanceTelemetry) -> None:
        self.app = app
        self.performance = performance

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Polling/export must not become its own reported bottleneck.
        if scope.get("path") in {
            "/api/v1/diagnostics/performance",
            "/api/v1/diagnostics/export",
        }:
            await self.app(scope, receive, send)
            return
        request = _Request(scope.get("state", {}).get("request_id"))
        started = time.perf_counter()
        status = 500
        streaming = False
        ready = False

        def label(stage: str) -> str:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method", "OTHER")
            if method not in {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
            }:
                method = "OTHER"
            return f"http.{stage} {method} {route}"

        async def measured_send(message: Message) -> None:
            nonlocal status, streaming, ready
            if message["type"] == "http.response.start":
                status = message["status"]
                streaming = any(
                    key.lower() == b"content-type"
                    and b"text/event-stream" in value.lower()
                    for key, value in message.get("headers", [])
                )
                ready = True
                self.performance.record(
                    label("response"),
                    (time.perf_counter() - started) * 1000,
                    "error" if status >= 500 else "ok",
                    http_status=status,
                )
            await send(message)

        with self.performance.activate(request):
            try:
                await self.app(scope, receive, measured_send)
            except BaseException as exc:
                outcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                )
                self.performance.record(
                    label("stream" if streaming else "request"),
                    (time.perf_counter() - started) * 1000,
                    outcome,
                    http_status=status,
                )
                raise
            else:
                if streaming or not ready:
                    self.performance.record(
                        label("stream" if streaming else "request"),
                        (time.perf_counter() - started) * 1000,
                        "error" if status >= 500 else "ok",
                        include_recent=not streaming,
                        http_status=status,
                    )
            finally:
                self.performance.close_request(request)
