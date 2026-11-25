from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import timedelta

import pytest

from nebula.v3.butler_note_release import (
    ButlerNoteRelease,
    NoteReleaseError,
    is_gating_text,
    post_goal_note,
    post_note_and_restart,
    require_release_for_gating,
    retire_released_notes,
    sweep_note_releases,
    validate_release_condition,
)
from nebula.v3.butler_supervisor_policy import SupervisorDisabled
from nebula.v3.chat_goal_addenda import (
    GUIDANCE_MARKER,
    NOTE_RELEASES_KEY,
    goal_guidance_text,
    guidance_addendum,
    notes_progress,
)
from nebula.v3.chat_goals import ChatGoalService, GoalNoteCreate
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    WorkItem,
    WorkUpdate,
    utc_now,
)
from nebula.v3.storage import NebulaStore

from tests.v3.test_butler_stuck_turns import FakeWork, _policy, _setup, _turn

GATE = "STOP WORK. Operator gate: make no tool calls until the operator rules."


def test_gating_language_is_detected():
    for text in (
        GATE,
        "Please do not proceed until reviewed.",
        "Wait for the operator before the next step.",
        "Make no tool calls.",
        "HALT everything",
    ):
        assert is_gating_text(text), text
    for text in (
        "Prefer the smaller binary first.",
        "Stop the scan when it finishes and summarize.",
        "Record evidence paths in the handoff.",
    ):
        assert not is_gating_text(text), text


def test_butler_gating_note_needs_a_release_condition():
    with pytest.raises(NoteReleaseError, match="release_condition"):
        require_release_for_gating(GATE, "butler", None)
    require_release_for_gating(GATE, "operator", None)
    require_release_for_gating(GATE, "butler", {"kind": "until_note_superseded"})
    require_release_for_gating("Prefer the smaller binary.", "butler", None)


async def test_conditions_are_validated_on_write(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _profile, _session, goal = await _setup(store)
    other, _p, _s, other_goal = await _setup(store)
    now = utc_now()
    with pytest.raises(NoteReleaseError, match="kind"):
        await validate_release_condition(store, goal, {"kind": "until_never"})
    with pytest.raises(NoteReleaseError, match="past"):
        await validate_release_condition(
            store,
            goal,
            {"kind": "until_time", "at": (now - timedelta(minutes=1)).isoformat()},
        )
    assert (
        await validate_release_condition(
            store, goal, {"kind": "until_time", "at": "2999-01-01T00:00:00Z"}
        )
    )["at"].startswith("2999-01-01T00:00:00")
    root = tmp_path / "gates"
    root.mkdir()
    with pytest.raises(NoteReleaseError, match="allowed root"):
        await validate_release_condition(
            store,
            goal,
            {"kind": "until_path_exists", "path": str(tmp_path / "elsewhere")},
            roots=[str(root)],
        )
    with pytest.raises(NoteReleaseError, match="no release roots"):
        await validate_release_condition(
            store,
            goal,
            {"kind": "until_path_exists", "path": str(root / "x")},
            roots=[],
        )
    item = await store.create(WorkItem(engagement_id=other.id, title="Elsewhere"))
    with pytest.raises(NoteReleaseError, match="project"):
        await validate_release_condition(
            store, goal, {"kind": "until_work_item", "item_id": item.id, "closed": True}
        )
    mine = await store.create(WorkItem(engagement_id=project.id, title="Ruling"))
    with pytest.raises(NoteReleaseError, match="at least one"):
        await validate_release_condition(
            store, goal, {"kind": "until_work_item", "item_id": mine.id}
        )
    with pytest.raises(NoteReleaseError, match="same project"):
        await validate_release_condition(
            store,
            goal,
            {
                "kind": "until_goal_status",
                "goal_id": other_goal.id,
                "statuses": ["completed"],
            },
        )
    with pytest.raises(NoteReleaseError, match="does not take"):
        await validate_release_condition(
            store, goal, {"kind": "until_note_superseded", "path": "/x"}
        )


async def test_post_refuses_unconditioned_butler_gate(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    _project, _profile, session, goal = await _setup(store)
    goals = ChatGoalService(store)
    with pytest.raises(NoteReleaseError):
        await post_goal_note(store, goals, session.id, GATE, author="butler")
    assert (await store.get(ChatGoal, goal.id)).supervisor_notes == []
    # Operator notes are not checked.
    posted = await post_goal_note(store, goals, session.id, GATE, author="operator")
    assert posted["release_condition"] is None


async def test_time_release_retires_note_and_stops_delivery(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    _project, _profile, session, goal = await _setup(store)
    goals = ChatGoalService(store)
    await goals.add_note(session.id, GoalNoteCreate(text="Keep the scope narrow."))
    at = utc_now() + timedelta(minutes=30)
    posted = await post_goal_note(
        store,
        goals,
        session.id,
        GATE,
        author="butler",
        release_condition={"kind": "until_time", "at": at.isoformat()},
    )
    goal = await store.get(ChatGoal, goal.id)
    assert any(n["text"] == GATE for n in notes_progress(goal) or [])
    # Not yet.
    same = await retire_released_notes(store, goal)
    assert same.revision == goal.revision
    heard = goal.guidance_revision
    retired = await retire_released_notes(store, goal, now=at + timedelta(seconds=1))
    entry = retired.metadata[NOTE_RELEASES_KEY][posted["note_id"]]
    assert entry["retired_at"] and "reached" in entry["retired_reason"]
    # Kept on the goal, no longer delivered.
    assert any(note.id == posted["note_id"] for note in retired.supervisor_notes)
    assert [n["text"] for n in notes_progress(retired) or []] == [
        "Keep the scope narrow."
    ]
    assert GATE not in goal_guidance_text(retired)
    # A running turn hears the release, not the gate.
    assert retired.guidance_revision == heard + 1
    turn = ChatTurn.model_construct(goal_id=goal.id)
    addendum = await guidance_addendum(retired, turn, {GUIDANCE_MARKER: heard - 1})
    assert addendum is not None
    assert "released" in addendum.text
    assert "STOP WORK" not in addendum.text.split("released", 1)[0]
    assert addendum.text.count(GATE) == 1  # only quoted in the release notice


async def test_work_goal_superseded_and_path_conditions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _profile, session, goal = await _setup(store)
    goals = ChatGoalService(store)
    item = await store.create(
        WorkItem(engagement_id=project.id, title="Operator decision")
    )
    root = tmp_path / "gates"
    root.mkdir()
    marker = root / "approved.flag"
    roots = [str(root)]
    ruling = await post_goal_note(
        store,
        goals,
        session.id,
        "Wait for the operator ruling on scope.",
        author="butler",
        release_condition={
            "kind": "until_work_item",
            "item_id": item.id,
            "ruling": True,
        },
    )
    staged = await post_goal_note(
        store,
        goals,
        session.id,
        "Do not proceed past triage.",
        author="butler",
        release_condition={
            "kind": "until_work_item",
            "item_id": item.id,
            "stage": "validate",
        },
    )
    paused = await post_goal_note(
        store,
        goals,
        session.id,
        "Keep at most two workers.",
        author="butler",
        release_condition={"kind": "until_goal_status", "statuses": ["paused"]},
    )
    from nebula.v3.butler_note_release import attach_release_condition

    # A path condition attached with explicit roots, as validated on write.
    path_note = await post_goal_note(
        store, goals, session.id, "Hold the publish step.", author="operator"
    )
    goal = await store.get(ChatGoal, goal.id)
    await attach_release_condition(
        store,
        goal.id,
        path_note["note_id"],
        await validate_release_condition(
            store, goal, {"kind": "until_path_exists", "path": str(marker)}, roots=roots
        ),
        author="operator",
    )
    goal = await store.get(ChatGoal, goal.id)
    assert (
        await retire_released_notes(store, goal, roots=roots)
    ).revision == goal.revision

    await store.create(
        WorkUpdate(
            engagement_id=project.id,
            item_id=item.id,
            summary="Ruled: proceed with the narrow scope.",
            status="in_progress",
            actor_kind="operator",
            actor_id="operator",
        )
    )
    marker.write_text("ok")
    goal = await retire_released_notes(store, goal, roots=roots)
    releases = goal.metadata[NOTE_RELEASES_KEY]
    assert releases[ruling["note_id"]]["retired_reason"].startswith("operator ruled")
    assert releases[path_note["note_id"]]["retired_at"]
    assert not releases[staged["note_id"]]["retired_at"]
    assert not releases[paused["note_id"]]["retired_at"]

    current = await store.get(WorkItem, item.id)
    await store.update(
        WorkItem, item.id, {"stage": "validate"}, expected_revision=current.revision
    )
    goal = await retire_released_notes(store, goal, roots=roots)
    assert goal.metadata[NOTE_RELEASES_KEY][staged["note_id"]]["retired_at"]

    superseded = await post_goal_note(
        store,
        goals,
        session.id,
        "Halt the fuzzing lane.",
        author="butler",
        release_condition={"kind": "until_note_superseded"},
    )
    goal = await retire_released_notes(store, await store.get(ChatGoal, goal.id))
    assert not goal.metadata[NOTE_RELEASES_KEY][superseded["note_id"]]["retired_at"]
    await goals.add_note(
        session.id, GoalNoteCreate(text="Resume the fuzzing lane.", author="butler")
    )
    retired = await sweep_note_releases(store, [project.id])
    assert {row["note_id"] for row in retired} == {superseded["note_id"]}
    goal = await store.get(ChatGoal, goal.id)
    assert goal.status == ChatGoalStatus.RUNNING
    texts = [n["text"] for n in notes_progress(goal) or []]
    assert texts == ["Resume the fuzzing lane.", "Keep at most two workers."]


async def test_post_note_and_restart_reuses_recovery(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    turn = await _turn(store, project, profile, session, goal, ChatTurnStatus.ROUTING)
    work = FakeWork(store)
    result = await post_note_and_restart(
        store,
        work,
        session.id,
        "Switch to the second binary now.",
        author="butler",
    )
    assert result["restart"]["outcome"] == "recovered", result
    assert result["restart"]["resumed"] is True
    assert work.chat.stopped == [turn.id]
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.RUNNING

    idle = await post_note_and_restart(
        store, work, session.id, "One more hint.", author="butler"
    )
    assert idle["restart"] == {"outcome": "no_turn_in_flight"}


async def test_butler_tool_posts_with_condition_and_audits(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _profile, session, goal = await _setup(store)
    work = FakeWork(store)
    disabled = ButlerNoteRelease(
        store, work, project.id, policy_loader=lambda: _policy(tmp_path, enabled=False)
    )
    (action,) = disabled.actions()
    assert action.spec.name == "nebula.supervisor.post_goal_note"
    with pytest.raises(SupervisorDisabled):
        await action._handler({"session_id": session.id, "text": "x"})
    tool = ButlerNoteRelease(
        store, work, project.id, policy_loader=lambda: _policy(tmp_path)
    )
    with pytest.raises(NoteReleaseError):
        await tool.post({"session_id": session.id, "text": GATE})
    result = await tool.post(
        {
            "session_id": session.id,
            "text": GATE,
            "release_condition": {"kind": "until_goal_status", "statuses": ["paused"]},
        }
    )
    saved = await store.get(ChatGoal, goal.id)
    assert saved.metadata[NOTE_RELEASES_KEY][result["note_id"]]["author"] == "butler"
    audit = [
        json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert [entry["allowed"] for entry in audit] == [False, True]
    other, _p, other_session, _g = await _setup(store)
    with pytest.raises(PermissionError):
        await tool.post({"session_id": other_session.id, "text": "hint"})
