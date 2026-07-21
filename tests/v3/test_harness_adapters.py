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
    HarnessConfigurationError,
    HarnessPermissionDecision,
    PermissionTicket,
    _CodexRpc,
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
        if method == "model/list":
            return {
                "data": [
                    {"id": "gpt-5.4", "model": "gpt-5.4", "isDefault": True},
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
        assert health.capabilities.models == ["gpt-5.4", "gpt-5.3-codex"]
        assert [method for method, _ in rpc.calls] == ["initialize", "model/list"]
        assert rpc.calls[0][1]["capabilities"] == {"requestAttestation": False}
        assert rpc.closed is True

    asyncio.run(scenario())


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
                }
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
            )
        )
        events = [
            event async for event in connection.run_turn("inspect", model="gpt-test")
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
        _validate("v2/TurnStartParams.json", rpc.calls[2][1])
        assert rpc.calls[2][1]["summary"] == "auto"
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
        assert decisions[0].category == "command"
        assert rpc.responses == [(41, {"decision": "accept"})]
        instructions = rpc.calls[1][1]["developerInstructions"]
        assert "unrestricted vendor workspace agent" in instructions
        assert "BEGIN TRUSTED VENDOR-NATIVE CAPABILITIES (JSON)\n[]" in instructions
        assert '"name":"run_command"' in instructions
        _validate("CommandExecutionRequestApprovalResponse.json", rpc.responses[0][1])

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
        assert events[-1].message == "The authoritative final answer."
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
        assert trusted_events[-1].message == "The authoritative final answer."
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

        assert rpc.calls[0][1]["summary"] == "auto"
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


def test_codex_reasoning_summary_preserves_bounded_stream_without_snapshot():
    class StreamOnlyRpc(FixtureCodexRpc):
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method != "turn/start":
                return {}
            long_summary = "s" * 70_000
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
        streamed = next(event for event in events if event.type == "output_delta")
        completed = next(
            event
            for event in events
            if event.item_id == "reasoning-1" and event.item_status == "completed"
        )
        assert len(streamed.delta or "") == 64_000
        assert (streamed.delta or "").endswith("…[truncated]")
        assert completed.payload["reasoning_summary_state"] == "available"
        assert completed.payload["reasoning_summary_source"] == "stream"
        assert completed.payload["reasoning_summary_text"] == streamed.delta

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


def test_codex_gateway_thread_disables_vendor_execution_and_environment():
    config = _codex_thread_config({})

    assert config["features"]["shell_tool"] is False
    assert config["features"]["unified_exec"] is False
    assert config["features"]["plugins"] is False
    assert config["features"]["browser_use"] is False
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
    assert config["shell_environment_policy"]["inherit"] == "none"
    assert config["shell_environment_policy"]["set"]["PATH"] != "/nonexistent"


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
        FakeClaudeClient.latest = self

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

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


def test_claude_reasoning_text_is_discarded_and_future_messages_are_notices(tmp_path):
    class ReasoningClient:
        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
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


def test_claude_native_capabilities_use_an_explicit_toolset_and_pretool_gate(
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
            "Read",
            "Glob",
            "Grep",
            "Bash",
            "WebSearch",
            "WebFetch",
            "Skill",
            "Agent",
        }
        assert options["setting_sources"] == ["user"]
        assert options["skills"] == "all"
        assert {"Write", "Edit", "NotebookEdit"}.issubset(options["disallowed_tools"])
        assert "BEGIN TRUSTED VENDOR-NATIVE CAPABILITIES" in options["system_prompt"]

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
        assert read_result["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert skill_result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert write_result["hookSpecificOutput"]["permissionDecision"] == "deny"

        allowed = await options["can_use_tool"](
            "Read", {"file_path": "README.md"}, None
        )
        assert allowed.behavior == "allow"
        assert observed_permissions[-1].vendor_name == "Read"
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
