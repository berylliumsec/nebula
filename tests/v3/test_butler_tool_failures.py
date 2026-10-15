"""Core-observed exact read-only failure concern and bounded parent guidance."""

import asyncio
from datetime import timedelta

import pytest

import nebula.v3.butler_tool_failures as tool_failures
from nebula.v3.butler_authority import AuthorityWrite
from nebula.v3.butler_oversight import (
    ButlerOversightService,
    project_state_id,
    project_stream_id,
)
from nebula.v3.butler_review import ButlerReviewOutput, ButlerReviewResult, review_pending
from nebula.v3.domain import (
    ButlerAction,
    ButlerIncident,
    ButlerProjectState,
    ChatBackend,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_authority import _setup


class _CitedLoopReviewer:
    async def review(self, packet):
        refs = [ref for ref in packet.evidence_refs if ref.kind == "tool_call"]
        return ButlerReviewResult(
            output=ButlerReviewOutput(
                choice="steer",
                summary="The exact read-only invocation failed three times.",
                reason="Core recorded three failed tool calls in separate completed turns.",
                why_now="A fourth identical call needs a review of saved outcomes first.",
                evidence_refs=refs,
            )
        )


def _record_call(
    store,
    goal,
    *,
    index,
    tool="workspace.read",
    arguments=None,
    result=None,
    status=ToolCallStatus.FAILED,
):
    session = store.get(ChatSession, goal.session_id)
    started = utc_now() - timedelta(minutes=10 - index * 2)
    turn = store.create(
        ChatTurn(
            engagement_id=goal.engagement_id,
            session_id=session.id,
            goal_id=goal.id,
            backend=ChatBackend.PROVIDER,
            provider_profile_id=session.provider_profile_id,
            model=session.model,
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
            created_at=started,
        )
    )
    call = store.create(
        ToolCall(
            engagement_id=goal.engagement_id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=session.id,
            chat_turn_id=turn.id,
            tool_name=tool,
            status=status,
            risk_class=RiskClass.LOCAL_READ,
            arguments=arguments or {"path": "notes.txt"},
            result=result,
            started_at=started,
            completed_at=started + timedelta(seconds=10),
            created_at=started,
        )
    )
    store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    return call


def test_three_exact_failures_create_one_concern_and_queue_only_to_parent(tmp_path):
    store, app, project, main, _, child, worker = _setup(tmp_path, child=True)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    calls = [_record_call(store, child, index=index) for index in range(3)]
    _record_call(store, child, index=3)
    events = [
        event
        for event in store.replay_operation_events(project_stream_id(project.id))
        if event.event_type == "goal.read_only_tool_failures_repeated"
    ]
    assert len(events) == 1
    assert events[0].payload["verification_state"] == "core_observed"
    assert {ref["id"] for ref in events[0].payload["evidence_refs"] if ref["kind"] == "tool_call"} == {
        call.id for call in calls
    }
    incidents = store.find_entities(ButlerIncident, {}, engagement_id=project.id)
    assert len(incidents) == 1
    assert incidents[0].source == "repeated_read_only_tool_failure"
    assert len(incidents[0].evidence_summary) == 3

    decision = asyncio.run(
        review_pending(
            store,
            project.id,
            worker.worker_id,
            _CitedLoopReviewer(),
            worker.memory_service,
        )
    )
    assert decision is not None and decision.choice == "steer"
    asyncio.run(authority.process_project(project.id, worker.worker_id))
    actions = store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(actions) == 1 and actions[0].status == "dispatched"
    assert actions[0].target_session_id == main.id
    parent_items = app.state.control_assistant_work.queue.get(main.id).items
    assert len(parent_items) == 1
    message = parent_items[0]["request"]["messages"][0]["content"]
    assert "three separate completed turns" in message
    assert "wider goal failed" in message
    assert app.state.control_assistant_work.queue.get(child.session_id).items == []

    reopened = NebulaStore(tmp_path / "core.db")
    restarted = ButlerOversightService(reopened)
    state = reopened.get(ButlerProjectState, project_state_id(project.id))
    reopened.update(
        ButlerProjectState,
        state.id,
        {"lease_expires_at": utc_now() - timedelta(seconds=1)},
        expected_revision=state.revision,
    )
    assert restarted.replay_project(project.id)
    assert reopened.count_entities(ButlerIncident, engagement_id=project.id) == 1
    assert len(
        [
            event
            for event in reopened.replay_operation_events(project_stream_id(project.id))
            if event.event_type == "goal.read_only_tool_failures_repeated"
        ]
    ) == 1


def test_changed_or_ambiguous_read_only_outcomes_do_not_create_concern(tmp_path):
    store, _, project, _, goal, _, worker = _setup(tmp_path)
    _record_call(store, goal, index=0)
    _record_call(store, goal, index=1, arguments={"path": "other.txt"})
    _record_call(store, goal, index=2)
    _record_call(
        store,
        goal,
        index=3,
        result={"schema": "nebula.tool-failure/v1", "side_effects": "unknown"},
    )
    assert store.count_entities(ButlerIncident, engagement_id=project.id) == 0


def test_success_after_review_cancels_redundant_parent_steer(tmp_path):
    store, app, project, main, goal, _, worker = _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    for index in range(3):
        _record_call(store, goal, index=index)
    decision = asyncio.run(
        review_pending(
            store,
            project.id,
            worker.worker_id,
            _CitedLoopReviewer(),
            worker.memory_service,
        )
    )
    assert decision is not None and decision.choice == "steer"
    _record_call(store, goal, index=3, status=ToolCallStatus.COMPLETE)
    asyncio.run(authority.process_project(project.id, worker.worker_id))
    assert store.count_entities(ButlerAction, engagement_id=project.id) == 0
    assert app.state.control_assistant_work.queue.get(main.id).items == []
    assert store.count_entities(ButlerIncident, engagement_id=project.id) == 1


def test_turn_completion_and_concern_roll_back_together_then_replay_once(
    tmp_path, monkeypatch
):
    store, _, project, _, goal, _, _ = _setup(tmp_path)
    _record_call(store, goal, index=0)
    _record_call(store, goal, index=1)
    append = tool_failures._append_project_event

    def fail_append(*args, **kwargs):
        raise RuntimeError("injected event write failure")

    monkeypatch.setattr(tool_failures, "_append_project_event", fail_append)
    with pytest.raises(RuntimeError, match="injected event write failure"):
        _record_call(store, goal, index=2)
    latest = store.find_entities(
        ChatTurn,
        {"session_id": goal.session_id},
        engagement_id=project.id,
        newest_first=True,
        limit=1,
    )[0]
    assert latest.status == ChatTurnStatus.ROUTING
    assert store.count_entities(ButlerIncident, engagement_id=project.id) == 0
    monkeypatch.setattr(tool_failures, "_append_project_event", append)
    completed = store.update(
        ChatTurn,
        latest.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=latest.revision,
    )
    assert completed.status == ChatTurnStatus.COMPLETE
    assert store.count_entities(ButlerIncident, engagement_id=project.id) == 1
    store.update(
        ChatTurn,
        latest.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=completed.revision,
    )
    assert store.count_entities(ButlerIncident, engagement_id=project.id) == 1
