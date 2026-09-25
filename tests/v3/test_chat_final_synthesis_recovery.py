"""The tool-free final synthesis ends a tool turn with an answer, not a failure.

A model that still reaches for a tool while it answers, or whose attempt at a
call arrives malformed or refused upstream, is a normal event at that point in
a turn: the request allows no call, so every call it makes is one it was not
allowed to make. These tests pin what the turn does instead of failing.
"""

import asyncio
import json

import httpx
import pytest

from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import (
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ProviderResponseError,
    StreamEventType,
    ToolCall,
    ToolChoice,
)
from tests.v3.test_chat_tool_loop import (
    RecordingBroker,
    ScriptedProvider,
    _prepared,
    _response,
)

ANSWER = "The stored value is a."


def _call(call_id: str, value: str) -> ToolCall:
    return ToolCall(id=call_id, name="safe_read", arguments={"value": value})


def _finish() -> ToolCall:
    return ToolCall(id="finish-1", name="finish_response", arguments={})


def _stream(service, prepared):
    async def scenario():
        return [event async for event in service.stream(prepared)]

    return asyncio.run(scenario())


def _visible(events) -> str:
    return "".join(payload["delta"] for name, payload in events if name == "delta")


def _turn_requests(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


# --- The OpenAI-compatible adapter behind a scripted wire -------------------


def _completion(message: dict) -> dict:
    return {
        "id": "gen-1",
        "model": "model-a",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _wire_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


_ROUTE_READ = _completion(
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [_wire_call("call-1", "safe_read", '{"value":"a"}')],
    }
)
_ROUTE_FINISH = _completion(
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [_wire_call("finish-1", "finish_response", "{}")],
    }
)


def _sse(chunks: list[dict]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    choice: dict = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"id": "gen-s", "model": "model-a", "choices": [choice]}


def _text_stream(text: str) -> httpx.Response:
    return _sse([_chunk({"content": text}), _chunk({}, "stop")])


_REFUSAL = (
    "Upstream error from Sail Research: model emitted an undeclared or "
    "disallowed function name"
)


def _refusal_frame(code: int | None) -> dict:
    error: dict = {"message": _REFUSAL}
    if code is not None:
        error["code"] = code
    return {
        "id": "gen-s",
        "error": error,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
    }


def _wire_turn(tmp_path, script: list):
    """A tool turn whose provider is the real OpenRouter adapter."""

    seen: list[dict] = []
    remaining = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        item = remaining.pop(0)
        return (
            item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
        )

    provider = OpenAICompatibleProvider(
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
        transport=httpx.MockTransport(handler),
    )
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = provider
    return store, service, prepared, broker, seen


def test_final_synthesis_recovers_from_a_nameless_tool_call_fragment(tmp_path):
    """Live DeepSeek via OpenRouter: a nameless call with two argument bytes."""

    fragment = _sse(
        [
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"x'}}]}),
            _chunk({}, "tool_calls"),
        ]
    )
    store, service, prepared, broker, seen = _wire_turn(
        tmp_path,
        [_ROUTE_READ, _ROUTE_FINISH, fragment, _text_stream("Recovered answer.")],
    )

    events = _stream(service, prepared)

    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == "Recovered answer."
    assert _visible(events) == "Recovered answer."
    assert [call.tool_name for call in broker.calls] == ["safe_read"]

    # The synthesis requests declared the routing tools with calling off; the
    # second one is the recovery. Routing offers no finish tool since #521.
    def declared(payload: dict) -> list[str]:
        return [tool["function"]["name"] for tool in payload.get("tools", [])]

    assert declared(seen[0]) == ["safe_read"]
    assert [
        (declared(payload), payload.get("tool_choice")) for payload in seen[2:]
    ] == [
        (declared(seen[0]), "none"),
        (declared(seen[0]), "none"),
    ]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.request_snapshot["final_answer_recovery"]["reason"] == "tool_call"
    assert turn.request_snapshot["final_answer_recovery"]["attempts"] == 1


@pytest.mark.parametrize(
    "lead,code",
    [
        ([], 400),
        ([], None),
        # The refusal arrived with a transient status after thinking began.
        ([_chunk({"reasoning": "Let me call the tool again."})], 502),
    ],
)
def test_final_synthesis_recovers_when_the_upstream_refuses_a_function_call(
    tmp_path, lead, code
):
    """Live Sail Research via OpenRouter: every function is undeclared here."""

    refusal = _sse([*lead, _refusal_frame(code)])
    store, service, prepared, broker, seen = _wire_turn(
        tmp_path,
        [_ROUTE_READ, _ROUTE_FINISH, refusal, _text_stream("Recovered answer.")],
    )

    events = _stream(service, prepared)

    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == "Recovered answer."
    assert len(seen) == 4
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.request_snapshot["final_answer_recovery"]["reason"] == "tool_call"


def test_answer_text_before_a_refused_call_completes_the_turn_when_recovery_is_spent(
    tmp_path,
):
    """An answer the model wrote before its refused call beats a failed turn."""

    def answer_then_refusal() -> httpx.Response:
        return _sse([_chunk({"content": ANSWER}), _refusal_frame(400)])

    store, service, prepared, broker, seen = _wire_turn(
        tmp_path,
        [_ROUTE_READ, _ROUTE_FINISH, answer_then_refusal(), answer_then_refusal()],
    )

    events = _stream(service, prepared)

    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == ANSWER
    assert _visible(events) == ANSWER
    assert len(seen) == 4
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.request_snapshot["final_answer_recovery"]["attempts"] == 2
    assert turn.request_snapshot["final_answer_recovery"]["reason"] == "tool_call"


def test_final_synthesis_recovers_from_a_rejected_tool_call_event(tmp_path):
    """Any adapter that reports a rejected call gets the same recovery."""

    class RejectingSynthesisProvider(ScriptedProvider):
        async def stream(self, request: ModelRequest):
            if request.tool_choice != ToolChoice.NONE or request.metadata.get(
                "final_answer_recovery"
            ):
                async for event in super().stream(request):
                    yield event
                return
            self.requests.append(request)
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(
                type=StreamEventType.ERROR,
                error="provider returned malformed tool arguments",
                tool_call_rejected=True,
            )

    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            _response(text="Recovered answer."),
        ],
        broker,
    )
    provider = RejectingSynthesisProvider(prepared.provider.responses)
    prepared.provider = provider

    events = _stream(service, prepared)

    assert _visible(events) == "Recovered answer."
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.request_snapshot["final_answer_recovery"]["reason"] == "tool_call"
    assert _turn_requests(provider)[-1].metadata["final_answer_recovery"] == (
        "tool_call"
    )


# --- What the synthesis returned --------------------------------------------


@pytest.mark.parametrize(
    "synthesis",
    [
        _response(text=ANSWER, calls=[_call("call-9", "b")]),
        # A readable DSML frame in Core's first-known spelling.
        _response(
            text=f"{ANSWER}\n\n"
            '<｜DSML｜ calls> <｜DSML｜ invoke name="safe_read">'
            '<｜DSML｜ parameter name="value">b</｜DSML｜ parameter>'
            "</｜DSML｜ invoke> </｜DSML｜ calls>"
        ),
        # DeepSeek V3.2's published spelling.
        _response(
            text=f"{ANSWER}\n\n<｜DSML｜function_calls>\n"
            '<｜DSML｜invoke name="safe_read">\n'
            '<｜DSML｜parameter name="value" string="true">b</｜DSML｜parameter>\n'
            "</｜DSML｜invoke>\n</｜DSML｜function_calls>"
        ),
    ],
    ids=["structured-call", "dsml-calls", "dsml-function-calls"],
)
def test_final_synthesis_keeps_answer_text_beside_a_tool_call(tmp_path, synthesis):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            synthesis,
        ],
        broker,
    )

    events = _stream(service, prepared)

    assert _visible(events) == ANSWER
    assert "DSML" not in json.dumps(events, ensure_ascii=False)
    # The answer was already there: no recovery request, no second tool run.
    assert len(_turn_requests(provider)) == 3
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert "final_answer_recovery" not in turn.request_snapshot


@pytest.mark.parametrize(
    "frame",
    [
        '<｜DSML｜ calls> <｜DSML｜ invoke name="tool_output_read">'
        "the artifact I mentioned</｜DSML｜ invoke> </｜DSML｜ calls>",
        '<｜DSML｜tool_call><｜DSML｜invoke name="safe_read">',
    ],
    ids=["unreadable", "unknown-spelling"],
)
def test_final_synthesis_strips_a_trailing_unparsed_frame(tmp_path, frame):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            _response(text=f"{ANSWER}\n\n{frame}"),
        ],
        broker,
    )

    events = _stream(service, prepared)

    assert _visible(events) == ANSWER
    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == ANSWER
    assert "DSML" not in json.dumps(events, ensure_ascii=False)
    assert len(_turn_requests(provider)) == 3
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_final_synthesis_tool_call_with_budget_routes_again(tmp_path):
    """A model that still wants data gets its tool while the turn can afford it."""

    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            # Synthesis: only a call, with 4 of 5 execution calls left.
            _response(calls=[_call("call-2", "b")]),
            # Routing again, with the tools offered.
            _response(calls=[_call("call-3", "b")]),
            _response(calls=[_finish()]),
            _response(text="The stored values are a and b."),
        ],
        broker,
    )

    events = _stream(service, prepared)

    assert _visible(events) == "The stored values are a and b."
    assert [call.arguments for call in broker.calls] == [{"value": "a"}, {"value": "b"}]
    requests = _turn_requests(provider)
    # Calls allowed: routing, routing, synthesis, routing again, synthesis.
    assert [request.tool_choice != ToolChoice.NONE for request in requests] == [
        True,
        True,
        False,
        True,
        True,
        False,
    ]
    assert not any(
        request.metadata.get("final_answer_recovery") for request in requests
    )
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.execution_tool_calls == 2
    assert turn.request_snapshot["final_answer_rerouted"] is True


def test_final_synthesis_routes_again_only_once_per_turn(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            _response(calls=[_call("call-2", "b")]),
            _response(calls=[_finish()]),
            # A second tool-only synthesis is re-asked, not routed again.
            _response(calls=[_call("call-3", "b")]),
            _response(text=ANSWER),
        ],
        broker,
    )

    events = _stream(service, prepared)

    assert _visible(events) == ANSWER
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    requests = _turn_requests(provider)
    assert [request.tool_choice != ToolChoice.NONE for request in requests] == [
        True,
        True,
        False,
        True,
        False,
        False,
    ]
    assert requests[-1].metadata["final_answer_recovery"] == "tool_call"
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_final_synthesis_tool_call_without_budget_is_re_asked(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            # The budget is spent, so the turn goes straight to synthesis.
            _response(calls=[_call("call-2", "b")]),
            _response(text=ANSWER),
        ],
        broker,
        max_tool_calls=1,
    )

    events = _stream(service, prepared)

    assert _visible(events) == ANSWER
    assert len(broker.calls) == 1
    requests = _turn_requests(provider)
    assert [request.tool_choice != ToolChoice.NONE for request in requests] == [
        True,
        False,
        False,
    ]
    assert requests[-1].metadata["final_answer_recovery"] == "tool_call"
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert "final_answer_rerouted" not in turn.request_snapshot


def test_reasoning_only_synthesis_fails_saying_the_model_never_answered(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            _response(reasoning="The value is a, so the answer is a."),
            _response(reasoning="The answer: a."),
        ],
        broker,
    )

    with pytest.raises(ProviderResponseError, match="only reasoning"):
        _stream(service, prepared)

    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert "only reasoning" in (failed.error or "")
