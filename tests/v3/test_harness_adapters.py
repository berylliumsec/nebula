from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft7Validator
from pydantic import SecretStr

from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    HarnessAuthMode,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    CodexAppServerAdapter,
    HarnessPermissionDecision,
    PermissionTicket,
    _CodexRpc,
)


SCHEMAS = Path(__file__).parent / "fixtures" / "codex_app_server" / "0.144.0"


def _validate(relative: str, value: dict[str, Any]) -> None:
    schema = json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(value)


class FixtureCodexRpc:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "codex-cli/0.144.0"}
        if method == "thread/start":
            return {"thread": {"id": "thread-fixture"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            await self.events.put(
                {
                    "id": 41,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "itemId": "command-1",
                        "command": "pwd",
                        "cwd": "/workspace",
                        "startedAtMs": 1,
                    },
                }
            )
            await self.events.put(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "itemId": "message-1",
                        "delta": "done",
                    },
                }
            )
            await self.events.put(
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-fixture",
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "server": "workspace",
                            "tool": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    },
                }
            )
            await self.events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-fixture",
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "server": "workspace",
                            "tool": "read_file",
                            "result": "ok",
                        },
                    },
                }
            )
            await self.events.put(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "tokenUsage": {
                            "last": {"inputTokens": 3, "outputTokens": 2}
                        },
                    },
                }
            )
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-fixture",
                        "turn": {"id": "turn-fixture", "status": "completed"},
                    },
                }
            )
            return {"turn": {"id": "turn-fixture"}}
        return {}

    async def notify(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        self.notifications.append((method, params))

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def close(self) -> None:
        self.closed = True


class FixtureCodexAdapter(CodexAppServerAdapter):
    def __init__(self, rpc: FixtureCodexRpc) -> None:
        self.rpc = rpc

    async def _connect(self, *args: Any, **kwargs: Any) -> Any:
        return self.rpc


def _mcp_profile() -> McpServerProfile:
    return McpServerProfile(
        id="mcp-a",
        name="workspace_server",
        transport=McpTransport.STREAMABLE_HTTP,
        url="https://mcp.invalid/api",
        enabled=True,
        capabilities=McpCapabilitySnapshot(
            tools=[McpToolSnapshot(name="read_file")]
        ),
    )


def test_codex_schema_pinned_handshake_streaming_and_approvals(tmp_path):
    async def scenario() -> None:
        rpc = FixtureCodexRpc()
        adapter = FixtureCodexAdapter(rpc)
        decisions: list[Any] = []

        async def permission(request):
            decisions.append(request)
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket("approval-1", "call-1", future)

        profile = HarnessProfile(
            id="codex-a",
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-test",
        )
        session = HarnessSession(
            id="session-a",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="gpt-test",
        )
        connection = await adapter.open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(_mcp_profile(),),
                credential_store=CredentialStore(),
                permission_handler=permission,
            )
        )
        events = [
            event
            async for event in connection.run_turn("inspect", model="gpt-test")
        ]

        assert [method for method, _ in rpc.calls[:3]] == [
            "initialize",
            "thread/start",
            "turn/start",
        ]
        assert rpc.notifications == [("initialized", None)]
        _validate("v1/InitializeParams.json", rpc.calls[0][1])
        _validate("v2/ThreadStartParams.json", rpc.calls[1][1])
        _validate("v2/TurnStartParams.json", rpc.calls[2][1])
        assert [event.type for event in events] == [
            "started",
            "approval_required",
            "message_delta",
            "tool_started",
            "tool_completed",
            "usage",
            "completed",
        ]
        assert events[-1].message == "done"
        assert decisions[0].category == "command"
        assert rpc.responses == [(41, {"decision": "accept"})]
        _validate("CommandExecutionRequestApprovalResponse.json", rpc.responses[0][1])

    asyncio.run(scenario())


def test_codex_rpc_rejects_malformed_and_uncorrelated_messages():
    async def scenario() -> None:
        rpc = _CodexRpc()
        try:
            await rpc._dispatch(b"not-json")
        except Exception as exc:
            assert "malformed JSON" in str(exc)
        else:
            raise AssertionError("malformed app-server JSON was accepted")

        try:
            await rpc._dispatch(json.dumps({"id": "not-an-integer", "result": {}}))
        except Exception as exc:
            assert "uncorrelatable" in str(exc)
        else:
            raise AssertionError("uncorrelated app-server response was accepted")

    asyncio.run(scenario())


class StreamEvent:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    def __init__(self, identifier: str, name: str, input_data: dict[str, Any]) -> None:
        self.id = identifier
        self.name = name
        self.input = input_data


class ToolResultBlock:
    def __init__(self, identifier: str, content: str) -> None:
        self.tool_use_id = identifier
        self.content = content
        self.is_error = False


class AssistantMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class UserMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ResultMessage:
    session_id = "claude-session-learned"
    usage = {"input_tokens": 7, "output_tokens": 4}
    is_error = False
    result = "ok"


class FakeClaudeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeClaudeClient:
    latest: "FakeClaudeClient"

    def __init__(self, *, options: FakeClaudeOptions) -> None:
        self.options = options
        self.connected = False
        self.queries: list[str] = []
        self.interrupted = False
        self.disconnected = False
        FakeClaudeClient.latest = self

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        yield StreamEvent(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}
        )
        yield AssistantMessage(
            [
                TextBlock("ok"),
                ToolUseBlock(
                    "tool-1",
                    "mcp__workspace_server__read_file",
                    {"path": "README.md"},
                ),
            ]
        )
        yield UserMessage([ToolResultBlock("tool-1", "contents")])
        yield ResultMessage()

    async def get_mcp_status(self) -> dict[str, Any]:
        return {
            "mcpServers": [
                {"name": "workspace_server", "status": "connected"}
            ]
        }

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


class PermissionResultAllow:
    behavior = "allow"

    def __init__(self, updated_input: dict[str, Any]) -> None:
        self.updated_input = updated_input


def test_claude_sdk_strict_mcp_resume_permissions_and_partial_messages(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            PermissionResultAllow=PermissionResultAllow,
        )
        monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
        observed_permissions: list[Any] = []

        async def permission(request):
            observed_permissions.append(request)
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket(None, "call-1", future)

        credentials = CredentialStore()
        credential = credentials.create(
            CredentialCreateRequest(
                secret=SecretStr("anthropic-fixture-secret"), persistence="session"
            )
        )
        profile = HarnessProfile(
            id="claude-a",
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            auth_mode=HarnessAuthMode.SECRET_REF,
            secret_ref=credential.reference,
            default_model="claude-test",
        )
        session = HarnessSession(
            id="session-a",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            external_session_id="claude-session-existing",
            model="claude-test",
        )
        connection = await ClaudeAgentSdkAdapter().open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(_mcp_profile().model_copy(update={"required": True}),),
                credential_store=credentials,
                permission_handler=permission,
            )
        )
        options = FakeClaudeClient.latest.options.kwargs
        assert options["strict_mcp_config"] is True
        assert options["setting_sources"] == []
        assert options["resume"] == "claude-session-existing"
        assert options["include_partial_messages"] is True
        assert set(options["mcp_servers"]) == {"workspace_server"}
        assert options["disallowed_tools"] == ["WebFetch", "WebSearch"]
        assert options["env"] == {"ANTHROPIC_API_KEY": "anthropic-fixture-secret"}

        permission_result = await options["can_use_tool"](
            "mcp__workspace_server__read_file", {"path": "README.md"}, None
        )
        assert permission_result.behavior == "allow"
        assert observed_permissions[0].server_name == "workspace_server"
        assert observed_permissions[0].tool_name == "read_file"

        events = [
            event
            async for event in connection.run_turn("continue", model="claude-test")
        ]
        assert [event.type for event in events] == [
            "started",
            "message_delta",
            "tool_started",
            "tool_completed",
            "usage",
            "completed",
        ]
        tool_started = next(event for event in events if event.type == "tool_started")
        assert tool_started.server_id == "workspace_server"
        assert tool_started.tool_name == "read_file"
        assert events[-1].message == "ok"
        assert events[-1].external_session_id == "claude-session-learned"

        connection.active = True
        await connection.steer("focus")
        await connection.interrupt()
        assert FakeClaudeClient.latest.queries == ["continue", "focus"]
        assert FakeClaudeClient.latest.interrupted is True
        await connection.close()
        assert FakeClaudeClient.latest.disconnected is True

    asyncio.run(scenario())
