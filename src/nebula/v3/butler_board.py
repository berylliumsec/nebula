"""Butler mode's status board: every project's chats in one bounded read.

The board answers "what is running, and what needs me?" across all projects
without waking a project worker. It reads durable rows only: pending turn
headers for running chats, pending approvals and harness questions for chats
waiting on the operator, the newest failed turns of the last day, and each
chat's goal. Idle chats are only counted unless the caller pages through them.
Both the supervisor's API worker and the single-process app mount this route.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from .chat_subagents import is_subagent_session
from .chat_turn_headers import (
    ChatTurnHeader,
    latest_turn_header,
    pending_turn_headers_in_projects,
)
from .conditional_reads import conditional, content_etag
from .database import EntityRow
from .domain import (
    Approval,
    ApprovalStatus,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    HarnessInteraction,
    HarnessInteractionStatus,
    utc_now,
)
from .linked_project_chats import (
    UNFINISHED_TURN_STATUSES,
    session_state,
    visible_pending,
)
from .storage import NebulaStore, NotFoundError, not_temporary_chat_session

# A board shows at most this many running or waiting chats; the counts stay exact.
MAX_ACTIVE_ITEMS = 500
# Failed chats stay on the board for a day after their last turn failed.
FAILED_WINDOW = timedelta(hours=24)
MAX_FAILED_ITEMS = 100
_STATE_ORDER = {"waiting": 0, "working": 1, "failed": 2, "idle": 3}
_SUMMARY_CHARACTERS = 160

_WORKING_TEXT = {
    ChatTurnStatus.QUEUED.value: "Queued to start",
    ChatTurnStatus.ROUTING.value: "Working",
    ChatTurnStatus.WAITING_CALLBACK.value: "Waiting for a command to finish",
    ChatTurnStatus.FINALIZING.value: "Writing the answer",
    ChatTurnStatus.INTERRUPTED.value: "Resuming after an interruption",
    ChatTurnStatus.WAITING_APPROVAL.value: "Waiting for an approval",
}


def _short(text: str | None) -> str:
    value = " ".join((text or "").split())
    if len(value) <= _SUMMARY_CHARACTERS:
        return value
    return value[: _SUMMARY_CHARACTERS - 1].rstrip() + "…"


def _approval_summary(approval: Approval) -> str:
    request = approval.exact_request or {}
    tool = request.get("tool_name") or "a tool request"
    arguments = request.get("arguments")
    detail = None
    if isinstance(arguments, dict):
        for key in ("command", "path", "url", "query"):
            if isinstance(arguments.get(key), str) and arguments[key].strip():
                detail = arguments[key]
                break
    detail = detail or approval.target
    return _short(f"Approve {tool}: {detail}" if detail else f"Approve {tool}")


def _visible_projects() -> Any:
    """Every project that is not archived, as a subquery of IDs."""

    return (
        select(EntityRow.id)
        .where(
            EntityRow.kind == Engagement.entity_kind,
            func.coalesce(EntityRow.payload["status"].as_string(), "")
            != EngagementStatus.ARCHIVED.value,
        )
        .scalar_subquery()
    )


def _proposal_condition() -> Any:
    """A chat Butler proposed whose goal is still an unstarted draft."""

    goal = aliased(EntityRow)
    drafts = (
        select(goal.payload["session_id"].as_string())
        .where(
            goal.kind == ChatGoal.entity_kind,
            goal.payload["status"].as_string() == ChatGoalStatus.DRAFT.value,
        )
        .scalar_subquery()
    )
    return and_(
        func.coalesce(EntityRow.payload["metadata"]["proposed_by"].as_string(), "")
        == "butler",
        EntityRow.id.in_(drafts),
    )


def _is_proposal(session: ChatSession) -> bool:
    return session.metadata.get("proposed_by") == "butler"


def butler_board_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/butler/board", response_model=None)
    async def read_butler_board(
        request: Request,
        response: Response,
        q: str = Query(default="", max_length=200),
        state: Literal["active", "idle"] = "active",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Any:
        """Chats across every project, those needing the operator first.

        ``state=active`` (the default) lists chats waiting on the operator
        (including goal chats Butler proposed and nobody has started yet),
        running chats and chats whose last turn failed in the last day, with
        exact counts for every state. ``state=idle`` pages through the rest by
        recent activity. Butler's own conversations, side chats and archived
        projects or chats are never listed.
        """

        response.headers["Cache-Control"] = "no-store"
        projects = _visible_projects()
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
                EntityRow.engagement_id.in_(projects),
                not_temporary_chat_session(),
                func.coalesce(
                    EntityRow.payload["metadata"]["control_assistant"].as_boolean(),
                    False,
                ).is_(False),
                EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
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

        cutoff = utc_now() - FAILED_WINDOW
        async with store.database.session() as database:
            headers = await pending_turn_headers_in_projects(
                database, projects, statuses=UNFINISHED_TURN_STATUSES
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
            failed_session_ids = [
                session_id
                for session_id in dict.fromkeys(
                    (
                        await database.scalars(
                            select(EntityRow.chat_session_id)
                            .where(
                                EntityRow.kind == ChatTurn.entity_kind,
                                EntityRow.engagement_id.in_(projects),
                                EntityRow.chat_turn_status
                                == ChatTurnStatus.FAILED.value,
                                EntityRow.updated_at >= cutoff,
                            )
                            .order_by(EntityRow.updated_at.desc())
                            .limit(MAX_FAILED_ITEMS * 4)
                        )
                    ).all()
                )
                if session_id and session_id not in pending
            ]
            failed: dict[str, ChatTurnHeader] = {}
            for session_id in failed_session_ids:
                latest = await latest_turn_header(database, session_id)
                if latest and latest.status == ChatTurnStatus.FAILED.value:
                    failed[session_id] = latest
                if len(failed) >= MAX_FAILED_ITEMS:
                    break
            failed_rows = (
                list(
                    await database.execute(
                        visible(
                            select(EntityRow.payload, project.payload["name"])
                        ).where(EntityRow.id.in_(list(failed)))
                    )
                )
                if failed
                else []
            )
            total = int(
                await database.scalar(
                    visible(select(func.count()).select_from(EntityRow))
                )
                or 0
            )
            # Butler's proposed goal chats wait for the operator to start them.
            # They have no turn yet, so they are found by their draft goal.
            listed = [*pending, *failed]

            def proposed(statement: Any) -> Any:
                statement = visible(statement).where(_proposal_condition())
                if listed:
                    statement = statement.where(EntityRow.id.not_in(listed))
                return statement

            proposal_total = int(
                await database.scalar(
                    proposed(select(func.count()).select_from(EntityRow))
                )
                or 0
            )
            proposal_rows = (
                list(
                    await database.execute(
                        proposed(select(EntityRow.payload, project.payload["name"]))
                        .order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
                        .limit(MAX_ACTIVE_ITEMS)
                    )
                )
                if proposal_total
                else []
            )

        entries: list[tuple[ChatSession, str, str, ChatTurnHeader | None]] = []
        for payload, name in active_rows:
            session = ChatSession.model_validate(payload)
            header = pending[session.id]
            entries.append(
                (
                    session,
                    str(name or ""),
                    await session_state(store, session, header),
                    header,
                )
            )
        for payload, name in failed_rows:
            session = ChatSession.model_validate(payload)
            entries.append((session, str(name or ""), "failed", failed[session.id]))
        counts = {"waiting": 0, "working": 0, "failed": 0, "idle": 0}
        for entry in entries:
            counts[entry[2]] += 1
        counts["waiting"] += proposal_total
        counts["idle"] = max(0, total - len(entries) - proposal_total)
        for payload, name in proposal_rows:
            entries.append(
                (ChatSession.model_validate(payload), str(name or ""), "waiting", None)
            )
        unlisted_proposals = proposal_total - len(proposal_rows)

        if state == "idle":
            excluded = [entry[0].id for entry in entries]
            statement = visible(
                select(EntityRow.payload, project.payload["name"])
            ).where(~_proposal_condition())
            if excluded:
                statement = statement.where(EntityRow.id.not_in(excluded))
            statement = (
                statement.order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
            async with store.database.session(bulk=True) as database:
                idle = [
                    (ChatSession.model_validate(payload), str(name or ""), "idle", None)
                    for payload, name in await database.execute(statement)
                ]
            page = idle[:limit]
            body = {
                "counts": counts,
                "items": [await _item(store, *entry, {}, {}, {}) for entry in page],
                "next_offset": offset + limit if len(idle) > limit else None,
                "truncated": False,
            }
        else:
            entries.sort(
                key=lambda item: (item[0].updated_at, item[0].id), reverse=True
            )
            entries.sort(key=lambda item: _STATE_ORDER[item[2]])
            shown = entries[:MAX_ACTIVE_ITEMS]
            session_ids = [entry[0].id for entry in shown]
            approvals = await _pending_approvals(store, shown)
            questions = await _pending_questions(store, session_ids)
            goals = await _goals(store, session_ids)
            body = {
                "counts": counts,
                "items": [
                    await _item(store, *entry, approvals, questions, goals)
                    for entry in shown
                ],
                "next_offset": None,
                "truncated": len(entries) > len(shown) or unlisted_proposals > 0,
            }
        if unchanged := conditional(
            request, response, content_etag("butler-board", body)
        ):
            return unchanged
        return body

    @router.get("/butler/threads", response_model=None)
    async def list_butler_threads(
        request: Request,
        response: Response,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Any:
        """Butler mode threads in every project, newest activity first.

        A thread is working while Butler answers it and waiting when that
        answer stopped for the operator. Archived threads and projects are
        left out.
        """

        response.headers["Cache-Control"] = "no-store"
        projects = _visible_projects()
        project = aliased(EntityRow)
        async with store.database.session() as database:
            rows = list(
                await database.execute(
                    select(EntityRow.payload, project.payload["name"])
                    .join(
                        project,
                        and_(
                            project.id == EntityRow.engagement_id,
                            project.kind == Engagement.entity_kind,
                        ),
                    )
                    .where(
                        EntityRow.kind == ChatSession.entity_kind,
                        EntityRow.engagement_id.in_(projects),
                        EntityRow.payload["metadata"]["butler_thread"]
                        .as_boolean()
                        .is_(True),
                        EntityRow.payload["metadata"]["archived_at"]
                        .as_string()
                        .is_(None),
                    )
                    .order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
                    .limit(limit)
                )
            )
            sessions = [
                (ChatSession.model_validate(payload), str(name or ""))
                for payload, name in rows
            ]
            pending: dict[str, ChatTurnHeader] = {}
            if sessions:
                thread_projects = sorted({item.engagement_id for item, _ in sessions})
                for header in visible_pending(
                    await pending_turn_headers_in_projects(
                        database, thread_projects, statuses=UNFINISHED_TURN_STATUSES
                    )
                ):
                    pending.setdefault(header.session_id, header)
        items = []
        for session, name in sessions:
            thread_turn = pending.get(session.id)
            items.append(
                {
                    "session_id": session.id,
                    "title": session.title,
                    "project": {"id": session.engagement_id, "name": name},
                    "state": await session_state(store, session, thread_turn),
                    "turn_id": thread_turn.id if thread_turn else None,
                    "updated_at": session.updated_at,
                }
            )
        body = {"items": items}
        if unchanged := conditional(
            request, response, content_etag("butler-threads", body)
        ):
            return unchanged
        return body

    from .chat_brief import chat_brief_router

    router.include_router(chat_brief_router(store))
    return router


async def _pending_approvals(
    store: NebulaStore,
    entries: list[tuple[ChatSession, str, str, ChatTurnHeader | None]],
) -> dict[str, Approval]:
    """Each waiting chat's pending approval, by chat session ID."""

    approvals: dict[str, Approval] = {}
    for session, _name, state, header in entries:
        if state != "waiting" or header is None or not header.approval_id:
            continue
        try:
            approval = await store.get(Approval, header.approval_id)
        except NotFoundError:
            # diagnostic-expected: an approval decided and pruned after the
            # turn scan leaves the chat with no pending action to show.
            continue
        if approval.status == ApprovalStatus.PENDING:
            approvals[session.id] = approval
    return approvals


async def _pending_questions(
    store: NebulaStore, session_ids: list[str]
) -> dict[str, HarnessInteraction]:
    """The newest pending harness question of each chat, by chat session ID."""

    if not session_ids:
        return {}
    async with store.database.session() as database:
        rows = (
            await database.scalars(
                select(EntityRow.payload)
                .where(
                    EntityRow.kind == HarnessInteraction.entity_kind,
                    EntityRow.payload["chat_session_id"].as_string().in_(session_ids),
                    EntityRow.payload["status"].as_string()
                    == HarnessInteractionStatus.PENDING.value,
                )
                .order_by(EntityRow.created_at)
            )
        ).all()
    questions: dict[str, HarnessInteraction] = {}
    for payload in rows:
        interaction = HarnessInteraction.model_validate(payload)
        if interaction.chat_session_id:
            questions[interaction.chat_session_id] = interaction
    return questions


async def _goals(store: NebulaStore, session_ids: list[str]) -> dict[str, ChatGoal]:
    if not session_ids:
        return {}
    async with store.database.session() as database:
        rows = (
            await database.scalars(
                select(EntityRow.payload).where(
                    EntityRow.kind == ChatGoal.entity_kind,
                    EntityRow.payload["session_id"].as_string().in_(session_ids),
                )
            )
        ).all()
    goals: dict[str, ChatGoal] = {}
    for payload in rows:
        goal = ChatGoal.model_validate(payload)
        goals[goal.session_id] = goal
    return goals


async def _item(
    store: NebulaStore,
    session: ChatSession,
    project_name: str,
    state: str,
    header: ChatTurnHeader | None,
    approvals: dict[str, Approval],
    questions: dict[str, HarnessInteraction],
    goals: dict[str, ChatGoal],
) -> dict[str, Any]:
    approval = approvals.get(session.id)
    question = questions.get(session.id)
    goal = goals.get(session.id)
    attention: dict[str, Any] | None = None
    if (
        state == "waiting"
        and header is None
        and _is_proposal(session)
        and goal is not None
        and goal.status == ChatGoalStatus.DRAFT
    ):
        attention = {
            "kind": "proposal",
            "action_id": session.id,
            "summary": "Butler proposed this goal chat",
            "requested_at": session.created_at,
        }
    elif state == "waiting" and approval is not None:
        attention = {
            "kind": "approval",
            "action_id": approval.id,
            "summary": _approval_summary(approval),
            "requested_at": approval.requested_at,
        }
    elif state == "waiting" and question is not None:
        attention = {
            "kind": "question",
            "action_id": question.id,
            "summary": _short(question.prompt),
            "requested_at": question.created_at,
        }
    elif state == "waiting":
        attention = {
            "kind": "recovery",
            "action_id": header.id if header else None,
            "summary": "Interrupted: choose whether to retry or stop",
            "requested_at": header.updated_at if header else session.updated_at,
        }
    if state == "failed":
        now = _short(header.error if header else None) or "The last response failed"
    elif attention is not None:
        now = attention["summary"]
    elif header is not None:
        now = _WORKING_TEXT.get(header.status, "Working")
    else:
        now = None
    return {
        "session_id": session.id,
        "title": session.title,
        "project": {"id": session.engagement_id, "name": project_name},
        "state": state,
        "backend": session.backend.value,
        "provider_profile_id": session.provider_profile_id,
        "harness_profile_id": session.harness_profile_id,
        "model": session.model,
        "parent_session_id": session.parent_session_id,
        "is_subagent": is_subagent_session(session),
        "turn_id": header.id if header else None,
        "turn_status": header.status if header else None,
        "turn_started_at": header.created_at if header else None,
        "updated_at": session.updated_at,
        "session_revision": session.revision,
        "started_by": (
            "butler" if session.metadata.get("butler_initiated") is True else None
        ),
        "proposed_by": "butler" if _is_proposal(session) else None,
        "now": now,
        "attention": attention,
        "goal": (
            {
                "objective": _short(goal.objective),
                "status": goal.status.value,
                "current_step": goal.current_step,
                "plan_length": len(goal.plan),
                "revision": goal.revision,
            }
            if goal
            else None
        ),
    }
