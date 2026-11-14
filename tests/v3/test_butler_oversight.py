"""Focused durability and ownership checks for Core's Butler Watch foundation."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta
from pathlib import Path

import asyncio
import pytest
from fastapi import FastAPI
from tests.v3.async_test_support import AsyncTestClient as TestClient

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
from nebula.v3.butler_recovery import ButlerRecoveryAgent
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import (
    Artifact,
    ButlerAgentReport,
    ButlerGoalWatch,
    ButlerIncident,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
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


async def _setup(tmp_path, *, projects=("project",)):
    path = tmp_path / "butler.db"
    store = NebulaStore(path)
    for project_id in projects:
        (await store.create(Engagement(id=project_id, name=project_id)))
        (
            await store.create(
                ProviderProfile(
                    id=f"provider-{project_id}",
                    name="Provider",
                    provider_type="openrouter",
                )
            )
        )
        (
            await store.create(
                ChatSession(
                    id=f"session-{project_id}",
                    engagement_id=project_id,
                    title="Goal chat",
                    provider_profile_id=f"provider-{project_id}",
                    model="model",
                )
            )
        )
    return path, store, ChatGoalService(store)


async def _start(goals: ChatGoalService, project_id="project"):
    session_id = f"session-{project_id}"
    draft = await goals.create(
        session_id,
        GoalCreate(
            objective=f"Research {project_id}", completion_criteria=["Evidence"]
        ),
    )
    return await goals.write(
        session_id, GoalWrite(expected_revision=draft.revision, action="start")
    )


async def _events(store: NebulaStore, project_id="project"):
    return await store.replay_operation_events(project_stream_id(project_id))


async def test_scheduled_check_overview_links_saved_turn_and_recovery_report(tmp_path):
    _, store, _ = await _setup(tmp_path)
    session = await store.create(
        ChatSession(
            id="session-recovery",
            engagement_id="project",
            title="Recovery",
            provider_profile_id="provider-project",
            model="model",
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id="project",
            session_id=session.id,
            objective="Recover the paused work",
            completion_criteria=["Report evidence"],
            plan=["Inspect the blocker"],
            metadata={"butler_recovery_source_goal_id": "source-goal"},
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id="project",
                session_id=session.id,
                sequence=1,
                role=ChatRole.ASSISTANT,
                content="The source goal can resume after checking its revision.",
            )
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id="project",
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="scheduled-event",
            source_event_sequence=1,
            authority_revision=1,
            status="resolved",
            summary="Scheduled work check",
            recovery_goal_ids=[goal.id],
        )
    )
    (
        await ButlerRecoveryAgent(
            store,
            provider_factory=lambda _: None,
            harness_runtime=None,
            artifact_store=None,
            work=None,
            memory=None,
        )._record_model_turn(
            case.id, ChatBackend.HARNESS, "profile", "model", "saved-harness-turn"
        )
    )
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    body = (
        await TestClient(app).get("/api/v1/engagements/project/butler/overview")
    ).json()
    saved = next(item for item in body["recovery_cases"] if item["id"] == case.id)
    assert saved["model_turn_id"] == "saved-harness-turn"
    assert saved["recovery_goals"][0]["source_goal_id"] == "source-goal"
    assert (
        saved["recovery_goals"][0]["report"]
        == "The source goal can resume after checking its revision."
    )


async def _turn(store: NebulaStore, goal: ChatGoal, turn_id: str) -> ChatTurn:
    return await store.create(
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


async def test_goal_transitions_commit_watch_and_project_events_then_close_after_replay(
    tmp_path,
):
    _, store, goals = await _setup(tmp_path)
    signals = []

    async def record_signal(project_id):
        signals.append((project_id, len(await _events(store, project_id))))

    store.on_project_oversight_change(record_signal)
    running = await _start(goals)

    watch = await store.get(ButlerGoalWatch, watch_id(running.id))
    assert watch.status == ChatGoalStatus.RUNNING
    assert watch.progress_due_at is not None
    assert [(item.sequence, item.event_type) for item in (await _events(store))] == [
        (1, "goal.started")
    ]
    assert signals == [("project", 1)]

    paused = await goals.write(
        "session-project", GoalWrite(expected_revision=running.revision, action="pause")
    )
    assert (await store.get(ButlerGoalWatch, watch.id)).progress_due_at is None
    resumed = await goals.write(
        "session-project", GoalWrite(expected_revision=paused.revision, action="resume")
    )
    resumed_watch = await store.get(ButlerGoalWatch, watch.id)
    assert resumed_watch.progress_due_at is not None
    assert resumed_watch.deadline_generation == 2
    cancelled = await goals.write(
        "session-project",
        GoalWrite(expected_revision=resumed.revision, action="cancel"),
    )
    assert cancelled.status == ChatGoalStatus.CANCELLED
    assert (await store.get(ButlerGoalWatch, watch.id)).closed_at is None
    assert [event.event_type for event in (await _events(store))] == [
        "goal.started",
        "goal.paused",
        "goal.resumed",
        "goal.cancelled",
    ]
    worker = ButlerOversightService(store)
    assert await worker.replay_project("project")
    assert (await store.get(ButlerGoalWatch, watch.id)).closed_at is not None
    assert (
        await store.get(ButlerProjectState, project_state_id("project"))
    ).last_event_sequence == 4


async def test_child_joins_watch_on_start_and_parent_pause_cascades_atomically(
    tmp_path,
):
    _, store, goals = await _setup(tmp_path)
    draft = await goals.create(
        "session-project",
        GoalCreate(
            objective="Lead research",
            completion_criteria=["Evidence"],
            child_budget=1,
        ),
    )
    parent = await goals.write(
        "session-project", GoalWrite(expected_revision=draft.revision, action="start")
    )
    child = await goals.start_child(
        "session-project",
        GoalCreate(objective="Check hypothesis", completion_criteria=["Result"]),
    )
    assert (await store.find_entity(ButlerGoalWatch, "goal_id", child.id)) is None
    child_running = await goals.write(
        child.session_id, GoalWrite(expected_revision=child.revision, action="start")
    )
    assert (
        await store.get(ButlerGoalWatch, watch_id(child.id))
    ).status == ChatGoalStatus.RUNNING
    assert (await _events(store))[-1].payload["actor_role"] == "subagent"

    parent = await store.get(ChatGoal, parent.id)
    (
        await goals.write(
            "session-project",
            GoalWrite(expected_revision=parent.revision, action="pause"),
        )
    )
    assert (await store.get(ChatGoal, child_running.id)).status == ChatGoalStatus.PAUSED
    assert (
        await store.get(ButlerGoalWatch, watch_id(child.id))
    ).progress_due_at is None
    assert [event.sequence for event in (await _events(store))] == [1, 2, 3, 4]
    assert [event.event_type for event in (await _events(store))][-2:] == [
        "goal.paused",
        "goal.paused",
    ]


async def test_event_failure_rolls_back_goal_and_watch_and_does_not_wake(
    tmp_path, monkeypatch
):
    _, store, goals = await _setup(tmp_path)
    draft = await goals.create(
        "session-project",
        GoalCreate(objective="Research", completion_criteria=["Evidence"]),
    )
    signals = []
    store.on_project_oversight_change(signals.append)

    def fail_append(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr(oversight, "_append_project_event", fail_append)
    with pytest.raises(RuntimeError, match="event storage unavailable"):
        (
            await goals.write(
                "session-project",
                GoalWrite(expected_revision=draft.revision, action="start"),
            )
        )
    assert (await store.get(ChatGoal, draft.id)).status == ChatGoalStatus.DRAFT
    assert (await store.find_entity(ButlerGoalWatch, "goal_id", draft.id)) is None
    assert (await _events(store)) == []
    assert signals == []


async def test_restart_restores_missing_watches_and_replays_unseen_events(tmp_path):
    path, store, goals = await _setup(tmp_path)
    running = await _start(goals)
    first = ButlerOversightService(store)
    assert await first.replay_project("project")
    paused = await goals.write(
        "session-project", GoalWrite(expected_revision=running.revision, action="pause")
    )
    assert paused.status == ChatGoalStatus.PAUSED

    restarted_store = NebulaStore(path)
    restarted = ButlerOversightService(restarted_store)
    assert (
        await restarted.replay_project("project")
    ) is False  # first lease is still held
    (
        await restarted_store.update(
            ButlerProjectState,
            project_state_id("project"),
            {"lease_expires_at": utc_now() - timedelta(seconds=1)},
        )
    )
    assert await restarted.replay_project("project")
    assert (
        await restarted_store.get(ButlerProjectState, project_state_id("project"))
    ).last_event_sequence == 2

    # Upgrade/recovery case: a goal row predates Watch and has no transition event.
    legacy = await restarted_store.create(
        ChatGoal(
            id="legacy-goal",
            engagement_id="project",
            session_id="session-project",
            objective="Legacy research",
            completion_criteria=["Evidence"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    (await restarted.reconcile_watches())
    recovered = await restarted_store.get(ButlerGoalWatch, watch_id(legacy.id))
    assert recovered.status == ChatGoalStatus.RUNNING
    assert recovered.progress_due_at is not None
    assert "restored" in recovered.coverage_reason
    assert (await _events(restarted_store))[-1].event_type == "goal.watch_recovered"
    (await restarted.reconcile_watches())
    assert len((await _events(restarted_store))) == 3


async def test_expired_worker_cannot_emit_deadline_after_takeover(tmp_path):
    _, store, goals = await _setup(tmp_path)
    running = await _start(goals)
    old_worker = ButlerOversightService(store)
    new_worker = ButlerOversightService(store)
    assert await old_worker.replay_project("project")
    (
        await store.update(
            ButlerGoalWatch,
            watch_id(running.id),
            {"progress_due_at": utc_now() - timedelta(seconds=1)},
        )
    )
    assert (await new_worker.replay_project("project")) is False
    (
        await store.update(
            ButlerProjectState,
            project_state_id("project"),
            {"lease_expires_at": utc_now() - timedelta(seconds=1)},
        )
    )
    assert await new_worker.replay_project("project")
    assert (await old_worker.fire_due_deadlines("project")) == 0
    assert (await new_worker.fire_due_deadlines("project")) == 1
    assert (await new_worker.fire_due_deadlines("project")) == 0
    assert [event.event_type for event in (await _events(store))].count(
        "goal.progress_overdue"
    ) == 1
    assert await new_worker.replay_project("project")
    assert (
        await store.get(ButlerProjectState, project_state_id("project"))
    ).last_event_sequence == 3


async def test_overdue_checkpoint_is_a_durable_incident_until_progress_report(tmp_path):
    path, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    worker = ButlerOversightService(store)
    assert await worker.replay_project("project")
    (
        await store.update(
            ButlerGoalWatch,
            watch_id(goal.id),
            {"progress_due_at": utc_now() - timedelta(seconds=1)},
        )
    )
    assert (await worker.fire_due_deadlines("project")) == 1
    assert (await worker.fire_due_deadlines("project")) == 0
    incidents = await store.find_entities(
        ButlerIncident, {"source": "goal_checkpoint"}, engagement_id="project", limit=10
    )
    assert len(incidents) == 1
    assert incidents[0].status == "open"
    assert incidents[0].verification_state == "core_observed"
    assert [event.event_type for event in (await _events(store))][-2:] == [
        "goal.progress_overdue",
        "incident.opened",
    ]

    paused = await goals.write(
        goal.session_id, GoalWrite(expected_revision=goal.revision, action="pause")
    )
    assert (await store.get(ButlerIncident, incidents[0].id)).status == "open"
    resumed = await goals.write(
        goal.session_id, GoalWrite(expected_revision=paused.revision, action="resume")
    )
    blocked = await goals.write(
        goal.session_id,
        GoalWrite(expected_revision=resumed.revision, action="block", reason="Waiting"),
    )
    assert (await store.get(ButlerIncident, incidents[0].id)).status == "open"
    (
        await goals.write(
            goal.session_id,
            GoalWrite(expected_revision=blocked.revision, action="resume"),
        )
    )

    restarted_store = NebulaStore(path)
    app = FastAPI()
    app.include_router(
        butler_router(ButlerOversightService(restarted_store)), prefix="/api/v1"
    )
    client = TestClient(app)
    overview = (await client.get("/api/v1/engagements/project/butler/overview")).json()
    assert overview["summary"]["open_incidents"] == 1
    assert overview["incidents"][0]["id"] == incidents[0].id
    turn = await _turn(restarted_store, goal, "turn-after-overdue")
    (
        await ingest_agent_report(
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
    )
    assert (
        await restarted_store.get(ButlerIncident, incidents[0].id)
    ).status == "resolved"
    assert (await client.get("/api/v1/engagements/project/butler/overview")).json()[
        "summary"
    ]["open_incidents"] == 0


async def test_concurrent_goal_starts_get_one_ordered_project_stream(tmp_path):
    _, store, goals = await _setup(tmp_path)
    first = await goals.create(
        "session-project",
        GoalCreate(objective="First", completion_criteria=["Evidence"]),
    )
    (
        await store.create(
            ChatSession(
                id="session-second",
                engagement_id="project",
                title="Second goal chat",
                provider_profile_id="provider-project",
                model="model",
            )
        )
    )
    second = await goals.create(
        "session-second",
        GoalCreate(objective="Second", completion_criteria=["Evidence"]),
    )

    async def start(session_id, revision):
        return await goals.write(
            session_id, GoalWrite(expected_revision=revision, action="start")
        )

    results = list(
        (
            await asyncio.gather(
                *(
                    (lambda item: start(*item))(_item)
                    for _item in [
                        ("session-project", first.revision),
                        ("session-second", second.revision),
                    ]
                )
            )
        )
    )
    assert len(results) == 2
    events = await _events(store)
    assert [item.sequence for item in events] == [1, 2]
    assert {item.payload["goal_id"] for item in events} == {first.id, second.id}


async def test_project_overview_and_event_pages_do_not_expose_other_project(tmp_path):
    _, store, goals = await _setup(tmp_path, projects=("alpha", "beta"))
    alpha = await _start(goals, "alpha")
    beta = await _start(goals, "beta")
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)

    overview = (await client.get("/api/v1/engagements/alpha/butler/overview")).json()
    assert overview["authority"] == "watch"
    assert overview["capabilities"]["incidents"] is True
    assert overview["watched_goals"][0]["goal_id"] == alpha.id
    assert overview["watched_goals"][0]["actor_role"] == "main_agent"
    assert (
        overview["watched_goals"][0]["coverage"]["report_capability"]
        == "turn_output_only"
    )
    assert beta.id not in str(overview)

    page = (await client.get("/api/v1/engagements/alpha/butler/events")).json()
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


async def test_progress_report_reschedules_bounded_deadline_and_replay_is_idempotent(
    tmp_path,
):
    _, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    turn = await _turn(store, goal, "turn-progress")
    before = await store.get(ButlerGoalWatch, watch_id(goal.id))
    body = GoalProgressReport(
        what_changed="Ruled out the first hypothesis.",
        evidence_gained=["The repeatable probe returned no matching response."],
        criterion_advanced="Distinguish candidate input paths.",
        next_step="Try the second input path.",
        next_checkpoint_at=utc_now() + timedelta(hours=8),
        evidence_refs=[ReportEvidenceRef(kind="chat_turn", id=turn.id)],
    )
    report, incident = await ingest_agent_report(
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
    after = await store.get(ButlerGoalWatch, watch_id(goal.id))
    assert after.deadline_generation == before.deadline_generation + 1
    assert after.last_progress_at is not None
    assert (
        timedelta(hours=1, minutes=59)
        < after.progress_due_at - after.last_progress_at
        <= timedelta(hours=2)
    )
    assert after.report_capability == "validated"
    assert (await _events(store))[-1].event_type == "agent.progress_reported"
    assert (await _events(store))[-1].payload["verification_state"] == "agent_reported"

    sequence = len((await _events(store)))
    same, _ = await ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="checkpoint-1",
        body=body,
        source="provider_tool",
    )
    assert same.id == report.id
    assert len((await _events(store))) == sequence
    assert (
        await store.get(ButlerGoalWatch, watch_id(goal.id))
    ).revision == after.revision
    with pytest.raises(ConflictError, match="different content"):
        (
            await ingest_agent_report(
                store,
                turn_id=turn.id,
                idempotency_key="checkpoint-1",
                body=body.model_copy(update={"next_step": "Different action"}),
                source="provider_tool",
            )
        )


async def test_two_goal_agents_share_one_agent_reported_machine_incident(tmp_path):
    path, store, goals = await _setup(tmp_path)
    draft = await goals.create(
        "session-project",
        GoalCreate(
            objective="Lead research", completion_criteria=["Evidence"], child_budget=1
        ),
    )
    parent = await goals.write(
        "session-project", GoalWrite(expected_revision=draft.revision, action="start")
    )
    child = await goals.start_child(
        "session-project",
        GoalCreate(objective="Check host", completion_criteria=["Reach host"]),
    )
    child = await goals.write(
        child.session_id, GoalWrite(expected_revision=child.revision, action="start")
    )
    parent_turn = await _turn(store, parent, "turn-parent")
    child_turn = await _turn(store, child, "turn-child")
    first, opened = await ingest_agent_report(
        store,
        turn_id=parent_turn.id,
        idempotency_key="parent-observation",
        body=_attention("Lab.EXAMPLE."),
        source="provider_tool",
    )
    second, updated = await ingest_agent_report(
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
    assert (await store.count_entities(ButlerIncident, engagement_id="project")) == 1
    assert second.actor_role == "subagent"
    assert [event.event_type for event in (await _events(store))].count(
        "incident.opened"
    ) == 1
    assert [event.event_type for event in (await _events(store))].count(
        "incident.updated"
    ) == 1

    restarted = NebulaStore(path)
    duplicate, incident = await ingest_agent_report(
        restarted,
        turn_id=child_turn.id,
        idempotency_key="child-observation",
        body=_attention("lab.example"),
        source="provider_tool",
    )
    assert duplicate.id == second.id
    assert incident is not None and incident.id == opened.id
    assert incident.evidence_summary == updated.evidence_summary
    assert (
        await restarted.count_entities(ButlerIncident, engagement_id="project")
    ) == 1
    with pytest.raises(ValueError, match="evidence summaries"):
        ButlerIncident(
            engagement_id="project",
            incident_key="machine:other",
            source="agent_report",
            verification_state="agent_reported",
            summary="Agent report",
            evidence_summary=["x" * 301],
        )


async def test_concurrent_agent_reports_deduplicate_machine_incident(tmp_path):
    _, store, goals = await _setup(tmp_path)
    first_goal = await _start(goals)
    (
        await store.create(
            ChatSession(
                id="session-peer",
                engagement_id="project",
                title="Peer work",
                provider_profile_id="provider-project",
                model="model",
            )
        )
    )
    peer_draft = await goals.create(
        "session-peer",
        GoalCreate(objective="Check shared host", completion_criteria=["Reach host"]),
    )
    peer_goal = await goals.write(
        "session-peer", GoalWrite(expected_revision=peer_draft.revision, action="start")
    )
    first_turn = await _turn(store, first_goal, "concurrent-turn-first")
    peer_turn = await _turn(store, peer_goal, "concurrent-turn-peer")

    async def report(turn_id: str):
        return await ingest_agent_report(
            store,
            turn_id=turn_id,
            idempotency_key=f"machine-report-{turn_id}",
            body=_attention("LAB.example"),
            source="provider_tool",
        )

    results = list(
        (
            await asyncio.gather(
                *(report(_item) for _item in [first_turn.id, peer_turn.id])
            )
        )
    )
    assert len({incident.id for _, incident in results if incident is not None}) == 1
    assert (await store.count_entities(ButlerIncident, engagement_id="project")) == 1
    assert [event.event_type for event in (await _events(store))].count(
        "incident.opened"
    ) == 1
    assert [event.event_type for event in (await _events(store))].count(
        "incident.updated"
    ) == 1


async def test_report_identity_and_evidence_are_bound_to_core_project(tmp_path):
    _, store, goals = await _setup(tmp_path, projects=("alpha", "beta"))
    goal = await _start(goals, "alpha")
    turn = await _turn(store, goal, "turn-alpha")
    foreign = await store.create(
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
    before = len((await _events(store, "alpha")))
    with pytest.raises(ConflictError, match="another project"):
        (
            await ingest_agent_report(
                store,
                turn_id=turn.id,
                idempotency_key="foreign-evidence",
                body=body,
                source="provider_tool",
            )
        )
    assert (await store.count_entities(ButlerAgentReport, engagement_id="alpha")) == 0
    assert len((await _events(store, "alpha"))) == before
    with pytest.raises(ConflictError, match="does not match"):
        (
            await ingest_agent_report(
                store,
                turn_id=turn.id,
                idempotency_key="forged-project",
                body=body.model_copy(update={"evidence_refs": []}),
                source="provider_tool",
                expected_project_id="beta",
            )
        )


async def test_acknowledgment_is_project_scoped_idempotent_and_survives_new_report(
    tmp_path,
):
    path, store, goals = await _setup(tmp_path, projects=("alpha", "beta"))
    goal = await _start(goals, "alpha")
    turn = await _turn(store, goal, "turn-alert")
    _, opened = await ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="alert-1",
        body=_attention(),
        source="provider_tool",
    )
    assert opened is not None
    with pytest.raises(NotFoundError):
        (
            await acknowledge_incident(
                store,
                engagement_id="beta",
                record_id=opened.id,
                expected_revision=opened.revision,
            )
        )
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)
    route = f"/api/v1/engagements/alpha/butler/incidents/{opened.id}/acknowledge"
    response = await client.post(route, json={"expected_revision": opened.revision})
    assert response.status_code == 200
    acknowledged = response.json()
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_at"] is not None
    sequence = len((await _events(store, "alpha")))
    duplicate = await client.post(route, json={"expected_revision": opened.revision})
    assert duplicate.status_code == 200
    assert duplicate.json()["revision"] == acknowledged["revision"]
    assert len((await _events(store, "alpha"))) == sequence

    reopened_store = NebulaStore(path)
    assert (
        await reopened_store.get(ButlerIncident, opened.id)
    ).status == "acknowledged"
    (await _turn(reopened_store, goal, "turn-alert-2"))
    _, updated = await ingest_agent_report(
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


async def test_report_tool_rejects_non_agent_invocation_and_binds_saved_turn(tmp_path):
    _, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    turn = await _turn(store, goal, "turn-tool")
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
    accepted = await broker.execute(invocation, scope)
    assert accepted.output["accepted"] is True
    assert accepted.output["incident_id"] is not None
    with pytest.raises(ToolNotPermitted):
        (
            await broker.execute(
                invocation.model_copy(update={"requested_by": "operator"}), scope
            )
        )
    with pytest.raises(ConflictError, match="does not match"):
        (
            await broker.execute(
                invocation.model_copy(update={"engagement_id": "other"}), scope
            )
        )
    (await store.update(ChatTurn, turn.id, {"status": ChatTurnStatus.COMPLETE}))
    with pytest.raises(ConflictError, match="active tool turn"):
        (
            await broker.execute(
                invocation.model_copy(update={"idempotency_key": "late-report"}), scope
            )
        )


async def test_provider_goal_turn_offers_core_reports_and_ingests_machine_alert(
    tmp_path, monkeypatch
):
    _, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    profile = await store.get(ProviderProfile, "provider-project")
    capabilities = profile.capabilities.model_dump(mode="python")
    capabilities["tool_calling"] = True
    (
        await store.update(
            ProviderProfile,
            profile.id,
            {
                "capabilities": capabilities,
                "capability_verifications": {
                    "model": {"model": "model", "status": "verified"}
                },
            },
        )
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
    prepared = await chat.prepare(
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
    (await store.update(ChatTurn, prepared.turn.id, {"status": ChatTurnStatus.ROUTING}))
    result = await prepared.tool_components.broker.execute(
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
    assert result.output["incident_id"] is not None
    assert (await store.count_entities(ButlerIncident, engagement_id="project")) == 1


async def test_overview_pages_watches_and_prioritizes_open_incidents(
    tmp_path, monkeypatch
):
    _, store, goals = await _setup(tmp_path)
    first = await _start(goals)
    monkeypatch.setattr(oversight, "OVERVIEW_PAGE_SIZE", 2)
    for index in range(2):
        goal = await store.create(
            ChatGoal(
                id=f"extra-goal-{index}",
                engagement_id="project",
                session_id="session-project",
                objective=f"Extra {index}",
                completion_criteria=["Evidence"],
                status=ChatGoalStatus.RUNNING,
            )
        )
        (
            await store.create(
                ButlerGoalWatch(
                    id=watch_id(goal.id),
                    engagement_id="project",
                    goal_id=goal.id,
                    session_id=goal.session_id,
                    status=goal.status,
                    coverage_reason="Lifecycle only",
                )
            )
        )
    for index, status in enumerate(("acknowledged", "open", "open")):
        (
            await store.create(
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
        )
    app = FastAPI()
    app.include_router(butler_router(ButlerOversightService(store)), prefix="/api/v1")
    client = TestClient(app)
    route = "/api/v1/engagements/project/butler/overview"
    first_page = (await client.get(route)).json()
    assert first_page["summary"] == {
        "watched_goals": 3,
        "open_incidents": 2,
        "acknowledged_incidents": 1,
        "resolved_incidents": 0,
        "account_resources": 0,
        "harness_quota_resources": 0,
        "decisions": 0,
        "actions": 0,
        "recovery_cases": 0,
        "recovery_attention": 0,
        "recovery_active": 0,
    }
    assert len(first_page["watched_goals"]) == 2
    assert len(first_page["incidents"]) == 2
    assert {incident["status"] for incident in first_page["incidents"]} == {"open"}
    assert first_page["next_watch_offset"] == 2
    assert first_page["next_incident_offset"] == 2
    second_page = (
        await client.get(route, params={"watch_offset": 2, "incident_offset": 2})
    ).json()
    assert [item["goal_id"] for item in second_page["watched_goals"]] == [first.id]
    assert [item["status"] for item in second_page["incidents"]] == ["acknowledged"]
    assert second_page["next_watch_offset"] is None
    assert second_page["next_incident_offset"] is None


pytestmark = pytest.mark.anyio
