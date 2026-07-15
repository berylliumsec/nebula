"""Structured, privacy-preserving local diagnostics for Nebula 3.

This module deliberately does not use the Nebula 2/PyQt logging stack.  It is
the canonical Core implementation of ``nebula.diagnostic/v1`` and owns only
diagnostic data.  Evidence, terminal output, prompts, documents, command text,
and application records must remain in their purpose-built stores.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import platform
import re
import stat
import sys
import threading
import time
import traceback
import warnings
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypeVar
from uuid import uuid4

from .redaction import redact_text
from .version import __version__, build_metadata

DIAGNOSTIC_SCHEMA = "nebula.diagnostic/v1"
SETTINGS_SCHEMA = "nebula.diagnostics-settings/v1"

LEVELS = ("debug", "info", "warning", "error", "critical")
LEVEL_VALUES = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

FEATURE_FILES: dict[str, str] = {
    "desktop": "desktop.log",
    "interface": "interface.log",
    "api": "api.log",
    "setup": "setup.log",
    "storage": "storage.log",
    "projects": "projects.log",
    "terminal": "terminal.log",
    "terminal-audit": "terminal-audit.log",
    "workspace": "workspace.log",
    "notes": "notes.log",
    "capture": "capture.log",
    "providers": "providers.log",
    "chat": "chat.log",
    "knowledge": "knowledge.log",
    "harnesses": "harnesses.log",
    "missions": "missions.log",
    "toolbox": "toolbox.log",
    "sandbox": "sandbox.log",
    "executions": "executions.log",
    "findings": "findings.log",
    "evidence": "evidence.log",
    "reports": "reports.log",
    "diagnostics": "diagnostics.log",
}

DESKTOP_OWNED_FEATURES = frozenset({"desktop", "interface"})
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROTATIONS = 2
MAX_ROTATION_AGE = timedelta(days=14)
MAX_DIRECTORY_BYTES = 256 * 1024 * 1024
MAX_QUEUE_RECORDS = 4096
MAX_METADATA_DEPTH = 5
MAX_METADATA_ITEMS = 64
MAX_STRING_LENGTH = 2048
MAX_STACK_FRAMES = 32
ERROR_MIRROR_PREFIX = "NEBULA_DIAGNOSTIC_ERROR "

_EVENT_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DENIED_KEY = re.compile(
    r"(?:secret|credential|authorization|cookie|header|body|prompt|content|"
    r"source(?:_?code)?|command|argv|stdout|stderr|document(?:_?text)?|"
    r"terminal(?:_?bytes|_?output)?|evidence(?:_?bytes)?|private(?:_?key)?|"
    r"password|passwd|api(?:_?key)?|access(?:_?token)?|refresh(?:_?token)?|"
    r"filename|file_?path|path|query|sql|payload|selected(?:_?text)?)",
    re.IGNORECASE,
)

# Metadata is intentionally constrained.  Feature-specific data should be
# represented by opaque identifiers, enums, counters, fingerprints, and timing.
_ALLOWED_METADATA_KEYS = frozenset(
    {
        "action",
        "adapter",
        "attempt",
        "available",
        "backend",
        "batch_count",
        "byte_count",
        "capability",
        "category",
        "chunk_count",
        "code",
        "collection_count",
        "component",
        "connection_state",
        "count",
        "current_revision",
        "decision",
        "digest",
        "direction",
        "disk_bytes",
        "dropped_count",
        "entity_count",
        "entity_id",
        "entity_type",
        "expected_revision",
        "feature",
        "fingerprint",
        "format",
        "health",
        "http_status",
        "image_digest",
        "installed",
        "item_count",
        "kind",
        "limit",
        "method",
        "mode",
        "model_id",
        "operation",
        "origin",
        "policy",
        "port_class",
        "provider",
        "queue_depth",
        "reason_code",
        "record_count",
        "recovered_count",
        "result",
        "retry_count",
        "revision",
        "route",
        "runner",
        "sequence_end",
        "sequence_start",
        "size_class",
        "state",
        "status",
        "step",
        "target_fingerprint",
        "task_count",
        "timeout_seconds",
        "tool_id",
        "transport",
        "truncated",
        "validation",
        "vendor_request_id",
        "version",
        "warning_count",
    }
)

DiagnosticLevel = Literal["debug", "info", "warning", "error", "critical"]
T = TypeVar("T")

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_request_id", default=None
)
_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_operation_id", default=None
)
_parent_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_parent_operation_id", default=None
)
_project_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_project_id", default=None
)
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_run_id", default=None
)
_execution_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_execution_id", default=None
)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nebula_diagnostic_session_id", default=None
)


class DiagnosticsError(RuntimeError):
    """A safe diagnostics configuration or I/O failure."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def new_request_id() -> str:
    return _new_id("req")


def new_operation_id() -> str:
    return _new_id("op")


def new_error_id() -> str:
    return _new_id("err")


def _safe_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _ID.fullmatch(text) else None


def _safe_text(value: Any, *, limit: int = MAX_STRING_LENGTH) -> str:
    """Return bounded single-record text with known secret forms removed."""

    if not isinstance(value, str):
        value = str(value)
    value = redact_text(value).replace("\x00", "�")
    if len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _safe_duration(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        # diagnostic-expected: invalid optional timing is omitted safely.
        return None
    if not (0 <= duration <= 86_400_000) or not duration == duration:
        return None
    if duration in (float("inf"), float("-inf")):
        return None
    return round(duration, 3)


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth > MAX_METADATA_DEPTH:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        return _sanitize_metadata(value, depth=depth + 1)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _sanitize_value(item, depth=depth + 1)
            for item in list(value)[:MAX_METADATA_ITEMS]
        ]
    # Bytes and arbitrary objects are never coerced: repr/string methods may
    # expose payloads or credentials.
    return f"[{type(value).__name__}]"


def _sanitize_metadata(
    metadata: Mapping[str, Any] | None, *, depth: int = 0
) -> dict[str, Any]:
    if not metadata:
        return {}
    safe: dict[str, Any] = {}
    for raw_key, value in list(metadata.items())[:MAX_METADATA_ITEMS]:
        key = str(raw_key).lower().replace("-", "_")
        if _DENIED_KEY.search(key) or key not in _ALLOWED_METADATA_KEYS:
            continue
        safe[key] = _sanitize_value(value, depth=depth + 1)
    return safe


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Public metadata sanitizer used by all Core diagnostic ingress paths."""

    return _sanitize_metadata(metadata)


def _normalize_level(value: str) -> DiagnosticLevel:
    level = value.strip().lower()
    if level not in LEVEL_VALUES:
        raise ValueError(f"unsupported diagnostics level: {value}")
    return level  # type: ignore[return-value]


def _normalize_feature(value: str) -> str:
    feature = value.strip().lower().replace("_", "-")
    if feature not in FEATURE_FILES:
        raise ValueError(f"unsupported diagnostics feature: {value}")
    return feature


@dataclass(frozen=True)
class DiagnosticSettings:
    global_level: DiagnosticLevel = "error"
    feature_levels: dict[str, DiagnosticLevel] = field(default_factory=dict)
    schema: str = SETTINGS_SCHEMA

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DiagnosticSettings:
        if payload.get("schema") != SETTINGS_SCHEMA:
            raise ValueError("unsupported diagnostics settings schema")
        if set(payload) - {"schema", "global_level", "feature_levels"}:
            raise ValueError("diagnostics settings contain unsupported fields")
        global_level = _normalize_level(str(payload.get("global_level", "")))
        raw_features = payload.get("feature_levels", {})
        if not isinstance(raw_features, Mapping):
            raise ValueError("feature_levels must be an object")
        features: dict[str, DiagnosticLevel] = {}
        for feature, level in raw_features.items():
            features[_normalize_feature(str(feature))] = _normalize_level(str(level))
        return cls(global_level=global_level, feature_levels=features)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "global_level": self.global_level,
            "feature_levels": dict(sorted(self.feature_levels.items())),
        }

    def effective_level(self, feature: str) -> DiagnosticLevel:
        return self.feature_levels.get(feature, self.global_level)


@dataclass
class _PendingRecord:
    record: dict[str, Any]
    line: bytes
    completed: threading.Event | None = None


class DiagnosticManager:
    """Secure JSON-lines writer, settings owner, reader, and exporter."""

    def __init__(
        self,
        data_dir: Path,
        *,
        log_dir: Path | None = None,
        settings_path: Path | None = None,
        desktop_parent: bool = False,
        level_override: str | None = None,
        feature_level_overrides: Mapping[str, str] | None = None,
        queue_capacity: int = MAX_QUEUE_RECORDS,
        watch_settings: bool = True,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.log_dir = (log_dir or self.data_dir / "logs").expanduser().resolve()
        self.settings_path = (
            (settings_path or self.data_dir / "diagnostics-settings.json")
            .expanduser()
            .resolve()
        )
        self.desktop_parent = desktop_parent
        self.launch_id = _new_id("launch")
        self._settings = DiagnosticSettings()
        self._level_override = level_override or os.getenv("NEBULA_DIAGNOSTICS_LEVEL")
        self._feature_level_overrides = dict(feature_level_overrides or {})
        raw_feature_override = os.getenv("NEBULA_DIAGNOSTICS_FEATURE_LEVELS")
        if raw_feature_override and not self._feature_level_overrides:
            try:
                decoded = json.loads(raw_feature_override)
                if isinstance(decoded, dict):
                    self._feature_level_overrides = {
                        str(key): str(value) for key, value in decoded.items()
                    }
            except json.JSONDecodeError:
                # diagnostic-expected: recorded after secure sinks initialize.
                self._override_decode_failed = True
            else:
                self._override_decode_failed = False
        else:
            self._override_decode_failed = False
        self._queue_capacity = max(1, queue_capacity)
        self._queue: deque[_PendingRecord] = deque()
        self._in_flight = 0
        self._condition = threading.Condition()
        self._writer_lock = threading.RLock()
        self._sequence = 0
        self._stop = False
        self._closed = False
        self._degraded = False
        self._last_failure: dict[str, Any] | None = None
        # Disk rotation is bounded, but errors in flight or produced while a
        # sink is unavailable are never evicted during the current process.
        self._memory_errors: deque[dict[str, Any]] = deque()
        self._dropped_count = 0
        self._last_drop_notice = 0.0
        self._last_sink_failure_notice = 0.0
        self._last_rotation: str | None = None
        self._settings_mtime_ns: int | None = None
        self._settings_load_failed = False
        self._last_prune = 0.0
        self._writer_thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        try:
            self._application_version = build_metadata()["version"]
        except Exception:
            # diagnostic-expected: source builds use the module version until
            # the secure diagnostic sinks exist; frozen builds fail elsewhere.
            self._application_version = __version__

        try:
            self._initialize_paths()
        except OSError as exc:
            # A manager still starts in memory-only degraded mode so health and
            # recent-error APIs remain useful when its configured directory is
            # unavailable. Native supervision captures this bounded message.
            self._mark_degraded("Local diagnostics could not initialize.", exc)
            with contextlib.suppress(OSError):
                # diagnostic-expected: stderr is the final emergency sink.
                sys.stderr.write(
                    "NEBULA_DIAGNOSTICS_UNAVAILABLE initialization-failed "
                    f"{type(exc).__name__}\n"
                )
                sys.stderr.flush()
        self._settings = self._load_settings(record_failure=False)
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="nebula-diagnostics-writer",
            daemon=True,
        )
        self._writer_thread.start()
        self._validate_overrides()
        if watch_settings:
            self._watcher_thread = threading.Thread(
                target=self._settings_watch_loop,
                name="nebula-diagnostics-settings",
                daemon=True,
            )
            self._watcher_thread.start()
        self.record(
            "info",
            "diagnostics",
            "diagnostics.initialized",
            "Local diagnostics initialized.",
            outcome="success",
            metadata={"mode": "desktop-child" if desktop_parent else "headless"},
        )
        if self._override_decode_failed:
            self.record(
                "error",
                "diagnostics",
                "diagnostics.environment_override_invalid",
                "A diagnostics environment override was invalid; safe defaults remain active.",
                outcome="failure",
                stage="configuration",
                retryable=False,
            )
        if self._settings_load_failed:
            self.record(
                "error",
                "diagnostics",
                "diagnostics.settings_invalid",
                "Diagnostics preferences were unreadable; Error logging is active.",
                outcome="fallback",
                stage="settings-load",
                retryable=True,
            )

    @property
    def settings(self) -> DiagnosticSettings:
        return self._settings

    def _initialize_paths(self) -> None:
        for directory in (self.data_dir, self.log_dir, self.settings_path.parent):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError as exc:
                self._mark_degraded("Unable to secure the diagnostics directory.", exc)

        owned_features = set(FEATURE_FILES)
        if self.desktop_parent:
            owned_features -= DESKTOP_OWNED_FEATURES
        for feature in sorted(owned_features):
            self._secure_touch(self.log_dir / FEATURE_FILES[feature])
        if not self.desktop_parent:
            self._secure_touch(self.log_dir / "errors.log")

        if not self.settings_path.exists():
            self._atomic_write_json(self.settings_path, DiagnosticSettings().as_dict())
        else:
            self._secure_permissions(self.settings_path)
        self._remember_settings_mtime()
        self._prune(force=True)

    def _secure_touch(self, path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            self._secure_permissions(path)
        except OSError as exc:
            self._mark_degraded("A diagnostics log file is unavailable.", exc)

    @staticmethod
    def _secure_permissions(path: Path) -> None:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            path.chmod(0o600)

    def _atomic_write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._secure_permissions(path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            with contextlib.suppress(FileNotFoundError):
                # diagnostic-expected: os.replace consumes the temp on success.
                temporary.unlink()

    def _load_settings(self, *, record_failure: bool = True) -> DiagnosticSettings:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("diagnostics settings must be an object")
            settings = DiagnosticSettings.from_mapping(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            settings = DiagnosticSettings()
            self._settings_load_failed = True
            if record_failure:
                self.record(
                    "error",
                    "diagnostics",
                    "diagnostics.settings_invalid",
                    "Diagnostics preferences were unreadable; Error logging is active.",
                    outcome="fallback",
                    stage="settings-load",
                    retryable=True,
                    exception=exc,
                )
        else:
            self._settings_load_failed = False
        self._remember_settings_mtime()
        return settings

    def _remember_settings_mtime(self) -> None:
        try:
            self._settings_mtime_ns = self.settings_path.stat().st_mtime_ns
        except OSError:
            # diagnostic-expected: the settings watcher records unavailability.
            self._settings_mtime_ns = None

    def _validate_overrides(self) -> None:
        try:
            if self._level_override:
                self._level_override = _normalize_level(self._level_override)
            self._feature_level_overrides = {
                _normalize_feature(feature): _normalize_level(level)
                for feature, level in self._feature_level_overrides.items()
            }
        except ValueError as exc:
            self._level_override = None
            self._feature_level_overrides = {}
            self.record(
                "error",
                "diagnostics",
                "diagnostics.process_override_invalid",
                "A process-only logging override was invalid; saved preferences are active.",
                outcome="fallback",
                stage="configuration",
                retryable=False,
                exception=exc,
            )

    def effective_level(self, feature: str) -> DiagnosticLevel:
        normalized = _normalize_feature(feature)
        feature_override = self._feature_level_overrides.get(normalized)
        if feature_override:
            return _normalize_level(feature_override)
        if self._level_override:
            return _normalize_level(self._level_override)
        return self._settings.effective_level(normalized)

    def enabled(self, level: str, feature: str) -> bool:
        normalized_level = _normalize_level(level)
        return (
            LEVEL_VALUES[normalized_level]
            >= LEVEL_VALUES[self.effective_level(feature)]
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _exception_fields(
        self, exception: BaseException | None
    ) -> tuple[str | None, list[str], list[dict[str, Any]]]:
        if exception is None:
            return None, [], []
        chain: list[str] = []
        seen: set[int] = set()
        current: BaseException | None = exception
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        frames: list[dict[str, Any]] = []
        if exception.__traceback__ is not None:
            for frame in traceback.extract_tb(exception.__traceback__)[
                -MAX_STACK_FRAMES:
            ]:
                module = Path(frame.filename).stem
                frames.append(
                    {
                        "module": _safe_text(module, limit=128),
                        "function": _safe_text(frame.name, limit=128),
                        "line": frame.lineno,
                    }
                )
        return type(exception).__name__, chain, frames

    def _build_record(
        self,
        level: DiagnosticLevel,
        feature: str,
        event_code: str,
        message: str,
        *,
        source: str,
        error_id: str | None,
        request_id: str | None,
        operation_id: str | None,
        parent_operation_id: str | None,
        project_id: str | None,
        run_id: str | None,
        execution_id: str | None,
        session_id: str | None,
        outcome: str | None,
        stage: str | None,
        duration_ms: float | int | None,
        retryable: bool | None,
        safe_failure_cause: str | None,
        exception: BaseException | None,
        exception_type_override: str | None,
        stack_frames_override: Sequence[Mapping[str, Any]] | None,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        exception_type, exception_chain, stack_frames = self._exception_fields(
            exception
        )
        if exception is None and exception_type_override:
            exception_type = _safe_text(exception_type_override, limit=128)
        if exception is None and stack_frames_override:
            stack_frames = []
            for frame in list(stack_frames_override)[:MAX_STACK_FRAMES]:
                module = frame.get("module")
                function = frame.get("function")
                line = frame.get("line")
                if not isinstance(module, str) or not isinstance(function, str):
                    continue
                if not isinstance(line, int) or line < 0:
                    continue
                stack_frames.append(
                    {
                        "module": _safe_text(module, limit=128),
                        "function": _safe_text(function, limit=128),
                        "line": line,
                    }
                )
        record: dict[str, Any] = {
            "schema": DIAGNOSTIC_SCHEMA,
            "timestamp": _utc_now(),
            "sequence": self._next_sequence(),
            "level": level.upper(),
            "feature": feature,
            "source": _safe_text(source, limit=128),
            "event_code": event_code,
            "message": _safe_text(message),
            "application_version": self._application_version,
            "launch_id": self.launch_id,
        }
        optional: dict[str, Any] = {
            "request_id": _safe_identifier(request_id),
            "operation_id": _safe_identifier(operation_id),
            "parent_operation_id": _safe_identifier(parent_operation_id),
            "error_id": _safe_identifier(error_id),
            "project_id": _safe_identifier(project_id),
            "run_id": _safe_identifier(run_id),
            "execution_id": _safe_identifier(execution_id),
            "session_id": _safe_identifier(session_id),
            "outcome": _safe_text(outcome, limit=64) if outcome else None,
            "stage": _safe_text(stage, limit=128) if stage else None,
            "duration_ms": _safe_duration(duration_ms),
            "retryable": retryable,
            "safe_failure_cause": _safe_text(safe_failure_cause)
            if safe_failure_cause
            else None,
            "exception_type": exception_type,
            "exception_chain": exception_chain or None,
            "stack_frames": stack_frames or None,
        }
        record.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        safe_metadata = sanitize_metadata(metadata)
        if safe_metadata:
            record["metadata"] = safe_metadata
        return record

    def record(
        self,
        level: str,
        feature: str,
        event_code: str,
        message: str,
        *,
        source: str = "core",
        error_id: str | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        execution_id: str | None = None,
        session_id: str | None = None,
        outcome: str | None = None,
        stage: str | None = None,
        duration_ms: float | int | None = None,
        retryable: bool | None = None,
        safe_failure_cause: str | None = None,
        exception: BaseException | None = None,
        exception_type: str | None = None,
        stack_frames: Sequence[Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        normalized_level = _normalize_level(level)
        normalized_feature = _normalize_feature(feature)
        if not _EVENT_CODE.fullmatch(event_code):
            raise ValueError("diagnostic event codes must be stable dotted identifiers")
        if not self.enabled(normalized_level, normalized_feature):
            return None
        if normalized_level in {"error", "critical"}:
            error_id = _safe_identifier(error_id) or new_error_id()
        wait_for: threading.Event | None = None
        with self._condition:
            record = self._build_record(
                normalized_level,
                normalized_feature,
                event_code,
                message,
                source=source,
                error_id=error_id,
                request_id=request_id or _request_id.get(),
                operation_id=operation_id or _operation_id.get(),
                parent_operation_id=parent_operation_id or _parent_operation_id.get(),
                project_id=project_id or _project_id.get(),
                run_id=run_id or _run_id.get(),
                execution_id=execution_id or _execution_id.get(),
                session_id=session_id or _session_id.get(),
                outcome=outcome,
                stage=stage,
                duration_ms=duration_ms,
                retryable=retryable,
                safe_failure_cause=safe_failure_cause,
                exception=exception,
                exception_type_override=exception_type,
                stack_frames_override=stack_frames,
                metadata=metadata,
            )
            line = (
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            pending = _PendingRecord(record=record, line=line)
            if normalized_level in {"error", "critical"}:
                wait_for = threading.Event()
                pending.completed = wait_for
                # ERROR/CRITICAL records are allowed beyond the lower-level
                # queue bound. They are never discarded or overwritten.
                self._queue.append(pending)
                self._condition.notify()
            else:
                if len(self._queue) >= self._queue_capacity:
                    self._dropped_count += 1
                    wait_for = self._enqueue_drop_notice_locked()
                else:
                    self._queue.append(pending)
                    self._condition.notify()
                    return error_id

        # The single writer preserves global sequence order. Error calls wait
        # for durable append/fsync; lower-level records only wait when they had
        # to synchronously report queue pressure.
        if wait_for is not None and not wait_for.wait(timeout=10):
            self._mark_degraded(
                "The diagnostics writer did not acknowledge an error record.",
                TimeoutError("diagnostics writer acknowledgement timed out"),
            )
            with contextlib.suppress(OSError):
                # diagnostic-expected: stderr is the final emergency sink.
                sys.stderr.write("NEBULA_DIAGNOSTICS_UNAVAILABLE writer-timeout\n")
                sys.stderr.flush()
        return error_id

    def _enqueue_drop_notice_locked(self) -> threading.Event | None:
        now = time.monotonic()
        if now - self._last_drop_notice < 60:
            return None
        self._last_drop_notice = now
        error_id = new_error_id()
        record = self._build_record(
            "error",
            "diagnostics",
            "diagnostics.records_dropped",
            "Lower-level diagnostic records were dropped because the local queue was full.",
            source="core",
            error_id=error_id,
            request_id=_request_id.get(),
            operation_id=_operation_id.get(),
            parent_operation_id=_parent_operation_id.get(),
            project_id=None,
            run_id=None,
            execution_id=None,
            session_id=None,
            outcome="degraded",
            stage="queue",
            duration_ms=None,
            retryable=True,
            safe_failure_cause="The bounded diagnostics queue reached capacity.",
            exception=None,
            exception_type_override=None,
            stack_frames_override=None,
            metadata={"dropped_count": self._dropped_count},
        )
        completed = threading.Event()
        pending = _PendingRecord(
            record=record,
            line=(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8"),
            completed=completed,
        )
        self._queue.append(pending)
        self._condition.notify()
        return completed

    def _writer_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._stop, timeout=0.5)
                if self._stop and not self._queue:
                    return
                batch: list[_PendingRecord] = []
                while self._queue and len(batch) < 256:
                    batch.append(self._queue.popleft())
                self._in_flight += len(batch)
            if batch:
                with self._writer_lock:
                    for pending in batch:
                        try:
                            self._write_pending(pending)
                        except BaseException as exc:
                            # The writer itself must not disappear because an
                            # unexpected filesystem/runtime failure escaped the
                            # normal sink handling.
                            self._emergency_sink_failure(pending, exc)
                        finally:
                            if pending.completed is not None:
                                pending.completed.set()
                with self._condition:
                    self._in_flight -= len(batch)
                    self._condition.notify_all()

    def _write_pending(self, pending: _PendingRecord) -> None:
        feature = str(pending.record["feature"])
        path = self.log_dir / FEATURE_FILES[feature]
        try:
            self._append(path, pending.line)
            if pending.record["level"] in {"ERROR", "CRITICAL"}:
                self._memory_errors.append(dict(pending.record))
                if self.desktop_parent:
                    # Desktop validates this complete sanitized frame and owns
                    # aggregate errors.log.  stderr is already supervised.
                    sys.stderr.write(
                        ERROR_MIRROR_PREFIX
                        + pending.line.decode("utf-8").rstrip()
                        + "\n"
                    )
                    sys.stderr.flush()
                else:
                    self._append(self.log_dir / "errors.log", pending.line)
            if time.monotonic() - self._last_prune > 300:
                self._prune()
        except OSError as exc:
            self._emergency_sink_failure(pending, exc)

    def _emergency_sink_failure(
        self, pending: _PendingRecord, exception: BaseException
    ) -> None:
        """Retain errors and report logger failure through the final safe sink."""

        self._mark_degraded("Local diagnostics storage became unavailable.", exception)
        is_error = pending.record.get("level") in {"ERROR", "CRITICAL"}
        if is_error:
            self._memory_errors.append(dict(pending.record))
        try:
            encoded_pending = pending.line.decode("utf-8").rstrip()
        except UnicodeError:
            # diagnostic-expected: pending lines are rebuilt as safe JSON below.
            encoded_pending = ""
        with contextlib.suppress(OSError):
            # diagnostic-expected: stderr is the final emergency sink.
            if is_error and self.desktop_parent and encoded_pending:
                sys.stderr.write(ERROR_MIRROR_PREFIX + encoded_pending + "\n")
            elif encoded_pending:
                sys.stderr.write(
                    "NEBULA_DIAGNOSTICS_UNAVAILABLE " + encoded_pending + "\n"
                )
            sys.stderr.flush()

        now = time.monotonic()
        if now - self._last_sink_failure_notice < 60:
            return
        self._last_sink_failure_notice = now
        with self._condition:
            failure = self._build_record(
                "critical",
                "diagnostics",
                "diagnostics.logging_unavailable",
                "The local diagnostics sink is unavailable.",
                source="core",
                error_id=new_error_id(),
                request_id=_request_id.get(),
                operation_id=_operation_id.get(),
                parent_operation_id=_parent_operation_id.get(),
                project_id=None,
                run_id=None,
                execution_id=None,
                session_id=None,
                outcome="degraded",
                stage="write",
                duration_ms=None,
                retryable=True,
                safe_failure_cause="A required diagnostics file could not be written.",
                exception=exception,
                exception_type_override=None,
                stack_frames_override=None,
                metadata={"component": "writer"},
            )
        self._memory_errors.append(failure)
        encoded_failure = json.dumps(failure, sort_keys=True, separators=(",", ":"))
        with contextlib.suppress(OSError):
            # diagnostic-expected: stderr is the final emergency sink.
            prefix = (
                ERROR_MIRROR_PREFIX
                if self.desktop_parent
                else "NEBULA_DIAGNOSTICS_UNAVAILABLE "
            )
            sys.stderr.write(prefix + encoded_failure + "\n")
            sys.stderr.flush()

    def _append(self, path: Path, line: bytes) -> None:
        self._rotate_if_needed(path, len(line))
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "ab", closefd=True) as handle:
                handle.write(line)
                handle.flush()
                if b'"level":"ERROR"' in line or b'"level":"CRITICAL"' in line:
                    os.fsync(handle.fileno())
        finally:
            # fdopen owns the descriptor on the successful path.
            pass

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            # diagnostic-expected: first write creates the pre-secured file.
            self._secure_touch(path)
            size = 0
        if size + incoming_bytes <= MAX_FILE_BYTES:
            return
        oldest = path.with_name(f"{path.name}.{MAX_ROTATIONS}")
        with contextlib.suppress(FileNotFoundError):
            # diagnostic-expected: the oldest rotation is optional.
            oldest.unlink()
        for index in range(MAX_ROTATIONS - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            target = path.with_name(f"{path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        if path.exists():
            os.replace(path, path.with_name(f"{path.name}.1"))
        self._secure_touch(path)
        self._last_rotation = _utc_now()

    def _prune(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_prune < 300:
            return
        self._last_prune = now
        cutoff = datetime.now(UTC) - MAX_ROTATION_AGE
        rotations = [
            path
            for path in self.log_dir.glob("*.log.*")
            if path.is_file() and path.name.rsplit(".", 1)[-1].isdigit()
        ]
        for path in rotations:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                if modified < cutoff:
                    path.unlink()
            except OSError as exc:
                self._mark_degraded(
                    "An expired diagnostics rotation could not be removed.", exc
                )

        files = [path for path in self.log_dir.iterdir() if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        if total <= MAX_DIRECTORY_BYTES:
            return
        removable = sorted(
            (path for path in files if re.search(r"\.log\.\d+$", path.name)),
            key=lambda item: item.stat().st_mtime,
        )
        for path in removable:
            if total <= MAX_DIRECTORY_BYTES:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError as exc:
                self._mark_degraded(
                    "The diagnostics directory cap could not be enforced.", exc
                )

    def _mark_degraded(self, message: str, exception: BaseException) -> None:
        self._degraded = True
        self._last_failure = {
            "timestamp": _utc_now(),
            "message": message,
            "exception_type": type(exception).__name__,
        }

    def _settings_watch_loop(self) -> None:
        while not self._stop:
            with self._condition:
                self._condition.wait(timeout=0.5)
                if self._stop:
                    return
            try:
                mtime = self.settings_path.stat().st_mtime_ns
            except OSError as exc:
                if self._settings_mtime_ns is not None:
                    self._settings_mtime_ns = None
                    self.record(
                        "error",
                        "diagnostics",
                        "diagnostics.settings_unavailable",
                        "Diagnostics preferences became unavailable; Error logging is active.",
                        outcome="fallback",
                        stage="settings-reload",
                        retryable=True,
                        exception=exc,
                    )
                    self._settings = DiagnosticSettings()
                continue
            if mtime == self._settings_mtime_ns:
                continue
            self._settings = self._load_settings()
            self.record(
                "info",
                "diagnostics",
                "diagnostics.settings_reloaded",
                "Diagnostics preferences were reloaded.",
                outcome="success",
                stage="settings-reload",
            )

    def update_settings(self, payload: Mapping[str, Any]) -> DiagnosticSettings:
        settings = DiagnosticSettings.from_mapping(payload)
        try:
            self._atomic_write_json(self.settings_path, settings.as_dict())
        except OSError as exc:
            error_id = self.record(
                "error",
                "diagnostics",
                "diagnostics.settings_write_failed",
                "Diagnostics preferences could not be saved.",
                outcome="failure",
                stage="settings-write",
                retryable=True,
                exception=exc,
            )
            raise DiagnosticsError(
                f"diagnostics preferences could not be saved ({error_id})"
            ) from exc
        self._settings = settings
        self._remember_settings_mtime()
        self.record(
            "info",
            "diagnostics",
            "diagnostics.settings_updated",
            "Diagnostics preferences were updated.",
            outcome="success",
            stage="settings-write",
            metadata={"count": len(settings.feature_levels)},
        )
        return settings

    def reset_settings(self) -> DiagnosticSettings:
        return self.update_settings(DiagnosticSettings().as_dict())

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._condition:
                if not self._queue and self._in_flight == 0:
                    return True
                self._condition.notify()
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._closed:
            return
        self.record(
            "info",
            "diagnostics",
            "diagnostics.stopping",
            "Local diagnostics are stopping.",
            outcome="success",
        )
        self.flush()
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._writer_thread:
            self._writer_thread.join(timeout=5)
        if self._watcher_thread:
            self._watcher_thread.join(timeout=2)
        self._closed = True

    def _disk_usage(self) -> int:
        try:
            return sum(
                path.stat().st_size for path in self.log_dir.iterdir() if path.is_file()
            )
        except OSError as exc:
            self._mark_degraded("Diagnostics disk usage could not be measured.", exc)
            return 0

    def status(self) -> dict[str, Any]:
        writable = os.access(self.log_dir, os.W_OK)
        return {
            "schema": "nebula.diagnostics-status/v1",
            "writable": writable and not self._degraded,
            "degraded": self._degraded or not writable,
            "log_directory": str(self.log_dir),
            "settings_path": str(self.settings_path),
            "global_level": self._settings.global_level,
            "feature_levels": dict(sorted(self._settings.feature_levels.items())),
            "effective_levels": {
                feature: self.effective_level(feature) for feature in FEATURE_FILES
            },
            "process_override": self._level_override,
            "disk_usage_bytes": self._disk_usage(),
            "last_rotation": self._last_rotation,
            "dropped_record_count": self._dropped_count,
            "queued_record_count": len(self._queue) + self._in_flight,
            "last_failure": self._last_failure,
        }

    def list_files(self) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for path in sorted(self.log_dir.glob("*.log*")):
            if not path.is_file() or path.is_symlink():
                continue
            info = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size_bytes": info.st_size,
                    "modified_at": datetime.fromtimestamp(info.st_mtime, UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                }
            )
        return files

    def recent_errors(
        self,
        *,
        feature: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_feature = _normalize_feature(feature) if feature else None
        bounded_limit = min(max(limit, 1), 500)
        records: list[dict[str, Any]] = []
        paths: list[Path]
        if self.desktop_parent:
            # Core cannot read the desktop-owned aggregate. Its in-memory copy
            # still makes the API useful if the native command is unavailable.
            paths = []
        else:
            aggregate = self.log_dir / "errors.log"
            paths = [
                aggregate.with_name(f"{aggregate.name}.{index}")
                for index in range(MAX_ROTATIONS, 0, -1)
            ] + [aggregate]
        for path in paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError as exc:
                            self.record(
                                "error",
                                "diagnostics",
                                "diagnostics.viewer_record_invalid",
                                "A local diagnostic error record was malformed.",
                                outcome="failure",
                                stage="viewer-read",
                                retryable=False,
                                exception=exc,
                            )
                            continue
                        if isinstance(payload, dict):
                            records.append(payload)
            except FileNotFoundError:
                # diagnostic-expected: a retained rotation need not exist.
                continue
            except OSError as exc:
                self.record(
                    "error",
                    "diagnostics",
                    "diagnostics.viewer_read_failed",
                    "Recent diagnostic errors could not be read.",
                    outcome="failure",
                    stage="viewer-read",
                    retryable=True,
                    exception=exc,
                )
                break
        with self._writer_lock:
            records.extend(
                dict(item)
                for item in self._memory_errors
                if item.get("level") in {"ERROR", "CRITICAL"}
            )
        if normalized_feature:
            records = [
                item for item in records if item.get("feature") == normalized_feature
            ]
        if after:
            records = [
                item for item in records if str(item.get("timestamp", "")) > after
            ]
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in records:
            key = str(item.get("error_id") or json.dumps(item, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        unique.sort(
            key=lambda item: (
                str(item.get("timestamp", "")),
                int(item.get("sequence", 0)),
            )
        )
        return unique[-bounded_limit:]

    def export(self, output: Path) -> Path:
        output = output.expanduser().resolve()
        if output.suffix.lower() != ".zip":
            raise ValueError("diagnostics export must use a .zip extension")
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        manifest: dict[str, str] = {}
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for path in sorted(self.log_dir.iterdir()):
                    if not path.is_file() or path.is_symlink():
                        continue
                    if not re.fullmatch(r"[A-Za-z0-9-]+\.log(?:\.[12])?", path.name):
                        continue
                    if path.name.startswith("nebula-core-startup.log"):
                        try:
                            data = self._sanitize_emergency_log(
                                path.read_text(encoding="utf-8", errors="replace")
                            )
                        except OSError as exc:
                            self.record(
                                "error",
                                "diagnostics",
                                "diagnostics.export_source_failed",
                                "A diagnostics file could not be included in the export.",
                                outcome="degraded",
                                stage="export-read",
                                retryable=True,
                                exception=exc,
                                metadata={"count": 1},
                            )
                            continue
                        name = f"logs/{path.name}"
                        archive.writestr(name, data)
                        manifest[name] = hashlib.sha256(data).hexdigest()
                        continue
                    sanitized_lines: list[str] = []
                    try:
                        for line in path.read_text(encoding="utf-8").splitlines():
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError as exc:
                                self.record(
                                    "error",
                                    "diagnostics",
                                    "diagnostics.export_record_invalid",
                                    "A malformed diagnostic record was excluded from export.",
                                    outcome="failure",
                                    stage="export-redaction",
                                    retryable=False,
                                    exception=exc,
                                )
                                continue
                            if not isinstance(payload, dict):
                                continue
                            sanitized = self._sanitize_export_record(payload)
                            sanitized_lines.append(
                                json.dumps(
                                    sanitized, sort_keys=True, separators=(",", ":")
                                )
                            )
                    except (OSError, UnicodeError) as exc:
                        self.record(
                            "error",
                            "diagnostics",
                            "diagnostics.export_source_failed",
                            "A diagnostics file could not be included in the export.",
                            outcome="degraded",
                            stage="export-read",
                            retryable=True,
                            exception=exc,
                            metadata={"count": 1},
                        )
                        continue
                    data = (
                        "\n".join(sanitized_lines) + ("\n" if sanitized_lines else "")
                    ).encode("utf-8")
                    name = f"logs/{path.name}"
                    archive.writestr(name, data)
                    manifest[name] = hashlib.sha256(data).hexdigest()

                export_health = dict(self.status())
                export_health.pop("log_directory", None)
                export_health.pop("settings_path", None)
                metadata = {
                    "schema": "nebula.diagnostics-export/v1",
                    "created_at": _utc_now(),
                    "build": build_metadata(),
                    "platform": {
                        "system": platform.system(),
                        "release": platform.release(),
                        "machine": platform.machine(),
                        "python": platform.python_version(),
                    },
                    "settings": self._settings.as_dict(),
                    "health": export_health,
                }
                metadata_bytes = json.dumps(
                    metadata, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                archive.writestr("metadata.json", metadata_bytes)
                manifest["metadata.json"] = hashlib.sha256(metadata_bytes).hexdigest()
                manifest_bytes = json.dumps(
                    {"schema": "nebula.diagnostics-manifest/v1", "sha256": manifest},
                    sort_keys=True,
                    indent=2,
                ).encode("utf-8")
                archive.writestr("SHA256SUMS.json", manifest_bytes)
            os.chmod(temporary, 0o600)
            os.replace(temporary, output)
            os.chmod(output, 0o600)
        except Exception as exc:
            with contextlib.suppress(FileNotFoundError):
                # diagnostic-expected: creation can fail before the temp exists.
                temporary.unlink()
            self.record(
                "error",
                "diagnostics",
                "diagnostics.export_failed",
                "The local diagnostics export could not be generated.",
                outcome="failure",
                stage="export",
                retryable=True,
                exception=exc,
            )
            raise
        self.record(
            "info",
            "diagnostics",
            "diagnostics.export_completed",
            "A redacted local diagnostics export was generated.",
            outcome="success",
            stage="export",
            metadata={"count": len(manifest)},
        )
        return output

    @staticmethod
    def _sanitize_export_record(payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "schema",
            "timestamp",
            "sequence",
            "level",
            "feature",
            "source",
            "event_code",
            "message",
            "application_version",
            "launch_id",
            "request_id",
            "operation_id",
            "parent_operation_id",
            "error_id",
            "project_id",
            "run_id",
            "execution_id",
            "session_id",
            "outcome",
            "stage",
            "duration_ms",
            "retryable",
            "safe_failure_cause",
            "exception_type",
            "exception_chain",
            "stack_frames",
            "metadata",
        }
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "metadata" and isinstance(value, Mapping):
                result[key] = sanitize_metadata(value)
            else:
                result[key] = DiagnosticManager._sanitize_export_value(value, depth=0)
        return result

    @staticmethod
    def _sanitize_emergency_log(value: str) -> bytes:
        """Export only structured/summarized emergency startup lines."""

        safe_lines: list[str] = []
        exact_lines = {
            "[nebula] Nebula Core startup diagnostics (sensitive values redacted)",
            "[nebula] startup log truncated at 256 KiB",
        }
        summary = re.compile(
            r"^\[nebula\] Core stderr line redacted; bytes=\d+"
            r"(?:; exception_type=[A-Za-z_][A-Za-z0-9_.]{0,127})?$"
        )
        for line in value.splitlines():
            if line in exact_lines or summary.fullmatch(line):
                safe_lines.append(line)
                continue
            structured: dict[str, Any] | None = None
            prefix: str | None = None
            for candidate in (
                ERROR_MIRROR_PREFIX,
                "NEBULA_DIAGNOSTICS_UNAVAILABLE ",
            ):
                if line.startswith(candidate):
                    try:
                        decoded = json.loads(line.removeprefix(candidate))
                    except json.JSONDecodeError:
                        # diagnostic-expected: untrusted emergency text is replaced.
                        break
                    if isinstance(decoded, dict):
                        structured = DiagnosticManager._sanitize_export_record(decoded)
                        prefix = candidate
                    break
            if structured is not None and prefix is not None:
                safe_lines.append(
                    prefix
                    + json.dumps(structured, sort_keys=True, separators=(",", ":"))
                )
            else:
                byte_count = len(line.encode("utf-8", errors="replace"))
                safe_lines.append(
                    f"[nebula] Core stderr line redacted; bytes={byte_count}"
                )
        return ("\n".join(safe_lines) + ("\n" if safe_lines else "")).encode("utf-8")

    @staticmethod
    def _sanitize_export_value(value: Any, *, depth: int) -> Any:
        if depth > MAX_METADATA_DEPTH:
            return "[MAX_DEPTH]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _safe_text(value)
        if isinstance(value, Mapping):
            return {
                _safe_text(
                    str(key), limit=64
                ): DiagnosticManager._sanitize_export_value(item, depth=depth + 1)
                for key, item in list(value.items())[:MAX_METADATA_ITEMS]
                if not _DENIED_KEY.search(str(key))
            }
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [
                DiagnosticManager._sanitize_export_value(item, depth=depth + 1)
                for item in list(value)[:MAX_METADATA_ITEMS]
            ]
        return f"[{type(value).__name__}]"


_manager_lock = threading.RLock()
_manager: DiagnosticManager | None = None
_hooks_lock = threading.Lock()
_hooks_installed = False


def configure_diagnostics(
    data_dir: Path,
    *,
    log_dir: Path | None = None,
    settings_path: Path | None = None,
    desktop_parent: bool | None = None,
    level_override: str | None = None,
    feature_level_overrides: Mapping[str, str] | None = None,
    watch_settings: bool = True,
) -> DiagnosticManager:
    """Configure the process-global manager, closing any prior instance."""

    global _manager
    if log_dir is None and os.getenv("NEBULA_V3_LOG_DIR"):
        log_dir = Path(os.environ["NEBULA_V3_LOG_DIR"])
    if settings_path is None and os.getenv("NEBULA_V3_DIAGNOSTICS_SETTINGS"):
        settings_path = Path(os.environ["NEBULA_V3_DIAGNOSTICS_SETTINGS"])
    if desktop_parent is None:
        desktop_parent = os.getenv("NEBULA_V3_DIAGNOSTICS_PARENT", "").lower() in {
            "1",
            "true",
            "desktop",
        }
    with _manager_lock:
        if _manager is not None:
            _manager.close()
        _manager = DiagnosticManager(
            data_dir,
            log_dir=log_dir,
            settings_path=settings_path,
            desktop_parent=desktop_parent,
            level_override=level_override,
            feature_level_overrides=feature_level_overrides,
            watch_settings=watch_settings,
        )
        return _manager


def get_diagnostics() -> DiagnosticManager | None:
    return _manager


def require_diagnostics() -> DiagnosticManager:
    if _manager is None:
        raise DiagnosticsError("local diagnostics are not initialized")
    return _manager


def shutdown_diagnostics() -> None:
    """Flush, close, and clear the process-global diagnostics owner."""

    global _manager
    with _manager_lock:
        manager = _manager
        _manager = None
    if manager is not None:
        manager.close()


def record_diagnostic(
    level: str,
    feature: str,
    event_code: str,
    message: str,
    **fields: Any,
) -> str | None:
    manager = get_diagnostics()
    if manager is None:
        return None
    return manager.record(level, feature, event_code, message, **fields)


def diagnostic_error_id(exception: BaseException) -> str | None:
    """Return an error identifier already attached anywhere in an exception chain."""

    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate = getattr(current, "_nebula_diagnostic_error_id", None)
        if isinstance(candidate, str) and _ID.fullmatch(candidate):
            return candidate
        current = current.__cause__ or current.__context__
    return None


def diagnostic_error_feature(exception: BaseException) -> str | None:
    """Return the feature that first recorded an exception-chain failure."""

    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate = getattr(current, "_nebula_diagnostic_feature", None)
        if isinstance(candidate, str) and candidate in FEATURE_FILES:
            return candidate
        current = current.__cause__ or current.__context__
    return None


def _attach_error_id(
    exception: BaseException, error_id: str | None, feature: str | None = None
) -> None:
    if error_id is None:
        return
    with contextlib.suppress(Exception):
        # diagnostic-expected: some built-in exception instances are immutable.
        setattr(exception, "_nebula_diagnostic_error_id", error_id)
        if feature in FEATURE_FILES:
            setattr(exception, "_nebula_diagnostic_feature", feature)


def record_caught_exception(
    feature: str,
    event_code: str,
    message: str,
    exception: BaseException,
    *,
    stage: str,
) -> str | None:
    """Classify a reviewed catch site without recording exception messages.

    Validation/fallback conditions are warnings, cancellation is expected
    control flow at Debug, and other caught exceptions are operation failures.
    Callers still decide whether to recover, persist a failed state, or rethrow.
    """

    name = type(exception).__name__
    lowered_name = name.lower()
    status_code = getattr(exception, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    if isinstance(exception, (asyncio.CancelledError, GeneratorExit)) or any(
        marker in lowered_name
        for marker in ("disconnect", "stopasynciteration", "endofstream")
    ):
        level = "debug"
        outcome = "disconnected" if "disconnect" in lowered_name else "cancelled"
        retryable = False
        safe_cause = "The operation ended through expected control flow."
    elif status_code is not None and status_code < 500:
        level = "warning"
        outcome = "denied"
        retryable = status_code in {408, 409, 425, 429}
        safe_cause = "The operation was rejected safely."
    elif isinstance(
        exception,
        (
            ValueError,
            LookupError,
            UnicodeError,
            TimeoutError,
        ),
    ) or any(
        marker in lowered_name
        for marker in (
            "conflict",
            "validation",
            "invalid",
            "notfound",
            "not_found",
            "policy",
            "privacy",
            "denied",
            "capacity",
            "configuration",
            "unsupported",
            "stateerror",
            "state_error",
        )
    ):
        level = "warning"
        outcome = "fallback"
        retryable = isinstance(exception, TimeoutError)
        if isinstance(exception, TimeoutError):
            safe_cause = "The operation exceeded its bounded time limit."
        elif "conflict" in lowered_name:
            safe_cause = "The saved revision changed before the operation completed."
        elif "notfound" in lowered_name or "not_found" in lowered_name:
            safe_cause = "A required local entity or resource was not found."
        elif any(marker in lowered_name for marker in ("policy", "privacy", "denied")):
            safe_cause = "A safety or privacy policy denied the operation."
        else:
            safe_cause = "The operation encountered recoverable invalid or stale input."
    else:
        level = (
            "critical"
            if any(
                marker in lowered_name for marker in ("integrity", "securityinvariant")
            )
            else "error"
        )
        outcome = "failure"
        retryable = isinstance(exception, OSError)
        if isinstance(exception, PermissionError):
            safe_cause = (
                "The operating system denied access to a required local resource."
            )
        elif isinstance(exception, OSError):
            safe_cause = "A required local operating-system operation failed."
        elif "integrity" in lowered_name:
            safe_cause = "A required integrity verification failed."
        elif "provider" in lowered_name:
            safe_cause = (
                "The configured model provider could not complete the operation."
            )
        else:
            safe_cause = f"The operation raised {name}."
        existing = diagnostic_error_id(exception)
        if existing is not None:
            return existing
    stable_code = getattr(exception, "code", None)
    metadata = (
        {"code": stable_code}
        if isinstance(stable_code, str)
        and re.fullmatch(r"[a-z][a-z0-9._-]{1,159}", stable_code)
        else None
    )
    error_id = record_diagnostic(
        level,
        feature,
        event_code,
        message,
        outcome=outcome,
        stage=stage,
        retryable=retryable,
        safe_failure_cause=safe_cause,
        exception=exception,
        metadata=metadata,
    )
    if level in {"error", "critical"}:
        _attach_error_id(exception, error_id, feature)
    return error_id


@contextlib.contextmanager
def diagnostic_context(
    *,
    request_id: str | None = None,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[None]:
    values = (
        (_request_id, request_id),
        (_operation_id, operation_id),
        (_parent_operation_id, parent_operation_id),
        (_project_id, project_id),
        (_run_id, run_id),
        (_execution_id, execution_id),
        (_session_id, session_id),
    )
    tokens: list[
        tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]
    ] = []
    try:
        for variable, value in values:
            if value is not None:
                tokens.append((variable, variable.set(_safe_identifier(value))))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@contextlib.contextmanager
def diagnostic_operation(
    feature: str,
    event_prefix: str,
    message: str,
    *,
    operation_id: str | None = None,
    parent_operation_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[str]:
    operation = operation_id or new_operation_id()
    parent = parent_operation_id or _operation_id.get()
    started = time.monotonic()
    with diagnostic_context(operation_id=operation, parent_operation_id=parent):
        record_diagnostic(
            "info",
            feature,
            f"{event_prefix}.started",
            message,
            outcome="started",
            metadata=metadata,
        )
        try:
            yield operation
        except BaseException as exc:
            error_id = record_diagnostic(
                "error",
                feature,
                f"{event_prefix}.failed",
                f"{message} failed.",
                outcome="failure",
                duration_ms=(time.monotonic() - started) * 1000,
                retryable=False,
                exception=exc,
                metadata=metadata,
            )
            _attach_error_id(exc, error_id, feature)
            raise
        else:
            record_diagnostic(
                "info",
                feature,
                f"{event_prefix}.completed",
                f"{message} completed.",
                outcome="success",
                duration_ms=(time.monotonic() - started) * 1000,
                metadata=metadata,
            )


def current_request_id() -> str | None:
    return _request_id.get()


def current_operation_id() -> str | None:
    return _operation_id.get()


def create_diagnostic_task(
    coroutine: Any,
    *,
    feature: str,
    event_code: str,
    failure_message: str,
    durable_failure: Callable[[BaseException], Any] | None = None,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create a correlated task whose unhandled failure is never silent."""

    parent = current_operation_id()
    child = new_operation_id()

    task_context = contextvars.copy_context()
    task_context.run(_operation_id.set, child)
    task_context.run(_parent_operation_id.set, parent)
    # Passing the original coroutine directly avoids leaking it when a newly
    # created task is cancelled before its first scheduling opportunity.
    task = asyncio.create_task(coroutine, name=name, context=task_context)

    def completed(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            record_diagnostic(
                "debug",
                feature,
                f"{event_code}.cancelled",
                "A background operation was cancelled.",
                outcome="cancelled",
                operation_id=child,
                parent_operation_id=parent,
            )
            return
        try:
            exception = done.exception()
        except asyncio.CancelledError:
            record_diagnostic(
                "debug",
                feature,
                f"{event_code}.cancelled",
                "A background operation completed through cancellation.",
                outcome="cancelled",
                operation_id=child,
                parent_operation_id=parent,
            )
            return
        if exception is None:
            return
        if diagnostic_error_id(exception) is None:
            error_id = record_diagnostic(
                "error",
                feature,
                f"{event_code}.failed",
                failure_message,
                outcome="failure",
                retryable=False,
                exception=exception,
                operation_id=child,
                parent_operation_id=parent,
            )
            _attach_error_id(exception, error_id, feature)
        if durable_failure is not None:
            try:
                result = durable_failure(exception)
                if asyncio.iscoroutine(result):
                    create_diagnostic_task(
                        result,
                        feature=feature,
                        event_code=f"{event_code}.durable_state",
                        failure_message=(
                            "A failed background operation could not persist its "
                            "terminal state."
                        ),
                        name=(f"{name}-durable-state" if name else None),
                    )
            except Exception as callback_error:
                record_diagnostic(
                    "error",
                    feature,
                    f"{event_code}.durable_state_failed",
                    "A failed background operation could not persist its terminal state.",
                    outcome="failure",
                    stage="durable-state",
                    retryable=True,
                    exception=callback_error,
                    operation_id=child,
                    parent_operation_id=parent,
                )

    task.add_done_callback(completed, context=task_context)
    return task


async def gather_diagnostic(
    *awaitables: Awaitable[Any],
    feature: str,
    event_code: str,
    failure_message: str,
    stage: str,
) -> list[Any]:
    """Drain several operations and classify every returned exception."""

    results = list(await asyncio.gather(*awaitables, return_exceptions=True))
    for result in results:
        if isinstance(result, BaseException):
            record_caught_exception(
                feature,
                event_code,
                failure_message,
                result,
                stage=stage,
            )
    return results


def install_exception_hooks() -> None:
    """Install process, thread, and asyncio hooks without swallowing failures."""

    global _hooks_installed
    with _hooks_lock:
        if _hooks_installed:
            return
        _hooks_installed = True

    previous_sys_hook = sys.excepthook

    def system_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        trace: TracebackType | None,
    ) -> None:
        del exception_type, trace
        record_diagnostic(
            "critical",
            "diagnostics",
            "diagnostics.process_unhandled_exception",
            "Nebula Core stopped because of an unhandled exception.",
            outcome="failure",
            stage="process",
            retryable=False,
            exception=exception,
        )
        previous_sys_hook(type(exception), exception, exception.__traceback__)

    sys.excepthook = system_hook

    previous_thread_hook = threading.excepthook

    def thread_hook(arguments: threading.ExceptHookArgs) -> None:
        record_diagnostic(
            "critical",
            "diagnostics",
            "diagnostics.thread_unhandled_exception",
            "A Nebula Core thread stopped because of an unhandled exception.",
            outcome="failure",
            stage="thread",
            retryable=False,
            exception=arguments.exc_value,
            metadata={
                "component": arguments.thread.name if arguments.thread else "thread"
            },
        )
        previous_thread_hook(arguments)

    threading.excepthook = thread_hook
    _install_warning_hook()

    with contextlib.suppress(RuntimeError):
        # diagnostic-expected: synchronous startup may not yet own an event loop.
        install_asyncio_exception_hook()


def _install_warning_hook() -> None:
    """Route Python/third-party warnings without retaining their message or path."""

    if getattr(warnings.showwarning, "_nebula_diagnostics_hook", False):
        return

    def warning_hook(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: Any = None,
        line: str | None = None,
    ) -> None:
        del message, filename, lineno, file, line
        record_diagnostic(
            "warning",
            "diagnostics",
            "diagnostics.runtime.warning",
            "A Python runtime or dependency warning was emitted.",
            outcome="degraded",
            stage="runtime-warning",
            retryable=False,
            metadata={"category": category.__name__},
        )

    setattr(warning_hook, "_nebula_diagnostics_hook", True)
    warnings.showwarning = warning_hook
    _install_logging_adapter()


class _SanitizingDiagnosticHandler(logging.Handler):
    """Translate dependency log warnings without formatting their payload."""

    _nebula_diagnostics_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        level: DiagnosticLevel = (
            "error" if record.levelno >= logging.ERROR else "warning"
        )
        exception = (
            record.exc_info[1]
            if record.exc_info is not None
            and isinstance(record.exc_info[1], BaseException)
            else None
        )
        record_diagnostic(
            level,
            "diagnostics",
            f"diagnostics.runtime.log_{level}",
            "A Python dependency emitted a runtime diagnostic.",
            outcome="failure" if level == "error" else "degraded",
            stage="dependency-logging",
            retryable=False,
            exception=exception,
            metadata={"category": record.levelname},
        )


def _install_logging_adapter() -> None:
    root = logging.getLogger()
    if any(
        getattr(handler, "_nebula_diagnostics_handler", False)
        for handler in root.handlers
    ):
        return
    root.addHandler(_SanitizingDiagnosticHandler(level=logging.WARNING))


def install_asyncio_exception_hook() -> None:
    """Install the sanitizing handler on the currently running event loop."""

    loop = asyncio.get_running_loop()
    if getattr(loop, "_nebula_diagnostics_hook_installed", False):
        return
    previous_asyncio_hook = loop.get_exception_handler()

    def asyncio_hook(
        event_loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if not isinstance(exception, BaseException):
            exception = RuntimeError("unhandled asyncio operation")
        error_id = record_diagnostic(
            "error",
            "diagnostics",
            "diagnostics.asyncio_unhandled_exception",
            "An asynchronous Core operation failed without a handler.",
            outcome="failure",
            stage="asyncio",
            retryable=False,
            exception=exception,
        )
        _attach_error_id(exception, error_id, "diagnostics")
        if previous_asyncio_hook:
            previous_asyncio_hook(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(asyncio_hook)
    setattr(loop, "_nebula_diagnostics_hook_installed", True)


__all__ = [
    "DESKTOP_OWNED_FEATURES",
    "DIAGNOSTIC_SCHEMA",
    "ERROR_MIRROR_PREFIX",
    "FEATURE_FILES",
    "LEVELS",
    "SETTINGS_SCHEMA",
    "DiagnosticManager",
    "DiagnosticSettings",
    "DiagnosticsError",
    "configure_diagnostics",
    "create_diagnostic_task",
    "current_operation_id",
    "current_request_id",
    "diagnostic_error_feature",
    "diagnostic_error_id",
    "diagnostic_context",
    "diagnostic_operation",
    "gather_diagnostic",
    "get_diagnostics",
    "install_asyncio_exception_hook",
    "install_exception_hooks",
    "new_error_id",
    "new_operation_id",
    "new_request_id",
    "record_diagnostic",
    "record_caught_exception",
    "require_diagnostics",
    "sanitize_metadata",
    "shutdown_diagnostics",
]
