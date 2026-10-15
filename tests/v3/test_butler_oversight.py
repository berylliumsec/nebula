"""Focused durability and ownership checks for Core's Butler Watch foundation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
import nebula.v3.butler_oversight as oversight
from nebula.v3.butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    ButlerOversightService,
    GoalAttentionReport,
    GoalProgressReport,
    ReportEvidenceRef,
    acknowledge_incident,
    butler_router,
    ingest_agent_report,
    project_stream_id,
    project_state_id,
    watch_id,
)
from nebula.v3.butler_reporting_tools import ButlerReportBroker, report_specs
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import (
    Artifact,
    ButlerAgentReport,
    ButlerGoalWatch,
    ButlerIncident,
    ButlerProjectState,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    ScopePolicy,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from nebula.v3.tools import ToolInvocation, ToolNotPermitted
from tests.v3.test_chat import FakeProvider


def _setup(tmp_path, *, projects=("project",)):
    path = tmp_path / "butler.db"
    store = NebulaStore(path)
    for project_id in projects:
        store.create(Engagement(id=project_id, name=project_id))
        store.create(
            ProviderProfile(
                id=f"provider-{project_id}",
                name="Provider",
                provider_type="openrouter",
            )
        )
        store.create(
            ChatSession(
                id=f"session-{project_id}",
                engagement_id=project_id,
                title="Goal chat",
                provider_profile_id=f"provider-{project_id}",
                model="model",
            )
        )
    return path, store, ChatGoalService(store)


def _start(goals: ChatGoalService, project_id="project"):
    session_id = f"session-{project_id}"
    draft = goals.create(
        session_id,
        GoalCreate(
            objective=f"Research {project_id}", completion_criteria=["Evidence"]
        ),
    )
    return goals.write(
        session_id, GoalWrite(expected_revision=draft.revision, action="start")
    )


def _events(store: NebulaStore, project_id="project"):
    return store.replay_operation_events(project_stream_id(project_id))


def _turn(store: NebulaStore, goal: ChatGoal, turn_id: str) -> ChatTurn:
    return store.create(
        ChatTurn(
            id=turn_id,
            engagement_id=goal.engagement_id,
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id=f"provider-{goal.engagement_id}",
            model="model",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
        )
    )


def _attention(machine: str = "Lab.EXAMPLE.") -> GoalAttentionReport:
    return GoalAttentionReport(
        condition="machine_unreachable",
        machine=machine,
        symptom="SSH timed out.",
        impact="The active check cannot continue.",
        evidence=["Three connection attempts timed out."],
        attempted_recovery="Retried from the scoped runner.",
        urgency="urgent",
        help_requested="Confirm host availability or approve a different target.",
    )


def test_goal_transitions_commit_watch_and_project_events_then_close_after_replay(
    tmp_path,
):
    _, store, goals = _setup(tmp_path)
    signals = []
    store.on_project_oversight_change(
        lambda project_id: signals.append((project_id, len(_events(store, project_id))))
    )
    running = _start(goals)

    watch = store.get(ButlerGoalWatch, watch_id(running.id))
    assert watch.status == ChatGoalStatus.RUNNING
    assert watch.progress_due_at is not None
    assert [(item.sequence, item.event_type) for item in _events(store)] == [
        (1, "goal.started")
    ]
    assert signals == [("project", 1)]

    paused = goals.write(
        "session-project", GoalWrite(expected_revision=running.revision, action="pause")
    )
    assert store.get(ButlerGoalWatch, watch.id).progress_due_at is None
    resumed = goals.write(
        "session-project", GoalWrite(expected_revision=paused.revision, action="resume")
    )
    resumed_watch = store.get(ButlerGoalWatch, watch.id)
    assert resumed_watch.progress_due_at is not None
    assert resumed_watch.deadline_generation == 2
    cancelled = goals.write(
        "session-project",
        GoalWrite(expected_revision=resumed.revision, action="cancel"),
    )
    assert cancelled.status == ChatGoalStatus.CANCELLED
    assert store.get(ButlerGoalWatch, watch.id).closed_at is None
    assert [event.event_type for event in _events(store)] == [
        "goal.started",
        "goal.paused",
        "goal.resumed",
        "goal.cancelled",
    ]
    worker = ButlerOversightService(store)
    assert worker.replay_project("project")
    assert store.get(ButlerGoalWatch, watch.id).closed_at is not None
    assert (
        store.get(ButlerProjectState, project_state_id("project")).last_event_sequence
        == 4
    )


def test_child_joins_watch_on_start_and_parent_pause_cascades_atomically(tmp_path):
    _, store, goals = _setup(tmp_path)
    draft = goals.create(
        "session-project",
        GoalCreate(
            objective="Lead research",
            completion_criteria=["Evidence"],
            child_budget=1,
        ),
    )
    parent = goals.write(
        "session-project", GoalWrite(expected_revision=draft.revision, action="start")
    )
    child = goals.start_child(
        "session-project",
        GoalCreate(objective="Check hypothesis", completion_criteria=["Result"]),
    )
    assert store.find_entity(ButlerGoalWatch, "goal_id", child.id) is None
    child_running = goals.write(
        child.session_id, GoalWrite(expected_revision=child.revision, action="start")
    )
    assert (
        store.get(ButlerGoalWatch, watch_id(child.id)).status == ChatGoalStatus.RUNNING
    )
    assert _events(store)[-1].payload["actor_role"] == "subagent"

    parent = store.get(ChatGoal, parent.id)
    goals.write(
        "session-project", GoalWrite(expected_revision=parent.revision, action="pause")
    )
    assert store.get(ChatGoal, child_running.id).status == ChatGoalStatus.PAUSED
    assert store.get(ButlerGoalWatch, watch_id(child.id)).progress_due_at is None
    assert [event.sequence for event in _events(store)] == [1, 2, 3, 4]
    assert [event.event_type for event in _events(store)][-2:] == [
        "goal.paused",
        "goal.paused",
    ]


def test_event_failure_rolls_back_goal_and_watch_and_does_not_wake(
    tmp_path, monkeypatch
):
    _, store, goals = _setup(tmp_path)
    draft = goals.create(
        "session-project",
        GoalCreate(objective="Research", completion_criteria=["Evidence"]),
    )
    signals = []
    store.on_project_oversight_change(signals.append)

    def fail_append(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr(oversight, "_append_project_event", fail_append)
    with pytest.raises(RuntimeError, match="event storage unavailable"):
        goals.write(
            "session-project",
            GoalWrite(expected_revision=draft.revision, action="start"),
        )
    assert store.get(ChatGoal, draft.id).status == ChatGoalStatus.DRAFT
    assert store.find_entity(ButlerGoalWatch, "goal_id", draft.id) is None
    assert _events(store) == []
    assert signals == []


def test_restart_restores_missing_watches_and_replays_unseen_events(tmp_path):
    path, store, goals = _setup(tmp_path)
    running = _start(goals)
    first = ButlerOversightService(store)
    assert first.replay_project("project")
    paused = goals.write(
        "session-project", GoalWrite(expected_revision=running.revision, action="pause")
    )
    assert paused.status == ChatGoalStatus.PAUSED

    restarted_store = NebulaStore(path)
    restarted = ButlerOversightService(restarted_store)
    assert restarted.replay_project("project") is False  # first lease is still held
    restarted_store.update(
        ButlerProjectState,
        project_state_id("project"),
        {"lease_expires_at": utc_now() - timedelta(seconds=1)},
    )
    assert restarted.replay_project("project")
    assert (
        restarted_store.get(
            ButlerProjectState, project_state_id("project")
        ).last_event_sequence
        == 2
    )

    # Upgrade/recovery case: a goal row predates Watch and has no transition event.
    legacy = restarted_store.create(
        ChatGoal(
            id="legacy-goal",
            engagement_id="project",
            session_id="session-project",
            objective="Legacy research",
            completion_criteria=["Evidence"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    restarted.reconcile_watches()
    recovered = restarted_store.get(ButlerGoalWatch, watch_id(legacy.id))
    assert recovered.status == ChatGoalStatus.RUNNING
    assert recovered.progress_due_at is not None
    assert "restored" in recovered.coverage_reason
    assert _events(restarted_store)[-1].event_type == "goal.watch_recovered"
    restarted.reconcile_watches()
    assert len(_events(restarted_store)) == 3


def test_expired_worker_cannot_emit_deadline_after_takeover(tmp_path):
    _, store, goals = _setup(tmp_path)
    running = _start(goals)
    old_worker = ButlerOversightService(store)
    new_worker = ButlerOversightService(store)
    assert old_worker.replay_project("project")
    store.update(
        ButlerGoalWatch,
        watch_id(running.id),
        {"progress_due_at": utc_now() - timedelta(seconds=1)},
    )
    assert new_worker.replay_project("project") is False
    store.update(
        ButlerProjectState,
        project_state_id("project"),
        {"lease_expires_at": utc_now() - timedelta(seconds=1)},
    )
    assert new_worker.replay_project("project")
    assert old_worker.fire_due_deadlines("project") == 0
    assert new_worker.fire_due_deadlines("project") == 1
    assert new_worker.fire_due_deadlines("project") == 0
    assert [event.event_type for event in _events(store)].count(
        "goal.progress_overdue"
    ) == 1
    assert new_worker.replay_project("project")
    assert (
        store.get(ButlerProjectState, project_state_id("project")).last_event_sequence
        == 3
    )


def test_overdue_checkpoint_is_a_durable_incident_until_progress_report(tmp_path):
    path, store, goals = _setup(tmp_path)
    goal = _start(goals)
    worker = ButlerOversightService(store)
    assert worker.replay_project("project")
    store.update(
        ButlerGoalWatch,
        watch_id(goal.id),
        {"progress_due_at": utc_now() - timedelta(seconds=1)},
    )
    assert worker.fire_due_deadlines("project") == 1
    assert worker.fire_due_deadlines("project") == 0
    incidents = store.find_entities(
        ButlerIncident, {"source": "goal_checkpoint"}, engagement_id="project", limit=10
    )
    assert len(incidents) == 1
    assert incidents[0].status == "open"
    assert incidents[0].verification_state == "core_observed"
    assert [event.event_type for event in _events(store)][-2:] == [
        "goal.progress_overdue",
        "incident.opened",
    ]

    paused = goals.write(
        goal.session_id, GoalWrite(expected_revision=goal.revision, action="pause")
    )
    assert store.get(ButlerIncident, incidents[0].id).status == "open"
    resumed = goals.write(
        goal.session_id, GoalWrite(expected_revision=paused.revision, action="resume")
    )
    blocked = goals.write(
        goal.session_id,
        GoalWrite(expected_revision=resumed.revision, action="block", reason="Waiting"),
    )
    assert store.get(ButlerIncident, incidents[0].id).status == "open"
    goals.write(
        goal.session_id, GoalWrite(expected_revision=blocked.revision, action="resume")
    )

    restarted_store = NebulaStore(path)
    app = FastAPI()
    app.include_router(
        butler_router(ButlerOversightService(restarted_store)), prefix="/api/v1"
    )
    client = TestClient(app)
    overview = client.get("/api/v1/engagements/project/butler/overview").json()
    assert overview["summary"]["open_incidents"] == 1
    assert overview["incidents"][0]["id"] == incidents[0].id
    turn = _turn(restarted_store, goal, "turn-after-overdue")
    ingest_agent_report(
        restarted_store,
        turn_id=turn.id,
        idempotency_key="progress-after-overdue",
        body=GoalProgressReport(
            what_changed="Checked the second input path.",
            evidence_gained=["The probe completed."],
            criterion_advanced="Documented the result.",
            next_step="Continue with the remaining path.",
        ),
        source="provider_tool",
    )
    assert restarted_store.get(ButlerIncident, incidents[0].id).status == "resolved"
    assert (
        client.get("/api/v1/engagements/project/butler/overview").json()["summary"][
            "open_incidents"
        ]
        == 0
    )


def test_concurrent_goal_starts_get_one_ordered_project_stream(tmp_path):
    _, store, goals = _setup(tmp_path)
    first = goals.create(
        "session-project",
        GoalCreate(objective="First", completion_criteria=["Evidence"]),
    )
    store.create(
        ChatSession(
            id="session-second",
            engagement_id="project",
            title="Second goal chat",
            provider_profile_id="provider-project",
            model="model",
        )
    )
    second = goals.create(
        "session-second",
        GoalCreate(objective="Second", completion_criteria=["Evidence"]),
    )

    def start(session_id, revision):
        return goals.write(
            session_id, GoalWrite(expected_revision=revision, action="start")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: start(*item),
                [
                    ("session-project", first.revision),
                    ("session-second", second.revision),
                ],
            )
        )
    assert len(results) == 2
    events = _events(store)
    assert [item.sequence for item in events] == [1, 2]
    assert {item.payload["goal_id"] for item in events} == {first.id, second.id}


def test_project_overview_and_event_pages_do_not_expose_other_project(tmp_path):
    _, store, goals = _setup(tmp_path, projects=("alpha", "beta"))
    alpha = _start(goals, "alpha")
    beta = _start(goals, "beta")
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)

    overview = client.get("/api/v1/engagements/alpha/butler/overview").json()
    assert overview["authority"] == "watch"
    assert overview["capabilities"]["incidents"] is True
    assert overview["watched_goals"][0]["goal_id"] == alpha.id
    assert overview["watched_goals"][0]["actor_role"] == "main_agent"
    assert (
        overview["watched_goals"][0]["coverage"]["report_capability"] == "unavailable"
    )
    assert beta.id not in str(overview)

    page = client.get("/api/v1/engagements/alpha/butler/events").json()
    assert [event["payload"]["goal_id"] for event in page["events"]] == [alpha.id]
    assert page["events"][0]["payload"]["project_id"] == "alpha"


def test_event_packet_rejects_foreign_or_oversize_evidence():
    with pytest.raises(ValueError, match="event evidence must belong"):
        ButlerEventPayload(
            project_id="alpha",
            source="core",
            actor_role="core",
            summary="Observation",
            verification_state="core_observed",
            evidence_refs=[ButlerEvidenceRef(kind="goal", id="g", project_id="beta")],
        )
    with pytest.raises(ValueError):
        ButlerEventPayload(
            project_id="alpha",
            source="core",
            actor_role="core",
            summary="Observation",
            verification_state="core_observed",
            evidence_refs=[
                ButlerEvidenceRef(kind="goal", id=str(i), project_id="alpha")
                for i in range(11)
            ],
        )


def test_progress_report_reschedules_bounded_deadline_and_replay_is_idempotent(
    tmp_path,
):
    _, store, goals = _setup(tmp_path)
    goal = _start(goals)
    turn = _turn(store, goal, "turn-progress")
    before = store.get(ButlerGoalWatch, watch_id(goal.id))
    body = GoalProgressReport(
        what_changed="Ruled out the first hypothesis.",
        evidence_gained=["The repeatable probe returned no matching response."],
        criterion_advanced="Distinguish candidate input paths.",
        next_step="Try the second input path.",
        next_checkpoint_at=utc_now() + timedelta(hours=8),
        evidence_refs=[ReportEvidenceRef(kind="chat_turn", id=turn.id)],
    )
    report, incident = ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="checkpoint-1",
        body=body,
        source="provider_tool",
        expected_project_id="project",
        expected_session_id=goal.session_id,
    )
    assert incident is None
    assert report.actor_role == "main_agent"
    assert report.evidence_refs == [
        {"kind": "chat_turn", "id": turn.id, "project_id": "project"}
    ]
    after = store.get(ButlerGoalWatch, watch_id(goal.id))
    assert after.deadline_generation == before.deadline_generation + 1
    assert after.last_progress_at is not None
    assert (
        timedelta(hours=1, minutes=59)
        < after.progress_due_at - after.last_progress_at
        <= timedelta(hours=2)
    )
    assert after.report_capability == "validated"
    assert _events(store)[-1].event_type == "agent.progress_reported"
    assert _events(store)[-1].payload["verification_state"] == "agent_reported"

    sequence = len(_events(store))
    same, _ = ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="checkpoint-1",
        body=body,
        source="provider_tool",
    )
    assert same.id == report.id
    assert len(_events(store)) == sequence
    assert store.get(ButlerGoalWatch, watch_id(goal.id)).revision == after.revision
    with pytest.raises(ConflictError, match="different content"):
        ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="checkpoint-1",
            body=body.model_copy(update={"next_step": "Different action"}),
            source="provider_tool",
        )


def test_two_goal_agents_share_one_agent_reported_machine_incident(tmp_path):
    path, store, goals = _setup(tmp_path)
    draft = goals.create(
        "session-project",
        GoalCreate(
            objective="Lead research", completion_criteria=["Evidence"], child_budget=1
        ),
    )
    parent = goals.write(
        "session-project", GoalWrite(expected_revision=draft.revision, action="start")
    )
    child = goals.start_child(
        "session-project",
        GoalCreate(objective="Check host", completion_criteria=["Reach host"]),
    )
    child = goals.write(
        child.session_id, GoalWrite(expected_revision=child.revision, action="start")
    )
    parent_turn = _turn(store, parent, "turn-parent")
    child_turn = _turn(store, child, "turn-child")
    first, opened = ingest_agent_report(
        store,
        turn_id=parent_turn.id,
        idempotency_key="parent-observation",
        body=_attention("Lab.EXAMPLE."),
        source="provider_tool",
    )
    second, updated = ingest_agent_report(
        store,
        turn_id=child_turn.id,
        idempotency_key="child-observation",
        body=_attention("lab.example"),
        source="provider_tool",
    )
    assert opened is not None and updated is not None
    assert opened.id == updated.id
    assert updated.incident_key == "machine:lab.example"
    assert updated.source == "agent_report"
    assert updated.verification_state == "agent_reported"
    assert updated.status == "open"
    assert updated.evidence_summary == [
        "Agent-reported symptom: SSH timed out.",
        "Agent-reported evidence: Three connection attempts timed out.",
        "Agent-reported recovery attempt: Retried from the scoped runner.",
    ]
    assert set(updated.goal_ids) == {parent.id, child.id}
    assert set(updated.session_ids) == {parent.session_id, child.session_id}
    assert {item["id"] for item in updated.evidence_refs} == {first.id, second.id}
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1
    assert second.actor_role == "subagent"
    assert [event.event_type for event in _events(store)].count("incident.opened") == 1
    assert [event.event_type for event in _events(store)].count("incident.updated") == 1

    restarted = NebulaStore(path)
    duplicate, incident = ingest_agent_report(
        restarted,
        turn_id=child_turn.id,
        idempotency_key="child-observation",
        body=_attention("lab.example"),
        source="provider_tool",
    )
    assert duplicate.id == second.id
    assert incident is not None and incident.id == opened.id
    assert incident.evidence_summary == updated.evidence_summary
    assert restarted.count_entities(ButlerIncident, engagement_id="project") == 1
    with pytest.raises(ValueError, match="evidence summaries"):
        ButlerIncident(
            engagement_id="project",
            incident_key="machine:other",
            source="agent_report",
            verification_state="agent_reported",
            summary="Agent report",
            evidence_summary=["x" * 301],
        )


def test_concurrent_agent_reports_deduplicate_machine_incident(tmp_path):
    _, store, goals = _setup(tmp_path)
    first_goal = _start(goals)
    store.create(
        ChatSession(
            id="session-peer",
            engagement_id="project",
            title="Peer work",
            provider_profile_id="provider-project",
            model="model",
        )
    )
    peer_draft = goals.create(
        "session-peer",
        GoalCreate(objective="Check shared host", completion_criteria=["Reach host"]),
    )
    peer_goal = goals.write(
        "session-peer", GoalWrite(expected_revision=peer_draft.revision, action="start")
    )
    first_turn = _turn(store, first_goal, "concurrent-turn-first")
    peer_turn = _turn(store, peer_goal, "concurrent-turn-peer")

    def report(turn_id: str):
        return ingest_agent_report(
            store,
            turn_id=turn_id,
            idempotency_key=f"machine-report-{turn_id}",
            body=_attention("LAB.example"),
            source="provider_tool",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(report, [first_turn.id, peer_turn.id]))
    assert len({incident.id for _, incident in results if incident is not None}) == 1
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1
    assert [event.event_type for event in _events(store)].count("incident.opened") == 1
    assert [event.event_type for event in _events(store)].count("incident.updated") == 1


def test_report_identity_and_evidence_are_bound_to_core_project(tmp_path):
    _, store, goals = _setup(tmp_path, projects=("alpha", "beta"))
    goal = _start(goals, "alpha")
    turn = _turn(store, goal, "turn-alpha")
    foreign = store.create(
        Artifact(
            id="foreign-artifact",
            engagement_id="beta",
            sha256="a" * 64,
            size=1,
            storage_path="stored",
        )
    )
    body = GoalProgressReport(
        what_changed="A test completed.",
        evidence_gained=["The tool returned one observation."],
        criterion_advanced="Check the candidate path.",
        next_step="Review the result.",
        evidence_refs=[ReportEvidenceRef(kind="artifact", id=foreign.id)],
    )
    before = len(_events(store, "alpha"))
    with pytest.raises(ConflictError, match="another project"):
        ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="foreign-evidence",
            body=body,
            source="provider_tool",
        )
    assert store.count_entities(ButlerAgentReport, engagement_id="alpha") == 0
    assert len(_events(store, "alpha")) == before
    with pytest.raises(ConflictError, match="does not match"):
        ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="forged-project",
            body=body.model_copy(update={"evidence_refs": []}),
            source="provider_tool",
            expected_project_id="beta",
        )


def test_acknowledgment_is_project_scoped_idempotent_and_survives_new_report(tmp_path):
    path, store, goals = _setup(tmp_path, projects=("alpha", "beta"))
    goal = _start(goals, "alpha")
    turn = _turn(store, goal, "turn-alert")
    _, opened = ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="alert-1",
        body=_attention(),
        source="provider_tool",
    )
    assert opened is not None
    with pytest.raises(NotFoundError):
        acknowledge_incident(
            store,
            engagement_id="beta",
            record_id=opened.id,
            expected_revision=opened.revision,
        )
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)
    route = f"/api/v1/engagements/alpha/butler/incidents/{opened.id}/acknowledge"
    response = client.post(route, json={"expected_revision": opened.revision})
    assert response.status_code == 200
    acknowledged = response.json()
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_at"] is not None
    sequence = len(_events(store, "alpha"))
    duplicate = client.post(route, json={"expected_revision": opened.revision})
    assert duplicate.status_code == 200
    assert duplicate.json()["revision"] == acknowledged["revision"]
    assert len(_events(store, "alpha")) == sequence

    reopened_store = NebulaStore(path)
    assert reopened_store.get(ButlerIncident, opened.id).status == "acknowledged"
    _turn(reopened_store, goal, "turn-alert-2")
    _, updated = ingest_agent_report(
        reopened_store,
        turn_id="turn-alert-2",
        idempotency_key="alert-2",
        body=_attention(),
        source="provider_tool",
    )
    assert updated is not None
    assert updated.id == opened.id
    assert updated.status == "acknowledged"
    assert len(updated.evidence_refs) == 2


def test_report_tool_rejects_non_agent_invocation_and_binds_saved_turn(tmp_path):
    _, store, goals = _setup(tmp_path)
    goal = _start(goals)
    turn = _turn(store, goal, "turn-tool")
    assert set(report_specs()) == {"report_progress", "report_attention"}
    scope = ScopePolicy(id="scope", engagement_id="project")
    invocation = ToolInvocation(
        engagement_id="project",
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=goal.session_id,
        chat_turn_id=turn.id,
        tool_name="report_attention",
        arguments=_attention().model_dump(mode="json"),
        workspace=Path(tmp_path),
        idempotency_key="tool-report",
        requested_by="chat-assistant",
    )
    broker = ButlerReportBroker(store)
    accepted = asyncio.run(broker.execute(invocation, scope))
    assert accepted.output["accepted"] is True
    assert accepted.output["incident_id"] is not None
    with pytest.raises(ToolNotPermitted):
        asyncio.run(
            broker.execute(
                invocation.model_copy(update={"requested_by": "operator"}), scope
            )
        )
    with pytest.raises(ConflictError, match="does not match"):
        asyncio.run(
            broker.execute(
                invocation.model_copy(update={"engagement_id": "other"}), scope
            )
        )
    store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.COMPLETE})
    with pytest.raises(ConflictError, match="active tool turn"):
        asyncio.run(
            broker.execute(
                invocation.model_copy(update={"idempotency_key": "late-report"}), scope
            )
        )


def test_provider_goal_turn_offers_core_reports_and_ingests_machine_alert(
    tmp_path, monkeypatch
):
    _, store, goals = _setup(tmp_path)
    goal = _start(goals)
    profile = store.get(ProviderProfile, "provider-project")
    capabilities = profile.capabilities.model_dump(mode="python")
    capabilities["tool_calling"] = True
    store.update(
        ProviderProfile,
        profile.id,
        {
            "capabilities": capabilities,
            "capability_verifications": {
                "model": {"model": "model", "status": "verified"}
            },
        },
    )
    provider = FakeProvider(profile.id, local=True)
    provider.config.default_model = "model"
    provider.config.model_allowlist = ["model"]
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "check" / "SKILL.md"
    resource_path = skill_path.parent / "references" / "note.md"
    resource_path.parent.mkdir(parents=True)
    skill_path.write_text("Read [the note](references/note.md).", encoding="utf-8")
    resource_path.write_text("Evidence goes here.", encoding="utf-8")
    chat = ChatService(store, workspace_resolver=lambda _: workspace)
    prepared = chat.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id="project",
            session_id=goal.session_id,
            goal_id=goal.id,
            skill={"name": "check", "path": str(skill_path.resolve())},
            messages=[{"role": "user", "content": "Check the host"}],
            include_knowledge=False,
            stream=True,
        )
    )
    assert prepared.turn is not None
    assert prepared.tools_enabled is True
    assert {"report_progress", "report_attention"} <= set(
        prepared.tool_components.specs
    )
    assert "use report_progress" in prepared.model_request.instructions
    assert {"report_progress", "report_attention"} <= set(
        prepared.turn.request_snapshot["available_tool_names"]
    )
    # Preparation persists the turn as queued; the real scheduler moves it
    # to routing before any agent tool call can execute.
    store.update(ChatTurn, prepared.turn.id, {"status": ChatTurnStatus.ROUTING})
    result = asyncio.run(
        prepared.tool_components.broker.execute(
            ToolInvocation(
                engagement_id="project",
                run_id=prepared.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=goal.session_id,
                chat_turn_id=prepared.turn.id,
                tool_name="report_attention",
                arguments=_attention().model_dump(mode="json"),
                workspace=workspace,
                idempotency_key="real-path-report",
                requested_by="chat-assistant",
            ),
            prepared.tool_components.scope,
        )
    )
    assert result.output["incident_id"] is not None
    assert store.count_entities(ButlerIncident, engagement_id="project") == 1


def test_overview_pages_watches_and_prioritizes_open_incidents(tmp_path, monkeypatch):
    _, store, goals = _setup(tmp_path)
    first = _start(goals)
    monkeypatch.setattr(oversight, "OVERVIEW_PAGE_SIZE", 2)
    for index in range(2):
        goal = store.create(
            ChatGoal(
                id=f"extra-goal-{index}",
                engagement_id="project",
                session_id="session-project",
                objective=f"Extra {index}",
                completion_criteria=["Evidence"],
                status=ChatGoalStatus.RUNNING,
            )
        )
        store.create(
            ButlerGoalWatch(
                id=watch_id(goal.id),
                engagement_id="project",
                goal_id=goal.id,
                session_id=goal.session_id,
                status=goal.status,
                coverage_reason="Lifecycle only",
            )
        )
    for index, status in enumerate(("acknowledged", "open", "open")):
        store.create(
            ButlerIncident(
                id=f"incident-{index}",
                engagement_id="project",
                incident_key=f"machine:host-{index}",
                status=status,
                source="agent_report",
                verification_state="agent_reported",
                summary=f"Host {index} unreachable",
            )
        )
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)
    route = "/api/v1/engagements/project/butler/overview"
    first_page = client.get(route).json()
    assert first_page["summary"] == {
        "watched_goals": 3,
        "open_incidents": 2,
        "acknowledged_incidents": 1,
        "resolved_incidents": 0,
        "account_resources": 0,
        "harness_quota_resources": 0,
        "decisions": 0,
        "actions": 0,
    }
    assert len(first_page["watched_goals"]) == 2
    assert len(first_page["incidents"]) == 2
    assert {incident["status"] for incident in first_page["incidents"]} == {"open"}
    assert first_page["next_watch_offset"] == 2
    assert first_page["next_incident_offset"] == 2
    second_page = client.get(
        route, params={"watch_offset": 2, "incident_offset": 2}
    ).json()
    assert [item["goal_id"] for item in second_page["watched_goals"]] == [first.id]
    assert [item["status"] for item in second_page["incidents"]] == ["acknowledged"]
    assert second_page["next_watch_offset"] is None
    assert second_page["next_incident_offset"] is None
