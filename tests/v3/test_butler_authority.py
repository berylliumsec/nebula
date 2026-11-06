"""Focused project authority, reviewed-action, and parent-routing contracts."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.butler_authority import AuthorityWrite, ButlerAuthorityService
from nebula.v3.butler_oversight import (
    GoalAttentionReport,
    ingest_agent_report,
    project_stream_id,
    watch_id,
)
from nebula.v3.butler_review import (
    ButlerReviewOutput,
    ButlerReviewResult,
    review_pending,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalUpdate, GoalWrite
from nebula.v3.domain import (
    ButlerAction,
    ButlerAuthorityPolicy,
    ButlerGoalWatch,
    ButlerProjectState,
    ChatGoal,
    ChatGoalStatus,
    ChatPlanDirective,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_control_assistant_work import _app


class _Reviewer:
    def __init__(self, choice: str, *, why_now: str = "") -> None:
        self.choice = choice
        self.why_now = why_now

    async def review(self, packet):
        return ButlerReviewResult(
            output=ButlerReviewOutput(
                choice=self.choice,
                summary="The goal needs a checkpoint.",
                reason="The cited Core event calls for a bounded response.",
                impact="The next step depends on a current status.",
                next_action="Ask the parent for a status or pause the blocked goal.",
                why_now=self.why_now,
                evidence_refs=packet.evidence_refs[:1],
            )
        )


async def _setup(tmp_path, *, child: bool = False):
    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Selected project"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    main = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main agent",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    goals = ChatGoalService(store)
    draft = await goals.create(
        main.id,
        GoalCreate(
            objective="Check the service",
            completion_criteria=["Report an observed result"],
            child_budget=1 if child else None,
        ),
    )
    main_goal = await goals.write(
        main.id, GoalWrite(expected_revision=draft.revision, action="start")
    )
    target = main_goal
    if child:
        child_goal = await goals.start_child(
            main.id,
            GoalCreate(objective="Check a branch", completion_criteria=["Report"]),
        )
        target = await goals.write(
            child_goal.session_id,
            GoalWrite(expected_revision=child_goal.revision, action="start"),
        )
    worker = app.state.butler_oversight
    assert worker.replay_project(project.id)
    return store, app, project, main, main_goal, target, worker


def _review(store, project, worker, choice, *, why_now=""):
    decision = asyncio.run(
        review_pending(
            store,
            project.id,
            worker.worker_id,
            _Reviewer(choice, why_now=why_now),
            worker.memory_service,
        )
    )
    assert decision is not None and decision.status == "reviewed"
    return decision


async def _overdue(store, project, goal, worker):
    (
        await store.update(
            ButlerGoalWatch,
            watch_id(goal.id),
            {"progress_due_at": utc_now() - timedelta(seconds=1)},
        )
    )
    assert worker.fire_due_deadlines(project.id) == 1
    assert any(
        event.event_type == "goal.progress_overdue"
        for event in (
            await store.replay_operation_events(project_stream_id(project.id))
        )
    )


async def _attention(store, goal):
    profile_id = (await store.get(ChatSession, goal.session_id)).provider_profile_id
    turn = await store.create(
        ChatTurn(
            engagement_id=goal.engagement_id,
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id=profile_id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
        )
    )
    return await ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="urgent-host-report",
        body=GoalAttentionReport(
            condition="machine_unreachable",
            machine="target.example",
            symptom="The connection timed out.",
            impact="The check cannot continue.",
            evidence=["Three attempts timed out."],
            attempted_recovery="Retried once.",
            urgency="urgent",
            help_requested="Check whether the target is available.",
        ),
        source="provider_tool",
        expected_project_id=goal.engagement_id,
        expected_session_id=goal.session_id,
    )


def _capture_immediate_guidance(app):
    delivered = []

    async def message(project_id, arguments, *, idempotency_key):
        assert arguments["delivery"] == "immediate"
        delivered.append((project_id, arguments, idempotency_key))
        return {"delivery": "started", "turn_id": "accepted-parent-turn"}

    app.state.control_assistant_work.message_chat = message
    return delivered


async def test_authority_api_is_authenticated_revisioned_project_scoped_and_durable(
    tmp_path,
):
    store, app, project, *_ = await _setup(tmp_path)
    client = TestClient(app)
    path = f"/api/v1/engagements/{project.id}/butler/authority"
    assert (await client.get(path)).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    initial = await client.get(path, headers=headers)
    assert initial.status_code == 200
    assert initial.json()["authority"] == "watch"
    assert initial.json()["revision"] == 0
    changed = await client.put(
        path,
        headers=headers,
        json={
            "authority": "delegate",
            "delegate_actions": ["pause_urgent_machine_blocker"],
            "expected_revision": 0,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["effective_actions"] == [
        "request_parent_status",
        "request_parent_plan_reassessment",
        "pause_urgent_machine_blocker",
    ]
    assert (
        await client.put(
            path,
            headers=headers,
            json={"authority": "watch", "delegate_actions": [], "expected_revision": 0},
        )
    ).status_code == 409
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "authority": "steer",
                "delegate_actions": ["pause_urgent_machine_blocker"],
                "expected_revision": 1,
            },
        )
    ).status_code == 422
    overview = (
        await client.get(
            f"/api/v1/engagements/{project.id}/butler/overview", headers=headers
        )
    ).json()
    assert overview["authority"] == "delegate"
    assert overview["authority_revision"] == changed.json()["revision"]
    assert overview["capabilities"]["authority_levels"] == [
        "watch",
        "steer",
        "delegate",
        "act",
    ]
    assert (
        await ButlerAuthorityService(NebulaStore(tmp_path / "core.db"), None).get(
            project.id
        )
    )["authority"] == "delegate"
    assert (
        await store.count_entities(ButlerAuthorityPolicy, engagement_id=project.id)
    ) == 1


async def test_watch_cannot_act_and_steer_delivers_once_to_parent_main_agent(tmp_path):
    store, app, project, main, _, child, worker = await _setup(tmp_path, child=True)
    delivered = _capture_immediate_guidance(app)
    (await _overdue(store, project, child, worker))
    _review(store, project, worker, "ask")
    authority = app.state.butler_authority
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.count_entities(ButlerAction, engagement_id=project.id)) == 0
    assert app.state.control_assistant_work.queue.get(main.id).items == []

    # A later reviewed event under Steer may reach only the main agent.
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    child_watch = await store.get(ButlerGoalWatch, watch_id(child.id))
    (
        await store.update(
            ButlerGoalWatch,
            child_watch.id,
            {
                "progress_due_at": utc_now() - timedelta(seconds=1),
                "deadline_generation": child_watch.deadline_generation + 1,
            },
        )
    )
    assert worker.fire_due_deadlines(project.id) == 1
    # The review throttle applies per project. Advance it in this controlled fixture.
    state = await store.find_entity(ButlerProjectState, "engagement_id", project.id)
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"last_review_requested_at": utc_now() - timedelta(minutes=1)},
        )
    )
    _review(store, project, worker, "ask")
    (await authority.process_project(project.id, worker.worker_id))
    actions = await store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(actions) == 1
    assert actions[0].status == "dispatched"
    assert actions[0].target_session_id == main.id
    assert len(delivered) == 1
    assert delivered[0][1]["session_id"] == main.id
    assert "progress checkpoint" in delivered[0][1]["text"]
    assert app.state.control_assistant_work.queue.get(main.id).items == []
    assert app.state.control_assistant_work.queue.get(child.session_id).items == []
    (await authority.process_project(project.id, worker.worker_id))
    assert len(delivered) == 1


async def test_plan_change_steer_needs_explicit_reviewed_reason_before_parent_message(
    tmp_path,
):
    store, app, project, main, _, child, worker = await _setup(tmp_path, child=True)
    delivered = _capture_immediate_guidance(app)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    goals = ChatGoalService(store)
    changed = await goals.update(
        child.session_id,
        GoalUpdate(
            expected_revision=child.revision,
            objective=child.objective,
            completion_criteria=child.completion_criteria,
            plan=["Repeat the same probe"],
        ),
    )
    _review(store, project, worker, "steer")
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.count_entities(ButlerAction, engagement_id=project.id)) == 0
    assert app.state.control_assistant_work.queue.get(main.id).items == []

    state = await store.find_entity(ButlerProjectState, "engagement_id", project.id)
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"last_review_requested_at": utc_now() - timedelta(minutes=1)},
        )
    )
    (
        await goals.update(
            child.session_id,
            GoalUpdate(
                expected_revision=changed.revision,
                objective=changed.objective,
                completion_criteria=changed.completion_criteria,
                plan=["Repeat the same probe", "Repeat the same probe again"],
            ),
        )
    )
    decision = _review(
        store,
        project,
        worker,
        "steer",
        why_now="The revised plan repeats a probe without a new comparison.",
    )
    assert decision.why_now
    (await authority.process_project(project.id, worker.worker_id))
    actions = await store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(actions) == 1
    assert actions[0].action_type == "request_parent_plan_reassessment"
    assert actions[0].status == "dispatched"
    assert actions[0].target_session_id == main.id
    assert len(delivered) == 1
    text = delivered[0][1]["text"]
    assert "compare the current plan" in text
    assert "repeats a probe" not in text
    assert app.state.control_assistant_work.queue.get(child.session_id).items == []
    (await authority.process_project(project.id, worker.worker_id))
    assert len(delivered) == 1


async def test_delegate_plan_force_requires_grant_and_current_reviewed_goal_evidence(
    tmp_path,
):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(
            authority="delegate",
            delegate_actions=["force_one_task_plan"],
            expected_revision=0,
        ),
    )
    changed = await ChatGoalService(store).update(
        main.id,
        GoalUpdate(
            expected_revision=goal.revision,
            objective=goal.objective,
            completion_criteria=goal.completion_criteria,
            plan=["Inspect current state", "Recheck the changed condition"],
        ),
    )
    decision = _review(
        store,
        project,
        worker,
        "steer",
        why_now="The changed goal plan needs a fresh tool and file inventory.",
    )
    assert any(
        ref.get("kind") == "goal" and ref.get("id") == changed.id
        for ref in decision.evidence_refs
    )
    (await authority.process_project(project.id, worker.worker_id))
    actions = await store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(actions) == 1
    assert actions[0].action_type == "force_one_task_plan"
    assert actions[0].status == "applied", actions[0].outcome
    directives = await store.list_session_entities(ChatPlanDirective, main.id)
    assert len(directives) == 1
    assert directives[0].target_kind == "goal"
    assert directives[0].target_id == changed.id
    assert directives[0].reason == decision.why_now


async def test_plan_reassessment_re_reviews_when_precedent_changes(tmp_path):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    delivered = _capture_immediate_guidance(app)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    goals = ChatGoalService(store)
    (
        await goals.update(
            main.id,
            GoalUpdate(
                expected_revision=goal.revision,
                objective=goal.objective,
                completion_criteria=goal.completion_criteria,
                plan=["Repeat the same probe"],
            ),
        )
    )
    first = _review(
        store,
        project,
        worker,
        "steer",
        why_now="The new plan repeats the same probe without a control.",
    )
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    app.state.butler_memory_service.capture_operator_preference(
        project.id, "Always ask the parent before changing a goal."
    )
    (await authority.dispatch(action, worker.worker_id))
    assert (await store.get(ButlerAction, action.id)).status == "cancelled"
    recheck = [
        event
        for event in (
            await store.replay_operation_events(project_stream_id(project.id))
        )
        if event.payload.get("causal_event_id") == first.source_event_id
    ]
    assert len(recheck) == 1
    assert recheck[0].payload["metrics"]["plan_changed"] is True
    assert (
        recheck[0].payload["metrics"]["goal_revision"]
        == (await store.get(ChatGoal, goal.id)).revision
    )
    _review(
        store,
        project,
        worker,
        "steer",
        why_now="The new plan repeats the same probe without a control.",
    )
    (await authority.process_project(project.id, worker.worker_id))
    assert len(delivered) == 1


async def test_delegate_requires_explicit_pause_grant_and_pauses_main_goal(tmp_path):
    store, app, project, _, goal, _, worker = await _setup(tmp_path)
    report, _ = await _attention(store, goal)
    assert report.details["condition"] == "machine_unreachable"
    first_turn = await store.get(ChatTurn, report.turn_id)
    (
        await store.update(
            ChatTurn,
            first_turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=first_turn.revision,
        )
    )
    _review(store, project, worker, "steer")
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="delegate", delegate_actions=[], expected_revision=0),
    )
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.RUNNING
    assert (await store.count_entities(ButlerAction, engagement_id=project.id)) == 0

    # A new reviewed urgent report after the operator grants this one action.
    authority.write(
        project.id,
        AuthorityWrite(
            authority="delegate",
            delegate_actions=["pause_urgent_machine_blocker"],
            expected_revision=1,
        ),
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=goal.session_id,
            goal_id=goal.id,
            provider_profile_id=(
                await store.get(ChatSession, goal.session_id)
            ).provider_profile_id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
        )
    )
    (
        await ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="urgent-host-report-2",
            body=GoalAttentionReport(
                condition="machine_down",
                machine="target.example",
                symptom="The host did not respond.",
                impact="The check cannot continue.",
                evidence=["A second scoped attempt failed."],
                attempted_recovery="Retried from the runner.",
                urgency="urgent",
                help_requested="Check host availability.",
            ),
            source="provider_tool",
            expected_project_id=project.id,
            expected_session_id=goal.session_id,
        )
    )
    (
        await store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=turn.revision,
        )
    )
    state = await store.find_entity(ButlerProjectState, "engagement_id", project.id)
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"last_review_requested_at": utc_now() - timedelta(minutes=1)},
        )
    )
    _review(store, project, worker, "steer")
    (await authority.process_project(project.id, worker.worker_id))
    paused = await store.get(ChatGoal, goal.id)
    assert paused.status == ChatGoalStatus.PAUSED
    assert "agent-reported" in paused.blocked_reason
    actions = await store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(actions) == 1 and actions[0].status == "applied"
    (await authority.process_project(project.id, worker.worker_id))
    assert (
        len((await store.find_entities(ButlerAction, {}, engagement_id=project.id)))
        == 1
    )


async def test_revoked_authority_cancels_planned_action_before_dispatch(tmp_path):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await _overdue(store, project, goal, worker))
    _review(store, project, worker, "ask")
    assert authority.plan_next(project.id, worker.worker_id)
    planned = await store.find_entities(ButlerAction, {}, engagement_id=project.id)
    assert len(planned) == 1 and planned[0].status == "planned"
    authority.write(
        project.id,
        AuthorityWrite(authority="watch", delegate_actions=[], expected_revision=1),
    )
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.get(ButlerAction, planned[0].id)).status == "cancelled"
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_revoke_and_regrant_during_dispatch_cannot_use_old_event(
    tmp_path, monkeypatch
):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await _overdue(store, project, goal, worker))
    _review(store, project, worker, "ask")
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    original_proposal = authority._proposal
    changed = False

    def change_grant_after_proposal(project_id, decision, policy):
        nonlocal changed
        proposal = original_proposal(project_id, decision, policy)
        if proposal is not None and not changed:
            changed = True
            authority.write(
                project.id,
                AuthorityWrite(
                    authority="watch", delegate_actions=[], expected_revision=1
                ),
            )
            authority.write(
                project.id,
                AuthorityWrite(
                    authority="steer", delegate_actions=[], expected_revision=2
                ),
            )
        return proposal

    monkeypatch.setattr(authority, "_proposal", change_grant_after_proposal)
    (await authority.dispatch(action, worker.worker_id))
    assert changed
    assert (await store.get(ButlerAction, action.id)).status == "planned"
    assert app.state.control_assistant_work.queue.get(main.id).items == []

    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.get(ButlerAction, action.id)).status == "cancelled"
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_changed_precedent_cancels_undelivered_action_and_requests_fresh_review(
    tmp_path,
):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    delivered = _capture_immediate_guidance(app)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await _overdue(store, project, goal, worker))
    original = _review(store, project, worker, "ask")
    assert original.precedent_digest
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    saved = app.state.butler_memory_service.capture_operator_preference(
        project.id, "Always ask the parent before changing a goal."
    )
    assert saved is not None

    (await authority.dispatch(action, worker.worker_id))
    cancelled = await store.get(ButlerAction, action.id)
    assert cancelled.status == "cancelled"
    assert cancelled.outcome == {
        "reason": "precedent_changed_re_review_requested",
        "delivered": False,
    }
    assert app.state.control_assistant_work.queue.get(main.id).items == []
    recheck = [
        event
        for event in (
            await store.replay_operation_events(project_stream_id(project.id))
        )
        if event.payload.get("causal_event_id") == original.source_event_id
    ]
    assert len(recheck) == 1
    assert recheck[0].event_type == "goal.progress_overdue"
    assert recheck[0].payload["verification_state"] == "core_observed"

    reconsidered = _review(store, project, worker, "ask")
    assert reconsidered.id != original.id
    assert reconsidered.precedent_digest != original.precedent_digest
    (await authority.process_project(project.id, worker.worker_id))
    assert len(delivered) == 1
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_memory_lookup_failure_keeps_action_planned_and_retryable(
    tmp_path, monkeypatch
):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    delivered = _capture_immediate_guidance(app)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await _overdue(store, project, goal, worker))
    _review(store, project, worker, "ask")
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    original_search = authority.memory_service.search

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("memory index unavailable")

    monkeypatch.setattr(authority.memory_service, "search", unavailable)
    (await authority.dispatch(action, worker.worker_id))
    waiting = await store.get(ButlerAction, action.id)
    assert waiting.status == "planned"
    assert waiting.outcome == {"reason": "memory_unavailable", "delivered": False}
    assert app.state.control_assistant_work.queue.get(main.id).items == []

    monkeypatch.setattr(authority.memory_service, "search", original_search)
    (await authority.dispatch(waiting, worker.worker_id))
    assert (await store.get(ButlerAction, action.id)).status == "dispatched"
    assert len(delivered) == 1
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_attention_re_review_keeps_agent_claim_unverified_and_parent_only(
    tmp_path,
):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(
            authority="delegate",
            delegate_actions=["pause_urgent_machine_blocker"],
            expected_revision=0,
        ),
    )
    report, _ = await _attention(store, goal)
    turn = await store.get(ChatTurn, report.turn_id)
    (
        await store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=turn.revision,
        )
    )
    original = _review(store, project, worker, "steer")
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    app.state.butler_memory_service.capture_operator_preference(
        project.id, "Always ask the parent before pausing a goal."
    )
    (await authority.dispatch(action, worker.worker_id))
    assert (await store.get(ButlerAction, action.id)).status == "cancelled"
    recheck = [
        event
        for event in (
            await store.replay_operation_events(project_stream_id(project.id))
        )
        if event.payload.get("causal_event_id") == original.source_event_id
    ]
    assert len(recheck) == 1
    assert recheck[0].event_type == "agent.attention_reported"
    assert recheck[0].payload["source"] == "core_recovery"
    assert recheck[0].payload["verification_state"] == "agent_reported"
    assert any(
        ref["kind"] == "agent_report" and ref["id"] == report.id
        for ref in recheck[0].payload["evidence_refs"]
    )
    _review(store, project, worker, "steer")
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.PAUSED
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_new_authority_does_not_replay_older_reviewed_decisions(tmp_path):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    (await _overdue(store, project, goal, worker))
    _review(store, project, worker, "ask")
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.count_entities(ButlerAction, engagement_id=project.id)) == 0
    assert app.state.control_assistant_work.queue.get(main.id).items == []


async def test_interrupted_dispatch_without_queue_receipt_stays_uncertain(tmp_path):
    store, app, project, main, goal, _, worker = await _setup(tmp_path)
    authority = app.state.butler_authority
    authority.write(
        project.id,
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    (await _overdue(store, project, goal, worker))
    _review(store, project, worker, "ask")
    assert authority.plan_next(project.id, worker.worker_id)
    action = (await store.find_entities(ButlerAction, {}, engagement_id=project.id))[0]
    (
        await store.update(
            ButlerAction,
            action.id,
            {
                "status": "dispatching",
                "target_session_id": main.id,
                "started_at": utc_now(),
            },
            expected_revision=action.revision,
        )
    )
    (await authority.process_project(project.id, worker.worker_id))
    assert (await store.get(ButlerAction, action.id)).status == "uncertain"
    assert app.state.control_assistant_work.queue.get(main.id).items == []
    (await authority.process_project(project.id, worker.worker_id))
    assert app.state.control_assistant_work.queue.get(main.id).items == []


pytestmark = pytest.mark.anyio
