"""Butler mode's status board: every project's goal chats in one bounded read.

The board answers "what is running, and what needs me?" across all projects
without waking a project worker. It lists chats that have a goal, read from
durable ``chat_goals`` rows through the project index and joined to their
chats by ID, in three sections by goal status: **active** (running),
**idle** (paused, blocked, or a draft Butler proposed and nobody has started)
and **completed** (completed or cancelled). Counts per project and in total are
exact. Listed chats are enriched with their turn state (working, waiting on
the operator, or a failed last turn) from their own pending and newest turns,
never from a scan of every project's turns. A card also carries Butler's newest open
(or freshly resolved) recovery case about its chat, so the operator sees
that Butler is investigating it or needs them, and the project Work stage
of the Work item assigned to it. Chats without a goal, Butler's own
conversations, side chats, subagent chats and archived projects or chats are
never listed. The operator can narrow the board to goal chats in chosen Work
stages (by label, across projects) or with no stage; every read counts the
searched goal chats per stage so the filter shows what each choice holds.
Both the supervisor's API worker and the single-process app mount this route.
"""

from __future__ import annotations

from datetime import timedelta
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from .work_priority import marked_high
from .butler_case_stage import cases_for_goal_chats
from .chat_work_stage import work_stage_labels_in_projects, work_stages_for_chats
from .stored_entities import decode_stored
from .chat_subagents import is_subagent_session
from .chat_turn_headers import (
    ChatTurnHeader,
    latest_turn_headers,
    pending_turn_headers_of_sessions,
    pending_turn_headers_in_projects,
)
from .conditional_reads import conditional, content_etag, json_native, native_etag
from .database import EntityRow
from .domain import (
    GOAL_COMPLETION_REVIEW_REASON,
    Approval,
    ApprovalStatus,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    HarnessInteraction,
    HarnessInteractionStatus,
    ProjectHold,
)
from .linked_project_chats import (
    UNFINISHED_TURN_STATUSES,
    session_state,
    visible_pending,
)
from .project_hold_gate import ACTIVE_HOLD_STATUSES
from .storage import NebulaStore, NotFoundError, not_temporary_chat_session

BoardSection = Literal["active", "idle", "completed"]
SECTIONS: tuple[BoardSection, ...] = ("active", "idle", "completed")
SECTION_STATUSES: dict[BoardSection, tuple[str, ...]] = {
    "active": (ChatGoalStatus.RUNNING.value,),
    "idle": (
        ChatGoalStatus.PAUSED.value,
        ChatGoalStatus.BLOCKED.value,
        ChatGoalStatus.DRAFT.value,
    ),
    "completed": (ChatGoalStatus.COMPLETED.value, ChatGoalStatus.CANCELLED.value),
}
_SECTION_OF_STATUS = {
    status: section
    for section, statuses in SECTION_STATUSES.items()
    for status in statuses
}
# Other chat reads (the brief, file attribution) count a failed last turn as
# failed for a day; the board shows it while the goal stays active.
FAILED_WINDOW = timedelta(hours=24)
MAX_FAILED_ITEMS = 100
# The sections a poll lists; Completed is paged on request.
POLLED: tuple[BoardSection, ...] = ("active", "idle")
# A poll lists at most this many chats of a section; the counts stay exact.
MAX_SECTION_ITEMS = 500
_STATE_ORDER = {"waiting": 0, "working": 1, "failed": 2, "idle": 3}
_SUMMARY_CHARACTERS = 160
# The board's stage filter names at most this many stages at once.
MAX_STAGE_FILTERS = 50
# A blocked reason up to this long is shown on the card's status line.
_SHORT_REASON_CHARACTERS = 80

_WORKING_TEXT = {
    ChatTurnStatus.QUEUED.value: "Queued to start",
    ChatTurnStatus.ROUTING.value: "Working",
    ChatTurnStatus.WAITING_CALLBACK.value: "Waiting for a command to finish",
    ChatTurnStatus.FINALIZING.value: "Writing the answer",
    ChatTurnStatus.INTERRUPTED.value: "Resuming after an interruption",
    ChatTurnStatus.WAITING_APPROVAL.value: "Waiting for an approval",
}

_Entry = tuple[ChatSession, "BoardGoal", str, str, ChatTurnHeader | None]
"""(chat, goal, project name, turn state, the turn that state comes from)."""


def _short(text: str | None, limit: int = _SUMMARY_CHARACTERS) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


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


@dataclass(frozen=True, slots=True)
class BoardGoal:
    """The goal fields a board card shows, read from SQL without the payload."""

    status: ChatGoalStatus
    objective: str
    current_step: int
    plan_length: int
    revision: int
    blocked_reason: str | None = None

    @classmethod
    def of(cls, goal: ChatGoal | BoardGoal) -> BoardGoal:
        if isinstance(goal, BoardGoal):
            return goal
        return cls(
            status=goal.status,
            objective=goal.objective,
            current_step=goal.current_step,
            plan_length=len(goal.plan),
            revision=goal.revision,
            blocked_reason=goal.blocked_reason,
        )


def _is_proposal(session: ChatSession) -> bool:
    return session.metadata.get("proposed_by") == "butler"


def _goal_status_line(goal: BoardGoal) -> str | None:
    """What an idle or completed goal chat's card says when nothing needs the operator."""

    if goal.status == ChatGoalStatus.PAUSED:
        return "Paused"
    if goal.status == ChatGoalStatus.BLOCKED:
        if goal.blocked_reason == GOAL_COMPLETION_REVIEW_REASON:
            return "Awaiting closure review"
        reason = " ".join((goal.blocked_reason or "").split())
        if reason and len(reason) <= _SHORT_REASON_CHARACTERS:
            return f"Blocked: {reason}"
        return "Blocked"
    if goal.status == ChatGoalStatus.COMPLETED:
        return "Completed"
    if goal.status == ChatGoalStatus.CANCELLED:
        return "Cancelled"
    return None


class _GoalChats:
    """The board's SQL: goal rows by kind and project, joined to their chats by ID."""

    def __init__(self, query: str) -> None:
        self.goal = aliased(EntityRow, name="board_goal")
        self.project = aliased(EntityRow, name="board_project")
        self.status = self.goal.payload["status"].as_string()
        self.query = query

    def select(self, *columns: Any) -> Any:
        goal, project = self.goal, self.project
        statement = (
            select(*columns)
            .select_from(goal)
            .join(
                EntityRow,
                and_(
                    EntityRow.id == goal.payload["session_id"].as_string(),
                    EntityRow.kind == ChatSession.entity_kind,
                    EntityRow.engagement_id == goal.engagement_id,
                ),
            )
            .join(
                project,
                and_(
                    project.id == goal.engagement_id,
                    project.kind == Engagement.entity_kind,
                ),
            )
            .where(
                goal.kind == ChatGoal.entity_kind,
                func.coalesce(project.payload["status"].as_string(), "")
                != EngagementStatus.ARCHIVED.value,
                goal.payload["parent_goal_id"].as_string().is_(None),
                not_temporary_chat_session(),
                func.coalesce(
                    EntityRow.payload["metadata"]["control_assistant"].as_boolean(),
                    False,
                ).is_(False),
                EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
                EntityRow.payload["metadata"]["subagent_id"].as_string().is_(None),
                # An unstarted draft is listed only when Butler proposed it.
                or_(
                    self.status != ChatGoalStatus.DRAFT.value,
                    func.coalesce(
                        EntityRow.payload["metadata"]["proposed_by"].as_string(), ""
                    )
                    == "butler",
                ),
            )
        )
        if self.query:
            statement = statement.where(
                or_(
                    EntityRow.payload["title"]
                    .as_string()
                    .icontains(self.query, autoescape=True),
                    EntityRow.payload["model"]
                    .as_string()
                    .icontains(self.query, autoescape=True),
                    project.payload["name"]
                    .as_string()
                    .icontains(self.query, autoescape=True),
                )
            )
        return statement

    def rows(self, section: BoardSection) -> Any:
        """A section's chats, goals and project names, newest activity first."""

        # Operator-marked high-priority chats, or chats in a high-priority
        # project, lead every section.
        order: list[Any] = [
            case(
                (
                    or_(
                        EntityRow.payload["metadata"]["priority"].as_string() == "high",
                        self.project.payload["metadata"]["priority"].as_string()
                        == "high",
                    ),
                    0,
                ),
                else_=1,
            )
        ]
        if section == "idle":
            # Butler's proposals wait for the operator, so they lead Idle.
            order.append(case((self.status == ChatGoalStatus.DRAFT.value, 0), else_=1))
        order += [EntityRow.updated_at.desc(), EntityRow.id.desc()]
        goal = self.goal
        return (
            self.select(
                EntityRow.payload,
                self.status,
                goal.payload["objective"].as_string(),
                goal.payload["current_step"].as_integer(),
                func.json_array_length(goal.payload["plan"]),
                goal.revision,
                goal.payload["blocked_reason"].as_string(),
                self.project.payload["name"].as_string(),
            )
            .where(self.status.in_(SECTION_STATUSES[section]))
            .order_by(*order)
        )


def butler_board_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/butler/board", response_model=None)
    async def read_butler_board(
        request: Request,
        response: Response,
        q: str = Query(default="", max_length=200),
        section: BoardSection | None = None,
        project_id: str | None = Query(default=None, max_length=200),
        state: Literal["active", "idle"] | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        stage: list[str] = Query(default_factory=list, max_length=MAX_STAGE_FILTERS),
        no_stage: bool = False,
    ) -> Any:
        """Goal chats across every project in Active, Idle and Completed.

        Without ``section`` (the poll) the body lists the Active and Idle
        sections, each up to ``MAX_SECTION_ITEMS``, Active chats needing the
        operator first. ``section`` pages through one section by recent
        activity (``offset``, ``limit``), optionally in one project
        (``project_id``). Every answer carries exact section counts in total
        and per project, and the poll also counts Active chats by turn state.
        ``state=idle`` is the older spelling of ``section=idle``.

        ``stage`` (repeatable, a Work stage label, any case) and ``no_stage``
        narrow every list and count to goal chats in those stages or with no
        stage. ``states`` (the header's needs-you and working counts) stays
        unfiltered, so a filter never hides a chat waiting on the operator.
        ``stages`` and ``no_stage`` count the searched goal chats per stage
        and section, ignoring the stage filter.
        """

        response.headers["Cache-Control"] = "no-store"
        chats = _GoalChats(q.strip())
        if section is None and state == "idle":
            section = "idle"
        wanted_stages = {
            label for value in stage if (label := " ".join(value.split()).casefold())
        }
        filtering = bool(wanted_stages) or no_stage
        async with store.database.session() as database:
            goal_rows = list(
                await database.execute(
                    chats.select(
                        chats.goal.engagement_id, chats.status, EntityRow.id
                    ).distinct()
                )
            )
            stage_of, stage_rank = await work_stage_labels_in_projects(
                database, {str(row[0]) for row in goal_rows}
            )

            def selected(session_id: str) -> bool:
                label = stage_of.get(session_id)
                if label is None:
                    return no_stage
                return label.casefold() in wanted_stages

            project_counts: dict[str, dict[str, int]] = {}
            # Per case-folded label: section counts and each spelling's chats.
            stage_counts: dict[str, dict[str, int]] = {}
            spellings: dict[str, dict[str, int]] = {}
            unstaged = dict.fromkeys(SECTIONS, 0)
            proposals = 0
            matching: set[str] = set()
            for engagement_id, status, session_id in goal_rows:
                listed_section = _SECTION_OF_STATUS.get(str(status))
                if listed_section is None:
                    continue
                if status == ChatGoalStatus.DRAFT.value:
                    proposals += 1
                label = stage_of.get(str(session_id))
                if label is None:
                    unstaged[listed_section] += 1
                else:
                    folded = label.casefold()
                    stage_counts.setdefault(folded, dict.fromkeys(SECTIONS, 0))[
                        listed_section
                    ] += 1
                    spelled = spellings.setdefault(folded, {})
                    spelled[label] = spelled.get(label, 0) + 1
                if filtering and not selected(str(session_id)):
                    continue
                matching.add(str(session_id))
                counts = project_counts.setdefault(
                    str(engagement_id), dict.fromkeys(SECTIONS, 0)
                )
                counts[listed_section] += 1
            names: dict[str, str] = {}
            if project_counts:
                for engagement_id, name in await database.execute(
                    select(EntityRow.id, EntityRow.payload["name"].as_string()).where(
                        EntityRow.kind == Engagement.entity_kind,
                        EntityRow.id.in_(list(project_counts)),
                    )
                ):
                    names[str(engagement_id)] = str(name or "")
            listed: dict[BoardSection, list[tuple[ChatSession, BoardGoal, str]]] = {}
            wanted: tuple[BoardSection, ...] = (section,) if section else POLLED
            for name in wanted:
                statement = chats.rows(name)
                if project_id:
                    statement = statement.where(chats.goal.engagement_id == project_id)
                if filtering and name != "active":
                    # Active is filtered after its turn states are counted.
                    statement = statement.where(EntityRow.id.in_(matching))
                if section is None or section == "active":
                    # Active is ordered by turn state, which SQL cannot see.
                    statement = statement.limit(MAX_SECTION_ITEMS + 1)
                else:
                    statement = statement.offset(offset).limit(limit + 1)
                listed[name] = [
                    (
                        decode_stored(ChatSession, chat),
                        BoardGoal(
                            status=ChatGoalStatus(status),
                            objective=str(objective or ""),
                            current_step=int(current_step or 0),
                            plan_length=int(plan_length or 0),
                            revision=int(revision),
                            blocked_reason=blocked_reason,
                        ),
                        str(project or ""),
                    )
                    for (
                        chat,
                        status,
                        objective,
                        current_step,
                        plan_length,
                        revision,
                        blocked_reason,
                        project,
                    ) in await database.execute(statement)
                ]
            session_ids = {
                chat.id for rows in listed.values() for chat, _goal, _name in rows
            }
            # Pending turns come from the status index (few rows), not from
            # every turn of the listed chats.
            pending: dict[str, ChatTurnHeader] = {}
            for pending_header in visible_pending(
                await pending_turn_headers_of_sessions(
                    database, session_ids, statuses=UNFINISHED_TURN_STATUSES
                )
            ):
                pending.setdefault(pending_header.session_id, pending_header)
            latest = await latest_turn_headers(
                database,
                [
                    chat.id
                    for chat, _goal, _name in listed.get("active", [])
                    if chat.id not in pending
                ],
            )

        entries: dict[BoardSection, list[_Entry]] = {}
        for name, rows in listed.items():
            section_entries: list[_Entry] = []
            for chat, goal, project_name in rows:
                header = pending.get(chat.id)
                turn_state = await session_state(store, chat, header)
                newest = latest.get(chat.id)
                if (
                    header is None
                    and name == "active"
                    and newest is not None
                    and newest.status == ChatTurnStatus.FAILED.value
                ):
                    turn_state, header = "failed", newest
                elif (
                    header is None
                    and goal.status == ChatGoalStatus.DRAFT
                    and _is_proposal(chat)
                ):
                    turn_state = "waiting"
                section_entries.append((chat, goal, project_name, turn_state, header))
            entries[name] = section_entries

        folded_rank: dict[str, int] = {}
        for label, position in stage_rank.items():
            folded = label.casefold()
            folded_rank[folded] = min(folded_rank.get(folded, position), position)
        totals = {
            name: sum(counts[name] for counts in project_counts.values())
            for name in SECTIONS
        }
        states: dict[str, int] | None = None
        if "active" in entries and section is None:
            states = {"waiting": proposals, "working": 0, "failed": 0}
            for entry in entries["active"]:
                if entry[3] in states:
                    states[entry[3]] += 1
        # Core listed at most MAX_SECTION_ITEMS + 1 Active chats before the filter.
        active_cut = len(entries.get("active", [])) > MAX_SECTION_ITEMS
        if "active" in entries and filtering:
            entries["active"] = [
                entry for entry in entries["active"] if entry[0].id in matching
            ]
        if "active" in entries:
            entries["active"].sort(
                key=lambda item: (item[0].updated_at, item[0].id), reverse=True
            )
            entries["active"].sort(key=lambda item: _STATE_ORDER[item[3]])
            entries["active"].sort(key=lambda item: not marked_high(item[0].metadata))

        truncated = False
        next_offset = None
        shown: list[tuple[BoardSection, _Entry]] = []
        if section is None:
            for name in POLLED:
                truncated = truncated or len(entries[name]) > MAX_SECTION_ITEMS
                shown += [(name, entry) for entry in entries[name][:MAX_SECTION_ITEMS]]
            truncated = truncated or active_cut
        elif section == "active":
            page = entries["active"][offset : offset + limit + 1]
            truncated = active_cut
            next_offset = offset + limit if len(page) > limit else None
            shown = [("active", entry) for entry in page[:limit]]
        else:
            page = entries[section]
            next_offset = offset + limit if len(page) > limit else None
            shown = [(section, entry) for entry in page[:limit]]

        approvals = await _pending_approvals(
            store,
            [
                (chat, project_name, turn_state, header)
                for _name, (chat, _goal, project_name, turn_state, header) in shown
            ],
        )
        questions = await _pending_questions(
            store,
            [entry[0].id for _name, entry in shown if entry[3] == "waiting"],
        )
        async with store.database.session() as database:
            shown_ids = [entry[0].id for _name, entry in shown]
            shown_projects = {entry[0].engagement_id for _name, entry in shown}
            cases = await cases_for_goal_chats(database, shown_ids, shown_projects)
            work_stages = await work_stages_for_chats(
                database, shown_ids, shown_projects
            )
        items = await _with_holds(
            store,
            [
                await _board_item(store, name, *entry, approvals, questions)
                for name, entry in shown
            ],
        )
        for item in items:
            # Butler's newest open (or freshly resolved) case about this chat.
            item["butler_case"] = cases.get(item["session_id"])
            # The project Work stage of the chat's assigned Work item.
            item["work_stage"] = work_stages.get(item["session_id"])
        body = {
            "counts": totals,
            "states": states,
            "projects": sorted(
                (
                    {
                        "id": engagement_id,
                        "name": names.get(engagement_id, ""),
                        "counts": counts,
                    }
                    for engagement_id, counts in project_counts.items()
                ),
                key=lambda item: (str(item["name"]).casefold(), str(item["id"])),
            ),
            "stages": [
                {
                    # Projects may spell a stage differently ("Recon",
                    # "recon"); show the spelling most chats use.
                    "label": min(
                        spellings[folded].items(), key=lambda item: (-item[1], item[0])
                    )[0],
                    "counts": stage_counts[folded],
                }
                for folded in sorted(
                    stage_counts,
                    key=lambda folded: (
                        folded_rank.get(folded, len(folded_rank)),
                        folded,
                    ),
                )
            ],
            "no_stage": unstaged,
            "section": section,
            "items": items,
            "next_offset": next_offset,
            "truncated": truncated,
        }
        # The body is encoded once, for both the ETag and the response: a
        # board of hundreds of goal chats is otherwise walked twice by
        # FastAPI's generic encoder, which costs more than the reads.
        native = json_native(body)
        if unchanged := conditional(
            request, response, native_etag("butler-board", native)
        ):
            return unchanged
        return JSONResponse(native, headers=dict(response.headers))

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
                (decode_stored(ChatSession, payload), str(name or ""))
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


async def _with_holds(
    store: NebulaStore, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark each card whose chat (or parent chat) is on a chat hold.

    The board groups those cards by hold batch, so the operator sees what
    they set aside and can release it.
    """

    if not items:
        return items
    holds = {
        hold.session_id: {
            "id": hold.id,
            "batch_id": hold.batch_id,
            "status": hold.status,
            "reason": hold.reason,
            "created_at": hold.created_at,
        }
        for hold in await store.find_entities(
            ProjectHold, {"status": list(ACTIVE_HOLD_STATUSES)}
        )
        if hold.session_id
    }
    for item in items:
        item["hold"] = holds.get(item["session_id"]) or holds.get(
            item.get("parent_session_id") or ""
        )
    return items


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
        interaction = decode_stored(HarnessInteraction, payload)
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
        goal = decode_stored(ChatGoal, payload)
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
    goals: Mapping[str, ChatGoal | BoardGoal],
) -> dict[str, Any]:
    approval = approvals.get(session.id)
    question = questions.get(session.id)
    found = goals.get(session.id)
    goal = BoardGoal.of(found) if found is not None else None
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
        "priority": "high" if marked_high(session.metadata) else "normal",
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
                "plan_length": goal.plan_length,
                "revision": goal.revision,
            }
            if goal
            else None
        ),
    }


async def _board_item(
    store: NebulaStore,
    section: BoardSection,
    session: ChatSession,
    goal: BoardGoal,
    project_name: str,
    state: str,
    header: ChatTurnHeader | None,
    approvals: dict[str, Approval],
    questions: dict[str, HarnessInteraction],
) -> dict[str, Any]:
    """One goal chat's card: ``_item`` plus its section and goal status line."""

    item = await _item(
        store,
        session,
        project_name,
        state,
        header,
        approvals,
        questions,
        {session.id: goal},
    )
    if section != "active" and state not in {"waiting", "failed"}:
        # An idle or completed goal says where it stopped, even mid-turn.
        item["now"] = _goal_status_line(goal)
    return {"section": section, **item}
