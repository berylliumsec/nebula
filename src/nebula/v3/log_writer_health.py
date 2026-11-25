"""Heartbeat, staleness detection, and recovery for Core's file diagnostics.

Core's default diagnostics level is ``error``, so ``runtime.log`` normally
changes only when something fails.  An unchanged file therefore cannot tell
"nothing happened" from "the writer stopped".  The monitor here closes that
gap:

* every interval it writes one forced ``runtime.log_writer_heartbeat`` record
  to ``runtime.log`` (forced past the level filter);
* it then checks that the writer thread is alive, that no sink is marked as
  failed, that ``runtime.log`` was modified at or after the heartbeat it just
  queued, and that the log volume has free space;
* when a check fails it records one ``critical`` alert (kept in the in-memory
  error ring even while disk writes fail) and, once free space is back,
  recovers the sinks: restart a dead writer, recreate deleted files, force the
  failed-path probe, and clear the sticky degraded flag;
* when the sinks are healthy again it records a recovery record.

``log_writer_status()`` returns the latest assessment for the process-global
manager, for Butler or the supervisor to expose.  ``assess_log_dir`` checks a
log directory from another process using file timestamps only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any

from .diagnostics import FEATURE_FILES, get_diagnostics, record_caught_exception

if TYPE_CHECKING:
    from .diagnostics import DiagnosticManager

HEARTBEAT_ENV = "NEBULA_LOG_HEARTBEAT_SECONDS"
DEFAULT_HEARTBEAT_SECONDS = 300.0
MIN_HEARTBEAT_SECONDS = 1.0
# A file is stale when it has not changed for this many heartbeat intervals.
STALE_INTERVALS = 2.5
# Below this much free space a write is likely to fail with ENOSPC.
MIN_FREE_BYTES = 8 * 1024 * 1024
HEARTBEAT_FEATURE = "runtime"
HEARTBEAT_FILE = FEATURE_FILES[HEARTBEAT_FEATURE]
FLUSH_TIMEOUT_SECONDS = 2.0


def heartbeat_interval(
    value: float | None = None, environ: Mapping[str, str] | None = None
) -> float:
    """Configured interval in seconds; ``0`` disables the monitor."""

    if value is None:
        raw = (environ if environ is not None else os.environ).get(HEARTBEAT_ENV)
        if raw is None or not raw.strip():
            return DEFAULT_HEARTBEAT_SECONDS
        try:
            value = float(raw)
        except ValueError as exc:
            record_caught_exception(
                "diagnostics",
                "diagnostics.heartbeat_config_invalid",
                "The log heartbeat interval is not a number; the default is used.",
                exc,
                stage="configuration",
            )
            return DEFAULT_HEARTBEAT_SECONDS
    if value <= 0:
        return 0.0
    return max(MIN_HEARTBEAT_SECONDS, value)


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError as exc:
        # diagnostic-expected: an unreadable volume is reported as unknown
        # free space and surfaces through the sink checks instead.
        del exc
        return None


def assess_log_dir(
    log_dir: Path,
    *,
    interval_seconds: float,
    now: float | None = None,
    files: tuple[str, ...] = (HEARTBEAT_FILE,),
) -> dict[str, Any]:
    """Timestamp-only health of heartbeat files, usable from another process.

    Only files that receive the heartbeat (``runtime.log``) are expected to
    change on a schedule; ``errors.log`` changes only when an error occurs.
    """

    current = time.time() if now is None else now
    limit = interval_seconds * STALE_INTERVALS
    report: list[dict[str, Any]] = []
    for name in files:
        path = log_dir / name
        try:
            age = max(0.0, current - path.stat().st_mtime)
        except OSError as exc:
            # diagnostic-expected: a missing file is reported as stale below.
            del exc
            report.append({"file": name, "exists": False, "stale": True})
            continue
        report.append(
            {
                "file": name,
                "exists": True,
                "age_seconds": round(age, 3),
                "stale": interval_seconds > 0 and age > limit,
            }
        )
    free = _free_bytes(log_dir)
    return {
        "files": report,
        "stale_files": [entry["file"] for entry in report if entry["stale"]],
        "free_bytes": free,
        "low_space": free is not None and free < MIN_FREE_BYTES,
    }


class LogWriterHealthMonitor:
    """Periodic heartbeat and self-repair for one :class:`DiagnosticManager`."""

    def __init__(
        self,
        manager: DiagnosticManager,
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
        free_bytes: Callable[[Path], int | None] = _free_bytes,
    ) -> None:
        self.manager = manager
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._free_bytes = free_bytes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._alerting = False
        self._status: dict[str, Any] = {
            "schema": "nebula.log-writer-status/v1",
            "healthy": None,
            "interval_seconds": interval_seconds,
            "last_heartbeat_at": None,
            "last_checked_at": None,
            "problems": [],
        }

    def start(self) -> None:
        if self._thread is not None or self.interval_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._run, name="nebula-log-writer-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as exc:
                record_caught_exception(
                    "diagnostics",
                    "diagnostics.log_writer_health_failed",
                    "A log writer health check failed; the next interval retries.",
                    exc,
                    stage="log-writer-health",
                )

    def beat(self) -> float:
        """Queue one forced heartbeat record to ``runtime.log``."""

        self._sequence += 1
        stamp = self._clock()
        self.manager.record(
            "info",
            HEARTBEAT_FEATURE,
            "runtime.log_writer_heartbeat",
            "Core log writer heartbeat.",
            outcome="success",
            stage="heartbeat",
            metadata={"sequence_end": self._sequence},
            force=True,
        )
        return stamp

    def check(self, *, heartbeat_at: float | None = None) -> dict[str, Any]:
        """Assess the sinks; ``heartbeat_at`` is when a heartbeat was queued."""

        manager = self.manager
        problems: list[str] = []
        writer_alive = manager.writer_alive()
        if not writer_alive:
            problems.append("writer_stopped")
        failed = manager.failed_write_files()
        if failed:
            problems.append("sink_write_failed")
        heartbeat_path = manager.log_dir / HEARTBEAT_FILE
        mtime: float | None
        try:
            mtime = heartbeat_path.stat().st_mtime
        except OSError as exc:
            # diagnostic-expected: a missing runtime.log is itself the problem.
            del exc
            mtime = None
            problems.append("heartbeat_file_missing")
        now = self._clock()
        # Filesystem timestamps can be coarser than the clock; allow 2 s.
        if heartbeat_at is not None and mtime is not None and mtime < heartbeat_at - 2:
            problems.append("heartbeat_not_written")
        elif (
            heartbeat_at is None
            and mtime is not None
            and self.interval_seconds > 0
            and now - mtime > self.interval_seconds * STALE_INTERVALS
        ):
            problems.append("heartbeat_stale")
        free = self._free_bytes(manager.log_dir)
        if free is not None and free < MIN_FREE_BYTES:
            problems.append("low_disk_space")
        status = {
            "schema": "nebula.log-writer-status/v1",
            "healthy": not problems,
            "interval_seconds": self.interval_seconds,
            "last_heartbeat_at": heartbeat_at
            if heartbeat_at is not None
            else self._status.get("last_heartbeat_at"),
            "last_checked_at": now,
            "heartbeat_file_age_seconds": (
                round(max(0.0, now - mtime), 3) if mtime is not None else None
            ),
            "writer_alive": writer_alive,
            "failed_files": failed,
            "free_bytes": free,
            "problems": problems,
            "writer_restarts": getattr(manager, "_writer_restarts", 0),
        }
        return status

    def recover(self, status: Mapping[str, Any]) -> dict[str, Any] | None:
        """Reopen the sinks unless the disk is still full."""

        if "low_disk_space" in status.get("problems", ()) and not status.get(
            "writer_alive", True
        ):
            # A dead writer is restarted even on a full disk: errors must
            # still reach the in-memory ring and stderr.
            self.manager.ensure_writer()
            return {"restarted_writer": True, "deferred": "low_disk_space"}
        if "low_disk_space" in status.get("problems", ()):
            return {"deferred": "low_disk_space"}
        result: dict[str, Any] = self.manager.recover_sinks()
        return result

    def tick(self) -> dict[str, Any]:
        """One heartbeat, check, alert/recovery cycle."""

        with self._lock:
            heartbeat_at = self.beat()
            self.manager.flush(timeout=FLUSH_TIMEOUT_SECONDS)
            status = self.check(heartbeat_at=heartbeat_at)
            if not status["healthy"]:
                recovery = self.recover(status)
                status["recovery"] = recovery
                if recovery is not None and "deferred" not in recovery:
                    retry_at = self.beat()
                    self.manager.flush(timeout=FLUSH_TIMEOUT_SECONDS)
                    after = self.check(heartbeat_at=retry_at)
                    after["recovery"] = recovery
                    after["recovered_from"] = status["problems"]
                    status = after
            self._report(status)
            self._status = status
            return dict(status)

    def _report(self, status: Mapping[str, Any]) -> None:
        if not status["healthy"] and not self._alerting:
            self._alerting = True
            self.manager.record(
                "critical",
                "diagnostics",
                "diagnostics.log_writer_unhealthy",
                "Core file logs are not being written: "
                + ", ".join(status["problems"])
                + ".",
                outcome="degraded",
                stage="log-writer-health",
                retryable=True,
                safe_failure_cause="Core file logs stopped updating.",
                metadata={
                    "health": "unhealthy",
                    "count": len(status["failed_files"]),
                    "state": ",".join(status["problems"]),
                },
                force=True,
            )
        elif status["healthy"] and self._alerting:
            self._alerting = False
            self.manager.record(
                "warning",
                "diagnostics",
                "diagnostics.log_writer_recovered",
                "Core file logs are being written again.",
                outcome="success",
                stage="log-writer-health",
                metadata={"health": "healthy"},
                force=True,
            )

    def status(self) -> dict[str, Any]:
        """Latest assessment, refreshed from current state without writing."""

        snapshot = dict(self._status)
        live = self.check()
        snapshot.update(
            {
                key: live[key]
                for key in (
                    "writer_alive",
                    "failed_files",
                    "free_bytes",
                    "heartbeat_file_age_seconds",
                    "writer_restarts",
                )
            }
        )
        problems = sorted(set(snapshot.get("problems") or []) | set(live["problems"]))
        snapshot["problems"] = problems
        snapshot["healthy"] = not problems
        snapshot["alerting"] = self._alerting
        return snapshot


def start_log_writer_monitor(
    manager: DiagnosticManager, *, interval_seconds: float | None = None
) -> LogWriterHealthMonitor | None:
    """Start the monitor for ``manager``; ``None`` when heartbeats are off."""

    interval = heartbeat_interval(interval_seconds)
    if interval <= 0:
        return None
    monitor = LogWriterHealthMonitor(manager, interval_seconds=interval)
    monitor.start()
    return monitor


def log_writer_status() -> dict[str, Any]:
    """Log-writer health of the process-global diagnostics, for Butler."""

    manager = get_diagnostics()
    if manager is None:
        return {
            "schema": "nebula.log-writer-status/v1",
            "healthy": None,
            "problems": ["diagnostics_not_initialized"],
        }
    monitor = manager.health_monitor
    if isinstance(monitor, LogWriterHealthMonitor):
        return monitor.status()
    # Heartbeats are disabled: report what can be seen without them.
    probe = LogWriterHealthMonitor(manager, interval_seconds=0)
    status = probe.check()
    status["heartbeat"] = "disabled"
    return status


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "HEARTBEAT_ENV",
    "LogWriterHealthMonitor",
    "assess_log_dir",
    "heartbeat_interval",
    "log_writer_status",
    "start_log_writer_monitor",
]
