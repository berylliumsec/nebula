"""Host-owned Butler memory with authoritative SQL fenced to its producer.

The optional vector index is derived. It is touched only in the host process,
after committed SQL; workers use the explicit private-memory protocol.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import Field
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from .butler_memory import (
    ButlerMemoryItem,
    ButlerMemoryService,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryRow,
    MemoryScope,
    UnsafeMemoryError,
)
from .database import ButlerMemoryCursorRow, ButlerMemoryItemRow
from .domain import NebulaModel
from .execution_scope import current_execution_lease
from .runtime_coordination import LeaseLost, RuntimeCoordination


class MemoryWrite(NebulaModel):
    source_project_id: str = Field(min_length=1, max_length=200)
    scope: MemoryScope
    content: str = Field(min_length=8, max_length=800, repr=False)
    source_kind: str = Field(min_length=1, max_length=100)
    source_label: str = Field(min_length=1, max_length=500)
    source_ref: str | None = Field(default=None, max_length=1000, repr=False)
    project_name: str = Field(default="", max_length=500)


def _data(row: ButlerMemoryItemRow) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class SharedButlerMemory(ButlerMemoryService):
    def __init__(
        self,
        coordination: RuntimeCoordination,
        index_path: Path,
        *,
        embedding_index=None,
    ):
        # The compatibility path is never opened. Base helpers retain cleaning,
        # visibility, capture, ranking and derived-index behavior.
        super().__init__(
            index_path / "unused-compatibility.sqlite3",
            index_path=index_path,
            embedding_index=embedding_index,
        )
        self.coordination = coordination

    async def _ensure_ready(self):
        # Schema ownership belongs to the supervisor, not to a memory caller.
        return

    @asynccontextmanager
    async def _authority(self, project_id: str):
        lease = current_execution_lease()
        if lease is None or lease.project_id != project_id:
            raise LeaseLost("Shared Butler memory requires exact project ownership")
        async with self.coordination.fenced(lease) as session:
            # Global preferences and lessons can deduplicate across two projects.
            # Serialize their uniqueness and edits even on different project rows.
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtext(current_database()), "
                        "hashtext(current_schema() || ':nebula:shared-memory:v1'))"
                    )
                )
            yield session

    async def _save(
        self,
        *,
        source_project_id: str,
        scope: MemoryScope,
        content: str,
        source_kind: str,
        source_label: str,
        source_ref: str | None,
        project_name: str = "",
    ) -> ButlerMemoryItem:
        request = MemoryWrite(
            source_project_id=source_project_id,
            scope=scope,
            content=content,
            source_kind=source_kind,
            source_label=source_label,
            source_ref=source_ref,
            project_name=project_name,
        )
        value = self._clean(
            request.content,
            global_lesson=scope == "global_lesson",
            project_name=project_name,
        )
        scope_key = source_project_id if scope == "project_lesson" else "global"
        digest = hashlib.sha256(value.casefold().encode()).hexdigest()
        async with self._lock:
            async with self._authority(source_project_id) as session:
                row = await session.scalar(
                    select(ButlerMemoryItemRow).where(
                        ButlerMemoryItemRow.scope == scope,
                        ButlerMemoryItemRow.scope_key == scope_key,
                        ButlerMemoryItemRow.content_hash == digest,
                    )
                )
                if row is None:
                    now = self._utc()
                    row = ButlerMemoryItemRow(
                        id=str(uuid4()),
                        scope=scope,
                        scope_key=scope_key,
                        source_project_id=source_project_id,
                        content=value,
                        content_hash=digest,
                        source_kind=source_kind,
                        source_label=source_label,
                        source_ref=source_ref,
                        created_at=now,
                        updated_at=now,
                        revision=1,
                    )
                    session.add(row)
                    await session.flush()
                saved = _data(row)
            await asyncio.to_thread(self._index_one, saved)
        return self._public(saved, source_project_id)

    async def _authorized_rows(self, engagement_id: str) -> list[MemoryRow]:
        async with self._authority(engagement_id) as session:
            return [
                _data(row)
                for row in await session.scalars(
                    select(ButlerMemoryItemRow)
                    .where(
                        or_(
                            ButlerMemoryItemRow.scope.in_(
                                ("global_preference", "global_lesson")
                            ),
                            (ButlerMemoryItemRow.scope == "project_lesson")
                            & (ButlerMemoryItemRow.scope_key == engagement_id),
                        )
                    )
                    .order_by(ButlerMemoryItemRow.updated_at.desc())
                    .limit(5000)
                )
            ]

    async def _review_lesson_contents(self, project_id: str) -> list[str]:
        async with self._authority(project_id) as session:
            return list(
                await session.scalars(
                    select(ButlerMemoryItemRow.content)
                    .where(
                        ButlerMemoryItemRow.scope == "project_lesson",
                        ButlerMemoryItemRow.scope_key == project_id,
                        ButlerMemoryItemRow.source_kind == "butler_review_candidate",
                    )
                    .order_by(ButlerMemoryItemRow.updated_at.desc())
                    .limit(100)
                )
            )

    async def _read_event_cursor(self, project_id: str) -> int:
        async with self._authority(project_id) as session:
            row = await session.get(ButlerMemoryCursorRow, project_id)
            return row.last_event_sequence if row else 0

    async def _advance_event_cursor(self, project_id: str, sequence: int) -> None:
        if type(sequence) is not int or not 0 <= sequence < 2**63:
            raise ValueError("Memory event cursor must be a nonnegative SQL sequence")
        async with self._authority(project_id) as session:
            row = await session.get(
                ButlerMemoryCursorRow, project_id, with_for_update=True
            )
            if row is None:
                session.add(
                    ButlerMemoryCursorRow(
                        project_id=project_id, last_event_sequence=sequence
                    )
                )
            else:
                row.last_event_sequence = max(row.last_event_sequence, sequence)

    async def update(
        self, engagement_id: str, item_id: str, content: str, *, expected_revision: int
    ) -> ButlerMemoryItem:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Memory editing requires its current revision")
        async with self._lock:
            try:
                async with self._authority(engagement_id) as session:
                    row = await session.get(
                        ButlerMemoryItemRow, item_id, with_for_update=True
                    )
                    if row is None or not self._visible(_data(row), engagement_id):
                        raise MemoryNotFoundError(
                            "Memory is not available in this project"
                        )
                    if row.revision != expected_revision:
                        raise MemoryConflictError(
                            "Memory changed; refresh before editing"
                        )
                    value = self._clean(
                        content,
                        global_lesson=row.scope == "global_lesson",
                        project_name=row.source_project_id,
                    )
                    if (
                        row.scope == "global_lesson"
                        and row.source_ref
                        and row.source_ref.casefold() in value.casefold()
                    ):
                        raise UnsafeMemoryError(
                            "A global lesson must omit source identifiers"
                        )
                    row.content, row.content_hash = (
                        value,
                        hashlib.sha256(value.casefold().encode()).hexdigest(),
                    )
                    row.updated_at, row.revision = self._utc(), row.revision + 1
                    await session.flush()
                    saved = _data(row)
            except IntegrityError as error:
                raise MemoryConflictError(
                    "An identical memory already exists"
                ) from error
            await asyncio.to_thread(self._index_one, saved)
        return self._public(saved, engagement_id)

    async def delete(
        self, engagement_id: str, item_id: str, *, expected_revision: int
    ) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Memory deletion requires its current revision")
        async with self._lock:
            async with self._authority(engagement_id) as session:
                row = await session.get(
                    ButlerMemoryItemRow, item_id, with_for_update=True
                )
                if row is None or not self._visible(_data(row), engagement_id):
                    raise MemoryNotFoundError("Memory is not available in this project")
                if row.revision != expected_revision:
                    raise MemoryConflictError("Memory changed; refresh before deleting")
                await session.delete(row)
            await asyncio.to_thread(self._delete_from_index, item_id)
