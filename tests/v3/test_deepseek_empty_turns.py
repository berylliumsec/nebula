"""DeepSeek V4 on OpenRouter: replies that read as empty, and what they leave.

DeepSeek V4 sometimes writes its tool call into the reasoning channel and
sends no ``tool_calls`` and no answer. Core reads a whole DSML or DeepSeek
special-token frame there as the call it is; a reply that only holds stripped
markup stays empty but says so. Every empty or failed Chat Completions reply
keeps a bounded trace of its frames with the model's words replaced by their
lengths. The remaining shapes (an error frame after output, a repeated finish
reason, replayed reasoning fields) are pinned as already handled.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
from typing import Any

import httpx
import pytest

from nebula.v3 import diagnostics, providers
from nebula.v3.chat import _final_answer_problem, _final_answer_response_record
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    StreamEventType,
    ToolDefinition,
)

MODEL = "deepseek/deepseek-v4.1-flash-1017"
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
# A phrase that must never reach a trace or a diagnostic.
SECRET_THOUGHT = "the operator's private plan for host alpha"
DSML_CALL = (
    "<｜DSML｜function_calls>\n"
    '<｜DSML｜invoke name="lookup_asset">\n'
    '<｜DSML｜parameter name="address" string="true">10.0.0.5</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜function_calls>"
)
DEEPSEEK_TOKENS_CALL = (
    "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>lookup_asset<｜tool▁sep｜>"
    '{"address": "10.0.0.5"}<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
)
GLM_CALL = (
    "<tool_call>lookup_asset\n<arg_key>address</arg_key>\n"
    "<arg_value>10.0.0.5</arg_value>\n</tool_call>"
)
# What one client saw when a route stripped the protocol's special tokens.
STRIPPED_CLOSES = "Let me look it up.\n10.0.0.5</parameter>\n</invoke>\n</calls>"


def _config(flavor: ProviderFlavor = ProviderFlavor.OPENROUTER) -> ProviderConfig:
    return ProviderConfig(
        id="router",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        flavor=flavor,
        base_url="https://provider.invalid/v1",
        default_model=MODEL,
        capabilities=ModelCapabilities(tools=True, strict_tools=True, streaming=True),
        options={"retry_backoff_seconds": 0},
    )


def _request() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="inspect")], tools=[TOOL]
    )


def _sse(*frames: Any, done: bool = True) -> str:
    parts = [
        "data: " + (frame if isinstance(frame, str) else json.dumps(frame))
        for frame in frames
    ]
    if done:
        parts.append("data: [DONE]")
    return "\n\n".join(parts) + "\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict:
    return {
        "id": "gen-1",
        "model": MODEL,
        "provider": "DeepInfra",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


def _reasoning(text: str) -> dict[str, Any]:
    # OpenRouter sends each thought fragment in both channels.
    return {
        "role": "assistant",
        "content": "",
        "reasoning": text,
        "reasoning_details": [
            {"type": "reasoning.text", "text": text, "format": "unknown", "index": 0}
        ],
    }


def _stream(body: str) -> list[providers.ModelStreamEvent]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _config(), transport=httpx.MockTransport(handler)
    )

    async def collect():
        return [event async for event in provider.stream(_request())]

    return asyncio.run(collect())


def _complete(body: dict[str, Any]) -> providers.ModelResponse:
    provider = OpenAICompatibleProvider(
        _config(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )
    return asyncio.run(provider.complete(_request()))


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """Diagnostics as the sanitizer would keep them."""

    seen: list[tuple[str, dict[str, Any]]] = []

    def record(_level, _feature, event_code, _message, **fields):
        seen.append(
            (event_code, diagnostics.sanitize_metadata(fields.get("metadata") or {}))
        )

    monkeypatch.setattr(providers, "record_diagnostic", record)
    return seen


def _completed(events) -> providers.ModelResponse:
    assert events[-1].type == StreamEventType.COMPLETED, events[-1]
    assert events[-1].response is not None
    return events[-1].response


@pytest.mark.parametrize(
    "markup", [DSML_CALL, DEEPSEEK_TOKENS_CALL], ids=["dsml", "special-tokens"]
)
def test_a_call_written_into_streamed_reasoning_is_read_as_a_call(markup, recorded):
    events = _stream(
        _sse(
            _chunk(_reasoning("I should look up the asset.\n")),
            _chunk(_reasoning(markup)),
            _chunk({"content": ""}, finish="stop"),
        )
    )

    response = _completed(events)
    assert [(call.name, call.arguments) for call in response.tool_calls] == [
        ("lookup_asset", {"address": "10.0.0.5"})
    ]
    assert response.text == ""
    assert response.reasoning == "I should look up the asset."
    assert response.reasoning_tool_markup == "recovered"
    assert _final_answer_problem(response) == "tool_call"
    codes = [code for code, _ in recorded]
    assert "providers.dsml.recovered_reasoning_tool_calls" in codes
    assert "providers.stream.empty_reply" not in codes
    metadata = dict(recorded)["providers.dsml.recovered_reasoning_tool_calls"]
    assert metadata["recovered_count"] == 1
    assert metadata["model_id"] == MODEL


def test_a_call_in_non_streamed_reasoning_content_is_read_as_a_call(recorded):
    response = _complete(
        {
            "id": "gen-2",
            "model": MODEL,
            "provider": "DeepInfra",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Checking.\n" + DSML_CALL,
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    assert [call.name for call in response.tool_calls] == ["lookup_asset"]
    assert response.reasoning == "Checking."
    assert response.stream_trace is not None
    assert response.stream_trace["finish_reasons"] == ["tool_calls"]
    assert response.upstream_provider == "DeepInfra"


def test_a_glm_tag_in_reasoning_is_a_thought_not_a_call(recorded):
    events = _stream(
        _sse(_chunk(_reasoning("I could write " + GLM_CALL)), _chunk({}, "stop"))
    )

    response = _completed(events)
    assert response.tool_calls == []
    assert GLM_CALL in response.reasoning
    assert _final_answer_problem(response) == "reasoning_only"


def test_reasoning_markup_is_left_alone_when_the_reply_has_an_answer(recorded):
    events = _stream(
        _sse(
            _chunk(_reasoning(DSML_CALL)),
            _chunk({"content": "The asset is offline."}, "stop"),
        )
    )

    response = _completed(events)
    assert response.tool_calls == []
    assert response.text == "The asset is offline."
    assert DSML_CALL in response.reasoning
    assert response.reasoning_tool_markup is None


def test_stripped_markup_in_reasoning_is_reported_not_run(recorded):
    events = _stream(_sse(_chunk(_reasoning(STRIPPED_CLOSES)), _chunk({}, "stop")))

    response = _completed(events)
    assert response.tool_calls == []
    assert response.reasoning_tool_markup == "unreadable"
    assert _final_answer_problem(response) == "reasoning_only"
    events_by_code = dict(recorded)
    assert "providers.dsml.unreadable_reasoning_markup" in events_by_code
    empty = events_by_code["providers.stream.empty_reply"]
    assert empty["reason_code"] == "reasoning_markup_unreadable"
    record = _final_answer_response_record(response, "reasoning_only")
    assert record["reasoning_tool_markup"] == "unreadable"


def test_an_empty_reply_keeps_a_redacted_trace(recorded):
    events = _stream(
        _sse(
            ": OPENROUTER PROCESSING",
            _chunk(_reasoning(SECRET_THOUGHT)),
            _chunk({"content": ""}, finish="stop"),
            _chunk({}, usage={"prompt_tokens": 10, "completion_tokens": 3}),
        )
    )

    response = _completed(events)
    assert response.text == "" and response.tool_calls == []
    trace = response.stream_trace
    assert trace is not None
    assert trace["done"] is True
    assert trace["finish_reasons"] == ["stop"]
    assert trace["choices"] == [
        {
            "index": 0,
            "content_characters": 0,
            "reasoning_characters": len(SECRET_THOUGHT),
            "tool_call_deltas": 0,
        }
    ]
    assert trace["last_events"][-1] == "[DONE]"
    assert len(trace["last_events"]) <= 6
    assert f"<{len(SECRET_THOUGHT)} chars>" in json.dumps(trace)

    record = _final_answer_response_record(response, "reasoning_only")
    assert record["stream"] == trace
    assert record["model"] == MODEL
    assert record["upstream_provider"] == "DeepInfra"

    metadata = dict(recorded)["providers.stream.empty_reply"]
    assert metadata["reason_code"] == "reasoning_only"
    assert metadata["route"] == "DeepInfra"
    assert metadata["status"] == "stop"
    assert metadata["http_status"] == 200
    assert metadata["finish_reasons"] == ["stop"]
    assert metadata["stream_choices"] == [
        f"0:content=0,reasoning={len(SECRET_THOUGHT)},tool_call_deltas=0"
    ]
    assert metadata["stream_frames"][-1] == "[DONE]"
    everything = json.dumps([record, recorded])
    assert SECRET_THOUGHT not in everything


def test_a_trace_is_bounded():
    frames = [_chunk(_reasoning("x" * 5000)) for _ in range(40)]
    events = _stream(_sse(*frames, _chunk({}, "stop")))

    trace = _completed(events).stream_trace
    assert trace is not None
    assert trace["frames"] == 42
    assert len(trace["last_events"]) == 6
    assert all(len(frame) <= 400 for frame in trace["last_events"])


def test_an_error_frame_after_output_fails_the_stream_with_a_trace(recorded):
    events = _stream(
        _sse(
            _chunk(_reasoning(SECRET_THOUGHT)),
            {
                "id": "gen-1",
                "object": "chat.completion.chunk",
                "provider": "DeepInfra",
                "error": {"code": "server_error", "message": "Provider disconnected"},
                "choices": [
                    {"index": 0, "delta": {"content": ""}, "finish_reason": "error"}
                ],
            },
        )
    )

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].retryable is True
    assert "Provider disconnected" in (events[-1].error or "")
    metadata = dict(recorded)["providers.stream.failed_trace"]
    assert metadata["reason_code"] == "ProviderOverloadedError"
    assert metadata["chunk_count"] == 2
    assert "Provider disconnected" in metadata["stream_frames"][-1]
    assert SECRET_THOUGHT not in json.dumps(recorded)


def test_a_repeated_finish_reason_is_not_an_empty_reply(recorded):
    events = _stream(
        _sse(
            _chunk({"content": "Host alpha is up."}),
            _chunk({}, finish="stop"),
            _chunk({}, finish="stop"),
            _chunk({}, usage={"prompt_tokens": 1, "completion_tokens": 1}),
        )
    )

    response = _completed(events)
    assert response.text == "Host alpha is up."
    assert response.stream_trace is not None
    assert response.stream_trace["finish_reasons"] == ["stop", "stop"]
    assert "providers.stream.empty_reply" not in dict(recorded)


def test_openrouter_deepseek_v4_replay_carries_no_empty_reasoning_text():
    provider = OpenAICompatibleProvider(_config())
    state = {
        "provider_id": "router",
        "model": MODEL,
        "reasoning": "",
        "reasoning_details": [],
    }
    payload = provider._payload(
        ModelRequest(
            messages=[
                ModelMessage(role="user", content="first"),
                ModelMessage(role="assistant", content="An earlier answer."),
                ModelMessage(role="user", content="inspect"),
            ],
            tools=[TOOL],
            tool_results=[
                ModelToolResult(
                    call_id="call-1",
                    name="lookup_asset",
                    arguments={"address": "10.0.0.5"},
                    output={"status": "up"},
                    response_group="g1",
                    reasoning_state=state,
                )
            ],
        ),
        MODEL,
    )

    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 2
    for message in assistants:
        assert "reasoning_content" not in message
        assert "reasoning" not in message
        assert message["reasoning_details"] == []
