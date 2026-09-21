"""A malformed tool call goes back to the model instead of failing the turn.

Live evidence: deepseek/deepseek-v4.1-flash through OpenRouter emitted a tool
call Core could not read, and the whole turn failed with "provider returned
malformed tool arguments". Codex answers such a call with "failed to parse
function arguments", the AI SDK inserts a tool-error result for an invalid
call, and opencode routes it to an ``invalid`` tool: the model reads the error
and re-issues the call, while the valid calls beside it still run.

These tests drive the real OpenAI-compatible adapter over a scripted wire, so
the whole path is covered: the adapter returns the call it could not read with
``invalid_reason`` and chat answers it as a refused, non-charging step. Nothing
Core could not read ever reaches the broker.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.orchestration import SpecialistOutcome, call_records
from nebula.v3.providers import (
    ModelCapabilities,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
)
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared
from tests.v3.test_specialist_routing_tolerance import (
    DONE,
    FINISH,
)
from tests.v3.test_specialist_routing_tolerance import (
    RecordingBroker as SpecialistBroker,
)
from tests.v3.test_specialist_routing_tolerance import (
    _context,
    _specialist,
)

REISSUE = "re-issue the call with complete, valid JSON arguments"


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _routing(*calls: dict[str, Any], finish: str = "tool_calls") -> dict[str, Any]:
    return {
        "id": "gen-route",
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(calls),
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


FINISH_ROUTE = _routing(_call("fin-1", "finish_response", "{}"))


def _sse(*chunks: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _text(text: str) -> httpx.Response:
    return _sse(
        {
            "id": "gen-s",
            "model": "model-a",
            "choices": [{"index": 0, "delta": {"content": text}}],
        },
        {
            "id": "gen-s",
            "model": "model-a",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )


def _tool_stream(*deltas: dict[str, Any]) -> httpx.Response:
    return _sse(
        *(
            {
                "id": "gen-s",
                "model": "model-a",
                "choices": [{"index": 0, "delta": {"tool_calls": [delta]}}],
            }
            for delta in deltas
        ),
        {
            "id": "gen-s",
            "model": "model-a",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    )


class _Wire:
    """Answers each request with the next scripted body and keeps the payload."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        if not self.script:
            raise AssertionError("wire script exhausted")
        item = self.script.pop(0)
        return (
            item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
        )


def _turn(tmp_path, script: list[Any]):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    wire = _Wire(script)
    prepared.provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="model-a",
            model_allowlist=["model-a"],
            capabilities=ModelCapabilities(
                streaming=True, tools=True, strict_tools=True
            ),
            options={"retry_backoff_seconds": 0},
        ),
        transport=httpx.MockTransport(wire),
    )
    return store, service, prepared, broker, wire


def _tool_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [message for message in payload["messages"] if message["role"] == "tool"]


def test_unreadable_routing_call_is_answered_and_the_call_beside_it_runs(tmp_path):
    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(
                _call("call-1", "safe_read", '{"value":"a"}'),
                _call("call-2", "safe_read", '{"value": "b'),
            ),
            _routing(_call("call-3", "safe_read", '{"value":"b"}')),
            FINISH_ROUTE,
            _text("Read a and b."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a and b."
    # The unreadable call never reached the broker; the one beside it ran,
    # and so did the call the model re-issued.
    assert [call.arguments for call in broker.calls] == [{"value": "a"}, {"value": "b"}]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == [
        "complete",
        "failed",
        "complete",
    ]
    refused = turn.tool_history[1]
    assert refused["budget_class"] == "refused"
    assert refused["model_call_id"] == "call-2"
    assert refused["arguments"] == {}
    # Refusing the call spent none of the turn's budget.
    assert turn.execution_tool_calls == 2
    # The model reads why, in its own tool result, on the next routing step.
    [ran, answered] = _tool_messages(wire.payloads[1])
    assert ran["tool_call_id"] == "call-1"
    assert answered["tool_call_id"] == "call-2"
    detail = answered["content"]
    assert "arguments were not valid JSON: Unterminated string" in detail
    assert REISSUE in detail
    # The text the model sent is never replayed: the call carries no arguments.
    replayed = {
        call["id"]: call["function"]["arguments"]
        for message in wire.payloads[1]["messages"]
        for call in message.get("tool_calls") or []
    }
    assert replayed["call-2"] == "{}"


def test_an_unreadable_call_cut_off_by_the_output_limit_is_a_cut_off_call(tmp_path):
    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(_call("call-1", "safe_read", '{"value": "aaaa'), finish="length"),
            _routing(_call("call-2", "safe_read", '{"value":"aaaa"}')),
            FINISH_ROUTE,
            _text("Read aaaa."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read aaaa."
    assert [call.arguments for call in broker.calls] == [{"value": "aaaa"}]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    [answered] = _tool_messages(wire.payloads[1])
    # The output limit is the cause, so the model is told that, not that it
    # wrote bad JSON.
    assert "cut off by the output limit" in answered["content"]
    assert "not valid JSON" not in answered["content"]


def test_a_call_naming_no_tool_is_answered_with_the_offered_tools(tmp_path):
    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(_call("call-1", "Bash", '{"command":"id"}')),
            _routing(_call("call-2", "Safe_Read", '{"value":"a"}')),
            FINISH_ROUTE,
            _text("Read a."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a."
    # A case variant of a declared tool is that tool.
    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        ("safe_read", {"value": "a"})
    ]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    [answered] = _tool_messages(wire.payloads[1])
    assert "'Bash' is not the name of a tool" in answered["content"]
    assert "safe_read" in answered["content"]


def test_repeated_unreadable_calls_end_routing_through_synthesis(tmp_path):
    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(_call(f"call-{index}", "safe_read", '{"value": "a'))
            for index in range(3)
        ]
        + [_text("I could not read the value.")],
    )

    completion = asyncio.run(service.complete(prepared))

    # The #487 deviation cap bounds the loop: three refused routing responses
    # in a row, then the synthesis answers from what the turn has.
    assert completion.message.content == "I could not read the value."
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["failed"] * 3
    assert turn.execution_tool_calls == 0
    assert len(wire.payloads) == 4


def test_finish_response_with_unreadable_arguments_still_finishes(tmp_path):
    store, service, prepared, broker, _ = _turn(
        tmp_path,
        [
            _routing(_call("fin-1", "finish_response", '{"response": "Do')),
            _text("Done."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    # The finish tool takes no arguments, so unreadable ones carry nothing.
    assert completion.message.content == "Done."
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.tool_history == []


@pytest.mark.parametrize(
    "attempt",
    [
        # Live #2: a complete call, then the tail of its arguments again on a
        # new index with no id and no name.
        _tool_stream(
            {
                "index": 0,
                "id": "call_9",
                "type": "function",
                "function": {
                    "name": "lookup_asset",
                    "arguments": '{"address":"10.0.0.9"}',
                },
            },
            {"index": 1, "function": {"arguments": '"}'}},
        ),
        # A nameless fragment with nothing to join.
        _tool_stream({"index": 0, "function": {"arguments": '"x'}}),
        # A call whose arguments cannot be read.
        _tool_stream(
            {
                "index": 0,
                "id": "call_9",
                "type": "function",
                "function": {"name": "lookup_asset", "arguments": '{"address":'},
            }
        ),
    ],
    ids=["live-stray-tail", "nameless-fragment", "unreadable-arguments"],
)
def test_a_malformed_call_in_the_final_synthesis_is_recovered(tmp_path, attempt):
    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(_call("call-1", "safe_read", '{"value":"a"}')),
            FINISH_ROUTE,
            attempt,
            _text("Value is a."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    # The synthesis offered no tools, so the call is an attempted tool call:
    # the answer is asked for again instead of the turn failing.
    assert completion.message.content == "Value is a."
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    assert len(wire.payloads) == 4


def test_an_unreadable_call_after_the_synthesis_routes_again_is_answered(tmp_path):
    """The synthesis asks for a tool the turn can run, so the turn routes once
    more (#493); the call that routing step returns cannot be read."""

    store, service, prepared, broker, wire = _turn(
        tmp_path,
        [
            _routing(_call("call-1", "safe_read", '{"value":"a"}')),
            FINISH_ROUTE,
            _tool_stream(
                {
                    "index": 0,
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "safe_read", "arguments": '{"value":"b"}'},
                }
            ),
            _routing(_call("call-3", "safe_read", '{"value": "b')),
            _routing(_call("call-4", "safe_read", '{"value":"b"}')),
            _routing(_call("fin-2", "finish_response", "{}")),
            _text("Read a and b."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a and b."
    assert [call.arguments for call in broker.calls] == [
        {"value": "a"},
        {"value": "b"},
    ]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == [
        "complete",
        "failed",
        "complete",
    ]
    assert turn.tool_history[1]["model_call_id"] == "call-3"
    assert not wire.script


def test_a_specialist_never_runs_a_call_it_could_not_read(tmp_path):
    provider = _ScriptedSpecialistProvider(
        [
            [
                providers.ToolCall(
                    id="call-1", name="nmap.tcp", arguments={"ports": [80]}
                ),
                providers.ToolCall(
                    id="call-2",
                    name="browser.fetch",
                    arguments={},
                    invalid_reason=(
                        "arguments were not valid JSON: Unterminated string "
                        "starting at: line 1 column 9 (char 8)"
                    ),
                ),
            ],
            [providers.ToolCall(id="call-3", name=FINISH, arguments=DONE)],
        ]
    )
    broker = SpecialistBroker()
    specialist = _specialist(tmp_path, provider, broker)

    first = asyncio.run(specialist.run(_context()))
    second = asyncio.run(specialist.run(_context(prior_turns=[first])))

    assert [invocation.tool_name for invocation in broker.calls] == ["nmap.tcp"]
    assert first.tool_calls == 1
    records = call_records(first.output)
    assert [record["model_call_id"] for record in records] == ["call-1", "call-2"]
    assert records[1]["status"] == "failed"
    assert records[1]["routing_error"] == "invalid_call"
    detail = records[1]["provider_result"]["detail"]
    assert "arguments were not valid JSON" in detail
    assert REISSUE in detail
    replayed = {item.call_id: item for item in provider.requests[1].tool_results}
    assert replayed["call-2"].is_error is True
    assert second.outcome is SpecialistOutcome.COMPLETE


class _ScriptedSpecialistProvider(providers.ModelProvider):
    def __init__(self, batches) -> None:
        super().__init__(
            ProviderConfig(
                id="provider-invalid",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(),
            )
        )
        self.batches = list(batches)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return providers.ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            tool_calls=self.batches.pop(0),
            usage=providers.ModelUsage(
                input_tokens=10, output_tokens=5, total_tokens=15
            ),
            finish_reason="tool_calls",
        )

    async def health(self):
        return providers.ProviderHealth(provider_id=self.config.id, healthy=True)
