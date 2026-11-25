"""The event-loop watchdog names the code blocking a worker's loop in time."""

from __future__ import annotations

import asyncio
import time

import pytest

from nebula.v3 import loop_watchdog, runtime_coordination
from nebula.v3.loop_watchdog import EventLoopWatchdog
from nebula.v3.runtime_coordination import LeaseLost, LeaseRenewal
from nebula.v3.runtime_worker import keep_lease
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

pytestmark = pytest.mark.anyio


def _hold_the_loop_synchronously(seconds: float) -> None:
    time.sleep(seconds)  # synchronous work on the event-loop thread


async def test_watchdog_captures_the_blocking_stack_while_the_loop_is_blocked(
    monkeypatch,
):
    recorded: list[dict] = []

    def record(level, feature, event_code, message, **fields):
        recorded.append({"level": level, "event_code": event_code, **fields})
        return "diagnostic-1"

    monkeypatch.setattr(loop_watchdog, "record_diagnostic", record)
    logged: list[tuple[float, str]] = []
    watchdog = EventLoopWatchdog(
        threshold=0.3,
        capture_interval=60,
        beat_interval=0.05,
        log=lambda text: logged.append((time.monotonic(), text)),
    )
    watchdog.start()
    try:
        await asyncio.sleep(0.2)
        assert watchdog.last_stall is None
        _hold_the_loop_synchronously(1.0)
        unblocked = time.monotonic()
        await asyncio.sleep(0.2)
        stall = watchdog.last_stall
        assert stall is not None
        # Captured during the block, not after it.
        assert logged and logged[0][0] < unblocked
        functions = [function for _, function, _ in stall.frames]
        assert "_hold_the_loop_synchronously" in functions
        assert stall.lag_seconds > 0.3
        assert "_hold_the_loop_synchronously" in logged[0][1]
        (entry,) = recorded
        assert entry["event_code"] == "runtime.event_loop.stalled"
        assert any(
            frame["function"] == "_hold_the_loop_synchronously"
            for frame in entry["stack_frames"]
        )
        assert "diagnostic-1" in (watchdog.describe_last_stall() or "")

        # A second stall inside the capture interval is counted, not captured.
        _hold_the_loop_synchronously(0.8)
        await asyncio.sleep(0.2)
        assert watchdog.captures == 1
        assert watchdog.suppressed == 1
        assert len(logged) == 1
    finally:
        watchdog.stop()


async def test_disabled_watchdog_starts_nothing():
    watchdog = EventLoopWatchdog(threshold=0)
    watchdog.start()
    assert watchdog._thread is None
    watchdog.stop()


def test_threshold_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(loop_watchdog.STALL_THRESHOLD_ENV, "7.5")
    assert EventLoopWatchdog().threshold == 7.5
    monkeypatch.setenv(loop_watchdog.STALL_THRESHOLD_ENV, "not-a-number")
    assert EventLoopWatchdog().threshold == 5.0


async def test_lease_lost_to_a_blocked_loop_names_the_captured_stack(monkeypatch):
    monkeypatch.setattr(runtime_coordination, "LEASE_MS", 6_000)
    monkeypatch.setattr(runtime_coordination, "LEASE_SAFETY_MS", 3_000)
    monkeypatch.setattr(loop_watchdog, "record_diagnostic", lambda *a, **k: None)
    renewal = LeaseRenewal()
    watchdog = EventLoopWatchdog(
        threshold=0.5, capture_interval=60, beat_interval=0.05, log=lambda _: None
    )

    async def renew():
        return None

    async def hog():
        await asyncio.sleep(0.3)
        _hold_the_loop_synchronously(4.5)

    watchdog.start()
    try:
        blocker = asyncio.create_task(hog())
        with pytest.raises(LeaseLost, match="event loop was blocked") as raised:
            async with asyncio.timeout(15):
                await keep_lease(
                    renew,
                    renewal,
                    interval=0.1,
                    stall_report=watchdog.describe_last_stall,
                )
        await blocker
    finally:
        watchdog.stop()
    message = str(raised.value)
    assert "last captured loop stack" in message
    assert "_hold_the_loop_synchronously" in message
