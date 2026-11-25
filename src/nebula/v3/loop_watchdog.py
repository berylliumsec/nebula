"""Name what blocks a worker's event loop before the block costs its lease.

A worker whose asyncio loop is held by synchronous work cannot renew its
leases; ``keep_lease`` then raises ``LeaseLost`` and the worker exits. By then
the blocking code has returned and nothing records what it was. This watchdog
runs on its own daemon thread: the loop stamps a heartbeat through a cheap
re-arming timer, and when the stamp is older than a threshold the thread reads
the loop thread's current stack (``sys._current_frames``), which is the code
holding the loop at that moment. It writes that stack to the worker log
(stderr) and to a diagnostic record, at most once per stall and at most once
per ``capture_interval``, and keeps the last capture so ``LeaseLost`` can name
it.

Reading another thread's frames does not stop or signal that thread, and the
watchdog never touches the loop from its own thread, so it is safe to leave
running for the whole process life.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import sys
import threading
import time
from types import FrameType
from typing import Any, Callable

from .diagnostics import record_caught_exception, record_diagnostic

STALL_THRESHOLD_ENV = "NEBULA_LOOP_STALL_WATCHDOG_SECONDS"
CAPTURE_INTERVAL_ENV = "NEBULA_LOOP_STALL_CAPTURE_INTERVAL_SECONDS"
DEFAULT_STALL_THRESHOLD_SECONDS = 5.0
DEFAULT_CAPTURE_INTERVAL_SECONDS = 60.0
MAX_CAPTURED_FRAMES = 32


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        # diagnostic-expected: a malformed operator setting falls back to the default.
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class LoopStall:
    """One captured stall: how late the loop was and what it was running."""

    lag_seconds: float
    captured_at: float
    frames: tuple[tuple[str, str, int], ...]  # (module, function, line), outer first
    error_id: str | None = None

    def innermost(self, count: int = 6) -> str:
        """A short, single-line ``module:function:line`` trail, innermost last."""

        return " <- ".join(
            f"{module}.{function}:{line}"
            for module, function, line in reversed(self.frames[-count:])
        )

    def log_text(self) -> str:
        lines = [
            f"NEBULA_EVENT_LOOP_STALL lag={self.lag_seconds:.1f}s "
            f"error_id={self.error_id or '-'}; loop thread stack (most recent call last):"
        ]
        lines.extend(
            f"  {module}.{function}:{line}" for module, function, line in self.frames
        )
        return "\n".join(lines) + "\n"


def _frames(frame: FrameType | None) -> tuple[tuple[str, str, int], ...]:
    collected: list[tuple[str, str, int]] = []
    while frame is not None:
        module = frame.f_globals.get("__name__")
        collected.append(
            (
                module if isinstance(module, str) else frame.f_code.co_filename,
                frame.f_code.co_name,
                frame.f_lineno or 0,
            )
        )
        frame = frame.f_back
    collected.reverse()
    return tuple(collected[-MAX_CAPTURED_FRAMES:])


class EventLoopWatchdog:
    """Detect event-loop lag from a separate thread and capture the loop's stack."""

    def __init__(
        self,
        *,
        threshold: float | None = None,
        capture_interval: float | None = None,
        beat_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], Any] | None = None,
    ) -> None:
        self.threshold = (
            _env_seconds(STALL_THRESHOLD_ENV, DEFAULT_STALL_THRESHOLD_SECONDS)
            if threshold is None
            else threshold
        )
        self.capture_interval = (
            _env_seconds(CAPTURE_INTERVAL_ENV, DEFAULT_CAPTURE_INTERVAL_SECONDS)
            if capture_interval is None
            else capture_interval
        )
        # Beat and poll well inside the threshold so lag is seen promptly.
        self.beat_interval = (
            beat_interval
            if beat_interval is not None
            else max(0.05, min(1.0, self.threshold / 4))
        )
        self.clock = clock
        self._log = log or self._stderr
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._beat = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._episode_captured = False
        self._last_capture_at: float | None = None
        self.last_stall: LoopStall | None = None
        self.captures = 0
        self.suppressed = 0

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    @staticmethod
    def _stderr(text: str) -> None:
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except (OSError, ValueError):
            # diagnostic-expected: stderr is the final sink; a closed one cannot report itself.
            pass

    def start(self) -> None:
        """Start beating on the running loop and watching from a daemon thread."""

        if not self.enabled or self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._beat = self.clock()
        self._timer = self._loop.call_later(self.beat_interval, self._stamp)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, name="nebula-loop-watchdog", daemon=True
        )
        self._thread.start()

    def _stamp(self) -> None:
        self._beat = self.clock()
        if self._loop is not None and not self._stop.is_set():
            self._timer = self._loop.call_later(self.beat_interval, self._stamp)

    def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(1.0, self.beat_interval * 2))

    def _watch(self) -> None:
        while not self._stop.wait(self.beat_interval):
            try:
                self.check()
            except Exception as exc:
                # The watchdog must never end the process or itself on a bad capture.
                record_caught_exception(
                    "runtime",
                    "runtime.loop_watchdog.check_failed",
                    "The event-loop watchdog could not check the loop.",
                    exc,
                    stage="loop-watchdog",
                )

    def check(self) -> LoopStall | None:
        """Capture the loop thread's stack if the loop is late; returns a new capture."""

        lag = self.clock() - self._beat
        if lag <= self.threshold:
            self._episode_captured = False
            return None
        with self._lock:
            if self._episode_captured:
                return None
            self._episode_captured = True
            now = self.clock()
            if (
                self._last_capture_at is not None
                and now - self._last_capture_at < self.capture_interval
            ):
                self.suppressed += 1
                return None
            self._last_capture_at = now
        frame = (
            sys._current_frames().get(self._loop_thread)
            if self._loop_thread is not None
            else None
        )
        frames = _frames(frame)
        error_id = record_diagnostic(
            "warning",
            "runtime",
            "runtime.event_loop.stalled",
            "The worker's event loop was blocked; the code holding it is captured.",
            outcome="degraded",
            stage="event-loop",
            exception_type="EventLoopStall",
            stack_frames=[
                {"module": module, "function": function, "line": line}
                for module, function, line in frames
            ],
            metadata={
                "lag_seconds": round(lag, 1),
                "threshold_seconds": self.threshold,
                "suppressed_captures": self.suppressed,
                "innermost": "|".join(
                    f"{module}.{function}:{line}"
                    for module, function, line in frames[-8:]
                ),
            },
        )
        stall = LoopStall(
            lag_seconds=lag, captured_at=now, frames=frames, error_id=error_id
        )
        self.last_stall = stall
        self.captures += 1
        self._log(stall.log_text())
        return stall

    def describe_last_stall(self) -> str | None:
        """A bounded one-line description of the last capture, for LeaseLost."""

        stall = self.last_stall
        if stall is None:
            return None
        reference = f" (diagnostic {stall.error_id})" if stall.error_id else ""
        return (
            f"last captured loop stack after {stall.lag_seconds:.0f} s{reference}: "
            f"{stall.innermost()}"
        )[:1_500]
