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
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Collection, Mapping, Sequence

from .domain import (
    GOAL_GUIDANCE_CHANGE_LIMIT,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    GoalGuidanceChange,
    utc_now,
)
from .storage import NebulaStore, NotFoundError
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
    return {
        "delivered_by": "nebula",
        "note": (
            "Core delivered this because the goal's supervisor changed it while "
            "you worked. It supersedes the goal context you started with; "
            "continue under the updated goal."
        ),
        "goal_id": goal.id,
        GUIDANCE_MARKER: goal.guidance_revision,
        "addendum": addendum_text(addenda),
    }


@dataclass
class GoalAddenda:
    """Mid-turn addenda for a running provider goal turn."""

    store: NebulaStore
    history: Callable[[ChatTurn], Awaitable[list[dict[str, Any]]]]
    sources: list[AddendumSource] = field(default_factory=lambda: [guidance_addendum])

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
                for source in self.sources
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
        sources = ", ".join(sorted({item.source for item in addenda}))
        return (
            GOAL_READ_TOOL_NAME,
            addendum_output(goal, addenda),
            f"Supervisor addendum delivered ({sources})",
            marker,
        )


NOTE_ADDED = "Supervisor note added:"


def describe_note(text: str, *, removed: bool = False) -> str:
    if removed:
        return "Supervisor note withdrawn:\n" + _clip(text, 1_000)
    return f"{NOTE_ADDED}\n" + _clip(text, CHANGE_SUMMARY_CHARACTERS - 100)
