import asyncio

import pytest

from nebula.v3.harness_commands import parse_harness_command, usage_reply
from nebula.v3.harnesses import (
    CodexAppServerConnection,
    GrokAcpConnection,
    HarnessEvent,
    _harness_goal_snapshot,
    _run_harness_command,
)


class Rpc:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.events = asyncio.Queue()

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "session/prompt":
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Goal status: active"},
                        }
                    },
                }
            )
            return {"stopReason": "end_turn"}
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    "text,expected",
    [
        (" /goal build a clock ", ("/goal", "build a clock")),
        ("/goals", ("/goals", "")),
        ("/usage", ("/usage", "")),
        ("Explain /goal", None),
        ("/goalkeeper", ("/goalkeeper", "")),
        ("/yolo", ("/yolo", "")),
    ],
)
def test_command_parser_preserves_slash_syntax_for_catalog_validation(text, expected):
    assert parse_harness_command(text) == expected


@pytest.mark.parametrize("grok", [True, False])
def test_usage_calls_selected_session_without_inference_or_double_accounting(grok):
    async def scenario():
        rpc = Rpc(
            {"usage": {"inputTokens": 12, "outputTokens": 3}}
            if grok
            else {
                "threadUsage": {
                    "groups": [
                        {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}
                    ]
                }
            }
        )
        cls = GrokAcpConnection if grok else CodexAppServerConnection
        connection = cls(
            rpc, external_session_id="selected-session", permission_handler=None
        )
        events = [
            event
            async for event in _run_harness_command(
                connection,
                ("/usage", ""),
                model="model",
                context_prompt="wrapped /usage",
            )
        ]
        assert rpc.calls == [
            (
                "_x.ai/session/usage" if grok else "account/usage/read",
                {"sessionId" if grok else "threadId": "selected-session"},
            )
        ]
        assert "Input tokens: 12" in events[-1].message
        assert all(event.type != "usage" for event in events)

    asyncio.run(scenario())


def test_grok_goal_prefix_precedes_but_preserves_core_context():
    async def scenario():
        rpc = Rpc({})
        connection = GrokAcpConnection(
            rpc,
            external_session_id="grok",
            permission_handler=None,
            developer_instructions="trusted capabilities",
        )
        events = [
            event
            async for event in _run_harness_command(
                connection,
                ("/goal", "status"),
                model="model",
                context_prompt="Core capability state and operator request",
            )
        ]
        blocks = rpc.calls[0][1]["prompt"]
        assert blocks[0] == {"type": "text", "text": "/goal status"}
        assert "trusted capabilities" in blocks[1]["text"]
        assert "Core capability state" in blocks[1]["text"]
        assert events[-1].message == "Goal status: active"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "argument,method,extra",
    [
        ("status", "thread/goal/get", {}),
        ("pause", "thread/goal/set", {"status": "paused"}),
        ("clear", "thread/goal/clear", {}),
        (
            "build a clock",
            "thread/goal/set",
            {"objective": "build a clock", "status": "active"},
        ),
        ("resume", "thread/goal/set", {"status": "active"}),
    ],
)
def test_codex_goal_dispatch_and_continuation(argument, method, extra):
    async def scenario():
        rpc = Rpc(
            {
                "goal": {
                    "objective": "build a clock",
                    "status": "paused" if argument == "pause" else "active",
                }
            }
        )
        connection = CodexAppServerConnection(
            rpc, external_session_id="codex", permission_handler=None
        )
        prompts = []

        async def run_turn(prompt, **kwargs):
            assert kwargs["mode"] == "plan"
            prompts.append(prompt)
            yield HarnessEvent(type="completed", message="Working on goal")

        connection.run_turn = run_turn
        events = [
            event
            async for event in _run_harness_command(
                connection,
                ("/goal", argument),
                model="model",
                context_prompt="trusted context",
                mode="plan",
            )
        ]
        assert rpc.calls == [(method, {"threadId": "codex", **extra})]
        assert prompts == (
            ["trusted context"] if argument in {"build a clock", "resume"} else []
        )
        assert events[-1].type == "completed"

    asyncio.run(scenario())


def test_unsupported_native_command_fails_without_model_fallback():
    async def scenario():
        rpc = Rpc(RuntimeError("Method not found"))
        connection = CodexAppServerConnection(
            rpc, external_session_id="codex", permission_handler=None
        )
        with pytest.raises(RuntimeError, match="Method not found"):
            _ = [
                event
                async for event in _run_harness_command(
                    connection, ("/usage", ""), model="model", context_prompt="context"
                )
            ]
        assert len(rpc.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,normalized",
    [
        ("active", "running"),
        ("paused", "paused"),
        ("usageLimited", "usage_limited"),
        ("budgetLimited", "budget_limited"),
    ],
)
def test_current_codex_goal_states_and_elapsed_time(status, normalized):
    goal = _harness_goal_snapshot(
        {"objective": "clock", "status": status, "timeUsedSeconds": 4}
    )
    assert goal.status == normalized
    assert goal.elapsed_ms == 4_000


def test_missing_usage_is_not_presented_as_zero_spend():
    assert "no usage estimate" in usage_reply({"threadUsage": None}, grok=False)
    with pytest.raises(ValueError):
        usage_reply({}, grok=True)


@pytest.mark.parametrize(
    "method", ["_x.ai/session_notification", "_x.ai/session/update"]
)
def test_grok_native_goal_notifications_are_not_lost(method):
    class GoalRpc(Rpc):
        async def request(self, request_method, params):
            if request_method == "session/prompt":
                await self.events.put(
                    {
                        "method": method,
                        "params": {
                            "update": {
                                "sessionUpdate": "goal_updated",
                                "objective": "Clock",
                                "status": "user_paused",
                                "elapsedMs": 1200,
                            }
                        },
                    }
                )
            return await super().request(request_method, params)

    async def scenario():
        connection = GrokAcpConnection(
            GoalRpc({}), external_session_id="grok", permission_handler=None
        )
        events = [
            event async for event in connection.run_turn("status", model="fixture")
        ]
        goal = next(event.goal for event in events if event.item_kind == "goal")
        assert goal.status == "paused"
        assert goal.elapsed_ms == 1200

    asyncio.run(scenario())


def test_acp_catalog_survives_idle_replay_drain_and_replaces_per_session():
    import json
    from nebula.v3.harnesses import (
        _AcpRpc,
        _discard_queued_session_replay,
        _connection_commands,
    )

    async def scenario():
        rpc = _AcpRpc()

        async def advertise(session, commands):
            await rpc._dispatch(
                json.dumps(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": session,
                            "update": {
                                "sessionUpdate": "available_commands_update",
                                "availableCommands": commands,
                            },
                        },
                    }
                )
            )

        await advertise(
            "a",
            [
                {
                    "name": "vendor-check",
                    "description": "Check project",
                    "input": {"hint": "target"},
                }
            ],
        )
        await advertise("b", [{"name": "other", "description": "Other session"}])
        await rpc.events.put({"method": "session/update", "params": {}})
        _discard_queued_session_replay(rpc.events)
        connection = GrokAcpConnection(
            rpc, external_session_id="a", permission_handler=None
        )
        catalog = _connection_commands(connection)
        assert catalog[-1] == {
            "name": "vendor-check",
            "description": "Check project",
            "hint": "target",
            "source": "native",
        }
        assert not any(item["name"] == "other" for item in catalog)
        await advertise("a", [{"name": "replacement", "description": "New"}])
        assert [item["name"] for item in _connection_commands(connection)][
            -1
        ] == "replacement"
        assert not any(
            item["name"] == "vendor-check" for item in _connection_commands(connection)
        )
        await advertise("a", [])
        assert len(_connection_commands(connection)) == 4
        assert rpc.events.empty()

    asyncio.run(scenario())


def test_catalog_validation_bounds_metadata_and_preserves_bridge_semantics():
    from nebula.v3.harness_commands import normalize_commands
    from nebula.v3.harnesses import _connection_commands

    catalog = normalize_commands(
        [
            {"name": "../bad", "description": "bad"},
            {"name": "two words", "description": "bad"},
            {"name": "valid", "description": "x" * 2000, "input": {"hint": "y" * 900}},
            {"name": "usage", "description": "Vendor usage"},
            {"name": "bad", "description": None},
        ]
    )
    assert len(catalog) == 2
    assert len(catalog[0]["description"]) == 1000
    assert len(catalog[0]["hint"]) == 500
    rpc = Rpc({})
    rpc.command_catalogs = {"a": catalog}
    connection = GrokAcpConnection(
        rpc, external_session_id="a", permission_handler=None
    )
    assert (
        next(
            item for item in _connection_commands(connection) if item["name"] == "usage"
        )["source"]
        == "nebula"
    )


def test_discovered_command_dispatch_and_help_share_catalog_without_new_handler():
    async def scenario():
        rpc = Rpc({})
        rpc.command_catalogs = {
            "a": [
                {
                    "name": "vendor-check",
                    "description": "Check project",
                    "hint": "target",
                    "source": "native",
                }
            ]
        }
        connection = GrokAcpConnection(
            rpc, external_session_id="a", permission_handler=None
        )
        events = [
            event
            async for event in _run_harness_command(
                connection, ("/help", ""), model="fixture", context_prompt="context"
            )
        ]
        assert "/vendor-check target" in events[-1].message
        assert not rpc.calls
        events = [
            event
            async for event in _run_harness_command(
                connection,
                ("/vendor-check", "a  b"),
                model="fixture",
                context_prompt="trusted context",
            )
        ]
        assert rpc.calls[0] == (
            "session/prompt",
            {
                "sessionId": "a",
                "prompt": [
                    {"type": "text", "text": "/vendor-check a  b"},
                    {"type": "text", "text": "trusted context"},
                ],
            },
        )
        rpc.command_catalogs["a"] = []
        with pytest.raises(Exception, match="not available.*Use /help"):
            _ = [
                event
                async for event in _run_harness_command(
                    connection,
                    ("/vendor-check", ""),
                    model="fixture",
                    context_prompt="context",
                )
            ]
        assert len(rpc.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("grok", [True, False])
def test_unknown_command_is_rejected_without_inference(grok):
    from nebula.v3.harness_commands import parse_harness_command
    from nebula.v3.harnesses import HarnessConfigurationError

    async def scenario():
        rpc = Rpc({})
        connection = (GrokAcpConnection if grok else CodexAppServerConnection)(
            rpc, external_session_id="a", permission_handler=None
        )
        with pytest.raises(HarnessConfigurationError, match="Use /help"):
            _ = [
                event
                async for event in _run_harness_command(
                    connection,
                    parse_harness_command("/unknown argument"),
                    model="fixture",
                    context_prompt="context",
                )
            ]
        assert rpc.calls == []
        assert parse_harness_command("/home/operator/project") is None
        assert parse_harness_command("Explain /unknown") is None

    asyncio.run(scenario())


def test_discovered_command_without_text_has_an_explicit_durable_reply():
    async def scenario():
        rpc = Rpc({})
        rpc.command_catalogs = {
            "a": [
                {
                    "name": "quiet",
                    "description": "Quiet command",
                    "hint": "",
                    "source": "native",
                }
            ]
        }
        connection = GrokAcpConnection(
            rpc, external_session_id="a", permission_handler=None
        )

        async def run_turn(*args, **kwargs):
            yield HarnessEvent(type="completed", message="")

        connection.run_turn = run_turn
        events = [
            event
            async for event in _run_harness_command(
                connection, ("/quiet", ""), model="fixture", context_prompt="context"
            )
        ]
        assert events[-2].type == "message_delta"
        assert "returned no text for /quiet" in events[-1].message
        assert not rpc.calls

    asyncio.run(scenario())


def test_help_keeps_all_names_within_durable_message_limit():
    from nebula.v3.harness_commands import normalize_commands

    async def scenario():
        rpc = Rpc({})
        rpc.command_catalogs = {
            "a": normalize_commands(
                [
                    {
                        "name": "x" * 120 + str(i),
                        "description": "d" * 1000,
                        "input": {"hint": "h" * 500},
                    }
                    for i in range(256)
                ]
            )
        }
        connection = GrokAcpConnection(
            rpc, external_session_id="a", permission_handler=None
        )
        events = [
            event
            async for event in _run_harness_command(
                connection, ("/help", ""), model="fixture", context_prompt="context"
            )
        ]
        assert all(
            "/" + item["name"] in events[-1].message
            for item in rpc.command_catalogs["a"]
        )

    asyncio.run(scenario())
