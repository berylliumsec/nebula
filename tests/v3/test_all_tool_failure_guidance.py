"""Every Core-owned tool family uses the same actionable failure contract."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from nebula.v3.agent_security_tools import SecurityToolsRuntime
from nebula.v3.automation_tools import command_specs
from nebula.v3.domain import HarnessSession, RiskClass, ToolCall, ToolCallStatus
from nebula.v3.harnesses import (
    HarnessRuntimeService,
    _GATEWAY_AGENT_MESSAGE_SCHEMAS,
    _GATEWAY_KNOWLEDGE_SCHEMAS,
    _GATEWAY_RETRIEVAL_SCHEMAS,
    _GATEWAY_WORK_SCHEMAS,
)
from nebula.v3.mcp import McpProbeError, build_mcp_tool_plugins
from nebula.v3.notebook_mcp import NotebookMcpTools
from nebula.v3.tool_failures import (
    FAILURE_SCHEMA,
    tool_failure,
    with_failed_result_receipt,
)
from nebula.v3.tool_results import (
    ToolOutputPreview,
    ToolOutputQueryError,
    ToolResultReceipt,
    ToolResultStatus,
)
from nebula.v3.storage import ConflictError
from nebula.v3.tools import GuidedToolError, ToolSpec
from tests.v3.test_agent_security_tools import (
    StubSessions,
    _project as security_project,
)
from tests.v3.test_notebooks import _project as notebook_project
from tests.v3.test_harnesses import _runtime as harness_runtime


def _catalog_specs():
    specs = list(command_specs().values())
    for family, tools in (
        ("notebook", NotebookMcpTools.capabilities().tools),
        ("security", SecurityToolsRuntime.capabilities().tools),
    ):
        specs.extend(
            ToolSpec(
                name=f"{family}.{tool.name}",
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema={"type": "object"},
                risk_class=RiskClass.LOCAL_READ,
            )
            for tool in tools
        )
    for family, definitions in (
        (
            "retrieval",
            (
                (name, "Read captured output or workspace content.", schema)
                for name, schema in _GATEWAY_RETRIEVAL_SCHEMAS.items()
            ),
        ),
        (
            "knowledge",
            (
                (name, "Read available knowledge sources.", schema)
                for name, schema in _GATEWAY_KNOWLEDGE_SCHEMAS.items()
            ),
        ),
        ("work", ((name, *entry) for name, entry in _GATEWAY_WORK_SCHEMAS.items())),
        (
            "message",
            ((name, *entry) for name, entry in _GATEWAY_AGENT_MESSAGE_SCHEMAS.items()),
        ),
    ):
        specs.extend(
            ToolSpec(
                name=f"{family}.{name}",
                description=description,
                input_schema=schema,
                output_schema={"type": "object"},
                risk_class=RiskClass.LOCAL_READ,
            )
            for name, description, schema in definitions
        )
    return specs


@pytest.mark.parametrize("spec", _catalog_specs(), ids=lambda spec: spec.name)
def test_each_core_tool_declaration_gives_usable_error(spec):
    # Produce one schema rejection from the actual catalogued declaration.
    arguments = {} if spec.input_schema.get("required") else {"unexpected_input": True}
    errors = list(Draft202012Validator(spec.input_schema).iter_errors(arguments))
    if not errors:
        pytest.skip("open input object has no schema-defined misuse")
    failure = tool_failure(spec, arguments, errors[0], phase="before_execution")
    assert failure["schema"] == FAILURE_SCHEMA
    assert failure["category"] == "invalid_arguments"
    assert failure["side_effects"] == "none"
    assert failure["usage"]["description"]
    assert failure["usage"]["source"] == "offered_tool_declaration"
    assert failure["effective_input_schema"]
    assert failure["next_action"]
    assert len(json.dumps(failure, ensure_ascii=False).encode()) <= 8192


def test_failure_with_large_offered_schema_keeps_usage_within_result_limit():
    names = [f"field_{index:02d}_" + "n" * 70 for index in range(32)]
    schema = {
        "type": "object",
        "properties": {
            name: {"type": "string", "description": "value" * 60} for name in names
        },
        "required": names,
    }
    spec = ToolSpec(
        name="large_catalog_tool",
        description="Usage " * 2_000,
        input_schema=schema,
        output_schema={"type": "object"},
        risk_class=RiskClass.LOCAL_READ,
    )
    failure = tool_failure(
        spec,
        {},
        next(Draft202012Validator(schema).iter_errors({})),
        phase="before_execution",
    )
    assert failure["usage"]["description"]
    assert failure["next_action"]
    assert failure["schema_truncated"] is True
    assert len(json.dumps(failure, ensure_ascii=False).encode()) <= 8192


def test_notebook_path_refusal_explains_actual_path_rule_before_execution(tmp_path):
    store, project, notebooks = notebook_project(tmp_path)
    runtime = NotebookMcpTools(store, notebooks)
    runtime.register_profile(project.id)
    with pytest.raises(GuidedToolError) as caught:
        asyncio.run(runtime.call_tool(project.id, "read", {"path": "../secret.ipynb"}))
    snapshot = next(
        tool for tool in runtime.capabilities().tools if tool.name == "read"
    )
    spec = ToolSpec(
        name="notebook.read",
        description=snapshot.description,
        input_schema=snapshot.input_schema,
        output_schema={"type": "object"},
        risk_class=RiskClass.LOCAL_READ,
    )
    failure = tool_failure(
        spec, {"path": "../secret.ipynb"}, caught.value, phase="after_execution"
    )
    assert failure["category"] == "invalid_arguments"
    assert failure["invalid_input"] == "path"
    assert failure["side_effects"] == "none"
    assert ".ipynb" in failure["problem"]
    assert "Notebook list" in failure["next_action"]
    assert "secret.ipynb" not in json.dumps(failure)


def test_security_read_refusal_names_session_prerequisite():
    runtime = SecurityToolsRuntime.__new__(SecurityToolsRuntime)
    runtime._active = lambda _: None
    with pytest.raises(GuidedToolError) as caught:
        asyncio.run(runtime.call_tool("project", "traffic", {}))
    snapshot = next(
        tool for tool in runtime.capabilities().tools if tool.name == "traffic"
    )
    failure = tool_failure(
        ToolSpec(
            name="security.traffic",
            description=snapshot.description,
            input_schema=snapshot.input_schema,
            output_schema={"type": "object"},
            risk_class=RiskClass.LOCAL_READ,
        ),
        {},
        caught.value,
        phase="after_execution",
    )
    assert failure["category"] == "unavailable_resource"
    assert failure["side_effects"] == "none"
    assert failure["retry_safe"] is False
    assert "status" in failure["next_action"]
    assert "navigate" in failure["next_action"]


def test_security_proxy_refusal_gives_specific_usage_with_uncertain_effects(tmp_path):
    store, project, session = security_project(tmp_path)
    runtime = SecurityToolsRuntime(store, StubSessions(session))
    with pytest.raises(GuidedToolError) as caught:
        asyncio.run(runtime.call_tool(project.id, "upstream_proxy", {"enabled": True}))
    snapshot = next(
        tool for tool in runtime.capabilities().tools if tool.name == "upstream_proxy"
    )
    failure = tool_failure(
        ToolSpec(
            name="security.upstream_proxy",
            description=snapshot.description,
            input_schema=snapshot.input_schema,
            output_schema={"type": "object"},
            risk_class=RiskClass.LOCAL_READ,
        ),
        {"enabled": True},
        caught.value,
        phase="after_execution",
    )
    assert failure["error_code"] == "security_tools.proxy_url_required"
    assert failure["invalid_input"] == "url"
    assert "requires a URL" in failure["problem"]
    assert "HTTP(S) or SOCKS5" in failure["next_action"]
    assert failure["side_effects"] == "unknown"


def test_bounded_output_query_and_work_conflict_have_distinct_actions():
    read_spec = command_specs()["tool_output.search"]
    query = tool_failure(
        read_spec,
        {"query": ""},
        ToolOutputQueryError("query cannot be empty"),
        phase="after_execution",
    )
    assert query["category"] == "invalid_arguments"
    assert query["invalid_input"] == "query"
    assert query["side_effects"] == "none"
    work = tool_failure(
        ToolSpec(
            name="work_check_in",
            description="Check in to an existing Work item using its latest state.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_class=RiskClass.LOCAL_READ,
        ),
        {},
        ConflictError("private concurrent item detail"),
        phase="after_execution",
    )
    assert work["category"] == "conflict"
    assert work["side_effects"] == "unknown"
    assert work["retry_safe"] is False
    assert "current item" in work["next_action"]
    assert "private concurrent item detail" not in json.dumps(work)


def test_mcp_plugin_keeps_core_cause_without_exposing_raw_exception(tmp_path):
    store, project, notebooks = notebook_project(tmp_path)
    profile = NotebookMcpTools(store, notebooks).register_profile(project.id)
    error = GuidedToolError(
        "secret path /private/file",
        code="notebook.path_invalid",
        category="invalid_arguments",
        problem="A project-relative .ipynb path is required; no action ran.",
        next_action="Use Notebook list and correct path.",
        field="path",
        before_execution=True,
    )

    class FailingService:
        async def call_tool(self, *args, **kwargs):
            raise error

    plugin = next(
        plugin
        for plugin in build_mcp_tool_plugins(FailingService(), (profile,))
        if plugin.snapshot.name == "read"
    )
    result = asyncio.run(
        plugin.execute(
            SimpleNamespace(engagement_id=project.id, arguments={"path": "bad"}),
            None,
        )
    )
    assert result.failure_error is error
    assert "secret" not in result.stderr
    assert "project-relative" in result.stderr
    assert "Notebook tools operate" in plugin.spec.description
    assert "failure_error" not in result.model_dump()


def test_mcp_grant_error_has_actionable_guidance_and_no_effects():
    spec = ToolSpec(
        name="mcp.tool",
        description="Read the selected MCP resource.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_class=RiskClass.LOCAL_READ,
    )
    failure = tool_failure(
        spec,
        {},
        McpProbeError("private server name", usage_reason="grant_revoked"),
        phase="after_execution",
    )
    assert failure["category"] == "permission_denied"
    assert failure["side_effects"] == "none"
    assert "Refresh" in failure["next_action"]
    assert "private server name" not in json.dumps(failure)


def test_failed_receipt_keeps_searchable_evidence_and_redacted_preview():
    failure = tool_failure(
        ToolSpec(
            name="run_command",
            description="Run a command in the authorized workspace.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_class=RiskClass.LOCAL_READ,
        ),
        {},
        RuntimeError("private internal exception"),
        phase="after_execution",
    )
    receipt = ToolResultReceipt(
        tool_call_id="call-123",
        tool_name="run_command",
        tool_version="1",
        status=ToolResultStatus.FAILED,
        summary="Command failed",
        output_preview=[
            ToolOutputPreview(
                artifact_id="stderr-123",
                kind="stderr",
                text="wrong option; Bearer abcdefghijklmnopqrstuvwxyz",
            )
        ],
    )
    failure = with_failed_result_receipt(
        failure, receipt.as_model_result(), artifact_id="receipt-123"
    )
    assert failure["result_receipt"]["tool_call_id"] == "call-123"
    assert failure["result_receipt"]["artifact_id"] == "receipt-123"
    assert "wrong option" in failure["result_receipt"]["output_preview"][0]["text"]
    assert "Bearer [REDACTED]" in json.dumps(failure)
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(failure)
    assert "tool_output.search" in failure["next_action"]
    assert failure["side_effects"] == "unknown"


def test_gateway_preserves_guided_error_and_actual_offered_usage():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    class Gateway:
        def _active_gateway_turn(self, session_id):
            return None

        def _gateway_catalog(self, session, params=None, *, turn=None):
            return {
                "tools": [
                    {
                        "name": "mcp_notebook_read",
                        "description": "Read a project-relative .ipynb notebook.",
                        "inputSchema": schema,
                    }
                ]
            }

        async def _gateway_call_unwrapped(self, session, name, arguments):
            raise GuidedToolError(
                "private rejected path",
                code="notebook.path_invalid",
                category="invalid_arguments",
                problem="A project-relative .ipynb path is required; no action ran.",
                next_action="Use Notebook list and correct path.",
                field="path",
                before_execution=True,
            )

    response = asyncio.run(
        HarnessRuntimeService._gateway_call(
            Gateway(),
            SimpleNamespace(id="session"),
            "mcp_notebook_read",
            {"path": "secret"},
        )
    )
    failure = response["structuredContent"]
    assert response["isError"] is True
    assert failure["usage"]["description"] == "Read a project-relative .ipynb notebook."
    assert failure["effective_input_schema"] == schema
    assert failure["side_effects"] == "none"
    assert "private rejected path" not in json.dumps(failure)


def test_real_gateway_mcp_dispatch_preserves_core_error_and_receipt(tmp_path):
    async def scenario():
        store, project, profile, mcp, _, runtime = harness_runtime(tmp_path)
        _, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Read a selected tool",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[mcp.id],
        )
        session = store.get(HarnessSession, turn.harness_session_id)
        selected = next(
            item["name"]
            for item in runtime._gateway_catalog(session)["tools"]
            if item["name"].endswith("read_file")
        )

        async def reject(*args, **kwargs):
            raise GuidedToolError(
                "private error detail",
                code="test.path_rule",
                category="invalid_arguments",
                problem="A project-relative path is required; no action ran.",
                next_action="Choose a project-relative path and call again.",
                field="path",
                before_execution=True,
            )

        runtime.mcp_service.call_tool = reject
        runtime._active[session.id] = SimpleNamespace(
            turn_id=turn.id, connection=None, task=None
        )
        response = await runtime._gateway_call(session, selected, {"path": "bad"})
        failure = response["structuredContent"]
        assert response["isError"] is True
        assert failure["schema"] == FAILURE_SCHEMA
        assert failure["error_code"] == "test.path_rule"
        assert failure["side_effects"] == "none"
        assert failure["result_receipt"]["tool_call_id"]
        assert failure["result_receipt"]["artifact_id"]
        assert "private error detail" not in json.dumps(failure)
        call = store.get(ToolCall, failure["result_receipt"]["tool_call_id"])
        assert call.status == ToolCallStatus.FAILED
        runtime._active.pop(session.id)
        await runtime.close_session(session.id)

    asyncio.run(scenario())
