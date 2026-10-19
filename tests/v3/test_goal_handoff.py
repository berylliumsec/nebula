"""A failed harness planning attempt can continue once on a checked provider route."""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_oversight import ButlerEventPayload, _append_project_event
from nebula.v3.chat_goals import ChatGoalService, GoalHandoffRequest
from nebula.v3.domain import (
    Approval,
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatQueue,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    EngagementStatus,
    HarnessSession,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import ConflictError, NebulaStore


MODEL = "deepseek/deepseek-test"
PLANNING_FAILURE = (
    "This harness configuration cannot enforce restricted discovery and native "
    "pre-use checks. Choose a managed Claude Agent SDK profile and retry this task "
    "as a linked turn, or disable Plan before acting for a later task."
)


def _source(tmp_path):
    db_path = tmp_path / "core.db"
    store = NebulaStore(db_path)
    project = store.create(
        Engagement(name="Research", workspace_path=str(tmp_path), work_enabled=True)
    )
    profile = store.create(
        ProviderProfile(
            name="DeepSeek",
            provider_type="ollama",
            is_local=True,
            model_allowlist=[MODEL],
            capability_verifications={
                MODEL: ProviderCapabilityVerification(
                    model=MODEL, status=ProviderVerificationStatus.VERIFIED
                )
            },
            metadata={
                "options": {"context_window": 32_000, "max_output_tokens": 4_096}
            },
        )
    )
    harness = store.create(
        HarnessSession(
            engagement_id=project.id, harness_profile_id="harness-a", model="codex"
        )
    )
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Source research",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-a",
            harness_session_id=harness.id,
            model="codex",
            metadata={
                "assistant_config": {
                    "backend": "provider",
                    "profile_id": profile.id,
                    "model": MODEL,
                    "tools_enabled": True,
                    "planning_enabled": True,
                    "mcp_server_ids": [],
                    "allow_subagents": False,
                }
            },
        )
    )
    goal = store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective="Verify the saved static research checkpoint",
            completion_criteria=["Record exact evidence"],
            status=ChatGoalStatus.PAUSED,
            blocked_reason="The harness response stopped or failed.",
            current_step=2,
            step_budget=6,
            time_budget_seconds=7_200,
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            goal_id=goal.id,
            backend=ChatBackend.HARNESS,
            model="codex",
            status=ChatTurnStatus.INTERRUPTED,
            tools_enabled=True,
            error=PLANNING_FAILURE,
        )
    )
    return store, db_path, project, profile, session, goal, turn


def _request(goal):
    return GoalHandoffRequest(
        expected_revision=goal.revision,
        reason="Saved provider can enforce the selected planning contract",
        checkpoint="Read the saved source checkpoint before static analysis.",
        max_steps=3,
        max_time_seconds=3_600,
    )


def test_handoff_is_atomic_bounded_idempotent_and_survives_restart(tmp_path):
    store, db_path, _project, profile, source, old_goal, _turn = _source(tmp_path)
    service = ChatGoalService(store)
    request = _request(old_goal).model_copy(update={"butler_initiated": True})

    created = service.handoff_to_provider(source.id, request)
    assert created.already_exists is False
    assert created.session.backend == ChatBackend.PROVIDER
    assert created.session.provider_profile_id == profile.id
    assert created.session.metadata["planning_enabled"] is True
    assert created.session.metadata["butler_initiated"] is True
    assert created.goal.status == ChatGoalStatus.DRAFT
    assert created.goal.step_budget == 3
    assert created.goal.time_budget_seconds == 3_600
    assert created.goal.child_budget is None
    assert created.goal.metadata["handoff_source_goal_id"] == old_goal.id
    assert created.source_goal.status == ChatGoalStatus.PAUSED
    assert created.source_goal.metadata["handoff_target_goal_id"] == created.goal.id
    assert "saved source checkpoint" in created.goal.objective
    assert store.count_entities(ChatGoal, engagement_id=source.engagement_id) == 2

    recovered = ChatGoalService(NebulaStore(db_path))
    duplicate = recovered.handoff_to_provider(source.id, request)
    assert duplicate.already_exists is True
    assert duplicate.goal.id == created.goal.id
    assert recovered.get(source.id).status == ChatGoalStatus.PAUSED
    with pytest.raises(ConflictError, match="handed off"):
        from nebula.v3.chat_goals import GoalWrite

        recovered.write(
            source.id,
            GoalWrite(
                expected_revision=duplicate.source_goal.revision, action="resume"
            ),
        )


@pytest.mark.parametrize(
    "gate",
    [
        "stale",
        "admitted",
        "tool_effect",
        "approval",
        "queue",
        "queue_inflight",
        "queue_review",
        "provider",
    ],
)
def test_handoff_fails_closed_without_linking_a_target(tmp_path, gate):
    store, _db_path, project, profile, source, goal, turn = _source(tmp_path)
    request = _request(goal)
    if gate == "stale":
        request.expected_revision += 1
    elif gate == "admitted":
        store.update(
            ChatTurn,
            turn.id,
            {"admitted_at": turn.created_at},
            expected_revision=turn.revision,
        )
    elif gate == "tool_effect":
        store.update(
            ChatTurn,
            turn.id,
            {"execution_tool_calls": 1},
            expected_revision=turn.revision,
        )
    elif gate == "approval":
        store.create(
            Approval(
                engagement_id=project.id,
                run_id=turn.id,
                chat_session_id=source.id,
                risk_class=RiskClass.WORKSPACE_WRITE,
                exact_request={},
                policy_rationale="Needs approval",
                requested_by="agent",
            )
        )
    elif gate == "queue":
        store.create(
            ChatQueue(
                engagement_id=project.id,
                session_id=source.id,
                items=[{"id": "pending", "status": "queued"}],
            )
        )
    elif gate == "queue_inflight":
        store.create(
            ChatQueue(
                engagement_id=project.id,
                session_id=source.id,
                paused=True,
                items=[{"id": "inflight", "status": "sending"}],
            )
        )
    elif gate == "queue_review":
        store.create(
            ChatQueue(
                engagement_id=project.id,
                session_id=source.id,
                paused=True,
                items=[{"id": "uncertain", "status": "needs_review"}],
            )
        )
    elif gate == "provider":
        store.update(
            ProviderProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )

    with pytest.raises(ConflictError):
        ChatGoalService(store).handoff_to_provider(source.id, request)
    assert store.count_entities(ChatGoal, engagement_id=project.id) == 1
    assert (
        "handoff_target_session_id"
        not in ChatGoalService(store).get(source.id).metadata
    )


def test_handoff_respects_project_local_only_scope(tmp_path):
    store, _db_path, project, profile, source, goal, _turn = _source(tmp_path)
    policy = store.create(ScopePolicy(engagement_id=project.id, local_only=True))
    store.update(
        Engagement,
        project.id,
        {"scope_policy_id": policy.id},
        expected_revision=project.revision,
    )
    store.update(
        ProviderProfile,
        profile.id,
        {"is_local": False},
        expected_revision=profile.revision,
    )
    with pytest.raises(ConflictError, match="privacy scope"):
        ChatGoalService(store).handoff_to_provider(source.id, _request(goal))


def test_handoff_rejects_settings_changed_while_creating_draft(tmp_path, monkeypatch):
    store, _db_path, project, _profile, source, goal, _turn = _source(tmp_path)
    service = ChatGoalService(store)
    build = service._provider_conversation_entities

    def changed_settings(body):
        current = store.get(ChatSession, source.id)
        store.update(
            ChatSession,
            current.id,
            {"metadata": {**current.metadata, "updated_by_operator": True}},
            expected_revision=current.revision,
        )
        return build(body)

    monkeypatch.setattr(service, "_provider_conversation_entities", changed_settings)
    with pytest.raises(ConflictError, match="settings changed"):
        service.handoff_to_provider(source.id, _request(goal))
    assert store.count_entities(ChatGoal, engagement_id=project.id) == 1


def test_scheduled_butler_checks_act_revision_and_starts_only_linked_draft(tmp_path):
    store, _db_path, project, _profile, source, goal, _turn = _source(tmp_path)
    project = store.update(
        Engagement,
        project.id,
        {"status": EngagementStatus.ACTIVE},
        expected_revision=project.revision,
    )
    authority = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Continue eligible work.")
    digest = read_butler_instructions(tmp_path).sha256
    with store.transaction(immediate=True) as transaction:
        event = _append_project_event(
            transaction,
            engagement_id=project.id,
            event_type="butler.global_periodic_check",
            payload=ButlerEventPayload(
                project_id=project.id,
                source="core",
                actor_role="core",
                summary="Global check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key="handoff-test",
            now=utc_now(),
        )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=authority.revision,
            status="investigating",
            summary="Global check",
        )
    )

    class Work:
        calls = []

        def goal_detail(self, project_id, session_id):
            assert project_id == project.id
            assert session_id == source.id
            return {
                "kind": "main",
                "goal": {"id": goal.id, "revision": goal.revision},
            }

        async def handoff_goal(self, project_id, arguments):
            self.calls.append(("handoff", project_id, arguments))
            return {
                "session_id": "new-chat",
                "goal": {"id": "new-goal", "status": "draft", "revision": 1},
                "already_exists": False,
            }

        async def goal_action(self, project_id, arguments):
            self.calls.append(("start", project_id, arguments))
            return {
                "session_id": "new-chat",
                "goal": {"id": "new-goal", "status": "running"},
            }

    work = Work()
    actions = ButlerGlobalActions(store, work, tmp_path, case.id, runtime_local=True)
    arguments = {
        "project_id": project.id,
        "session_id": source.id,
        "expected_project_revision": project.revision,
        "expected_authority_revision": authority.revision,
        "expected_revision": goal.revision,
        "reason": "Pre-admission planning failure",
    }
    with pytest.raises(ValueError, match="Act authority changed"):
        asyncio.run(
            actions.handoff_goal({**arguments, "expected_authority_revision": 9})
        )
    assert work.calls == []
    result = asyncio.run(actions.handoff_goal(arguments))
    assert result["goal"]["status"] == "running"
    assert [call[0] for call in work.calls] == ["handoff", "start"]
    assert work.calls[0][2]["session_id"] == source.id
    assert work.calls[1][2] == {
        "session_id": "new-chat",
        "expected_revision": 1,
        "action": "start",
    }
