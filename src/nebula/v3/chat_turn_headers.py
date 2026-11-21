"""Small, revision-keyed status views of chat turns for polled projections.

A chat turn row carries its whole tool history, request snapshot and streamed
text, so a busy conversation's turns are megabytes of JSON. The views that
browsers poll every few seconds (session state, catch-up, pending-turn and
activity) only need a turn's identity and status fields. Parsing every payload
on every poll cost tens of milliseconds per request on real conversations.

A header is a pure function of one row version, so it is memoized by
``(id, revision, created_at)``. Every storage write bumps ``revision``, which
makes a changed turn miss and be re-read; unchanged turns cost one indexed
lookup. The version query reads only the ``(kind, chat_session_id, created_at,
id)`` index and the row's leading ``revision`` column, never the JSON payload,
so SQLite does not walk a large turn's overflow pages. The cache is per engine
so separate databases (and tests) never share entries, and it is bounded.
"""

from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .database import EntityRow
from .domain import ChatSession, ChatTurn

# Enough for every turn of every conversation an operator keeps open, many
# times over; a header is a few hundred bytes.
MAX_CACHED_HEADERS = 20_000
_LOOKUP_CHUNK = 500


@dataclass(frozen=True, slots=True)
class ChatTurnHeader:
    """The status fields of one chat turn row version."""

    id: str
    revision: int
    created_at: datetime
    updated_at: datetime
    session_id: str
    engagement_id: str
    status: str
    backend: str
    harness_turn_id: str | None
    approval_id: str | None
    final_message_id: str | None
    error: str | None
    recovery_required: bool
    automatic_retry_pending: bool
    final_answer_recovery_attempts: int | None


class _HeaderCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # turn id -> (row version, header); the version is the cache key.
        self.entries: OrderedDict[str, tuple[tuple[str, int, Any], ChatTurnHeader]] = (
            OrderedDict()
        )

    def get(self, version: tuple[str, int, Any]) -> ChatTurnHeader | None:
        with self.lock:
            cached = self.entries.get(version[0])
            if cached is None or cached[0] != version:
                return None
            self.entries.move_to_end(version[0])
            return cached[1]

    def put(self, version: tuple[str, int, Any], header: ChatTurnHeader) -> None:
        with self.lock:
            self.entries[version[0]] = (version, header)
            self.entries.move_to_end(version[0])
            while len(self.entries) > MAX_CACHED_HEADERS:
                self.entries.popitem(last=False)


_caches: weakref.WeakKeyDictionary[Engine, _HeaderCache] = weakref.WeakKeyDictionary()
_caches_lock = threading.Lock()


def _cache_for(database: AsyncSession) -> _HeaderCache:
    bind: Any = database.get_bind()
    engine = getattr(bind, "engine", bind)
    with _caches_lock:
        cache = _caches.get(engine)
        if cache is None:
            cache = _HeaderCache()
            _caches[engine] = cache
        return cache


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _header(payload: dict[str, Any], revision: int) -> ChatTurnHeader:
    snapshot = payload.get("request_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    recovery = snapshot.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    final_answer = snapshot.get("final_answer_recovery")
    attempts = final_answer.get("attempts") if isinstance(final_answer, dict) else None
    return ChatTurnHeader(
        id=str(payload["id"]),
        revision=revision,
        created_at=_aware(payload["created_at"]),
        updated_at=_aware(payload.get("updated_at") or payload["created_at"]),
        session_id=str(payload.get("session_id") or ""),
        engagement_id=str(payload.get("engagement_id") or ""),
        status=str(payload.get("status") or "routing"),
        backend=str(payload.get("backend") or "provider"),
        harness_turn_id=_optional_text(payload.get("harness_turn_id")),
        approval_id=_optional_text(payload.get("approval_id")),
        final_message_id=_optional_text(payload.get("final_message_id")),
        error=_optional_text(payload.get("error")),
        recovery_required=bool(recovery.get("required")),
        automatic_retry_pending=bool(recovery.get("automatic_retry_pending")),
        final_answer_recovery_attempts=(
            attempts
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else None
        ),
    )


async def _resolve(
    database: AsyncSession, versions: Sequence[tuple[str, int, Any]]
) -> list[ChatTurnHeader]:
    """Return headers for row versions in order, reading only changed payloads."""

    cache = _cache_for(database)
    resolved: dict[str, ChatTurnHeader] = {}
    missing: dict[str, tuple[str, int, Any]] = {}
    for version in versions:
        header = cache.get(version)
        if header is None:
            missing[version[0]] = version
        else:
            resolved[version[0]] = header
    identifiers = list(missing)
    for start in range(0, len(identifiers), _LOOKUP_CHUNK):
        chunk = identifiers[start : start + _LOOKUP_CHUNK]
        for turn_id, revision, payload in await database.execute(
            select(EntityRow.id, EntityRow.revision, EntityRow.payload).where(
                EntityRow.id.in_(chunk), EntityRow.kind == ChatTurn.entity_kind
            )
        ):
            header = _header(payload, revision)
            # Only a row still at the listed version may be memoized; a write
            # that landed between the two reads is simply re-read next time.
            if revision == missing[turn_id][1]:
                cache.put(missing[turn_id], header)
            resolved[turn_id] = header
    return [resolved[version[0]] for version in versions if version[0] in resolved]


async def session_turn_headers(
    database: AsyncSession, session_id: str, *, newest_first: bool = True
) -> list[ChatTurnHeader]:
    """Every turn of one conversation, ordered by ``(created_at, id)``."""

    order = (
        (EntityRow.created_at.desc(), EntityRow.id.desc())
        if newest_first
        else (EntityRow.created_at, EntityRow.id)
    )
    versions = [
        (row.id, row.revision, row.created_at)
        for row in (
            await database.execute(
                select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
                .where(
                    EntityRow.kind == ChatTurn.entity_kind,
                    EntityRow.chat_session_id == session_id,
                )
                .order_by(*order)
            )
        )
    ]
    return await _resolve(database, versions)


async def sessions_turn_headers(
    database: AsyncSession, session_ids: Iterable[str]
) -> list[ChatTurnHeader]:
    """Every turn of the given conversations, oldest first per conversation."""

    identifiers = sorted(set(session_ids))
    versions: list[tuple[str, int, Any]] = []
    for start in range(0, len(identifiers), _LOOKUP_CHUNK):
        chunk = identifiers[start : start + _LOOKUP_CHUNK]
        versions.extend(
            (row.id, row.revision, row.created_at)
            for row in (
                await database.execute(
                    select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
                    .where(
                        EntityRow.kind == ChatTurn.entity_kind,
                        EntityRow.chat_session_id.in_(chunk),
                    )
                    .order_by(EntityRow.created_at, EntityRow.id)
                )
            )
        )
    return await _resolve(database, versions)


async def latest_turn_header(
    database: AsyncSession, session_id: str
) -> ChatTurnHeader | None:
    """The newest turn of one conversation, by ``(created_at, id)``."""

    row = (
        await database.execute(
            select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
            .where(
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.chat_session_id == session_id,
            )
            .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    headers = await _resolve(database, [(row.id, row.revision, row.created_at)])
    return headers[0] if headers else None


async def latest_turn_headers(
    database: AsyncSession, session_ids: Iterable[str]
) -> dict[str, ChatTurnHeader]:
    """The newest turn of each given conversation, by ``(created_at, id)``.

    Each conversation's newest turn is one backward step on the conversation
    index, however many turns it has; one query reads a chunk of them.
    """

    identifiers = sorted(set(session_ids))
    conversation = aliased(EntityRow)
    turn = aliased(EntityRow)
    versions: list[tuple[str, int, Any]] = []
    for start in range(0, len(identifiers), _LOOKUP_CHUNK):
        chunk = identifiers[start : start + _LOOKUP_CHUNK]
        newest = (
            select(turn.id)
            .where(
                turn.kind == ChatTurn.entity_kind,
                turn.chat_session_id == conversation.id,
            )
            .order_by(turn.created_at.desc(), turn.id.desc())
            .limit(1)
            .correlate(conversation)
            .scalar_subquery()
        )
        newest_ids = select(newest).where(
            conversation.kind == ChatSession.entity_kind,
            conversation.id.in_(chunk),
        )
        versions.extend(
            (row.id, row.revision, row.created_at)
            for row in await database.execute(
                select(EntityRow.id, EntityRow.revision, EntityRow.created_at).where(
                    EntityRow.kind == ChatTurn.entity_kind,
                    EntityRow.id.in_(newest_ids),
                )
            )
        )
    return {header.session_id: header for header in await _resolve(database, versions)}


async def pending_turn_headers_of_sessions(
    database: AsyncSession, session_ids: Iterable[str], *, statuses: Sequence[str]
) -> list[ChatTurnHeader]:
    """Pending turns of the given conversations, oldest first.

    Read from the covering turn status index alone, as two seeks (the listed
    statuses, then rows without the projection) sorted here: Core holds few
    pending turns, while the listed conversations may hold thousands of
    finished ones. One ``OR`` of the two, or a join to the conversations,
    lets the planner scan the table instead.
    """

    wanted = set(session_ids)
    if not wanted:
        return []
    lookup = select(
        EntityRow.id,
        EntityRow.revision,
        EntityRow.created_at,
        EntityRow.chat_session_id,
        EntityRow.chat_turn_status,
        EntityRow.chat_turn_recovery_visible,
    ).where(EntityRow.kind == ChatTurn.entity_kind)
    rows = [
        *await database.execute(lookup.where(EntityRow.chat_turn_status.in_(statuses))),
        *await database.execute(lookup.where(EntityRow.chat_turn_status.is_(None))),
    ]
    versions = sorted(
        (
            (row.id, row.revision, row.created_at)
            for row in rows
            if row.chat_session_id in wanted
            and not (
                row.chat_turn_status == "interrupted"
                and row.chat_turn_recovery_visible is False
            )
        ),
        key=lambda version: (_aware(version[2]), version[0]),
    )
    return [
        header
        for header in await _resolve(database, versions)
        if header.status in statuses
    ]


async def engagement_turn_headers(
    database: AsyncSession, engagement_id: str
) -> list[ChatTurnHeader]:
    """Every turn of one project's conversations, oldest first per conversation.

    Conversations are found through the project index; their turns through the
    conversation index, so neither read touches a JSON payload.
    """

    session_ids = (
        await database.scalars(
            select(EntityRow.id).where(
                EntityRow.kind == ChatSession.entity_kind,
                EntityRow.engagement_id == engagement_id,
            )
        )
    ).all()
    return [
        item
        for item in (await sessions_turn_headers(database, session_ids))
        if item.engagement_id == engagement_id
    ]


async def pending_engagement_turn_headers(
    database: AsyncSession, engagement_id: str, *, statuses: Sequence[str]
) -> list[ChatTurnHeader]:
    """Resolve only indexed pending candidates, including legacy unknowns.

    Cold activity polls must not hydrate every historical turn while execution
    fencing prevents the worker heartbeat from renewing its lease.
    """
    return await _pending_turn_headers(
        database, EntityRow.engagement_id == engagement_id, statuses=statuses
    )


async def pending_turn_headers_in_projects(
    database: AsyncSession, engagement_ids: Any, *, statuses: Sequence[str]
) -> list[ChatTurnHeader]:
    """Pending turns of every project selected by ``engagement_ids``.

    ``engagement_ids`` is a SQL subquery, so a parent with thousands of linked
    projects never binds one parameter per project.
    """
    return await _pending_turn_headers(
        database, EntityRow.engagement_id.in_(engagement_ids), statuses=statuses
    )


async def _pending_turn_headers(
    database: AsyncSession, projects: Any, *, statuses: Sequence[str]
) -> list[ChatTurnHeader]:
    conversation = aliased(EntityRow)
    versions = [
        (row.id, row.revision, row.created_at)
        for row in await database.execute(
            select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
            .join(conversation, conversation.id == EntityRow.chat_session_id)
            .where(
                EntityRow.kind == ChatTurn.entity_kind,
                projects,
                conversation.kind == ChatSession.entity_kind,
                conversation.engagement_id == EntityRow.engagement_id,
                or_(
                    EntityRow.chat_turn_status.is_(None),
                    and_(
                        EntityRow.chat_turn_status.in_(statuses),
                        or_(
                            EntityRow.chat_turn_status != "interrupted",
                            EntityRow.chat_turn_recovery_visible.is_not(False),
                        ),
                    ),
                ),
            )
            .order_by(EntityRow.created_at, EntityRow.id)
        )
    ]
    return await _resolve(database, versions)


async def turn_header(
    database: AsyncSession, session_id: str, turn_id: str
) -> ChatTurnHeader | None:
    """One turn of a known conversation, through the same covering index."""

    row = (
        await database.execute(
            select(EntityRow.id, EntityRow.revision, EntityRow.created_at).where(
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.chat_session_id == session_id,
                EntityRow.id == turn_id,
            )
        )
    ).first()
    if row is None:
        return None
    headers = await _resolve(database, [(row.id, row.revision, row.created_at)])
    return headers[0] if headers else None


async def session_turn_page(
    database: AsyncSession,
    session_id: str,
    *,
    before: int | None,
    limit: int,
) -> tuple[list[ChatTurnHeader], int, int]:
    """One page of a conversation's turns, oldest first, by 1-based index.

    Returns the headers of the turns numbered below ``before`` (every turn when
    it is None), at most ``limit`` of the newest of them, the index of the
    first one returned and the conversation's turn count. Only the index
    columns of earlier turns are read; payloads are parsed for the page alone.
    """

    versions = [
        (row.id, row.revision, row.created_at)
        for row in (
            await database.execute(
                select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
                .where(
                    EntityRow.kind == ChatTurn.entity_kind,
                    EntityRow.chat_session_id == session_id,
                )
                .order_by(EntityRow.created_at, EntityRow.id)
            )
        )
    ]
    total = len(versions)
    end = total if before is None else max(0, min(total, before - 1))
    start = max(0, end - limit)
    return await _resolve(database, versions[start:end]), start + 1, total


async def pending_session_turn_headers(
    database: AsyncSession,
    session_id: str,
    engagement_id: str,
    *,
    statuses: Sequence[str],
) -> list[ChatTurnHeader]:
    """One conversation's pending turns, as the project-wide scan selects them."""

    versions = [
        (row.id, row.revision, row.created_at)
        for row in await database.execute(
            select(EntityRow.id, EntityRow.revision, EntityRow.created_at)
            .where(
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.chat_session_id == session_id,
                EntityRow.engagement_id == engagement_id,
                or_(
                    EntityRow.chat_turn_status.is_(None),
                    and_(
                        EntityRow.chat_turn_status.in_(statuses),
                        or_(
                            EntityRow.chat_turn_status != "interrupted",
                            EntityRow.chat_turn_recovery_visible.is_not(False),
                        ),
                    ),
                ),
            )
            .order_by(EntityRow.created_at, EntityRow.id)
        )
    ]
    return [
        header
        for header in await _resolve(database, versions)
        if header.status in statuses
    ]
