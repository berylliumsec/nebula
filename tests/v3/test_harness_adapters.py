from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft7Validator
from pydantic import SecretStr
import pytest

from nebula.v3 import harnesses as harness_module
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    HarnessAuthMode,
    HarnessCapabilities,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessWorkspaceAccess,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    utc_now,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    ClaudeAgentSdkConnection,
    CodexAppServerAdapter,
    CodexAppServerConnection,
    GrokAcpAdapter,
    GrokAcpConnection,
    HarnessConfigurationError,
    HarnessProviderError,
    HarnessSkillInvocation,
    HarnessPermissionDecision,
    HarnessTransportError,
    HarnessUnavailableError,
    PermissionTicket,
    _AcpRpc,
    _CodexRpc,
    _harness_goal_snapshot,
    _codex_process_overrides,
    _codex_thread_config,
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
        if method == "account/read":
            return {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "model": "gpt-5.4",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Faster"},
                            {"reasoningEffort": "medium", "description": "Balanced"},
                        ],
                        "defaultServiceTier": "default",
                        "serviceTiers": [
                            {
                                "id": "default",
                                "name": "Standard",
                                "description": "Standard speed",
                            },
                            {
                                "id": "fast",
                                "name": "Fast",
                                "description": "Priority speed",
                            },
                        ],
                    },
                    {"id": "gpt-5.3-codex", "model": "gpt-5.3-codex"},
                    {"id": "internal", "model": "internal", "hidden": True},
                ],
                "nextCursor": None,
            }
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
                        "tokenUsage": {"last": {"inputTokens": 3, "outputTokens": 2}},
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

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
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


class FixtureGrokRpc:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "grok", "version": "1.0.5"},
                "authMethods": [{"id": "cached_token", "name": "Cached login"}],
                "agentCapabilities": {"loadSession": True},
            }
        if method == "authenticate":
            return {}
        if method == "session/new":
            return {"sessionId": "grok-session"}
        if method == "session/prompt":
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "current_mode_update",
                            "currentModeId": "plan",
                        }
                    },
                }
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "plan",
                            "entries": [
                                {
                                    "id": "one",
                                    "content": "Inspect",
                                    "status": "in_progress",
                                }
                            ],
                        }
                    },
                }
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "done"},
                        }
                    },
                }
            )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications.append((method, params))

    async def respond(self, _request_id: Any, _result: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FixtureGrokAdapter(GrokAcpAdapter):
    def __init__(self, rpc: FixtureGrokRpc) -> None:
        self.rpc = rpc

    async def _connect(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.rpc


def _mcp_profile() -> McpServerProfile:
    return McpServerProfile(
        id="mcp-a",
        name="workspace_server",
        transport=McpTransport.STREAMABLE_HTTP,
        url="https://mcp.invalid/api",
        enabled=True,
        capabilities=McpCapabilitySnapshot(tools=[McpToolSnapshot(name="read_file")]),
    )


def test_codex_probe_discovers_selectable_models():
    async def scenario() -> None:
        rpc = FixtureCodexRpc()
        adapter = FixtureCodexAdapter(rpc)
        profile = HarnessProfile(
            id="codex-a",
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )

        health = await adapter.probe(profile, CredentialStore())

        assert health.healthy is True
        assert health.authentication_state == "verified"
        assert health.session_state == "unverified"
        assert health.turn_state == "unverified"
        assert health.capabilities.models == ["gpt-5.4", "gpt-5.3-codex"]
        assert health.capabilities.goal_monitoring is True
        options = health.capabilities.model_options[0]
        assert options.default_reasoning_effort == "medium"
        assert [item.id for item in options.reasoning_efforts] == ["low", "medium"]
        assert options.default_service_tier == "default"
        assert [item.id for item in options.service_tiers] == ["default", "fast"]
        assert [method for method, _ in rpc.calls] == [
            "initialize",
            "account/read",
            "model/list",
        ]
        assert rpc.calls[0][1]["capabilities"] == {"requestAttestation": False}
        assert rpc.closed is True

    asyncio.run(scenario())


def test_grok_probe_negotiates_cached_token_without_credentials():
    async def scenario() -> None:
        rpc = FixtureGrokRpc()
        adapter = FixtureGrokAdapter(rpc)
        profile = HarnessProfile(
            id="grok-a",
            name="Grok",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
        )

        health = await adapter.probe(profile, CredentialStore())

        assert health.healthy is True
        assert health.authentication_state == "verified"
        assert health.turn_state == "unverified"
        assert health.harness_version == "1.0.5"
        assert health.capabilities.plans is True
        assert health.capabilities.goal_monitoring is True
        assert health.capabilities.skill_invocation is True
        assert [method for method, _ in rpc.calls] == ["initialize", "authenticate"]
        assert rpc.calls[1][1] == {
            "methodId": "cached_token",
            "_meta": {"headless": True},
        }
        assert rpc.closed is True

    asyncio.run(scenario())


def test_grok_process_removes_host_command_and_workspace_tools(monkeypatch, tmp_path):
    observed: dict[str, Any] = {}

    class Process:
        stdin = SimpleNamespace()
        stdout = SimpleNamespace()
        stderr = SimpleNamespace()

    async def create_process(*argv: str, **kwargs: Any) -> Process:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(_AcpRpc, "start", lambda self: asyncio.sleep(0))
    executable = tmp_path / "grok"
    executable.write_text("", encoding="utf-8")
    profile = HarnessProfile(
        id="grok-a",
        name="Grok",
        kind=HarnessKind.GROK_ACP,
        executable=str(executable),
    )
    session = HarnessSession(
        id="session-a",
        engagement_id="eng-a",
        harness_profile_id=profile.id,
        model="grok-test",
    )

    rpc = asyncio.run(GrokAcpAdapter()._connect(profile, tmp_path, session))

    argv = observed["argv"]
    disallowed = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert "run_terminal_command" in disallowed
    assert "read_file" in disallowed
    assert "write" in disallowed
    assert argv[argv.index("--disallowed-tools") + 2] == "agent"
    assert observed["kwargs"]["cwd"] == str(tmp_path)
    assert isinstance(rpc, _AcpRpc)


def test_grok_host_session_preserves_native_tools_and_user_bus(monkeypatch, tmp_path):
    observed: dict[str, Any] = {}

    class Process:
        stdin = SimpleNamespace()
        stdout = SimpleNamespace()
        stderr = SimpleNamespace()

    async def create_process(*argv: str, **kwargs: Any) -> Process:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Process()

    runtime = tmp_path / "run-user"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime / 'bus'}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(_AcpRpc, "start", lambda self: asyncio.sleep(0))
    executable = tmp_path / "grok"
    executable.write_text("", encoding="utf-8")
    profile = HarnessProfile(
        id="grok-host",
        name="Grok host",
        kind=HarnessKind.GROK_ACP,
        executable=str(executable),
    )
    native = HarnessNativeCapabilities(
        workspace_access=HarnessWorkspaceAccess.WRITE,
        shell=True,
    )
    session = HarnessSession(
        id="session-host",
        engagement_id="eng-a",
        harness_profile_id=profile.id,
        model="grok-test",
        metadata={
            "execution_mode": "host",
            "native_capabilities": native.model_dump(mode="json"),
        },
    )

    asyncio.run(GrokAcpAdapter()._connect(profile, tmp_path, session))

    assert "--disallowed-tools" not in observed["argv"]
    assert observed["kwargs"]["env"]["XDG_RUNTIME_DIR"] == str(runtime)
    assert observed["kwargs"]["env"]["DBUS_SESSION_BUS_ADDRESS"] == (
        f"unix:path={runtime / 'bus'}"
    )


def test_grok_connection_normalizes_mode_plan_and_streamed_message():
    async def scenario() -> None:
        rpc = FixtureGrokRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )
        events = [
            event async for event in connection.run_turn("hello", model="grok-build")
        ]

        assert [event.type for event in events] == [
            "started",
            "item_upsert",
            "item_upsert",
            "message_delta",
            "completed",
        ]
        assert events[1].mode == "plan"
        assert events[2].plan[0].status == "in_progress"
        assert events[-1].message == "done"
        await connection.interrupt()
        assert rpc.notifications == []

    asyncio.run(scenario())


def test_grok_connection_reuses_one_acp_transport_for_sequential_turns():
    async def scenario() -> None:
        rpc = FixtureGrokRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )

        first = [
            event async for event in connection.run_turn("first", model="grok-build")
        ]
        second = [
            event async for event in connection.run_turn("second", model="grok-build")
        ]

        assert first[-1].type == "completed"
        assert first[-1].message == "done"
        assert second[-1].type == "completed"
        assert second[-1].message == "done"
        assert [method for method, _ in rpc.calls].count("session/prompt") == 2

    asyncio.run(scenario())


def test_grok_connection_carries_selected_skill_into_prompt():
    async def scenario() -> None:
        rpc = FixtureGrokRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )
        events = [
            event
            async for event in connection.run_turn(
                "inspect the project",
                model="grok-build",
                skill=HarnessSkillInvocation(
                    name="review",
                    path="/workspace/.grok/skills/review/SKILL.md",
                ),
            )
        ]

        assert events[-1].message == "done"
        prompt = next(
            params for method, params in rpc.calls if method == "session/prompt"
        )
        assert prompt["prompt"] == [
            {"type": "text", "text": "$review inspect the project"}
        ]

    asyncio.run(scenario())


def test_grok_connection_discards_session_load_replay_before_new_turn():
    async def scenario() -> None:
        rpc = FixtureGrokRpc()
        await rpc.events.put(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "old answer"},
                    }
                },
            }
        )

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )
        events = [
            event
            async for event in connection.run_turn(
                "explain the result",
                model="grok-build",
            )
        ]

        assert [event.delta for event in events if event.type == "message_delta"] == [
            "done"
        ]
        assert events[-1].message == "done"

    asyncio.run(scenario())


def test_grok_connection_bounds_verbose_tool_titles_before_activity_ingestion():
    class VerboseToolRpc(FixtureGrokRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method != "session/prompt":
                return await super().request(method, params)
            self.calls.append((method, params))
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "verbose-tool",
                            "title": "Execute `" + ("x" * 6_000),
                        }
                    },
                }
            )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}

    async def scenario() -> None:
        rpc = VerboseToolRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )
        events = [
            event async for event in connection.run_turn("hello", model="grok-build")
        ]

        tool = next(event for event in events if event.type == "tool_started")
        assert tool.tool_name is not None
        assert len(tool.tool_name) == 1_000
        assert tool.tool_name.startswith("Execute `")

    asyncio.run(scenario())


def test_grok_connection_separates_pre_tool_commentary_from_final_answer():
    class CommentaryRpc(FixtureGrokRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method != "session/prompt":
                return await super().request(method, params)
            self.calls.append((method, params))
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "I'll inspect it."},
                        }
                    },
                }
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tool-1",
                            "title": "Inspect",
                        }
                    },
                }
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "The result is clear."},
                        }
                    },
                }
            )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}

    async def scenario() -> None:
        rpc = CommentaryRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )
        events = [
            event async for event in connection.run_turn("inspect", model="grok-build")
        ]

        assert [event.delta for event in events if event.type == "output_delta"] == [
            "I'll inspect it."
        ]
        assert [event.delta for event in events if event.type == "message_delta"] == [
            "The result is clear."
        ]
        assert events[-1].message == "The result is clear."

    asyncio.run(scenario())


def test_grok_connection_keeps_each_pre_tool_commentary_as_a_spaced_item():
    class SpacedCommentaryRpc(FixtureGrokRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method != "session/prompt":
                return await super().request(method, params)
            self.calls.append((method, params))
            for index, text in enumerate(
                ("I'll inspect the binary.", "I'll bind the exact imports."), start=1
            ):
                await self.events.put(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text},
                            }
                        },
                    }
                )
                await self.events.put(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": f"tool-{index}",
                                "title": f"Inspect {index}",
                            }
                        },
                    }
                )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Finished."},
                        }
                    },
                }
            )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}

    async def scenario() -> None:
        connection = GrokAcpConnection(
            SpacedCommentaryRpc(),
            external_session_id="grok-session",
            permission_handler=lambda _request: None,
        )
        events = [
            event async for event in connection.run_turn("inspect", model="grok-build")
        ]
        commentary = [
            event
            for event in events
            if event.type == "output_delta" and event.stream == "commentary"
        ]
        assert [(event.item_id, event.delta) for event in commentary] == [
            ("commentary-1", "I'll inspect the binary."),
            ("commentary-2", "I'll bind the exact imports."),
        ]
        assert events[-1].message == "Finished."

    asyncio.run(scenario())


def test_grok_connection_keeps_final_answer_before_session_metadata():
    class MetadataAfterAnswerRpc(FixtureGrokRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method != "session/prompt":
                return await super().request(method, params)
            self.calls.append((method, params))
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {
                                "type": "text",
                                "text": "The sibling is apple_security_rnd.",
                            },
                        }
                    },
                }
            )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": [],
                        }
                    },
                }
            )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}

    async def scenario() -> None:
        connection = GrokAcpConnection(
            MetadataAfterAnswerRpc(),
            external_session_id="grok-session",
            permission_handler=lambda _request: None,
        )
        events = [
            event async for event in connection.run_turn("name it", model="grok-build")
        ]

        completed = next(event for event in events if event.type == "completed")
        assert completed.message == "The sibling is apple_security_rnd."
        assert any(
            event.type == "message_delta"
            and event.delta == "The sibling is apple_security_rnd."
            for event in events
        )
        assert not any(event.stream == "commentary" for event in events)

    asyncio.run(scenario())


def test_goal_snapshots_are_normalized_only_from_advertised_state():
    goal = _harness_goal_snapshot(
        {
            "objective": "Ship the adapter",
            "status": "in-progress",
            "progress": 50,
            "currentStep": "Run compatibility tests",
            "tokensUsed": 12,
            "childAgents": 2,
        }
    )
    assert goal is not None
    assert goal.status == "running"
    assert goal.progress == 0.5
    assert goal.current_step == "Run compatibility tests"
    assert goal.tokens_used == 12
    assert goal.child_agents == 2
    assert _harness_goal_snapshot({"status": "running"}) is None


def test_claude_probe_offers_stable_model_aliases(monkeypatch):
    monkeypatch.setattr(
        ClaudeAgentSdkAdapter,
        "_sdk",
        staticmethod(lambda: SimpleNamespace(__version__="1.2.3")),
    )
    profile = HarnessProfile(
        id="claude-a",
        name="Claude",
        kind=HarnessKind.CLAUDE_AGENT_SDK,
        default_model="custom-alias",
    )

    health = asyncio.run(ClaudeAgentSdkAdapter().probe(profile, CredentialStore()))

    assert health.healthy is True
    assert health.capabilities.models == ["custom-alias", "sonnet", "opus"]


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
            capabilities=HarnessCapabilities(
                checked_at=utc_now(), protocol_version="app-server-v2"
            ),
        )
        session = HarnessSession(
            id="session-a",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="gpt-test",
            metadata={
                "runtime_options": {
                    "reasoning_effort": "high",
                    "service_tier": "fast",
                },
                "command_runtime_snapshot": {
                    "schema": "nebula.harness-command-runtime/v1",
                    "runtime_digest": "sha256:" + "a" * 64,
                    "specs": {
                        "run_command": {
                            "description": "Run Bash in the pinned Kali runtime",
                            "risk_class": "workspace_write",
                            "network_access": False,
                        }
                    },
                },
            },
        )
        connection = await adapter.open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(_mcp_profile(),),
                credential_store=CredentialStore(),
                permission_handler=permission,
                gateway_tools=(
                    {
                        "name": "knowledge.list",
                        "description": "List available Nebula sources",
                    },
                    {
                        "name": "knowledge.search",
                        "description": "Search available Nebula sources",
                    },
                ),
            )
        )
        events = [
            event async for event in connection.run_turn("inspect", model="gpt-5.4")
        ]

        assert [method for method, _ in rpc.calls[:3]] == [
            "initialize",
            "thread/start",
            "turn/start",
        ]
        assert rpc.notifications == [("initialized", None)]
        _validate("v1/InitializeParams.json", rpc.calls[0][1])
        assert rpc.calls[0][1]["capabilities"]["experimentalApi"] is True
        assert rpc.calls[0][1]["capabilities"]["mcpServerOpenaiFormElicitation"] is True
        _validate("v2/ThreadStartParams.json", rpc.calls[1][1])
        assert rpc.calls[1][1]["approvalPolicy"] == "never"
        assert rpc.calls[1][1]["reasoningEffort"] == "high"
        assert rpc.calls[1][1]["serviceTier"] == "fast"
        _validate("v2/TurnStartParams.json", rpc.calls[2][1])
        assert rpc.calls[2][1]["summary"] == "detailed"
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
        usage_event = next(event for event in events if event.type == "usage")
        assert usage_event.detailed_usage is not None
        assert usage_event.detailed_usage.input_tokens == 3
        assert usage_event.detailed_usage.output_tokens == 2
        assert usage_event.detailed_usage.cost_usd == pytest.approx(0.0000375)
        assert usage_event.detailed_usage.model_usage["gpt-5.4"] == {
            "cost_usd": pytest.approx(0.0000375),
            "pricing_basis": "standard_api_equivalent",
            "pricing_model": "gpt-5.4",
            "pricing_verified_on": "2026-07-22",
            "pricing_source": "https://developers.openai.com/api/docs/models/gpt-5.4",
        }
        assert decisions[0].category == "command"
        assert rpc.responses == [(41, {"decision": "accept"})]
        instructions = rpc.calls[1][1]["developerInstructions"]
        assert instructions.startswith("Nebula Codex session.")
        assert "cwd '.'" in instructions
        assert "TRUSTED" not in instructions
        assert "untrusted" not in instructions
        _validate("CommandExecutionRequestApprovalResponse.json", rpc.responses[0][1])

    asyncio.run(scenario())


def test_codex_project_never_policy_auto_approves_native_and_gateway_tools(tmp_path):
    async def scenario() -> None:
        rpc = FixtureCodexRpc()
        profile = HarnessProfile(
            id="codex-never",
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-test",
        )
        session = HarnessSession(
            id="session-never",
            engagement_id="eng-never",
            harness_profile_id=profile.id,
            model="gpt-test",
            metadata={"approval_policy": "never"},
        )

        connection = await FixtureCodexAdapter(rpc).open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                credential_store=CredentialStore(),
                permission_handler=lambda _request: None,
                gateway_config={
                    "nebula": {
                        "url": "http://127.0.0.1/never",
                        "transport": "streamable_http",
                        "required": True,
                        "startup_timeout_seconds": 10.0,
                        "tool_timeout_seconds": 900.0,
                    }
                },
            )
        )

        thread_start = next(
            params for method, params in rpc.calls if method == "thread/start"
        )
        assert thread_start["approvalPolicy"] == "never"
        assert connection.approval_policy == "never"
        await connection.close()

    asyncio.run(scenario())


def test_codex_turn_controls_use_structured_skill_and_planning_mode():
    async def scenario() -> None:
        rpc = FixtureCodexRpc()

        async def permission(request):
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket(
                "approval-controls", request.vendor_request_id, future
            )

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-fixture",
            permission_handler=permission,
        )
        events = [
            event
            async for event in connection.run_turn(
                "inspect",
                model="gpt-test",
                mode="plan",
                images=[{"media_type": "image/png", "data": "aW1hZ2U="}],
                skill=HarnessSkillInvocation(
                    name="review",
                    path="/workspace/.codex/skills/review/SKILL.md",
                ),
            )
        ]
        assert events[-1].message == "done"
        turn = next(params for method, params in rpc.calls if method == "turn/start")
        assert turn["input"][1] == {
            "type": "image",
            "url": "data:image/png;base64,aW1hZ2U=",
        }
        assert turn["collaborationMode"] == {"mode": "plan"}
        assert turn["input"][-1] == {
            "type": "skill",
            "name": "review",
            "path": "/workspace/.codex/skills/review/SKILL.md",
        }

    asyncio.run(scenario())


def test_codex_connection_discards_session_resume_replay_before_new_turn():
    async def scenario() -> None:
        rpc = FixtureCodexRpc()
        await rpc.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"delta": "old answer", "itemId": "old-message"},
            }
        )

        async def permission(request):
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket(
                "approval-boundary", request.vendor_request_id, future
            )

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-fixture",
            permission_handler=permission,
        )
        events = [
            event
            async for event in connection.run_turn("new question", model="gpt-test")
        ]

        assert "old answer" not in "".join(
            event.delta or "" for event in events if event.type == "message_delta"
        )
        assert events[-1].message == "done"

    asyncio.run(scenario())


def test_codex_filters_commentary_and_declined_elicitation_is_nonterminal():
    class PhaseRpc(FixtureCodexRpc):
        def __init__(self, server_name: str) -> None:
            super().__init__()
            self.server_name = server_name

        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method != "turn/start":
                return {}
            events = [
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "scan"}],
                        },
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "commentary-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                        },
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "turnId": "turn-phase",
                        "itemId": "commentary-1",
                        "delta": "I am checking ",
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "turnId": "turn-phase",
                        "itemId": "commentary-1",
                        "delta": " the interface. ",
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {"id": "reasoning-1", "type": "reasoning"},
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "content": ["private reasoning must not be retained"],
                        },
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {"id": "plan-1", "type": "plan", "text": "Scan"},
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "plan-1",
                            "type": "plan",
                            "text": "Scan complete",
                        },
                    },
                },
                {
                    "id": 92,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "turnId": "turn-phase",
                        "serverName": self.server_name,
                        "mode": "form",
                        "requestedSchema": {"type": "object", "properties": {}},
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                        },
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "turnId": "turn-phase",
                        "itemId": "final-1",
                        "delta": "The scan completed.",
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-phase",
                        "item": {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "The authoritative final answer.",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": "turn-phase",
                        "turn": {"id": "turn-phase", "status": "completed"},
                    },
                },
            ]
            for event in events:
                await self.events.put(event)
            return {"turn": {"id": "turn-phase"}}

    async def scenario() -> None:
        rpc = PhaseRpc("external")

        async def permission(_):
            raise AssertionError("no permission request expected")

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-phase",
            permission_handler=permission,
        )
        events = [item async for item in connection.run_turn("scan", model="gpt-test")]

        assert not any(item.type == "error" for item in events)
        assert [item.delta for item in events if item.type == "message_delta"] == [
            "The scan completed."
        ]
        assert [
            item.delta
            for item in events
            if item.type == "output_delta" and item.stream == "commentary"
        ] == ["I am checking ", " the interface. "]
        # A completed plan item is Codex's proposed plan; it follows the answer.
        assert events[-1].message == "The authoritative final answer.\n\nScan complete"
        assert not any(item.title == "User Message" for item in events)
        assert not any("unsupported" in (item.summary or "") for item in events)
        reasoning = [item for item in events if item.item_id == "reasoning-1"]
        assert [item.item_status for item in reasoning] == ["running", "completed"]
        assert all(item.type == "item_upsert" for item in reasoning)
        assert reasoning[0].payload["reasoning_summary_state"] == "pending"
        assert reasoning[1].payload["reasoning_summary_state"] == "not_provided"
        assert all(item.summary is None for item in reasoning)
        plan = [item for item in events if item.item_id == "plan-1"]
        assert [item.item_kind for item in plan] == ["plan", "plan"]
        assert [item.item_status for item in plan] == ["running", "completed"]
        assert "private reasoning must not be retained" not in json.dumps(
            [item.model_dump(mode="json") for item in events]
        )
        assert rpc.responses == [(92, {"action": "decline"})]

        trusted_rpc = PhaseRpc("nebula")
        trusted_connection = CodexAppServerConnection(
            trusted_rpc,
            external_session_id="thread-trusted",
            permission_handler=permission,
            approval_policy="never",
            trusted_mcp_servers=frozenset({"nebula"}),
        )
        trusted_events = [
            item async for item in trusted_connection.run_turn("scan", model="gpt-test")
        ]
        assert trusted_events[-1].message == (
            "The authoritative final answer.\n\nScan complete"
        )
        assert trusted_rpc.responses == [(92, {"action": "accept", "content": {}})]

    asyncio.run(scenario())


def test_codex_reasoning_summary_uses_streams_and_authoritative_completion():
    class ReasoningRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method != "turn/start":
                return {}
            events = [
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-reasoning",
                        "item": {"id": "reasoning-1", "type": "reasoning"},
                    },
                },
                {
                    "method": "item/reasoning/summaryPartAdded",
                    "params": {
                        "turnId": "turn-reasoning",
                        "itemId": "reasoning-1",
                        "summaryIndex": 0,
                    },
                },
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "turnId": "turn-reasoning",
                        "itemId": "reasoning-1",
                        "summaryIndex": 0,
                        "delta": "Inspecting the adapter. ",
                    },
                },
                {
                    "method": "item/reasoning/summaryPartAdded",
                    "params": {
                        "turnId": "turn-reasoning",
                        "itemId": "reasoning-1",
                        "summaryIndex": 1,
                    },
                },
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "turnId": "turn-reasoning",
                        "itemId": "reasoning-1",
                        "summaryIndex": 1,
                        "delta": "Checking replay.",
                    },
                },
                {
                    "method": "item/reasoning/textDelta",
                    "params": {
                        "turnId": "turn-reasoning",
                        "itemId": "reasoning-1",
                        "delta": "PRIVATE RAW REASONING",
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-reasoning",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "summary": [
                                "Inspected the adapter.",
                                "Verified durable replay.",
                            ],
                            "content": ["PRIVATE COMPLETED REASONING"],
                            "signature": "PRIVATE SIGNATURE",
                            "encrypted_content": "PRIVATE ENCRYPTED CONTENT",
                        },
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-reasoning",
                        "item": {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "The visible assistant response.",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": "turn-reasoning",
                        "turn": {"id": "turn-reasoning", "status": "completed"},
                    },
                },
            ]
            for event in events:
                await self.events.put(event)
            return {"turn": {"id": "turn-reasoning"}}

    async def scenario() -> None:
        rpc = ReasoningRpc()

        async def permission(_):
            raise AssertionError("no permission request expected")

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-reasoning",
            permission_handler=permission,
        )
        events = [
            event async for event in connection.run_turn("inspect", model="gpt-test")
        ]

        assert rpc.calls[0][1]["summary"] == "detailed"
        streamed = [
            event
            for event in events
            if event.type == "output_delta" and event.stream == "reasoning_summary"
        ]
        assert [event.delta for event in streamed] == [
            "Inspecting the adapter. ",
            "\n\nChecking replay.",
        ]
        assert all(
            event.payload["reasoning_summary_state"] == "available"
            for event in streamed
        )
        completed = next(
            event
            for event in events
            if event.item_id == "reasoning-1" and event.item_status == "completed"
        )
        assert completed.title == "Reasoning"
        assert completed.summary is None
        assert completed.payload == {
            "type": "reasoning",
            "reasoning_summary_state": "available",
            "reasoning_summary_text": (
                "Inspected the adapter.\n\nVerified durable replay."
            ),
            "reasoning_summary_source": "completed_item",
        }
        assert events[-1].message == "The visible assistant response."
        serialized = json.dumps([event.model_dump(mode="json") for event in events])
        assert "PRIVATE RAW REASONING" not in serialized
        assert "PRIVATE COMPLETED REASONING" not in serialized
        assert "PRIVATE SIGNATURE" not in serialized
        assert "PRIVATE ENCRYPTED CONTENT" not in serialized

    asyncio.run(scenario())


def test_codex_reasoning_summary_preserves_long_stream_without_snapshot():
    class StreamOnlyRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method != "turn/start":
                return {}
            long_summary = "s" * 270_000
            for event in [
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "turnId": "turn-stream-only",
                        "itemId": "reasoning-1",
                        "summaryIndex": 0,
                        "delta": long_summary,
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-stream-only",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "summary": [],
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": "turn-stream-only",
                        "turn": {
                            "id": "turn-stream-only",
                            "status": "completed",
                        },
                    },
                },
            ]:
                await self.events.put(event)
            return {"turn": {"id": "turn-stream-only"}}

    async def scenario() -> None:
        rpc = StreamOnlyRpc()

        async def permission(_):
            raise AssertionError("no permission request expected")

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-stream-only",
            permission_handler=permission,
        )
        events = [
            event async for event in connection.run_turn("inspect", model="gpt-test")
        ]
        streamed = [event for event in events if event.type == "output_delta"]
        completed = next(
            event
            for event in events
            if event.item_id == "reasoning-1" and event.item_status == "completed"
        )
        assert "".join(event.delta for event in streamed) == "s" * 270_000
        assert all(len(event.delta) <= 200_000 for event in streamed)
        assert completed.payload["reasoning_summary_state"] == "available"
        assert completed.payload["reasoning_summary_source"] == "stream"
        assert completed.payload["reasoning_summary_text"] == "s" * 270_000

    asyncio.run(scenario())


def test_codex_reasoning_summary_rejects_malformed_private_payloads():
    class MalformedSummaryRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method != "turn/start":
                return {}
            for event in [
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {
                        "turnId": "turn-malformed",
                        "itemId": "reasoning-1",
                        "delta": {"private": "MALFORMED PRIVATE VALUE"},
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-malformed",
                        "item": {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "summary": [42, "Safe surviving summary"],
                            "content": ["PRIVATE CONTENT"],
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "turnId": "turn-malformed",
                        "turn": {"id": "turn-malformed", "status": "completed"},
                    },
                },
            ]:
                await self.events.put(event)
            return {"turn": {"id": "turn-malformed"}}

    async def scenario() -> None:
        rpc = MalformedSummaryRpc()

        async def permission(_):
            raise AssertionError("no permission request expected")

        connection = CodexAppServerConnection(
            rpc,
            external_session_id="thread-malformed",
            permission_handler=permission,
        )
        events = [
            event async for event in connection.run_turn("inspect", model="gpt-test")
        ]
        notice = next(event for event in events if event.type == "notice")
        assert notice.payload == {
            "method": "item/reasoning/summaryTextDelta",
            "value_type": "dict",
        }
        completed = next(
            event
            for event in events
            if event.item_id == "reasoning-1" and event.item_status == "completed"
        )
        assert completed.payload["reasoning_summary_text"] == ("Safe surviving summary")
        assert completed.payload["reasoning_summary_malformed"] is True
        serialized = json.dumps([event.model_dump(mode="json") for event in events])
        assert "MALFORMED PRIVATE VALUE" not in serialized
        assert "PRIVATE CONTENT" not in serialized

    asyncio.run(scenario())


def test_codex_probe_rejects_unverified_reasoning_summary_baseline():
    class OldCodexRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method == "initialize":
                self.calls.append((method, params))
                return {"userAgent": "codex-cli/0.143.0"}
            return await super().request(method, params)

    async def scenario() -> None:
        rpc = OldCodexRpc()
        adapter = FixtureCodexAdapter(rpc)
        profile = HarnessProfile(
            id="codex-old",
            name="Old Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )

        health = await adapter.probe(profile, CredentialStore())

        assert health.healthy is False
        assert health.capabilities.reasoning_summaries is False
        assert "0.144.0 or newer" in (health.detail or "")
        assert rpc.closed is True

        open_rpc = OldCodexRpc()
        open_adapter = FixtureCodexAdapter(open_rpc)
        session = HarnessSession(
            id="session-old",
            engagement_id="eng-old",
            harness_profile_id=profile.id,
            model="gpt-test",
        )

        async def permission(_):
            raise AssertionError("no permission request expected")

        try:
            await open_adapter.open(
                AdapterOpenRequest(
                    profile=profile,
                    session=session,
                    workspace=Path.cwd(),
                    mcp_profiles=(),
                    credential_store=CredentialStore(),
                    permission_handler=permission,
                )
            )
        except HarnessConfigurationError as exc:
            assert "0.144.0 or newer" in str(exc)
        else:
            raise AssertionError("an incompatible Codex app-server was opened")
        assert [method for method, _ in open_rpc.calls] == ["initialize"]
        assert open_rpc.closed is True

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


def test_codex_rpc_reader_survives_one_invalid_frame():
    class FixtureStdout:
        def __init__(self) -> None:
            self.lines = iter(
                [
                    b'{"id":"not-an-integer","result":{}}\n',
                    b'{"method":"session/update","params":{"update":{"sessionUpdate":"plan"}}}\n',
                    b"",
                ]
            )

        async def read(self, _size: int = -1) -> bytes:
            return next(self.lines)

    async def scenario() -> None:
        rpc = _CodexRpc(
            process=SimpleNamespace(stdout=FixtureStdout(), stderr=None, returncode=0)
        )

        await rpc._reader()

        recovered = await rpc.events.get()
        closed = await rpc.events.get()
        assert isinstance(recovered, dict)
        assert recovered["method"] == "session/update"
        assert isinstance(closed, Exception)
        assert "transport closed" in str(closed)

    asyncio.run(scenario())


def test_grok_acp_rpc_reports_its_own_transport_identity():
    class ClosedStdout:
        async def read(self, _size: int = -1) -> bytes:
            return b""

    async def scenario() -> None:
        rpc = _AcpRpc(
            process=SimpleNamespace(stdout=ClosedStdout(), stderr=None, returncode=0)
        )

        await rpc._reader()

        closed = await rpc.events.get()
        assert isinstance(closed, Exception)
        assert str(closed) == "Grok ACP transport closed"
        assert "Codex" not in str(closed)

    asyncio.run(scenario())


def test_codex_rpc_malformed_frame_fails_an_affected_request_without_stopping_reader():
    async def scenario() -> None:
        rpc = _CodexRpc()
        pending = asyncio.get_running_loop().create_future()
        rpc._pending[1] = pending

        await rpc._dispatch_frame(b"not-json\n")

        with pytest.raises(Exception, match="malformed JSON"):
            await pending
        rejected = await rpc.events.get()
        assert isinstance(rejected, Exception)

    asyncio.run(scenario())


def test_codex_gateway_thread_disables_vendor_execution_and_environment():
    config = _codex_thread_config({})

    assert config["features"]["code_mode_host"] is True
    assert config["features"]["code_mode"] is False
    assert config["features"]["shell_tool"] is False
    assert config["features"]["unified_exec"] is False
    assert config["features"]["plugins"] is False
    assert config["features"]["browser_use"] is False
    assert config["features"]["goals"] is True
    assert config["web_search"] == "disabled"
    assert config["shell_environment_policy"] == {
        "inherit": "none",
        "set": {"PATH": "/nonexistent"},
    }


def test_codex_native_capabilities_are_explicit_and_keep_shell_environment_minimal():
    native = HarnessNativeCapabilities(
        workspace_access=HarnessWorkspaceAccess.WRITE,
        shell=True,
        web_search=True,
        browser=True,
        computer_use=True,
        image_generation=True,
        subagents=True,
    )
    config = _codex_thread_config({}, native_capabilities=native)

    assert config["features"]["shell_tool"] is True
    assert config["features"]["unified_exec"] is True
    assert config["features"]["browser_use"] is True
    assert config["features"]["in_app_browser"] is True
    assert config["features"]["browser_use_external"] is False
    assert config["features"]["computer_use"] is True
    assert config["features"]["image_generation"] is True
    assert config["features"]["multi_agent"] is True
    assert config["features"]["plugins"] is False
    assert config["web_search"] == "live"
    assert config["shell_environment_policy"]["inherit"] == "all"
    assert config["shell_environment_policy"]["set"]["PATH"] != "/nonexistent"


def test_codex_process_enables_goals_for_the_goal_command():
    # Codex rejects thread/goal/* with "goals feature is disabled" otherwise.
    overrides = _codex_process_overrides(HarnessNativeCapabilities())

    assert "features.goals=true" in overrides
    assert "features.goals=false" not in overrides


def _goal_turn(method: str, turn_id: str, status: str, thread: str = "thread-goal"):
    return {
        "method": method,
        "params": {"threadId": thread, "turn": {"id": turn_id, "status": status}},
    }


def _goal_update(turn_id: str | None, status: str) -> dict[str, Any]:
    return {
        "method": "thread/goal/updated",
        "params": {
            "threadId": "thread-goal",
            "turnId": turn_id,
            "goal": {
                "threadId": "thread-goal",
                "objective": "count to two",
                "status": status,
            },
        },
    }


def _goal_answer(turn_id: str, text: str) -> dict[str, Any]:
    return {
        "method": "item/agentMessage/delta",
        "params": {"turnId": turn_id, "itemId": f"answer-{turn_id}", "delta": text},
    }


class GoalRpc(FixtureCodexRpc):
    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__()
        self.script = script
        self.running_turns: dict[str, str] = {}

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method != "turn/start":
            return {}
        for event in self.script:
            await self.events.put(event)
        return {"turn": {"id": "turn-1"}}


def _goal_connection(rpc: GoalRpc) -> CodexAppServerConnection:
    async def permission(_):
        raise AssertionError("no permission request expected")

    return CodexAppServerConnection(
        rpc, external_session_id="thread-goal", permission_handler=permission
    )


def test_codex_turn_follows_the_turns_codex_starts_for_an_active_goal():
    rpc = GoalRpc(
        [
            _goal_answer("turn-1", "alpha"),
            _goal_update("turn-1", "active"),
            _goal_turn("turn/completed", "turn-1", "completed"),
            # A Codex subagent thread shares the connection; it is not a goal turn.
            _goal_turn("turn/started", "child-turn", "inProgress", thread="child"),
            _goal_turn("turn/started", "turn-2", "inProgress"),
            _goal_answer("turn-2", "beta"),
            _goal_update("turn-2", "complete"),
            _goal_turn("turn/completed", "turn-2", "completed"),
        ]
    )

    async def scenario() -> list[Any]:
        connection = _goal_connection(rpc)
        return [event async for event in connection.run_turn("go", model="gpt-test")]

    events = asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert [method for method, _ in rpc.calls] == ["turn/start"]
    assert [e.external_turn_id for e in events if e.type == "started"] == [
        "turn-1",
        "turn-2",
    ]
    assert "".join(e.delta for e in events if e.type == "message_delta") == (
        "alpha\n\nbeta"
    )
    assert [e.goal.status for e in events if e.goal is not None] == [
        "running",
        "complete",
    ]
    assert events[-1].type == "completed"
    assert events[-1].message == "alpha\n\nbeta"
    assert events[-1].external_turn_id == "turn-2"


def test_codex_goal_turn_completes_when_codex_does_not_continue(monkeypatch):
    monkeypatch.setattr(harness_module, "CODEX_GOAL_CONTINUATION_TIMEOUT_SECONDS", 0.01)
    rpc = GoalRpc(
        [
            _goal_answer("turn-1", "alpha"),
            _goal_update("turn-1", "active"),
            _goal_turn("turn/completed", "turn-1", "completed"),
        ]
    )

    async def scenario() -> list[Any]:
        connection = _goal_connection(rpc)
        return [event async for event in connection.run_turn("go", model="gpt-test")]

    events = asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    assert events[-1].type == "completed"
    assert events[-1].message == "alpha"
    assert events[-1].external_turn_id == "turn-1"


def test_codex_goal_paused_between_turns_ends_the_wait_at_once():
    rpc = GoalRpc(
        [
            _goal_answer("turn-1", "alpha"),
            _goal_update("turn-1", "active"),
            _goal_turn("turn/completed", "turn-1", "completed"),
            _goal_update(None, "paused"),
        ]
    )

    async def scenario() -> list[Any]:
        connection = _goal_connection(rpc)
        return [event async for event in connection.run_turn("go", model="gpt-test")]

    # Far below the 15 s continuation wait.
    events = asyncio.run(asyncio.wait_for(scenario(), timeout=2))

    assert events[-1].type == "completed"
    assert events[-1].message == "alpha"
    assert [e.goal.status for e in events if e.goal is not None] == [
        "running",
        "paused",
    ]


@pytest.mark.parametrize("started", [None, "turn-2"])
def test_codex_stop_between_goal_turns_pauses_the_goal(started):
    rpc = GoalRpc(
        [
            _goal_update("turn-1", "active"),
            _goal_turn("turn/completed", "turn-1", "completed"),
        ]
    )

    async def scenario() -> None:
        connection = _goal_connection(rpc)

        async def consume() -> None:
            async for _ in connection.run_turn("go", model="gpt-test"):
                pass

        # The runtime cancels the turn task, then interrupts the connection.
        task = asyncio.create_task(consume())
        while not connection._awaiting_goal_turn:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if started:
            rpc.running_turns["thread-goal"] = started
        await connection.interrupt()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    stop_calls = rpc.calls[1:]
    assert stop_calls[0] == (
        "thread/goal/set",
        {"threadId": "thread-goal", "status": "paused"},
    )
    assert stop_calls[1:] == (
        [("turn/interrupt", {"threadId": "thread-goal", "turnId": started})]
        if started
        else []
    )


def test_codex_rpc_records_the_running_turn_of_each_thread():
    async def scenario() -> None:
        rpc = _CodexRpc()
        await rpc._dispatch(
            json.dumps(_goal_turn("turn/started", "turn-2", "inProgress"))
        )
        assert rpc.running_turns == {"thread-goal": "turn-2"}
        await rpc._dispatch(
            json.dumps(_goal_turn("turn/completed", "turn-2", "completed"))
        )
        assert rpc.running_turns == {}
        # Recording never swallows the notification.
        assert rpc.events.qsize() == 2

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


class ThinkingBlock:
    def __init__(self, thinking: str, signature: str) -> None:
        self.thinking = thinking
        self.signature = signature


class FutureClaudeMessage:
    raw = "vendor diagnostics must not be retained"


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
        self.mcp_status_calls = 0
        self.queries: list[str] = []
        self.interrupted = False
        self.disconnected = False
        self.pending_turns: asyncio.Queue[str] = asyncio.Queue()
        FakeClaudeClient.latest = self

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self.pending_turns.put_nowait(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        # Like the SDK: one ordered stream for the session, one reply per query.
        while True:
            await self.pending_turns.get()
            async for message in self.receive_response():
                yield message

    async def receive_response(self) -> AsyncIterator[Any]:
        yield StreamEvent(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "ok"},
            }
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
        self.mcp_status_calls += 1
        return {
            "mcpServers": [
                {"name": name, "status": "connected"}
                for name in self.options.kwargs["mcp_servers"]
            ]
        }

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def test_claude_turn_does_not_scan_workspace(tmp_path, monkeypatch):
    class WriteClient(FakeClaudeClient):
        async def receive_response(self) -> AsyncIterator[Any]:
            yield AssistantMessage(
                [ToolUseBlock("tool-write", "Write", {"file_path": "out.txt"})]
            )
            yield UserMessage([ToolResultBlock("tool-write", "written")])
            yield ResultMessage()

    def no_recursive_walk(*_args, **_kwargs):
        raise AssertionError("Claude turns must not scan the project")

    monkeypatch.setattr(Path, "rglob", no_recursive_walk)

    async def scenario() -> None:
        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = ClaudeAgentSdkConnection(
            WriteClient(options=FakeClaudeOptions()),
            permission_handler=no_permission,
            sdk=SimpleNamespace(),
            external_session_id=None,
            workspace=tmp_path,
        )
        events = [event async for event in connection.run_turn("read", model="test")]
        assert events[-1].type == "completed"
        file_change = next(event for event in events if event.type == "tool_started")
        assert file_change.item_kind == "file_change"
        assert not any(event.item_id == "workspace-changes" for event in events)

    asyncio.run(scenario())


def test_claude_reasoning_text_is_discarded_and_future_messages_are_notices(tmp_path):
    class ReasoningClient:
        def __init__(self) -> None:
            self.pending_turns: asyncio.Queue[str] = asyncio.Queue()

        async def query(self, prompt: str) -> None:
            self.pending_turns.put_nowait(prompt)

        async def receive_messages(self) -> AsyncIterator[Any]:
            while True:
                await self.pending_turns.get()
                yield AssistantMessage(
                    [ThinkingBlock("private reasoning marker", "private signature")]
                )
                yield FutureClaudeMessage()
                yield ResultMessage()

    async def permission(_request):
        raise AssertionError("no permission request expected")

    async def scenario() -> None:
        connection = ClaudeAgentSdkConnection(
            ReasoningClient(),
            permission_handler=permission,
            sdk=SimpleNamespace(),
            external_session_id=None,
            workspace=tmp_path,
        )
        events = [event async for event in connection.run_turn("think", model="test")]
        serialized = json.dumps([event.model_dump(mode="json") for event in events])
        reasoning = next(event for event in events if event.item_kind == "reasoning")
        assert reasoning.summary == (
            "Claude reasoning trace is hidden; only lifecycle is retained."
        )
        assert "private reasoning marker" not in serialized
        assert "private signature" not in serialized
        notice = next(
            event
            for event in events
            if event.type == "notice" and "FutureClaudeMessage" in (event.summary or "")
        )
        assert notice.payload == {"message_type": "FutureClaudeMessage"}
        assert FutureClaudeMessage.raw not in serialized

    asyncio.run(scenario())


class PermissionResultAllow:
    behavior = "allow"

    def __init__(self, updated_input: dict[str, Any]) -> None:
        self.updated_input = updated_input


class FakeHookMatcher:
    def __init__(self, *, matcher: str | None, hooks: list[Any]) -> None:
        self.matcher = matcher
        self.hooks = hooks


def test_claude_sdk_strict_mcp_resume_permissions_and_partial_messages(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            HookMatcher=FakeHookMatcher,
            PermissionResultAllow=PermissionResultAllow,
        )
        monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
        observed_permissions: list[Any] = []
        handoff_receipts: list[str] = []

        async def permission(request):
            observed_permissions.append(request)
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket(None, "call-1", future, handoff_receipts.append)

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
        assert options["tools"] == []
        assert options["skills"] == []
        assert set(options["disallowed_tools"]) == {
            "Agent",
            "Bash",
            "Edit",
            "Glob",
            "Grep",
            "NotebookEdit",
            "Read",
            "Skill",
            "WebFetch",
            "WebSearch",
            "Write",
        }
        assert options["env"]["ANTHROPIC_API_KEY"] == "anthropic-fixture-secret"
        assert options["env"]["PATH"]

        pre_tool_use = options["hooks"]["PreToolUse"][0].hooks[0]
        hook_result = await pre_tool_use(
            {"tool_name": "mcp__workspace_server__read_file"}, None, None
        )
        assert hook_result["hookSpecificOutput"]["permissionDecision"] == "ask"

        permission_result = await options["can_use_tool"](
            "mcp__workspace_server__read_file", {"path": "README.md"}, None
        )
        assert permission_result.behavior == "allow"
        assert handoff_receipts == ["sent"]
        assert observed_permissions[0].server_name == "workspace_server"
        assert observed_permissions[0].tool_name == "read_file"

        events = [
            event
            async for event in connection.run_turn("continue", model="claude-test")
        ]
        assert [event.type for event in events] == [
            "started",
            "message_delta",
            "output_delta",
            "tool_started",
            "tool_completed",
            "usage",
            "completed",
        ]
        # Text a tool use follows is narration, not the answer.
        assert (events[2].stream, events[2].delta) == ("commentary", "ok")
        tool_started = next(event for event in events if event.type == "tool_started")
        assert tool_started.server_id == "workspace_server"
        assert tool_started.tool_name == "read_file"
        assert events[-1].message == "ok"
        assert events[-1].external_session_id == "claude-session-learned"
        usage_event = next(event for event in reversed(events) if event.type == "usage")
        assert usage_event.detailed_usage is not None
        assert usage_event.detailed_usage.total_tokens == 11

        connection.active = True
        await connection.steer("focus")
        await connection.interrupt()
        assert FakeClaudeClient.latest.queries == ["continue", "focus"]
        assert FakeClaudeClient.latest.interrupted is True
        await connection.close()
        assert FakeClaudeClient.latest.disconnected is True

    asyncio.run(scenario())


def test_claude_native_capabilities_exclude_project_files_and_shell(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            HookMatcher=FakeHookMatcher,
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
            return PermissionTicket(None, "call-native", future)

        native = HarnessNativeCapabilities(
            workspace_access=HarnessWorkspaceAccess.READ,
            shell=True,
            web_search=True,
            web_fetch=True,
            skills=True,
            subagents=True,
        )
        profile = HarnessProfile(
            id="claude-native",
            name="Claude native",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="claude-test",
            native_capabilities=native,
        )
        session = HarnessSession(
            id="session-native",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="claude-test",
            metadata={"native_capabilities": native.model_dump(mode="json")},
        )
        connection = await ClaudeAgentSdkAdapter().open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                credential_store=CredentialStore(),
                permission_handler=permission,
            )
        )
        options = FakeClaudeClient.latest.options.kwargs
        assert set(options["tools"]) == {
            "WebSearch",
            "WebFetch",
            "Skill",
            "Agent",
        }
        assert options["setting_sources"] == ["user"]
        assert options["skills"] == "all"
        assert {
            "Read",
            "Glob",
            "Grep",
            "Bash",
            "Write",
            "Edit",
            "NotebookEdit",
        }.issubset(options["disallowed_tools"])
        assert options["system_prompt"].startswith("Nebula Claude session.")
        assert (
            "Project operations use the supplied Nebula tools"
            in options["system_prompt"]
        )

        pre_tool_use = options["hooks"]["PreToolUse"][0].hooks[0]
        read_result = await pre_tool_use(
            {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
            None,
            None,
        )
        skill_result = await pre_tool_use(
            {"tool_name": "Skill", "tool_input": {"skill": "review"}},
            None,
            None,
        )
        write_result = await pre_tool_use(
            {"tool_name": "Write", "tool_input": {"file_path": "out.txt"}},
            None,
            None,
        )
        assert read_result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert skill_result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert write_result["hookSpecificOutput"]["permissionDecision"] == "deny"

        allowed = await options["can_use_tool"]("Skill", {"skill": "review"}, None)
        assert allowed.behavior == "allow"
        assert observed_permissions[-1].vendor_name == "Skill"
        await connection.close()

    asyncio.run(scenario())


def test_claude_host_execution_mode_keeps_native_project_tools(tmp_path, monkeypatch):
    async def scenario() -> None:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            HookMatcher=FakeHookMatcher,
            PermissionResultAllow=PermissionResultAllow,
        )
        monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))

        async def permission(_request):
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=True))
            return PermissionTicket(None, "call-host", future)

        native = HarnessNativeCapabilities(
            workspace_access=HarnessWorkspaceAccess.WRITE,
            shell=True,
            skills=True,
        )
        profile = HarnessProfile(
            id="claude-host",
            name="Claude host",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="claude-test",
            native_capabilities=native,
        )
        session = HarnessSession(
            id="session-host-claude",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="claude-test",
            metadata={
                "execution_mode": "host",
                "native_capabilities": native.model_dump(mode="json"),
            },
        )
        connection = await ClaudeAgentSdkAdapter().open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                credential_store=CredentialStore(),
                permission_handler=permission,
            )
        )
        options = FakeClaudeClient.latest.options.kwargs
        assert {
            "Read",
            "Glob",
            "Grep",
            "Write",
            "Edit",
            "NotebookEdit",
            "Bash",
        }.issubset(set(options["tools"]))
        assert options["disallowed_tools"] == ["Agent", "WebFetch", "WebSearch"]
        assert options["enable_file_checkpointing"] is True
        assert (
            "Use vendor-native filesystem and shell tools" in options["system_prompt"]
        )

        pre_tool_use = options["hooks"]["PreToolUse"][0].hooks[0]
        read_result = await pre_tool_use(
            {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
            None,
            None,
        )
        assert read_result["hookSpecificOutput"]["permissionDecision"] == "ask"
        await connection.close()

    asyncio.run(scenario())


def test_claude_gateway_is_required_and_ready_before_the_session_opens(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        sdk = SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeOptions,
            ClaudeSDKClient=FakeClaudeClient,
            HookMatcher=FakeHookMatcher,
            PermissionResultAllow=PermissionResultAllow,
        )
        monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))

        async def permission(_request):
            future: asyncio.Future[HarnessPermissionDecision] = (
                asyncio.get_running_loop().create_future()
            )
            future.set_result(HarnessPermissionDecision(allowed=False))
            return PermissionTicket(None, None, future)

        profile = HarnessProfile(
            id="claude-gateway",
            name="Claude gateway",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="claude-test",
        )
        session = HarnessSession(
            id="session-gateway",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="claude-test",
        )
        connection = await ClaudeAgentSdkAdapter().open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                gateway_config={
                    "nebula": {
                        "transport": "stdio",
                        "command": "/usr/bin/python3",
                        "args": ["-m", "nebula.v3.mcp_gateway"],
                        "env": {"NEBULA_MCP_GATEWAY_TOKEN": "fixture"},
                        "required": True,
                        "startup_timeout_seconds": 10.0,
                        "tool_timeout_seconds": 900.0,
                    }
                },
                credential_store=CredentialStore(),
                permission_handler=permission,
            )
        )

        options = FakeClaudeClient.latest.options.kwargs
        assert set(options["mcp_servers"]) == {"nebula"}
        assert FakeClaudeClient.latest.mcp_status_calls == 1
        assert "Bash" in options["disallowed_tools"]
        assert "Read" in options["disallowed_tools"]
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("resumed", [False, True])
def test_grok_receives_project_gateway_on_create_and_resume(tmp_path, resumed):
    async def scenario():
        rpc = FixtureGrokRpc()
        profile = HarnessProfile(
            id="grok-a",
            name="Grok",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
        )
        session = HarnessSession(
            engagement_id="linked-project",
            harness_profile_id=profile.id,
            model="grok-test",
            external_session_id="existing-session" if resumed else None,
        )

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = await FixtureGrokAdapter(rpc).open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                credential_store=CredentialStore(),
                permission_handler=no_permission,
                gateway_config={
                    "nebula": {
                        "transport": "stdio",
                        "command": "/gateway/python",
                        "args": ["-m", "nebula.gateway"],
                        "env": {"NEBULA_MCP_GATEWAY_TOKEN": "fixture-token"},
                    }
                },
                gateway_tools=(
                    {"name": "workspace.read", "description": "Read project files"},
                ),
            )
        )
        method = "session/load" if resumed else "session/new"
        params = next(params for name, params in rpc.calls if name == method)
        assert params["mcpServers"] == [
            {
                "name": "nebula",
                "command": "/gateway/python",
                "args": ["-m", "nebula.gateway"],
                "env": [{"name": "NEBULA_MCP_GATEWAY_TOKEN", "value": "fixture-token"}],
            }
        ]
        for prompt in ("Inspect the project", "Check it again"):
            _ = [
                event async for event in connection.run_turn(prompt, model="grok-test")
            ]
        prompts = [
            params["prompt"][0]["text"]
            for name, params in rpc.calls
            if name == "session/prompt"
        ]
        assert len(prompts) == 2
        for prompt in prompts:
            assert "private scratch, not the project workspace" in prompt
            assert "workspace.read" in prompt
            assert "fixture-token" not in prompt
        await connection.close()

    asyncio.run(scenario())


def test_grok_tool_identity_failure_and_nested_command_receipt():
    from nebula.v3.harnesses import _grok_tool_details

    start = _grok_tool_details(
        {
            "title": "use_tool",
            "rawInput": {"tool_name": "nebula__workspace_search_aabbccddeeff0011"},
        }
    )
    failed = _grok_tool_details(
        {
            "status": "failed",
            "rawOutput": {"message": "Mcp error: -32603: search deadline exceeded"},
        },
        start,
    )
    assert failed["tool_name"] == start["tool_name"]
    assert failed["summary"] == "Workspace search failed — search deadline exceeded"
    cancelled = _grok_tool_details({"status": "cancelled"}, start)
    assert cancelled["summary"] == "Workspace search cancelled"
    assert _grok_tool_details({"title": "use_tool"}, failed)["item_status"] == "failed"
    nested = _grok_tool_details(
        {
            "status": "failed",
            "rawOutput": {
                "tool_name": "runtime_aabbccdd_run_command",
                "server_name": "nebula",
                "output": {
                    "Error": '{"exit_code":2,"summary":"Some files were unreadable; partial output retained"}'
                },
            },
        }
    )
    assert nested["server_id"] == "nebula"
    assert "unreadable" in nested["summary"]
    assert nested["item_status"] == "failed"

    class RecoveryRpc(FixtureGrokRpc):
        async def request(self, method, params):
            if method != "session/prompt":
                return await super().request(method, params)
            for update in [
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "same-call",
                    "rawInput": {"tool_name": "nebula__workspace_search_aabbccddeeff"},
                },
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "same-call",
                    "status": "failed",
                    "rawOutput": {"message": "search deadline exceeded"},
                },
            ]:
                await self.events.put(
                    {"method": "session/update", "params": {"update": update}}
                )
            await asyncio.sleep(0.02)
            return {"stopReason": "end_turn"}

    async def scenario():
        async def denied(_):
            raise AssertionError("No commands or approvals expected")

        connection = GrokAcpConnection(
            RecoveryRpc(),
            external_session_id="saved-fixture",
            permission_handler=denied,
        )
        events = [
            event
            async for event in connection.run_turn(
                "synthetic normalization", model="fixture"
            )
        ]
        tools = [event for event in events if event.item_kind == "tool"]
        assert len(tools) == 2
        assert tools[0].item_id == tools[1].item_id == "same-call"
        assert tools[0].tool_name == tools[1].tool_name
        assert tools[1].item_status == "failed"
        assert "search deadline exceeded" in tools[1].summary

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
def test_grok_thinking_episodes_drain_completion_race(stop_reason):
    class ThinkingRpc(FixtureGrokRpc):
        async def request(self, method, params):
            if method != "session/prompt":
                return await super().request(method, params)
            for update in [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "First "},
                },
                {"sessionUpdate": "current_mode_update", "currentModeId": "auto"},
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "episode"},
                },
                {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read"},
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Second episode"},
                },
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Public answer"},
                },
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Last episode" + "." * 210_000},
                },
            ]:
                self.events.put_nowait(
                    {"method": "session/update", "params": {"update": update}}
                )
            # Return immediately: completion races with the queue reader.
            return {"stopReason": stop_reason}

    async def scenario():
        async def denied(_request):
            raise AssertionError("No permission expected")

        connection = GrokAcpConnection(
            ThinkingRpc(),
            external_session_id="thinking-test",
            permission_handler=denied,
        )
        events = [event async for event in connection.run_turn("test", model="fixture")]
        thoughts = [event for event in events if event.stream == "reasoning_summary"]
        assert [event.item_id for event in thoughts] == [
            "thinking-1",
            "thinking-1",
            "thinking-2",
            "thinking-3",
            "thinking-3",
        ]
        assert (
            "".join(event.delta for event in thoughts)
            == "First episodeSecond episodeLast episode" + "." * 210_000
        )
        closed = [
            event
            for event in events
            if event.type == "item_upsert" and event.item_kind == "reasoning"
        ]
        assert len(closed) == 3
        assert closed[-1].item_status == (
            "cancelled" if stop_reason == "cancelled" else "completed"
        )
        assert events[-1].type == (
            "interrupted" if stop_reason == "cancelled" else "completed"
        )
        next_turn = [
            event async for event in connection.run_turn("next", model="fixture")
        ]
        assert (
            next(
                event for event in next_turn if event.stream == "reasoning_summary"
            ).item_id
            == "thinking-1"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "account,healthy",
    [
        ({"account": None, "requiresOpenaiAuth": True}, False),
        ({"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}, True),
        ({"account": None, "requiresOpenaiAuth": False}, True),
        ({}, False),
    ],
)
def test_codex_health_checks_authentication(account, healthy):
    async def scenario():
        rpc = FixtureCodexRpc()
        original = rpc.request

        async def request(method, params):
            if method == "account/read":
                return account
            return await original(method, params)

        rpc.request = request
        profile = HarnessProfile(
            name="Codex", kind=HarnessKind.CODEX_APP_SERVER, executable="/bin/true"
        )
        result = await FixtureCodexAdapter(rpc).probe(profile, CredentialStore())
        assert result.healthy is healthy
        assert rpc.closed
        if account.get("requiresOpenaiAuth") and not account.get("account"):
            assert "codex login" in result.detail

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,method",
    [
        ("codex", "initialize"),
        ("codex", "thread/start"),
        ("codex", "thread/resume"),
        ("grok", "session/new"),
        ("grok", "session/load"),
    ],
)
@pytest.mark.parametrize("cancel", [False, True])
def test_harness_startup_deadline_and_cancellation_close_transport(
    tmp_path, monkeypatch, kind, method, cancel
):
    import nebula.v3.harnesses as module

    async def scenario():
        monkeypatch.setattr(
            module, "HARNESS_STARTUP_TIMEOUT_SECONDS", 0.03 if not cancel else 10
        )
        rpc = FixtureCodexRpc() if kind == "codex" else FixtureGrokRpc()
        original = rpc.request
        waiting = asyncio.Event()

        async def request(name, params):
            if name == method:
                waiting.set()
                await asyncio.Future()
            return await original(name, params)

        rpc.request = request
        adapter = (
            FixtureCodexAdapter(rpc) if kind == "codex" else FixtureGrokAdapter(rpc)
        )
        profile = HarnessProfile(name=kind, kind=adapter.kind, executable="/bin/true")
        session = HarnessSession(
            engagement_id="project",
            harness_profile_id=profile.id,
            model="fixture",
            external_session_id="existing"
            if method.endswith(("resume", "load"))
            else None,
        )
        task = asyncio.create_task(
            adapter.open(
                AdapterOpenRequest(
                    profile=profile,
                    session=session,
                    workspace=tmp_path,
                    mcp_profiles=(),
                    credential_store=CredentialStore(),
                    permission_handler=lambda _request: None,
                )
            )
        )
        await asyncio.wait_for(waiting.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await asyncio.wait_for(task, 1)
        assert rpc.closed

    asyncio.run(scenario())


def test_codex_stdio_accepts_large_thread_response(tmp_path):
    async def scenario():
        executable = tmp_path / "codex-fixture"
        executable.write_text(
            '#!/usr/bin/env python3\nimport json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n if "id" in r: print(json.dumps({"id":r["id"],"result":{"history":"x"*200000}}),flush=True)\n'
        )
        executable.chmod(0o700)
        profile = HarnessProfile(
            name="Large frame",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable=str(executable),
        )
        rpc = await CodexAppServerAdapter()._connect(
            profile, CredentialStore(), (), tmp_path
        )
        try:
            result = await asyncio.wait_for(rpc.request("thread/resume", {}), 3)
            assert len(result["history"]) == 200000
            assert rpc.connection_state == "connected"
        finally:
            await rpc.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,key",
    [(HarnessKind.CODEX_APP_SERVER, "CODEX_HOME"), (HarnessKind.GROK_ACP, "GROK_HOME")],
)
def test_account_homes_isolate_spawn_without_changing_workspace(
    tmp_path, monkeypatch, kind, key
):
    from nebula.v3.harnesses import _harness_environment

    observed = []

    async def create_process(*argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(stdin=None, stdout=None, stderr=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(_AcpRpc, "start", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(_CodexRpc, "start", lambda self: asyncio.sleep(0))
    monkeypatch.setenv("HOME", str(tmp_path / "host"))
    monkeypatch.setenv("CODEX_HOME", "/ambient-codex")
    monkeypatch.setenv("GROK_HOME", "/ambient-grok")
    executable = tmp_path / "inert-binary"
    executable.touch()

    async def scenario():
        for name in ("personal", "work"):
            home = tmp_path / name
            home.mkdir()
            profile = HarnessProfile(
                name=name,
                kind=kind,
                executable=str(executable),
                home_directory=str(home),
            )
            if kind == HarnessKind.CODEX_APP_SERVER:
                await CodexAppServerAdapter()._connect(
                    profile, CredentialStore(), (), tmp_path
                )
            else:
                await GrokAcpAdapter()._connect(profile, tmp_path)
        default = HarnessProfile(name="default", kind=kind, executable=str(executable))
        assert key not in _harness_environment(default)

    asyncio.run(scenario())
    assert [kwargs["env"][key] for _, kwargs in observed] == [
        str(tmp_path / "personal"),
        str(tmp_path / "work"),
    ]
    for argv, kwargs in observed:
        assert kwargs["env"]["HOME"] == str(tmp_path / "host")
        assert kwargs["cwd"] == str(tmp_path)
        assert ("GROK_HOME" if key == "CODEX_HOME" else "CODEX_HOME") not in kwargs[
            "env"
        ]
        if kind == HarnessKind.CODEX_APP_SERVER:
            assert 'cli_auth_credentials_store="file"' in argv


def test_missing_account_home_fails_before_launch(tmp_path):
    from nebula.v3.harnesses import _harness_environment

    profile = HarnessProfile(
        name="missing",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable="/bin/true",
        home_directory=str(tmp_path / "missing"),
    )
    with pytest.raises(HarnessConfigurationError, match="Choose or create"):
        _harness_environment(profile)


def test_grok_catalog_uses_selected_account_for_command_and_cache(
    tmp_path, monkeypatch
):
    from nebula.v3.harnesses import _grok_model_catalog

    observed = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"* grok-build\n", b""

    async def create_process(*argv, **kwargs):
        observed.append(kwargs["env"]["GROK_HOME"])
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    async def scenario():
        for account in ("personal", "work"):
            home = tmp_path / account
            home.mkdir()
            (home / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": {
                            "grok-build": {
                                "info": {
                                    "reasoning_efforts": [{"id": account}],
                                    "reasoning_effort": account,
                                }
                            }
                        }
                    }
                )
            )
            profile = HarnessProfile(
                name=account,
                kind=HarnessKind.GROK_ACP,
                executable="/bin/true",
                home_directory=str(home),
            )
            models, options = await _grok_model_catalog(profile)
            assert models == ["grok-build"]
            assert options[0].default_reasoning_effort == account

    asyncio.run(scenario())
    assert observed == [str(tmp_path / name) for name in ("personal", "work")]


def test_grok_connection_stays_usable_after_a_rejected_mode():
    class RejectingModeRpc(FixtureGrokRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method == "session/set_mode":
                self.calls.append((method, params))
                raise HarnessProviderError(
                    "Grok ACP", {"code": -32602, "message": "unknown mode"}
                )
            return await super().request(method, params)

    async def scenario() -> None:
        rpc = RejectingModeRpc()

        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = GrokAcpConnection(
            rpc, external_session_id="grok-session", permission_handler=no_permission
        )

        with pytest.raises(HarnessProviderError):
            async for _ in connection.run_turn(
                "first", model="grok-build", mode="unknown"
            ):
                pass
        assert connection.active is False

        # The next turn on the same transport must not be refused as "active".
        events = [
            event async for event in connection.run_turn("second", model="grok-build")
        ]
        assert events[-1].type == "completed"
        assert events[-1].message == "done"
        assert [method for method, _ in rpc.calls].count("session/prompt") == 1

    asyncio.run(scenario())


def test_claude_connection_clears_active_when_the_query_is_rejected(tmp_path):
    class RejectingClient(FakeClaudeClient):
        async def query(self, prompt: str) -> None:
            raise RuntimeError("Claude stdin closed")

    async def scenario() -> None:
        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        connection = ClaudeAgentSdkConnection(
            RejectingClient(options=FakeClaudeOptions()),
            permission_handler=no_permission,
            sdk=SimpleNamespace(),
            external_session_id="claude-session",
            workspace=tmp_path,
        )

        with pytest.raises(RuntimeError, match="stdin closed"):
            async for _ in connection.run_turn("first", model="claude-test"):
                pass

        assert connection.active is False

    asyncio.run(scenario())


def test_claude_open_is_bounded_by_the_startup_deadline(tmp_path, monkeypatch):
    class HangingClient(FakeClaudeClient):
        async def connect(self) -> None:
            await asyncio.Event().wait()

    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeClaudeOptions,
        ClaudeSDKClient=HangingClient,
        HookMatcher=FakeHookMatcher,
        PermissionResultAllow=PermissionResultAllow,
    )
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
    monkeypatch.setattr(harness_module, "HARNESS_STARTUP_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        async def no_permission(_request):
            raise AssertionError("permission was not expected")

        profile = HarnessProfile(
            id="claude-a",
            name="Claude",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model="claude-test",
        )
        session = HarnessSession(
            id="session-a",
            engagement_id="eng-a",
            harness_profile_id=profile.id,
            model="claude-test",
        )
        with pytest.raises(TimeoutError, match="Claude startup timed out"):
            await asyncio.wait_for(
                ClaudeAgentSdkAdapter().open(
                    AdapterOpenRequest(
                        profile=profile,
                        session=session,
                        workspace=tmp_path,
                        mcp_profiles=(),
                        credential_store=CredentialStore(),
                        permission_handler=no_permission,
                    )
                ),
                timeout=2,
            )

    asyncio.run(scenario())


def test_codex_session_listing_is_bounded_when_the_app_server_stalls(
    tmp_path, monkeypatch
):
    class StallingRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            if method == "thread/list":
                self.calls.append((method, params))
                await asyncio.Event().wait()
            return await super().request(method, params)

    monkeypatch.setattr(harness_module, "CODEX_SESSION_LIST_TIMEOUT_SECONDS", 0.05)
    rpc = StallingRpc()
    profile = HarnessProfile(
        name="Codex", kind=HarnessKind.CODEX_APP_SERVER, executable="/bin/true"
    )

    async def scenario() -> None:
        with pytest.raises(HarnessUnavailableError, match="Codex"):
            await asyncio.wait_for(
                FixtureCodexAdapter(rpc).list_external_sessions(
                    profile, CredentialStore(), tmp_path
                ),
                timeout=2,
            )
        assert rpc.closed is True

    asyncio.run(scenario())


def test_grok_session_listing_timeout_kills_the_child_process(tmp_path, monkeypatch):
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(harness_module, "GROK_SESSION_LIST_TIMEOUT_SECONDS", 0.1)
    created: list[Any] = []
    original = asyncio.create_subprocess_exec

    async def observed_create(*argv: str, **kwargs: Any) -> Any:
        process = await original(*argv, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_create)
    profile = HarnessProfile(
        id="grok-a",
        name="Grok",
        kind=HarnessKind.GROK_ACP,
        executable=str(executable),
    )

    async def scenario() -> None:
        with pytest.raises(HarnessUnavailableError):
            await GrokAcpAdapter().list_external_sessions(
                profile, CredentialStore(), tmp_path
            )
        assert len(created) == 1
        # The stalled `grok sessions list` child must not outlive the request.
        assert created[0].returncode is not None

    asyncio.run(scenario())


def test_codex_rpc_reader_failure_ends_the_transport():
    class FailingStdout:
        async def read(self, _size: int = -1) -> bytes:
            raise OSError("stdout read failed")

    class Stdin:
        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = FailingStdout()
            self.stderr = None
            self.stdin = Stdin()
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int | None:
            return self.returncode

    async def scenario() -> None:
        process = Process()
        rpc = _CodexRpc(process=process)
        pending = asyncio.get_running_loop().create_future()
        rpc._pending[1] = pending

        await rpc._reader()

        with pytest.raises(HarnessTransportError):
            await pending
        failure = await rpc.events.get()
        assert isinstance(failure, HarnessTransportError)
        # A live process with no reader would let every later request wait
        # forever, so the transport ends with it.
        assert process.killed is True
        assert rpc.connection_state == "disconnected"
        with pytest.raises(HarnessTransportError):
            await asyncio.wait_for(rpc.request("thread/list", {}), timeout=1)

    asyncio.run(scenario())
