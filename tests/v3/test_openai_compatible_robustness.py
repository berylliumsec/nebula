"""Transport, error and response-shape edge cases of the Chat Completions adapter.

Each case is a shape a real OpenAI-compatible server or gateway sends: a read
timeout before the first token, an error object inside an HTTP 200, a null
where a list belongs, a keepalive data frame, a legacy ``function_call``. None
of them may end a turn with a raw Python error, and a transient one is retried
like the equivalent HTTP status.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderContextLengthError,
    ProviderError,
    ProviderOverloadedError,
    ProviderResponseError,
    ProviderKind,
    StreamEventType,
    ToolDefinition,
)

TOOL = ToolDefinition(
    name="lookup_asset",
    description="Look up one asset",
    input_schema={
        "type": "object",
        "properties": {"address": {"type": "string"}},
        "required": ["address"],
        "additionalProperties": False,
    },
)


def _config(provider_id: str = "oac", **options: Any) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.invalid/v1",
        default_model="test-model",
        capabilities=ModelCapabilities(tools=True, strict_tools=True, streaming=True),
        # Zero backoff keeps the retry contract under test without real waiting.
        options={"retry_backoff_seconds": 0, **options},
    )


def _plain() -> ModelRequest:
    return ModelRequest(messages=[ModelMessage(role="user", content="hi")])


def _with_tools() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="inspect")], tools=[TOOL]
    )


def _sse(*frames: Any, done: bool = True, sep: str = "\n\n") -> str:
    parts = [
        "data: " + (frame if isinstance(frame, str) else json.dumps(frame))
        for frame in frames
    ]
    if done:
        parts.append("data: [DONE]")
    return sep.join(parts) + sep


def _answer(text: str = "answer", finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "chat-ok",
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }


def _completion(message: dict[str, Any], finish: str | None = "stop") -> dict:
    return {
        "id": "chat-ok",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def _provider(
    handler, config: ProviderConfig | None = None
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        config or _config(), transport=httpx.MockTransport(handler)
    )


def _collect(provider, request: ModelRequest | None = None):
    async def collect():
        return [event async for event in provider.stream(request or _plain())]

    return asyncio.run(collect())


def _stream_of(body: str, request: ModelRequest | None = None, calls=None):
    def handler(_request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    return _collect(_provider(handler), request)


def _complete_with(body: Any, request: ModelRequest | None = None, calls=None):
    def handler(_request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(1)
        return httpx.Response(200, json=body)

    return asyncio.run(_provider(handler).complete(request or _plain()))


def _types(events) -> list[StreamEventType]:
    return [event.type for event in events]


_TRANSPORT_FAILURES = {
    "ReadTimeout": lambda: httpx.ReadTimeout("The read operation timed out"),
    "RemoteProtocolError": lambda: httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    ),
    "ReadError": lambda: httpx.ReadError("[Errno 104] Connection reset by peer"),
    "WriteError": lambda: httpx.WriteError("[Errno 32] Broken pipe"),
}


class _FailsFirst(httpx.AsyncBaseTransport):
    """First attempt fails in transport; later attempts answer ``body``."""

    def __init__(self, failure, body: str, *, stream: bool, prelude: bytes = b""):
        self.attempts = 0
        self.failure = failure
        self.body = body
        self.stream = stream
        self.prelude = prelude

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts > 1:
            return httpx.Response(
                200,
                content=self.body.encode(),
                headers={
                    "content-type": "text/event-stream"
                    if self.stream
                    else "application/json"
                },
            )
        if not self.prelude:
            raise self.failure()
        failure, prelude = self.failure, self.prelude

        class Torn(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield prelude
                raise failure()

        return httpx.Response(
            200, stream=Torn(), headers={"content-type": "text/event-stream"}
        )


# --------------------------------------------------------------- OAC-4


@pytest.mark.parametrize("kind", sorted(_TRANSPORT_FAILURES))
def test_interrupted_transport_is_retried_for_completions(kind):
    transport = _FailsFirst(
        _TRANSPORT_FAILURES[kind],
        json.dumps(_completion({"content": "answer"})),
        stream=False,
    )
    provider = OpenAICompatibleProvider(_config(), transport=transport)

    result = asyncio.run(provider.complete(_plain()))

    assert transport.attempts == 2
    assert result.text == "answer"


def test_exhausted_transport_retries_are_labelled_retryable():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("")

    with pytest.raises(ProviderOverloadedError) as failure:
        asyncio.run(_provider(handler).complete(_plain()))

    assert len(attempts) == 3
    assert (
        str(failure.value)
        == "provider request timed out (ReadTimeout) after 3 attempts"
    )

    # With retries disabled the single failure is still transient.
    single: list[int] = []

    def once(_request: httpx.Request) -> httpx.Response:
        single.append(1)
        raise httpx.ReadTimeout("")

    with pytest.raises(ProviderOverloadedError) as failure:
        asyncio.run(_provider(once, _config(retry_attempts=1)).complete(_plain()))

    assert len(single) == 1
    assert str(failure.value) == "provider request timed out (ReadTimeout)"


def test_non_inference_requests_do_not_resend_after_a_read_timeout():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("")

    health = asyncio.run(_provider(handler).health())

    assert attempts == [1]
    assert health.healthy is False


@pytest.mark.parametrize("kind", sorted(_TRANSPORT_FAILURES))
@pytest.mark.parametrize("after_role_frame", [False, True])
def test_stream_transport_failure_before_output_is_retried(kind, after_role_frame):
    prelude = (
        b'data: {"id":"g","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
        if after_role_frame
        else b""
    )
    transport = _FailsFirst(
        _TRANSPORT_FAILURES[kind], _sse(_answer()), stream=True, prelude=prelude
    )
    provider = OpenAICompatibleProvider(_config(), transport=transport)

    events = _collect(provider)

    assert transport.attempts == 2
    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "answer"


def test_stream_transport_failure_exhausted_before_output_is_retryable():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("")

    events = _collect(_provider(handler))

    assert len(attempts) == 3
    assert _types(events) == [StreamEventType.STARTED, StreamEventType.ERROR]
    assert events[-1].retryable is True
    assert events[-1].error == (
        "provider request timed out (ReadTimeout) after 3 attempts"
    )


def test_stream_transport_failure_after_output_is_never_replayed():
    transport = _FailsFirst(
        _TRANSPORT_FAILURES["ReadTimeout"],
        _sse(_answer()),
        stream=True,
        prelude=b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n',
    )
    provider = OpenAICompatibleProvider(_config(), transport=transport)

    events = _collect(provider)

    assert transport.attempts == 1
    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert events[-1].retryable is False


# --------------------------------------------------------------- OAC-5


def test_null_tool_calls_and_null_choices_are_read_safely():
    # LiteLLM-style proxies serialize absent lists as null.
    result = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": None,
                "function_call": None,
            }
        )
    )
    assert result.text == "done"
    assert result.tool_calls == []

    result = _complete_with(
        _completion({"role": "assistant", "content": "done", "tool_calls": [None]})
    )
    assert result.text == "done"
    assert result.tool_calls == []

    result = _complete_with({"id": "g", "choices": [None], "usage": None})
    assert result.text == ""

    result = _complete_with({"id": "g", "choices": [{"index": 0, "message": None}]})
    assert result.text == ""


def test_a_tool_call_with_a_null_function_is_returned_for_the_model():
    result = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": None}],
            },
            finish="tool_calls",
        ),
        _with_tools(),
    )

    # The call names no function, so it cannot run; the model is told why.
    [call] = result.tool_calls
    assert (call.id, call.name, call.arguments) == ("c1", "invalid_tool_call", {})
    assert call.invalid_reason == "the call did not name a tool"


def test_a_non_json_200_body_is_a_provider_response_error():
    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>Bad gateway</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    with pytest.raises(ProviderResponseError) as failure:
        asyncio.run(_provider(html).complete(_plain()))

    assert "not JSON" in str(failure.value)
    assert "text/html" in str(failure.value)

    def listing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "completion"])

    with pytest.raises(ProviderResponseError):
        asyncio.run(_provider(listing).complete(_plain()))


def test_a_null_stream_choice_is_skipped():
    events = _stream_of(_sse({"id": "g", "choices": [None]}, _answer("x")))

    assert _types(events)[-1] == StreamEventType.COMPLETED
    assert events[-1].response.text == "x"


# --------------------------------------------------------------- OAC-6


OPENROUTER_BODY_ERROR = {
    "error": {
        "code": 502,
        "message": "Provider returned error",
        "metadata": {"raw": "upstream overloaded", "provider_name": "X"},
    }
}


def test_a_transient_error_in_a_200_body_is_retried_like_its_status():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json=OPENROUTER_BODY_ERROR)
        return httpx.Response(200, json=_completion({"content": "answer"}))

    result = asyncio.run(_provider(handler).complete(_plain()))

    assert len(calls) == 2
    assert result.text == "answer"


def test_an_error_in_a_200_body_is_raised_not_answered():
    calls: list[int] = []

    with pytest.raises(ProviderOverloadedError) as failure:
        _complete_with(OPENROUTER_BODY_ERROR, calls=calls)

    assert len(calls) == 3
    assert failure.value.status_code == 502
    assert str(failure.value) == (
        "provider returned an error instead of a reply: Provider returned error "
        "(upstream: upstream overloaded) after 3 attempts"
    )

    rejected: list[int] = []
    with pytest.raises(ProviderError) as failure:
        _complete_with(
            {"error": {"code": 400, "message": "model not found"}}, calls=rejected
        )

    assert rejected == [1]
    assert not isinstance(failure.value, ProviderOverloadedError)
    assert "model not found" in str(failure.value)

    with pytest.raises(ProviderContextLengthError):
        _complete_with(
            {
                "error": {
                    "code": "context_length_exceeded",
                    "message": "This model's maximum context length is 8192 tokens",
                }
            }
        )


def test_finish_reason_error_is_not_a_completed_answer():
    calls: list[int] = []

    with pytest.raises(ProviderOverloadedError) as failure:
        _complete_with(
            _completion({"role": "assistant", "content": ""}, finish="error"),
            calls=calls,
        )

    assert len(calls) == 3
    assert "ended the reply with an error" in str(failure.value)

    events = _stream_of(
        _sse(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "The scan found 3 open po"}}
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]},
        )
    )

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert events[-1].retryable is True
    assert "ended the reply with an error" in (events[-1].error or "")


def test_content_filter_stop_is_a_typed_error_naming_the_filter():
    calls: list[int] = []

    with pytest.raises(ProviderResponseError) as failure:
        _complete_with(
            {
                "id": "g",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Here is how to"},
                        "finish_reason": "content_filter",
                        "content_filter_results": {
                            "hate": {"filtered": False, "severity": "safe"},
                            "violence": {"filtered": True, "severity": "high"},
                        },
                    }
                ],
            },
            calls=calls,
        )

    assert calls == [1]
    assert "content filter" in str(failure.value)
    assert "violence" in str(failure.value)
    assert "hate" not in str(failure.value)

    events = _stream_of(
        _sse(
            {"choices": [{"index": 0, "delta": {"content": "Here is how to"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]},
        )
    )

    assert _types(events)[-1] == StreamEventType.ERROR
    assert "content filter" in (events[-1].error or "")
    assert events[-1].retryable is False


# --------------------------------------------------------------- OAC-7


@pytest.mark.parametrize(
    "code",
    [
        "server_error",
        "internal_error",
        "internal_server_error",
        "service_unavailable",
        "overloaded",
        "overloaded_error",
        "timeout",
        "upstream_error",
    ],
)
def test_string_error_codes_are_transient(code):
    failure = providers._stream_error_frame(
        {"error": {"code": code, "message": "try later"}}
    )

    assert isinstance(failure, ProviderOverloadedError)
    # Anthropic-style bodies carry the kind in ``type`` rather than ``code``.
    typed = providers._stream_error_frame(
        {"error": {"type": code, "message": "try later"}}
    )
    assert isinstance(typed, ProviderOverloadedError)


@pytest.mark.parametrize(
    "message", ["Provider returned error", "Provider disconnected unexpectedly"]
)
def test_openrouter_upstream_failures_without_a_status_are_transient(message):
    failure = providers._stream_error_frame({"error": {"message": message}})

    assert isinstance(failure, ProviderOverloadedError)

    # An explicit non-retryable status still wins over the generic wording.
    rejected = providers._stream_error_frame(
        {"error": {"code": 400, "message": message}}
    )
    assert not isinstance(rejected, ProviderOverloadedError)
    unknown = providers._stream_error_frame(
        {"error": {"code": "invalid_request_error", "message": "bad request"}}
    )
    assert not isinstance(unknown, ProviderOverloadedError)


def test_openrouter_server_error_frame_before_output_is_replayed():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = (
            _sse(
                {
                    "id": "g",
                    "object": "chat.completion.chunk",
                    "error": {
                        "code": "server_error",
                        "message": "Provider disconnected unexpectedly",
                    },
                    "choices": [
                        {"index": 0, "delta": {"content": ""}, "finish_reason": "error"}
                    ],
                },
                done=False,
            )
            if len(calls) == 1
            else _sse(_answer("fine"))
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    events = _collect(_provider(handler))

    assert len(calls) == 2
    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "fine"


# --------------------------------------------------------------- OAC-8


class _StallsAfter(httpx.AsyncBaseTransport):
    """Sends ``chunks`` and then fails, as a proxy holding the socket would."""

    def __init__(self, *chunks: bytes, failure) -> None:
        self.chunks = chunks
        self.failure = failure
        self.read_past_last_chunk = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        owner = self

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in owner.chunks:
                    yield chunk
                owner.read_past_last_chunk = True
                raise owner.failure()

        return httpx.Response(
            200, stream=Body(), headers={"content-type": "text/event-stream"}
        )


def test_stream_stops_reading_at_done():
    transport = _StallsAfter(
        b'data: {"id":"g","choices":[{"index":0,"delta":{"content":"The host has 3 open ports."},"finish_reason":"stop"}]}\n\n',
        b'data: {"id":"g","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}\n\n',
        b"data: [DONE]\n\n",
        failure=_TRANSPORT_FAILURES["ReadTimeout"],
    )
    provider = OpenAICompatibleProvider(_config(), transport=transport)

    events = _collect(provider)

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "The host has 3 open ports."
    assert events[-1].response.usage.total_tokens == 12
    assert transport.read_past_last_chunk is False


def test_a_connection_torn_after_the_finish_reason_keeps_the_reply(monkeypatch):
    recorded: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        providers,
        "record_diagnostic",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    transport = _StallsAfter(
        b'data: {"id":"g","choices":[{"index":0,"delta":{"content":"The host has 3 open ports."},"finish_reason":"stop"}]}\n\n',
        failure=_TRANSPORT_FAILURES["RemoteProtocolError"],
    )
    provider = OpenAICompatibleProvider(_config(), transport=transport)

    events = _collect(provider)

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "The host has 3 open ports."
    assert events[-1].response.finish_reason == "stop"
    [(args, fields)] = [
        item
        for item in recorded
        if item[0][2] == "providers.stream.closed_after_finish"
    ]
    assert args[0] == "warning"
    assert fields["metadata"]["exception_type"] == "RemoteProtocolError"


# -------------------------------------------------------------- OAC-12


CONTEXT_OVERFLOWS = {
    "xAI": (
        400,
        "This model's maximum prompt length is 131072 but the request contains 537812 tokens",
    ),
    "OpenRouter/Poolside": (
        400,
        "Input length 300000 exceeds the maximum allowed input length of 262144 tokens.",
    ),
    "llama.cpp": (
        400,
        "the request exceeds the available context size, try increasing it",
    ),
    "Kimi": (
        400,
        "Your request exceeded model token limit: 262144 (requested: 300000)",
    ),
    "Ollama 500": (500, "prompt too long; exceeded max context length by 1200 tokens"),
    "Anthropic 413": (413, "Request exceeds the maximum size"),
    # Already recognised before this change; kept as a regression guard.
    "vLLM": (
        400,
        "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens.",
    ),
    "Anthropic": (400, "prompt is too long: 213462 tokens > 200000 maximum"),
}


@pytest.mark.parametrize("vendor", sorted(CONTEXT_OVERFLOWS))
def test_context_overflow_messages_from_each_vendor_are_typed(vendor):
    status, message = CONTEXT_OVERFLOWS[vendor]
    response = httpx.Response(
        status,
        json={"error": {"message": message}},
        request=httpx.Request("POST", "https://provider.invalid"),
    )

    assert isinstance(providers._safe_error(response), ProviderContextLengthError)
    assert isinstance(
        providers._stream_error_frame({"error": {"message": message, "code": status}}),
        ProviderContextLengthError,
    )


def test_context_overflow_is_read_from_the_error_type_too():
    response = httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "type": "exceed_context_size_error",
                "message": "request (40000 tokens) too large",
            }
        },
        request=httpx.Request("POST", "https://provider.invalid"),
    )

    assert isinstance(providers._safe_error(response), ProviderContextLengthError)


def test_a_5xx_overflow_is_not_retried_as_an_overload():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": "prompt too long; exceeded max context length by 1200 tokens"
                }
            },
        )

    with pytest.raises(ProviderContextLengthError):
        asyncio.run(_provider(handler).complete(_plain()))

    assert calls == [1]

    # An ordinary 5xx is still a transient overload.
    overloaded = providers._safe_error(
        httpx.Response(
            500,
            json={"error": {"message": "internal failure"}},
            request=httpx.Request("POST", "https://provider.invalid"),
        )
    )
    assert isinstance(overloaded, ProviderOverloadedError)


# -------------------------------------------------------------- OAC-15


def test_stream_skips_non_json_keepalive_frames(monkeypatch):
    recorded: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        providers,
        "record_diagnostic",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    events = _stream_of(_sse("ping", _answer("ok"), "keep-alive"))

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "ok"
    skipped = [
        item for item in recorded if item[0][2] == "providers.stream.keepalive_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0][0][0] == "warning"


def test_a_keepalive_before_a_transient_error_frame_still_allows_replay():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = (
            _sse("ping", {"error": {"code": 503, "message": "overloaded"}}, done=False)
            if len(calls) == 1
            else _sse(_answer("fine"))
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    events = _collect(_provider(handler))

    assert len(calls) == 2
    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.text == "fine"


def test_a_damaged_json_chunk_is_a_typed_error_not_a_skip():
    events = _stream_of(
        _sse(
            {"choices": [{"index": 0, "delta": {"content": "a"}}]}, '{"choices": [{"del'
        )
    )

    assert _types(events)[-1] == StreamEventType.ERROR
    assert "malformed stream chunk" in (events[-1].error or "")


def test_an_error_event_surfaces_its_text():
    events = _stream_of("event: error\ndata: upstream request timed out after 60s\n\n")

    assert _types(events) == [StreamEventType.STARTED, StreamEventType.ERROR]
    assert events[-1].error == (
        "provider reported an error while streaming: upstream request timed out "
        "after 60s"
    )

    events = _stream_of(
        'event: error\ndata: {"message":"upstream gone","code":400}\n\n'
    )

    assert events[-1].type == StreamEventType.ERROR
    assert "upstream gone" in (events[-1].error or "")


def test_lf_joined_data_lines_are_parsed_one_line_at_a_time():
    events = _stream_of(
        _sse(
            {"id": "g", "choices": [{"index": 0, "delta": {"content": "a"}}]},
            {
                "id": "g",
                "choices": [
                    {"index": 0, "delta": {"content": "b"}, "finish_reason": "stop"}
                ],
            },
            sep="\n",
        )
    )

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "ab"


def test_legacy_function_call_is_read_as_a_tool_call():
    result = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "function_call": {
                    "name": "lookup_asset",
                    "arguments": '{"address":"10.0.0.9"}',
                },
            },
            finish="function_call",
        ),
        _with_tools(),
    )

    [call] = result.tool_calls
    assert call.name == "lookup_asset"
    assert call.arguments == {"address": "10.0.0.9"}
    assert call.id

    events = _stream_of(
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "function_call": {"name": "lookup_asset", "arguments": ""}
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"function_call": {"arguments": '{"address":'}},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"function_call": {"arguments": '"10.0.0.9"}'}},
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "function_call"}]},
        ),
        _with_tools(),
    )

    calls = [e.tool_call for e in events if e.type == StreamEventType.TOOL_CALL]
    assert [(call.name, call.arguments) for call in calls] == [
        ("lookup_asset", {"address": "10.0.0.9"})
    ]
    assert events[-1].type == StreamEventType.COMPLETED
    assert len(events[-1].response.tool_calls) == 1


def test_a_legacy_function_call_beside_tool_calls_is_not_run_twice():
    result = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup_asset",
                            "arguments": '{"address":"a"}',
                        },
                    }
                ],
                "function_call": {
                    "name": "lookup_asset",
                    "arguments": '{"address":"a"}',
                },
            },
            finish="tool_calls",
        ),
        _with_tools(),
    )

    assert [call.id for call in result.tool_calls] == ["call_1"]


def test_a_refusal_is_surfaced_as_the_reply():
    result = _complete_with(
        _completion(
            {"role": "assistant", "content": None, "refusal": "I can't help with that."}
        )
    )

    assert result.text == "I can't help with that."

    events = _stream_of(
        _sse(
            {
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "refusal": "I can't "}}
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"refusal": "help with that."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )

    assert _types(events) == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "I can't help with that."


def test_double_encoded_arguments_that_decode_to_an_object_are_accepted():
    result = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "lookup_asset",
                            "arguments": json.dumps(json.dumps({"address": "a"})),
                        },
                    }
                ],
            },
            finish="tool_calls",
        ),
        _with_tools(),
    )

    assert result.tool_calls[0].arguments == {"address": "a"}

    # A string that is not an encoded object is still refused.
    refused = _complete_with(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "lookup_asset",
                            "arguments": json.dumps(json.dumps(["a"])),
                        },
                    }
                ],
            },
            finish="tool_calls",
        ),
        _with_tools(),
    )

    [call] = refused.tool_calls
    assert call.arguments == {}
    assert call.invalid_reason == "arguments were not a JSON object"
