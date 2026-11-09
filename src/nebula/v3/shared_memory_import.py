"""One-time, resumable compatibility-memory import before runtime admission."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import os
import sqlite3
import stat

import aiosqlite
from sqlalchemy import func, select

from .async_utils import finish_cleanup
from .database import (
    BootstrapStateRow,
    ButlerMemoryCursorRow,
    ButlerMemoryItemRow,
    RuntimeProcessRow,
)
from .deployment_protocol import SHARED_MEMORY_BOOTSTRAP_KEY
from .domain import utc_now
from .runtime_coordination import CoordinationConflict
from .runtime_ipc import RuntimeIpcError
from .shared_butler_memory import MemoryWrite, _data

_BATCH = 500
_COLUMNS = tuple(column.name for column in ButlerMemoryItemRow.__table__.columns)


class CompatibilityMemoryImport:
    def __init__(self, host):
        self.host, self.coordinator = host, host.coordinator

    async def _begin(self):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            marker = await session.get(BootstrapStateRow, SHARED_MEMORY_BOOTSTRAP_KEY)
            if marker is not None and marker.status == "complete":
                return False
            resident = await session.scalar(
                select(func.count())
                .select_from(RuntimeProcessRow)
                .where(
                    RuntimeProcessRow.role == "execution",
                    RuntimeProcessRow.state != "retired",
                )
            )
            if resident:
                raise CoordinationConflict(
                    "Finish the initial coordinated shutdown before importing Butler memory; execution workers remain resident"
                )
            if marker is None:
                session.add(
                    BootstrapStateRow(
                        key=SHARED_MEMORY_BOOTSTRAP_KEY, status="importing"
                    )
                )
            else:
                marker.status, marker.completed_at = "importing", None
            return True

    @staticmethod
    def _memory(row):
        value = {name: row[name] for name in _COLUMNS}
        MemoryWrite.model_validate(
            {
                name: value[name]
                for name in (
                    "source_project_id",
                    "scope",
                    "content",
                    "source_kind",
                    "source_label",
                    "source_ref",
                )
            }
        )
        if (
            not isinstance(value["id"], str)
            or not 1 <= len(value["id"]) <= 200
            or type(value["revision"]) is not int
            or value["revision"] < 1
            or value["scope_key"]
            != (
                value["source_project_id"]
                if value["scope"] == "project_lesson"
                else "global"
            )
            or value["content_hash"]
            != hashlib.sha256(value["content"].casefold().encode()).hexdigest()
        ):
            raise ValueError("Unsupported compatibility memory record")
        for field in ("created_at", "updated_at"):
            if not isinstance(value[field], str) or len(value[field]) > 50:
                raise ValueError("Unsupported compatibility memory timestamp")
            datetime.fromisoformat(value[field])
        return value

    async def _copy_memories(self, batch):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            existing = {
                row.id: row
                for row in await session.scalars(
                    select(ButlerMemoryItemRow).where(
                        ButlerMemoryItemRow.id.in_([item["id"] for item in batch])
                    )
                )
            }
            for value in batch:
                if value["id"] in existing:
                    if _data(existing[value["id"]]) != value:
                        raise ValueError(
                            "Compatibility memory differs from its imported checkpoint"
                        )
                else:
                    session.add(ButlerMemoryItemRow(**value))

    async def _copy_cursors(self, batch):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            for value in batch:
                project, sequence = value["project_id"], value["last_event_sequence"]
                if (
                    not isinstance(project, str)
                    or not 1 <= len(project) <= 200
                    or type(sequence) is not int
                    or not 0 <= sequence < 2**63
                ):
                    raise ValueError("Unsupported compatibility memory cursor")
                row = await session.get(ButlerMemoryCursorRow, project)
                if row is None:
                    session.add(
                        ButlerMemoryCursorRow(
                            project_id=project, last_event_sequence=sequence
                        )
                    )
                elif row.last_event_sequence != sequence:
                    raise ValueError(
                        "Compatibility cursor differs from its imported checkpoint"
                    )

    async def _mark(self, status, *, items=0, cursors=0):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(BootstrapStateRow, SHARED_MEMORY_BOOTSTRAP_KEY)
            if row is None:
                raise CoordinationConflict("Shared memory import state changed")
            if row.status == "complete":
                return
            row.status = status
            if status == "complete":
                row.completed_at = utc_now()
                await self.coordinator._receipt(
                    session,
                    "host.memory_imported",
                    {
                        "items": items,
                        "cursors": cursors,
                        "source_preserved": True,
                    },
                )

    async def run(self):
        # Serialize with every detached spawn/close in this host. The durable
        # importing marker also holds admissions from another coordinator pool.
        async with self.host._launch:
            if self.host.closed or self.host.lock.descriptor is None:
                raise RuntimeError(
                    "Compatibility memory import requires exclusive host ownership"
                )
            if not await self._begin():
                return {"state": "already_imported"}
            items, cursors = 0, 0
            try:
                path = self.host.data_root / "butler-memory.sqlite3"
                if path.exists() or path.is_symlink():
                    details = path.lstat()
                    if (
                        not stat.S_ISREG(details.st_mode)
                        or details.st_uid != os.getuid()
                        or details.st_mode & 0o077
                    ):
                        raise ValueError(
                            "Compatibility memory must remain a private owned regular file"
                        )
                    # A read-only snapshot includes committed WAL rows and keeps
                    # the source untouched. Copies use bounded SQL batches; exact
                    # matching checkpoints make interrupted imports resumable.
                    async with aiosqlite.connect(
                        path.as_uri() + "?mode=ro", uri=True, timeout=15
                    ) as source:
                        source.row_factory = sqlite3.Row
                        await source.execute("PRAGMA query_only=ON")
                        await source.execute("BEGIN")
                        tables = {
                            row[0]
                            for row in await (
                                await source.execute(
                                    "SELECT name FROM sqlite_master WHERE type='table'"
                                )
                            ).fetchall()
                        }
                        if "butler_memories" not in tables:
                            raise ValueError("Compatibility memory table is missing")
                        reader = await source.execute(
                            "SELECT * FROM butler_memories ORDER BY id"
                        )
                        while batch := await reader.fetchmany(_BATCH):
                            values = [self._memory(row) for row in batch]
                            await self._copy_memories(values)
                            items += len(values)
                        if "butler_memory_event_cursors" in tables:
                            reader = await source.execute(
                                "SELECT project_id, last_event_sequence FROM butler_memory_event_cursors ORDER BY project_id"
                            )
                            while batch := await reader.fetchmany(_BATCH):
                                await self._copy_cursors(batch)
                                cursors += len(batch)
                        await source.rollback()
                await self._mark("complete", items=items, cursors=cursors)
                return {
                    "state": "imported" if items or cursors else "empty",
                    "items": items,
                    "cursors": cursors,
                }
            except BaseException as error:
                await finish_cleanup(self._mark("blocked"))
                if isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                ):
                    raise
                # Database/validation exceptions can contain candidate memories.
                raise RuntimeIpcError(
                    "Compatibility Butler memory import is blocked. The original file is preserved; repair or restore the source and restart the supervisor before admitting runtimes."
                ) from None
