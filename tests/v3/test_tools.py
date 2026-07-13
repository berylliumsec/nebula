import asyncio

import pytest

from nebula.v3.database import Database
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    Artifact,
    Evidence,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.sandbox import (
    AnalysisOnlyRunner,
    SandboxResult,
    SandboxRunner,
    SandboxWorkspaceAccess,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.storage import RunBudgetExceededError
from nebula.v3.tools import (
    AnalysisTool,
    ApprovalRequired,
    IdempotencyBehavior,
    InvalidToolArguments,
    SandboxCommandTool,
    StoreToolLedger,
    StoreToolEvidenceRecorder,
    ToolBroker,
    ToolExecutionResult,
    ToolInvocation,
    ToolBrokerError,
    ToolPlugin,
    ToolRegistry,
    ToolSpec,
)


OBJECT_SCHEMA = {"type": "object", "additionalProperties": False}


class StubActiveTool(ToolPlugin):
    def __init__(self):
        self.calls = 0
        self.spec = ToolSpec(
            name="scan.tcp",
            description="Perform a bounded TCP scan",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "ports": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["target", "ports"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"open": {"type": "array", "items": {"type": "integer"}}},
                "required": ["open"],
                "additionalProperties": False,
            },
            risk_class=RiskClass.ACTIVE_SCAN,
            requires_approval=True,
            network_access=True,
            target_argument="target",
            port_argument="ports",
            idempotency=IdempotencyBehavior.KEY_REQUIRED,
        )

    async def execute(self, invocation, runner):
        del invocation, runner
        self.calls += 1
        return ToolExecutionResult(output={"open": [443]})


class CapturingRunner(SandboxRunner):
    def __init__(self):
        self.request = None

    async def available(self):
        return True, "test runner"

    async def run(self, request):
        self.request = request
        timestamp = utc_now()
        return SandboxResult(
            command=request.command,
            image=request.image,
            runtime="test",
            started_at=timestamp,
            completed_at=timestamp,
            duration_seconds=0,
            exit_code=0,
            stdout="{}",
            stderr="",
        )


def _broker(tmp_path, plugin):
    store = NebulaStore(Database(tmp_path / "nebula.db"))
    registry = ToolRegistry()
    registry.register(plugin)
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda engagement_id: tmp_path,
        evidence_recorder=StoreToolEvidenceRecorder(
            store, ArtifactStore(tmp_path / "artifacts")
        ),
    )
    return broker, store


@pytest.mark.parametrize(
    "filesystem_access,expected",
    [
        ("none", SandboxWorkspaceAccess.NONE),
        ("read", SandboxWorkspaceAccess.READ),
        ("workspace_write", SandboxWorkspaceAccess.WRITE),
    ],
)
def test_command_tool_maps_declared_filesystem_scope_to_sandbox(
    tmp_path, filesystem_access, expected
):
    plugin = SandboxCommandTool(
        ToolSpec(
            name="parse.command",
            description="Run a typed parser",
            input_schema=OBJECT_SCHEMA,
            output_schema=OBJECT_SCHEMA,
            risk_class=RiskClass.LOCAL_READ,
            filesystem_access=filesystem_access,
        ),
        image="example.invalid/parser@sha256:" + "b" * 64,
        command_builder=lambda arguments: ["parser", "--json"],
        output_parser=lambda stdout, stderr, exit_code: {},
    )
    runner = CapturingRunner()
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-mounts",
        tool_name="parse.command",
        workspace=tmp_path,
    )

    asyncio.run(plugin.execute(invocation, runner))
    assert runner.request.workspace_access == expected


def test_analysis_tool_result_is_persisted_and_idempotently_replayed(tmp_path):
    executions = []

    async def handler(arguments):
        executions.append(arguments)
        return {"count": len(arguments["items"])}

    plugin = AnalysisTool(
        ToolSpec(
            name="parse.scan",
            description="Parse normalized scan data",
            input_schema={
                "type": "object",
                "properties": {"items": {"type": "array"}},
                "required": ["items"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
            risk_class=RiskClass.LOCAL_READ,
        ),
        handler,
    )
    broker, store = _broker(tmp_path, plugin)
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-1",
        tool_name="parse.scan",
        arguments={"items": [1, 2, 3]},
        workspace=tmp_path,
        idempotency_key="parse-input-1",
    )
    scope = ScopePolicy(engagement_id="eng-1")

    first = asyncio.run(broker.execute(invocation, scope))
    replay = asyncio.run(broker.execute(invocation, scope))

    assert first.output == replay.output == {"count": 3}
    assert executions == [{"items": [1, 2, 3]}]
    calls = store.list_entities(ToolCall, engagement_id="eng-1")
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.COMPLETE
    assert [event.event_type for event in store.replay_events("run-1")] == [
        "tool.proposed",
        "tool.running",
        "tool.complete",
    ]


def test_parser_failure_still_records_raw_immutable_execution_evidence(tmp_path):
    plugin = SandboxCommandTool(
        ToolSpec(
            name="parse.failing",
            description="Exercise parser failure evidence",
            input_schema=OBJECT_SCHEMA,
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            risk_class=RiskClass.LOCAL_READ,
        ),
        image="example.invalid/parser@sha256:" + "c" * 64,
        command_builder=lambda arguments: ["/usr/bin/parser", "--json"],
        output_parser=lambda stdout, stderr, exit_code: (_ for _ in ()).throw(
            ValueError("malformed parser fixture")
        ),
    )
    store = NebulaStore(Database(tmp_path / "failed-evidence.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry()
    registry.register(plugin)
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=CapturingRunner(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda engagement_id: tmp_path,
        evidence_recorder=StoreToolEvidenceRecorder(store, artifacts),
    )
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-parser-failure",
        tool_name=plugin.spec.name,
        workspace=tmp_path,
    )

    with pytest.raises(ToolBrokerError, match="output parsing failed"):
        asyncio.run(broker.execute(invocation, ScopePolicy(engagement_id="eng-1")))

    [call] = store.list_entities(ToolCall, engagement_id="eng-1")
    assert call.status == ToolCallStatus.FAILED
    [evidence] = store.list_entities(Evidence, engagement_id="eng-1")
    artifact = store.get(Artifact, evidence.artifact_id)
    envelope = artifacts.path_for(artifact).read_text(encoding="utf-8")
    assert '"stdout":"{}"' in envelope
    assert "malformed parser fixture" in envelope


def test_active_tool_creates_durable_approval_and_resumes_after_decision(tmp_path):
    plugin = StubActiveTool()
    broker, store = _broker(tmp_path, plugin)
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-2",
        task_id="task-1",
        tool_name="scan.tcp",
        arguments={"target": "10.50.1.8", "ports": [443]},
        workspace=tmp_path,
        target="10.50.1.8",
        port=443,
        idempotency_key="scan-1",
        requested_by="agent-network",
    )
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_cidrs=["10.50.0.0/16"],
        allowed_ports=[443],
    )

    with pytest.raises(ApprovalRequired) as waiting:
        asyncio.run(broker.execute(invocation, scope))
    pending = waiting.value.approval
    assert pending.status == ApprovalStatus.PENDING
    assert pending.exact_request == {
        "tool_name": "scan.tcp",
        "arguments": {"target": "10.50.1.8", "ports": [443]},
    }
    assert pending.target == "10.50.1.8"
    persisted_call = store.get(ToolCall, pending.tool_call_id)
    assert persisted_call.status == ToolCallStatus.WAITING_APPROVAL
    assert persisted_call.approval_id == pending.id
    assert plugin.calls == 0

    approved = store.update(
        Approval,
        pending.id,
        {
            "status": ApprovalStatus.APPROVED,
            "decided_by": "operator-1",
            "decided_at": utc_now(),
        },
        expected_revision=pending.revision,
    )
    result = asyncio.run(broker.execute(invocation, scope, approval=approved))

    assert result.output == {"open": [443]}
    assert len(result.evidence_ids) == 1
    assert plugin.calls == 1
    assert store.get(ToolCall, pending.tool_call_id).status == ToolCallStatus.COMPLETE
    event_types = [event.event_type for event in store.replay_events("run-2")]
    assert event_types == [
        "tool.proposed",
        "approval.requested",
        "tool.waiting_approval",
        "tool.running",
        "tool.complete",
    ]


def test_in_scope_active_tool_runs_when_its_contract_does_not_require_approval(
    tmp_path,
):
    plugin = StubActiveTool()
    plugin.spec = plugin.spec.model_copy(update={"requires_approval": False})
    broker, store = _broker(tmp_path, plugin)
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-active-default",
        tool_name="scan.tcp",
        arguments={"target": "10.50.1.8", "ports": [443]},
        workspace=tmp_path,
        target="10.50.1.8",
        port=443,
        idempotency_key="scan-default",
    )
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_cidrs=["10.50.0.0/16"],
        allowed_ports=[443],
    )

    result = asyncio.run(broker.execute(invocation, scope))

    assert result.output == {"open": [443]}
    assert plugin.calls == 1
    assert store.count(Approval) == 0
    assert [
        event.event_type for event in store.replay_events("run-active-default")
    ] == ["tool.proposed", "tool.running", "tool.complete"]


def test_expiring_approval_persists_transition_and_event_atomically(tmp_path):
    store = NebulaStore(Database(tmp_path / "expired-approval.db"))
    run = store.create(
        AgentRun(id="expiry-run", engagement_id="eng-1", objective="expire gate")
    )
    approval = store.create(
        Approval(
            engagement_id=run.engagement_id,
            run_id=run.id,
            risk_class=RiskClass.ACTIVE_SCAN,
            exact_request={"tool_name": "scan.tcp", "arguments": {}},
            policy_rationale="operator approval required",
            requested_by="agent-network",
        )
    )

    expired = asyncio.run(StoreToolLedger(store).expire_approval(approval))

    assert expired.status == ApprovalStatus.EXPIRED
    assert store.get(Approval, approval.id) == expired
    [event] = store.replay_events(run.id)
    assert event.event_type == "approval.expired"
    assert event.payload == {
        "approval_id": approval.id,
        "status": ApprovalStatus.EXPIRED.value,
    }


def test_broker_rejects_an_unpersisted_forged_approval_decision(tmp_path):
    plugin = StubActiveTool()
    broker, store = _broker(tmp_path, plugin)
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-forged",
        tool_name="scan.tcp",
        arguments={"target": "10.50.1.8", "ports": [443]},
        workspace=tmp_path,
        target="10.50.1.8",
        port=443,
        idempotency_key="scan-forged",
    )
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_cidrs=["10.50.0.0/16"],
        allowed_ports=[443],
    )
    with pytest.raises(ApprovalRequired) as waiting:
        asyncio.run(broker.execute(invocation, scope))
    pending = waiting.value.approval
    forged = pending.model_copy(
        update={"status": ApprovalStatus.APPROVED, "decided_by": "untrusted-agent"}
    )

    with pytest.raises(ApprovalRequired):
        asyncio.run(broker.execute(invocation, scope, approval=forged))
    assert store.get(Approval, pending.id).status == ApprovalStatus.PENDING
    assert (
        store.get(ToolCall, pending.tool_call_id).status
        == ToolCallStatus.WAITING_APPROVAL
    )
    assert plugin.calls == 0


def test_schema_validation_and_required_idempotency_key_stop_before_execution(tmp_path):
    plugin = StubActiveTool()
    broker, store = _broker(tmp_path, plugin)
    scope = ScopePolicy(engagement_id="eng-1")
    base = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-3",
        tool_name="scan.tcp",
        arguments={"target": "10.50.1.8", "ports": [443]},
        workspace=tmp_path,
    )
    with pytest.raises(InvalidToolArguments, match="idempotency key"):
        asyncio.run(broker.execute(base, scope))
    with pytest.raises(InvalidToolArguments, match="invalid tool input"):
        asyncio.run(
            broker.execute(
                base.model_copy(
                    update={
                        "arguments": {"target": "10.50.1.8", "ports": ["443"]},
                        "idempotency_key": "bad",
                    }
                ),
                scope,
            )
        )
    assert plugin.calls == 0
    assert store.count(ToolCall) == 0


def test_reusing_idempotency_key_for_different_arguments_is_rejected(tmp_path):
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return {}

    plugin = AnalysisTool(
        ToolSpec(
            name="parse.empty",
            description="test",
            input_schema={"type": "object"},
            output_schema=OBJECT_SCHEMA,
            risk_class=RiskClass.LOCAL_READ,
        ),
        handler,
    )
    broker, _ = _broker(tmp_path, plugin)
    scope = ScopePolicy(engagement_id="eng-1")
    first = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-4",
        tool_name="parse.empty",
        arguments={"one": 1},
        workspace=tmp_path,
        idempotency_key="same-key",
    )
    asyncio.run(broker.execute(first, scope))
    with pytest.raises(ToolBrokerError, match="idempotency key was reused"):
        asyncio.run(
            broker.execute(first.model_copy(update={"arguments": {"two": 2}}), scope)
        )
    assert calls == [{"one": 1}]


def test_broker_rejects_caller_target_and_workspace_mismatches(tmp_path):
    plugin = StubActiveTool()
    broker, _ = _broker(tmp_path, plugin)
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_cidrs=["10.50.0.0/16"],
        allowed_ports=[443],
    )
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-mismatch",
        tool_name="scan.tcp",
        arguments={"target": "203.0.113.8", "ports": [443]},
        target="10.50.1.8",
        port=443,
        workspace=tmp_path,
        idempotency_key="mismatch",
    )
    with pytest.raises(InvalidToolArguments, match="caller target"):
        asyncio.run(broker.execute(invocation, scope))

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(InvalidToolArguments, match="engagement-owned workspace"):
        asyncio.run(
            broker.execute(
                invocation.model_copy(
                    update={
                        "arguments": {"target": "10.50.1.8", "ports": [443]},
                        "target": "10.50.1.8",
                        "workspace": other,
                    }
                ),
                scope,
            )
        )


def test_tool_spec_rejects_network_access_with_local_read_risk():
    with pytest.raises(ValueError, match="network-capable risk"):
        ToolSpec(
            name="invalid.network",
            description="invalid",
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
                "additionalProperties": False,
            },
            output_schema=OBJECT_SCHEMA,
            risk_class=RiskClass.LOCAL_READ,
            network_access=True,
            target_argument="target",
        )


def test_store_ledger_reserves_hard_tool_budget_before_execution(tmp_path):
    executions = []

    async def handler(arguments):
        executions.append(arguments)
        return {}

    plugin = AnalysisTool(
        ToolSpec(
            name="parse.budgeted",
            description="budgeted",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=OBJECT_SCHEMA,
            risk_class=RiskClass.LOCAL_READ,
        ),
        handler,
    )
    store = NebulaStore(Database(tmp_path / "budget.db"))
    store.create(
        AgentRun(
            id="budget-run",
            engagement_id="eng-1",
            objective="test",
            budget={"max_tool_calls": 1},
        )
    )
    registry = ToolRegistry()
    registry.register(plugin)
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda engagement_id: tmp_path,
    )
    scope = ScopePolicy(engagement_id="eng-1")
    first = ToolInvocation(
        engagement_id="eng-1",
        run_id="budget-run",
        tool_name="parse.budgeted",
        workspace=tmp_path,
        idempotency_key="one",
    )
    asyncio.run(broker.execute(first, scope))
    with pytest.raises(RunBudgetExceededError, match="exhausted"):
        asyncio.run(
            broker.execute(first.model_copy(update={"idempotency_key": "two"}), scope)
        )
    assert executions == [{}]
