"""Butler recovery-case status shown beside the chats it concerns.

A case chat (the transcript Butler opens to investigate) and the goal chat a
case is about both show the case's stage at a glance. Nothing new is stored:
each read joins the current ``ButlerRecoveryCase.status`` to the chats it
lists, in one bounded query per page.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EntityRow
from .domain import ButlerRecoveryCase, ChatSession, utc_now
from .stored_entities import decode_stored
from .storage import NebulaStore

CASE_STATUS_KEY = "butler_case_status"
"""Read-time session metadata: the status of the case a case chat investigates."""

OPEN_CASE_STATUSES = frozenset(
    {"observed", "investigating", "verifying", "needs_attention", "uncertain"}
)
# A goal card keeps showing a resolved case for a day, so the operator sees
# that Butler repaired it overnight.
RESOLVED_CASE_WINDOW = timedelta(hours=24)
# Goal cards consider at most this many recent cases per page of cards.
_MAX_BOARD_CASES = 2000


async def with_case_status(
    store: NebulaStore, sessions: Sequence[ChatSession]
) -> list[ChatSession]:
    """Each Butler case chat with its case's current status in its metadata."""

    case_ids = {
        str(value)
        for session in sessions
        if isinstance(value := session.metadata.get("butler_recovery_case_id"), str)
    }
    statuses = await store.butler_case_statuses(case_ids)
    if not statuses:
        return list(sessions)
    presented: list[ChatSession] = []
    for session in sessions:
        status = statuses.get(str(session.metadata.get("butler_recovery_case_id")))
        presented.append(
            session.model_copy(
                update={"metadata": {**session.metadata, CASE_STATUS_KEY: status}}
            )
            if status
            else session
        )
    return presented


def _shown_on_goal(case: ButlerRecoveryCase, now: datetime) -> bool:
    if case.status in OPEN_CASE_STATUSES:
        return True
    return case.status == "resolved" and now - case.updated_at <= RESOLVED_CASE_WINDOW


async def cases_for_goal_chats(
    database: AsyncSession,
    session_ids: Iterable[str],
    engagement_ids: Iterable[str],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """The newest Butler case about each listed chat, when it is worth showing.

    A case is about the chat where its signal happened (``source_session_id``).
    Only the newest non-cancelled case counts: an open case, or one resolved
    within ``RESOLVED_CASE_WINDOW``. Older cases are superseded by it.
    """

    ids = set(session_ids)
    projects = set(engagement_ids)
    if not ids or not projects:
        return {}
    source = EntityRow.payload["source_session_id"].as_string()
    rows = (
        await database.scalars(
            select(EntityRow.payload)
            .where(
                EntityRow.kind == ButlerRecoveryCase.entity_kind,
                EntityRow.engagement_id.in_(projects),
                source.in_(ids),
                EntityRow.payload["status"].as_string() != "cancelled",
            )
            .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
            .limit(_MAX_BOARD_CASES)
        )
    ).all()
    moment = now or utc_now()
    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for payload in rows:
        case = decode_stored(ButlerRecoveryCase, payload)
        if not case.source_session_id or case.source_session_id in seen:
            continue
        seen.add(case.source_session_id)
        if _shown_on_goal(case, moment):
            found[case.source_session_id] = {
                "id": case.id,
                "status": case.status,
                "session_id": case.investigation_session_id,
                "updated_at": case.updated_at,
            }
    return found
