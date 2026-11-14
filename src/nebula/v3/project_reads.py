"""Project reads answered from SQL alone, shared by workers and the API worker.

Execution workers serve these through their project routes. The API worker
serves the same functions for a project without a live execution owner, since
reads never admit a worker. Keep them free of controllers and process state.
"""

from __future__ import annotations

from datetime import datetime

from .assistant_code import LANGUAGE_ALIASES
from .domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    NotebookRun,
    OperatorExecution,
    OperatorExecutionStatus,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError


async def active_control_assistant_session(
    store: NebulaStore, engagement_id: str
) -> ChatSession | None:
    """The project's current Butler conversation, if one is open."""
    (await store.get(Engagement, engagement_id))
    return next(
        (
            item
            for item in (
                await store.find_entities(
                    ChatSession,
                    {},
                    engagement_id=engagement_id,
                    newest_first=True,
                    limit=None,
                )
            )
            if item.metadata.get("control_assistant") is True
            and not item.metadata.get("butler_recovery_case_id")
            and item.metadata.get("archived_at") is None
        ),
        None,
    )


async def stored_chat_goal(store: NebulaStore, session_id: str) -> ChatGoal:
    (await store.get(ChatSession, session_id))
    goals = await store.list_session_entities(ChatGoal, session_id)
    if not goals:
        raise NotFoundError(f"chat goal not found for session: {session_id}")
    if len(goals) > 1:
        raise ConflictError("conversation has more than one authoritative goal")
    return goals[0]


def presented_chat_goal(goal: ChatGoal) -> ChatGoal:
    """Current presentation state, without advancing the durable revision."""
    if goal.status != ChatGoalStatus.RUNNING or goal.active_since is None:
        return goal
    # The open stretch is reported as banked up to now and still open from
    # now, so a client that adds the time since ``active_since`` to
    # ``elapsed_seconds`` counts it once, not twice.
    now = utc_now()
    return goal.model_copy(
        update={
            "elapsed_seconds": goal.active_elapsed_seconds(now),
            "active_since": now,
        }
    )


async def active_notebook_runs(
    store: NebulaStore, engagement_id: str
) -> list[NotebookRun]:
    (await store.get(Engagement, engagement_id))
    return await store.find_entities(
        NotebookRun,
        {"engagement_id": engagement_id, "status": ["queued", "running"]},
        limit=None,
    )


async def project_executions(
    store: NebulaStore, engagement_id: str | None
) -> list[OperatorExecution]:
    rows: list[OperatorExecution] = []
    offset = 0
    while True:
        page = await store.list_entities(
            OperatorExecution,
            engagement_id=engagement_id,
            offset=offset,
            limit=1_000,
        )
        rows.extend(page)
        if len(page) < 1_000:
            return rows
        offset += len(page)


def filter_executions(
    rows: list[OperatorExecution],
    *,
    offset: int = 0,
    limit: int = 100,
    status: OperatorExecutionStatus | None = None,
    language: str | None = None,
    operator_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    query: str | None = None,
) -> list[OperatorExecution]:
    """Apply the Execution history filters; dates must carry a timezone."""
    folded = query.casefold() if query else None
    operator_folded = operator_id.casefold() if operator_id else None
    filtered = [
        row
        for row in rows
        if (status is None or row.status == status)
        and (
            language is None or row.language == LANGUAGE_ALIASES.get(language, language)
        )
        and (operator_folded is None or operator_folded in row.operator_id.casefold())
        and (date_from is None or row.queued_at >= date_from)
        and (date_to is None or row.queued_at < date_to)
        and (
            folded is None
            or folded in row.source_preview.casefold()
            or folded in row.operator_id.casefold()
        )
    ]
    filtered.sort(key=lambda row: (row.queued_at, row.id), reverse=True)
    return filtered[offset : offset + limit]
