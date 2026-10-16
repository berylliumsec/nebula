from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_timeline import ButlerTimeline
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.domain import (
    ButlerAgentReport,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    Engagement,
    HarnessTurn,
    HarnessTurnOrigin,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore


def _store(tmp_path):
    return NebulaStore(tmp_path / "core.db")


def _chat(store, project, title, *, parent=None, created_at=None):
    return store.create(
        ChatSession(
            engagement_id=project.id,
            title=title,
            provider_profile_id="provider",
            model="model-a",
            parent_session_id=parent.id if parent else None,
            created_at=created_at or utc_now(),
        )
    )


def test_timeline_joins_parent_child_provider_harness_and_report_evidence(tmp_path):
    store = _store(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    now = utc_now()
    parent = _chat(store, selected, "Protocol review")
    child = _chat(store, selected, "Parser subagent", parent=parent)
    other = _chat(store, foreign, "Foreign review")
    turn = store.create(
        ChatTurn(
            engagement_id=selected.id,
            session_id=parent.id,
            provider_profile_id="provider",
            model="model-a",
        )
    )
    store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=parent.id,
            sequence=1,
            role=ChatRole.USER,
            content="Review the parser boundary",
        )
    )
    store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=child.id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="Found a length check to inspect",
        )
    )
    store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=parent.id,
            sequence=2,
            role=ChatRole.ASSISTANT,
            content="Old answer outside requested window",
            created_at=now - timedelta(hours=3),
        )
    )
    store.create(
        ChatMessage(
            engagement_id=foreign.id,
            session_id=other.id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="Foreign project secret",
        )
    )
    store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=parent.id,
            sequence=3,
            role=ChatRole.ASSISTANT,
            content="Retracted answer must stay out of the timeline",
            metadata={"retracted_at": now.isoformat()},
        )
    )
    store.create(
        ChatSubagent(
            engagement_id=selected.id,
            parent_session_id=parent.id,
            parent_turn_id=turn.id,
            child_session_id=child.id,
            name="Parser subagent",
            task="Inspect length checks",
            status=ChatSubagentStatus.COMPLETED,
            started_at=now - timedelta(minutes=20),
            finished_at=now - timedelta(minutes=5),
            result="Identified a candidate check",
        )
    )
    store.create(
        ToolCall(
            engagement_id=selected.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.id,
            chat_turn_id=turn.id,
            tool_name="workspace_search",
            status=ToolCallStatus.COMPLETE,
            risk_class=RiskClass.LOCAL_READ,
            arguments={"query": "length check"},
        )
    )
    ChatTurnLedger(store.database).append(
        turn.id,
        {
            "step": 1,
            "status": "completed",
            "name": "workspace_search",
            "arguments": {"query": "length check"},
            "result_summary": "Found three candidate files",
        },
    )
    store.create(
        ButlerAgentReport(
            engagement_id=selected.id,
            goal_id="goal",
            session_id=parent.id,
            turn_id=turn.id,
            report_type="progress",
            source="provider_tool",
            actor_role="main_agent",
            idempotency_key="progress-1",
            details={
                "what_changed": "Mapped two parser paths",
                "evidence_gained": ["Static map only"],
                "next_step": "Run a controlled case",
            },
        )
    )
    harness = store.create(
        HarnessTurn(
            engagement_id=selected.id,
            harness_session_id="harness-session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=child.id,
            chat_turn_id="child-turn",
            prompt="Inspect the parser",
        )
    )
    store.append_operation_event(
        harness.id,
        "harness_turn",
        selected.id,
        "harness.tool_completed",
        {
            "type": "tool_completed",
            "title": "Search source",
            "summary": "Read parser files",
        },
    )
    store.append_operation_event(
        harness.id,
        "harness_turn",
        selected.id,
        "harness.output_delta",
        {"type": "output_delta", "delta": "Raw streamed output is excluded"},
    )

    result = ButlerTimeline(store).read(
        selected.id, {"agent": "Protocol review", "hours": 2}
    )
    kinds = {item["kind"] for item in result["events"]}
    assert kinds == {
        "message",
        "turn",
        "tool_call",
        "provider_step",
        "report",
        "subagent",
        "harness_event",
    }
    assert {item["session_id"] for item in result["events"]} == {parent.id, child.id}
    assert all("Foreign project secret" not in str(item) for item in result["events"])
    assert all("Old answer" not in str(item) for item in result["events"])
    assert all("Raw streamed output" not in str(item) for item in result["events"])
    assert all("Retracted answer" not in str(item) for item in result["events"])
    assert (
        next(item for item in result["events"] if item["kind"] == "report")[
            "evidence_level"
        ]
        == "agent_reported"
    )
    assert result["active_turns"][0]["turn_id"] == turn.id
    assert result["lineage_complete"] is True
    assert result["has_more"] is False


def test_timeline_pages_same_time_without_repeats_and_binds_cursor(tmp_path):
    store = _store(tmp_path)
    project = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Other"))
    chat = _chat(store, project, "Long review")
    when = utc_now() - timedelta(minutes=10)
    for sequence in range(1, 7):
        store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=chat.id,
                sequence=sequence,
                role=ChatRole.ASSISTANT,
                content=f"Checkpoint {sequence}",
                created_at=when,
            )
        )
    timeline = ButlerTimeline(store)
    seen = []
    result = timeline.read(project.id, {"session_id": chat.id, "hours": 1, "limit": 2})
    while True:
        seen.extend(item["source_id"] for item in result["events"])
        if not result["has_more"]:
            break
        with pytest.raises(ValueError, match="cursor"):
            timeline.read(foreign.id, {"cursor": result["next_cursor"]})
        result = timeline.read(
            project.id, {"cursor": result["next_cursor"], "limit": 2}
        )
    assert len(seen) == 6
    assert len(set(seen)) == 6


def test_timeline_pages_fit_core_model_delivery_bound(tmp_path):
    store = _store(tmp_path)
    project = store.create(Engagement(name="Selected"))
    chat = _chat(store, project, "Verbose review")
    for sequence in range(1, 41):
        store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=chat.id,
                sequence=sequence,
                role=ChatRole.ASSISTANT,
                content=f"Checkpoint {sequence}: " + "observed parser state " * 30,
            )
        )
    timeline = ButlerTimeline(store)
    seen = []
    result = timeline.read(project.id, {"session_id": chat.id, "hours": 1, "limit": 80})
    for _ in range(40):
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 7_000
        assert result["events"]
        seen.extend(item["source_id"] for item in result["events"])
        if not result["has_more"]:
            break
        assert result["next_cursor"]
        result = timeline.read(
            project.id, {"cursor": result["next_cursor"], "limit": 80}
        )
    assert len(seen) == 40
    assert len(set(seen)) == 40


def test_timeline_resolves_ambiguous_titles_and_rejects_cross_project_target(tmp_path):
    store = _store(tmp_path)
    project = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Other"))
    first = _chat(store, project, "Research agent")
    second = _chat(store, project, "Research agent")
    other = _chat(store, foreign, "Research agent")
    timeline = ButlerTimeline(store)
    choice = timeline.read(project.id, {"agent": "Research agent", "hours": 4})
    assert choice["selection_required"] is True
    assert {item["session_id"] for item in choice["matches"]} == {first.id, second.id}
    with pytest.raises(ValueError, match="this project"):
        timeline.read(project.id, {"session_id": other.id, "hours": 4})
    with pytest.raises(ValueError, match="hours"):
        timeline.read(project.id, {"session_id": first.id, "hours": 721})
    empty = timeline.read(project.id, {"session_id": first.id, "hours": 4})
    assert empty["events"] == []
    assert empty["has_more"] is False


def test_butler_discovers_timeline_through_provider_capability_gateway(tmp_path):
    store = _store(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    project = store.create(Engagement(name="Selected"))
    chat = _chat(store, project, "Agent A")
    capability = ButlerCapabilities(
        store, artifacts, project.id, work=app.state.control_assistant_work
    )

    async def scenario():
        found = await capability.search(
            {"query": "what was my agent working on for hours"}
        )
        assert "nebula.work.timeline" in {
            item["operation"] for item in found["matches"]
        }
        described = await capability.describe({"operation": "nebula.work.timeline"})
        assert described["invoke_with"] == "nebula.capabilities.read"
        result = await capability.invoke(
            {
                "operation": "nebula.work.timeline",
                "arguments": {"session_id": chat.id, "hours": 12},
            },
            change=False,
        )
        assert result["target"]["session_id"] == chat.id
        assert result["events"] == []

    asyncio.run(scenario())
