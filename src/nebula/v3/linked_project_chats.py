"""One bounded list of the chats owned by projects linked under a project.

A parent project can have thousands of linked projects, most without chats.
The conversation list reads their chats here in one page instead of one
request per linked project. Both the supervisor's API worker and the
single-process app mount the same route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from .stored_entities import decode_stored
from .chat_subagents import core_resumes_interrupted_turn, is_subagent_session
from .chat_turn_headers import ChatTurnHeader, pending_turn_headers_in_projects
from .conditional_reads import conditional, content_etag
from .database import EntityRow
from .domain import ChatSession, ChatTurn, ChatTurnStatus, Engagement
from .storage import NebulaStore, not_temporary_chat_session

UNFINISHED_TURN_STATUSES = (
    ChatTurnStatus.QUEUED.value,
    ChatTurnStatus.ROUTING.value,
    ChatTurnStatus.WAITING_APPROVAL.value,
    ChatTurnStatus.WAITING_CALLBACK.value,
    ChatTurnStatus.FINALIZING.value,
    ChatTurnStatus.INTERRUPTED.value,
)

_LINKED_STATE_ORDER = {"waiting": 0, "working": 1}


async def session_state(
    store: NebulaStore, session: ChatSession, turn: ChatTurnHeader | None
) -> str:
    """Idle, working, or waiting for the operator, as the activity view shows."""

    if turn is None:
        return "idle"
    waiting = not is_subagent_session(session) and (
        turn.status == ChatTurnStatus.WAITING_APPROVAL.value
        or turn.status == ChatTurnStatus.INTERRUPTED.value
        and not core_resumes_interrupted_turn(await store.get(ChatTurn, turn.id))
    )
    return "waiting" if waiting else "working"


def visible_pending(headers: list[ChatTurnHeader]) -> list[ChatTurnHeader]:
    """Drop interrupted turns that need neither recovery nor a retry."""

    return [
        header
        for header in headers
        if not (
            header.status == ChatTurnStatus.INTERRUPTED.value
            and not (header.recovery_required or header.automatic_retry_pending)
        )
    ]


def _linked_projects(project_id: str) -> Any:
    """The IDs of projects linked directly under ``project_id``, as a subquery."""

    return (
        select(EntityRow.id)
        .where(
            EntityRow.kind == Engagement.entity_kind,
            EntityRow.payload["parent_engagement_id"].as_string() == project_id,
        )
        .scalar_subquery()
    )


def linked_project_chats_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["chat"])

    @router.get("/chat/projects/{project_id}/linked-chats", response_model=None)
    async def list_linked_project_chats(
        project_id: str,
        request: Request,
        response: Response,
        q: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=25, ge=1, le=200),
    ) -> Any:
        """Chats of the projects linked under one project, in one bounded page.

        Chats needing the operator come first, then running chats, then the
        rest by recent activity. The order is applied before paging, so an
        active chat is never left on a later page. Projects without chats add
        nothing. Each chat stays owned by its own project.
        """
        response.headers["Cache-Control"] = "no-store"
        await store.get(Engagement, project_id)
        linked = _linked_projects(project_id)
        project = aliased(EntityRow)
        query = q.strip()

        def visible(statement: Any) -> Any:
            statement = statement.join(
                project,
                and_(
                    project.id == EntityRow.engagement_id,
                    project.kind == Engagement.entity_kind,
                ),
            ).where(
                EntityRow.kind == ChatSession.entity_kind,
                EntityRow.engagement_id.in_(linked),
                not_temporary_chat_session(),
            )
            if query:
                statement = statement.where(
                    or_(
                        EntityRow.payload["title"]
                        .as_string()
                        .icontains(query, autoescape=True),
                        EntityRow.payload["model"]
                        .as_string()
                        .icontains(query, autoescape=True),
                        project.payload["name"]
                        .as_string()
                        .icontains(query, autoescape=True),
                    )
                )
            return statement

        async with store.database.session() as database:
            headers = await pending_turn_headers_in_projects(
                database, linked, statuses=UNFINISHED_TURN_STATUSES
            )
            pending: dict[str, ChatTurnHeader] = {}
            for header in visible_pending(headers):
                pending.setdefault(header.session_id, header)
            active_rows = (
                list(
                    await database.execute(
                        visible(
                            select(EntityRow.payload, project.payload["name"])
                        ).where(EntityRow.id.in_(list(pending)))
                    )
                )
                if pending
                else []
            )
        active: list[tuple[ChatSession, str, str]] = []
        for payload, name in active_rows:
            session = decode_stored(ChatSession, payload)
            state = await session_state(store, session, pending[session.id])
            active.append((session, str(name or ""), state))
        active.sort(key=lambda item: (item[0].updated_at, item[0].id), reverse=True)
        active.sort(key=lambda item: _LINKED_STATE_ORDER[item[2]])

        page = active[offset : offset + limit + 1]
        if len(page) <= limit:
            idle_offset = max(0, offset - len(active))
            idle_statement = visible(select(EntityRow.payload, project.payload["name"]))
            if pending:
                idle_statement = idle_statement.where(
                    EntityRow.id.not_in(list(pending))
                )
            idle_statement = (
                idle_statement.order_by(
                    EntityRow.updated_at.desc(), EntityRow.id.desc()
                )
                .offset(idle_offset)
                .limit(limit + 1 - len(page))
            )
            async with store.database.session(bulk=True) as database:
                page.extend(
                    (decode_stored(ChatSession, payload), str(name or ""), "idle")
                    for payload, name in await database.execute(idle_statement)
                )
        has_more = len(page) > limit
        body = {
            "items": [
                {
                    "session": session.model_dump(mode="json"),
                    "project": {"id": session.engagement_id, "name": name},
                    "state": state,
                    "turn_id": (
                        pending[session.id].id if session.id in pending else None
                    ),
                }
                for session, name, state in page[:limit]
            ],
            "next_offset": offset + limit if has_more else None,
        }
        if unchanged := conditional(
            request, response, content_etag("linked-chats", body)
        ):
            return unchanged
        return body

    return router
