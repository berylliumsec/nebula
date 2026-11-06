"""Selective Butler memory capture from committed, project-scoped Core events."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import time

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_memory import ButlerMemoryService
from nebula.v3.butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    project_state_id,
)
from nebula.v3.domain import (
    ButlerAction,
    ButlerDecision,
    ButlerProjectState,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore


async def _setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    memory = ButlerMemoryService(
        tmp_path / "butler-memory.sqlite3",
        index_path=tmp_path / "memory-vectors",
    )
    project = await store.create(Engagement(name="Project A"))
    other = await store.create(Engagement(name="Project B"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main agent",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective="Check the target",
            completion_criteria=["Evidence"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    return store, memory, project, other, session, goal


_USEFUL_LESSON = (
    "When a progress checkpoint is missed, ask for cited evidence before "
    "revising the plan because the next step depends on current observations."
)


async def _decision_event(
    store,
    project,
    session,
    goal,
    *,
    observed=True,
    choice="escalate",
    lesson_candidate=_USEFUL_LESSON,
    event_key="",
):
    verification = "core_observed" if observed else "agent_reported"
    source_type = "goal.progress_overdue" if observed else "agent.attention_reported"
    async with store.transaction() as transaction:
        source = await _append_project_event(
            transaction,
            engagement_id=project.id,
            event_type=source_type,
            payload=ButlerEventPayload(
                project_id=project.id,
                goal_id=goal.id,
                session_id=session.id,
                source="core" if observed else "agent",
                actor_role="main_agent",
                summary="A source event requiring review.",
                verification_state=verification,
                evidence_refs=[
                    ButlerEvidenceRef(kind="goal", id=goal.id, project_id=project.id)
                ],
            ),
            idempotency_key=f"source-{verification}-{choice}-{event_key}",
            now=utc_now(),
        )
        decision = ButlerDecision(
            engagement_id=project.id,
            source_event_id=source.id,
            source_sequence=source.sequence,
            source_event_type=source_type,
            goal_id=goal.id,
            session_id=session.id,
            actor_role="main_agent",
            status="reviewed",
            choice=choice,
            summary="An agent mentioned API key sk-test-secret-123456789012345.",
            reason="The source needs attention.",
            why_now="The event was just observed." if choice == "escalate" else "",
            lesson_candidate=lesson_candidate,
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="goal", id=goal.id, project_id=project.id
                ).model_dump(mode="json")
            ],
            source_verification_state=verification,
        )
        (await transaction.add(decision))
        (
            await _append_project_event(
                transaction,
                engagement_id=project.id,
                event_type="butler.decision_recorded",
                payload=ButlerEventPayload(
                    project_id=project.id,
                    goal_id=goal.id,
                    session_id=session.id,
                    source="core",
                    actor_role="core",
                    summary="Butler recorded a review.",
                    metrics={"choice": choice, "status": "reviewed"},
                    verification_state="core_observed",
                    evidence_refs=[
                        ButlerEvidenceRef(
                            kind="decision", id=decision.id, project_id=project.id
                        )
                    ],
                ),
                idempotency_key=f"decision-{decision.id}",
                now=utc_now(),
            )
        )
    return decision, source


async def _applied_pause_event(store, project, session, goal, source):
    action = await store.create(
        ButlerAction(
            engagement_id=project.id,
            decision_id="reviewed-decision",
            source_event_id=source.id,
            goal_id=goal.id,
            source_session_id=session.id,
            target_session_id=session.id,
            action_type="pause_urgent_machine_blocker",
            status="applied",
            idempotency_key="action-pause",
            verification_state="agent_reported",
            summary="Pause after urgent report.",
            outcome={"goal_id": goal.id},
            finished_at=utc_now(),
        )
    )
    async with store.transaction() as transaction:
        (
            await _append_project_event(
                transaction,
                engagement_id=project.id,
                event_type="butler.action_applied",
                payload=ButlerEventPayload(
                    project_id=project.id,
                    goal_id=goal.id,
                    session_id=session.id,
                    source="core",
                    actor_role="core",
                    summary="Butler applied a bounded action.",
                    metrics={
                        "action_type": "pause_urgent_machine_blocker",
                        "status": "applied",
                    },
                    verification_state="core_observed",
                    evidence_refs=[
                        ButlerEvidenceRef(
                            kind="action", id=action.id, project_id=project.id
                        )
                    ],
                ),
                idempotency_key=f"action-{action.id}",
                now=utc_now(),
            )
        )
    return action


async def _dispatched_status_event(store, project, session, goal, decision, source):
    action = await store.create(
        ButlerAction(
            engagement_id=project.id,
            decision_id=decision.id,
            source_event_id=source.id,
            goal_id=goal.id,
            source_session_id=session.id,
            target_session_id=session.id,
            action_type="request_parent_status",
            status="dispatched",
            idempotency_key=f"action-status-{decision.id}",
            verification_state="core_observed",
            summary="Asked the parent for a status.",
            outcome={"queue_item_id": f"queued-{decision.id}"},
            finished_at=utc_now(),
        )
    )
    async with store.transaction() as transaction:
        (
            await _append_project_event(
                transaction,
                engagement_id=project.id,
                event_type="butler.action_dispatched",
                payload=ButlerEventPayload(
                    project_id=project.id,
                    goal_id=goal.id,
                    session_id=session.id,
                    source="core",
                    actor_role="core",
                    summary="Butler dispatched a bounded parent status request.",
                    metrics={
                        "action_type": action.action_type,
                        "status": action.status,
                    },
                    verification_state="core_observed",
                    evidence_refs=[
                        ButlerEvidenceRef(
                            kind="action", id=action.id, project_id=project.id
                        )
                    ],
                ),
                idempotency_key=f"action-{action.id}",
                now=utc_now(),
            )
        )
    return action


async def test_pivotal_capture_is_selective_scoped_and_replay_safe(tmp_path):
    store, memory, project, other, session, goal = await _setup(tmp_path)
    decision, source = await _decision_event(store, project, session, goal)
    (await _decision_event(store, project, session, goal, observed=False))
    action = await _applied_pause_event(store, project, session, goal, source)

    assert (await memory.capture_pivotal_project_events(store, project.id)) == 3
    own = (await memory.list(project.id)).items
    assert {item.scope for item in own} == {"project_lesson", "global_lesson"}
    assert {item.source_ref for item in own} == {decision.id, action.id}
    assert any(_USEFUL_LESSON in item.content for item in own)
    assert all("sk-test-secret" not in item.content for item in own)
    assert all(
        "machine condition was not independently verified" in item.content
        for item in own
        if item.scope == "global_lesson"
    )
    foreign = (await memory.list(other.id)).items
    assert len(foreign) == 1
    assert foreign[0].scope == "global_lesson"
    assert foreign[0].source_ref is None
    assert "Project A" not in foreign[0].content
    assert (await memory.search(other.id, "machine blocker")).items[0].id == foreign[
        0
    ].id
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 0
    restarted = ButlerMemoryService(tmp_path / "butler-memory.sqlite3")
    assert (await restarted.capture_pivotal_project_events(store, project.id)) == 0
    assert len((await restarted.list(project.id)).items) == 2


async def test_pivotal_capture_does_not_store_unverified_or_routine_review(tmp_path):
    store, memory, project, _, session, goal = await _setup(tmp_path)
    (await _decision_event(store, project, session, goal, observed=False))
    (
        await _decision_event(
            store, project, session, goal, observed=True, choice="observe"
        )
    )
    (
        await _decision_event(
            store, project, session, goal, choice="steer", lesson_candidate=None
        )
    )
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 3
    assert (await memory.list(project.id)).items == []


async def test_review_lesson_keeps_distinct_cited_process_insights_only(tmp_path):
    store, memory, project, other, session, goal = await _setup(tmp_path)
    first, _ = await _decision_event(store, project, session, goal)
    near, near_source = await _decision_event(
        store,
        project,
        session,
        goal,
        choice="steer",
        lesson_candidate=(
            "When a progress checkpoint is missed, ask for cited evidence "
            "before changing the plan because the next step depends on current observations."
        ),
    )
    distinct, distinct_source = await _decision_event(
        store,
        project,
        session,
        goal,
        choice="steer",
        event_key="blocked-context",
        lesson_candidate=(
            "If a goal remains blocked, distinguish unavailable tools from "
            "failed tests before changing scope because the remedies differ."
        ),
    )
    unsafe, _ = await _decision_event(
        store,
        project,
        session,
        goal,
        event_key="unsafe",
        lesson_candidate=(
            "When progress stalls, record API key sk-test-secret-123456789012345 "
            "before retrying because the result may need comparison."
        ),
    )
    (await _dispatched_status_event(store, project, session, goal, near, near_source))
    (
        await _dispatched_status_event(
            store, project, session, goal, distinct, distinct_source
        )
    )
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 6
    own = (await memory.list(project.id)).items
    assert {item.source_ref for item in own} == {first.id, distinct.id}
    assert near.id not in {item.source_ref for item in own}
    assert unsafe.id not in {item.source_ref for item in own}
    assert all(
        item.source_label == "Butler review suggestion · Core observed" for item in own
    )
    assert (await memory.list(other.id)).items == []


async def test_steer_candidate_waits_for_confirmed_delivery_without_losing_replay(
    tmp_path,
):
    store, memory, project, _, session, goal = await _setup(tmp_path)
    decision, source = await _decision_event(
        store, project, session, goal, choice="steer"
    )
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 1
    assert (await memory.list(project.id)).items == []

    action = await _dispatched_status_event(
        store, project, session, goal, decision, source
    )
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 1
    saved = (await memory.list(project.id)).items
    assert len(saved) == 1
    assert saved[0].source_ref == decision.id
    assert action.id != saved[0].source_ref
    assert (await memory.capture_pivotal_project_events(store, project.id)) == 0


async def test_direct_operator_guidance_needs_stable_intent_and_preserves_scope(
    tmp_path,
):
    _, memory, project, other, _, _ = await _setup(tmp_path)
    assert (
        await memory.capture_operator_preference(
            project.id, "From now on, keep progress summaries concise."
        )
    ) is not None
    assert (
        await memory.capture_operator_preference(
            project.id, "I want brief status updates by default."
        )
    ) is not None
    assert (
        await memory.capture_operator_preference(
            project.id, "I prefer gpt-6.1-sol for code reviews."
        )
    ) is not None
    assert (
        await memory.capture_operator_preference(
            project.id, "I prefer a warning when quota reaches 20%."
        )
    ) is not None
    assert (
        await memory.capture_operator_preference(
            project.id, "I want to start a new mission."
        )
    ) is None
    assert (
        await memory.capture_operator_preference(
            project.id, "Please always approve pending requests."
        )
    ) is None
    assert (
        await memory.capture_operator_preference(
            project.id, "From now on, use API key sk-test-secret-123456789012345."
        )
    ) is None
    assert (
        await memory.capture_operator_preference(
            project.id, "I prefer progress notes at 192.168.1.99."
        )
    ) is None
    assert (
        await memory.capture_operator_preference(
            project.id, "I prefer code updates sent to account 1234 5678 9012 3456."
        )
    ) is None
    assert (
        await memory.capture_operator_decision(
            project.id,
            "We decided to always send child agent updates through the parent chat.",
        )
    ) is not None
    assert (
        await memory.capture_operator_decision(
            project.id, "We decided to always keep this project's summaries brief."
        )
    ) is not None
    assert (
        await memory.capture_operator_decision(
            project.id, "We decided to use a new model for this task."
        )
    ) is None
    assert (
        await memory.capture_operator_decision(
            project.id, "Decision: always bypass approvals for this project."
        )
    ) is None
    assert len((await memory.list(project.id)).items) == 6
    foreign = (await memory.list(other.id)).items
    assert len(foreign) == 5
    assert all(item.source_ref is None for item in foreign)


async def test_oversight_worker_captures_committed_pivotal_events_automatically(
    tmp_path,
):
    store, memory, project, _, session, goal = await _setup(tmp_path)
    decision, _ = await _decision_event(store, project, session, goal)
    state = await store.get(ButlerProjectState, project_state_id(project.id))
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"last_review_sequence": decision.source_sequence},
            expected_revision=state.revision,
        )
    )
    app = create_app(store, auth_token="test-token", butler_memory_service=memory)
    assert app.state.butler_oversight.memory_service is memory
    async with TestClient(app):
        for _ in range(50):
            if (await memory.list(project.id)).items:
                break
            time.sleep(0.05)
    assert len((await memory.list(project.id)).items) == 1
    assert (await memory.list(project.id)).items[0].source_ref == decision.id
    assert not any(
        item.source_ref == decision.id
        for item in (await memory.list("not-selected")).items
    )


pytestmark = pytest.mark.anyio
