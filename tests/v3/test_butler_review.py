"""Focused semantic-review selection, evidence, and replay contracts."""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nebula.v3.butler_oversight as oversight
from nebula.v3.butler_oversight import (
    ButlerEvidenceRef,
    ButlerOversightService,
    GoalProgressReport,
    butler_router,
    ingest_agent_report,
    project_stream_id,
    watch_id,
)
from nebula.v3.butler_review import (
    MEANINGFUL_EVENTS,
    ButlerReviewOutput,
    ButlerReviewResult,
    ProviderButlerReviewer,
    build_review_packet,
    decision_id,
    is_meaningful_event,
    review_pending,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalUpdate, GoalWrite
from nebula.v3.domain import (
    ButlerDecision,
    ButlerGoalWatch,
    ButlerIncident,
    ButlerProjectState,
    ChatGoal,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.providers import ModelResponse, ModelUsage
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider


def _setup(tmp_path, *, child_budget=None):
    path = tmp_path / "semantic-review.db"
    store = NebulaStore(path)
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Local provider",
            provider_type="openai-compatible",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Research goal",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    goals = ChatGoalService(store)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Investigate the observed response",
            completion_criteria=["Reproduce the response with evidence"],
            plan=["Probe the candidate input"],
            child_budget=child_budget,
        ),
    )
    goal = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    worker = ButlerOversightService(store)
    assert worker.replay_project("project")
    return path, store, goals, goal, worker


def _events(store):
    return store.replay_operation_events(project_stream_id("project"))


def _plan_edit(goals, goal):
    return goals.update(
        goal.session_id,
        GoalUpdate(
            expected_revision=goal.revision,
            objective=goal.objective,
            completion_criteria=goal.completion_criteria,
            plan=["Probe the candidate input", "Compare with a control"],
            token_budget=goal.token_budget,
            time_budget_seconds=goal.time_budget_seconds,
            step_budget=goal.step_budget,
            child_budget=goal.child_budget,
        ),
    )


class CitingReviewer:
    def __init__(self, *, choice="observe", invalid_citation=False):
        self.calls = []
        self.choice = choice
        self.invalid_citation = invalid_citation

    async def review(self, packet):
        self.calls.append(packet)
        refs = packet.evidence_refs[:1]
        if self.invalid_citation:
            refs = [ButlerEvidenceRef(kind="goal", id="other", project_id="elsewhere")]
        return ButlerReviewResult(
            output=ButlerReviewOutput(
                choice=self.choice,
                summary="The revised plan has a concrete control comparison.",
                reason="The saved goal plan now includes a discriminating control step.",
                impact="The next result can separate two explanations.",
                next_action="Continue watching the next checkpoint.",
                why_now="The next step could change the investigation direction."
                if self.choice == "escalate"
                else "",
                evidence_refs=refs,
            ),
            provider_id="provider",
            model="model-a",
            input_tokens=40,
            output_tokens=15,
        )


def test_meaningful_event_filter_is_narrow():
    assert MEANINGFUL_EVENTS == {
        "agent.progress_reported",
        "agent.attention_reported",
        "agent.plan_changed",
        "goal.progress_overdue",
        "goal.plan_changed",
        "goal.blocked",
    }
    assert is_meaningful_event(type("Event", (), {"event_type": "goal.blocked"})())
    assert not is_meaningful_event(
        type("Event", (), {"event_type": "resource.openrouter_balance_observed"})()
    )


def test_plan_change_event_is_atomic_and_skips_budget_only_edit(tmp_path, monkeypatch):
    _, store, goals, goal, _ = _setup(tmp_path)
    before = len(_events(store))
    changed = _plan_edit(goals, goal)
    event = _events(store)[-1]
    assert len(_events(store)) == before + 1
    assert event.event_type == "goal.plan_changed"
    assert event.payload["metrics"]["plan_changed"] is True

    same_plan = goals.update(
        changed.session_id,
        GoalUpdate(
            expected_revision=changed.revision,
            objective=changed.objective,
            completion_criteria=changed.completion_criteria,
            plan=changed.plan,
            token_budget=1000,
        ),
    )
    assert same_plan.token_budget == 1000
    assert len(_events(store)) == before + 1

    def fail(*_args, **_kwargs):
        raise RuntimeError("event unavailable")

    monkeypatch.setattr(oversight, "_append_project_event", fail)
    with pytest.raises(RuntimeError, match="event unavailable"):
        goals.update(
            same_plan.session_id,
            GoalUpdate(
                expected_revision=same_plan.revision,
                objective=same_plan.objective,
                completion_criteria=same_plan.completion_criteria,
                plan=[*same_plan.plan, "A third step"],
                token_budget=1000,
            ),
        )
    assert store.get(ChatGoal, same_plan.id).plan == same_plan.plan


def test_review_packet_is_bounded_and_contains_core_citations(tmp_path):
    _, store, goals, goal, _ = _setup(tmp_path)
    _plan_edit(goals, goal)
    event = _events(store)[-1]
    packet = build_review_packet(store, "project", event)
    assert packet.event_type == "goal.plan_changed"
    assert packet.objective == goal.objective
    assert packet.source_verification_state == "core_observed"
    assert packet.evidence_refs == [
        ButlerEvidenceRef(kind="goal", id=goal.id, project_id="project")
    ]
    assert len(packet.model_dump_json().encode()) <= 16_000
    with pytest.raises(Exception, match="another project"):
        build_review_packet(store, "other", event)


def test_review_decision_survives_restart_without_duplicate_model_call(tmp_path):
    path, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)
    candidate = _events(store)[-1]
    reviewer = CitingReviewer(choice="steer")
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert saved is not None
    assert saved.id == decision_id("project", candidate.id)
    assert saved.choice == "steer"
    assert saved.status == "reviewed"
    assert saved.evidence_refs == [
        {"kind": "goal", "id": goal.id, "project_id": "project"}
    ]
    assert saved.input_tokens == 40
    assert len(reviewer.calls) == 1
    assert (
        asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
        is None
    )
    assert len(reviewer.calls) == 1
    assert [item.event_type for item in _events(store)].count(
        "butler.decision_recorded"
    ) == 1

    restarted = NebulaStore(path)
    restarted.update(
        ButlerProjectState,
        oversight.project_state_id("project"),
        {"lease_expires_at": utc_now() - timedelta(seconds=1)},
    )
    second = ButlerOversightService(restarted)
    assert second.replay_project("project")
    assert (
        asyncio.run(review_pending(restarted, "project", second.worker_id, reviewer))
        is None
    )
    assert len(reviewer.calls) == 1
    assert restarted.get(ButlerDecision, saved.id).source_event_id == candidate.id


def test_same_goal_plan_burst_is_coalesced_into_one_review(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    first = _plan_edit(goals, goal)
    second = goals.update(
        first.session_id,
        GoalUpdate(
            expected_revision=first.revision,
            objective=first.objective,
            completion_criteria=first.completion_criteria,
            plan=[*first.plan, "Check the alternate path"],
        ),
    )
    candidate = _events(store)[-1]
    reviewer = CitingReviewer()
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert saved is not None
    assert saved.source_event_id == candidate.id
    assert reviewer.calls[0].current_plan[-1] == "Check the alternate path"
    assert (
        store.get(
            ButlerProjectState, oversight.project_state_id("project")
        ).last_review_sequence
        == candidate.sequence
    )
    assert store.get(ChatGoal, second.id).plan == second.plan


def test_overview_exposes_project_decision_and_pages_history(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)
    saved = asyncio.run(
        review_pending(store, "project", worker.worker_id, CitingReviewer())
    )
    app = FastAPI()
    app.include_router(butler_router(worker), prefix="/api/v1")
    client = TestClient(app)
    route = "/api/v1/engagements/project/butler/overview"
    first = client.get(route).json()
    assert first["summary"]["decisions"] == 1
    assert first["decisions"][0]["id"] == saved.id
    assert first["decisions"][0]["evidence_refs"][0]["project_id"] == "project"
    assert first["next_decision_offset"] is None
    second = client.get(route, params={"decision_offset": 1}).json()
    assert second["decisions"] == []
    assert second["summary"]["decisions"] == 1


def test_child_review_records_observation_without_messaging_child(tmp_path):
    _, store, goals, parent, worker = _setup(tmp_path, child_budget=1)
    child = goals.start_child(
        parent.session_id,
        GoalCreate(objective="Check one hypothesis", completion_criteria=["Result"]),
    )
    child = goals.write(
        child.session_id, GoalWrite(expected_revision=child.revision, action="start")
    )
    assert worker.replay_project("project")
    _plan_edit(goals, child)
    review = CitingReviewer(choice="ask")
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, review))
    assert saved is not None
    assert saved.actor_role == "subagent"
    assert review.calls[0].actor_role == "subagent"
    assert store.count_entities(ChatMessage, engagement_id="project") == 0
    assert store.count_entities(ButlerDecision, engagement_id="project") == 1


def test_invalid_model_citation_becomes_unavailable_without_foreign_evidence(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)
    saved = asyncio.run(
        review_pending(
            store,
            "project",
            worker.worker_id,
            CitingReviewer(invalid_citation=True),
        )
    )
    assert saved is not None
    assert saved.status == "unavailable"
    assert saved.reason == "invalid_evidence_citation"
    assert all(ref["project_id"] == "project" for ref in saved.evidence_refs)


def test_model_failure_cannot_suppress_deterministic_overdue_incident(tmp_path):
    _, store, _, goal, worker = _setup(tmp_path)
    store.update(
        ButlerGoalWatch,
        watch_id(goal.id),
        {"progress_due_at": utc_now() - timedelta(seconds=1)},
    )
    assert worker.fire_due_deadlines("project") == 1
    assert (
        store.count_entities(
            ButlerIncident, {"source": "goal_checkpoint"}, engagement_id="project"
        )
        == 1
    )

    class FailingReviewer:
        async def review(self, _packet):
            raise RuntimeError("provider failed")

    saved = asyncio.run(
        review_pending(store, "project", worker.worker_id, FailingReviewer())
    )
    assert saved is not None
    assert saved.status == "unavailable"
    assert saved.reason == "model_unavailable"
    assert (
        store.count_entities(
            ButlerIncident, {"source": "goal_checkpoint"}, engagement_id="project"
        )
        == 1
    )


def test_provider_reviewer_uses_goal_runtime_without_chat_mutation(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)
    chat_before = store.get(ChatSession, goal.session_id)

    class ReviewProvider(FakeProvider):
        async def complete(self, request):
            self.requests.append(request)
            payload = {
                "choice": "observe",
                "summary": "The new control is useful.",
                "reason": "The plan compares the candidate with a control.",
                "impact": "",
                "next_action": "Watch the next checkpoint.",
                "why_now": "",
                "evidence_refs": [
                    {"kind": "goal", "id": goal.id, "project_id": "project"}
                ],
            }
            return ModelResponse(
                provider_id="provider",
                model="model-a",
                text=json.dumps(payload),
                usage=ModelUsage(input_tokens=30, output_tokens=12, total_tokens=42),
            )

    provider = ReviewProvider("provider", local=True)
    reviewer = ProviderButlerReviewer(store, lambda _profile: provider)
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert saved is not None and saved.choice == "observe"
    assert provider.requests[0].metadata["operation"] == "butler_semantic_review"
    assert provider.requests[0].tools == []
    assert store.get(ChatSession, goal.session_id).revision == chat_before.revision
    assert store.count_entities(ChatTurn, engagement_id="project") == 0


def test_progress_report_packet_keeps_agent_claims_unverified(tmp_path):
    _, store, _, goal, worker = _setup(tmp_path)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
        )
    )
    ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="checkpoint",
        body=GoalProgressReport(
            what_changed="The first probe completed.",
            evidence_gained=["The response differed."],
            criterion_advanced="Reproduction",
            next_step="Run a control.",
        ),
        source="provider_tool",
    )
    event = _events(store)[-1]
    packet = build_review_packet(store, "project", event)
    assert packet.source_verification_state == "agent_reported"
    assert (
        packet.recent_reports[0]["claims"]["what_changed"]
        == "The first probe completed."
    )
    assert any(ref.kind == "agent_report" for ref in packet.evidence_refs)
    assert (
        asyncio.run(review_pending(store, "project", worker.worker_id, None)).status
        == "unavailable"
    )
