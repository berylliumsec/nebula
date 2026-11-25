"""Release conditions on goal supervisor notes.

A Butler note that told a goal to stop work ("operator gate, make no tool
calls") outranked every later instruction and kept the goal idle for hours
after the operator had acted, because nothing ever took the note back. A
note may now carry a machine-checkable ``release_condition``:

* ``until_time``: ``{"kind": "until_time", "at": "<ISO 8601>"}``
* ``until_path_exists``: ``{"kind": "until_path_exists", "path": "/abs"}``,
  the path under an allowed root (``NEBULA_BUTLER_NOTE_RELEASE_ROOTS`` and
  the supervisor policy's gate and evidence roots)
* ``until_work_item``: ``{"kind": "until_work_item", "item_id": ...}`` with
  any of ``stage`` (the item is at that stage), ``status`` (the item has
  that status), ``closed: true`` (status ``done``) or ``ruling: true`` (an
  operator-authored Work update on the item after the note); any one
  satisfies it
* ``until_goal_status``: ``{"kind": "until_goal_status", "statuses": [...]}``
  for this goal, or another goal of the project by ``goal_id``
* ``until_note_superseded``: ``{"kind": "until_note_superseded"}``, a later
  note on the goal from the same author

Conditions are validated when the note is written and kept in the goal's
metadata under ``NOTE_RELEASES_KEY``. Core evaluates them whenever it
assembles a goal turn's notes (``retire_released_notes``) and on Butler's
periodic tick; a satisfied note is marked retired with its reason and time,
never deleted, and stops being delivered (``chat_goal_addenda``). Its
retirement is recorded as a guidance change, so a running turn hears it.

A Butler note whose text gates work (stop, halt, do not proceed, wait for
the operator, make no tool calls) must carry a release condition; without
one it is refused (``require_release_for_gating``). Operator notes are not
checked.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .butler_supervisor_policy import SupervisorPolicy, load_policy, write_audit
from .chat_goal_addenda import (
    NOTE_RELEASES_KEY,
    describe_note_release,
    guidance_change_fields,
)
from .chat_goals import GoalNoteCreate
from .diagnostics import record_caught_exception
from .domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    RiskClass,
    WorkItem,
    WorkUpdate,
    utc_now,
)
from .storage import NebulaStore, NotFoundError, revise_latest
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

RELEASE_ROOTS_ENV = "NEBULA_BUTLER_NOTE_RELEASE_ROOTS"
POST_TOOL_NAME = "nebula.supervisor.post_goal_note"
AUDIT_TOOL = "goal_note_release"

CONDITION_KINDS = (
    "until_time",
    "until_path_exists",
    "until_work_item",
    "until_goal_status",
    "until_note_superseded",
)
_WORK_STATUSES = ("backlog", "ready", "in_progress", "blocked", "review", "done")
_CONDITION_BYTES = 2_000
_TERMINAL_GOAL = {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}

# Language that tells an agent to stop working. Uppercase STOP/HALT are
# matched as written; the phrases in any case.
_GATING_UPPER = re.compile(r"\b(STOP|HALT|FREEZE)\b")
_GATING_PHRASES = re.compile(
    r"\b("
    r"stop (all |any |the )?(work|working|activity|tool use|everything)|"
    r"halt (all |any )?(work|activity|tool)|"
    r"(do not|don'?t|must not) (proceed|continue|resume|start|run|act)|"
    r"no further (work|action|tool calls?)|"
    r"(wait|waiting) (for|on) (the )?operator|"
    r"until the operator|operator gate|gated on (the )?operator|"
    r"make no tool calls?|no tool calls?|"
    r"(do not|don'?t) (make|use|call|run) (any )?tools?|"
    r"stand down|cease (all )?(work|activity)|pause all work"
    r")\b",
    re.IGNORECASE,
)


class NoteReleaseError(ValueError):
    """A note or release condition that Core refuses to save."""


def is_gating_text(text: str) -> bool:
    """Whether a note tells the agent to stop or wait instead of working."""

    return bool(_GATING_UPPER.search(text) or _GATING_PHRASES.search(text))


def require_release_for_gating(
    text: str, author: str, release_condition: Mapping[str, Any] | None
) -> None:
    """Refuse a Butler note that gates work without a release condition."""

    if author == "operator" or release_condition:
        return
    if is_gating_text(text):
        raise NoteReleaseError(
            "Butler notes that stop or gate work (stop, halt, do not proceed, "
            "wait for the operator, make no tool calls) need a release_condition "
            "so Core can lift them once the condition holds: one of "
            + ", ".join(CONDITION_KINDS)
            + ". Without one the note outranks later guidance indefinitely."
        )


def release_roots(policy: SupervisorPolicy | None = None) -> list[str]:
    """Absolute roots an ``until_path_exists`` condition may watch."""

    raw = os.environ.get(RELEASE_ROOTS_ENV, "")
    roots = [
        item.strip()
        for item in re.split(rf"[,{re.escape(os.pathsep)}]", raw)
        if item.strip()
    ]
    if policy is None:
        try:
            policy = load_policy()
        except (
            ValueError
        ):  # diagnostic-expected: a malformed policy only drops its roots
            policy = None
    if policy is not None:
        roots += [*policy.gate_roots, *policy.evidence_roots]
    return [os.path.realpath(item) for item in roots if os.path.isabs(item)]


def _contained_path(path: str, roots: Iterable[str]) -> str | None:
    real = os.path.realpath(path)
    for root in roots:
        if Path(real).is_relative_to(root):
            return real
    return None


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NoteReleaseError("until_time needs 'at' as an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise NoteReleaseError(f"until_time 'at' is not ISO 8601: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _only(condition: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(condition) - allowed - {"kind"}
    if extra:
        raise NoteReleaseError(
            f"{condition['kind']} does not take {sorted(extra)}; it takes {sorted(allowed)}"
        )


async def validate_release_condition(
    store: NebulaStore,
    goal: ChatGoal,
    condition: Any,
    *,
    now: datetime | None = None,
    roots: list[str] | None = None,
) -> dict[str, Any]:
    """Return the normalized condition, or raise ``NoteReleaseError``."""

    if not isinstance(condition, Mapping):
        raise NoteReleaseError("release_condition must be an object with a 'kind'")
    if len(json.dumps(condition, default=str)) > _CONDITION_BYTES:
        raise NoteReleaseError("release_condition is too large")
    kind = condition.get("kind")
    if kind not in CONDITION_KINDS:
        raise NoteReleaseError(
            f"release_condition kind must be one of {list(CONDITION_KINDS)}"
        )
    now = now or utc_now()
    if kind == "until_time":
        _only(condition, {"at"})
        at = _parse_time(condition.get("at"))
        if at <= now:
            raise NoteReleaseError("until_time is already past; choose a later time")
        return {"kind": kind, "at": at.isoformat()}
    if kind == "until_path_exists":
        _only(condition, {"path"})
        path = condition.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise NoteReleaseError("until_path_exists needs an absolute 'path'")
        allowed = roots if roots is not None else release_roots()
        if not allowed:
            raise NoteReleaseError(
                f"no release roots are configured; set {RELEASE_ROOTS_ENV}"
            )
        if _contained_path(path, allowed) is None:
            raise NoteReleaseError(
                "until_path_exists path must be under an allowed root "
                f"({RELEASE_ROOTS_ENV} or the supervisor gate/evidence roots)"
            )
        return {"kind": kind, "path": os.path.normpath(path)}
    if kind == "until_work_item":
        _only(condition, {"item_id", "stage", "status", "closed", "ruling"})
        item_id = condition.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise NoteReleaseError("until_work_item needs 'item_id'")
        try:
            item = await store.get(WorkItem, item_id)
        except NotFoundError as exc:
            raise NoteReleaseError(f"Work item not found: {item_id}") from exc
        if item.engagement_id != goal.engagement_id:
            raise NoteReleaseError("the Work item must belong to the goal's project")
        result: dict[str, Any] = {"kind": kind, "item_id": item_id}
        stage = condition.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or not stage.strip():
                raise NoteReleaseError("until_work_item 'stage' must be a stage key")
            result["stage"] = stage.strip()
        status = condition.get("status")
        if status is not None:
            if status not in _WORK_STATUSES:
                raise NoteReleaseError(
                    f"until_work_item 'status' must be one of {list(_WORK_STATUSES)}"
                )
            result["status"] = status
        for flag in ("closed", "ruling"):
            value = condition.get(flag)
            if value is not None and not isinstance(value, bool):
                raise NoteReleaseError(
                    f"until_work_item '{flag}' must be true or false"
                )
            if value:
                result[flag] = True
        if len(result) == 2:
            raise NoteReleaseError(
                "until_work_item needs at least one of stage, status, closed or ruling"
            )
        return result
    if kind == "until_goal_status":
        _only(condition, {"statuses", "goal_id"})
        statuses = condition.get("statuses")
        if isinstance(statuses, str):
            statuses = [statuses]
        valid = {item.value for item in ChatGoalStatus}
        if (
            not isinstance(statuses, list)
            or not statuses
            or any(item not in valid for item in statuses)
        ):
            raise NoteReleaseError(
                f"until_goal_status needs 'statuses' from {sorted(valid)}"
            )
        target_id = condition.get("goal_id") or goal.id
        if not isinstance(target_id, str):
            raise NoteReleaseError("until_goal_status 'goal_id' must be a goal id")
        if target_id != goal.id:
            try:
                target = await store.get(ChatGoal, target_id)
            except NotFoundError as exc:
                raise NoteReleaseError(f"goal not found: {target_id}") from exc
            if target.engagement_id != goal.engagement_id:
                raise NoteReleaseError("the watched goal must be in the same project")
        return {"kind": kind, "goal_id": target_id, "statuses": sorted(set(statuses))}
    _only(condition, set())
    return {"kind": kind}


async def evaluate_release(
    store: NebulaStore,
    goal: ChatGoal,
    note_id: str,
    condition: Mapping[str, Any],
    *,
    now: datetime | None = None,
    roots: list[str] | None = None,
) -> str | None:
    """The reason the condition holds now, or None while it does not."""

    note = next((item for item in goal.supervisor_notes if item.id == note_id), None)
    if note is None:
        return None
    now = now or utc_now()
    kind = condition.get("kind")
    if kind == "until_time":
        at = _parse_time(condition.get("at"))
        return f"time {at.isoformat()} reached" if now >= at else None
    if kind == "until_path_exists":
        path = str(condition.get("path", ""))
        allowed = roots if roots is not None else release_roots()
        real = _contained_path(path, allowed)
        if real is not None and os.path.exists(real):
            return f"path {path} exists"
        return None
    if kind == "until_work_item":
        item_id = str(condition.get("item_id"))
        try:
            item = await store.get(WorkItem, item_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: a deleted item no longer gates the goal
            return f"Work item {item_id} was deleted"
        if condition.get("stage") and item.stage == condition["stage"]:
            return f"Work item {item_id} reached stage {item.stage}"
        if condition.get("status") and item.status == condition["status"]:
            return f"Work item {item_id} has status {item.status}"
        if condition.get("closed") and item.status == "done":
            return f"Work item {item_id} is closed"
        if condition.get("ruling"):
            updates = await store.find_entities(
                WorkUpdate,
                {"item_id": item_id},
                engagement_id=item.engagement_id,
                newest_first=True,
                limit=50,
            )
            for update in updates:
                if (
                    update.actor_kind == "operator"
                    and update.created_at > note.created_at
                ):
                    return f"operator ruled on Work item {item_id}"
        return None
    if kind == "until_goal_status":
        target_id = str(condition.get("goal_id") or goal.id)
        if target_id == goal.id:
            target = goal
        else:
            try:
                target = await store.get(ChatGoal, target_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: a deleted goal no longer gates this one
                return f"goal {target_id} was deleted"
        statuses = condition.get("statuses") or []
        if target.status.value in statuses:
            return f"goal {target_id} is {target.status.value}"
        return None
    if kind == "until_note_superseded":
        for later in goal.supervisor_notes:
            if (
                later.id != note.id
                and later.author == note.author
                and later.created_at > note.created_at
            ):
                return f"superseded by a later {note.author} note"
        return None
    return None


async def retire_released_notes(
    store: NebulaStore,
    goal: ChatGoal,
    *,
    now: datetime | None = None,
    roots: list[str] | None = None,
) -> ChatGoal:
    """Retire every note whose release condition holds; return the goal.

    Never raises: turn preparation calls it, and a failed evaluation leaves
    the note delivered, recorded for diagnosis.
    """

    try:
        releases = goal.metadata.get(NOTE_RELEASES_KEY)
        if not isinstance(releases, dict) or goal.status in _TERMINAL_GOAL:
            return goal
        now = now or utc_now()
        satisfied: dict[str, str] = {}
        for note_id, entry in releases.items():
            if not isinstance(entry, dict) or entry.get("retired_at"):
                continue
            condition = entry.get("condition")
            if not isinstance(condition, dict):
                continue
            try:
                reason = await evaluate_release(
                    store, goal, note_id, condition, now=now, roots=roots
                )
            except Exception as exc:
                record_caught_exception(
                    "butler",
                    "butler.note_release.evaluate_failed",
                    "A supervisor note's release condition could not be evaluated; the note stays.",
                    exc,
                    stage="note-release",
                )
                continue
            if reason:
                satisfied[note_id] = reason
        if not satisfied:
            return goal
        stamp = now.isoformat()

        def plan(current: ChatGoal) -> dict[str, Any] | None:
            saved = current.metadata.get(NOTE_RELEASES_KEY)
            if not isinstance(saved, dict):
                return None
            texts = {note.id: note.text for note in current.supervisor_notes}
            updated = dict(saved)
            summaries: list[str] = []
            for note_id, reason in satisfied.items():
                entry = updated.get(note_id)
                if (
                    not isinstance(entry, dict)
                    or entry.get("retired_at")
                    or note_id not in texts
                ):
                    continue
                updated[note_id] = {
                    **entry,
                    "retired_at": stamp,
                    "retired_reason": reason,
                }
                summaries.append(describe_note_release(texts[note_id], reason))
            if not summaries:
                return None
            return {
                "metadata": {**current.metadata, NOTE_RELEASES_KEY: updated},
                **guidance_change_fields(
                    current, "\n\n".join(summaries), author="core"
                ),
            }

        return await revise_latest(store, ChatGoal, goal.id, plan)
    except Exception as exc:
        record_caught_exception(
            "butler",
            "butler.note_release.retire_failed",
            "Core could not retire released supervisor notes; they stay delivered until the next check.",
            exc,
            stage="note-release",
        )
        return goal


async def sweep_note_releases(
    store: NebulaStore, project_ids: Iterable[str], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Retire released notes on every non-terminal goal of the projects."""

    retired: list[dict[str, Any]] = []
    for project_id in project_ids:
        async for goal in store.iter_readable_entities(
            ChatGoal,
            {
                "status": [
                    status.value
                    for status in ChatGoalStatus
                    if status not in _TERMINAL_GOAL
                ]
            },
            engagement_id=project_id,
        ):
            if not isinstance(goal.metadata.get(NOTE_RELEASES_KEY), dict):
                continue
            before = _retired(goal)
            after = _retired(await retire_released_notes(store, goal, now=now))
            for note_id in sorted(after - before):
                retired.append(
                    {"project_id": project_id, "goal_id": goal.id, "note_id": note_id}
                )
    return retired


def _retired(goal: ChatGoal) -> set[str]:
    releases = goal.metadata.get(NOTE_RELEASES_KEY)
    if not isinstance(releases, dict):
        return set()
    return {
        key
        for key, entry in releases.items()
        if isinstance(entry, dict) and entry.get("retired_at")
    }


async def attach_release_condition(
    store: NebulaStore,
    goal_id: str,
    note_id: str,
    condition: Mapping[str, Any],
    *,
    author: str,
) -> ChatGoal:
    """Record a validated condition for one note on the goal's newest revision."""

    stamp = utc_now().isoformat()

    def plan(current: ChatGoal) -> dict[str, Any]:
        if not any(note.id == note_id for note in current.supervisor_notes):
            raise NotFoundError(f"supervisor note not found: {note_id}")
        saved = current.metadata.get(NOTE_RELEASES_KEY)
        releases = dict(saved) if isinstance(saved, dict) else {}
        releases[note_id] = {
            "condition": dict(condition),
            "author": author,
            "set_at": stamp,
            "retired_at": None,
            "retired_reason": None,
        }
        return {"metadata": {**current.metadata, NOTE_RELEASES_KEY: releases}}

    return await revise_latest(store, ChatGoal, goal_id, plan)


async def post_goal_note(
    store: NebulaStore,
    goals: Any,
    session_id: str,
    text: str,
    *,
    author: str,
    release_condition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, append and condition one supervisor note.

    ``goals`` is a ``ChatGoalService``. The gating check runs before the
    note is saved; if its condition cannot be recorded the note is removed
    again, so a gating Butler note never stays unconditioned.
    """

    body = GoalNoteCreate.model_validate(
        {"text": text, "author": author, "release_condition": release_condition}
    )
    # ChatGoalService.add_note enforces the gating rule, validates the
    # condition and attaches it (removing the note again if that fails).
    result = await goals.add_note(session_id, body)
    saved = result.goal.metadata.get(NOTE_RELEASES_KEY)
    entry = saved.get(result.note_id) if isinstance(saved, dict) else None
    condition = entry.get("condition") if isinstance(entry, dict) else None
    return {
        "goal_id": result.goal.id,
        "session_id": session_id,
        "note_id": result.note_id,
        "delivery": result.delivery,
        "release_condition": condition,
    }


async def post_note_and_restart(
    store: NebulaStore,
    work: Any,
    session_id: str,
    text: str,
    *,
    author: str,
    release_condition: Mapping[str, Any] | None = None,
    reason: str = "Restarted so a new supervisor note takes effect.",
    policy: SupervisorPolicy | None = None,
) -> dict[str, Any]:
    """Post a note, then cancel and resume the goal's current turn.

    The restart reuses ``butler_stuck_turns.recover_turn`` (identity guard,
    per-goal backoff, Core's own cancel and resume). A goal with no turn in
    flight needs no restart: its next turn reads the note. A paused or
    blocked goal is left as it is.
    """

    from .butler_stuck_turns import recover_turn

    posted = await post_goal_note(
        store,
        work.goals,
        session_id,
        text,
        author=author,
        release_condition=release_condition,
    )
    goal = await store.get(ChatGoal, posted["goal_id"])
    if goal.status != ChatGoalStatus.RUNNING:
        return {
            **posted,
            "restart": {"outcome": "not_running", "status": goal.status.value},
        }
    pending = await work.chat.pending_turn(session_id)
    if pending is None:
        return {**posted, "restart": {"outcome": "no_turn_in_flight"}}
    receipt = await recover_turn(
        store,
        work,
        session_id=session_id,
        turn_id=pending.id,
        require_stuck=False,
        reason=reason,
        policy=policy,
    )
    return {**posted, "restart": receipt}


_CONDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(CONDITION_KINDS)},
        "at": {"type": "string", "maxLength": 64},
        "path": {"type": "string", "maxLength": 1024},
        "item_id": {"type": "string", "maxLength": 200},
        "stage": {"type": "string", "maxLength": 100},
        "status": {"type": "string", "enum": list(_WORK_STATUSES)},
        "closed": {"type": "boolean"},
        "ruling": {"type": "boolean"},
        "goal_id": {"type": "string", "maxLength": 200},
        "statuses": {
            "type": "array",
            "items": {"type": "string", "enum": [s.value for s in ChatGoalStatus]},
            "maxItems": 8,
        },
    },
    "required": ["kind"],
    "additionalProperties": False,
}

_POST_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "text": {"type": "string", "minLength": 1, "maxLength": 8_000},
        "release_condition": _CONDITION_SCHEMA,
        "restart_turn": {"type": "boolean", "default": False},
    },
    "required": ["session_id", "text"],
    "additionalProperties": False,
}


class ButlerNoteRelease:
    """Butler's note-with-release-condition tool."""

    def __init__(
        self,
        store: NebulaStore,
        work: Any,
        project_id: str,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
    ) -> None:
        self.store = store
        self.work = work
        self.project_id = project_id
        self.policy_loader = policy_loader

    async def _session(self, session_id: str, policy: SupervisorPolicy) -> None:
        session = await self.store.get(ChatSession, session_id)
        allowed = {self.project_id, *policy.hygiene_project_ids}
        if session.engagement_id not in allowed:
            raise PermissionError(
                "choose a goal chat in this project or a supervised hygiene project"
            )

    async def post(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy_loader()
        policy.require_enabled()
        session_id = str(arguments["session_id"])
        await self._session(session_id, policy)
        condition = arguments.get("release_condition")
        audit = {
            "tool": AUDIT_TOOL,
            "project_id": self.project_id,
            "session_id": session_id,
            "release_condition": condition,
            "restart_turn": bool(arguments.get("restart_turn")),
        }
        try:
            if arguments.get("restart_turn"):
                result = await post_note_and_restart(
                    self.store,
                    self.work,
                    session_id,
                    arguments["text"],
                    author="butler",
                    release_condition=condition,
                    policy=policy,
                )
            else:
                result = await post_goal_note(
                    self.store,
                    self.work.goals,
                    session_id,
                    arguments["text"],
                    author="butler",
                    release_condition=condition,
                )
        except NoteReleaseError as exc:
            write_audit(policy, {**audit, "allowed": False, "detail": str(exc)})
            raise
        write_audit(
            policy,
            {
                **audit,
                "allowed": True,
                "goal_id": result["goal_id"],
                "note_id": result["note_id"],
            },
        )
        return result

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=POST_TOOL_NAME,
                    description=(
                        "Add a Butler supervisor note to a goal with an optional "
                        "machine-checked release_condition (until_time, "
                        "until_path_exists, until_work_item, until_goal_status, "
                        "until_note_superseded). Core retires the note, without "
                        "deleting it, once the condition holds, and stops delivering "
                        "it. A note that stops or gates work (stop, halt, wait for "
                        "the operator, make no tool calls) is refused without a "
                        "release_condition. restart_turn cancels and resumes the "
                        "goal's running turn so the note applies now."
                    ),
                    input_schema=_POST_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.post,
            )
        ]
