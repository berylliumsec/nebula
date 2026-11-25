"""Log-writer heartbeat, stale-sink detection, and recovery."""

import errno
import json
import os
from pathlib import Path
import threading
import time

import pytest

from nebula.v3 import diagnostics as diagnostics_module
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.log_writer_health import (
    HEARTBEAT_ENV,
    MIN_FREE_BYTES,
    LogWriterHealthMonitor,
    assess_log_dir,
    heartbeat_interval,
    log_writer_status,
)

PLENTY = 10 * MIN_FREE_BYTES


@pytest.fixture
def manager(tmp_path: Path):
    value = DiagnosticManager(tmp_path, watch_settings=False, heartbeat_seconds=0)
    try:
        yield value
    finally:
        value.close()


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _monitor(manager: DiagnosticManager, free: list[int]) -> LogWriterHealthMonitor:
    return LogWriterHealthMonitor(
        manager, interval_seconds=60, free_bytes=lambda _path: free[0]
    )


def test_interval_configuration() -> None:
    assert heartbeat_interval(None, {}) == 300.0
    assert heartbeat_interval(None, {HEARTBEAT_ENV: "0"}) == 0.0
    assert heartbeat_interval(None, {HEARTBEAT_ENV: "0.01"}) == 1.0
    assert heartbeat_interval(None, {HEARTBEAT_ENV: "nope"}) == 300.0
    assert heartbeat_interval(45.0) == 45.0


def test_heartbeat_reaches_runtime_log_at_default_error_level(
    manager: DiagnosticManager,
) -> None:
    assert manager.settings.global_level == "error"
    status = _monitor(manager, [PLENTY]).tick()
    assert status["healthy"] is True, status
    events = [
        record["event_code"] for record in _records(manager.log_dir / "runtime.log")
    ]
    assert events == ["runtime.log_writer_heartbeat"]


def test_dead_writer_is_detected_alerted_and_restarted(
    manager: DiagnosticManager,
) -> None:
    with manager._condition:
        manager._stop = True
        manager._condition.notify_all()
    assert manager._writer_thread is not None
    manager._writer_thread.join(timeout=5)
    manager._stop = False
    assert manager.writer_alive() is False

    monitor = _monitor(manager, [PLENTY])
    status = monitor.tick()

    assert manager.writer_alive() is True
    assert status["recovery"]["restarted_writer"] is True
    assert "writer_stopped" in status["recovered_from"]
    assert status["healthy"] is True
    assert manager.flush()
    codes = [r["event_code"] for r in _records(manager.log_dir / "diagnostics.log")]
    # Recovery inside one tick: no alert is raised for a repaired fault.
    assert "diagnostics.log_writer_unhealthy" not in codes


def test_disk_full_alerts_defers_recovery_and_recovers_when_space_returns(
    manager: DiagnosticManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = manager._append
    full = threading.Event()
    full.set()

    def append(path: Path, line: bytes) -> None:
        if full.is_set() and path.name in {"runtime.log", "errors.log"}:
            raise OSError(errno.ENOSPC, "No space left on device")
        original(path, line)

    monkeypatch.setattr(manager, "_append", append)
    free = [0]
    monitor = _monitor(manager, free)
    before = (manager.log_dir / "runtime.log").stat().st_mtime
    time.sleep(0.01)

    status = monitor.tick()

    assert status["healthy"] is False
    assert {"sink_write_failed", "low_disk_space"} <= set(status["problems"])
    assert status["recovery"] == {"deferred": "low_disk_space"}
    assert "runtime.log" in status["failed_files"]
    assert (manager.log_dir / "runtime.log").stat().st_mtime == before
    alerts = [
        record
        for record in manager._memory_errors
        if record.get("event_code") == "diagnostics.log_writer_unhealthy"
    ]
    assert len(alerts) == 1
    assert log_status_problems(monitor) >= {"sink_write_failed"}

    # A second unhealthy tick does not repeat the alert.
    monitor.tick()
    assert (
        sum(
            1
            for record in manager._memory_errors
            if record.get("event_code") == "diagnostics.log_writer_unhealthy"
        )
        == 1
    )

    full.clear()
    free[0] = PLENTY
    status = monitor.tick()

    assert status["healthy"] is True, status
    assert manager.failed_write_files() == []
    assert manager.status()["degraded"] is False
    assert manager.flush()
    runtime_events = [
        r["event_code"] for r in _records(manager.log_dir / "runtime.log")
    ]
    assert runtime_events[-1] == "runtime.log_writer_heartbeat"
    codes = [r["event_code"] for r in _records(manager.log_dir / "diagnostics.log")]
    assert "diagnostics.log_writer_recovered" in codes


def log_status_problems(monitor: LogWriterHealthMonitor) -> set[str]:
    return set(monitor.status()["problems"])


def test_recover_recreates_deleted_logs_and_clears_sticky_degraded(
    manager: DiagnosticManager,
) -> None:
    (manager.log_dir / "runtime.log").unlink()
    manager._mark_degraded("simulated earlier failure", OSError(errno.ENOSPC, "x"))
    assert manager.status()["degraded"] is True

    result = manager.recover_sinks()

    assert result["recreated_files"] >= 1
    assert result["failed_files"] == []
    assert result["cleared_degraded"] is True
    assert (manager.log_dir / "runtime.log").exists()
    assert manager.status()["degraded"] is False


def test_closed_stderr_does_not_kill_the_writer(
    manager: DiagnosticManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Closed:
        def write(self, _value: str) -> int:
            raise ValueError("I/O operation on closed file")

        def flush(self) -> None:
            raise ValueError("I/O operation on closed file")

    def failing(path: Path, line: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(diagnostics_module.sys, "stderr", Closed())
    monkeypatch.setattr(manager, "_append", failing)
    manager.record("warning", "api", "api.test.closed_stderr", "Sink failed.")
    assert manager.flush()
    assert manager.writer_alive() is True


def test_assess_log_dir_flags_stale_and_missing_files(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.log"
    runtime.write_text("")
    old = time.time() - 1_000
    os.utime(runtime, (old, old))
    report = assess_log_dir(tmp_path, interval_seconds=60)
    assert report["stale_files"] == ["runtime.log"]
    fresh = assess_log_dir(tmp_path, interval_seconds=600)
    assert fresh["stale_files"] == []
    missing = assess_log_dir(tmp_path / "none", interval_seconds=60)
    assert missing["files"][0]["exists"] is False


def test_log_writer_status_for_the_global_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = diagnostics_module.get_diagnostics()
    monkeypatch.setenv(HEARTBEAT_ENV, "3600")
    try:
        diagnostics_module._manager = None
        assert log_writer_status()["problems"] == ["diagnostics_not_initialized"]
        manager = DiagnosticManager(tmp_path, watch_settings=False)
        diagnostics_module._manager = manager
        assert manager.health_monitor is not None
        status = log_writer_status()
        assert status["writer_alive"] is True
        assert status["interval_seconds"] == 3600
        assert status["failed_files"] == []
        manager.close()
        assert manager.health_monitor._stop.is_set()
    finally:
        diagnostics_module._manager = previous
