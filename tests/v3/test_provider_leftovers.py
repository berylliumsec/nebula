"""Provider adapter gaps left after the DeepSeek/GLM and native adapter work.

- Tool calls recovered from assistant text (DSML, GLM ``<tool_call>``,
  DeepSeek's special tokens) name the tool as the model saw it on the wire,
  so they decode back to the Nebula tool the way structured calls do.
- Stream errors say what kind of provider failure they are, so chat raises
  the typed error a ``complete()`` call would have raised.
- DeepSeek's ``/beta`` base URL takes no extra ``/v1``.
- Bedrock resends a request whose connection never opened.
- Qwen/Hermes ``<tool_call>{"name": ..., "arguments": ...}</tool_call>`` JSON
  is recovered or quarantined like the other native markups.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from nebula.v3 import tool_markup
from nebula.v3.chat import ChatError, _final_answer_problem, _StreamedAnswer
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderContextLengthError,
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    ProviderOverloadedError,
    ProviderQuotaError,
    ProviderRefusalError,
    StreamEventType,
    ToolCall,
    ToolDefinition,
)
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_native_adapter_outcomes import (
    CYBER_REFUSAL,
    KEY_ENV,
    _anthropic,
    _anthropic_call,
    _chat,
    _http_provider,
)
from tests.v3.test_native_adapter_requests import _Bedrock
from tests.v3.test_native_tool_markup import MODES, _calls, _pieces, _reply

QWEN = "qwen/qwen3-235b-a22b"


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")


def _config(kind: ProviderKind, flavor: ProviderFlavor, **extra: Any):
    return ProviderConfig(
        id="provider",
        kind=kind,
        flavor=flavor,
        base_url=extra.pop("base_url", "https://provider.invalid/v1"),
        default_model="model-a",
        api_key_env=None if kind == ProviderKind.BEDROCK else KEY_ENV,
        capabilities=ModelCapabilities(streaming=True, tools=True, strict_tools=True),
        # Zero backoff keeps retries under test without real waiting.
        options={"retry_backoff_seconds": 0, **extra.pop("options", {})},
        **extra,
    )


def _sse(*frames: Any) -> httpx.Response:
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": "model-a",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def _collect(events: Any) -> list[Any]:
    return [event async for event in events]


# --- Recovered tool calls decode their wire names ------------------------------

# A Nebula tool whose name is not a valid vendor function name: it goes on the
# wire as ``tool_output_search``, which is all the model ever sees.
DOTTED = ToolDefinition(
    name="tool_output.search",
    description="Search stored tool output",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
WIRE = "tool_output_search"
RECOVERED_MARKUP = pytest.mark.parametrize(
    "markup",
    [
        '<｜DSML｜function_calls>\n<｜DSML｜invoke name="tool_output_search">\n'
        '<｜DSML｜parameter name="query" string="true">open ports</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>\n</｜DSML｜function_calls>",
        "<tool_call>tool_output_search\n<arg_key>query</arg_key>\n"
        "<arg_value>open ports</arg_value>\n</tool_call>",
        "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>tool_output_search<｜tool▁sep｜>"
        '{"query": "open ports"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>',
    ],
    ids=["dsml", "glm", "deepseek"],
)


def _dotted_request() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="Search the scan output.")],
        tools=[DOTTED],
    )


def _compat_reply(text: str, *, streamed: bool):
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if streamed:
            return _sse(_chunk({"content": text}), _chunk({}, "stop"))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER),
        transport=httpx.MockTransport(handler),
    )
    if streamed:
        events = asyncio.run(_collect(provider.stream(_dotted_request())))
        assert events[-1].type == StreamEventType.COMPLETED, events[-1]
        response = events[-1].response
    else:
        response = asyncio.run(provider.complete(_dotted_request()))
    return response, sent


@RECOVERED_MARKUP
@MODES
def test_a_recovered_call_names_the_nebula_tool_not_its_wire_name(markup, streamed):
    response, sent = _compat_reply(markup, streamed=streamed)

    # The model was shown the wire name and wrote that one back.
    assert sent[0]["tools"][0]["function"]["name"] == WIRE
    assert _calls(response) == [("tool_output.search", {"query": "open ports"})]
    assert response.text == ""


def _native_reply(kind: str, text: str):
    """One reply carrying ``text`` from a native adapter, for ``_dotted_request``."""

    if kind == "anthropic":
        provider, _ = _http_provider(
            AnthropicProvider,
            ProviderKind.ANTHROPIC,
            _anthropic([{"type": "text", "text": text}], "end_turn"),
        )
    elif kind == "responses":
        provider, _ = _http_provider(
            OpenAIResponsesProvider,
            ProviderKind.OPENAI_RESPONSES,
            {
                "id": "resp_1",
                "model": "model-a",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    else:
        provider, _ = _http_provider(
            GeminiProvider,
            ProviderKind.GEMINI,
            {
                "responseId": "g_1",
                "candidates": [
                    {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
                ],
            },
        )
    return asyncio.run(provider.complete(_dotted_request()))


@pytest.mark.parametrize("kind", ["anthropic", "responses", "gemini"])
def test_every_native_adapter_decodes_a_recovered_call(kind):
    glm = (
        "<tool_call>tool_output_search\n<arg_key>query</arg_key>\n"
        "<arg_value>open ports</arg_value>\n</tool_call>"
    )

    response = _native_reply(kind, glm)

    assert _calls(response) == [("tool_output.search", {"query": "open ports"})]


def test_bedrock_decodes_a_recovered_call(monkeypatch):
    bedrock = _Bedrock(monkeypatch)
    glm = (
        "<tool_call>tool_output_search\n<arg_key>query</arg_key>\n"
        "<arg_value>open ports</arg_value>\n</tool_call>"
    )
    bedrock.converse = lambda **kwargs: {  # type: ignore[method-assign]
        "output": {"message": {"role": "assistant", "content": [{"text": glm}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    response = asyncio.run(provider.complete(_dotted_request()))

    assert _calls(response) == [("tool_output.search", {"query": "open ports"})]


def test_a_recovered_call_to_an_undeclared_tool_keeps_its_name():
    response, _ = _compat_reply("<tool_call>list_hosts\n</tool_call>", streamed=False)

    # Nothing to decode it to; chat answers it as an unavailable tool.
    assert _calls(response) == [("list_hosts", {})]


# --- Typed stream errors --------------------------------------------------------


def _compat_stream(*responses: httpx.Response) -> list[ModelStreamEvent]:
    queue = list(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(
            item.status_code, headers=item.headers, content=item.content
        )

    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER),
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(messages=[ModelMessage(role="user", content="hi")])
    return asyncio.run(_collect(provider.stream(request)))


STREAM_FAILURES = pytest.mark.parametrize(
    ("frame", "kind", "error_type"),
    [
        (_chunk({}, "content_filter"), "refusal", ProviderRefusalError),
        (
            {
                "error": {
                    "code": "context_length_exceeded",
                    "message": "This model's maximum context length is 8192 tokens.",
                }
            },
            "context_length",
            ProviderContextLengthError,
        ),
        (
            {
                "error": {
                    "code": 429,
                    "type": "insufficient_quota",
                    "message": "You exceeded your current quota.",
                }
            },
            "quota",
            ProviderQuotaError,
        ),
        (
            {"error": {"code": 503, "message": "upstream overloaded"}},
            "overloaded",
            ProviderOverloadedError,
        ),
    ],
    ids=["refusal", "context-length", "quota", "overloaded"],
)


@STREAM_FAILURES
def test_a_stream_error_event_carries_its_typed_failure(frame, kind, error_type):
    events = _compat_stream(_sse(frame))

    error = events[-1]
    assert error.type == StreamEventType.ERROR
    assert error.error_kind == kind
    failure = error.provider_error()
    assert type(failure) is error_type
    assert str(failure) == error.error


def test_a_streamed_refusal_keeps_its_reason():
    events = _compat_stream(_sse(_chunk({}, "sensitive")))

    failure = events[-1].provider_error()
    assert isinstance(failure, ProviderRefusalError)
    assert failure.reason == "content filter stopped the reply (sensitive)"


def test_an_unclassified_stream_error_names_no_kind():
    events = _compat_stream(
        _sse({"error": {"code": 400, "message": "unsupported parameter"}})
    )

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].error_kind is None
    assert events[-1].provider_error() is None


def test_the_non_streaming_fallback_types_its_error_events():
    provider, _ = _http_provider(
        AnthropicProvider, ProviderKind.ANTHROPIC, CYBER_REFUSAL
    )
    request = ModelRequest(messages=[ModelMessage(role="user", content="hi")])

    events = asyncio.run(_collect(provider.stream(request)))

    assert events[-1].error_kind == "refusal"
    assert isinstance(events[-1].provider_error(), ProviderRefusalError)


def _tool_free_stream(tmp_path, provider):
    store, service, prepared, _broker = _chat(tmp_path, provider)
    prepared.tools_enabled = False

    async def drain():
        return [item async for item in service.stream(prepared)]

    return store, lambda: asyncio.run(drain())


def test_a_streamed_answer_refusal_reaches_the_operator_as_a_refusal(tmp_path):
    provider, _ = _http_provider(
        AnthropicProvider, ProviderKind.ANTHROPIC, CYBER_REFUSAL
    )
    _store, drain = _tool_free_stream(tmp_path, provider)

    with pytest.raises(ProviderRefusalError) as failure:
        drain()

    assert failure.value.reason.startswith("refusal (category: cyber)")


def test_a_streamed_answer_on_spent_quota_is_a_quota_error(tmp_path):
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER),
        transport=httpx.MockTransport(
            lambda _request: _sse(
                {
                    "error": {
                        "code": 429,
                        "type": "insufficient_quota",
                        "message": "You exceeded your current quota.",
                    }
                }
            )
        ),
    )
    _store, drain = _tool_free_stream(tmp_path, provider)

    with pytest.raises(ProviderQuotaError, match="exceeded your current quota"):
        drain()


def test_an_unclassified_streamed_answer_failure_is_still_a_chat_error(tmp_path):
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER),
        transport=httpx.MockTransport(
            lambda _request: _sse(
                {"error": {"code": 400, "message": "unsupported parameter"}}
            )
        ),
    )
    _store, drain = _tool_free_stream(tmp_path, provider)

    with pytest.raises(ChatError, match="unsupported parameter"):
        drain()


QUOTA_SPENT = {
    "type": "error",
    "error": {
        "type": "insufficient_quota",
        "message": "Your credit balance is too low.",
    },
}


@pytest.mark.parametrize(
    ("status", "synthesis", "error_type", "wording"),
    [
        (200, CYBER_REFUSAL, ProviderRefusalError, "provider blocked the response"),
        (429, QUOTA_SPENT, ProviderQuotaError, "quota or billing limit reached"),
    ],
    ids=["refusal", "quota"],
)
def test_a_synthesis_stream_failure_is_raised_as_its_typed_error(
    tmp_path, status, synthesis, error_type, wording
):
    replies = [
        (200, _anthropic_call("toolu_1", "safe_read", {"value": "a"})),
        (200, _anthropic_call("toolu_2", "finish_response", {})),
        (status, synthesis),
    ]
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        code, body = replies[min(len(sent), len(replies)) - 1]
        return httpx.Response(code, json=body)

    provider = AnthropicProvider(
        _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC),
        transport=httpx.MockTransport(handler),
    )
    store, service, prepared, broker = _chat(tmp_path, provider)

    with pytest.raises(error_type, match=wording):
        asyncio.run(service.complete(prepared))

    # Neither is retried: the same request gets the same answer.
    assert len(sent) == 3
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert wording in failed.error


# --- DeepSeek's beta base URL ---------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/chat/completions", "/chat/completions"),
        ("/v1/models", "/models"),
    ],
)
def test_deepseek_beta_base_url_takes_no_extra_version(path, expected):
    provider = OpenAICompatibleProvider(
        _config(
            ProviderKind.OPENAI_COMPATIBLE,
            ProviderFlavor.DEEPSEEK,
            base_url="https://api.deepseek.com/beta",
        )
    )

    assert provider._path(path) == expected


def test_deepseek_beta_requests_reach_the_beta_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "3 hosts."},
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        _config(
            ProviderKind.OPENAI_COMPATIBLE,
            ProviderFlavor.DEEPSEEK,
            base_url="https://api.deepseek.com/beta",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="hi")])
        )
    )

    assert response.text == "3 hosts."
    assert seen == ["https://api.deepseek.com/beta/chat/completions"]


# --- Bedrock connections that never opened ----------------------------------------

UNOPENED = pytest.mark.parametrize(
    "failure",
    [
        EndpointConnectionError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
        ),
        ConnectTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
        ),
    ],
    ids=["endpoint-connection", "connect-timeout"],
)


@UNOPENED
def test_bedrock_resends_a_request_whose_connection_never_opened(monkeypatch, failure):
    request = ModelRequest(messages=[ModelMessage(role="user", content="hello")])
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    recovered = _Bedrock(monkeypatch, failure)
    assert asyncio.run(provider.complete(request)).text == "ok"
    assert len(recovered.calls) == 2

    exhausted = _Bedrock(monkeypatch, *[failure] * 3)
    with pytest.raises(ProviderOverloadedError, match=type(failure).__name__):
        asyncio.run(provider.complete(request))
    assert len(exhausted.calls) == 3


def test_bedrock_still_never_resends_after_a_read_timeout(monkeypatch):
    bedrock = _Bedrock(
        monkeypatch,
        ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
        ),
    )
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    with pytest.raises(ProviderError) as failure:
        asyncio.run(
            provider.complete(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        )

    # The request may have reached Bedrock; a whole generation is not resent.
    assert not isinstance(failure.value, ProviderOverloadedError)
    assert len(bedrock.calls) == 1


# --- Qwen/Hermes <tool_call> JSON --------------------------------------------------

# Qwen 2.5/3 and Hermes templates: one JSON object per tag, on its own line.
HERMES_NEWLINE = (
    '<tool_call>\n{"name": "safe_read", "arguments": {"value": "a"}}\n</tool_call>'
)
HERMES_COMPACT = '<tool_call>{"name":"safe_read","arguments":{"value":"a"}}</tool_call>'
# Hermes 2 Pro writes the arguments first.
HERMES_ARGUMENTS_FIRST = (
    '<tool_call>\n{"arguments": {"value": "a"}, "name": "safe_read"}\n</tool_call>'
)
# A template that renders history arguments as a string teaches the model to.
HERMES_STRING_ARGUMENTS = (
    '<tool_call>\n{"name": "safe_read", "arguments": "{\\"value\\": \\"a\\"}"}\n'
    "</tool_call>"
)
HERMES_CALLS = pytest.mark.parametrize(
    "markup",
    [HERMES_NEWLINE, HERMES_COMPACT, HERMES_ARGUMENTS_FIRST, HERMES_STRING_ARGUMENTS],
    ids=["newline", "compact", "arguments-first", "string-arguments"],
)


@HERMES_CALLS
@MODES
def test_hermes_tool_call_json_is_recovered_as_a_tool_call(markup, streamed):
    response, _ = _reply(QWEN, _pieces(markup), streamed=streamed)

    assert _calls(response) == [("safe_read", {"value": "a"})]
    assert response.text == ""
    assert response.tool_calls[0].id.startswith("hermes-")
    assert _final_answer_problem(response) == "tool_call"


@MODES
def test_an_answer_before_hermes_calls_stands_and_every_call_is_recovered(streamed):
    reply = (
        "Checking both.\n\n"
        + HERMES_NEWLINE
        + '\n<tool_call>\n{"name": "list_hosts", "arguments": {}}\n</tool_call>'
    )

    response, _ = _reply(QWEN, _pieces(reply, 7), streamed=streamed)

    assert response.text == "Checking both."
    assert _calls(response) == [("safe_read", {"value": "a"}), ("list_hosts", {})]


@MODES
def test_glm_and_hermes_frames_are_each_read_in_their_own_grammar(streamed):
    glm = "<tool_call>list_hosts\n<arg_key>limit</arg_key>\n<arg_value>5</arg_value>\n</tool_call>"

    response, _ = _reply(QWEN, _pieces(glm + "\n" + HERMES_NEWLINE), streamed=streamed)

    assert _calls(response) == [
        ("list_hosts", {"limit": 5}),
        ("safe_read", {"value": "a"}),
    ]
    assert [call.id.split("-")[0] for call in response.tool_calls] == ["glm", "hermes"]


UNREADABLE_HERMES = pytest.mark.parametrize(
    "frame",
    [
        '<tool_call>\n{"name": "safe_read", "arguments": {"value": "a"}\n</tool_call>',
        '<tool_call>\n{"name": "safe_read"}\n</tool_call>',
        '<tool_call>\n{"name": "safe_read", "arguments": ["a"]}\n</tool_call>',
        '<tool_call>\n{"name": "safe_read", "arguments": {}, "run": "now"}\n</tool_call>',
        '<tool_call>\n{"name": "Safe Read", "arguments": {}}\n</tool_call>',
        '<tool_call>\n{"name": "safe_read", "arguments": {"value": "a"}}',
    ],
    ids=[
        "not-json",
        "no-arguments",
        "arguments-not-object",
        "unknown-key",
        "bad-name",
        "truncated",
    ],
)


@UNREADABLE_HERMES
@MODES
def test_hermes_markup_core_cannot_read_is_quarantined_not_answered(frame, streamed):
    response, _ = _reply(QWEN, _pieces(frame), streamed=streamed)

    assert response.tool_calls == []
    assert _final_answer_problem(response) == "provider_control_frame"


@UNREADABLE_HERMES
def test_the_answer_before_unreadable_hermes_markup_is_where_it_stops(frame):
    text = f"The value is a.\n{frame}"

    assert tool_markup.frame_start(text) == len("The value is a.\n")


@pytest.mark.parametrize(
    "frame", [HERMES_NEWLINE, HERMES_COMPACT, HERMES_ARGUMENTS_FIRST]
)
def test_a_streamed_answer_never_shows_hermes_markup(frame):
    for size in (1, 3, 7, 64):
        answer = _StreamedAnswer()
        shown = "".join(
            answer.push(piece) for piece in _pieces(f"The value is a.\n{frame}", size)
        )
        assert shown == "The value is a.\n", size


@pytest.mark.parametrize(
    "text",
    [
        "Qwen writes <tool_call> and then a JSON object.",
        'Wrap the JSON in <tool_call> tags, as in <tool_call>{"x": 1}</tool_call>.',
    ],
)
def test_text_that_only_mentions_the_tag_is_still_an_answer(text):
    assert tool_markup.recover(text).calls == []
    assert tool_markup.frame_start(text) is None
    answer = _StreamedAnswer()
    shown = "".join(answer.push(piece) for piece in _pieces(text, 3))
    assert shown + answer.held_tail(text) == text


def test_hermes_markup_in_a_routing_step_runs_like_any_other_tool_call(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _provider = _prepared(
        tmp_path,
        [
            _response(text=HERMES_NEWLINE),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="The safe tool returned a."),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The safe tool returned a."
    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        ("safe_read", {"value": "a"})
    ]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
