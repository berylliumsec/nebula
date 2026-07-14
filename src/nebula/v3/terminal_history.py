"""Durable human-terminal command audit records and OSC 633 framing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .artifacts import ArtifactStore
from .database import Base, Database, EntityRow, OperationEventRow
from .domain import Artifact, Engagement, OperationEvent, utc_now
from .redaction import redacted_display
from .storage import NebulaStore, NotFoundError, StoreTransaction
from .kali_tool_inventory import TOOL_NAME_PATTERN

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1_000
MAX_COMMAND_BYTES = 1024 * 1024
MAX_CWD_BYTES = 16 * 1024
MAX_CAPTURED_OUTPUT_BYTES = 10 * 1024 * 1024
OUTPUT_PREVIEW_CHARACTERS = 4_096
LOGGER = logging.getLogger(__name__)

TerminalCommandStatus = Literal[
    "completed",
    "interrupted",
    "framing_lost",
    "capture_failed",
    "legacy_metadata_only",
]
TerminalOutputCaptureDecision = Literal[
    "selected_tool",
    "not_selected",
    "classification_failed",
    "capture_failed",
    "legacy_all_commands",
    "legacy_metadata_only",
]


class TerminalAuditImmutableError(RuntimeError):
    code = "immutable_audit_history"


class TerminalCommandRow(Base):
    __tablename__ = "terminal_command_records"
    __table_args__ = (
        Index(
            "ix_terminal_commands_project_time",
            "engagement_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_terminal_commands_project_session",
            "engagement_id",
            "session_id",
        ),
        Index(
            "ix_terminal_commands_project_operator",
            "engagement_id",
            "operator_id",
        ),
        Index(
            "ix_terminal_commands_project_status",
            "engagement_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operator_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    shell_sequence: Mapped[str | None] = mapped_column(String(200), nullable=True)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="legacy_metadata_only"
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    raw_output_artifact_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    redacted_output_artifact_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    observed_output_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_output_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    output_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    capture_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    capture_decision: Mapped[str] = mapped_column(
        String(40), nullable=False, default="legacy_metadata_only"
    )
    matched_tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recording_policy_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    runtime_image_digest: Mapped[str | None] = mapped_column(
        String(71), nullable=True
    )


class TerminalCommandPreferenceRow(Base):
    """Per-Project overlays on the verified Kali security-tool inventory."""

    __tablename__ = "terminal_command_preferences"

    engagement_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    custom_tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    disabled_tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class _ExactTextModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
    )


class TerminalCommandRecord(_ExactTextModel):
    id: str = Field(min_length=1, max_length=200)
    engagement_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    operator_id: str | None = Field(default=None, min_length=1, max_length=200)
    shell_sequence: str | None = None
    command: str = Field(min_length=1)
    command_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cwd: str
    status: TerminalCommandStatus
    exit_code: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    occurred_at: datetime
    raw_output_available: bool = False
    redacted_output_available: bool = False
    observed_output_bytes: int = Field(default=0, ge=0)
    captured_output_bytes: int = Field(default=0, ge=0)
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_truncated: bool = False
    output_preview: str = ""
    capture_error: str | None = None
    capture_decision: TerminalOutputCaptureDecision = "legacy_metadata_only"
    matched_tools: list[str] = Field(default_factory=list)
    recording_policy_revision: int | None = Field(default=None, ge=0)
    runtime_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @field_validator("occurred_at", "started_at", "completed_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("terminal command timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class TerminalCommandHistoryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    capture_mode: Literal["selected_tools"] = "selected_tools"
    record_count: int = Field(ge=0)
    recorded_output_count: int = Field(default=0, ge=0)
    metadata_only_count: int = Field(default=0, ge=0)
    classification_failure_count: int = Field(default=0, ge=0)
    degraded_count: int = Field(default=0, ge=0)
    truncated_count: int = Field(default=0, ge=0)
    audit_gap_count: int = Field(default=0, ge=0)
    captured_output_bytes: int = Field(default=0, ge=0)
    retention_days: int | None = None
    max_records: int | None = None
    oldest_recorded_at: datetime | None = None
    newest_recorded_at: datetime | None = None


class TerminalCommandPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[TerminalCommandRecord]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE)
    next_offset: int | None = Field(default=None, ge=0)


class TerminalCommandHistoryPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class TerminalRecordingToolsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_tools: list[str] = Field(default_factory=list, max_length=10_000)
    disabled_tools: list[str] = Field(default_factory=list, max_length=20_000)
    expected_revision: int = Field(ge=0)
    expected_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("custom_tools", "disabled_tools")
    @classmethod
    def valid_tool_names(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("tool names must be sorted and unique")
        if any(TOOL_NAME_PATTERN.fullmatch(name) is None for name in value):
            raise ValueError("tool names must be executable basenames")
        return value


class TerminalRecordingTools(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str
    inventory_status: Literal["verified", "unavailable"]
    runtime_image_digest: str | None = None
    manifest_sha256: str | None = None
    default_tools: list[str] = Field(default_factory=list)
    custom_tools: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)
    effective_tools: list[str] = Field(default_factory=list)
    revision: int = Field(ge=0)
    updated_at: datetime | None = None


class TerminalRecordingToolsConflict(RuntimeError):
    code = "terminal_recording_tools_conflict"


@dataclass(frozen=True, slots=True)
class TerminalRecordingPolicy:
    revision: int
    effective_tools: frozenset[str]
    runtime_image_digest: str | None
    manifest_sha256: str | None


class TerminalCommandHistoryClearResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engagement_id: str = Field(min_length=1, max_length=200)
    cleared: int = Field(ge=0)


class ParsedTerminalCommand(_ExactTextModel):
    """Compatibility record emitted by the pre-audit completion marker."""

    command: str = Field(min_length=1)
    cwd: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class CapturedTerminalCommand:
    shell_sequence: str
    command: str
    cwd: str
    status: TerminalCommandStatus
    exit_code: int | None
    started_at: datetime
    completed_at: datetime
    output: bytes
    observed_output_bytes: int
    output_sha256: str
    output_truncated: bool
    capture_error: str | None = None
    record_id: str | None = None
    spool_path: Path | None = field(default=None, compare=False)
    spool_metadata_path: Path | None = field(default=None, compare=False)
    capture_decision: TerminalOutputCaptureDecision = "legacy_all_commands"
    matched_tools: tuple[str, ...] = ()
    recording_policy_revision: int | None = None
    runtime_image_digest: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalCommandParseResult:
    passthrough: bytes
    records: tuple[ParsedTerminalCommand, ...] = ()
    captures: tuple[CapturedTerminalCommand, ...] = ()


@dataclass(slots=True)
class _CaptureAccumulator:
    record_id: str
    shell_sequence: str
    command: str
    cwd: str
    started_at: datetime
    max_output_bytes: int
    output: bytearray = field(default_factory=bytearray)
    observed_output_bytes: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    spool_path: Path | None = None
    spool_metadata_path: Path | None = None
    spool_metadata: dict[str, Any] = field(default_factory=dict)
    selected_tools: frozenset[str] | None = None
    matched_tools: set[str] = field(default_factory=set)
    recording_policy_revision: int | None = None
    runtime_image_digest: str | None = None
    classification_failed: bool = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.digest.update(data)
        self.observed_output_bytes += len(data)
        captured_size = (
            self.spool_path.stat().st_size
            if self.spool_path is not None and self.spool_path.exists()
            else len(self.output)
        )
        remaining = self.max_output_bytes - captured_size
        if remaining > 0:
            captured = data[:remaining]
            if self.spool_path is None:
                self.output.extend(captured)
            else:
                with self.spool_path.open("ab") as stream:
                    stream.write(captured)
                    stream.flush()
                    os.fsync(stream.fileno())
        if self.spool_metadata_path is not None:
            self.spool_metadata["observed_output_bytes"] = self.observed_output_bytes
            self.spool_metadata["output_sha256"] = self.digest.hexdigest()
            _write_spool_metadata(self.spool_metadata_path, self.spool_metadata)

    def observe_execution(self, command: str) -> None:
        if self.selected_tools is None:
            return
        try:
            executables = command_executables(command)
        except ValueError:
            self.classification_failed = True
            executables = ()
        self.matched_tools.update(
            executable
            for executable in executables
            if executable in self.selected_tools
        )
        if self.spool_metadata_path is not None:
            self.spool_metadata["matched_tools"] = sorted(self.matched_tools)
            self.spool_metadata["classification_failed"] = self.classification_failed
            _write_spool_metadata(self.spool_metadata_path, self.spool_metadata)

    def finish(
        self,
        *,
        status: TerminalCommandStatus,
        exit_code: int | None,
        completed_at: datetime,
        capture_error: str | None = None,
    ) -> CapturedTerminalCommand:
        output = (
            self.spool_path.read_bytes()
            if self.spool_path is not None and self.spool_path.exists()
            else bytes(self.output)
        )
        if self.selected_tools is None:
            capture_decision: TerminalOutputCaptureDecision = "legacy_all_commands"
        elif self.classification_failed:
            capture_decision = "classification_failed"
            capture_error = capture_error or "terminal command classification failed"
        elif self.matched_tools:
            capture_decision = "selected_tool"
        else:
            capture_decision = "not_selected"
        return CapturedTerminalCommand(
            shell_sequence=self.shell_sequence,
            command=self.command,
            cwd=self.cwd,
            status=status,
            exit_code=exit_code,
            started_at=self.started_at,
            completed_at=completed_at,
            output=output,
            observed_output_bytes=self.observed_output_bytes,
            output_sha256=self.digest.hexdigest(),
            output_truncated=self.observed_output_bytes > len(output),
            capture_error=capture_error,
            record_id=self.record_id,
            spool_path=self.spool_path,
            spool_metadata_path=self.spool_metadata_path,
            capture_decision=capture_decision,
            matched_tools=tuple(sorted(self.matched_tools)),
            recording_policy_revision=self.recording_policy_revision,
            runtime_image_digest=self.runtime_image_digest,
        )


class TerminalCommandHistory:
    """Persist project-lifetime terminal audit metadata and output artifacts."""

    def __init__(
        self,
        database: Database,
        *,
        store: NebulaStore | None = None,
        artifact_store: ArtifactStore | None = None,
        clock: Callable[[], datetime] = utc_now,
        **_legacy_limits: Any,
    ) -> None:
        self.database = database
        self.store = store
        self.artifact_store = artifact_store
        self._clock = clock
        self._inventory_lock = threading.RLock()
        self._inventory: tuple[str, str, tuple[str, ...]] | None = None
        self.spool_root = (
            artifact_store.root.parent / "terminal-audit-spool"
            if artifact_store is not None
            else None
        )
        if self.spool_root is not None:
            self.spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.spool_root.chmod(0o700)

    def new_parser(
        self,
        *,
        nonce: str,
        engagement_id: str,
        session_id: str,
        operator_id: str,
        runtime_image_digest: str | None = None,
        manifest_sha256: str | None = None,
        default_tools: tuple[str, ...] | list[str] = (),
    ) -> "Osc633CommandParser":
        policy_provider = (
            lambda: self.recording_policy(
                engagement_id,
                runtime_image_digest=runtime_image_digest,
                manifest_sha256=manifest_sha256,
                default_tools=default_tools,
            )
            if runtime_image_digest is not None
            and manifest_sha256 is not None
            and bool(default_tools)
            else None
        )
        return Osc633CommandParser(
            nonce=nonce,
            spool_root=self.spool_root,
            spool_context={
                "engagement_id": engagement_id,
                "session_id": session_id,
                "operator_id": operator_id,
            },
            clock=self._clock,
            policy_provider=policy_provider,
        )

    def register_tool_inventory(
        self,
        *,
        runtime_image_digest: str,
        manifest_sha256: str,
        default_tools: tuple[str, ...] | list[str],
    ) -> None:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest) is None:
            raise ValueError("runtime image digest is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
            raise ValueError("security-tool manifest digest is invalid")
        normalized = _normalized_tool_names(default_tools)
        if not normalized:
            raise ValueError("security-tool inventory cannot be empty")
        with self._inventory_lock:
            self._inventory = (runtime_image_digest, manifest_sha256, normalized)

    def recording_tools(self, engagement_id: str) -> TerminalRecordingTools:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        with self._inventory_lock:
            inventory = self._inventory
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            row = session.get(TerminalCommandPreferenceRow, engagement_id)
            custom, disabled, revision, updated_at = self._preference_values(row)
        defaults = inventory[2] if inventory is not None else ()
        effective = sorted((set(defaults) | set(custom)) - set(disabled))
        return TerminalRecordingTools(
            engagement_id=engagement_id,
            inventory_status="verified" if inventory is not None else "unavailable",
            runtime_image_digest=inventory[0] if inventory is not None else None,
            manifest_sha256=inventory[1] if inventory is not None else None,
            default_tools=list(defaults),
            custom_tools=list(custom),
            disabled_tools=list(disabled),
            effective_tools=effective,
            revision=revision,
            updated_at=updated_at,
        )

    def update_recording_tools(
        self,
        engagement_id: str,
        request: TerminalRecordingToolsUpdate,
        *,
        actor_id: str,
    ) -> TerminalRecordingTools:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        actor_id = _bounded_identifier("actor_id", actor_id)
        with self._inventory_lock:
            inventory = self._inventory
        current_manifest = inventory[1] if inventory is not None else None
        if (
            request.expected_manifest_sha256 is not None
            and request.expected_manifest_sha256 != current_manifest
        ):
            raise TerminalRecordingToolsConflict(
                "the verified Kali security-tool inventory changed; reload before saving"
            )
        now = _aware_utc(self._clock(), field="clock")
        next_revision = request.expected_revision + 1
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            row = session.get(TerminalCommandPreferenceRow, engagement_id)
            observed_revision = row.revision if row is not None else 0
            if observed_revision != request.expected_revision:
                raise TerminalRecordingToolsConflict(
                    "terminal recording-tool preference revision changed"
                )
            custom_json = _dump_tool_names(request.custom_tools)
            disabled_json = _dump_tool_names(request.disabled_tools)
            if row is None:
                row = TerminalCommandPreferenceRow(
                    engagement_id=engagement_id,
                    enabled=True,
                    custom_tools=custom_json,
                    disabled_tools=disabled_json,
                    revision=next_revision,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.enabled = True
                row.custom_tools = custom_json
                row.disabled_tools = disabled_json
                row.revision = next_revision
                row.updated_at = now
            operation_id = f"terminal-recording-policy:{engagement_id}"
            last_sequence = session.scalar(
                select(func.max(OperationEventRow.sequence)).where(
                    OperationEventRow.operation_id == operation_id
                )
            )
            event = OperationEvent(
                operation_id=operation_id,
                operation_kind="terminal_recording_policy",
                engagement_id=engagement_id,
                sequence=int(last_sequence or 0) + 1,
                event_type="terminal.recording_policy.updated",
                payload={
                    "revision": next_revision,
                    "custom_tools": request.custom_tools,
                    "disabled_tools": request.disabled_tools,
                    "manifest_sha256": current_manifest,
                },
                actor_id=actor_id,
                idempotency_key=f"terminal-recording-policy:{next_revision}",
                occurred_at=now,
            )
            session.add(OperationEventRow(**event.model_dump(mode="python")))
            session.flush()
        return self.recording_tools(engagement_id)

    def recording_policy(
        self,
        engagement_id: str,
        *,
        runtime_image_digest: str | None,
        manifest_sha256: str | None,
        default_tools: tuple[str, ...] | list[str],
    ) -> TerminalRecordingPolicy:
        defaults = set(_normalized_tool_names(default_tools))
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            row = session.get(TerminalCommandPreferenceRow, engagement_id)
            custom, disabled, revision, _updated_at = self._preference_values(row)
        return TerminalRecordingPolicy(
            revision=revision,
            effective_tools=frozenset((defaults | set(custom)) - set(disabled)),
            runtime_image_digest=runtime_image_digest,
            manifest_sha256=manifest_sha256,
        )

    @staticmethod
    def _preference_values(
        row: TerminalCommandPreferenceRow | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...], int, datetime | None]:
        if row is None:
            return (), (), 0, None
        return (
            _load_tool_names(row.custom_tools),
            _load_tool_names(row.disabled_tools),
            row.revision,
            _optional_utc(row.updated_at),
        )

    def recover_spools(self) -> int:
        """Commit bounded output left by an interrupted Core process."""

        if self.spool_root is None:
            return 0
        recovered = 0
        for metadata_path in sorted(self.spool_root.glob("*.json")):
            metadata: dict[str, Any] | None = None
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                raw_path = self.spool_root / f"{metadata_path.stem}.raw"
                output = raw_path.read_bytes() if raw_path.exists() else b""
                observed = max(
                    len(output), int(metadata.get("observed_output_bytes", len(output)))
                )
                output_sha256 = str(
                    metadata.get("output_sha256", hashlib.sha256(output).hexdigest())
                )
                if re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None:
                    raise ValueError("recovered terminal output hash is invalid")
                started_at = datetime.fromisoformat(str(metadata["started_at"]))
                matched_tools = _normalized_tool_names(
                    metadata.get("matched_tools", [])
                )
                policy_revision_value = metadata.get("recording_policy_revision")
                policy_revision = (
                    int(policy_revision_value)
                    if policy_revision_value is not None
                    else None
                )
                if policy_revision is None:
                    decision: TerminalOutputCaptureDecision = "legacy_all_commands"
                elif bool(metadata.get("classification_failed")):
                    decision = "classification_failed"
                elif matched_tools:
                    decision = "selected_tool"
                else:
                    decision = "not_selected"
                capture = CapturedTerminalCommand(
                    shell_sequence=str(metadata["shell_sequence"]),
                    command=str(metadata["command"]),
                    cwd=str(metadata["cwd"]),
                    status="interrupted",
                    exit_code=None,
                    started_at=_aware_utc(started_at, field="started_at"),
                    completed_at=_aware_utc(self._clock(), field="clock"),
                    output=output,
                    observed_output_bytes=observed,
                    output_sha256=output_sha256,
                    output_truncated=observed > len(output),
                    capture_error="Core restarted before the command completion marker",
                    record_id=str(metadata["record_id"]),
                    spool_path=raw_path,
                    spool_metadata_path=metadata_path,
                    capture_decision=decision,
                    matched_tools=matched_tools,
                    recording_policy_revision=policy_revision,
                    runtime_image_digest=metadata.get("runtime_image_digest"),
                )
                self.record_capture(
                    engagement_id=str(metadata["engagement_id"]),
                    session_id=str(metadata["session_id"]),
                    operator_id=str(metadata["operator_id"]),
                    capture=capture,
                )
                recovered += 1
            except Exception as exc:
                # Preserve malformed or temporarily uncommittable spools for
                # doctor/recovery rather than silently deleting audit bytes.
                if self.store is not None and isinstance(metadata, dict):
                    try:
                        engagement_id = _bounded_identifier(
                            "engagement_id", str(metadata["engagement_id"])
                        )
                        session_id = _bounded_identifier(
                            "session_id", str(metadata["session_id"])
                        )
                        actor = str(metadata.get("operator_id") or "system")
                        self.store.append_operation_event(
                            session_id,
                            "container_terminal",
                            engagement_id,
                            "container_terminal.audit_gap",
                            {
                                "status": "capture_failed",
                                "record_id": metadata.get("record_id"),
                                "shell_sequence": metadata.get("shell_sequence"),
                                "output_sha256": metadata.get("output_sha256"),
                                "error": f"spool_recovery_{type(exc).__name__}",
                            },
                            actor_id=actor,
                            idempotency_key=(
                                "terminal-audit-spool-recovery:"
                                f"{metadata_path.stem}"
                            ),
                        )
                    except Exception:
                        LOGGER.error(
                            "terminal audit spool recovery gap could not be persisted (%s)",
                            type(exc).__name__,
                        )
                continue
        return recovered

    def status(self, engagement_id: str) -> TerminalCommandHistoryStatus:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        degraded = ("interrupted", "framing_lost", "capture_failed")
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            count, oldest, newest, output_bytes = session.execute(
                select(
                    func.count(TerminalCommandRow.id),
                    func.min(TerminalCommandRow.occurred_at),
                    func.max(TerminalCommandRow.occurred_at),
                    func.coalesce(func.sum(TerminalCommandRow.captured_output_bytes), 0),
                ).where(TerminalCommandRow.engagement_id == engagement_id)
            ).one()
            degraded_count = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(
                        TerminalCommandRow.engagement_id == engagement_id,
                        TerminalCommandRow.status.in_(degraded),
                    )
                )
                or 0
            )
            recorded_output_count = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(
                        TerminalCommandRow.engagement_id == engagement_id,
                        TerminalCommandRow.capture_decision.in_(
                            ("selected_tool", "legacy_all_commands")
                        ),
                        TerminalCommandRow.raw_output_artifact_id.is_not(None),
                    )
                )
                or 0
            )
            metadata_only_count = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(
                        TerminalCommandRow.engagement_id == engagement_id,
                        TerminalCommandRow.capture_decision.in_(
                            ("not_selected", "legacy_metadata_only")
                        ),
                    )
                )
                or 0
            )
            classification_failure_count = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(
                        TerminalCommandRow.engagement_id == engagement_id,
                        TerminalCommandRow.capture_decision
                        == "classification_failed",
                    )
                )
                or 0
            )
            truncated_count = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(
                        TerminalCommandRow.engagement_id == engagement_id,
                        TerminalCommandRow.output_truncated.is_(True),
                    )
                )
                or 0
            )
            audit_gap_count = int(
                session.scalar(
                    select(func.count(OperationEventRow.id)).where(
                        OperationEventRow.engagement_id == engagement_id,
                        OperationEventRow.event_type
                        == "container_terminal.audit_gap",
                    )
                )
                or 0
            )
        return TerminalCommandHistoryStatus(
            engagement_id=engagement_id,
            record_count=int(count or 0),
            recorded_output_count=recorded_output_count,
            metadata_only_count=metadata_only_count,
            classification_failure_count=classification_failure_count,
            degraded_count=degraded_count,
            truncated_count=truncated_count,
            audit_gap_count=audit_gap_count,
            captured_output_bytes=int(output_bytes or 0),
            oldest_recorded_at=_optional_utc(oldest),
            newest_recorded_at=_optional_utc(newest),
        )

    def record(
        self,
        *,
        engagement_id: str,
        session_id: str,
        command: str,
        cwd: str,
        exit_code: int,
        occurred_at: datetime | None = None,
    ) -> TerminalCommandRecord:
        """Persist compatibility metadata when no framed result is available."""

        timestamp = _aware_utc(occurred_at or self._clock(), field="occurred_at")
        return self._insert_metadata_record(
            engagement_id=engagement_id,
            session_id=session_id,
            operator_id=None,
            shell_sequence=None,
            command=command,
            cwd=cwd,
            status="legacy_metadata_only",
            exit_code=exit_code,
            started_at=timestamp,
            completed_at=timestamp,
            occurred_at=timestamp,
            capture_error=(
                "result output and operator attribution were not captured by the "
                "legacy command marker"
            ),
        )

    def record_capture(
        self,
        *,
        engagement_id: str,
        session_id: str,
        operator_id: str,
        capture: CapturedTerminalCommand,
    ) -> TerminalCommandRecord:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        session_id = _bounded_identifier("session_id", session_id)
        operator_id = _bounded_identifier("operator_id", operator_id)
        _validate_text_bytes("command", capture.command, minimum=1, maximum=MAX_COMMAND_BYTES)
        _validate_text_bytes("cwd", capture.cwd, minimum=0, maximum=MAX_CWD_BYTES)
        if capture.observed_output_bytes < len(capture.output):
            raise ValueError("observed terminal output cannot be smaller than captured output")
        if capture.output_truncated != (
            capture.observed_output_bytes > len(capture.output)
        ):
            raise ValueError("terminal output truncation metadata is inconsistent")
        if not capture.output_truncated and hashlib.sha256(
            capture.output
        ).hexdigest() != capture.output_sha256:
            raise ValueError("terminal output hash does not match captured bytes")
        if capture.capture_decision in {"not_selected", "classification_failed"}:
            try:
                return self._insert_metadata_record(
                    engagement_id=engagement_id,
                    session_id=session_id,
                    operator_id=operator_id,
                    shell_sequence=capture.shell_sequence,
                    command=capture.command,
                    cwd=capture.cwd,
                    status=capture.status,
                    exit_code=capture.exit_code,
                    started_at=capture.started_at,
                    completed_at=capture.completed_at,
                    occurred_at=capture.completed_at,
                    capture_error=capture.capture_error,
                    capture_decision=capture.capture_decision,
                    matched_tools=capture.matched_tools,
                    recording_policy_revision=capture.recording_policy_revision,
                    runtime_image_digest=capture.runtime_image_digest,
                    record_id=capture.record_id,
                )
            finally:
                self._cleanup_spool(capture)
        if self.store is None or self.artifact_store is None:
            return self._insert_metadata_record(
                engagement_id=engagement_id,
                session_id=session_id,
                operator_id=operator_id,
                shell_sequence=capture.shell_sequence,
                command=capture.command,
                cwd=capture.cwd,
                status="capture_failed",
                exit_code=capture.exit_code,
                started_at=capture.started_at,
                completed_at=capture.completed_at,
                occurred_at=capture.completed_at,
                output_sha256=capture.output_sha256,
                observed_output_bytes=capture.observed_output_bytes,
                output_truncated=capture.output_truncated,
                capture_error="terminal artifact storage is unavailable",
                capture_decision="capture_failed",
                matched_tools=capture.matched_tools,
                recording_policy_revision=capture.recording_policy_revision,
                runtime_image_digest=capture.runtime_image_digest,
                record_id=capture.record_id,
            )

        record_id = capture.record_id or str(uuid4())
        with self.database.session() as session:
            existing = session.get(TerminalCommandRow, record_id)
            if existing is not None:
                self._cleanup_spool(capture)
                return self._to_record(existing)
        command_sha256 = hashlib.sha256(capture.command.encode("utf-8")).hexdigest()
        redacted = redacted_display(capture.output.decode("utf-8", errors="replace"))
        raw = self.artifact_store.put_bytes_with_status(
            capture.output,
            engagement_id=engagement_id,
            filename=f"terminal-command-{record_id}-output.raw",
            media_type="application/octet-stream",
            source="human-terminal-audit-raw",
            metadata={"terminal_command_id": record_id, "session_id": session_id},
        )
        safe = self.artifact_store.put_bytes_with_status(
            redacted.encode("utf-8"),
            engagement_id=engagement_id,
            filename=f"terminal-command-{record_id}-output.txt",
            media_type="text/plain",
            source="human-terminal-audit-redacted",
            metadata={
                "terminal_command_id": record_id,
                "session_id": session_id,
                "redacted": True,
            },
        )
        row = TerminalCommandRow(
            id=record_id,
            engagement_id=engagement_id,
            session_id=session_id,
            operator_id=operator_id,
            shell_sequence=capture.shell_sequence,
            command=capture.command,
            command_sha256=command_sha256,
            cwd=capture.cwd,
            status=capture.status,
            exit_code=capture.exit_code,
            started_at=_aware_utc(capture.started_at, field="started_at"),
            completed_at=_aware_utc(capture.completed_at, field="completed_at"),
            occurred_at=_aware_utc(capture.completed_at, field="completed_at"),
            raw_output_artifact_id=raw.artifact.id,
            redacted_output_artifact_id=safe.artifact.id,
            observed_output_bytes=capture.observed_output_bytes,
            captured_output_bytes=len(capture.output),
            output_sha256=capture.output_sha256,
            output_truncated=capture.output_truncated,
            output_preview=redacted[:OUTPUT_PREVIEW_CHARACTERS],
            capture_error=capture.capture_error,
            capture_decision=capture.capture_decision,
            matched_tools=_dump_tool_names(capture.matched_tools),
            recording_policy_revision=capture.recording_policy_revision,
            runtime_image_digest=capture.runtime_image_digest,
        )
        event_payload = {
            "record_id": record_id,
            "shell_sequence": capture.shell_sequence,
            "status": capture.status,
            "exit_code": capture.exit_code,
            "command_sha256": command_sha256,
            "output_sha256": capture.output_sha256,
            "raw_output_artifact_id": raw.artifact.id,
            "redacted_output_artifact_id": safe.artifact.id,
            "observed_output_bytes": capture.observed_output_bytes,
            "captured_output_bytes": len(capture.output),
            "output_truncated": capture.output_truncated,
            "capture_decision": capture.capture_decision,
            "matched_tools": list(capture.matched_tools),
            "recording_policy_revision": capture.recording_policy_revision,
            "runtime_image_digest": capture.runtime_image_digest,
        }
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            StoreTransaction(session).add_all([raw.artifact, safe.artifact])
            session.add(row)
            last_sequence = session.scalar(
                select(func.max(OperationEventRow.sequence)).where(
                    OperationEventRow.operation_id == session_id
                )
            )
            event = OperationEvent(
                operation_id=session_id,
                operation_kind="container_terminal",
                engagement_id=engagement_id,
                sequence=int(last_sequence or 0) + 1,
                event_type="container_terminal.command",
                payload=event_payload,
                actor_id=operator_id,
                idempotency_key=(
                    f"container-terminal:{session_id}:command:{record_id}"
                ),
                occurred_at=capture.completed_at,
            )
            session.add(
                OperationEventRow(**event.model_dump(mode="python"))
            )
            session.flush()
        self._cleanup_spool(capture)
        return self._to_record(row)

    @staticmethod
    def _cleanup_spool(capture: CapturedTerminalCommand) -> None:
        if capture.spool_path is not None:
            capture.spool_path.unlink(missing_ok=True)
        if capture.spool_metadata_path is not None:
            capture.spool_metadata_path.unlink(missing_ok=True)

    def _insert_metadata_record(
        self,
        *,
        engagement_id: str,
        session_id: str,
        operator_id: str | None,
        shell_sequence: str | None,
        command: str,
        cwd: str,
        status: TerminalCommandStatus,
        exit_code: int | None,
        started_at: datetime | None,
        completed_at: datetime | None,
        occurred_at: datetime,
        output_sha256: str | None = None,
        observed_output_bytes: int = 0,
        output_truncated: bool = False,
        capture_error: str | None = None,
        capture_decision: TerminalOutputCaptureDecision = "legacy_metadata_only",
        matched_tools: tuple[str, ...] | list[str] = (),
        recording_policy_revision: int | None = None,
        runtime_image_digest: str | None = None,
        record_id: str | None = None,
    ) -> TerminalCommandRecord:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        session_id = _bounded_identifier("session_id", session_id)
        if operator_id is not None:
            operator_id = _bounded_identifier("operator_id", operator_id)
        _validate_text_bytes("command", command, minimum=1, maximum=MAX_COMMAND_BYTES)
        _validate_text_bytes("cwd", cwd, minimum=0, maximum=MAX_CWD_BYTES)
        normalized_tools = _normalized_tool_names(matched_tools)
        command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
        row = TerminalCommandRow(
            id=record_id or str(uuid4()),
            engagement_id=engagement_id,
            session_id=session_id,
            operator_id=operator_id,
            shell_sequence=shell_sequence,
            command=command,
            command_sha256=command_sha256,
            cwd=cwd,
            status=status,
            exit_code=exit_code,
            started_at=_optional_utc(started_at),
            completed_at=_optional_utc(completed_at),
            occurred_at=_aware_utc(occurred_at, field="occurred_at"),
            observed_output_bytes=observed_output_bytes,
            captured_output_bytes=0,
            output_sha256=output_sha256,
            output_truncated=output_truncated,
            output_preview="",
            capture_error=capture_error,
            capture_decision=capture_decision,
            matched_tools=_dump_tool_names(normalized_tools),
            recording_policy_revision=recording_policy_revision,
            runtime_image_digest=runtime_image_digest,
        )
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            existing = session.get(TerminalCommandRow, row.id)
            if existing is not None:
                return self._to_record(existing)
            session.add(row)
            if operator_id is not None and shell_sequence is not None:
                last_sequence = session.scalar(
                    select(func.max(OperationEventRow.sequence)).where(
                        OperationEventRow.operation_id == session_id
                    )
                )
                event = OperationEvent(
                    operation_id=session_id,
                    operation_kind="container_terminal",
                    engagement_id=engagement_id,
                    sequence=int(last_sequence or 0) + 1,
                    event_type="container_terminal.command",
                    payload={
                        "record_id": row.id,
                        "shell_sequence": shell_sequence,
                        "status": status,
                        "exit_code": exit_code,
                        "command_sha256": command_sha256,
                        "output_sha256": output_sha256,
                        "raw_output_artifact_id": None,
                        "redacted_output_artifact_id": None,
                        "observed_output_bytes": observed_output_bytes,
                        "captured_output_bytes": 0,
                        "output_truncated": output_truncated,
                        "capture_decision": capture_decision,
                        "matched_tools": list(normalized_tools),
                        "recording_policy_revision": recording_policy_revision,
                        "runtime_image_digest": runtime_image_digest,
                    },
                    actor_id=operator_id,
                    idempotency_key=(
                        f"container-terminal:{session_id}:command:{row.id}"
                    ),
                    occurred_at=occurred_at,
                )
                session.add(OperationEventRow(**event.model_dump(mode="python")))
            session.flush()
        return self._to_record(row)

    def list(
        self,
        engagement_id: str,
        *,
        search: str | None = None,
        operator_id: str | None = None,
        session_id: str | None = None,
        status: TerminalCommandStatus | None = None,
        exit_code: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> TerminalCommandPage:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        predicate: Any = TerminalCommandRow.engagement_id == engagement_id
        if search is not None:
            _validate_text_bytes("search", search, minimum=0, maximum=4096)
            if search:
                escaped = _escape_like(search.casefold())
                predicate = predicate & func.lower(TerminalCommandRow.command).like(
                    f"%{escaped}%", escape="\\"
                )
        if operator_id:
            predicate = predicate & (TerminalCommandRow.operator_id == operator_id)
        if session_id:
            predicate = predicate & (TerminalCommandRow.session_id == session_id)
        if status:
            predicate = predicate & (TerminalCommandRow.status == status)
        if exit_code is not None:
            predicate = predicate & (TerminalCommandRow.exit_code == exit_code)
        if date_from is not None:
            predicate = predicate & (
                TerminalCommandRow.occurred_at >= _aware_utc(date_from, field="date_from")
            )
        if date_to is not None:
            predicate = predicate & (
                TerminalCommandRow.occurred_at <= _aware_utc(date_to, field="date_to")
            )
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            total = int(
                session.scalar(select(func.count(TerminalCommandRow.id)).where(predicate))
                or 0
            )
            rows = session.scalars(
                select(TerminalCommandRow)
                .where(predicate)
                .order_by(
                    TerminalCommandRow.occurred_at.desc(),
                    TerminalCommandRow.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            ).all()
        records = [self._to_record(row) for row in rows]
        consumed = offset + len(records)
        return TerminalCommandPage(
            records=records,
            total=total,
            offset=offset,
            limit=limit,
            next_offset=consumed if consumed < total else None,
        )

    def all_records(self, engagement_id: str) -> list[TerminalCommandRecord]:
        records: list[TerminalCommandRecord] = []
        offset = 0
        while True:
            page = self.list(engagement_id, offset=offset, limit=MAX_PAGE_SIZE)
            records.extend(page.records)
            if page.next_offset is None:
                return records
            offset = page.next_offset

    def export_payload(
        self, engagement_id: str
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Return chronological audit records plus their immutable artifacts."""

        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            rows = session.scalars(
                select(TerminalCommandRow)
                .where(TerminalCommandRow.engagement_id == engagement_id)
                .order_by(TerminalCommandRow.occurred_at, TerminalCommandRow.id)
            ).all()
        payloads: list[dict[str, Any]] = []
        artifact_ids: set[str] = set()
        for row in rows:
            payload = self._to_record(row).model_dump(mode="json")
            payload["raw_output_artifact_id"] = row.raw_output_artifact_id
            payload["redacted_output_artifact_id"] = row.redacted_output_artifact_id
            payloads.append(payload)
            artifact_ids.update(
                value
                for value in (
                    row.raw_output_artifact_id,
                    row.redacted_output_artifact_id,
                )
                if value is not None
            )
        return payloads, artifact_ids

    def output_bytes(
        self, engagement_id: str, record_id: str, *, raw: bool
    ) -> tuple[bytes, str]:
        if self.store is None or self.artifact_store is None:
            raise NotFoundError("terminal audit artifact storage is unavailable")
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        with self.database.session() as session:
            row = session.get(TerminalCommandRow, record_id)
            if row is None or row.engagement_id != engagement_id:
                raise NotFoundError(f"terminal command record not found: {record_id}")
            artifact_id = (
                row.raw_output_artifact_id if raw else row.redacted_output_artifact_id
            )
        if artifact_id is None:
            raise NotFoundError("terminal command output is unavailable")
        artifact = self.store.get(Artifact, artifact_id)
        if not self.artifact_store.verify(artifact):
            raise ValueError("terminal command output failed integrity verification")
        return self.artifact_store.read(artifact), artifact.media_type

    def clear(self, engagement_id: str) -> int:
        self._require_existing_project(engagement_id)
        raise TerminalAuditImmutableError(
            "terminal audit records are retained for the Project lifetime"
        )

    def set_enabled(
        self, engagement_id: str, *, enabled: bool
    ) -> TerminalCommandHistoryStatus:
        self._require_existing_project(engagement_id)
        if enabled:
            return self.status(engagement_id)
        raise TerminalAuditImmutableError("terminal audit capture cannot be disabled")

    def _require_existing_project(self, engagement_id: str) -> None:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        with self.database.session() as session:
            self._require_project(session, engagement_id)

    @staticmethod
    def _require_project(session: Session, engagement_id: str) -> None:
        exists = session.scalar(
            select(EntityRow.id).where(
                EntityRow.id == engagement_id,
                EntityRow.kind == Engagement.entity_kind,
            )
        )
        if exists is None:
            raise NotFoundError(f"engagement entity not found: {engagement_id}")

    @staticmethod
    def _to_record(row: TerminalCommandRow) -> TerminalCommandRecord:
        occurred_at = _optional_utc(row.occurred_at)
        if occurred_at is None:
            raise ValueError("terminal command row is missing occurred_at")
        return TerminalCommandRecord(
            id=row.id,
            engagement_id=row.engagement_id,
            session_id=row.session_id,
            operator_id=row.operator_id,
            shell_sequence=row.shell_sequence,
            command=row.command,
            command_sha256=row.command_sha256,
            cwd=row.cwd,
            status=row.status,
            exit_code=row.exit_code,
            started_at=_optional_utc(row.started_at),
            completed_at=_optional_utc(row.completed_at),
            occurred_at=occurred_at,
            raw_output_available=row.raw_output_artifact_id is not None,
            redacted_output_available=row.redacted_output_artifact_id is not None,
            observed_output_bytes=row.observed_output_bytes,
            captured_output_bytes=row.captured_output_bytes,
            output_sha256=row.output_sha256,
            output_truncated=row.output_truncated,
            output_preview=row.output_preview,
            capture_error=row.capture_error,
            capture_decision=row.capture_decision,
            matched_tools=list(_load_tool_names(row.matched_tools)),
            recording_policy_revision=row.recording_policy_revision,
            runtime_image_digest=row.runtime_image_digest,
        )


class Osc633CommandParser:
    """Strip nonce-bound audit frames and associate exact PTY result bytes."""

    LEGACY_PREFIX = b"\x1b]633;NebulaCommand;"
    START_PREFIX = b"\x1b]633;NebulaCommandStart;"
    EXEC_PREFIX = b"\x1b]633;NebulaCommandExec;"
    END_PREFIX = b"\x1b]633;NebulaCommandEnd;"
    PREFIXES = (START_PREFIX, EXEC_PREFIX, END_PREFIX, LEGACY_PREFIX)
    _BEL = b"\x07"
    _ST = b"\x1b\\"
    _EXIT_CODE = re.compile(rb"-?[0-9]{1,10}\Z")

    def __init__(
        self,
        *,
        nonce: str | None = None,
        max_frame_bytes: int = 2 * 1024 * 1024,
        max_output_bytes: int = MAX_CAPTURED_OUTPUT_BYTES,
        spool_root: Path | None = None,
        spool_context: dict[str, str] | None = None,
        clock: Callable[[], datetime] = utc_now,
        policy_provider: Callable[[], TerminalRecordingPolicy] | None = None,
    ) -> None:
        if max_frame_bytes < len(self.START_PREFIX) + 5:
            raise ValueError("max_frame_bytes is too small")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        if nonce is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
            raise ValueError("terminal audit nonce is invalid")
        self.nonce = nonce
        self.max_frame_bytes = max_frame_bytes
        self.max_output_bytes = max_output_bytes
        self._clock = clock
        self.spool_root = spool_root
        self.spool_context = spool_context
        self.policy_provider = policy_provider
        self._pending = b""
        self._active: _CaptureAccumulator | None = None

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    @property
    def capture_active(self) -> bool:
        return self._active is not None

    def feed(self, chunk: bytes) -> TerminalCommandParseResult:
        if not isinstance(chunk, bytes):
            raise TypeError("terminal chunks must be bytes")
        data = self._pending + chunk
        self._pending = b""
        passthrough = bytearray()
        records: list[ParsedTerminalCommand] = []
        captures: list[CapturedTerminalCommand] = []
        cursor = 0

        while cursor < len(data):
            found = self._next_marker(data, cursor)
            if found is None:
                tail_size = max(
                    _matching_prefix_suffix(data[cursor:], prefix)
                    for prefix in self.PREFIXES
                )
                end = len(data) - tail_size
                visible = data[cursor:end]
                self._append_visible(passthrough, visible)
                self._pending = data[end:]
                break
            marker, prefix = found
            self._append_visible(passthrough, data[cursor:marker])
            terminator = self._find_terminator(data, marker + len(prefix))
            if terminator is None:
                candidate = data[marker:]
                if len(candidate) <= self.max_frame_bytes:
                    self._pending = candidate
                    break
                self._append_visible(passthrough, data[marker : marker + 1])
                cursor = marker + 1
                continue

            terminator_start, terminator_size = terminator
            frame_end = terminator_start + terminator_size
            raw_frame = data[marker:frame_end]
            payload = data[marker + len(prefix) : terminator_start]
            handled = False
            if len(raw_frame) <= self.max_frame_bytes:
                if prefix == self.START_PREFIX:
                    started = self._parse_start(payload)
                    if started is not None:
                        if self._active is not None:
                            captures.append(
                                self._active.finish(
                                    status="framing_lost",
                                    exit_code=None,
                                    completed_at=_aware_utc(self._clock(), field="clock"),
                                    capture_error="a new command started before the prior completion marker",
                                )
                            )
                        self._active = started
                        handled = True
                elif prefix == self.END_PREFIX:
                    ended = self._parse_end(payload)
                    if ended is not None and self._active is not None:
                        sequence, exit_code, classifier_ok = ended
                        if sequence == self._active.shell_sequence:
                            if classifier_ok is False:
                                self._active.classification_failed = True
                            captures.append(
                                self._active.finish(
                                    status="completed",
                                    exit_code=exit_code,
                                    completed_at=_aware_utc(self._clock(), field="clock"),
                                )
                            )
                            self._active = None
                            handled = True
                elif prefix == self.EXEC_PREFIX:
                    executed = self._parse_exec(payload)
                    if executed is not None:
                        _sequence, command = executed
                        handled = True
                        if self._active is not None:
                            self._active.observe_execution(command)
                    elif self._active is not None:
                        self._active.classification_failed = True
                elif self.nonce is None:
                    legacy = self._parse_legacy(payload)
                    if legacy is not None:
                        records.append(legacy)
                        handled = True
            if not handled:
                self._append_visible(passthrough, raw_frame)
            cursor = frame_end

        return TerminalCommandParseResult(
            passthrough=bytes(passthrough),
            records=tuple(records),
            captures=tuple(captures),
        )

    def flush(self) -> TerminalCommandParseResult:
        pending = self._pending
        self._pending = b""
        passthrough = bytearray()
        self._append_visible(passthrough, pending)
        return TerminalCommandParseResult(bytes(passthrough))

    def finish_active(
        self,
        *,
        exit_code: int | None = None,
        status: TerminalCommandStatus = "interrupted",
        detail: str | None = None,
    ) -> CapturedTerminalCommand | None:
        if self._active is None:
            return None
        capture = self._active.finish(
            status=status,
            exit_code=exit_code,
            completed_at=_aware_utc(self._clock(), field="clock"),
            capture_error=detail,
        )
        self._active = None
        return capture

    def _append_visible(self, passthrough: bytearray, data: bytes) -> None:
        passthrough.extend(data)
        if self._active is not None:
            self._active.append(data)

    @classmethod
    def _next_marker(cls, data: bytes, start: int) -> tuple[int, bytes] | None:
        candidates = [
            (position, prefix)
            for prefix in cls.PREFIXES
            if (position := data.find(prefix, start)) >= 0
        ]
        return min(candidates, key=lambda item: item[0]) if candidates else None

    @classmethod
    def _find_terminator(
        cls, data: bytes, start: int
    ) -> tuple[int, int] | None:
        bel = data.find(cls._BEL, start)
        st = data.find(cls._ST, start)
        if bel < 0 and st < 0:
            return None
        if bel >= 0 and (st < 0 or bel < st):
            return bel, 1
        return st, 2

    def _parse_start(self, payload: bytes) -> _CaptureAccumulator | None:
        parts = payload.split(b";", 3)
        if len(parts) != 4:
            return None
        try:
            nonce = parts[0].decode("ascii")
            sequence = parts[1].decode("ascii")
            cwd_bytes = base64.b64decode(parts[2], validate=True)
            command_bytes = base64.b64decode(parts[3], validate=True)
            if nonce != self.nonce or not sequence or len(sequence) > 200:
                return None
            if len(cwd_bytes) > MAX_CWD_BYTES:
                return None
            if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
                return None
            cwd = cwd_bytes.decode("utf-8")
            command = command_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError, binascii.Error):
            return None
        record_id = str(uuid4())
        started_at = _aware_utc(self._clock(), field="clock")
        policy: TerminalRecordingPolicy | None = None
        classification_failed = False
        if self.policy_provider is not None:
            try:
                policy = self.policy_provider()
            except Exception:
                policy = TerminalRecordingPolicy(
                    revision=0,
                    effective_tools=frozenset(),
                    runtime_image_digest=None,
                    manifest_sha256=None,
                )
                classification_failed = True
        spool_path: Path | None = None
        spool_metadata_path: Path | None = None
        spool_metadata: dict[str, Any] = {}
        if self.spool_root is not None and self.spool_context is not None:
            self.spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            spool_path = self.spool_root / f"{record_id}.raw"
            spool_metadata_path = self.spool_root / f"{record_id}.json"
            descriptor = os.open(
                spool_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
            spool_metadata = {
                **self.spool_context,
                "record_id": record_id,
                "shell_sequence": sequence,
                "command": command,
                "cwd": cwd,
                "started_at": started_at.isoformat(),
                "observed_output_bytes": 0,
                "output_sha256": hashlib.sha256().hexdigest(),
                "recording_policy_revision": policy.revision if policy else None,
                "runtime_image_digest": policy.runtime_image_digest if policy else None,
                "manifest_sha256": policy.manifest_sha256 if policy else None,
                "matched_tools": [],
                "classification_failed": classification_failed,
            }
            _write_spool_metadata(spool_metadata_path, spool_metadata)
            _fsync_directory(self.spool_root)
        return _CaptureAccumulator(
            record_id=record_id,
            shell_sequence=sequence,
            command=command,
            cwd=cwd,
            started_at=started_at,
            max_output_bytes=self.max_output_bytes,
            spool_path=spool_path,
            spool_metadata_path=spool_metadata_path,
            spool_metadata=spool_metadata,
            selected_tools=policy.effective_tools if policy is not None else None,
            recording_policy_revision=policy.revision if policy is not None else None,
            runtime_image_digest=policy.runtime_image_digest if policy is not None else None,
            classification_failed=classification_failed,
        )

    def _parse_end(self, payload: bytes) -> tuple[str, int, bool | None] | None:
        parts = payload.split(b";", 3)
        if len(parts) not in {3, 4} or self._EXIT_CODE.fullmatch(parts[2]) is None:
            return None
        try:
            nonce = parts[0].decode("ascii")
            sequence = parts[1].decode("ascii")
            exit_code = int(parts[2].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        if nonce != self.nonce or not sequence or len(sequence) > 200:
            return None
        if not -(2**31) <= exit_code < 2**31:
            return None
        classifier_ok: bool | None = None
        if len(parts) == 4:
            if parts[3] not in {b"0", b"1"}:
                return None
            classifier_ok = parts[3] == b"1"
        return sequence, exit_code, classifier_ok

    def _parse_exec(self, payload: bytes) -> tuple[str, str] | None:
        parts = payload.split(b";", 2)
        if len(parts) != 3:
            return None
        try:
            nonce = parts[0].decode("ascii")
            sequence = parts[1].decode("ascii")
            command_bytes = base64.b64decode(parts[2], validate=True)
            command = command_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError, binascii.Error):
            return None
        if nonce != self.nonce or not sequence or len(sequence) > 200:
            return None
        if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
            return None
        return sequence, command

    @classmethod
    def _parse_legacy(cls, payload: bytes) -> ParsedTerminalCommand | None:
        parts = payload.split(b";", 2)
        if len(parts) != 3 or cls._EXIT_CODE.fullmatch(parts[0]) is None:
            return None
        try:
            exit_code = int(parts[0].decode("ascii"))
            cwd_bytes = base64.b64decode(parts[1], validate=True)
            command_bytes = base64.b64decode(parts[2], validate=True)
            if not -(2**31) <= exit_code < 2**31:
                return None
            if len(cwd_bytes) > MAX_CWD_BYTES:
                return None
            if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
                return None
            cwd = cwd_bytes.decode("utf-8")
            command = command_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError, binascii.Error):
            return None
        return ParsedTerminalCommand(command=command, cwd=cwd, exit_code=exit_code)


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*\Z", re.DOTALL)
_WRAPPERS = frozenset(
    {"command", "env", "exec", "nohup", "sudo", "time", "timeout", "nice", "stdbuf"}
)


def command_executables(command: str) -> tuple[str, ...]:
    """Return executable basenames from one Bash DEBUG simple-command frame."""

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError("shell command could not be classified") from exc
    index = 0
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    observed: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in {"<", ">", ">>", "2>", "2>>", "&>"}:
            return tuple(observed)
        executable = os.path.basename(token)
        if TOOL_NAME_PATTERN.fullmatch(executable) is None:
            return tuple(observed)
        observed.append(executable)
        index += 1
        if executable not in _WRAPPERS:
            return tuple(dict.fromkeys(observed))
        index = _wrapper_target_index(executable, tokens, index)
        while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]):
            index += 1
    return tuple(dict.fromkeys(observed))


def _wrapper_target_index(wrapper: str, tokens: list[str], index: int) -> int:
    value_options: dict[str, frozenset[str]] = {
        "sudo": frozenset(
            {"-C", "-D", "-g", "-h", "-p", "-r", "-R", "-T", "-t", "-u"}
        ),
        "nice": frozenset({"-n"}),
        "stdbuf": frozenset({"-i", "-o", "-e"}),
        "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
        "env": frozenset({"-C", "--chdir", "-S", "--split-string", "-u", "--unset"}),
    }
    consumes = value_options.get(wrapper, frozenset())
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        option_name = token.split("=", 1)[0]
        if token.startswith("-") and token != "-":
            index += 1
            if option_name in consumes and "=" not in token and index < len(tokens):
                index += 1
            continue
        if wrapper == "env" and _ASSIGNMENT.fullmatch(token):
            index += 1
            continue
        break
    if wrapper == "timeout" and index < len(tokens):
        index += 1  # duration
    return index


def _normalized_tool_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("tool names must be a collection")
    names = tuple(sorted(set(value)))
    if any(
        not isinstance(name, str) or TOOL_NAME_PATTERN.fullmatch(name) is None
        for name in names
    ):
        raise ValueError("tool names must be executable basenames")
    return names


def _dump_tool_names(value: Any) -> str:
    return json.dumps(
        list(_normalized_tool_names(value)),
        separators=(",", ":"),
    )


def _load_tool_names(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored terminal tool preferences are invalid") from exc
    return _normalized_tool_names(decoded)


def _bounded_identifier(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 200:
        raise ValueError(f"{field} must contain between 1 and 200 characters")
    return normalized


def _validate_text_bytes(
    field: str, value: str, *, minimum: int, maximum: int
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    size = len(value.encode("utf-8"))
    if not minimum <= size <= maximum:
        raise ValueError(f"{field} must contain between {minimum} and {maximum} bytes")


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matching_prefix_suffix(data: bytes, prefix: bytes) -> int:
    maximum = min(len(data), len(prefix) - 1)
    for size in range(maximum, 0, -1):
        if data.endswith(prefix[:size]):
            return size
    return 0


def _write_spool_metadata(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
