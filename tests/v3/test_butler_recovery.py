"""Focused Act-mode receipts, project bounds, and parent retry verification."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
import nebula.v3.butler_recovery as recovery_module
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_oversight import project_state_id, project_stream_id
from nebula.v3.butler_recovery import (
    ButlerRecoveryAgent,
    ButlerRecoveryService,
    GLOBAL_STATE_ID,
    record_subagent_start_failure,
)
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerGlobalState,
    ButlerGoalWatch,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatQueue,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    ProviderCapabilityVerification,
    ProviderVerificationStatus,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.tools import (
    AnalysisTool,
    CapacityReached,
    InvalidToolArguments,
    StoreToolLedger,
    PolicyDenied,
    ToolBroker,
    ToolInvocation,
    ToolNotPermitted,
    ToolRegistry,
    ToolSpec,
)
from nebula.v3.providers import ModelResponse, ToolCall as ModelToolCall
from nebula.v3.policy import PolicyDecision, PolicyEffect


async def _setup(tmp_path):
    store = NebulaStore(tmp_path / "recovery.db")
    project = await store.create(Engagement(name="Selected"))
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Parent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    worker_id = "worker-1"
    (
        await store.create(
            ButlerProjectState(
                id=project_state_id(project.id),
                engagement_id=project.id,
                lease_owner=worker_id,
                lease_expires_at=utc_now() + timedelta(minutes=3),
            )
        )
    )
    return store, project, parent, turn, policy, worker_id


async def _failure(store, project, parent, turn, error=None):
    call = await store.create(
        ToolCall(
            engagement_id=project.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.id,
            chat_turn_id=turn.id,
            tool_name="subagent.start",
            status=ToolCallStatus.FAILED,
            risk_class=RiskClass.LOCAL_READ,
            arguments={"task": "Check one boundary"},
            error="No capacity",
        )
    )
    async with store.transaction(immediate=True) as transaction:
        (
            await record_subagent_start_failure(
                transaction,
                call,
                error
                or CapacityReached(
                    "full", resource="active_children", maximum=1, current=1
                ),
            )
        )
    return call


async def test_exact_subagent_refusal_creates_one_act_case_with_core_evidence(tmp_path):
    store, project, parent, turn, policy, worker_id = await _setup(tmp_path)
    call = await _failure(store, project, parent, turn)
    async with store.transaction(immediate=True) as transaction:
        (
            await record_subagent_start_failure(
                transaction,
                call,
                CapacityReached(
                    "full", resource="active_children", maximum=1, current=1
                ),
            )
        )
    events = await store.replay_operation_events(project_stream_id(project.id))
    assert [event.event_type for event in events] == ["agent.subagent_start_failed"]
    assert events[0].payload["metrics"]["category"] == "capacity_reached"
    assert events[0].payload["evidence_refs"][1]["id"] == call.id
    recovery = ButlerRecoveryService(store)
    assert (await recovery.capture_events(project.id, worker_id)) == 1
    assert (await recovery.capture_events(project.id, worker_id)) == 0
    cases = await store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    assert len(cases) == 1
    assert cases[0].source_tool_call_id == call.id
    assert cases[0].source_session_id == parent.id
    assert cases[0].authority_revision == policy.revision


async def test_provider_broker_failed_subagent_call_wakes_butler_from_durable_receipt(
    tmp_path,
):
    store, project, parent, turn, _, _ = await _setup(tmp_path)
    registry = ToolRegistry()

    async def unused(_arguments):
        return {}

    registry.register(
        AnalysisTool(
            ToolSpec(
                name="subagent.start",
                description="Start a child",
                input_schema={
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                risk_class=RiskClass.LOCAL_READ,
            ),
            unused,
        )
    )
    broker = ToolBroker(
        registry=registry,
        policy_engine=None,
        runner=None,
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _: tmp_path,
    )
    invocation = ToolInvocation(
        id="bad-subagent-call",
        engagement_id=project.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=parent.id,
        chat_turn_id=turn.id,
        tool_name="subagent.start",
        arguments={},
        workspace=tmp_path,
    )

    async def scenario():
        with pytest.raises(InvalidToolArguments):
            await broker.prepare(invocation, ScopePolicy(engagement_id=project.id))

    (await scenario())
    failures = await store.find_entities(
        ToolCall, {"tool_name": "subagent.start"}, engagement_id=project.id
    )
    assert len(failures) == 1 and failures[0].status == ToolCallStatus.FAILED
    events = await store.replay_operation_events(project_stream_id(project.id))
    assert len(events) == 1 and events[0].event_type == "agent.subagent_start_failed"
    assert events[0].payload["metrics"]["category"] == "invalid_arguments"

    denied_broker = ToolBroker(
        registry=registry,
        policy_engine=SimpleNamespace(
            evaluate=lambda *_: PolicyDecision(
                effect=PolicyEffect.DENY,
                reason="Project setting disallows this tool",
                rule="project_subagent_policy",
            )
        ),
        runner=None,
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _: tmp_path,
    )
    denied_invocation = invocation.model_copy(
        update={
            "id": "denied-subagent-call",
            "arguments": {"task": "Check one boundary"},
        }
    )

    async def denied_scenario():
        with pytest.raises(PolicyDenied):
            await denied_broker.prepare(
                denied_invocation, ScopePolicy(engagement_id=project.id)
            )

    (await denied_scenario())
    events = await store.replay_operation_events(project_stream_id(project.id))
    assert len(events) == 2
    assert events[-1].payload["metrics"] == {
        "category": "permission_denied",
        "rule": "project_subagent_policy",
        "side_effects": "none",
    }


async def test_harness_gateway_failure_commits_refusal_and_butler_signal_together(
    tmp_path,
):
    store, project, parent, turn, _, worker_id = await _setup(tmp_path)
    call = await store.create(
        ToolCall(
            engagement_id=project.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.id,
            chat_turn_id=turn.id,
            tool_name="subagent.start",
            status=ToolCallStatus.RUNNING,
            risk_class=RiskClass.LOCAL_READ,
            arguments={"task": "Check one boundary"},
        )
    )
    (
        await HarnessRuntimeService._finish_gateway_call(
            SimpleNamespace(store=store),
            call.id,
            error=ToolNotPermitted(
                "No runtime selected", rule="subagents.runtime_model"
            ),
        )
    )
    assert (await store.get(ToolCall, call.id)).status == ToolCallStatus.FAILED
    events = await store.replay_operation_events(project_stream_id(project.id))
    assert len(events) == 1
    assert events[0].payload["metrics"] == {
        "category": "permission_denied",
        "rule": "subagents.runtime_model",
        "side_effects": "none",
    }
    recovery = ButlerRecoveryService(store)
    assert (await recovery.capture_events(project.id, worker_id)) == 1
    assert (
        await store.count_entities(ButlerRecoveryCase, engagement_id=project.id)
    ) == 1


async def test_background_harness_analysis_has_only_butler_gateway_authority(tmp_path):
    store, project, _, _, _, _ = await _setup(tmp_path)
    profile = await store.create(
        HarnessProfile(
            name="Butler harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            default_model="model-a",
            executable="/usr/bin/true",
        )
    )

    class Runtime:
        def __init__(self):
            self.store = store
            self.session_id = None

        def _validate_harness_privacy(self, *_args, **_kwargs):
            return None

        async def create_session(self, **kwargs):
            assert kwargs["control_assistant"] is True
            session = await store.create(
                HarnessSession(
                    engagement_id=project.id,
                    harness_profile_id=profile.id,
                    model=kwargs["model"],
                    metadata={"control_assistant": True},
                )
            )
            self.session_id = session.id
            return session

        async def stream_turn(self, turn_id):
            turn = await store.get(HarnessTurn, turn_id)
            (
                await store.update(
                    HarnessTurn,
                    turn_id,
                    {
                        "status": HarnessTurnStatus.COMPLETE,
                        "response": "Checked Core state",
                    },
                    expected_revision=turn.revision,
                )
            )
            if False:
                yield None

        async def close_session(self, _session_id):
            return None

    runtime = Runtime()
    result = await HarnessRuntimeService.analyze_structured(
        runtime,
        engagement_id=project.id,
        profile_id=profile.id,
        model="model-a",
        prompt="Inspect case",
        nebula_tools=True,
        butler_recovery_case_id="case-1",
    )
    assert result.status == HarnessTurnStatus.COMPLETE
    session = await store.get(HarnessSession, runtime.session_id)
    assert session.metadata["butler_recovery_case_id"] == "case-1"
    assert session.metadata["command_runtime_enabled"] is False
    assert session.metadata["browser_runtime_enabled"] is False
    assert session.metadata[
        "native_capabilities"
    ] == HarnessNativeCapabilities().model_dump(mode="json")


async def test_periodic_check_is_coalesced_and_act_revocation_stops_new_cases(tmp_path):
    store, project, parent, _, policy, worker_id = await _setup(tmp_path)
    (
        await store.create(
            ButlerGoalWatch(
                engagement_id=project.id,
                goal_id="goal-1",
                session_id=parent.id,
                status=ChatGoalStatus.RUNNING,
                coverage_reason="Goal lifecycle",
            )
        )
    )
    recovery = ButlerRecoveryService(store)
    assert await recovery.fire_periodic_check(project.id, worker_id)
    assert not (await recovery.fire_periodic_check(project.id, worker_id))
    assert (await recovery.capture_events(project.id, worker_id)) == 1
    assert (
        await store.count_entities(ButlerRecoveryCase, engagement_id=project.id)
    ) == 1
    (
        await store.update(
            ButlerAuthorityPolicy,
            policy.id,
            {"authority": "watch"},
            expected_revision=policy.revision,
        )
    )
    assert not (await recovery.fire_periodic_check(project.id, worker_id))
    assert (await recovery._claim_case(project.id, worker_id)) is None


async def test_project_fallback_check_uses_nebula_wide_saved_interval(tmp_path):
    store, project, parent, _, _, worker_id = await _setup(tmp_path)
    (
        await store.create(
            ButlerGoalWatch(
                engagement_id=project.id,
                goal_id="goal-1",
                session_id=parent.id,
                status=ChatGoalStatus.RUNNING,
                coverage_reason="Goal lifecycle",
            )
        )
    )
    state = await store.get(ButlerProjectState, project_state_id(project.id))
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"last_periodic_recovery_at": utc_now() - timedelta(minutes=3)},
            expected_revision=state.revision,
        )
    )
    recovery = ButlerRecoveryService(store)
    assert not (await recovery.fire_periodic_check(project.id, worker_id))
    (
        await store.create(
            ButlerGlobalState(
                id=GLOBAL_STATE_ID,
                check_interval_minutes=2,
            )
        )
    )
    assert await recovery.fire_periodic_check(project.id, worker_id)
    assert not (await recovery.fire_periodic_check(project.id, worker_id))


async def test_parent_follow_up_requires_same_child_task_completion_before_resolved(
    tmp_path,
):
    store, project, parent, turn, policy, _ = await _setup(tmp_path)
    original = await store.create(
        ToolCall(
            engagement_id=project.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.id,
            chat_turn_id=turn.id,
            tool_name="subagent.start",
            status=ToolCallStatus.FAILED,
            risk_class=RiskClass.LOCAL_READ,
            arguments={"task": "Check one boundary"},
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="immediate",
            signal="agent.subagent_start_failed",
            source_event_id="event-1",
            source_event_sequence=1,
            source_tool_call_id=original.id,
            authority_revision=policy.revision,
            status="verifying",
            summary="Subagent start failed",
            queued_item_id="item-1",
            target_session_id=parent.id,
            started_at=utc_now(),
        )
    )
    (
        await store.create(
            ChatQueue(
                id="chat-queue-" + parent.id,
                engagement_id=project.id,
                session_id=parent.id,
                items=[{"id": "item-1", "status": "sending", "turn_id": "retry-turn"}],
            )
        )
    )
    recovery = ButlerRecoveryService(store)
    assert (await recovery.verify_follow_up(case)).status == "verifying"
    (
        await store.create(
            ChatSubagent(
                engagement_id=project.id,
                parent_session_id=parent.id,
                parent_turn_id="retry-turn",
                child_session_id="other-child",
                name="Other worker",
                task="Work on a different task",
                status=ChatSubagentStatus.COMPLETED,
                finished_at=utc_now(),
            )
        )
    )
    assert (
        await recovery.verify_follow_up((await store.get(ButlerRecoveryCase, case.id)))
    ).status == "verifying"
    child = await store.create(
        ChatSubagent(
            engagement_id=project.id,
            parent_session_id=parent.id,
            parent_turn_id="retry-turn",
            child_session_id="child-chat",
            name="Boundary checker",
            task="Check one boundary",
        )
    )
    assert (
        await recovery.verify_follow_up((await store.get(ButlerRecoveryCase, case.id)))
    ).status == "verifying"
    (
        await store.update(
            ChatSubagent,
            child.id,
            {"status": ChatSubagentStatus.COMPLETED, "finished_at": utc_now()},
            expected_revision=child.revision,
        )
    )
    settled = await recovery.verify_follow_up(
        (await store.get(ButlerRecoveryCase, case.id))
    )
    assert settled.status == "resolved"
    assert "Core confirmed" in settled.outcome


async def test_legacy_global_parent_guidance_completion_does_not_require_a_subagent(
    tmp_path,
):
    store, project, parent, turn, policy, _ = await _setup(tmp_path)
    completed = await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="verifying",
            summary="Guide active project",
            delivered_turn_id=completed.id,
            target_session_id=parent.id,
            started_at=utc_now(),
        )
    )
    settled = await ButlerRecoveryService(store).verify_follow_up(case)
    assert settled.status == "resolved"
    assert "guidance turn completed" in settled.outcome
    assert "Goal results remain independently tracked" in settled.outcome


async def test_blocked_goal_checkin_continues_same_case_through_agent_conversation(
    tmp_path,
):
    store, project, parent, first_turn, policy, worker_id = await _setup(tmp_path)
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=parent.id,
            objective="Finish the research",
            completion_criteria=["Evidence saved"],
            status=ChatGoalStatus.BLOCKED,
            blocked_reason="Missing trace",
        )
    )
    second_turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=parent.provider_profile_id,
            model="model-a",
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="observed",
            summary="Help the blocked main agent",
        )
    )

    class Agent:
        calls = 0

        async def run(self, claimed):
            assert claimed.id == case.id
            self.calls += 1
            current = await store.get(ButlerRecoveryCase, case.id)
            if self.calls == 1:
                assert current.dialogue_results == []
                target = first_turn
            elif self.calls == 2:
                assert current.dialogue_results[-1]["turn_id"] == first_turn.id
                assert current.dialogue_read_turn_ids == []
                target = second_turn
            else:
                assert self.calls == 3
                assert current.dialogue_results[-1]["turn_id"] == second_turn.id
                assert current.dialogue_read_turn_ids == [first_turn.id]
                target = None
            changes = {
                "steps": [
                    *current.steps,
                    {
                        "operation": "nebula.work.await_chat_result"
                        if target is None
                        else "nebula.work.message_chat",
                        "status": "applied",
                    },
                ],
            }
            if self.calls > 1:
                changes["dialogue_read_turn_ids"] = [
                    *current.dialogue_read_turn_ids,
                    current.dialogue_results[-1]["turn_id"],
                ]
            if target is not None:
                changes["dialogue_pending_turns"] = [
                    {
                        "project_id": project.id,
                        "session_id": parent.id,
                        "turn_id": target.id,
                    }
                ]
                changes["delivered_turn_id"] = target.id
                changes["target_session_id"] = parent.id
            (
                await store.update(
                    ButlerRecoveryCase,
                    current.id,
                    changes,
                    expected_revision=current.revision,
                )
            )
            return (
                f"Butler conversation turn {self.calls}",
                ChatBackend.PROVIDER,
                "local",
                "model-a",
                None,
            )

    agent = Agent()
    recovery = ButlerRecoveryService(store, agent)
    (await recovery.process_project(project.id, worker_id))
    assert (await store.get(ButlerRecoveryCase, case.id)).status == "verifying"
    assert agent.calls == 1

    (
        await store.update(
            ChatTurn,
            first_turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=first_turn.revision,
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=parent.id,
                sequence=1,
                role=ChatRole.ASSISTANT,
                content="I need the saved trace; the goal is still blocked.",
                metadata={"chat_turn_id": first_turn.id},
            )
        )
    )
    pending = await recovery.verify_follow_up(
        (await store.get(ButlerRecoveryCase, case.id))
    )
    assert pending.status == "observed"
    assert pending.id == case.id
    assert pending.dialogue_results[-1]["turn_id"] == first_turn.id
    assert pending.finished_at is None

    (await recovery.process_project(project.id, worker_id))
    assert (await store.get(ButlerRecoveryCase, case.id)).status == "verifying"
    assert agent.calls == 2
    (
        await store.update(
            ChatTurn,
            second_turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=second_turn.revision,
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=parent.id,
                sequence=2,
                role=ChatRole.ASSISTANT,
                content="I found the trace and resumed the goal.",
                metadata={"chat_turn_id": second_turn.id},
            )
        )
    )
    (
        await store.update(
            ChatGoal,
            goal.id,
            {"status": ChatGoalStatus.RUNNING, "blocked_reason": None},
            expected_revision=goal.revision,
        )
    )
    assert (
        await recovery.verify_follow_up((await store.get(ButlerRecoveryCase, case.id)))
    ).status == "observed"
    (await recovery.process_project(project.id, worker_id))
    assert agent.calls == 3
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "resolved"
    assert settled.dialogue_read_turn_ids == [first_turn.id, second_turn.id]
    assert len(settled.dialogue_results) == 2


async def test_completed_main_turn_waits_for_its_saved_reply(tmp_path):
    store, project, parent, turn, policy, _ = await _setup(tmp_path)
    completed = await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="verifying",
            summary="Help the blocked main agent",
            started_at=utc_now(),
            dialogue_pending_turns=[
                {
                    "project_id": project.id,
                    "session_id": parent.id,
                    "turn_id": completed.id,
                }
            ],
        )
    )
    recovery = ButlerRecoveryService(store)
    waiting = await recovery.verify_follow_up(case)
    assert waiting.status == "verifying"
    assert waiting.dialogue_results == []
    (
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=parent.id,
                sequence=1,
                role=ChatRole.ASSISTANT,
                content="The missing trace is my blocker.",
                metadata={"chat_turn_id": completed.id},
            )
        )
    )
    ready = await recovery.verify_follow_up(
        (await store.get(ButlerRecoveryCase, case.id))
    )
    assert ready.status == "observed"
    assert ready.dialogue_results[-1]["turn_id"] == completed.id


async def test_crash_with_possible_side_effect_becomes_uncertain_without_replay(
    tmp_path,
):
    store, project, _, _, policy, _ = await _setup(tmp_path)
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="immediate",
            signal="agent.subagent_start_failed",
            source_event_id="event-1",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="investigating",
            summary="Subagent start failed",
            started_at=utc_now() - timedelta(minutes=2),
            steps=[
                {
                    "id": "step-1",
                    "operation": "nebula.work.set_chat_settings",
                    "status": "running",
                }
            ],
        )
    )
    recovery = ButlerRecoveryService(store)
    assert (await recovery.recover_interrupted_cases(project.id)) == 1
    assert (await store.get(ButlerRecoveryCase, case.id)).status == "uncertain"
    assert (await recovery.recover_interrupted_cases(project.id)) == 0


async def test_act_switches_worker_to_provider_and_queues_only_the_parent(tmp_path):
    store, project, parent, turn, _, worker_id = await _setup(tmp_path)
    provider = await store.get(ProviderProfile, parent.provider_profile_id)
    (
        await store.update(
            ProviderProfile,
            provider.id,
            {
                "capability_verifications": {
                    "model-a": ProviderCapabilityVerification(
                        model="model-a", status=ProviderVerificationStatus.VERIFIED
                    ).model_dump(mode="json")
                }
            },
            expected_revision=provider.revision,
        )
    )
    (
        await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Butler",
                provider_profile_id=provider.id,
                model="model-a",
                metadata={"control_assistant": True},
            )
        )
    )
    (
        await _failure(
            store, project, parent, turn, ConnectionError("runtime unavailable")
        )
    )

    class Provider:
        config = SimpleNamespace(local=True)

        def require(self, request):
            assert request.metadata["operation"] == "butler_recovery"
            assert len(request.tools) == 4

        async def complete(self, request):
            step = len(request.tool_results)
            if step == 0:
                return ModelResponse(
                    provider_id=provider.id,
                    model="model-a",
                    tool_calls=[
                        ModelToolCall(
                            id="call-1",
                            name="nebula.capabilities.read",
                            arguments={
                                "operation": "nebula.work.chat_settings",
                                "arguments": {
                                    "session_id": parent.id,
                                },
                            },
                        )
                    ],
                )
            assert request.tool_results[-1].is_error is False
            if step == 1:
                return ModelResponse(
                    provider_id=provider.id,
                    model="model-a",
                    tool_calls=[
                        ModelToolCall(
                            id="call-2",
                            name="nebula.capabilities.change",
                            arguments={
                                "operation": "nebula.work.set_chat_settings",
                                "arguments": {
                                    "session_id": parent.id,
                                    "expected_revision": 1,
                                    "settings": {
                                        "subagent_provider_id": provider.id,
                                        "subagent_harness_profile_id": None,
                                        "subagent_model": "model-a",
                                    },
                                },
                            },
                        )
                    ],
                )
            if step == 2:
                return ModelResponse(
                    provider_id=provider.id,
                    model="model-a",
                    tool_calls=[
                        ModelToolCall(
                            id="call-3",
                            name="nebula.capabilities.change",
                            arguments={
                                "operation": "nebula.work.message_chat",
                                "arguments": {
                                    "session_id": parent.id,
                                    "text": "Please retry the bounded check.",
                                    "delivery": "immediate",
                                },
                            },
                        )
                    ],
                )
            return ModelResponse(
                provider_id=provider.id,
                model="model-a",
                text="Parent guidance delivered",
            )

    class Work:
        def __init__(self):
            self.targets = []
            self.changes = []
            self.validated = []
            self.settings = {
                "backend": "harness",
                "allow_subagents": True,
                "subagent_harness_profile_id": "old-harness",
                "subagent_provider_id": None,
                "subagent_model": None,
                "max_active_subagents": 2,
                "subagent_reasoning_effort": None,
            }
            self.chat = SimpleNamespace(
                subagents=SimpleNamespace(
                    validate_harness_setting=lambda *args: self.validated.append(args)
                )
            )

        def chat_settings(self, project_id, session_id):
            assert (project_id, session_id) == (project.id, parent.id)
            return {"revision": 1, "settings": dict(self.settings)}

        def set_chat_settings(self, project_id, arguments):
            assert project_id == project.id
            assert arguments["session_id"] == parent.id
            assert arguments["expected_revision"] == 1
            self.changes.append(arguments["settings"])
            self.settings.update(arguments["settings"])
            return {
                "session_id": parent.id,
                "revision": 2,
                "settings": dict(self.settings),
            }

        async def message_chat(self, project_id, arguments, *, idempotency_key):
            self.targets.append((project_id, arguments["session_id"], idempotency_key))
            assert arguments["delivery"] == "immediate"
            return {"delivery": "started", "turn_id": "accepted-parent-turn"}

    work = Work()
    agent = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: Provider(),
        harness_runtime=None,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        work=work,
        memory=None,
    )
    recovery = ButlerRecoveryService(store, agent)
    (await recovery.process_project(project.id, worker_id))
    cases = await store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    assert len(cases) == 1
    assert cases[0].status == "verifying"
    assert cases[0].delivered_turn_id == "accepted-parent-turn"
    assert [step["operation"] for step in cases[0].steps] == [
        "nebula.work.chat_settings",
        "nebula.work.set_chat_settings",
        "nebula.work.message_chat",
    ]
    assert work.changes == [
        {
            "subagent_provider_id": provider.id,
            "subagent_harness_profile_id": None,
            "subagent_model": "model-a",
        }
    ]
    assert work.validated[0][:3] == (project.id, provider.id, "model-a")
    assert len(work.targets) == 1
    assert work.targets[0][:2] == (project.id, parent.id)
    assert work.targets[0][2].startswith("recovery:" + cases[0].id)


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("nebula.work.goals", "resolved"),
        ("nebula.work.set_chat_settings", "needs_attention"),
    ],
)
async def test_periodic_case_distinguishes_live_reads_from_changes_and_renews_lease(
    tmp_path, monkeypatch, operation, expected_status
):
    store, project, _, _, policy, worker_id = await _setup(tmp_path)
    state = await store.get(ButlerProjectState, project_state_id(project.id))
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {"lease_expires_at": utc_now() + timedelta(seconds=30)},
            expected_revision=state.revision,
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="observed",
            summary="Check project work",
        )
    )

    class Agent:
        async def run(self, claimed):
            assert claimed.id == case.id
            await asyncio.sleep(0.05)
            current = await store.get(ButlerRecoveryCase, case.id)
            (
                await store.update(
                    ButlerRecoveryCase,
                    case.id,
                    {
                        "steps": [
                            {
                                "id": "observed-step",
                                "operation": operation,
                                "status": "applied",
                            }
                        ]
                    },
                    expected_revision=current.revision,
                )
            )
            return "Live Core checked", ChatBackend.PROVIDER, "provider", "model", None

    monkeypatch.setattr(recovery_module, "LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    (await ButlerRecoveryService(store, Agent()).process_project(project.id, worker_id))
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == expected_status
    renewed = await store.get(ButlerProjectState, state.id)
    assert renewed.lease_expires_at > utc_now() + timedelta(seconds=45)


async def test_global_periodic_reads_do_not_resolve_when_all_goals_are_idle(tmp_path):
    store, project, parent, _, policy, worker_id = await _setup(tmp_path)
    (
        await store.create(
            ChatGoal(
                engagement_id=project.id,
                session_id=parent.id,
                objective="Finish the research",
                completion_criteria=["Evidence saved"],
                status=ChatGoalStatus.PAUSED,
                elapsed_seconds=14_401,
                time_budget_seconds=14_400,
            )
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="global-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="observed",
            summary="Check all goals",
        )
    )

    class Agent:
        async def run(self, claimed):
            current = await store.get(ButlerRecoveryCase, claimed.id)
            (
                await store.update(
                    ButlerRecoveryCase,
                    claimed.id,
                    {
                        "steps": [
                            {
                                "id": "read-step",
                                "operation": "nebula.global.project",
                                "status": "applied",
                            }
                        ]
                    },
                    expected_revision=current.revision,
                )
            )
            return "All goals are okay", ChatBackend.PROVIDER, "provider", "model", None

    (await ButlerRecoveryService(store, Agent()).process_project(project.id, worker_id))
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "needs_attention"
    assert "No main goal is running" in settled.outcome


@pytest.mark.parametrize(
    ("signal", "changed_state", "expected_status"),
    [
        ("butler.global_periodic_check", False, "resolved"),
        ("butler.global_periodic_check", True, "needs_attention"),
        ("butler.periodic_check", False, "needs_attention"),
    ],
)
async def test_active_parent_turn_hold_does_not_block_read_only_global_check(
    tmp_path, signal, changed_state, expected_status
):
    store, project, parent, _, policy, worker_id = await _setup(tmp_path)
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal=signal,
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="observed",
            summary="Check active parent work",
        )
    )

    class Agent:
        async def run(self, claimed):
            current = await store.get(ButlerRecoveryCase, claimed.id)
            steps = [{"operation": "nebula.work.chat", "status": "applied"}]
            if changed_state:
                steps.append(
                    {"operation": "nebula.work.set_chat_settings", "status": "applied"}
                )
            steps.append({"operation": "nebula.work.message_chat", "status": "failed"})
            (
                await store.update(
                    ButlerRecoveryCase,
                    current.id,
                    {
                        "steps": steps,
                        "delivery_hold_reason": "active_turn_cannot_steer",
                        "target_session_id": parent.id,
                    },
                    expected_revision=current.revision,
                )
            )
            return "Read parent state", ChatBackend.PROVIDER, "provider", "model", None

    (await ButlerRecoveryService(store, Agent()).process_project(project.id, worker_id))
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == expected_status
    if expected_status == "resolved":
        assert "not delivered" in settled.outcome
    else:
        assert "Review the parent chat" in settled.outcome


async def test_interrupted_read_only_investigation_can_replay(tmp_path):
    store, project, _, _, policy, _ = await _setup(tmp_path)
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="investigating",
            summary="Check project work",
            started_at=utc_now() - timedelta(minutes=2),
            steps=[
                {"id": "read", "operation": "nebula.work.goals", "status": "applied"}
            ],
        )
    )
    recovery = ButlerRecoveryService(store)
    assert (await recovery.recover_interrupted_cases(project.id)) == 1
    assert (await store.get(ButlerRecoveryCase, case.id)).status == "observed"


async def test_lost_project_lease_cancels_investigation(tmp_path, monkeypatch):
    store, project, _, _, policy, worker_id = await _setup(tmp_path)
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.periodic_check",
            source_event_id="periodic-event",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="observed",
            summary="Check project work",
        )
    )
    cancelled = False

    class Agent:
        async def run(self, claimed):
            nonlocal cancelled
            assert claimed.id == case.id
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled = True
                raise

    recovery = ButlerRecoveryService(store, Agent())
    monkeypatch.setattr(recovery_module, "LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(recovery, "_renew_project_lease", lambda *_: False)
    (await recovery.process_project(project.id, worker_id))
    assert cancelled
    settled = await store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "needs_attention"
    assert "could not finish the investigation" in settled.outcome


pytestmark = pytest.mark.anyio
