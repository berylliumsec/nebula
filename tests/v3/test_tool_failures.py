import asyncio
import json
import stat
from dataclasses import replace

import pytest

from nebula.v3.diagnostics import configure_diagnostics, shutdown_diagnostics
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.automation_tools import AutomationBroker
from nebula.v3.domain import (
    Artifact,
    ChatTurn,
    Engagement,
    RiskClass,
    ScopePolicy,
    ToolCall as StoredCall,
    ToolCallStatus,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.providers import ToolCall
from nebula.v3.sandbox import AnalysisOnlyRunner
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import ToolOutputAccessError, ToolOutputService
from nebula.v3.tool_results import ToolResultReceipt, ToolResultStatus
from nebula.v3.tool_failures import FAILURE_SCHEMA, tool_failure
from nebula.v3.tools import (
    AnalysisTool,
    InvalidToolArguments,
    StoreToolLedger,
    ToolBroker,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
)
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_specialist_routing_tolerance import (
    ScriptedProvider as MissionProvider,
    _call as mission_call,
    _context as mission_context,
    _specialist,
)


def _spec():
    return ToolSpec(
        name="tool_output.read",
        description="Read an authorized artifact.",
        input_schema={
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "ID from the artifact receipt; sha256 is separate.",
                },
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )


def test_failure_contract_keeps_exact_schema_in_private_diagnostic(tmp_path):
    configure_diagnostics(tmp_path / "diagnostics", watch_settings=False)
    try:
        spec = _spec()
        digest = "a" * 64
        failure = tool_failure(
            spec,
            {"artifact_id": digest},
            InvalidToolArguments("artifact_id is a SHA-256 digest"),
            phase="before_execution",
            call_id="call-1",
        )
        assert failure["schema"] == FAILURE_SCHEMA
        assert failure["invalid_input"] == "artifact_id"
        assert "receipt" in failure["next_action"]
        assert "sha256" in failure["next_action"]
        assert failure["side_effects"] == "none"
        assert failure["retry_safe"] is True
        assert failure["diagnostic_available"] is True
        assert failure["effective_input_schema"] == spec.input_schema
        assert failure["schema_truncated"] is False
        assert digest not in json.dumps(failure)
        path = (
            tmp_path
            / "diagnostics"
            / "tool-failure-diagnostics"
            / f"{failure['diagnostic_reference']}.json"
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        detail = json.loads(path.read_text())
        assert detail["effective_input_schema"] == spec.input_schema
        assert "SHA-256 digest" in detail["original_failure"]
    finally:
        shutdown_diagnostics()


@pytest.mark.parametrize(
    "arguments,invalid_input",
    [({}, "artifact_id"), ({"artifact_id": "receipt-id", "extra": True}, "extra")],
)
def test_validation_reports_the_missing_or_unexpected_field(arguments, invalid_input):
    spec = _spec()
    with pytest.raises(InvalidToolArguments) as caught:
        ToolBroker._validate(spec.input_schema, arguments, "input")
    failure = tool_failure(spec, arguments, caught.value, phase="before_execution")
    assert failure["category"] == "invalid_arguments"
    assert failure["invalid_input"] == invalid_input
    assert failure["effective_input_schema"] == spec.input_schema


def test_broker_rejects_hash_before_execution_and_finishes_call(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        StoredCall(
            id="prior",
            engagement_id="project",
            run_id="run-1",
            tool_name="safe_read",
            risk_class=RiskClass.LOCAL_READ,
            status=ToolCallStatus.COMPLETE,
        )
    )
    store.create(
        Artifact(
            id="receipt-artifact-id",
            engagement_id="project",
            sha256="b" * 64,
            size=0,
            storage_path=str(tmp_path / "artifact"),
            metadata={"tool_call_id": "prior"},
        )
    )
    assert (
        ToolOutputService(store, None)
        ._authorized_artifact(
            engagement_id="project", owner_id="run-1", artifact_id="receipt-artifact-id"
        )
        .sha256
        == "b" * 64
    )
    effects = []

    async def handler(arguments):
        effects.append(arguments)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(AnalysisTool(_spec(), handler))
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda _: tmp_path,
    )
    invocation = ToolInvocation(
        id="call-1",
        engagement_id="project",
        run_id="run-1",
        tool_name="tool_output.read",
        arguments={"artifact_id": "b" * 64},
        workspace=tmp_path,
    )
    with pytest.raises(InvalidToolArguments):
        asyncio.run(broker.execute(invocation, ScopePolicy(engagement_id="project")))
    assert effects == []
    assert store.get(StoredCall, "call-1").status == ToolCallStatus.FAILED


def test_missing_and_unauthorized_artifacts_have_same_public_failure(tmp_path):
    store = NebulaStore(tmp_path / "access.db")
    store.create(Engagement(id="project-a", name="A"))
    store.create(Engagement(id="project-b", name="B"))
    store.create(
        StoredCall(
            id="other-call",
            engagement_id="project-b",
            run_id="other-run",
            tool_name="safe_read",
            risk_class=RiskClass.LOCAL_READ,
            status=ToolCallStatus.COMPLETE,
        )
    )
    store.create(
        Artifact(
            id="other-artifact",
            engagement_id="project-b",
            sha256="a" * 64,
            size=0,
            storage_path=str(tmp_path / "private"),
            metadata={"tool_call_id": "other-call"},
        )
    )
    output = ToolOutputService(store, None)
    public = []
    for artifact_id in ("missing-artifact", "other-artifact"):
        with pytest.raises(ToolOutputAccessError) as caught:
            output._authorized_artifact(
                engagement_id="project-a",
                owner_id="run-a",
                artifact_id=artifact_id,
            )
        failure = tool_failure(
            _spec(),
            {"artifact_id": artifact_id},
            caught.value,
            phase="after_execution",
        )
        failure.pop("diagnostic_reference")
        public.append(failure)
    assert public[0] == public[1]


def test_read_only_missing_and_permission_errors_have_same_public_failure():
    spec = _spec()
    public = []
    for error in (
        FileNotFoundError("missing artifact"),
        PermissionError("private artifact"),
    ):
        failure = tool_failure(
            spec, {"artifact_id": "receipt-id"}, error, phase="after_execution"
        )
        failure.pop("diagnostic_reference")
        public.append(failure)
    assert public[0] == public[1]
    assert public[0]["category"] == "unavailable_resource"


def test_next_model_request_reads_guidance_and_corrects_arguments(tmp_path):
    responses = [
        _response(
            calls=[ToolCall(id="bad", name="safe_read", arguments={"value": 42})]
        ),
        _response(
            calls=[
                ToolCall(
                    id="fixed", name="safe_read", arguments={"value": "receipt-id"}
                )
            ]
        ),
        _response(calls=[ToolCall(id="finish", name="finish_response", arguments={})]),
        _response(text="The corrected read succeeded."),
    ]
    store, service, prepared, provider = _prepared(
        tmp_path, responses, RecordingBroker()
    )
    registry = ToolRegistry()

    async def read(arguments):
        return {"value": arguments["value"]}

    registry.register(AnalysisTool(prepared.tool_components.specs["safe_read"], read))
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=AnalysisOnlyRunner(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda _: tmp_path,
    )
    prepared.tool_components = replace(prepared.tool_components, broker=broker)
    completed = asyncio.run(service.complete(prepared))
    assert completed.message.content == "The corrected read succeeded."
    guidance = provider.requests[1].tool_results[0]
    assert guidance.is_error is True
    assert guidance.output["schema"] == FAILURE_SCHEMA
    assert guidance.output["category"] == "invalid_arguments"
    assert guidance.output["invalid_input"] == "value"
    assert guidance.output["side_effects"] == "none"
    assert guidance.output["schema_reference"].startswith("safe_read@")
    assert (
        guidance.output["effective_input_schema"]
        == prepared.tool_components.specs["safe_read"].input_schema
    )
    assert provider.requests[2].tool_results[-1].output["value"] == "receipt-id"
    assert [
        store.get(StoredCall, call_id).status
        for call_id in store.get(ChatTurn, "turn").tool_call_ids
    ] == [ToolCallStatus.FAILED, ToolCallStatus.COMPLETE]
    assert [entry["status"] for entry in store.get(ChatTurn, "turn").tool_history] == [
        "failed",
        "complete",
    ]


@pytest.mark.parametrize(
    "error,phase,category,side_effects,retry_safe",
    [
        (
            ToolOutputAccessError("artifact is unavailable"),
            "before_execution",
            "unavailable_resource",
            "none",
            False,
        ),
        (
            ToolOutputAccessError("artifact is unavailable"),
            "after_execution",
            "unavailable_resource",
            "none",
            False,
        ),
        (
            PermissionError("private target exists"),
            "before_execution",
            "permission_denied",
            "none",
            False,
        ),
        (
            ConnectionError("secret endpoint"),
            "after_execution",
            "unavailable_dependency",
            "unknown",
            False,
        ),
        (
            TimeoutError("secret timeout"),
            "after_execution",
            "timeout",
            "unknown",
            False,
        ),
        (
            asyncio.CancelledError("secret cancel"),
            "after_execution",
            "cancelled",
            "unknown",
            False,
        ),
        (
            RuntimeError("secret mutation response"),
            "after_execution",
            "execution_failed",
            "unknown",
            False,
        ),
    ],
)
def test_failure_categories_never_expose_exception_text(
    error,
    phase,
    category,
    side_effects,
    retry_safe,
):
    spec = _spec()
    if category == "permission_denied" or (
        phase == "after_execution" and category != "unavailable_resource"
    ):
        spec = spec.model_copy(
            update={"name": "mutating.write", "risk_class": RiskClass.WORKSPACE_WRITE}
        )
    failure = tool_failure(spec, {"artifact_id": "receipt-id"}, error, phase=phase)
    assert failure["category"] == category
    assert failure["side_effects"] == side_effects
    assert failure["retry_safe"] is retry_safe
    assert "secret" not in json.dumps(failure)
    assert "private target" not in json.dumps(failure)
    assert "receipt-id" not in json.dumps(failure)


def test_mission_next_request_receives_effective_schema(tmp_path):
    class InvalidBroker:
        async def execute(self, invocation, scope):
            if invocation.arguments["ports"] == "wrong":
                raise InvalidToolArguments("ports has the wrong type")
            return ToolExecutionResult(output={"ok": True})

    provider = MissionProvider(
        [
            {"calls": [mission_call("bad", "nmap.tcp", {"ports": "wrong"})]},
            {"calls": [mission_call("fixed", "nmap.tcp", {"ports": [443]})]},
            {
                "calls": [
                    mission_call(
                        "finish",
                        "nebula.finish_task",
                        {
                            "status": "complete",
                            "summary": "Reviewed corrected result",
                            "rationale": "The corrected call completed",
                        },
                    )
                ]
            },
        ]
    )
    specialist = _specialist(tmp_path, provider, InvalidBroker())
    first = asyncio.run(specialist.run(mission_context()))
    second = asyncio.run(specialist.run(mission_context(prior_turns=[first])))
    asyncio.run(specialist.run(mission_context(prior_turns=[first, second])))
    delivered = provider.requests[1].tool_results[0]
    assert delivered.output["schema"] == FAILURE_SCHEMA
    assert (
        delivered.output["effective_input_schema"]
        == specialist.specs["nmap.tcp"].input_schema
    )
    assert provider.requests[2].tool_results[-1].output["ok"] is True


def test_mission_failed_receipt_reaches_next_request_with_uncertain_effects(tmp_path):
    class ReceiptBroker:
        async def execute(self, invocation, scope):
            receipt = ToolResultReceipt(
                tool_call_id=invocation.id,
                tool_name=invocation.tool_name,
                tool_version="1",
                status=ToolResultStatus.TIMED_OUT,
            )
            return ToolExecutionResult(
                output=receipt.as_model_result(),
                receipt=receipt,
                execution={"timed_out": True},
            )

    provider = MissionProvider(
        [
            {"calls": [mission_call("timed", "nmap.tcp", {"ports": [443]})]},
            {
                "calls": [
                    mission_call(
                        "finish",
                        "nebula.finish_task",
                        {
                            "status": "blocked",
                            "summary": "Outcome uncertain",
                            "rationale": "Inspect recorded state before another action",
                        },
                    )
                ]
            },
        ]
    )
    specialist = _specialist(tmp_path, provider, ReceiptBroker())
    first = asyncio.run(specialist.run(mission_context()))
    asyncio.run(specialist.run(mission_context(prior_turns=[first])))
    failure = provider.requests[1].tool_results[0].output
    assert failure["schema"] == FAILURE_SCHEMA
    assert failure["category"] == "timeout"
    assert failure["side_effects"] == "unknown"
    assert failure["retry_safe"] is False
    assert (
        failure["effective_input_schema"] == specialist.specs["nmap.tcp"].input_schema
    )


@pytest.mark.parametrize(
    "artifact_id,expected_status",
    [
        ("d" * 64, "invalid_arguments"),
        ("missing-receipt-id", "unavailable_resource"),
    ],
)
def test_fixed_runtime_retrieval_failures_are_terminal(
    tmp_path, artifact_id, expected_status
):
    store = NebulaStore(tmp_path / "automation.db")
    store.create(Engagement(id="project", name="Project"))
    broker = AutomationBroker.__new__(AutomationBroker)
    broker.specs = {"tool_output.read": _spec()}
    broker.ledger = StoreToolLedger(store, enforce_run_budget=False)

    class MissingOutput:
        def read(self, **kwargs):
            raise ToolOutputAccessError("artifact is unavailable")

    broker.output_service = MissingOutput()
    invocation = ToolInvocation(
        id="call-1",
        engagement_id="project",
        run_id="run-1",
        tool_name="tool_output.read",
        arguments={"artifact_id": artifact_id},
        workspace=tmp_path,
    )
    with pytest.raises((InvalidToolArguments, ToolOutputAccessError)) as caught:
        asyncio.run(broker.execute(invocation, ScopePolicy(engagement_id="project")))
    failure = tool_failure(
        _spec(),
        invocation.arguments,
        caught.value,
        phase="before_execution"
        if expected_status == "invalid_arguments"
        else "after_execution",
    )
    assert failure["category"] == expected_status
    assert store.get(StoredCall, "call-1").status == ToolCallStatus.FAILED


def test_gateway_preserves_mcp_schema_and_name_on_denial():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    class Gateway:
        def _gateway_catalog(self, session, params=None):
            return {
                "tools": [
                    {
                        "name": "mcp_abc_readFile",
                        "description": "Read a file",
                        "inputSchema": schema,
                    }
                ]
            }

        async def _gateway_call_unwrapped(self, session, name, arguments):
            return {
                "structuredContent": {"status": "denied", "detail": "private path"},
                "isError": True,
            }

    response = asyncio.run(
        HarnessRuntimeService._gateway_call(
            Gateway(),
            None,
            "mcp_abc_readFile",
            {"path": "file.txt"},
        )
    )
    failure = response["structuredContent"]
    assert response["isError"] is True
    assert failure["schema"] == FAILURE_SCHEMA
    assert failure["tool"] == "mcp_abc_readFile"
    assert failure["effective_input_schema"] == schema
    assert failure["category"] == "permission_denied"
    assert failure["side_effects"] == "none"
    assert "private path" not in json.dumps(failure)
