"""Focused semantic-review selection, evidence, and replay contracts."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nebula.v3.butler_oversight as oversight
import nebula.v3.butler_review as semantic_review
from nebula.v3.butler_oversight import (
    ButlerEvidenceRef,
    ButlerOversightService,
    GoalAttentionReport,
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
from nebula.v3.butler_memory import ButlerMemoryService
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_precedent import read_precedents
from nebula.v3.butler_recovery import ButlerRecoveryService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalUpdate, GoalWrite
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerDecision,
    ButlerGoalWatch,
    ButlerIncident,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoal,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessKind,
    HarnessProfile,
    ProviderPrivacy,
    ProviderProfile,
    utc_now,
)
from nebula.v3.providers import ModelResponse, ModelUsage
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider


def test_completed_turn_has_precedent_query_before_semantic_review():
    class Memory:
        def list(self, project_id, *, limit):
            assert project_id == "project" and limit == 40
            return SimpleNamespace(items=[])

        def search(self, project_id, query, *, limit):
            assert project_id == "project" and limit == 3
            assert "completed investigation" in query
            return SimpleNamespace(
                items=[], index_status=SimpleNamespace(state="lexical_fallback")
            )

    snapshot = read_precedents(Memory(), "project", "agent.turn_completed")
    assert snapshot.index_state == "lexical_fallback" and snapshot.items == []


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
        "agent.turn_completed",
        "goal.progress_overdue",
        "goal.plan_changed",
        "goal.blocked",
        "goal.read_only_tool_failures_repeated",
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


def test_precedent_packet_is_bounded_and_excludes_foreign_project_memory(tmp_path):
    memory = ButlerMemoryService(tmp_path / "precedent.db")
    preference = memory.capture_operator_preference(
        "other-project",
        "Always ask the parent before changing a goal.",
        source_ref="other-project-private-source",
    )
    foreign = memory.capture_verified_lesson(
        "other-project",
        "Record the other project's private checkpoint before continuing.",
        source_ref="other-project-private-result",
        verification_state="verified",
    )
    assert preference is not None
    snapshot = read_precedents(memory, "project", "goal.progress_overdue")
    assert [item.id for item in snapshot.items] == [preference.id]
    assert foreign.id not in [item.id for item in snapshot.items]
    assert "other-project-private-source" not in snapshot.model_dump_json()
    assert "other-project-private-result" not in snapshot.model_dump_json()
    assert len(snapshot.items) <= 4


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


def test_escalation_opens_one_project_incident_with_cited_evidence_and_replays_cleanly(
    tmp_path,
):
    path, store, goals, goal, worker = _setup(tmp_path)
    store.create(Engagement(id="other-project", name="Other Project"))
    _plan_edit(goals, goal)
    reviewer = CitingReviewer(choice="escalate")

    first = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert first is not None and first.choice == "escalate"
    incidents = store.find_entities(ButlerIncident, {}, engagement_id="project")
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.source == "semantic_review"
    assert incident.verification_state == "model_assessed"
    assert incident.status == "open"
    assert incident.goal_ids == [goal.id]
    assert incident.session_ids == [goal.session_id]
    assert {
        (ref["kind"], ref["id"], ref["project_id"]) for ref in incident.evidence_refs
    } == {
        ("decision", first.id, "project"),
        ("goal", goal.id, "project"),
    }
    assert any("saved goal plan" in item for item in incident.evidence_summary)
    assert any("Why now" in item for item in incident.evidence_summary)
    assert store.count_entities(ChatMessage, engagement_id="project") == 0

    app = FastAPI()
    app.include_router(butler_router(worker), prefix="/api/v1")
    client = TestClient(app)
    project_overview = client.get("/api/v1/engagements/project/butler/overview").json()
    other_overview = client.get(
        "/api/v1/engagements/other-project/butler/overview"
    ).json()
    assert project_overview["summary"]["open_incidents"] == 1
    assert project_overview["incidents"][0]["id"] == incident.id
    assert other_overview["summary"]["open_incidents"] == 0
    assert other_overview["incidents"] == []
    assert store.count_entities(ButlerIncident, engagement_id="other-project") == 0

    restarted = NebulaStore(path)
    restarted.update(
        ButlerProjectState,
        oversight.project_state_id("project"),
        {"lease_expires_at": utc_now() - timedelta(seconds=1)},
    )
    second_worker = ButlerOversightService(restarted)
    assert second_worker.replay_project("project")
    assert (
        asyncio.run(
            review_pending(restarted, "project", second_worker.worker_id, reviewer)
        )
        is None
    )
    assert len(reviewer.calls) == 1
    assert restarted.count_entities(ButlerIncident, engagement_id="project") == 1
    assert [item.event_type for item in _events(restarted)].count(
        "incident.opened"
    ) == 1


def test_saved_agent_review_request_wakes_act_once_with_turn_evidence(tmp_path):
    _, store, _, goal, worker = _setup(tmp_path)
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id("project"), engagement_id="project", authority="act"
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id="project",
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model-a",
        )
    )
    message = store.create(
        ChatMessage(
            engagement_id="project",
            session_id=goal.session_id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="Please review this evidence and decide whether I should continue.",
        )
    )
    store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE, "final_message_id": message.id},
        expected_revision=turn.revision,
    )

    reviewer = CitingReviewer(choice="escalate")
    decision = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert decision is not None and decision.source_event_type == "agent.turn_completed"
    assert reviewer.calls[0].event_summary.endswith(message.content)
    decision_event = next(
        event
        for event in _events(store)
        if event.event_type == "butler.decision_recorded"
    )
    assert {ref["kind"] for ref in decision_event.payload["evidence_refs"]} == {
        "decision",
        "chat_turn",
    }
    recovery = ButlerRecoveryService(store)
    assert recovery.capture_events("project", worker.worker_id) > 0
    assert recovery.capture_events("project", worker.worker_id) == 0
    cases = store.find_entities(ButlerRecoveryCase, {}, engagement_id="project")
    assert [item.signal for item in cases] == ["review.agent_requested"]
    case = cases[0]
    assert case.signal == "review.agent_requested"
    assert case.source_turn_id == turn.id
    assert case.authority_revision == policy.revision


def test_repeated_escalation_updates_existing_concern_without_duplicate(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    reviewer = CitingReviewer(choice="escalate")
    first_plan = _plan_edit(goals, goal)
    first = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert first is not None
    store.update(
        ButlerProjectState,
        oversight.project_state_id("project"),
        {"last_review_requested_at": utc_now() - timedelta(minutes=1)},
    )
    goals.update(
        first_plan.session_id,
        GoalUpdate(
            expected_revision=first_plan.revision,
            objective=first_plan.objective,
            completion_criteria=first_plan.completion_criteria,
            plan=[*first_plan.plan, "Check an independent sample"],
        ),
    )
    second = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert second is not None and second.id != first.id
    incidents = store.find_entities(ButlerIncident, {}, engagement_id="project")
    assert len(incidents) == 1
    assert {
        ref["id"] for ref in incidents[0].evidence_refs if ref["kind"] == "decision"
    } == {
        first.id,
        second.id,
    }
    event_types = [item.event_type for item in _events(store)]
    assert event_types.count("incident.opened") == 1
    assert event_types.count("incident.updated") == 1


def test_expired_review_lease_cannot_record_escalation(tmp_path):
    _, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)

    class ExpiringReviewer(CitingReviewer):
        async def review(self, packet):
            result = await super().review(packet)
            store.update(
                ButlerProjectState,
                oversight.project_state_id("project"),
                {"lease_expires_at": utc_now() - timedelta(seconds=1)},
            )
            return result

    saved = asyncio.run(
        review_pending(
            store, "project", worker.worker_id, ExpiringReviewer(choice="escalate")
        )
    )
    assert saved is None
    assert store.count_entities(ButlerDecision, engagement_id="project") == 0
    assert store.count_entities(ButlerIncident, engagement_id="project") == 0
    assert [item.event_type for item in _events(store)].count(
        "butler.decision_recorded"
    ) == 0


def test_incident_write_failure_rolls_back_decision_and_review_cursor(
    tmp_path, monkeypatch
):
    _, store, goals, goal, worker = _setup(tmp_path)
    _plan_edit(goals, goal)
    original = semantic_review._append_project_event

    def fail_on_incident(*args, **kwargs):
        if kwargs["event_type"] == "incident.opened":
            raise RuntimeError("incident event unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic_review, "_append_project_event", fail_on_incident)
    with pytest.raises(RuntimeError, match="incident event unavailable"):
        asyncio.run(
            review_pending(
                store, "project", worker.worker_id, CitingReviewer(choice="escalate")
            )
        )
    assert store.count_entities(ButlerDecision, engagement_id="project") == 0
    assert store.count_entities(ButlerIncident, engagement_id="project") == 0
    assert (
        store.get(
            ButlerProjectState, oversight.project_state_id("project")
        ).last_review_sequence
        == 0
    )
    assert [item.event_type for item in _events(store)].count(
        "butler.decision_recorded"
    ) == 0

    monkeypatch.setattr(semantic_review, "_append_project_event", original)
    saved = asyncio.run(
        review_pending(
            store, "project", worker.worker_id, CitingReviewer(choice="escalate")
        )
    )
    assert saved is not None and saved.choice == "escalate"
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1


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


@pytest.mark.parametrize("attention_first", [True, False])
def test_attention_report_is_review_boundary_even_with_adjacent_plan_change(
    tmp_path, attention_first
):
    _, store, goals, goal, worker = _setup(tmp_path)
    turn = store.create(
        ChatTurn(
            id="attention-turn",
            engagement_id="project",
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
        )
    )

    def report_attention():
        ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="urgent-machine",
            body=GoalAttentionReport(
                condition="machine_unreachable",
                machine="target.example",
                symptom="The connection timed out.",
                impact="The check cannot continue.",
                evidence=["Three attempts timed out."],
                attempted_recovery="Retried once.",
                urgency="urgent",
                help_requested="Check whether the machine is available.",
            ),
            source="provider_tool",
        )

    if attention_first:
        report_attention()
        _plan_edit(goals, goal)
    else:
        _plan_edit(goals, goal)
        report_attention()
    attention_event = next(
        event
        for event in _events(store)
        if event.event_type == "agent.attention_reported"
    )
    reviewer = CitingReviewer(choice="steer")
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert saved is not None
    assert saved.source_event_id == attention_event.id
    assert saved.source_event_type == "agent.attention_reported"
    assert reviewer.calls[0].source_verification_state == "agent_reported"


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
    assert store.count_entities(ButlerIncident, engagement_id="project") == 0


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
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1

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
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1


def test_escalation_joins_guaranteed_overdue_incident(tmp_path):
    _, store, _, goal, worker = _setup(tmp_path)
    store.update(
        ButlerGoalWatch,
        watch_id(goal.id),
        {"progress_due_at": utc_now() - timedelta(seconds=1)},
    )
    assert worker.fire_due_deadlines("project") == 1
    prior = store.find_entities(ButlerIncident, {}, engagement_id="project")[0]
    reviewer = CitingReviewer(choice="escalate")
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))

    assert saved is not None and saved.choice == "escalate"
    incidents = store.find_entities(ButlerIncident, {}, engagement_id="project")
    assert len(incidents) == 1
    assert incidents[0].id == prior.id
    assert incidents[0].source == "goal_checkpoint"
    assert incidents[0].verification_state == "core_observed"
    assert {
        ref["id"] for ref in incidents[0].evidence_refs if ref["kind"] == "decision"
    } == {saved.id}
    event_types = [item.event_type for item in _events(store)]
    assert event_types.count("incident.opened") == 1
    assert event_types.count("incident.updated") == 1


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


@pytest.mark.parametrize("foreign_citation", [False, True])
def test_harness_reviewer_uses_project_scoped_tool_free_analysis_and_checks_citations(
    tmp_path, foreign_citation
):
    _, store, goals, goal, worker = _setup(tmp_path)
    store.create(
        HarnessProfile(
            id="codex",
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/usr/bin/codex",
            default_model="model-a",
            privacy=ProviderPrivacy(local_only=True),
        )
    )
    store.update(
        ChatSession,
        goal.session_id,
        {
            "backend": ChatBackend.HARNESS,
            "provider_profile_id": None,
            "harness_profile_id": "codex",
            "harness_session_id": "existing-codex-session",
        },
    )
    _plan_edit(goals, goal)
    chat_before = store.get(ChatSession, goal.session_id)

    class FakeHarnessRuntime:
        def __init__(self):
            self.requests = []

        async def analyze_structured(self, **request):
            self.requests.append(request)
            packet = json.loads(request["files"]["review-packet.json"])
            payload = {
                "choice": "steer",
                "summary": "The plan needs a control.",
                "reason": "The saved plan lacks a control comparison.",
                "evidence_refs": [
                    {
                        "kind": "goal",
                        "id": goal.id,
                        "project_id": "other-project"
                        if foreign_citation
                        else "project",
                    }
                ],
            }
            return SimpleNamespace(
                engagement_id=packet["project_id"],
                response=json.dumps(payload),
                usage=ChatTokenUsage(input_tokens=25, output_tokens=10),
            )

    runtime = FakeHarnessRuntime()
    reviewer = ProviderButlerReviewer(store, lambda _profile: None, runtime)
    saved = asyncio.run(review_pending(store, "project", worker.worker_id, reviewer))
    assert saved is not None
    assert saved.status == ("unavailable" if foreign_citation else "reviewed")
    assert saved.reason == (
        "invalid_evidence_citation"
        if foreign_citation
        else "The saved plan lacks a control comparison."
    )
    assert runtime.requests[0]["engagement_id"] == "project"
    assert runtime.requests[0]["profile_id"] == "codex"
    assert runtime.requests[0]["nebula_tools"] is False
    assert "Return exactly one JSON object" in runtime.requests[0]["prompt"]
    assert f'"id":"{goal.id}"' in runtime.requests[0]["prompt"]
    assert '"project_id":"project"' in runtime.requests[0]["prompt"]
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
