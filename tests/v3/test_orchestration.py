import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from nebula.v3.domain import (
    AgentRun,
    Approval,
    RiskClass,
    RunBudget,
    RunStatus,
    Task,
    TaskStatus,
)
from nebula.v3.orchestration import (
    EvidenceVerifier,
    MissionPlan,
    MissionRuntime,
    PlannedTask,
    SpecialistResult,
    SpecialistRole,
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


def _runtime(tmp_path, supervisor, specialists):
    store = NebulaStore(tmp_path / "nebula.db")
    runtime = MissionRuntime(
        store=store,
        checkpointer=InMemorySaver(),
        supervisor=supervisor,
        specialists=specialists,
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

    assert state["task_status"] == {task.id: TaskStatus.FAILED.value}
    assert state["errors"] == {task.id: "mission tool-call budget exceeded"}
    assert state["final_summary"].startswith("mission failed:")
    assert store.get(AgentRun, state["run_id"]).status == RunStatus.FAILED
    assert [event.event_type for event in store.replay_events(state["run_id"])] == [
        "run.started",
        "run.planned",
        "task.started",
        "task.failed",
        "run.failed",
    ]


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
