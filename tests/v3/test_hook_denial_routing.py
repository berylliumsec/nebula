"""A project hook's denial names the routed tool, and repeats stop routing.

Live Core saw up to 16 denials by one search-routing hook in a single turn: the
model reworded a blocked shell search instead of calling the tool the hook
routes to, and every call that ran in between reset the deviation count.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import UTC, datetime

from nebula.v3.domain import (
    ChatTurn,
    ChatTurnStatus,
    NativeHookExecution,
    RiskClass,
)
from nebula.v3.policy import PolicyDecision, PolicyEffect
from nebula.v3.native_hooks import NativeHookBlocked, NativeHookManifest
from nebula.v3.providers import ToolCall, ToolChoice
from nebula.v3.tool_failures import tool_failure
from nebula.v3.tools import ProjectHookPolicyDenied, ToolExecutionResult, ToolSpec
from tests.v3.test_chat_tool_loop import (
    RecordingBroker,
    ScriptedProvider,
    _prepared,
    _response,
)

import pytest


SEARCH_HOOK = "repo-search-routing"


def _denial(hook_id: str = SEARCH_HOOK, route_to=("workspace.search",)):
    return ProjectHookPolicyDenied(
        PolicyDecision(
            effect=PolicyEffect.DENY,
            reason=f"required project hook {hook_id!r} did not complete",
            rule="project_native_hook",
        ),
        hook_id=hook_id,
        stdout="",
        stderr="repo_search_guard (run_command): shell search is routed",
        route_to=route_to,
    )


class HookBroker(RecordingBroker):
    """Denies values naming a hook (``deny:<hook>:<n>``); runs the rest."""

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        value = invocation.arguments["value"]
        if value.startswith("deny:"):
            raise _denial(value.split(":")[1])
        return ToolExecutionResult(output={"value": value})


def _read(call_id: str, value: str) -> ToolCall:
    return ToolCall(id=call_id, name="safe_read", arguments={"value": value})


def _finish(call_id: str = "finish-1") -> ToolCall:
    return ToolCall(id=call_id, name="finish_response", arguments={})


def _turn_requests(provider: ScriptedProvider):
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


def _run_command_spec() -> ToolSpec:
    return ToolSpec(
        name="run_command",
        description="Run a command.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        output_schema={"type": "object"},
        risk_class=RiskClass.LOCAL_READ,
    )


def test_hook_denial_names_the_routed_tool():
    failure = tool_failure(
        _run_command_spec(),
        {"command": "find . -name '*.py'"},
        _denial(route_to=("workspace.find", "workspace.search")),
        phase="before_execution",
    )

    assert "routes it to workspace.find or workspace.search" in failure["problem"]
    assert failure["next_action"].startswith(
        "Call workspace.find or workspace.search instead."
    )
    assert failure["use_instead"] == ["workspace.find", "workspace.search"]
    assert failure["hook_output"]["hook_id"] == SEARCH_HOOK


def test_hook_denial_without_a_route_keeps_todays_text():
    failure = tool_failure(
        _run_command_spec(),
        {"command": "find ."},
        _denial(route_to=()),
        phase="before_execution",
    )

    assert failure["problem"] == (
        "A required project hook blocked the command before it ran."
    )
    assert "untrusted hook output" in failure["next_action"]
    assert "use_instead" not in failure


def test_manifest_route_to_reaches_the_blocked_denial():
    manifest = NativeHookManifest(
        version=1,
        name=SEARCH_HOOK,
        events=["tool.before"],
        command=["run.py"],
        failure_policy="block",
        route_to=["workspace.search", "workspace.find", "workspace.search"],
    )
    assert manifest.route_to == ["workspace.search", "workspace.find"]
    with pytest.raises(ValueError):
        NativeHookManifest(
            version=1,
            name=SEARCH_HOOK,
            events=["tool.before"],
            command=["run.py"],
            route_to=["use grep instead; it is fine"],
        )
    now = datetime.now(UTC)
    blocked = NativeHookBlocked(
        NativeHookExecution(
            engagement_id="project",
            hook_id=SEARCH_HOOK,
            hook_snapshot={"manifest": manifest.model_dump(mode="json")},
            event_name="tool.before",
            status="failed",
            started_at=now,
            completed_at=now,
            exit_code=2,
            error="hook exited unsuccessfully",
        )
    )
    assert blocked.route_to == ("workspace.search", "workspace.find")
    # A snapshot from before the field existed routes nowhere.
    legacy = NativeHookBlocked(
        NativeHookExecution(
            engagement_id="project",
            hook_id=SEARCH_HOOK,
            hook_snapshot={"manifest": {"version": 1}},
            event_name="tool.before",
            status="failed",
            started_at=now,
            completed_at=now,
        )
    )
    assert legacy.route_to == ()


async def test_repeated_denials_by_one_hook_stop_routing_between_runs(tmp_path):
    broker = HookBroker()
    responses = [
        _response(calls=[_read("call-1", f"deny:{SEARCH_HOOK}:1")]),
        _response(calls=[_read("call-2", "a")]),
        _response(calls=[_read("call-3", f"deny:{SEARCH_HOOK}:2")]),
        _response(calls=[_read("call-4", "b")]),
        # The third denial ends routing; the queued read behind it never runs.
        _response(
            calls=[
                _read("call-5", f"deny:{SEARCH_HOOK}:3"),
                _read("call-6", "never"),
            ]
        ),
        _response(text="The search hook keeps routing me elsewhere."),
    ]
    store, service, prepared, provider = await _prepared(
        tmp_path, responses, broker, max_tool_calls=20
    )

    completion = await service.complete(prepared)

    assert completion.message.content == "The search hook keeps routing me elsewhere."
    assert [call.arguments["value"] for call in broker.calls] == [
        f"deny:{SEARCH_HOOK}:1",
        "a",
        f"deny:{SEARCH_HOOK}:2",
        "b",
        f"deny:{SEARCH_HOOK}:3",
    ]
    turn = await store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == [
        "denied",
        "complete",
        "denied",
        "complete",
        "denied",
    ]
    requests = _turn_requests(provider)
    assert len(requests) == 6
    assert requests[-1].tool_choice == ToolChoice.NONE
    denied = requests[1].tool_results[-1]
    assert denied.is_error is True
    assert "Call workspace.search instead" in str(denied.output)


async def test_one_denial_then_the_routed_tool_continues(tmp_path):
    broker = HookBroker()
    responses = [
        _response(calls=[_read("call-1", f"deny:{SEARCH_HOOK}:1")]),
        _response(calls=[_read("call-2", "routed")]),
        # Other hooks count on their own, so two denials each stay below
        # the limit and routing continues to the model's own finish.
        _response(calls=[_read("call-3", "deny:other-hook:1")]),
        _response(calls=[_read("call-4", f"deny:{SEARCH_HOOK}:2")]),
        _response(calls=[_read("call-5", "deny:other-hook:2")]),
        _response(calls=[_finish()]),
        _response(text="Found it with the routed search."),
    ]
    store, service, prepared, provider = await _prepared(
        tmp_path, responses, broker, max_tool_calls=20
    )

    completion = await service.complete(prepared)

    assert completion.message.content == "Found it with the routed search."
    assert len(broker.calls) == 5
    turn = await store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == [
        "denied",
        "complete",
        "denied",
        "denied",
        "denied",
    ]
    assert len(_turn_requests(provider)) == 7


class _DenyingGatewayBroker:
    """The command broker as a harness gateway sees it, blocked by a hook."""

    def __init__(self, denial):
        self.denial = denial
        self.calls = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        raise self.denial


async def test_harness_gateway_command_keeps_the_hooks_reason(tmp_path):
    """A hook block in a harness chat reaches the model with the hook's words.

    The gateway used to flatten the denial to a ``denied`` status, so the
    model saw only "denied by an access or approval rule" and no next step.
    """

    from types import SimpleNamespace

    from nebula.v3.domain import HarnessSession
    from nebula.v3.tool_failures import FAILURE_SCHEMA
    from tests.v3.test_harnesses import _runtime

    store, engagement, profile, _mcp, _, runtime = await _runtime(tmp_path)
    _chat, _chat_turn, turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Search the repository",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    broker = _DenyingGatewayBroker(_denial(route_to=()))
    runtime._gateway_oci_components[session.id] = SimpleNamespace(
        specs={"run_command": _run_command_spec()},
        broker=broker,
        scope=None,
        workspace=tmp_path,
    )
    try:
        result = await runtime._gateway_oci_call(
            session,
            turn,
            "runtime_fixture_run_command",
            "run_command",
            {"command": "find . -name '*.md'"},
        )
    finally:
        await runtime.shutdown()

    assert broker.calls
    failure = result["structuredContent"]
    assert result["isError"] is True
    # The gateway wrapper passes a classified failure through unchanged.
    assert failure["schema"] == FAILURE_SCHEMA
    assert failure["category"] == "permission_denied"
    assert failure["problem"] == (
        "A required project hook blocked the command before it ran."
    )
    assert failure["policy_rule"] == "project_native_hook"
    assert failure["hook_output"]["hook_id"] == SEARCH_HOOK
    assert "shell search is routed" in failure["hook_output"]["stderr"]
    assert json.loads(result["content"][0]["text"]) == failure


async def test_harness_gateway_command_names_the_hooks_routed_tool(tmp_path):
    from types import SimpleNamespace

    from nebula.v3.domain import HarnessSession
    from tests.v3.test_harnesses import _runtime

    store, engagement, profile, _mcp, _, runtime = await _runtime(tmp_path)
    _chat, _chat_turn, turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Search the repository",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    runtime._gateway_oci_components[session.id] = SimpleNamespace(
        specs={"run_command": _run_command_spec()},
        broker=_DenyingGatewayBroker(_denial(route_to=("workspace.search",))),
        scope=None,
        workspace=tmp_path,
    )
    try:
        result = await runtime._gateway_oci_call(
            session,
            turn,
            "runtime_fixture_run_command",
            "run_command",
            {"command": "grep -R needle ."},
        )
    finally:
        await runtime.shutdown()

    failure = result["structuredContent"]
    assert failure["use_instead"] == ["workspace.search"]
    assert failure["next_action"].startswith("Call workspace.search instead.")
