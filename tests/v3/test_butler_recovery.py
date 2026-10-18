"""Focused Act-mode receipts, project bounds, and parent retry verification."""

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
    record_subagent_start_failure,
)
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerGoalWatch,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoalStatus,
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


def _setup(tmp_path):
    store = NebulaStore(tmp_path / "recovery.db")
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Parent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=parent.id,
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    worker_id = "worker-1"
    store.create(
        ButlerProjectState(
            id=project_state_id(project.id),
            engagement_id=project.id,
            lease_owner=worker_id,
            lease_expires_at=utc_now() + timedelta(minutes=3),
        )
    )
    return store, project, parent, turn, policy, worker_id


def _failure(store, project, parent, turn, error=None):
    call = store.create(
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
    with store.transaction(immediate=True) as transaction:
        record_subagent_start_failure(
            transaction,
            call,
            error
            or CapacityReached(
                "full", resource="active_children", maximum=1, current=1
            ),
        )
    return call


def test_exact_subagent_refusal_creates_one_act_case_with_core_evidence(tmp_path):
    store, project, parent, turn, policy, worker_id = _setup(tmp_path)
    call = _failure(store, project, parent, turn)
    with store.transaction(immediate=True) as transaction:
        record_subagent_start_failure(
            transaction,
            call,
            CapacityReached("full", resource="active_children", maximum=1, current=1),
        )
    events = store.replay_operation_events(project_stream_id(project.id))
    assert [event.event_type for event in events] == ["agent.subagent_start_failed"]
    assert events[0].payload["metrics"]["category"] == "capacity_reached"
    assert events[0].payload["evidence_refs"][1]["id"] == call.id
    recovery = ButlerRecoveryService(store)
    assert recovery.capture_events(project.id, worker_id) == 1
    assert recovery.capture_events(project.id, worker_id) == 0
    cases = store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
    assert len(cases) == 1
    assert cases[0].source_tool_call_id == call.id
    assert cases[0].source_session_id == parent.id
    assert cases[0].authority_revision == policy.revision


def test_provider_broker_failed_subagent_call_wakes_butler_from_durable_receipt(
    tmp_path,
):
    store, project, parent, turn, _, _ = _setup(tmp_path)
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

    asyncio.run(scenario())
    failures = store.find_entities(
        ToolCall, {"tool_name": "subagent.start"}, engagement_id=project.id
    )
    assert len(failures) == 1 and failures[0].status == ToolCallStatus.FAILED
    events = store.replay_operation_events(project_stream_id(project.id))
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

    asyncio.run(denied_scenario())
    events = store.replay_operation_events(project_stream_id(project.id))
    assert len(events) == 2
    assert events[-1].payload["metrics"] == {
        "category": "permission_denied",
        "rule": "project_subagent_policy",
        "side_effects": "none",
    }


def test_harness_gateway_failure_commits_refusal_and_butler_signal_together(tmp_path):
    store, project, parent, turn, _, worker_id = _setup(tmp_path)
    call = store.create(
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
    HarnessRuntimeService._finish_gateway_call(
        SimpleNamespace(store=store),
        call.id,
        error=ToolNotPermitted("No runtime selected", rule="subagents.runtime_model"),
    )
    assert store.get(ToolCall, call.id).status == ToolCallStatus.FAILED
    events = store.replay_operation_events(project_stream_id(project.id))
    assert len(events) == 1
    assert events[0].payload["metrics"] == {
        "category": "permission_denied",
        "rule": "subagents.runtime_model",
        "side_effects": "none",
    }
    recovery = ButlerRecoveryService(store)
    assert recovery.capture_events(project.id, worker_id) == 1
    assert store.count_entities(ButlerRecoveryCase, engagement_id=project.id) == 1


def test_background_harness_analysis_has_only_butler_gateway_authority(tmp_path):
    store, project, _, _, _, _ = _setup(tmp_path)
    profile = store.create(
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

        def create_session(self, **kwargs):
            assert kwargs["control_assistant"] is True
            session = store.create(
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
            turn = store.get(HarnessTurn, turn_id)
            store.update(
                HarnessTurn,
                turn_id,
                {
                    "status": HarnessTurnStatus.COMPLETE,
                    "response": "Checked Core state",
                },
                expected_revision=turn.revision,
            )
            if False:
                yield None

        async def close_session(self, _session_id):
            return None

    runtime = Runtime()
    result = asyncio.run(
        HarnessRuntimeService.analyze_structured(
            runtime,
            engagement_id=project.id,
            profile_id=profile.id,
            model="model-a",
            prompt="Inspect case",
            nebula_tools=True,
            butler_recovery_case_id="case-1",
        )
    )
    assert result.status == HarnessTurnStatus.COMPLETE
    session = store.get(HarnessSession, runtime.session_id)
    assert session.metadata["butler_recovery_case_id"] == "case-1"
    assert session.metadata["command_runtime_enabled"] is False
    assert session.metadata["browser_runtime_enabled"] is False
    assert session.metadata[
        "native_capabilities"
    ] == HarnessNativeCapabilities().model_dump(mode="json")


def test_periodic_check_is_coalesced_and_act_revocation_stops_new_cases(tmp_path):
    store, project, parent, _, policy, worker_id = _setup(tmp_path)
    store.create(
        ButlerGoalWatch(
            engagement_id=project.id,
            goal_id="goal-1",
            session_id=parent.id,
            status=ChatGoalStatus.RUNNING,
            coverage_reason="Goal lifecycle",
        )
    )
    recovery = ButlerRecoveryService(store)
    assert recovery.fire_periodic_check(project.id, worker_id)
    assert not recovery.fire_periodic_check(project.id, worker_id)
    assert recovery.capture_events(project.id, worker_id) == 1
    assert store.count_entities(ButlerRecoveryCase, engagement_id=project.id) == 1
    store.update(
        ButlerAuthorityPolicy,
        policy.id,
        {"authority": "watch"},
        expected_revision=policy.revision,
    )
    assert not recovery.fire_periodic_check(project.id, worker_id)
    assert recovery._claim_case(project.id, worker_id) is None


def test_parent_follow_up_requires_same_child_task_completion_before_resolved(tmp_path):
    store, project, parent, turn, policy, _ = _setup(tmp_path)
    original = store.create(
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
    case = store.create(
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
    store.create(
        ChatQueue(
            id="chat-queue-" + parent.id,
            engagement_id=project.id,
            session_id=parent.id,
            items=[{"id": "item-1", "status": "sending", "turn_id": "retry-turn"}],
        )
    )
    recovery = ButlerRecoveryService(store)
    assert recovery.verify_follow_up(case).status == "verifying"
    store.create(
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
    assert (
        recovery.verify_follow_up(store.get(ButlerRecoveryCase, case.id)).status
        == "verifying"
    )
    child = store.create(
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
        recovery.verify_follow_up(store.get(ButlerRecoveryCase, case.id)).status
        == "verifying"
    )
    store.update(
        ChatSubagent,
        child.id,
        {"status": ChatSubagentStatus.COMPLETED, "finished_at": utc_now()},
        expected_revision=child.revision,
    )
    settled = recovery.verify_follow_up(store.get(ButlerRecoveryCase, case.id))
    assert settled.status == "resolved"
    assert "Core confirmed" in settled.outcome


def test_global_parent_guidance_completion_does_not_require_a_subagent(tmp_path):
    store, project, parent, turn, policy, _ = _setup(tmp_path)
    completed = store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    case = store.create(
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
    settled = ButlerRecoveryService(store).verify_follow_up(case)
    assert settled.status == "resolved"
    assert "guidance turn completed" in settled.outcome
    assert "Goal results remain independently tracked" in settled.outcome


def test_crash_with_possible_side_effect_becomes_uncertain_without_replay(tmp_path):
    store, project, _, _, policy, _ = _setup(tmp_path)
    case = store.create(
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
    assert recovery.recover_interrupted_cases(project.id) == 1
    assert store.get(ButlerRecoveryCase, case.id).status == "uncertain"
    assert recovery.recover_interrupted_cases(project.id) == 0


def test_act_switches_worker_to_provider_and_queues_only_the_parent(tmp_path):
    store, project, parent, turn, _, worker_id = _setup(tmp_path)
    provider = store.get(ProviderProfile, parent.provider_profile_id)
    store.update(
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
    store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            provider_profile_id=provider.id,
            model="model-a",
            metadata={"control_assistant": True},
        )
    )
    _failure(store, project, parent, turn, ConnectionError("runtime unavailable"))

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
    asyncio.run(recovery.process_project(project.id, worker_id))
    cases = store.find_entities(ButlerRecoveryCase, {}, engagement_id=project.id)
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
def test_periodic_case_distinguishes_live_reads_from_changes_and_renews_lease(
    tmp_path, monkeypatch, operation, expected_status
):
    store, project, _, _, policy, worker_id = _setup(tmp_path)
    state = store.get(ButlerProjectState, project_state_id(project.id))
    store.update(
        ButlerProjectState,
        state.id,
        {"lease_expires_at": utc_now() + timedelta(seconds=30)},
        expected_revision=state.revision,
    )
    case = store.create(
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
            current = store.get(ButlerRecoveryCase, case.id)
            store.update(
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
            return "Live Core checked", ChatBackend.PROVIDER, "provider", "model", None

    monkeypatch.setattr(recovery_module, "LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    asyncio.run(
        ButlerRecoveryService(store, Agent()).process_project(project.id, worker_id)
    )
    settled = store.get(ButlerRecoveryCase, case.id)
    assert settled.status == expected_status
    renewed = store.get(ButlerProjectState, state.id)
    assert renewed.lease_expires_at > utc_now() + timedelta(seconds=45)


@pytest.mark.parametrize(
    ("signal", "changed_state", "expected_status"),
    [
        ("butler.global_periodic_check", False, "resolved"),
        ("butler.global_periodic_check", True, "needs_attention"),
        ("butler.periodic_check", False, "needs_attention"),
    ],
)
def test_active_parent_turn_hold_does_not_block_read_only_global_check(
    tmp_path, signal, changed_state, expected_status
):
    store, project, parent, _, policy, worker_id = _setup(tmp_path)
    case = store.create(
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
            current = store.get(ButlerRecoveryCase, claimed.id)
            steps = [{"operation": "nebula.work.chat", "status": "applied"}]
            if changed_state:
                steps.append(
                    {"operation": "nebula.work.set_chat_settings", "status": "applied"}
                )
            steps.append({"operation": "nebula.work.message_chat", "status": "failed"})
            store.update(
                ButlerRecoveryCase,
                current.id,
                {
                    "steps": steps,
                    "delivery_hold_reason": "active_turn_cannot_steer",
                    "target_session_id": parent.id,
                },
                expected_revision=current.revision,
            )
            return "Read parent state", ChatBackend.PROVIDER, "provider", "model", None

    asyncio.run(
        ButlerRecoveryService(store, Agent()).process_project(project.id, worker_id)
    )
    settled = store.get(ButlerRecoveryCase, case.id)
    assert settled.status == expected_status
    if expected_status == "resolved":
        assert "not delivered" in settled.outcome
    else:
        assert "Review the parent chat" in settled.outcome


def test_interrupted_read_only_investigation_can_replay(tmp_path):
    store, project, _, _, policy, _ = _setup(tmp_path)
    case = store.create(
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
    assert recovery.recover_interrupted_cases(project.id) == 1
    assert store.get(ButlerRecoveryCase, case.id).status == "observed"


def test_lost_project_lease_cancels_investigation(tmp_path, monkeypatch):
    store, project, _, _, policy, worker_id = _setup(tmp_path)
    case = store.create(
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
    asyncio.run(recovery.process_project(project.id, worker_id))
    assert cancelled
    settled = store.get(ButlerRecoveryCase, case.id)
    assert settled.status == "needs_attention"
    assert "could not finish the investigation" in settled.outcome
