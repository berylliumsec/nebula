"""Read-only project views that remain available without an execution owner."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import aliased

from .stored_entities import decode_stored
from .butler_board import butler_board_router
from .butler_case_stage import with_case_status
from .file_attribution import file_attribution_router
from .chat_turn_headers import pending_engagement_turn_headers
from .conditional_reads import conditional, content_etag
from .linked_project_chats import (
    UNFINISHED_TURN_STATUSES,
    linked_project_chats_router,
    session_state,
    visible_pending,
)
from .database import EntityRow
from .domain import (
    ChatBookmark,
    ChatGoalStatus,
    ChatMessage,
    ChatSession,
    Engagement,
    NotebookRun,
    OperatorExecution,
    OperatorExecutionStatus,
    WorkItem,
    WorkUpdate,
    message_is_replaced,
)
from .project_execution_status import project_execution_waiting
from .project_reads import (
    ChatGoalRead,
    active_control_assistant_session,
    active_notebook_runs,
    filter_executions,
    presented_chat_goal,
    project_executions,
    stored_chat_goal,
)
from .storage import NebulaStore, NotFoundError

_UNFINISHED_TURN_STATUSES = UNFINISHED_TURN_STATUSES


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
        return await with_case_status(
            store,
            [
                item.model_copy(
                    update={"metadata": {**item.metadata, "butler_initiated": True}}
                )
                if item.id in legacy_ids
                else item
                for item in sessions
            ],
        )

    @app.get(
        "/api/v1/chat-sessions/{session_id}",
        response_model=ChatSession,
        dependencies=dependencies,
    )
    async def get_project_chat_session(
        session_id: str, response: Response
    ) -> ChatSession:
        response.headers["Cache-Control"] = "no-store"
        return (await with_case_status(store, [await store.get(ChatSession, session_id)]))[0]

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
        pending = {}
        for header in visible_pending(headers):
            if header.session_id in pending:
                raise HTTPException(409, "chat session has multiple active turns")
            pending[header.session_id] = header
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
            activity.append(
                {
                    "session_id": session.id,
                    "state": await session_state(store, session, turn),
                    "turn_id": turn.id if turn else None,
                }
            )
        if unchanged := conditional(
            request, response, content_etag("activity", activity)
        ):
            return unchanged
        return activity

    app.include_router(
        linked_project_chats_router(store), prefix="/api/v1", dependencies=dependencies
    )
    app.include_router(
        butler_board_router(store), prefix="/api/v1", dependencies=dependencies
    )
    # SQL-only: an inline patch the activity record cut short is reported
    # without line counts here, since this worker never opens artifact files.
    app.include_router(
        file_attribution_router(store), prefix="/api/v1", dependencies=dependencies
    )

    # Deployment inventory reads (the deploy gate, and Butler's pause and
    # resume checks) cover every project, including those without a worker.
    @app.get(
        "/api/v1/chat/control-assistant/session",
        response_model=ChatSession | None,
        dependencies=dependencies,
    )
    async def get_project_control_assistant_session(
        engagement_id: str, response: Response
    ) -> ChatSession | None:
        response.headers["Cache-Control"] = "no-store"
        return await active_control_assistant_session(store, engagement_id)

    @app.get(
        "/api/v1/chat/sessions/{session_id}/goal",
        response_model=ChatGoalRead,
        dependencies=dependencies,
    )
    async def get_project_chat_goal(
        session_id: str, response: Response
    ) -> ChatGoalRead:
        response.headers["Cache-Control"] = "no-store"
        goal = await stored_chat_goal(store, session_id)
        waiting = None
        if goal.status == ChatGoalStatus.RUNNING:
            # Served without the project's worker: a stored "running" goal only
            # progresses while a ready worker owns the project.
            async with store.database.session() as session:
                waiting = await project_execution_waiting(session, goal.engagement_id)
        return ChatGoalRead.model_validate(
            {**presented_chat_goal(goal).model_dump(), "execution": waiting}
        )

    @app.get(
        "/api/v1/engagements/{engagement_id}/notebooks/runs/active",
        response_model=list[NotebookRun],
        dependencies=dependencies,
    )
    async def list_project_active_notebook_runs(
        engagement_id: str, response: Response
    ) -> list[NotebookRun]:
        response.headers["Cache-Control"] = "no-store"
        return await active_notebook_runs(store, engagement_id)

    @app.get(
        "/api/v1/engagements/{engagement_id}/executions",
        response_model=list[OperatorExecution],
        dependencies=dependencies,
    )
    async def list_project_executions(
        engagement_id: str,
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        execution_status: OperatorExecutionStatus | None = Query(
            default=None, alias="status"
        ),
        language: str | None = Query(default=None, max_length=32),
        operator_id: str | None = Query(default=None, max_length=200),
        date_from: datetime | None = Query(default=None),
        date_to: datetime | None = Query(default=None),
        query: str | None = Query(default=None, max_length=500),
    ) -> Any:
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in (date_from, date_to)
        ):
            return JSONResponse(
                {
                    "detail": "execution date filters must include a timezone",
                    "code": "invalid_date_filter",
                },
                status_code=422,
            )
        response.headers["Cache-Control"] = "no-store"
        await store.get(Engagement, engagement_id)
        return filter_executions(
            await project_executions(store, engagement_id),
            offset=offset,
            limit=limit,
            status=execution_status,
            language=language,
            operator_id=operator_id,
            date_from=date_from,
            date_to=date_to,
            query=query,
        )

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
                            visible_chat.payload["metadata"][
                                "control_assistant"
                            ].as_boolean(),
                            False,
                        ).is_(False),
                        func.coalesce(
                            visible_chat.payload["metadata"][
                                "temporary_assistant"
                            ].as_boolean(),
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
                decode_stored(ChatMessage, row.payload)
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
