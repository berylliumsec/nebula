"""Read-only project views that remain available without an execution owner."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import aliased

from .chat_subagents import core_resumes_interrupted_turn, is_subagent_session
from .chat_turn_headers import pending_engagement_turn_headers
from .conditional_reads import conditional, content_etag
from .database import EntityRow
from .domain import (
    ChatBookmark,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    WorkItem,
    WorkUpdate,
    message_is_replaced,
)
from .storage import NebulaStore, NotFoundError

_UNFINISHED_TURN_STATUSES = (
    ChatTurnStatus.QUEUED.value,
    ChatTurnStatus.ROUTING.value,
    ChatTurnStatus.WAITING_APPROVAL.value,
    ChatTurnStatus.WAITING_CALLBACK.value,
    ChatTurnStatus.FINALIZING.value,
    ChatTurnStatus.INTERRUPTED.value,
)


def mount_global_project_reads(
    app: FastAPI, store: NebulaStore, actor: ContextVar[str]
) -> None:
    """Mount safe SQL reads normally proxied to a project worker.

    These routes expose durable metadata only. Project mutations and transcript
    streaming still require the exact project's execution owner.
    """

    def authenticated() -> str:
        value = actor.get()
        if value != "operator" and not value.startswith("device:"):
            raise HTTPException(403, "An authenticated Core actor is required")
        return value

    dependencies = [Depends(authenticated)]

    @app.get(
        "/api/v1/engagements/{engagement_id}/work",
        response_model=list[WorkItem],
        dependencies=dependencies,
    )
    async def list_project_work(
        engagement_id: str,
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> list[WorkItem]:
        response.headers["Cache-Control"] = "no-store"
        await store.get(Engagement, engagement_id)
        return await store.list_entities(
            WorkItem,
            engagement_id=engagement_id,
            offset=offset,
            limit=limit,
            newest_first=True,
        )

    @app.get(
        "/api/v1/engagements/{engagement_id}/work/{item_id}/updates",
        response_model=list[WorkUpdate],
        dependencies=dependencies,
    )
    async def list_project_work_updates(
        engagement_id: str, item_id: str, response: Response
    ) -> list[WorkUpdate]:
        response.headers["Cache-Control"] = "no-store"
        item = await store.get(WorkItem, item_id)
        if item.engagement_id != engagement_id:
            raise NotFoundError("work item does not belong to this project")
        return await store.find_entities(
            WorkUpdate,
            {"item_id": item_id},
            engagement_id=engagement_id,
            limit=200,
            newest_first=True,
        )

    @app.get("/api/v1/chat-sessions", dependencies=dependencies)
    async def list_project_chat_sessions(
        response: Response,
        engagement_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        recent_first: bool = False,
    ) -> list[ChatSession]:
        response.headers["Cache-Control"] = "no-store"
        sessions = await store.list_entities(
            ChatSession,
            engagement_id=engagement_id,
            offset=offset,
            limit=limit,
            recent_first=recent_first,
        )
        legacy_ids = await store.butler_recovery_session_ids(
            item.id
            for item in sessions
            if item.metadata.get("butler_initiated") is not True
        )
        return [
            item.model_copy(
                update={"metadata": {**item.metadata, "butler_initiated": True}}
            )
            if item.id in legacy_ids
            else item
            for item in sessions
        ]

    @app.get(
        "/api/v1/chat-sessions/{session_id}",
        response_model=ChatSession,
        dependencies=dependencies,
    )
    async def get_project_chat_session(
        session_id: str, response: Response
    ) -> ChatSession:
        response.headers["Cache-Control"] = "no-store"
        return await store.get(ChatSession, session_id)

    @app.get(
        "/api/v1/chat/session-activity", response_model=None, dependencies=dependencies
    )
    async def list_project_chat_activity(
        engagement_id: str, request: Request, response: Response
    ) -> Any:
        response.headers["Cache-Control"] = "no-store"
        await store.get(Engagement, engagement_id)
        async with store.database.session() as database:
            headers = await pending_engagement_turn_headers(
                database, engagement_id, statuses=_UNFINISHED_TURN_STATUSES
            )
        pending = {header.session_id: header for header in headers}
        sessions: list[ChatSession] = []
        offset = 0
        while page := await store.list_entities(
            ChatSession, engagement_id=engagement_id, offset=offset, limit=1000
        ):
            sessions.extend(page)
            offset += len(page)
        activity: list[dict[str, str | None]] = []
        for session in sessions:
            if (
                session.metadata.get("temporary_assistant") is True
                or session.metadata.get("side_chat") is True
            ):
                continue
            turn = pending.get(session.id)
            waiting = bool(
                turn
                and not is_subagent_session(session)
                and (
                    turn.status == ChatTurnStatus.WAITING_APPROVAL.value
                    or turn.status == ChatTurnStatus.INTERRUPTED.value
                    and not core_resumes_interrupted_turn(
                        await store.get(ChatTurn, turn.id)
                    )
                )
            )
            activity.append(
                {
                    "session_id": session.id,
                    "state": (
                        "idle"
                        if turn is None
                        else "waiting"
                        if waiting
                        else "working"
                    ),
                    "turn_id": turn.id if turn else None,
                }
            )
        if unchanged := conditional(
            request, response, content_etag("activity", activity)
        ):
            return unchanged
        return activity

    @app.get("/api/v1/chat/projects/{project_id}/search", dependencies=dependencies)
    async def search_project_chats(
        response: Response,
        project_id: str,
        q: str = Query(default="", max_length=512),
        session_id: str | None = None,
        bookmarked: bool = False,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        await store.get(Engagement, project_id)
        statement = select(EntityRow).where(
            EntityRow.kind == "chat_messages", EntityRow.engagement_id == project_id
        )
        visible_chat = aliased(EntityRow)
        statement = statement.where(
            exists(
                select(visible_chat.id).where(
                    visible_chat.kind == ChatSession.entity_kind,
                    visible_chat.id == EntityRow.chat_session_id,
                    and_(
                        func.coalesce(
                            visible_chat.payload["metadata"]["control_assistant"].as_boolean(),
                            False,
                        ).is_(False),
                        func.coalesce(
                            visible_chat.payload["metadata"]["temporary_assistant"].as_boolean(),
                            False,
                        ).is_(False),
                        func.coalesce(
                            visible_chat.payload["metadata"]["side_chat"].as_boolean(),
                            False,
                        ).is_(False),
                    ),
                )
            )
        )
        if session_id:
            session = await store.get(ChatSession, session_id)
            if session.engagement_id != project_id:
                raise NotFoundError("Conversation is not in this project")
            statement = statement.where(EntityRow.chat_session_id == session_id)
        if q.strip():
            statement = statement.where(
                EntityRow.payload["content"]
                .as_string()
                .icontains(q.strip(), autoescape=True)
            )
        if bookmarked:
            mark = aliased(EntityRow)
            statement = statement.where(
                exists(
                    select(mark.id).where(
                        mark.kind == ChatBookmark.entity_kind,
                        mark.engagement_id == project_id,
                        mark.payload["message_id"].as_string() == EntityRow.id,
                        mark.payload["active"].as_boolean().is_(True),
                    )
                )
            )
        statement = (
            statement.order_by(EntityRow.created_at, EntityRow.id)
            .offset(offset)
            .limit(limit + 1)
        )
        async with store.database.session() as database:
            page = [
                ChatMessage.model_validate(row.payload)
                for row in (await database.scalars(statement))
            ]
        has_more = len(page) > limit
        rows = [message for message in page[:limit] if not message_is_replaced(message)]
        items = []
        for message in rows:
            text = message.content
            start = (
                max(0, text.casefold().find(q.strip().casefold()) - 80)
                if q.strip()
                else 0
            )
            session = await store.get(ChatSession, message.session_id)
            items.append(
                {
                    "message_id": message.id,
                    "session_id": session.id,
                    "title": session.title,
                    "role": message.role,
                    "excerpt": text[start : start + 400],
                    "sequence": message.sequence,
                    "created_at": message.created_at,
                }
            )
        return {"items": items, "next_offset": offset + limit if has_more else None}
