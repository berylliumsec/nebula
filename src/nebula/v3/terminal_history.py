"""Local command-only terminal history and OSC 633 frame parsing.

The tables in this module are intentionally separate from Nebula entities and
event ledgers. Command history is local convenience data: it is not evidence,
is not exported, and never contains terminal output.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
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
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .database import Base, Database, EntityRow
from .domain import Engagement, utc_now
from .storage import NotFoundError

COMMAND_RETENTION_DAYS = 90
MAX_COMMANDS_PER_PROJECT = 10_000
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1_000
MAX_COMMAND_BYTES = 1024 * 1024
MAX_CWD_BYTES = 16 * 1024


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
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    engagement_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(200), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TerminalCommandPreferenceRow(Base):
    __tablename__ = "terminal_command_preferences"

    engagement_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class _ExactTextModel(BaseModel):
    """Pydantic base that does not trim shell commands or working directories."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
    )


class TerminalCommandRecord(_ExactTextModel):
    id: str = Field(min_length=1, max_length=200)
    engagement_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1)
    cwd: str
    exit_code: int
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(timezone.utc)


class TerminalCommandHistoryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str = Field(min_length=1, max_length=200)
    enabled: bool
    record_count: int = Field(ge=0)
    retention_days: int = Field(ge=1)
    max_records: int = Field(ge=1)
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


class TerminalCommandHistoryClearResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str = Field(min_length=1, max_length=200)
    cleared: int = Field(ge=0)


class ParsedTerminalCommand(_ExactTextModel):
    command: str = Field(min_length=1)
    cwd: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class TerminalCommandParseResult:
    """Terminal bytes to display plus zero or more completed command records."""

    passthrough: bytes
    records: tuple[ParsedTerminalCommand, ...]


class TerminalCommandHistory:
    """Persist bounded, per-project command metadata without terminal output."""

    def __init__(
        self,
        database: Database,
        *,
        retention_days: int = COMMAND_RETENTION_DAYS,
        max_records: int = MAX_COMMANDS_PER_PROJECT,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.database = database
        self.retention_days = retention_days
        self.max_records = max_records
        self._clock = clock

    def status(self, engagement_id: str) -> TerminalCommandHistoryStatus:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        now = _aware_utc(self._clock(), field="clock")
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            self._prune(session, engagement_id, now)
            enabled = self._is_enabled(session, engagement_id)
            count, oldest, newest = session.execute(
                select(
                    func.count(TerminalCommandRow.id),
                    func.min(TerminalCommandRow.occurred_at),
                    func.max(TerminalCommandRow.occurred_at),
                ).where(TerminalCommandRow.engagement_id == engagement_id)
            ).one()
        return TerminalCommandHistoryStatus(
            engagement_id=engagement_id,
            enabled=enabled,
            record_count=int(count or 0),
            retention_days=self.retention_days,
            max_records=self.max_records,
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
    ) -> TerminalCommandRecord | None:
        """Record one completed command, or return ``None`` when disabled/expired."""

        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        session_id = _bounded_identifier("session_id", session_id)
        _validate_text_bytes("command", command, minimum=1, maximum=MAX_COMMAND_BYTES)
        _validate_text_bytes("cwd", cwd, minimum=0, maximum=MAX_CWD_BYTES)
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("exit_code must be an integer")
        if not -(2**31) <= exit_code < 2**31:
            raise ValueError("exit_code must fit a signed 32-bit integer")
        now = _aware_utc(self._clock(), field="clock")
        timestamp = _aware_utc(occurred_at or now, field="occurred_at")
        record_id = str(uuid4())

        with self.database.session() as session:
            self._require_project(session, engagement_id)
            self._prune(session, engagement_id, now)
            if not self._is_enabled(session, engagement_id):
                return None
            if timestamp < now - timedelta(days=self.retention_days):
                return None
            row = TerminalCommandRow(
                id=record_id,
                engagement_id=engagement_id,
                session_id=session_id,
                command=command,
                cwd=cwd,
                exit_code=exit_code,
                occurred_at=timestamp,
            )
            session.add(row)
            session.flush()
            self._prune_count(session, engagement_id)

        return TerminalCommandRecord(
            id=record_id,
            engagement_id=engagement_id,
            session_id=session_id,
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            occurred_at=timestamp,
        )

    def list(
        self,
        engagement_id: str,
        *,
        search: str | None = None,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> TerminalCommandPage:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if search is not None:
            _validate_text_bytes("search", search, minimum=0, maximum=4096)
            search = search or None
        now = _aware_utc(self._clock(), field="clock")

        with self.database.session() as session:
            self._require_project(session, engagement_id)
            self._prune(session, engagement_id, now)
            predicate: Any = TerminalCommandRow.engagement_id == engagement_id
            if search is not None:
                escaped = _escape_like(search.casefold())
                predicate = predicate & func.lower(TerminalCommandRow.command).like(
                    f"%{escaped}%", escape="\\"
                )
            total = int(
                session.scalar(
                    select(func.count(TerminalCommandRow.id)).where(predicate)
                )
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

    def clear(self, engagement_id: str) -> int:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            result = session.execute(
                delete(TerminalCommandRow).where(
                    TerminalCommandRow.engagement_id == engagement_id
                )
            )
            return int(result.rowcount or 0)

    def set_enabled(
        self, engagement_id: str, *, enabled: bool
    ) -> TerminalCommandHistoryStatus:
        engagement_id = _bounded_identifier("engagement_id", engagement_id)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        now = _aware_utc(self._clock(), field="clock")
        with self.database.session() as session:
            self._require_project(session, engagement_id)
            row = session.get(TerminalCommandPreferenceRow, engagement_id)
            if row is None:
                session.add(
                    TerminalCommandPreferenceRow(
                        engagement_id=engagement_id,
                        enabled=enabled,
                        updated_at=now,
                    )
                )
            else:
                row.enabled = enabled
                row.updated_at = now
        return self.status(engagement_id)

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
    def _is_enabled(session: Session, engagement_id: str) -> bool:
        value = session.scalar(
            select(TerminalCommandPreferenceRow.enabled).where(
                TerminalCommandPreferenceRow.engagement_id == engagement_id
            )
        )
        return True if value is None else bool(value)

    def _prune(self, session: Session, engagement_id: str, now: datetime) -> None:
        cutoff = now - timedelta(days=self.retention_days)
        session.execute(
            delete(TerminalCommandRow).where(
                TerminalCommandRow.engagement_id == engagement_id,
                TerminalCommandRow.occurred_at < cutoff,
            )
        )
        self._prune_count(session, engagement_id)

    def _prune_count(self, session: Session, engagement_id: str) -> None:
        expired_ids = (
            select(TerminalCommandRow.id)
            .where(TerminalCommandRow.engagement_id == engagement_id)
            .order_by(
                TerminalCommandRow.occurred_at.desc(),
                TerminalCommandRow.id.desc(),
            )
            .offset(self.max_records)
        )
        session.execute(
            delete(TerminalCommandRow).where(
                TerminalCommandRow.id.in_(expired_ids)
            )
        )

    @staticmethod
    def _to_record(row: TerminalCommandRow) -> TerminalCommandRecord:
        occurred_at = _optional_utc(row.occurred_at)
        if occurred_at is None:  # The database column is non-nullable.
            raise ValueError("terminal command row is missing occurred_at")
        return TerminalCommandRecord(
            id=row.id,
            engagement_id=row.engagement_id,
            session_id=row.session_id,
            command=row.command,
            cwd=row.cwd,
            exit_code=row.exit_code,
            occurred_at=occurred_at,
        )


class Osc633CommandParser:
    """Strip valid Nebula command markers while preserving all other bytes.

    Only an incomplete possible marker is buffered between calls. The buffer is
    bounded, and completed terminal output is neither retained nor exposed on
    parsed records.
    """

    PREFIX = b"\x1b]633;NebulaCommand;"
    _BEL = b"\x07"
    _ST = b"\x1b\\"
    _EXIT_CODE = re.compile(rb"-?[0-9]{1,10}\Z")

    def __init__(self, *, max_frame_bytes: int = 2 * 1024 * 1024) -> None:
        if max_frame_bytes < len(self.PREFIX) + 5:
            raise ValueError("max_frame_bytes is too small")
        self.max_frame_bytes = max_frame_bytes
        self._pending = b""

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    def feed(self, chunk: bytes) -> TerminalCommandParseResult:
        if not isinstance(chunk, bytes):
            raise TypeError("terminal chunks must be bytes")
        data = self._pending + chunk
        self._pending = b""
        passthrough = bytearray()
        records: list[ParsedTerminalCommand] = []
        cursor = 0

        while cursor < len(data):
            marker = data.find(self.PREFIX, cursor)
            if marker < 0:
                tail_size = _matching_prefix_suffix(data[cursor:], self.PREFIX)
                end = len(data) - tail_size
                passthrough.extend(data[cursor:end])
                self._pending = data[end:]
                break

            passthrough.extend(data[cursor:marker])
            terminator = self._find_terminator(data, marker + len(self.PREFIX))
            if terminator is None:
                candidate = data[marker:]
                if len(candidate) <= self.max_frame_bytes:
                    self._pending = candidate
                    break
                # The candidate is not a bounded control frame. Emit its first
                # byte and scan the remainder so a later valid marker is found.
                passthrough.append(data[marker])
                cursor = marker + 1
                continue

            terminator_start, terminator_size = terminator
            frame_end = terminator_start + terminator_size
            raw_frame = data[marker:frame_end]
            payload = data[marker + len(self.PREFIX) : terminator_start]
            parsed = (
                None
                if len(raw_frame) > self.max_frame_bytes
                else self._parse_payload(payload)
            )
            if parsed is None:
                passthrough.extend(raw_frame)
            else:
                records.append(parsed)
            cursor = frame_end

        return TerminalCommandParseResult(bytes(passthrough), tuple(records))

    def flush(self) -> TerminalCommandParseResult:
        pending = self._pending
        self._pending = b""
        return TerminalCommandParseResult(pending, ())

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

    @classmethod
    def _parse_payload(cls, payload: bytes) -> ParsedTerminalCommand | None:
        parts = payload.split(b";", 2)
        if len(parts) != 3 or cls._EXIT_CODE.fullmatch(parts[0]) is None:
            return None
        try:
            exit_code = int(parts[0].decode("ascii"))
            if not -(2**31) <= exit_code < 2**31:
                return None
            cwd_bytes = base64.b64decode(parts[1], validate=True)
            command_bytes = base64.b64decode(parts[2], validate=True)
            if len(cwd_bytes) > MAX_CWD_BYTES:
                return None
            if not 1 <= len(command_bytes) <= MAX_COMMAND_BYTES:
                return None
            cwd = cwd_bytes.decode("utf-8")
            command = command_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError, binascii.Error):
            return None
        return ParsedTerminalCommand(command=command, cwd=cwd, exit_code=exit_code)


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
