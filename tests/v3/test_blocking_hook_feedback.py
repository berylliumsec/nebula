"""A command hook can deny an effect and still give the model useful feedback."""

import asyncio
import json

import pytest

from nebula.v3.automation_tools import AutomationBroker, command_specs
from nebula.v3.chat import CompletionHookBlocked, StartHookBlocked
from nebula.v3.domain import (
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    ScopePolicy,
    ToolCall as StoredToolCall,
    ToolCallStatus,
)
from nebula.v3.policy import PolicyDecision, PolicyEffect
from nebula.v3.native_hooks import discover_native_hooks, snapshot_native_hook
from nebula.v3.providers import ModelRequest, ModelResponse, ToolCall, ToolChoice
from nebula.v3.tool_failures import failure_receipt_text, tool_failure
from nebula.v3.tool_results import ToolOutputService, serialize_model_result
from nebula.v3.tools import ProjectHookPolicyDenied
from tests.v3.test_automation_runtime import create_tool_chat, runtime, tool_invocation
from tests.v3.test_chat import FakeProvider, _write_native_hook
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_native_hook_turn_ends import _service


def test_blocking_start_hook_sends_output_to_model_but_keeps_turn_failed(tmp_path):
    class GuidanceProvider(FakeProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("start_hook_feedback") == "1":
                self.requests.append(request)
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model-a",
                    text="Ask the workspace owner to resolve the existing changes.",
                    finish_reason="stop",
                )
            return await super().complete(request)

    store, service, provider, request, _ = _service(
        tmp_path,
        GuidanceProvider,
        hook_id="start-check",
        events=["chat.turn.started"],
        script=(
            "#!/bin/sh\n"
            "printf 'Workspace has another owner\\n'\n"
            "printf 'Bearer private-token\\n' >&2\n"
            "exit 3\n"
        ),
        failure_policy="block",
    )
    prepared = service.prepare(request)

    with pytest.raises(StartHookBlocked, match="start-check"):
        asyncio.run(service.complete(prepared))

    [feedback_request] = [
        item
        for item in provider.requests
        if item.metadata.get("start_hook_feedback") == "1"
    ]
    assert feedback_request.tools == []
    assert feedback_request.tool_choice == ToolChoice.NONE
    assert (
        "stdout: Workspace has another owner" in feedback_request.messages[-1].content
    )
    assert "stderr: Bearer [REDACTED]" in feedback_request.messages[-1].content
    assert "private-token" not in json.dumps(feedback_request.model_dump())
    turn = store.get(ChatTurn, prepared.turn.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.request_snapshot["start_hook_resolution"]["candidate"] == (
        "Ask the workspace owner to resolve the existing changes."
    )
    note = next(
        item
        for item in service.session_messages(turn.session_id)
        if item.metadata.get("kind") == "turn_outcome"
    )
    assert "the turn remained blocked" in note.content
    assert "Ask the workspace owner" in note.content


def test_effectful_completion_hook_informs_model_without_replaying_hook(tmp_path):
    class GuidanceProvider(FakeProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("completion_hook_retry") == "1":
                self.requests.append(request)
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model-a",
                    text="The operator must review the existing workspace changes.",
                    finish_reason="stop",
                )
            return await super().complete(request)

    store, service, provider, request, _ = _service(
        tmp_path,
        GuidanceProvider,
        hook_id="workspace-check",
        events=["chat.turn.completed"],
        script=(
            "#!/bin/sh\n"
            "printf 'Review another owner\\n'\n"
            "printf 'Bearer private-token\\n' >&2\n"
            "exit 3\n"
        ),
        failure_policy="block",
        side_effects="workspace",
    )
    prepared = service.prepare(request)

    with pytest.raises(CompletionHookBlocked, match="workspace-check"):
        asyncio.run(service.complete(prepared))

    [feedback_request] = [
        item
        for item in provider.requests
        if item.metadata.get("completion_hook_retry") == "1"
    ]
    assert "stdout: Review another owner" in feedback_request.messages[-1].content
    assert "stderr: Bearer [REDACTED]" in feedback_request.messages[-1].content
    assert "cannot be replayed" in feedback_request.messages[-1].content
    assert len(service.list_turn_hook_executions(prepared.turn.id)) == 1
    turn = store.get(ChatTurn, prepared.turn.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert (
        "operator must review"
        in turn.request_snapshot["completion_hook_resolution"]["candidate"]
    )


def test_effectful_tool_completion_hook_routes_feedback_without_replay(tmp_path):
    _write_native_hook(
        tmp_path,
        "workspace-check",
        events=["chat.turn.completed"],
        script="#!/bin/sh\nprintf 'Review another owner\\n'\nexit 3\n",
        failure_policy="block",
        side_effects="workspace",
    )
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="Unverified answer."),
            _response(
                calls=[ToolCall(id="finish-2", name="finish_response", arguments={})]
            ),
            _response(text="The operator must review the other workspace."),
        ],
        RecordingBroker(),
    )
    prepared.hook_snapshots = [
        snapshot_native_hook("workspace-check", discover_native_hooks(tmp_path))
    ]

    with pytest.raises(CompletionHookBlocked, match="workspace-check"):
        asyncio.run(service.complete(prepared))

    assert any(
        "stdout: Review another owner" in str(message.content)
        for message in provider.requests[2].messages
    )
    assert "cannot be replayed" in str(provider.requests[2].messages[-1].content)
    assert len(service.list_turn_hook_executions("turn")) == 1
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.FAILED
    assert (
        "operator must review"
        in turn.request_snapshot["completion_hook_resolution"]["candidate"]
    )


def test_blocked_command_hook_output_enters_bounded_model_receipt(tmp_path):
    async def scenario():
        manager, store, artifacts, engagement, sessions = runtime(tmp_path)
        create_tool_chat(store, engagement)
        workspace = tmp_path / "workspaces" / engagement.id
        directory = workspace / ".agents" / "hooks" / "workspace-check"
        directory.mkdir(parents=True)
        script = directory / "run.sh"
        script.write_text(
            "#!/bin/sh\n"
            "printf 'Repository has another owner\\n'\n"
            "printf 'Bearer private-token\\n' >&2\n"
            "exit 9\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        (directory / "hook.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "name": "Workspace check",
                    "events": ["tool.before"],
                    "command": ["run.sh"],
                    "side_effects": "none",
                    "failure_policy": "block",
                }
            ),
            encoding="utf-8",
        )
        broker = AutomationBroker(
            manager=manager,
            store=store,
            output_service=ToolOutputService(store, artifacts),
        )
        invocation = tool_invocation(
            engagement, workspace, "run_command", {"command": "echo must-not-run"}, 1
        )

        with pytest.raises(ProjectHookPolicyDenied) as caught:
            await broker.execute(
                invocation, store.get(ScopePolicy, f"scope:{engagement.id}")
            )
        receipt = tool_failure(
            command_specs()["run_command"],
            invocation.arguments,
            caught.value,
            phase="before_execution",
        )
        return store, sessions, receipt

    store, sessions, receipt = asyncio.run(scenario())

    assert sessions == []
    assert store.list_entities(CommandExecution) == []
    assert store.list_entities(StoredToolCall)[0].status == ToolCallStatus.DENIED
    assert receipt["category"] == "permission_denied"
    assert receipt["side_effects"] == "none"
    assert receipt["retry_safe"] is False
    assert receipt["hook_output"]["stdout"] == "Repository has another owner"
    assert receipt["hook_output"]["stderr"] == "Bearer [REDACTED]"
    delivered = json.loads(serialize_model_result(receipt))
    assert delivered["hook_output"] == receipt["hook_output"]
    assert "private-token" not in json.dumps(delivered)


def test_blocked_hook_feedback_reaches_next_provider_request(tmp_path):
    class HookBlockingBroker(RecordingBroker):
        async def execute(self, invocation, scope, *, approval=None):
            del scope, approval
            self.calls.append(invocation)
            raise ProjectHookPolicyDenied(
                PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason="required project hook 'workspace-check' did not complete",
                    rule="project_native_hook",
                ),
                hook_id="workspace-check",
                stdout="Repository has another owner's changes.\n",
                stderr="",
            )

    broker = HookBlockingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            ),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="The operator must resolve the other owner's changes."),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    assert "operator" in completion.message.content
    assert len(broker.calls) == 1
    assert store.get(ChatTurn, "turn").tool_history[0]["status"] == "denied"
    [feedback] = provider.requests[1].tool_results
    assert feedback.is_error is True
    assert (
        feedback.output["hook_output"]["stdout"]
        == "Repository has another owner's changes.\n"
    )
    assert "untrusted" in feedback.output["hook_output"]["trust"]


def test_blocked_hook_output_is_bounded_and_searchable():
    denial = ProjectHookPolicyDenied(
        PolicyDecision(
            effect=PolicyEffect.DENY,
            reason="required project hook blocked the command",
            rule="project_native_hook",
        ),
        hook_id="audit",
        stdout="Bearer secret-value\n" + "é" * 2_000,
        stderr="Fix repository ownership before retrying.",
    )
    receipt = tool_failure(
        command_specs()["run_command"],
        {"command": "echo harmless"},
        denial,
        phase="before_execution",
    )

    assert receipt["hook_output"]["stdout"].startswith("Bearer [REDACTED]\n")
    assert len(receipt["hook_output"]["stdout"].encode("utf-8")) <= 1_024
    assert receipt["hook_output"]["truncated"] is True
    assert "secret-value" not in serialize_model_result(receipt)
    assert "Fix repository ownership" in failure_receipt_text(receipt)
