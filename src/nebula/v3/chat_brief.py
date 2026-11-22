"""A chat's brief for Butler mode: what it is doing, turn by turn, without a model.

Clicking a card on the Butler board opens this read. It answers "what has this
agent done, what is it doing now, and does it need me?" from durable rows
only: the chat, its goal, a page of turn headers, each turn's tool-call
classification and the files Core saw it change. Every turn's one-line
summary is derived mechanically (the first sentence of its answer, else a
phrase built from its tool calls, else its failure), never by a model.

Reads are bounded: one page of turns (default 30, at most 100), the tool calls
and workspace observations of that page, and the newest observations for the
chat's recent files. Both the supervisor's API worker and the single-process
app mount it through the Butler board router.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select

from .stored_entities import decode_stored
from .butler_board import (
    FAILED_WINDOW,
    _WORKING_TEXT,
    _goals,
    _item,
    _pending_approvals,
    _pending_questions,
)
from .chat_turn_headers import (
    ChatTurnHeader,
    latest_turn_header,
    pending_session_turn_headers,
    session_turn_page,
)
from .conditional_reads import conditional, content_etag
from .database import EntityRow
from .domain import (
    Approval,
    ButlerAgentReport,
    ChatMessage,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    ToolCall,
    WorkspaceProvenanceObservation,
    utc_now,
)
from .linked_project_chats import (
    UNFINISHED_TURN_STATUSES,
    session_state,
    visible_pending,
)
from .storage import NebulaStore, NotFoundError

DEFAULT_TURNS = 30
MAX_TURNS = 100
MAX_FILES = 50
MAX_TURN_FILES = 20
LINE_CHARACTERS = 160
# Bounds on the rows one brief reads.
MAX_TOOL_CALL_ROWS = 2_000
MAX_SESSION_EDIT_ROWS = 500
MAX_OBSERVATION_ROWS = 200
MAX_REPORT_ROWS = 1_000
# Only the start of an answer is read; the line is its first sentence.
_ANSWER_PREFIX = 2_000

_COMMAND_WORDS = (
    "run_command",
    "command",
    "shell",
    "bash",
    "exec",
    "terminal",
    "process_io",
)
_EDIT_WORDS = (
    "write",
    "edit",
    "patch",
    "apply",
    "replace",
    "create_file",
    "filechange",
    "file_change",
    "delete_file",
    "move",
    "rename",
)
# Tools whose names look like an edit but change no workspace file.
_NOT_EDITS = ("notes.", "report_", "memory", "goal.", "plan.", "message_")
_PATH_KEYS = ("path", "file_path", "filePath", "filename", "target_file")
_COMMAND_KEYS = ("command", "cmd")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_MARKDOWN_LEAD = re.compile(r"^(?:[-*+>]\s+|\d+[.)]\s+)+")


def _bounded(text: str, limit: int = LINE_CHARACTERS) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def first_sentence(text: str | None) -> str | None:
    """The first sentence of an answer's prose, at most 160 characters."""

    if not text:
        return None
    prose = _FENCE.sub(" ", text)
    for paragraph in re.split(r"\n\s*\n", prose):
        # Headings name a section; the line is the prose under them.
        lines = [
            _MARKDOWN_LEAD.sub("", line.strip()).strip("*_` ")
            for line in paragraph.splitlines()
            if not _HEADING.match(line)
        ]
        candidate = " ".join(line for line in lines if line)
        if not candidate:
            continue
        sentence = _SENTENCE_END.split(candidate, maxsplit=1)[0]
        return _bounded(sentence)
    return None


def _tool_kind(name: str) -> str:
    lowered = name.lower()
    tail = lowered.rsplit(":", 1)[-1]
    if any(word in tail for word in _NOT_EDITS):
        return "tool"
    if any(word in tail for word in _COMMAND_WORDS):
        return "command"
    if any(word in tail for word in _EDIT_WORDS):
        return "edit"
    return "tool"


def _argument_text(arguments: Any, keys: Iterable[str]) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in keys:
        value = arguments.get(key)
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) for item in value)
        ):
            value = " ".join(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def approval_command(approval: Approval) -> str | None:
    """The exact command or target an approval asks for, when it names one."""

    request = approval.exact_request or {}
    arguments = request.get("arguments")
    return (
        _argument_text(arguments, _COMMAND_KEYS)
        or _argument_text(request, _COMMAND_KEYS)
        or _argument_text(arguments, (*_PATH_KEYS, "url", "query"))
        or approval.target
    )


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def tool_phrase(counts: dict[str, int], files: list[str]) -> str | None:
    """A phrase for a turn from its tool calls, e.g. ``Ran 3 commands · edited a.c``."""

    parts: list[str] = []
    if counts["commands"]:
        parts.append(f"Ran {_plural(counts['commands'], 'command')}")
    if counts["edits"] or files:
        if len(files) == 1:
            parts.append(f"edited {_basename(files[0])}")
        else:
            parts.append(f"edited {_plural(max(counts['edits'], len(files)), 'file')}")
    others = counts["tools"] - counts["commands"] - counts["edits"]
    if others > 0:
        parts.append(f"used {_plural(others, 'tool')}")
    if not parts:
        return None
    text = " · ".join(parts)
    return _bounded(text[0].upper() + text[1:])


def _turn_line(
    header: ChatTurnHeader,
    answer: str | None,
    counts: dict[str, int],
    files: list[str],
    prose: str | None = None,
) -> str:
    if header.status == ChatTurnStatus.FAILED.value:
        return _bounded(header.error or "") or "The response failed"
    # The final answer first; prose streamed before tool results only when
    # the turn has neither an answer nor tool calls yet.
    line = first_sentence(answer) or tool_phrase(counts, files) or first_sentence(prose)
    if line:
        return line
    if header.status == ChatTurnStatus.CANCELLED.value:
        return "Stopped before answering"
    if header.status in _WORKING_TEXT:
        return _WORKING_TEXT[header.status]
    return "Finished without an answer"


def _aware(value: datetime) -> datetime:
    """SQLite returns stored instants without their (UTC) offset."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def chat_brief_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/butler/sessions/{session_id}/brief", response_model=None)
    async def read_chat_brief(
        session_id: str,
        request: Request,
        response: Response,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=DEFAULT_TURNS, ge=1, le=MAX_TURNS),
    ) -> Any:
        """One chat's brief: state, goal, attention, a page of turns and files.

        ``turns`` are oldest first and end with the newest turn below
        ``before`` (a 1-based turn index); ``older_before`` pages further
        back. Butler's own conversations, side chats and archived chats or
        projects are not found.
        """

        response.headers["Cache-Control"] = "no-store"
        async with store.database.session() as database:
            row = await database.get(EntityRow, session_id)
            if row is None or row.kind != ChatSession.entity_kind:
                raise NotFoundError(f"chat session not found: {session_id}")
            session = decode_stored(ChatSession, row.payload)
            metadata = session.metadata
            project_row = await database.get(EntityRow, session.engagement_id)
            if (
                metadata.get("control_assistant")
                or metadata.get("butler_thread")
                or metadata.get("temporary_assistant")
                or metadata.get("side_chat")
                or metadata.get("archived_at")
                or project_row is None
                or project_row.kind != Engagement.entity_kind
                or (project_row.payload or {}).get("status")
                == EngagementStatus.ARCHIVED.value
            ):
                raise NotFoundError(f"chat session not found: {session_id}")
            project_name = str((project_row.payload or {}).get("name") or "")
            page, first_index, total = await session_turn_page(
                database, session.id, before=before, limit=limit
            )
            pending = visible_pending(
                await pending_session_turn_headers(
                    database,
                    session.id,
                    session.engagement_id,
                    statuses=UNFINISHED_TURN_STATUSES,
                )
            )
            latest = await latest_turn_header(database, session.id)
            turn_ids = [header.id for header in page]
            turn_fields = await _turn_fields(database, session.id, turn_ids)
            answers = await _answers(
                database,
                [header.final_message_id for header in page if header.final_message_id],
            )
            calls = await _tool_calls(database, session.id, turn_ids)
            observed = await _observed_files(database, session.id, turn_ids)
            recent_files = await _recent_files(database, session.id)
            plan_reports = await _plan_reports(database, session.id)
            cost = await _session_cost(database, session.id)
            subagents = int(
                await database.scalar(
                    select(func.count())
                    .select_from(EntityRow)
                    .where(
                        EntityRow.kind == ChatSubagent.entity_kind,
                        EntityRow.engagement_id == session.engagement_id,
                        EntityRow.payload["parent_session_id"].as_string()
                        == session.id,
                    )
                )
                or 0
            )

        header = pending[0] if pending else None
        if header is not None:
            state = await session_state(store, session, header)
        elif (
            latest is not None
            and latest.status == ChatTurnStatus.FAILED.value
            and latest.updated_at >= utc_now() - FAILED_WINDOW
        ):
            state, header = "failed", latest
        else:
            state = "idle"
        entry = (session, project_name, state, header)
        approvals = await _pending_approvals(store, [entry])
        questions = await _pending_questions(store, [session.id])
        goals = await _goals(store, [session.id])
        item = await _item(store, *entry, approvals, questions, goals)
        attention = None
        if item["attention"] is not None:
            approval = approvals.get(session.id)
            attention = {
                "kind": item["attention"]["kind"],
                "action_id": item["attention"]["action_id"],
                "summary": item["attention"]["summary"],
                "command": (
                    approval_command(approval)
                    if item["attention"]["kind"] == "approval" and approval
                    else None
                ),
            }
        goal = goals.get(session.id)

        turns: list[dict[str, Any]] = []
        carried: int | None = None
        report_index = 0
        for offset, turn in enumerate(page):
            fields = turn_fields.get(turn.id, {})
            # The plan step a turn worked on: the last one reported in it, else
            # the one in effect when it started.
            while (
                report_index < len(plan_reports)
                and plan_reports[report_index][0] < turn.created_at
            ):
                carried = plan_reports[report_index][2]
                report_index += 1
            in_turn = [item[2] for item in plan_reports if item[1] == turn.id]
            if in_turn:
                carried = in_turn[-1]
            goal_turn = goal is not None and fields.get("goal_id") == goal.id
            counts = {"commands": 0, "edits": 0, "approvals": 0, "tools": 0}
            files: list[str] = []
            for call in calls.get(turn.id, []):
                counts["tools"] += 1
                kind = _tool_kind(call["name"])
                if kind == "command":
                    counts["commands"] += 1
                elif kind == "edit":
                    counts["edits"] += 1
                    if call["path"] and call["path"] not in files:
                        files.append(call["path"])
                if call["approval_id"]:
                    counts["approvals"] += 1
            for path in observed.get(turn.id, []):
                if path not in files:
                    files.append(path)
            files = files[:MAX_TURN_FILES]
            answer = answers.get(turn.final_message_id or "")
            turns.append(
                {
                    "turn_id": turn.id,
                    "index": first_index + offset,
                    "status": turn.status,
                    "started_at": turn.created_at,
                    "finished_at": (
                        turn.updated_at
                        if turn.status
                        in {
                            ChatTurnStatus.COMPLETE.value,
                            ChatTurnStatus.FAILED.value,
                            ChatTurnStatus.CANCELLED.value,
                        }
                        else None
                    ),
                    "line": _turn_line(
                        turn, answer, counts, files, fields.get("content")
                    ),
                    "plan_index": (
                        carried
                        if goal_turn
                        and carried is not None
                        and goal is not None
                        and carried < len(goal.plan)
                        else None
                    ),
                    "counts": counts,
                    "files": files,
                }
            )

        body = {
            "session_id": session.id,
            "title": session.title,
            "project": {"id": session.engagement_id, "name": project_name},
            "state": state,
            "backend": session.backend.value,
            "model": session.model,
            "started_at": session.created_at,
            "updated_at": session.updated_at,
            "turn_count": total,
            "cost_usd": cost,
            "subagent_count": subagents,
            "parent_session_id": session.parent_session_id,
            "goal": (
                {
                    "objective": goal.objective,
                    "completion_criteria": goal.completion_criteria,
                    "status": goal.status.value,
                    "plan": goal.plan,
                    "active_plan_index": goal.active_plan_index,
                    "turns_used": goal.current_step,
                    "blocked_reason": goal.blocked_reason,
                }
                if goal
                else None
            ),
            "attention": attention,
            "turns": turns,
            "older_before": first_index if first_index > 1 and page else None,
            "files": recent_files,
        }
        if unchanged := conditional(
            request, response, content_etag("chat-brief", body)
        ):
            return unchanged
        return body

    return router


async def _turn_fields(
    database: Any, session_id: str, turn_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """The goal and the start of the streamed prose of each turn on the page."""

    if not turn_ids:
        return {}
    rows = await database.execute(
        select(
            EntityRow.id,
            EntityRow.payload["goal_id"].as_string(),
            func.substr(EntityRow.payload["content"].as_string(), 1, _ANSWER_PREFIX),
        ).where(
            EntityRow.kind == ChatTurn.entity_kind,
            EntityRow.chat_session_id == session_id,
            EntityRow.id.in_(turn_ids),
        )
    )
    return {
        turn_id: {"goal_id": goal_id, "content": content}
        for turn_id, goal_id, content in rows
    }


async def _answers(database: Any, message_ids: list[str]) -> dict[str, str]:
    if not message_ids:
        return {}
    rows = await database.execute(
        select(
            EntityRow.id,
            func.substr(EntityRow.payload["content"].as_string(), 1, _ANSWER_PREFIX),
        ).where(
            EntityRow.kind == ChatMessage.entity_kind,
            EntityRow.id.in_(message_ids),
        )
    )
    return {message_id: content for message_id, content in rows if content}


async def _tool_calls(
    database: Any, session_id: str, turn_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Each page turn's tool calls: name, approval and edited path only."""

    if not turn_ids:
        return {}
    arguments = EntityRow.payload["arguments"]
    rows = await database.execute(
        select(
            EntityRow.payload["chat_turn_id"].as_string(),
            EntityRow.payload["tool_name"].as_string(),
            EntityRow.payload["vendor_tool_name"].as_string(),
            EntityRow.payload["approval_id"].as_string(),
            *(func.substr(arguments[key].as_string(), 1, 1_000) for key in _PATH_KEYS),
        )
        .where(
            EntityRow.kind == ToolCall.entity_kind,
            EntityRow.chat_session_id == session_id,
            EntityRow.payload["chat_turn_id"].as_string().in_(turn_ids),
        )
        .order_by(EntityRow.created_at, EntityRow.id)
        .limit(MAX_TOOL_CALL_ROWS)
    )
    calls: dict[str, list[dict[str, Any]]] = {}
    for turn_id, tool_name, vendor_name, approval_id, *paths in rows:
        calls.setdefault(turn_id, []).append(
            {
                "name": vendor_name or tool_name or "",
                "approval_id": approval_id,
                "path": next((path for path in paths if path), None),
            }
        )
    return calls


def _mutation_paths(mutations: Any) -> list[str]:
    if not isinstance(mutations, list):
        return []
    return [
        item["path"]
        for item in mutations
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]


async def _observed_files(
    database: Any, session_id: str, turn_ids: list[str]
) -> dict[str, list[str]]:
    """Paths Core's workspace observations saw each page turn change."""

    if not turn_ids:
        return {}
    rows = await database.execute(
        select(
            EntityRow.payload["chat_turn_id"].as_string(),
            EntityRow.payload["mutations"],
        )
        .where(
            EntityRow.kind == WorkspaceProvenanceObservation.entity_kind,
            EntityRow.chat_session_id == session_id,
            EntityRow.payload["chat_turn_id"].as_string().in_(turn_ids),
            EntityRow.payload["status"].as_string() == "complete",
        )
        .order_by(EntityRow.created_at, EntityRow.id)
        .limit(MAX_OBSERVATION_ROWS)
    )
    observed: dict[str, list[str]] = {}
    for turn_id, mutations in rows:
        paths = observed.setdefault(turn_id, [])
        for path in _mutation_paths(mutations):
            if path not in paths and len(paths) < MAX_TURN_FILES:
                paths.append(path)
    return observed


async def _recent_files(database: Any, session_id: str) -> list[dict[str, Any]]:
    """The chat's most recently changed files, newest first, at most 50.

    Workspace observations are authoritative where they exist; edit tool
    calls name the files of runtimes that Core did not observe.
    """

    latest: dict[str, tuple[datetime, str]] = {}

    def note(path: str | None, turn_id: str | None, at: datetime) -> None:
        if not path or not turn_id:
            return
        at = _aware(at)
        known = latest.get(path)
        if known is None or known[0] < at:
            latest[path] = (at, turn_id)

    observations = await database.execute(
        select(
            EntityRow.payload["chat_turn_id"].as_string(),
            EntityRow.payload["mutations"],
            EntityRow.updated_at,
        )
        .where(
            EntityRow.kind == WorkspaceProvenanceObservation.entity_kind,
            EntityRow.chat_session_id == session_id,
            EntityRow.payload["status"].as_string() == "complete",
        )
        .order_by(EntityRow.updated_at.desc(), EntityRow.id.desc())
        .limit(MAX_OBSERVATION_ROWS)
    )
    for turn_id, mutations, at in observations:
        for path in _mutation_paths(mutations):
            note(path, turn_id, at)
    arguments = EntityRow.payload["arguments"]
    edits = await database.execute(
        select(
            EntityRow.payload["chat_turn_id"].as_string(),
            EntityRow.payload["tool_name"].as_string(),
            EntityRow.payload["vendor_tool_name"].as_string(),
            EntityRow.updated_at,
            *(func.substr(arguments[key].as_string(), 1, 1_000) for key in _PATH_KEYS),
        )
        .where(
            EntityRow.kind == ToolCall.entity_kind,
            EntityRow.chat_session_id == session_id,
        )
        .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
        .limit(MAX_SESSION_EDIT_ROWS)
    )
    for turn_id, tool_name, vendor_name, at, *paths in edits:
        if _tool_kind(vendor_name or tool_name or "") == "edit":
            note(next((path for path in paths if path), None), turn_id, at)
    ordered = sorted(latest.items(), key=lambda item: item[1][0], reverse=True)
    return [
        {"path": path, "last_turn_id": turn_id, "last_changed_at": at}
        for path, (at, turn_id) in ordered[:MAX_FILES]
    ]


async def _plan_reports(
    database: Any, session_id: str
) -> list[tuple[datetime, str, int]]:
    """Plan steps the chat's goal reported, as (reported, turn, index), oldest first."""

    plan_index = EntityRow.payload["details"]["plan_index"].as_integer()
    rows = await database.execute(
        select(
            EntityRow.created_at,
            EntityRow.payload["turn_id"].as_string(),
            plan_index,
        )
        .where(
            EntityRow.kind == ButlerAgentReport.entity_kind,
            EntityRow.chat_session_id == session_id,
            EntityRow.payload["report_type"].as_string() == "progress",
            plan_index.is_not(None),
        )
        .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
        .limit(MAX_REPORT_ROWS)
    )
    return sorted(
        (
            (_aware(created_at), str(turn_id), int(index))
            for created_at, turn_id, index in rows
            if turn_id and index is not None
        ),
        key=lambda item: item[0],
    )


async def _session_cost(database: Any, session_id: str) -> float | None:
    """What the chat's turns billed in USD, when any route reported it."""

    value = await database.scalar(
        select(func.sum(EntityRow.payload["usage"]["cost_usd"].as_float())).where(
            EntityRow.kind == ChatTurn.entity_kind,
            EntityRow.chat_session_id == session_id,
        )
    )
    return round(float(value), 6) if value is not None else None
