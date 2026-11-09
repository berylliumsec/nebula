"""Bounded, shared Work invalidation from committed SQL, including old workers.

The cursor identifies current saved observations. It is not an event-history
cursor: subscribers reread current state on ready and after coalesced changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import time
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select

from .database import EntityRow
from .domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    WorkItem,
    WorkUpdate,
)
from .storage import NebulaStore

POLL_INTERVAL = 2.0
SCAN_TIMEOUT = 5.0
FETCH_ROWS = 128
MAX_SUBSCRIBERS = 64
HEARTBEAT_INTERVAL = 25.0
ACTIVE_TURNS = (
    ChatTurnStatus.QUEUED,
    ChatTurnStatus.ROUTING,
    ChatTurnStatus.WAITING_APPROVAL,
    ChatTurnStatus.WAITING_CALLBACK,
    ChatTurnStatus.FINALIZING,
    ChatTurnStatus.INTERRUPTED,
)


class WorkObservationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkSnapshot:
    work: str
    projects: str

    @property
    def cursor(self) -> str:
        return f"v1:{self.projects}:{self.work}"

    def change_from(self, previous: WorkSnapshot) -> Literal["projects", "work"] | None:
        if self.projects != previous.projects:
            return "projects"
        return "work" if self.work != previous.work else None


class WorkSubscription:
    def __init__(self, observer: SqlWorkObserver, project_id: str | None):
        self.observer, self.project_id = observer, project_id
        self.baseline: WorkSnapshot | None = None
        self.closed = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.observer._subscribers.discard(self)


class SqlWorkObserver:
    """One scan at a time per API, only while a bounded viewer set is present."""

    def __init__(self, store: NebulaStore):
        self.store = store
        self.poll_interval = POLL_INTERVAL
        self.scan_timeout = SCAN_TIMEOUT
        self.heartbeat_interval = HEARTBEAT_INTERVAL
        self.max_subscribers = MAX_SUBSCRIBERS
        self._subscribers: set[WorkSubscription] = set()
        self._lock = asyncio.Lock()
        self._cache: dict[str | None, WorkSnapshot] = {}
        self._next_scan = 0.0
        self._failed = False
        self.closed = False

    async def subscribe(self, project_id: str | None = None) -> WorkSubscription:
        if self.closed:
            raise HTTPException(503, "Work observations are stopping")
        if len(self._subscribers) >= self.max_subscribers:
            raise HTTPException(
                429,
                "Work observation streams are busy; retry shortly",
                headers={"Retry-After": "2"},
            )
        subscription = WorkSubscription(self, project_id)
        self._subscribers.add(subscription)
        try:
            # Baseline precedes ready; a commit during the client's reread will
            # remain different on the next completed scan.
            subscription.baseline = await self.current(project_id)
        except BaseException:
            await subscription.close()
            raise
        return subscription

    async def close(self) -> None:
        self.closed = True
        for subscription in tuple(self._subscribers):
            await subscription.close()

    async def current(self, project_id: str | None) -> WorkSnapshot:
        while not self.closed:
            delay = self._next_scan - time.monotonic()
            if delay > 0:
                if self._failed:
                    raise WorkObservationUnavailable(
                        "Saved Work observations are temporarily unavailable"
                    )
                if project_id in self._cache:
                    return self._cache[project_id]
                await asyncio.sleep(delay)
                continue
            async with self._lock:
                if time.monotonic() < self._next_scan:
                    continue
                scopes = {
                    item.project_id for item in self._subscribers if not item.closed
                }
                if project_id not in scopes:
                    raise WorkObservationUnavailable(
                        "Work observation subscription is closed"
                    )
                try:
                    async with asyncio.timeout(self.scan_timeout):
                        complete = await self._scan(scopes)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._failed = True
                    raise WorkObservationUnavailable(
                        "Saved Work observations are temporarily unavailable"
                    ) from error
                else:
                    # A timeout or partial scan never advances either digest.
                    self._cache = complete
                    self._failed = False
                    return complete[project_id]
                finally:
                    # Shared cooldown also covers failures and cancellation.
                    self._next_scan = time.monotonic() + self.poll_interval
        raise WorkObservationUnavailable("Work observations are stopping")

    async def _scan(self, scopes: set[str | None]) -> dict[str | None, WorkSnapshot]:
        work = {scope: sha256(b"work-observation/v1") for scope in scopes}
        projects = {scope: sha256(b"project-observation/v1") for scope in scopes}

        def add(kind: str, owner: str, value: Any) -> None:
            content = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode()
            target = projects if kind == "projects" else work
            for scope in (None, owner):
                if scope in target:
                    target[scope].update(len(content).to_bytes(8, "big"))
                    target[scope].update(content)

        async with self.store.database.session(bulk=True) as session:
            for model in (Engagement, WorkItem, WorkUpdate):
                statement = select(
                    EntityRow.id, EntityRow.engagement_id, EntityRow.revision
                ).where(EntityRow.kind == model.entity_kind)
                if None not in scopes:
                    owner = (
                        EntityRow.id if model is Engagement else EntityRow.engagement_id
                    )
                    statement = statement.where(owner.in_(scopes))
                statement = statement.order_by(
                    EntityRow.engagement_id, EntityRow.created_at, EntityRow.id
                )
                result = await session.stream(
                    statement.execution_options(yield_per=FETCH_ROWS)
                )
                try:
                    async for row in result:
                        owner = row.id if model is Engagement else row.engagement_id
                        add(
                            "projects" if model is Engagement else "work",
                            owner,
                            [model.entity_kind, *row],
                        )
                finally:
                    await result.close()

            # Session creation/deletion/title and roster flags affect Work, but
            # arbitrary chat metadata/revisions and streamed tokens do not.
            metadata = EntityRow.payload["metadata"]
            subagent_type = (
                func.json_type(EntityRow.payload, "$.metadata.subagent_id")
                if session.bind and session.bind.dialect.name == "sqlite"
                else func.json_typeof(metadata["subagent_id"])
            )
            session_statement = select(
                EntityRow.id,
                EntityRow.engagement_id,
                func.substr(EntityRow.payload["title"].as_string(), 1, 300),
                func.substr(cast(metadata["temporary_assistant"], String), 1, 512),
                func.substr(cast(metadata["side_chat"], String), 1, 512),
                subagent_type,
            ).where(EntityRow.kind == ChatSession.entity_kind)
            if None not in scopes:
                session_statement = session_statement.where(
                    EntityRow.engagement_id.in_(scopes)
                )
            session_statement = session_statement.order_by(
                EntityRow.engagement_id, EntityRow.created_at, EntityRow.id
            )
            result = await session.stream(
                session_statement.execution_options(yield_per=FETCH_ROWS)
            )
            try:
                async for row in result:
                    add("work", row.engagement_id, [ChatSession.entity_kind, *row])
            finally:
                await result.close()

            active_statement = select(
                EntityRow.id,
                EntityRow.engagement_id,
                EntityRow.chat_session_id,
                EntityRow.chat_turn_status,
                EntityRow.chat_turn_recovery_visible,
                EntityRow.chat_turn_core_resume_pending,
            ).where(
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.chat_turn_status.in_(
                    [status.value for status in ACTIVE_TURNS]
                ),
                or_(
                    EntityRow.chat_turn_status != ChatTurnStatus.INTERRUPTED.value,
                    EntityRow.chat_turn_recovery_visible.is_(True),
                ),
            )
            if None not in scopes:
                active_statement = active_statement.where(
                    EntityRow.engagement_id.in_(scopes)
                )
            active_statement = active_statement.order_by(
                EntityRow.chat_turn_status, EntityRow.created_at, EntityRow.id
            )
            result = await session.stream(
                active_statement.execution_options(yield_per=FETCH_ROWS)
            )
            try:
                async for row in result:
                    add("work", row.engagement_id, [ChatTurn.entity_kind, *row])
            finally:
                await result.close()
        return {
            scope: WorkSnapshot(work[scope].hexdigest(), projects[scope].hexdigest())
            for scope in scopes
        }
