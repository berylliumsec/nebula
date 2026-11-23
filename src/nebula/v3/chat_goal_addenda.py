"""Supervisor guidance for a running goal, and its delivery into the turn.

An operator supervising a long goal edits its objective, criteria or plan, or
adds a supervisor note. A goal's turn bakes the goal into its instructions
once, at preparation, so without this an edit waited for the next turn.

Core keeps a ``guidance_revision`` on the goal that only guidance edits bump
and a bounded log of what each edit changed. A running provider turn records
the guidance revision it was prepared with; before each routing call Core
compares it with the goal and delivers what changed as the result of a
``goal.read`` step it adds (``GoalAddenda.routing_delivery``). A running
harness turn is steered with the same text instead; a harness that cannot be
steered (Grok) reads the edited goal at its next turn.

Delivery is built from *sources*. Each source looks at the goal, the turn and
what the turn has already been told (``seen``), and returns an addendum with
the marker it wants recorded once the addendum is delivered. A source must be
monotonic: a later marker never makes it due again for what it already said. The marker is
saved on the delivered step itself, so a crash before the step is saved
delivers again and a saved step is never delivered twice. A later source,
such as a check-in reminder, plugs in by adding one more callable to
``GoalAddenda.sources`` with its own marker key.

``CheckinReminder`` is such a source. A running goal whose agent has posted
no Work check-in for a while (by default 30 minutes, or 40 tool calls in the
turn) is reminded once, in the turn, to post one. It never refuses a tool and
never escalates to Butler: the Butler escalation for overdue progress was
removed because it opened a recovery chat for every quiet goal.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Collection, Mapping, Sequence

from .domain import (
    GOAL_GUIDANCE_CHANGE_LIMIT,
    ButlerGoalWatch,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    GoalGuidanceChange,
    WorkItem,
    WorkUpdate,
    utc_now,
)
from .storage import NebulaStore, NotFoundError, revise_latest
from .tool_results import model_result_bytes

GOAL_READ_TOOL_NAME = "goal.read"

# What a turn records, in its request snapshot and on each delivered step,
# about the addenda it has heard.
SEEN_KEY = "goal_addenda_seen"
GUIDANCE_MARKER = "guidance_revision"

# The model-delivery bound for one Core-added result is 8 KiB; leave room
# for the result's framing.
ADDENDUM_CHARACTERS = 6_000
# Each change is kept on the goal, so its summary stays small; a note's
# text is read from the note itself when it is delivered.
CHANGE_SUMMARY_CHARACTERS = 3_000
READ_PAGE_CHARACTERS = 6_000
# Notes repeated with every turn's goal progress, newest first.
PROGRESS_NOTES_CHARACTERS = 12_000

ADDENDUM_HEADING = "Supervisor addendum since your last tool call"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _list_change(label: str, before: Sequence[str], after: Sequence[str]) -> str:
    if list(before) == list(after):
        return ""
    added = [item for item in after if item not in before]
    removed = [item for item in before if item not in after]
    lines: list[str] = []
    lines.extend(f"+ {item}" for item in added)
    lines.extend(f"- {item}" for item in removed)
    if not lines:
        return f"{label} reordered:\n" + "\n".join(
            f"{index}. {item}" for index, item in enumerate(after, 1)
        )
    return f"{label} changed:\n" + "\n".join(lines)


def _objective_change(before: str, after: str) -> str:
    if before == after:
        return ""
    if after.startswith(before):
        added = after[len(before) :].strip()
        if len(added) > CHANGE_SUMMARY_CHARACTERS * 2 // 3:
            return (
                f"Objective extended by {len(added)} characters. Call "
                f"{GOAL_READ_TOOL_NAME} for the full current objective."
            )
        return "Objective extended:\n" + added
    diff = [
        line
        for line in difflib.unified_diff(
            before.splitlines(), after.splitlines(), lineterm="", n=0
        )
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]
    text = "Objective changed:\n" + "\n".join(diff)
    if len(text) > CHANGE_SUMMARY_CHARACTERS * 2 // 3:
        return (
            f"Objective rewritten ({len(after)} characters). Call "
            f"{GOAL_READ_TOOL_NAME} for the full current objective."
        )
    return text


def describe_guidance_change(
    goal: ChatGoal,
    *,
    objective: str,
    completion_criteria: Sequence[str],
    plan: Sequence[str],
) -> str:
    """What an edit changes in the goal's guidance, or "" for nothing."""

    parts = [
        _objective_change(goal.objective, objective),
        _list_change(
            "Completion criteria", goal.completion_criteria, completion_criteria
        ),
        _list_change("Plan", goal.plan, plan),
    ]
    return _clip("\n\n".join(part for part in parts if part), CHANGE_SUMMARY_CHARACTERS)


def guidance_change_fields(
    goal: ChatGoal, summary: str, *, author: str, note_id: str | None = None
) -> dict[str, Any]:
    """The goal fields that record one guidance change."""

    revision = goal.guidance_revision + 1
    change = GoalGuidanceChange(
        guidance_revision=revision,
        summary=_clip(summary, CHANGE_SUMMARY_CHARACTERS),
        note_id=note_id,
        author=author,
        created_at=utc_now(),
    )
    return {
        "guidance_revision": revision,
        "guidance_changes": [
            *goal.guidance_changes,
            change,
        ][-GOAL_GUIDANCE_CHANGE_LIMIT:],
    }


def notes_progress(goal: ChatGoal) -> list[dict[str, Any]] | None:
    """The goal's supervisor notes for a turn's progress block, newest first
    and bounded; None when there are none."""

    if not goal.supervisor_notes:
        return None
    notes: list[dict[str, Any]] = []
    used = 0
    for note in reversed(goal.supervisor_notes):
        if used + len(note.text) > PROGRESS_NOTES_CHARACTERS and notes:
            break
        used += len(note.text)
        notes.append(
            {
                "added_at": note.created_at.isoformat(),
                "author": note.author,
                "text": note.text,
            }
        )
    omitted = len(goal.supervisor_notes) - len(notes)
    if omitted:
        notes.append(
            {
                "omitted_older_notes": omitted,
                "read_with": GOAL_READ_TOOL_NAME,
            }
        )
    return notes


def goal_guidance_text(goal: ChatGoal) -> str:
    """The goal's whole current guidance as one document ``goal.read`` pages."""

    sections = [
        f"Objective:\n{goal.objective}",
        "Completion criteria:\n"
        + "\n".join(f"- {item}" for item in goal.completion_criteria),
    ]
    if goal.plan:
        sections.append(
            "Plan:\n"
            + "\n".join(f"{index}. {item}" for index, item in enumerate(goal.plan, 1))
        )
    if goal.supervisor_notes:
        sections.append(
            "Supervisor notes (oldest first):\n"
            + "\n\n".join(
                f"[{note.created_at.isoformat()} · {note.author}]\n{note.text}"
                for note in goal.supervisor_notes
            )
        )
    return "\n\n".join(sections)


def read_goal_page(goal: ChatGoal, offset: int = 0) -> dict[str, Any]:
    """One bounded page of the goal's current guidance."""

    text = goal_guidance_text(goal)
    offset = max(0, min(offset, len(text)))
    size = READ_PAGE_CHARACTERS
    while True:
        chunk = text[offset : offset + size]
        output: dict[str, Any] = {
            "goal_id": goal.id,
            "status": goal.status.value,
            GUIDANCE_MARKER: goal.guidance_revision,
            "offset": offset,
            "text": chunk,
        }
        if offset + len(chunk) < len(text):
            output["next_offset"] = offset + len(chunk)
        if model_result_bytes(output) <= 7_600 or size <= 500:
            return output
        size = size * 3 // 4


@dataclass(frozen=True)
class GoalAddendum:
    """One thing a running goal turn must hear, and what to record once it has."""

    source: str
    text: str
    marker: Mapping[str, Any]


AddendumSource = Callable[
    [ChatGoal, ChatTurn, Mapping[str, Any]], Awaitable[GoalAddendum | None]
]


async def guidance_addendum(
    goal: ChatGoal, turn: ChatTurn, seen: Mapping[str, Any]
) -> GoalAddendum | None:
    """Objective, criteria, plan and note changes the turn has not heard."""

    del turn
    heard = seen.get(GUIDANCE_MARKER)
    # A turn prepared before this record existed heard nothing to compare.
    if not isinstance(heard, int) or heard >= goal.guidance_revision:
        return None
    changes = [
        change for change in goal.guidance_changes if change.guidance_revision > heard
    ]
    missing = (
        not changes or changes[0].guidance_revision > heard + 1
    )  # older changes fell out of the bounded log
    notes = {note.id: note.text for note in goal.supervisor_notes}
    lines = [
        f"{NOTE_ADDED}\n{notes[change.note_id]}"
        if change.note_id in notes
        else change.summary
        for change in changes
    ]
    if missing:
        lines.insert(
            0,
            "The goal changed more than Core can summarize here. Call "
            f"{GOAL_READ_TOOL_NAME} for the full current goal.",
        )
    return GoalAddendum(
        "guidance",
        "\n\n".join(lines),
        {GUIDANCE_MARKER: goal.guidance_revision},
    )


CHECKIN_SOURCE = "checkin"
CHECKIN_REMINDED_AT = "checkin_reminded_at"
CHECKIN_REMINDED_TOOL_CALLS = "checkin_reminded_tool_calls"
# The goal's last reminder, kept on the goal so that its next turn does not
# remind again within the same window.
CHECKIN_REMINDER_METADATA = "checkin_reminder"
CHECKIN_MINUTES_ENV = "NEBULA_GOAL_CHECKIN_REMINDER_MINUTES"
CHECKIN_TOOL_CALLS_ENV = "NEBULA_GOAL_CHECKIN_REMINDER_TOOL_CALLS"
DEFAULT_CHECKIN_MINUTES = 30
DEFAULT_CHECKIN_TOOL_CALLS = 40
MAX_CHECKIN_MINUTES = 24 * 60
MAX_CHECKIN_TOOL_CALLS = 10_000
NATIVE_CHECK_IN_TOOL = "work_check_in"
NATIVE_CREATE_TOOL = "work_create"
KNOWN_CHECK_IN_GOALS = 1_024


def _bounded_env(name: str, default: int, maximum: int) -> int:
    """An operator-set threshold, or the default when unset or invalid.

    0 turns that rule off; anything else is clamped to 1..maximum.
    """

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:  # diagnostic-expected: an invalid setting keeps the default
        return default
    return 0 if value <= 0 else min(value, maximum)


def checkin_reminder_thresholds() -> tuple[int, int]:
    """(minutes, tool calls) without a check-in before a goal is reminded."""

    return (
        _bounded_env(CHECKIN_MINUTES_ENV, DEFAULT_CHECKIN_MINUTES, MAX_CHECKIN_MINUTES),
        _bounded_env(
            CHECKIN_TOOL_CALLS_ENV, DEFAULT_CHECKIN_TOOL_CALLS, MAX_CHECKIN_TOOL_CALLS
        ),
    )


def _as_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:  # diagnostic-expected: an unreadable marker is ignored
            return None
    return None


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def calls_after_native_check_in(history: Sequence[Mapping[str, Any]]) -> int | None:
    """Execution tool calls a provider turn made after its last saved
    check-in, or None when it has not checked in."""

    calls: int | None = None
    for entry in history:
        if (
            entry.get("name") == NATIVE_CHECK_IN_TOOL
            and entry.get("status") == "complete"
        ):
            calls = 0
        elif calls is not None and entry.get("budget_class") == "execution":
            calls += 1
    return calls


@dataclass
class CheckinReminder:
    """Reminds a running goal's agent to post a Work check-in.

    Due when the goal has had no check-in for ``minutes`` (since its latest
    check-in, reminder or start), or when its turn made ``tool_calls`` tool
    calls since its latest check-in or reminder. A check-in is an agent update
    posted from the goal's conversation, an update to a Work item assigned to
    that conversation, or a progress report to Butler. Only a turn that has
    the Work check-in tool is reminded.

    Monotonic: the marker records when, and at which tool count, it reminded,
    and a later marker only moves both baselines on. The goal keeps its last
    reminder too, so the goal's next turn does not repeat it in the window.
    """

    store: NebulaStore
    # The provider turn's step history, for check-ins made in the turn. A
    # harness passes ``calls_after_check_in`` instead.
    history: Callable[[ChatTurn], Awaitable[list[dict[str, Any]]]] | None = None
    calls_after_check_in: Callable[[ChatTurn], Awaitable[int | None]] | None = None
    check_in_tool: str = NATIVE_CHECK_IN_TOOL
    create_tool: str = NATIVE_CREATE_TOOL
    # Whether the turn has the Work tools; by default, from the provider
    # turn's recorded tool names.
    offered: Callable[[ChatTurn], Awaitable[bool]] | None = None
    minutes: int = field(default_factory=lambda: checkin_reminder_thresholds()[0])
    tool_calls: int = field(default_factory=lambda: checkin_reminder_thresholds()[1])
    clock: Callable[[], datetime] = utc_now
    # The latest check-in read for each goal. Check-ins only ever get later,
    # so a remembered one is a safe lower bound that spares the reads.
    _known_check_ins: dict[str, datetime] = field(
        default_factory=dict, init=False, repr=False
    )

    async def _offered(self, turn: ChatTurn) -> bool:
        if self.offered is not None:
            return await self.offered(turn)
        names = turn.request_snapshot.get("available_tool_names")
        return isinstance(names, list) and self.check_in_tool in names

    @staticmethod
    def _reminded(
        goal: ChatGoal, turn: ChatTurn, seen: Mapping[str, Any]
    ) -> tuple[datetime | None, int]:
        """When the goal was last reminded, and the turn's tool count then."""

        kept = goal.metadata.get(CHECKIN_REMINDER_METADATA)
        kept = kept if isinstance(kept, Mapping) else {}
        at = _latest(_as_time(seen.get(CHECKIN_REMINDED_AT)), _as_time(kept.get("at")))
        calls = seen.get(CHECKIN_REMINDED_TOOL_CALLS)
        calls = calls if isinstance(calls, int) else 0
        kept_calls = kept.get("tool_calls")
        if kept.get("turn_id") == turn.id and isinstance(kept_calls, int):
            calls = max(calls, kept_calls)
        return at, calls

    async def last_check_in(self, goal: ChatGoal) -> datetime | None:
        """The latest check-in Core knows of for the goal's conversation."""

        updates = await self.store.find_entities(
            WorkUpdate,
            {"source_session_id": goal.session_id},
            newest_first=True,
            limit=1,
        )
        items = await self.store.find_entities(
            WorkItem,
            {"assignee_session_id": goal.session_id},
            engagement_id=goal.engagement_id,
        )
        from .butler_oversight import watch_id

        try:
            watch: ButlerGoalWatch | None = await self.store.get(
                ButlerGoalWatch, watch_id(goal.id)
            )
        except NotFoundError:  # diagnostic-expected: Butler does not watch every goal
            watch = None
        return _latest(
            updates[0].created_at if updates else None,
            *(item.last_update_at for item in items),
            watch.last_progress_at if watch is not None else None,
        )

    async def __call__(
        self, goal: ChatGoal, turn: ChatTurn, seen: Mapping[str, Any]
    ) -> GoalAddendum | None:
        if goal.status != ChatGoalStatus.RUNNING or not (
            self.minutes or self.tool_calls
        ):
            return None
        now = self.clock()
        reminded_at, reminded_calls = self._reminded(goal, turn, seen)
        started = goal.started_at or goal.created_at
        calls = max(0, turn.execution_tool_calls - reminded_calls)

        def due(quiet_since: datetime | None, calls_since: int) -> bool:
            return bool(
                self.minutes
                and quiet_since is not None
                and (now - quiet_since).total_seconds() >= self.minutes * 60
            ) or bool(self.tool_calls and calls_since >= self.tool_calls)

        # A check-in only moves the baselines on, so nothing due before the
        # check-ins are read, with the latest one already known, means
        # nothing is due: most calls stop here without a read.
        known = self._known_check_ins.get(goal.id)
        if not due(_latest(started, reminded_at, known), calls):
            return None
        if not await self._offered(turn):
            return None
        checked_in = _latest(known, await self.last_check_in(goal))
        if checked_in is not None:
            self._known_check_ins.pop(goal.id, None)
            self._known_check_ins[goal.id] = checked_in
            while len(self._known_check_ins) > KNOWN_CHECK_IN_GOALS:
                self._known_check_ins.pop(next(iter(self._known_check_ins)))
        in_turn: int | None = None
        if self.calls_after_check_in is not None:
            in_turn = await self.calls_after_check_in(turn)
        elif self.history is not None:
            in_turn = calls_after_native_check_in(await self.history(turn))
        if in_turn is not None:
            calls = min(calls, in_turn)
        if not due(_latest(started, reminded_at, checked_in), calls):
            return None
        quiet_since = _latest(started, checked_in) or now
        minutes = max(0, int((now - quiet_since).total_seconds() // 60))
        if checked_in is None:
            quiet = f"No Work check-in since this goal started {minutes} min ago"
        else:
            quiet = f"No Work check-in for {minutes} min"
        counted = (
            f"{in_turn} tool calls since your last one in this turn"
            if in_turn is not None
            else f"{turn.execution_tool_calls} tool calls in this turn"
        )
        text = (
            f"{quiet} ({counted}). Post {self.check_in_tool} on the Work item "
            "you are working on, with your progress and next step, before "
            f"continuing. If no item fits, create one with {self.create_tool}."
        )
        return GoalAddendum(
            CHECKIN_SOURCE,
            text,
            {
                CHECKIN_REMINDED_AT: now.isoformat(),
                CHECKIN_REMINDED_TOOL_CALLS: turn.execution_tool_calls,
            },
        )

    async def remember(
        self, goal_id: str, turn: ChatTurn, marker: Mapping[str, Any]
    ) -> None:
        """Keep a delivered reminder on the goal, for its later turns."""

        at = marker.get(CHECKIN_REMINDED_AT)
        record = {
            "at": at,
            "turn_id": turn.id,
            "tool_calls": marker.get(CHECKIN_REMINDED_TOOL_CALLS),
        }
        new_at = _as_time(at)

        def plan(latest: ChatGoal) -> dict[str, Any] | None:
            kept = latest.metadata.get(CHECKIN_REMINDER_METADATA)
            kept_at = _as_time(kept.get("at")) if isinstance(kept, Mapping) else None
            if kept_at is not None and new_at is not None and kept_at >= new_at:
                return None
            return {"metadata": {**latest.metadata, CHECKIN_REMINDER_METADATA: record}}

        try:
            (await revise_latest(self.store, ChatGoal, goal_id, plan))
        except NotFoundError:  # diagnostic-expected: goal deleted after its reminder
            return


def seen_from(
    snapshot: Mapping[str, Any], history: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """What the turn was prepared with, updated by each delivered step."""

    seen = dict(snapshot.get(SEEN_KEY) or {})
    for entry in history:
        marker = entry.get(SEEN_KEY)
        if isinstance(marker, Mapping):
            seen.update(marker)
    return seen


def prepared_marker(goal: ChatGoal) -> dict[str, Any]:
    """What a goal turn records at preparation: the guidance it was built on."""

    return {GUIDANCE_MARKER: goal.guidance_revision}


def addendum_text(addenda: Sequence[GoalAddendum]) -> str:
    """The bounded text delivered for one or more addenda."""

    body = "\n\n".join(item.text for item in addenda if item.text.strip())
    clipped = _clip(body, ADDENDUM_CHARACTERS)
    if clipped != body.strip():
        clipped += (
            f"\n\n(Shortened. Call {GOAL_READ_TOOL_NAME} for the full current goal.)"
        )
    return f"{ADDENDUM_HEADING}:\n{clipped}"


def addendum_output(goal: ChatGoal, addenda: Sequence[GoalAddendum]) -> dict[str, Any]:
    sources = {item.source for item in addenda}
    notes: list[str] = []
    if sources - {CHECKIN_SOURCE}:
        notes.append(
            "Core delivered this because the goal's supervisor changed it while "
            "you worked. It supersedes the goal context you started with; "
            "continue under the updated goal."
        )
    if CHECKIN_SOURCE in sources:
        notes.append(
            "Core reminds a goal that has not posted a Work check-in for a "
            "while. Your tools are unchanged."
        )
    return {
        "delivered_by": "nebula",
        "note": " ".join(notes),
        "goal_id": goal.id,
        GUIDANCE_MARKER: goal.guidance_revision,
        "addendum": addendum_text(addenda),
    }


@dataclass
class GoalAddenda:
    """Mid-turn addenda for a running provider goal turn."""

    store: NebulaStore
    history: Callable[[ChatTurn], Awaitable[list[dict[str, Any]]]]
    sources: list[AddendumSource] | None = None

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = [
                guidance_addendum,
                CheckinReminder(self.store, history=self.history),
            ]

    async def pending(
        self, turn: ChatTurn
    ) -> tuple[ChatGoal, list[GoalAddendum]] | None:
        if not turn.goal_id:
            return None
        try:
            goal = await self.store.get(ChatGoal, turn.goal_id)
        except NotFoundError:  # diagnostic-expected: goal deleted mid-turn
            return None
        if goal.status != ChatGoalStatus.RUNNING:
            return None

        async def collect(seen: Mapping[str, Any]) -> list[GoalAddendum]:
            return [
                item
                for source in self.sources or ()
                if (item := await source(goal, turn, seen)) is not None
            ]

        # Delivered steps only ever advance what the turn has heard, so
        # nothing due against what it was prepared with means nothing is due
        # at all, and the turn's step history is read only when something is.
        if not await collect(seen_from(turn.request_snapshot, ())):
            return None
        addenda = await collect(
            seen_from(turn.request_snapshot, await self.history(turn))
        )
        return (goal, addenda) if addenda else None

    async def routing_delivery(
        self, turn: ChatTurn, tool_names: Collection[str]
    ) -> tuple[str, dict[str, Any], str, dict[str, Any]] | None:
        """The step Core adds before the turn's next routing call:
        (tool name, result, summary, marker to save on the step)."""

        if GOAL_READ_TOOL_NAME not in tool_names:
            return None
        pending = await self.pending(turn)
        if pending is None:
            return None
        goal, addenda = pending
        marker: dict[str, Any] = {}
        for item in addenda:
            marker.update(item.marker)
        names = {item.source for item in addenda}
        if CHECKIN_SOURCE in names:
            # Kept on the goal before the step is saved: a crash in between
            # loses this one reminder, and the window starts again.
            for source in self.sources or ():
                if isinstance(source, CheckinReminder):
                    (await source.remember(goal.id, turn, marker))
        summary = (
            "Check-in reminder delivered"
            if names == {CHECKIN_SOURCE}
            else f"Supervisor addendum delivered ({', '.join(sorted(names))})"
        )
        return (
            GOAL_READ_TOOL_NAME,
            addendum_output(goal, addenda),
            summary,
            marker,
        )


NOTE_ADDED = "Supervisor note added:"


def describe_note(text: str, *, removed: bool = False) -> str:
    if removed:
        return "Supervisor note withdrawn:\n" + _clip(text, 1_000)
    return f"{NOTE_ADDED}\n" + _clip(text, CHANGE_SUMMARY_CHARACTERS - 100)
