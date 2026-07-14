import asyncio
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from nebula.v3.domain import (
    AgentRun,
    Approval,
    ContextOwnerType,
    ContextSnapshot,
    Engagement,
    ProviderProfile,
    RiskClass,
    RunBudget,
    RunStatus,
    Task,
    TaskStatus,
)
from nebula.v3.orchestration import (
    EvidenceVerifier,
    MissionError,
    MissionPlan,
    MissionRuntime,
    ModelSpecialist,
    PlannedTask,
    SpecialistOutcome,
    SpecialistResult,
    SpecialistRole,
    VerificationResult,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ApprovalRequired


class PlannedSupervisor:
    def __init__(self, plan):
        self.plan_value = plan
        self.received_results = None

    async def plan(self, objective, context, budget):
        del objective, context, budget
        return self.plan_value

    async def synthesize(self, objective, plan, results):
        del plan
        self.received_results = dict(results)
        return f"complete: {objective}"


class RecordingSpecialist:
    def __init__(self, role, result):
        self.role = role
        self.result = result
        self.allowed_tools = frozenset({"scan.tcp"})
        self.contexts = []

    async def run(self, context):
        self.contexts.append(context)
        return self.result


class InvestigatingSpecialist:
    role = SpecialistRole.NETWORK_SERVICE
    allowed_tools = frozenset({"scan.tcp"})

    def __init__(self, results):
        self.results = list(results)
        self.contexts = []

    async def run(self, context):
        self.contexts.append(context)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ApprovalSpecialist:
    role = SpecialistRole.NETWORK_SERVICE
    allowed_tools = frozenset({"scan.tcp"})

    def __init__(self):
        self.contexts = []

    async def run(self, context):
        self.contexts.append(context)
        if context.approval_response is None:
            raise ApprovalRequired(
                Approval(
                    id="approval-1",
                    engagement_id=context.engagement_id,
                    run_id=context.run_id,
                    task_id=context.task.id,
                    risk_class=RiskClass.ACTIVE_SCAN,
                    exact_request={
                        "tool_name": "scan.tcp",
                        "arguments": {"ports": [443]},
                    },
                    target="10.0.0.8",
                    policy_rationale="active scanning requires operator approval",
                    requested_by="network-specialist",
                )
            )
        return SpecialistResult(summary="approved scan analyzed")


class CompactingMissionProvider(ModelProvider):
    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                id="provider-a",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(),
            )
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.metadata.get("operation") == "context_compaction":
            text = json.dumps(
                {"summary": "Scope result compacted with canonical provenance."}
            )
        elif request.metadata.get("task_id") == "planning":
            text = "scope-result " * 180
        else:
            text = "report drafted from compacted dependency context"
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text=text,
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def _runtime(tmp_path, supervisor, specialists, verifier=None):
    store = NebulaStore(tmp_path / "nebula.db")
    runtime = MissionRuntime(
        store=store,
        checkpointer=InMemorySaver(),
        supervisor=supervisor,
        specialists=specialists,
        verifier=verifier,
    )
    return runtime, store


def test_in_memory_mission_respects_dependencies_and_verifies_evidence(tmp_path):
    planning = PlannedTask(
        id="planning",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Review scope",
        instructions="Normalize the supplied scope",
    )
    reporting = PlannedTask(
        id="reporting",
        role=SpecialistRole.REPORTING_REMEDIATION,
        title="Prepare result",
        instructions="Summarize independently verified evidence",
        depends_on=[planning.id],
    )
    supervisor = PlannedSupervisor(
        MissionPlan(
            summary="Bounded two-stage mission",
            rationale="Reporting depends on normalized scope",
            tasks=[planning, reporting],
        )
    )
    planner = RecordingSpecialist(
        SpecialistRole.SCOPE_PLANNING,
        SpecialistResult(
            summary="scope normalized",
            candidate_finding_ids=["finding-1"],
            evidence_ids=["evidence-1"],
            reproducible_steps=["Re-run the passive parser against artifact-1"],
            input_tokens=3,
            output_tokens=2,
        ),
    )
    reporter = RecordingSpecialist(
        SpecialistRole.REPORTING_REMEDIATION,
        SpecialistResult(summary="report drafted", input_tokens=2, output_tokens=3),
    )
    runtime, store = _runtime(
        tmp_path,
        supervisor,
        {
            SpecialistRole.SCOPE_PLANNING: planner,
            SpecialistRole.REPORTING_REMEDIATION: reporter,
        },
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-1",
            objective="Produce a defensible report",
            budget=RunBudget(max_tokens=10, max_tool_calls=0),
        )
    )

    assert state["final_summary"] == "complete: Produce a defensible report"
    assert state["input_tokens"] + state["output_tokens"] == 10
    assert state["tool_calls"] == 0
    assert state["verification"][planning.id]["accepted"] is True
    assert reporter.contexts[0].prior_results[planning.id].summary == "scope normalized"
    assert planner.contexts[0].allowed_tools == frozenset()
    assert reporter.contexts[0].allowed_tools == frozenset()
    assert store.get(AgentRun, state["run_id"]).status == RunStatus.COMPLETE
    assert store.get(Task, planning.id).status == TaskStatus.COMPLETE
    assert store.get(Task, reporting.id).status == TaskStatus.COMPLETE
    assert [event.event_type for event in store.replay_events(state["run_id"])] == [
        "run.started",
        "run.planned",
        "task.started",
        "task.completed",
        "task.verified",
        "task.started",
        "task.completed",
        "task.verified",
        "run.completed",
    ]


def test_investigative_turns_continue_without_consuming_retry_budget(tmp_path):
    task = PlannedTask(
        id="investigate",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Investigate service",
        instructions="Correct failures before finishing",
    )
    specialist = InvestigatingSpecialist(
        [
            SpecialistResult(
                summary="scan failed with exit code 2",
                outcome=SpecialistOutcome.CONTINUE,
                output={"tool": "scan.tcp", "status": "failed"},
                tool_calls=1,
            ),
            SpecialistResult(summary="corrected scan completed"),
        ]
    )
    runtime, store = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(summary="Investigate", rationale="Iterate", tasks=[task])
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-turns",
            objective="Inspect the service",
            budget=RunBudget(max_tool_calls=2, max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert state["attempts"] == {task.id: 2}
    assert state["retry_counts"] == {}
    assert state["tool_calls"] == 1
    assert specialist.contexts[1].prior_turns[0].summary.startswith("scan failed")
    assert [event.event_type for event in store.replay_events(state["run_id"])] == [
        "run.started",
        "run.planned",
        "task.started",
        "task.turn_completed",
        "task.continuing",
        "task.started",
        "task.completed",
        "task.verified",
        "run.completed",
    ]


def test_exact_token_exhaustion_blocks_before_another_turn(tmp_path):
    task = PlannedTask(
        id="token-exhausted",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Bounded investigation",
        instructions="Do not exceed the token budget",
    )
    specialist = InvestigatingSpecialist(
        [
            SpecialistResult(
                summary="partial observation",
                outcome=SpecialistOutcome.CONTINUE,
                input_tokens=1,
            )
        ]
    )
    runtime, store = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(summary="Bounded", rationale="Hard cap", tasks=[task])
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-token-exhausted",
            objective="Stop at the exact token cap",
            budget=RunBudget(max_tokens=1, max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.BLOCKED.value}
    assert state["retry_counts"] == {}
    assert len(specialist.contexts) == 1
    assert (
        store.get(AgentRun, state["run_id"]).metadata["partial_observations"][0][
            "summary"
        ]
        == "partial observation"
    )


def test_runtime_retry_receives_prior_error_separately_from_turns(tmp_path):
    task = PlannedTask(
        id="retry",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Retry provider",
        instructions="Recover from a transient provider error",
    )
    specialist = InvestigatingSpecialist(
        [
            RuntimeError("provider temporarily unavailable"),
            SpecialistResult(summary="ok"),
        ]
    )
    runtime, _ = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(summary="Retry", rationale="Transient", tasks=[task])
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-retry",
            objective="Recover",
            budget=RunBudget(max_tool_calls=1, max_retries=1),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert state["retry_counts"] == {task.id: 1}
    assert specialist.contexts[1].prior_turns == []
    assert specialist.contexts[1].retry_errors == ["provider temporarily unavailable"]


def test_verification_rejection_requests_new_evidence_before_completion(tmp_path):
    task = PlannedTask(
        id="verify-more",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Verify candidate",
        instructions="Gather reproducible evidence",
    )
    specialist = InvestigatingSpecialist(
        [
            SpecialistResult(
                summary="candidate without evidence",
                candidate_finding_ids=["finding-1"],
            ),
            SpecialistResult(
                summary="candidate verified",
                candidate_finding_ids=["finding-1"],
                evidence_ids=["evidence-1"],
                reproducible_steps=["scan --safe target"],
                tool_calls=1,
            ),
        ]
    )
    runtime, store = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(summary="Verify", rationale="Evidence gate", tasks=[task])
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-verify",
            objective="Verify the candidate",
            budget=RunBudget(max_tool_calls=1, max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert "Verification rejected" in specialist.contexts[1].retry_errors[0]
    assert specialist.contexts[1].prior_turns[0].summary == (
        "candidate without evidence"
    )
    event_types = [event.event_type for event in store.replay_events(state["run_id"])]
    assert event_types.count("task.verification_failed") == 1
    assert "task.continuing" in event_types
    assert event_types[-1] == "run.completed"


def test_unchanged_evidence_is_not_reverified(tmp_path):
    class RecordingVerifier:
        def __init__(self):
            self.evidence = []

        async def verify(self, task, result):
            del task
            self.evidence.append(list(result.evidence_ids))
            accepted = result.evidence_ids == ["evidence-2"]
            return VerificationResult(
                accepted=accepted,
                rationale="accepted" if accepted else "new evidence required",
                evidence_ids=result.evidence_ids,
            )

    task = PlannedTask(
        id="verify-changed",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Add evidence",
        instructions="Do not resubmit unchanged evidence",
    )
    specialist = InvestigatingSpecialist(
        [
            SpecialistResult(
                summary="first package",
                evidence_ids=["evidence-1"],
                reproducible_steps=["scan target"],
            ),
            SpecialistResult(
                summary="unchanged package",
                evidence_ids=["evidence-1"],
                reproducible_steps=["scan target"],
                tool_calls=1,
            ),
            SpecialistResult(
                summary="new package",
                evidence_ids=["evidence-2"],
                reproducible_steps=["scan target --detail"],
                tool_calls=1,
            ),
        ]
    )
    verifier = RecordingVerifier()
    runtime, _ = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(summary="Verify", rationale="Evidence gate", tasks=[task])
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
        verifier,
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-verify-changed",
            objective="Produce new evidence",
            budget=RunBudget(max_tool_calls=2, max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert verifier.evidence == [["evidence-1"], ["evidence-2"]]
    assert "unchanged" in specialist.contexts[2].retry_errors[-1]


def test_mission_compacts_only_model_facing_dependency_context_and_charges_usage(
    tmp_path,
):
    store = NebulaStore(tmp_path / "mission-context.db")
    engagement = store.create(Engagement(id="engagement-1", name="Compaction"))
    store.create(
        ProviderProfile(
            id="provider-a",
            name="Local model",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={
                "default_model": "model-a",
                "options": {"context_window": 600, "max_output_tokens": 100},
            },
        )
    )
    planning = PlannedTask(
        id="planning",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Review scope",
        instructions="Produce a detailed bounded scope result",
    )
    reporting = PlannedTask(
        id="reporting",
        role=SpecialistRole.REPORTING_REMEDIATION,
        title="Report",
        instructions="Use the prior scope result",
        depends_on=[planning.id],
    )
    provider = CompactingMissionProvider()
    runtime = MissionRuntime(
        store=store,
        checkpointer=InMemorySaver(),
        supervisor=PlannedSupervisor(
            MissionPlan(
                summary="Compaction mission",
                rationale="Exercise bounded dependency context",
                tasks=[planning, reporting],
            )
        ),
        specialists={
            SpecialistRole.SCOPE_PLANNING: ModelSpecialist(
                provider,
                role=SpecialistRole.SCOPE_PLANNING,
                model="model-a",
                max_output_tokens=100,
            ),
            SpecialistRole.REPORTING_REMEDIATION: ModelSpecialist(
                provider,
                role=SpecialistRole.REPORTING_REMEDIATION,
                model="model-a",
                max_output_tokens=100,
            ),
        },
    )

    state = asyncio.run(
        runtime.start(
            engagement_id=engagement.id,
            objective="Produce a bounded report",
            budget=RunBudget(max_tokens=2_000, max_tool_calls=0),
            provider_id=provider.config.id,
            model="model-a",
        )
    )

    compaction_requests = [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]
    report_request = next(
        request
        for request in provider.requests
        if request.metadata.get("task_id") == reporting.id
    )
    assert compaction_requests
    assert "DERIVED WORKING MEMORY" in str(report_request.messages[0].content)
    assert state["input_tokens"] + state["output_tokens"] == 30 + 15 * len(
        compaction_requests
    )
    assert state["results"][planning.id]["summary"].startswith("scope-result")
    assert store.get(AgentRun, state["run_id"]).status == RunStatus.COMPLETE
    snapshot = store.list_entities(
        ContextSnapshot, engagement_id=engagement.id, limit=100
    )[0]
    assert snapshot.owner_type == ContextOwnerType.AGENT_RUN
    assert snapshot.source_references[0].source_id == planning.id
    event_types = [event.event_type for event in store.replay_events(state["run_id"])]
    assert "context.compaction_started" in event_types
    assert "context.compacted" in event_types


def test_mission_rejects_compaction_before_call_when_budget_is_insufficient(
    tmp_path,
):
    store = NebulaStore(tmp_path / "mission-context-budget.db")
    engagement = store.create(Engagement(id="engagement-1", name="Compaction"))
    store.create(
        ProviderProfile(
            id="provider-a",
            name="Local model",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={
                "default_model": "model-a",
                "options": {"context_window": 600, "max_output_tokens": 100},
            },
        )
    )
    planning = PlannedTask(
        id="planning",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Review scope",
        instructions="Produce a detailed bounded scope result",
    )
    reporting = PlannedTask(
        id="reporting",
        role=SpecialistRole.REPORTING_REMEDIATION,
        title="Report",
        instructions="Use the prior scope result",
        depends_on=[planning.id],
    )
    provider = CompactingMissionProvider()
    runtime = MissionRuntime(
        store=store,
        checkpointer=InMemorySaver(),
        supervisor=PlannedSupervisor(
            MissionPlan(
                summary="Compaction budget mission",
                rationale="Reject before spending beyond the cap",
                tasks=[planning, reporting],
            )
        ),
        specialists={
            SpecialistRole.SCOPE_PLANNING: ModelSpecialist(
                provider,
                role=SpecialistRole.SCOPE_PLANNING,
                model="model-a",
                max_output_tokens=100,
            ),
            SpecialistRole.REPORTING_REMEDIATION: ModelSpecialist(
                provider,
                role=SpecialistRole.REPORTING_REMEDIATION,
                model="model-a",
                max_output_tokens=100,
            ),
        },
    )

    state = asyncio.run(
        runtime.start(
            engagement_id=engagement.id,
            objective="Produce a bounded report",
            budget=RunBudget(
                max_tokens=120,
                max_tool_calls=0,
                max_retries=0,
            ),
            provider_id=provider.config.id,
            model="model-a",
        )
    )

    assert not [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]
    assert state["task_status"][reporting.id] == TaskStatus.BLOCKED.value
    assert "insufficient mission token budget" in state["errors"][reporting.id]
    assert state["input_tokens"] + state["output_tokens"] == 15
    event_types = [event.event_type for event in store.replay_events(state["run_id"])]
    assert "context.compaction_started" in event_types
    assert "context.compaction_failed" in event_types


def test_approval_checkpoint_resumes_same_attempt_with_zero_retries(tmp_path):
    task = PlannedTask(
        id="scan",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Scan approved service",
        instructions="Perform only the approved operation",
        risk_class=RiskClass.ACTIVE_SCAN,
    )
    supervisor = PlannedSupervisor(
        MissionPlan(summary="Approval mission", rationale="Operator gate", tasks=[task])
    )
    specialist = ApprovalSpecialist()
    runtime, store = _runtime(
        tmp_path,
        supervisor,
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    waiting = asyncio.run(
        runtime.start(
            engagement_id="engagement-approval",
            objective="Run one bounded scan",
            budget=RunBudget(max_retries=0),
        )
    )
    assert waiting["task_status"] == {task.id: TaskStatus.WAITING_APPROVAL.value}
    assert waiting["attempts"] == {task.id: 0}
    assert waiting["__interrupt__"][0].value["kind"] == "tool_approval"

    completed = asyncio.run(
        runtime.resume(waiting["run_id"], {"status": "approved", "operator": "alice"})
    )

    assert completed["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert completed["attempts"] == {task.id: 1}
    assert completed["final_summary"] == "complete: Run one bounded scan"
    assert specialist.contexts[-1].approval_response == {
        "status": "approved",
        "operator": "alice",
    }
    assert [event.event_type for event in store.replay_events(waiting["run_id"])] == [
        "run.started",
        "run.planned",
        "task.started",
        "approval.resolved",
        "task.completed",
        "task.verified",
        "run.completed",
    ]


def test_rejected_approval_becomes_an_observation_and_allows_an_alternative(
    tmp_path,
):
    class RejectionAwareSpecialist:
        role = SpecialistRole.NETWORK_SERVICE
        allowed_tools = frozenset({"scan.tcp", "scan.passive"})

        def __init__(self):
            self.requested = False
            self.contexts = []

        async def run(self, context):
            self.contexts.append(context)
            if not self.requested:
                self.requested = True
                raise ApprovalRequired(
                    Approval(
                        id="approval-rejected",
                        engagement_id=context.engagement_id,
                        run_id=context.run_id,
                        task_id=context.task.id,
                        risk_class=RiskClass.ACTIVE_SCAN,
                        exact_request={
                            "tool_name": "scan.tcp",
                            "arguments": {"ports": [443]},
                        },
                        target="10.0.0.8",
                        policy_rationale="active scanning requires approval",
                        requested_by="network-specialist",
                    )
                )
            if context.approval_response is not None:
                return SpecialistResult(
                    summary="The active scan was denied; try passive analysis.",
                    outcome=SpecialistOutcome.CONTINUE,
                )
            return SpecialistResult(summary="Passive analysis completed.")

    task = PlannedTask(
        id="scan-rejected",
        role=SpecialistRole.NETWORK_SERVICE,
        title="Investigate service safely",
        instructions="Use a permitted alternative after denial",
    )
    specialist = RejectionAwareSpecialist()
    runtime, store = _runtime(
        tmp_path,
        PlannedSupervisor(
            MissionPlan(
                summary="Approval mission", rationale="Operator gate", tasks=[task]
            )
        ),
        {SpecialistRole.NETWORK_SERVICE: specialist},
    )

    waiting = asyncio.run(
        runtime.start(
            engagement_id="engagement-rejection",
            objective="Investigate without bypassing approval",
            budget=RunBudget(max_retries=0),
        )
    )
    completed = asyncio.run(
        runtime.resume(
            waiting["run_id"],
            {"status": "rejected", "operator": "alice"},
        )
    )

    assert completed["task_status"] == {task.id: TaskStatus.COMPLETE.value}
    assert completed["attempts"] == {task.id: 2}
    assert completed["retry_counts"] == {}
    assert specialist.contexts[1].approval_response == {
        "status": "rejected",
        "operator": "alice",
    }
    assert specialist.contexts[2].approval_response is None
    assert "task.continuing" in [
        event.event_type for event in store.replay_events(waiting["run_id"])
    ]


def test_projected_budget_overrun_fails_run_durably(tmp_path):
    task = PlannedTask(
        id="budgeted",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Local analysis",
        instructions="Do not execute tools",
    )
    supervisor = PlannedSupervisor(
        MissionPlan(summary="Budget test", rationale="Hard cap", tasks=[task])
    )
    specialist = RecordingSpecialist(
        SpecialistRole.SCOPE_PLANNING,
        SpecialistResult(summary="invalid usage", tool_calls=1),
    )
    runtime, store = _runtime(
        tmp_path,
        supervisor,
        {SpecialistRole.SCOPE_PLANNING: specialist},
    )

    state = asyncio.run(
        runtime.start(
            engagement_id="engagement-budget",
            objective="Remain analysis-only",
            budget=RunBudget(max_tool_calls=0, max_retries=0),
        )
    )

    assert state["task_status"] == {task.id: TaskStatus.BLOCKED.value}
    assert state["errors"] == {task.id: "mission tool-call budget exceeded"}
    assert state["tool_calls"] == 1
    assert state["final_summary"].startswith("mission failed:")
    persisted_run = store.get(AgentRun, state["run_id"])
    assert persisted_run.status == RunStatus.FAILED
    assert persisted_run.metadata["partial_observations"][0]["summary"] == (
        "invalid usage"
    )
    assert [event.event_type for event in store.replay_events(state["run_id"])] == [
        "run.started",
        "run.planned",
        "task.started",
        "task.blocked",
        "run.failed",
    ]


def test_start_does_not_resurrect_a_failed_run(tmp_path):
    task = PlannedTask(
        id="never-run",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Must remain terminal",
        instructions="No work should be dispatched",
    )
    supervisor = PlannedSupervisor(
        MissionPlan(summary="Terminal run", rationale="Retry guard", tasks=[task])
    )
    runtime, store = _runtime(tmp_path, supervisor, {})
    failed = store.create(
        AgentRun(
            id="failed-run",
            engagement_id="engagement-1",
            objective="Already failed",
            status=RunStatus.FAILED,
        )
    )

    with pytest.raises(MissionError, match="already terminal"):
        asyncio.run(
            runtime.start(
                engagement_id=failed.engagement_id,
                objective=failed.objective,
                run_id=failed.id,
            )
        )

    assert store.get(AgentRun, failed.id) == failed
    assert store.replay_events(failed.id) == []


def test_plan_node_recovery_reuses_durable_tasks_and_event(tmp_path):
    task = PlannedTask(
        id="recovered-task",
        role=SpecialistRole.SCOPE_PLANNING,
        title="Recover planning",
        instructions="Reuse the durable task",
    )
    supervisor = PlannedSupervisor(
        MissionPlan(summary="Recoverable", rationale="Checkpoint retry", tasks=[task])
    )
    runtime, store = _runtime(tmp_path, supervisor, {})
    run = store.create(
        AgentRun(
            id="recovered-run",
            engagement_id="engagement-1",
            objective="Recover the planning node",
            status=RunStatus.PLANNING,
        )
    )
    state = {
        "engagement_id": run.engagement_id,
        "run_id": run.id,
        "objective": run.objective,
        "context": {},
        "budget": run.budget.model_dump(mode="json"),
    }

    first = asyncio.run(runtime._plan(state))
    retried = asyncio.run(runtime._plan(state))

    assert retried == first
    assert store.count(Task, engagement_id=run.engagement_id) == 1
    assert store.get(AgentRun, run.id).status == RunStatus.RUNNING
    [event] = store.replay_events(run.id)
    assert event.event_type == "run.planned"


def test_evidence_verifier_requires_linked_evidence_and_reproduction_steps():
    verifier = EvidenceVerifier()
    task = PlannedTask(
        role=SpecialistRole.EVIDENCE_VERIFICATION,
        title="Verify candidate",
        instructions="Check the evidence",
    )

    no_evidence = asyncio.run(
        verifier.verify(
            task,
            SpecialistResult(summary="candidate", candidate_finding_ids=["finding-1"]),
        )
    )
    no_steps = asyncio.run(
        verifier.verify(
            task,
            SpecialistResult(
                summary="candidate",
                candidate_finding_ids=["finding-1"],
                evidence_ids=["evidence-1"],
            ),
        )
    )
    complete = asyncio.run(
        verifier.verify(
            task,
            SpecialistResult(
                summary="candidate",
                candidate_finding_ids=["finding-1"],
                evidence_ids=["evidence-1"],
                reproducible_steps=["Replay artifact-1"],
            ),
        )
    )

    assert no_evidence.accepted is False
    assert no_evidence.evidence_ids == []
    assert no_steps.accepted is False
    assert no_steps.evidence_ids == ["evidence-1"]
    assert complete.accepted is True
    assert complete.evidence_ids == ["evidence-1"]
