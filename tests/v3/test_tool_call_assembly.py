"""A defect in one tool call never fails the provider response.

Live evidence: deepseek/deepseek-v4.1-flash through OpenRouter streamed a
complete call and then the tail of its arguments again under a new index,
with no id and no name. Core assembled that tail into a nameless second call
and failed the whole turn with "provider returned malformed tool arguments".

Mature harnesses keep the turn: the AI SDK's streaming tool-call tracker looks
calls up by id before index and turns an unreadable call into an invalid one
the model is told about, Codex answers "failed to parse function arguments",
opencode repairs a mis-cased name and pi-mono repairs bad escapes. These tests
pin the same contract for every adapter: fragments are assembled into the call
they belong to, recoverable encodings are read, and a call that still cannot
be read comes back with ``invalid_reason`` instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderFlavor,
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
DOTTED = ToolDefinition(
    name="tool_output.search",
    description="Search stored output",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)
ADDRESS = '{"address":"10.0.0.9"}'
SYNTHETIC_ID = re.compile(r"^call_[0-9a-f]{32}$")
# The name a call carries when the one the provider sent is unusable.
INVALID = "invalid_tool_call"


def _request(**extra: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "messages": [ModelMessage(role="user", content="inspect")],
        "tools": [TOOL],
    }
    values.update(extra)
    return ModelRequest(**values)


def _openai_config(**extra: Any) -> ProviderConfig:
    return ProviderConfig(
        **{
            "id": "openrouter",
            "kind": ProviderKind.OPENAI_COMPATIBLE,
            "flavor": ProviderFlavor.OPENROUTER,
            "base_url": "https://openrouter.ai/api/v1",
            "default_model": "deepseek/deepseek-v4.1-flash",
            "capabilities": ModelCapabilities(
                tools=True, strict_tools=True, streaming=True
            ),
            "options": {"retry_backoff_seconds": 0},
            **extra,
        }
    )


def _chunk(*calls: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": {"tool_calls": list(calls)}}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "gen-1", "model": "deepseek/deepseek-v4.1-flash", "choices": [choice]}


def _finish(reason: str = "tool_calls") -> dict[str, Any]:
    return {
        "id": "gen-1",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
    }


def _sse(*frames: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + (
        "data: [DONE]\n\n"
    )


def _stream(body: str, request: ModelRequest | None = None):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _openai_config(), transport=httpx.MockTransport(handler)
    )

    async def collect():
        return [event async for event in provider.stream(request or _request())]

    return asyncio.run(collect())


def _complete(message: dict[str, Any], *, finish: str = "tool_calls", request=None):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "deepseek/deepseek-v4.1-flash",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            },
        )

    provider = OpenAICompatibleProvider(
        _openai_config(), transport=httpx.MockTransport(handler)
    )
    return asyncio.run(provider.complete(request or _request()))


def _shape(call) -> tuple[str, str, dict[str, Any], str | None]:
    call_id = "<synthetic>" if SYNTHETIC_ID.fullmatch(call.id) else call.id
    return call_id, call.name, call.arguments, call.invalid_reason


def _reason(prefix: str):
    """Match an ``invalid_reason`` by its start; the rest is the decoder's."""

    class _Starts(str):
        def __eq__(self, other: object) -> bool:
            return isinstance(other, str) and other.startswith(prefix)

        __hash__ = str.__hash__

    return _Starts(prefix)


A = {"address": "10.0.0.9"}

# The streamed shapes from the audit (oac/repro_tool_deltas.py), each with the
# calls Core must read out of it.
STREAM_SHAPES = {
    "S1 closing tail on a new index completes call 0": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"10.0.0.9',
                    },
                }
            ),
            _chunk({"index": 1, "function": {"arguments": '"}'}}),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S2 stray tail on a new index after a complete call (live #2)": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ),
            _chunk({"index": 1, "function": {"arguments": '"}'}}),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S3 stray tail with an empty id and name": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ),
            _chunk(
                {
                    "index": 1,
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": '"}'},
                }
            ),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S4 unindexed continuation under a fresh id": (
        [
            _chunk(
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"10.0.0.9',
                    },
                }
            ),
            _chunk({"id": "call_2", "function": {"arguments": '"}'}}),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S5 full arguments re-sent on the same index": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": '{"address":'},
                }
            ),
            _chunk({"index": 0, "function": {"arguments": '"10.0.0.9"}'}}),
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                },
                finish="tool_calls",
            ),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S6 cumulative dict arguments": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": {"address": "10.0"},
                    },
                }
            ),
            _chunk({"index": 0, "function": {"arguments": {"address": "10.0.0.9"}}}),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S7 two calls on index 0 with different ids": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"a"}',
                    },
                }
            ),
            _chunk(
                {
                    "index": 0,
                    "id": "call_b",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"b"}',
                    },
                }
            ),
            _finish(),
        ],
        [
            ("call_a", "lookup_asset", {"address": "a"}, None),
            ("call_b", "lookup_asset", {"address": "b"}, None),
        ],
    ),
    "S7b two id-less calls on index 0 with different names": (
        [
            _chunk(
                {
                    "index": 0,
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ),
            _chunk(
                {
                    "index": 0,
                    "type": "function",
                    "function": {"name": "tool_output.search", "arguments": "{}"},
                }
            ),
            _finish(),
        ],
        [
            ("<synthetic>", "lookup_asset", A, None),
            ("<synthetic>", "tool_output.search", {}, None),
        ],
    ),
    "S8 unindexed name before id": (
        [
            _chunk({"type": "function", "function": {"name": "lookup_asset"}}),
            _chunk({"id": "call_1", "function": {"arguments": ADDRESS}}),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S9 a call without any id": (
        [
            _chunk(
                {
                    "index": 0,
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ),
            _finish(),
        ],
        [("<synthetic>", "lookup_asset", A, None)],
    ),
    "S10 a backslash that starts no JSON escape": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"10\\.0\\.0\\.9"}',
                    },
                }
            ),
            _finish(),
        ],
        [("call_1", "lookup_asset", {"address": "10\\.0\\.0\\.9"}, None)],
    ),
    "S11 a raw newline inside a string": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"line1\nline2"}',
                    },
                }
            ),
            _finish(),
        ],
        [("call_1", "lookup_asset", {"address": "line1\nline2"}, None)],
    ),
    "S12 arguments cut off by the output limit are never completed": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"10.0',
                    },
                }
            ),
            _finish("length"),
        ],
        [
            (
                "call_1",
                "lookup_asset",
                {},
                _reason("arguments were not valid JSON: Unterminated string"),
            )
        ],
    ),
    "S13 a mis-cased tool name": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Lookup_Asset", "arguments": ADDRESS},
                }
            ),
            _finish(),
        ],
        [("call_1", "lookup_asset", A, None)],
    ),
    "S14 one unreadable call beside a good one": (
        [
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ),
            _chunk(
                {
                    "index": 1,
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "lookup_asset", "arguments": '{"address":'},
                }
            ),
            _finish(),
        ],
        [
            ("call_1", "lookup_asset", A, None),
            (
                "call_2",
                "lookup_asset",
                {},
                _reason("arguments were not valid JSON: Expecting value"),
            ),
        ],
    ),
}


@pytest.mark.parametrize(
    "shape", list(STREAM_SHAPES), ids=lambda label: label.split()[0]
)
def test_streamed_tool_call_shapes_complete_with_every_call_read(shape):
    frames, expected = STREAM_SHAPES[shape]

    events = _stream(_sse(*frames))

    types = [event.type for event in events]
    assert StreamEventType.ERROR not in types, events[-1].error
    assert types[-1] == StreamEventType.COMPLETED
    streamed = [
        event.tool_call for event in events if event.type == StreamEventType.TOOL_CALL
    ]
    assert [_shape(call) for call in streamed] == expected
    assert [_shape(call) for call in events[-1].response.tool_calls] == expected
    # Arguments Core could not read are never guessed at.
    for call in streamed:
        if call.invalid_reason is not None:
            assert call.arguments == {}


def test_the_live_stray_tail_is_dropped_with_a_diagnostic(monkeypatch):
    recorded: list[tuple[str, str, dict[str, Any]]] = []
    original = providers.record_diagnostic

    def capture(level, feature, event_code, message, **fields):
        recorded.append((level, event_code, fields))
        return original(level, feature, event_code, message, **fields)

    monkeypatch.setattr(providers, "record_diagnostic", capture)
    frames, _ = STREAM_SHAPES[
        "S2 stray tail on a new index after a complete call (live #2)"
    ]

    events = _stream(_sse(*frames))

    assert events[-1].type == StreamEventType.COMPLETED
    [(level, _, fields)] = [
        item for item in recorded if item[1] == "providers.tool_call.stray_fragment"
    ]
    assert level == "warning"
    assert fields["metadata"]["byte_count"] == 2
    assert fields["metadata"]["model_id"] == "deepseek/deepseek-v4.1-flash"
    # The fragment's text is never logged.
    assert '"}' not in json.dumps(fields["metadata"])


def test_a_nameless_fragment_with_nothing_to_join_is_an_invalid_call():
    """The synthesis stream's shape: an attempted call, not a failed response."""

    events = _stream(
        _sse(_chunk({"index": 0, "function": {"arguments": '"x'}}), _finish()),
        _request(tools=[]),
    )

    assert events[-1].type == StreamEventType.COMPLETED
    [call] = events[-1].response.tool_calls
    assert SYNTHETIC_ID.fullmatch(call.id)
    assert call.name == INVALID == providers.INVALID_TOOL_CALL_NAME
    assert call.arguments == {}
    assert call.invalid_reason.startswith("the call did not name a tool")
    assert [event.tool_call for event in events if event.tool_call] == [call]


def test_different_objects_run_together_are_not_guessed_apart():
    events = _stream(
        _sse(
            _chunk(
                {
                    "index": 0,
                    "id": "call_1",
                    "function": {
                        "name": "lookup_asset",
                        "arguments": '{"address":"a"}{"address":"b"}',
                    },
                }
            ),
            _finish(),
        )
    )

    [call] = events[-1].response.tool_calls
    assert call.arguments == {}
    assert call.invalid_reason.startswith("arguments were not valid JSON: Extra data")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("functions.lookup_asset", "lookup_asset"),
        (" lookup_asset ", "lookup_asset"),
        ("TOOL_OUTPUT_SEARCH", "tool_output.search"),
        ("Tool_Output.Search", "tool_output.search"),
    ],
)
def test_tool_names_decode_against_the_declared_tools(name, expected):
    result = _complete(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "function": {"name": name, "arguments": "{}"}}
            ],
        },
        request=_request(tools=[TOOL, DOTTED]),
    )

    [call] = result.tool_calls
    assert (call.name, call.invalid_reason) == (expected, None)


# The non-streamed defects of the routing audit (route/repro_wire.py W1-W4b),
# which every routing step reads through complete().
COMPLETE_SHAPES = {
    "W1 unterminated arguments": (
        {"id": "call-1", "function": {"name": "lookup_asset", "arguments": '{"a'}},
        "tool_calls",
        ("call-1", "lookup_asset", {}, _reason("arguments were not valid JSON")),
    ),
    "W2 arguments cut off by the output limit": (
        {
            "id": "call-1",
            "function": {"name": "lookup_asset", "arguments": '{"address": "aaa'},
        },
        "length",
        (
            "call-1",
            "lookup_asset",
            {},
            _reason("arguments were not valid JSON: Unterminated string"),
        ),
    ),
    "W3 an empty id": (
        {"id": "", "function": {"name": "lookup_asset", "arguments": ADDRESS}},
        "tool_calls",
        ("<synthetic>", "lookup_asset", A, None),
    ),
    "W4 an undeclared name that is not a tool name": (
        {"id": "call-1", "function": {"name": "Bash", "arguments": "{}"}},
        "tool_calls",
        (
            "call-1",
            INVALID,
            {},
            _reason("'Bash' is not the name of a tool"),
        ),
    ),
    "W4 a one-letter name": (
        {"id": "call-1", "function": {"name": "x", "arguments": "{}"}},
        "tool_calls",
        (
            "call-1",
            INVALID,
            {},
            _reason("'x' is not the name of a tool"),
        ),
    ),
    "W4b a case variant of a declared tool": (
        {"id": "call-1", "function": {"name": "LOOKUP_ASSET", "arguments": ADDRESS}},
        "tool_calls",
        ("call-1", "lookup_asset", A, None),
    ),
    "non-object arguments": (
        {"id": "call-1", "function": {"name": "lookup_asset", "arguments": "[1]"}},
        "tool_calls",
        ("call-1", "lookup_asset", {}, "arguments were not a JSON object"),
    ),
    "an invalid escape": (
        {
            "id": "call-1",
            "function": {
                "name": "lookup_asset",
                "arguments": '{"address":"C:\\Users\\x"}',
            },
        },
        "tool_calls",
        ("call-1", "lookup_asset", {"address": "C:\\Users\\x"}, None),
    ),
}


@pytest.mark.parametrize("shape", list(COMPLETE_SHAPES))
def test_complete_returns_a_defective_call_beside_the_good_one(shape):
    item, finish, expected = COMPLETE_SHAPES[shape]
    good = {"id": "call-0", "function": {"name": "lookup_asset", "arguments": ADDRESS}}

    result = _complete(
        {"role": "assistant", "content": None, "tool_calls": [good, item]},
        finish=finish,
    )

    assert [_shape(call) for call in result.tool_calls] == [
        ("call-0", "lookup_asset", A, None),
        expected,
    ]
    # The output limit stays visible: chat refuses every call it cut off.
    assert result.finish_reason == finish


def test_unreadable_arguments_keep_their_text_only_in_the_protected_diagnostic(
    monkeypatch,
):
    recorded: list[dict[str, Any]] = []
    original = providers.record_caught_exception

    def capture(feature, event_code, message, exc, **fields):
        recorded.append({"event_code": event_code, **fields})
        return original(feature, event_code, message, exc, **fields)

    monkeypatch.setattr(providers, "record_caught_exception", capture)
    secret = '{"address":"sensitive.example'

    result = _complete(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "lookup_asset", "arguments": secret},
                }
            ],
        }
    )

    [call] = result.tool_calls
    assert "sensitive.example" not in call.invalid_reason
    assert "sensitive.example" not in call.model_dump_json()
    [record] = [
        item
        for item in recorded
        if item["event_code"] == "providers.tool_arguments.invalid_json"
    ]
    assert "sensitive.example" in record["sensitive_detail"]
    assert "sensitive.example" not in json.dumps(record["metadata"])


def test_a_valid_call_serializes_as_before():
    result = _complete(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "lookup_asset", "arguments": ADDRESS},
                }
            ],
        }
    )

    assert result.tool_calls[0].model_dump() == {
        "id": "call-1",
        "name": "lookup_asset",
        "arguments": A,
    }


# Every native adapter reads its calls through the same rules.


def _native_config(kind: ProviderKind) -> ProviderConfig:
    return ProviderConfig(
        id=kind.value,
        kind=kind,
        base_url="https://provider.invalid",
        default_model="test-model",
        capabilities=ModelCapabilities(tools=True, strict_tools=True),
    )


def _responses(output: list[dict[str, Any]]):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "test-model",
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = OpenAIResponsesProvider(
        _native_config(ProviderKind.OPENAI_RESPONSES),
        transport=httpx.MockTransport(handler),
    )
    return asyncio.run(provider.complete(_request()))


def _anthropic(content: list[dict[str, Any]], monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "test-model",
                "stop_reason": "tool_use",
                "content": content,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    config = _native_config(ProviderKind.ANTHROPIC).model_copy(
        update={"api_key_env": "NEBULA_TEST_PROVIDER_KEY"}
    )
    provider = AnthropicProvider(config, transport=httpx.MockTransport(handler))
    return asyncio.run(provider.complete(_request()))


def _gemini(parts: list[dict[str, Any]], monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "responseId": "resp-1",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": parts},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )

    config = _native_config(ProviderKind.GEMINI).model_copy(
        update={"api_key_env": "NEBULA_TEST_PROVIDER_KEY"}
    )
    provider = GeminiProvider(config, transport=httpx.MockTransport(handler))
    return asyncio.run(provider.complete(_request()))


def _bedrock(content: list[dict[str, Any]], monkeypatch):
    class Client:
        def converse(self, **kwargs):
            del kwargs
            return {
                "output": {"message": {"role": "assistant", "content": content}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            }

    monkeypatch.setattr(providers.boto3, "client", lambda *args, **kwargs: Client())
    provider = BedrockProvider(_native_config(ProviderKind.BEDROCK))
    return asyncio.run(provider.complete(_request()))


def _responses_case(monkeypatch, calls):
    del monkeypatch
    return _responses(
        [
            {
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["arguments"]
                if isinstance(call["arguments"], str)
                else json.dumps(call["arguments"]),
            }
            for call in calls
        ]
    )


def _anthropic_case(monkeypatch, calls):
    return _anthropic(
        [
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call["arguments"],
            }
            for call in calls
        ],
        monkeypatch,
    )


def _gemini_case(monkeypatch, calls):
    return _gemini(
        [
            {
                "functionCall": {
                    "id": call["id"],
                    "name": call["name"],
                    "args": call["arguments"],
                }
            }
            for call in calls
        ],
        monkeypatch,
    )


def _bedrock_case(monkeypatch, calls):
    return _bedrock(
        [
            {
                "toolUse": {
                    "toolUseId": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
            }
            for call in calls
        ],
        monkeypatch,
    )


NATIVE_ADAPTERS = {
    "responses": _responses_case,
    "anthropic": _anthropic_case,
    "gemini": _gemini_case,
    "bedrock": _bedrock_case,
}


@pytest.mark.parametrize("adapter", list(NATIVE_ADAPTERS))
@pytest.mark.parametrize(
    "defect,expected",
    [
        (
            {"id": "", "name": "lookup_asset", "arguments": {"address": "b"}},
            ("<synthetic>", "lookup_asset", {"address": "b"}, None),
        ),
        (
            {"id": "call-2", "name": "Lookup_Asset", "arguments": {"address": "b"}},
            ("call-2", "lookup_asset", {"address": "b"}, None),
        ),
        (
            {"id": "call-2", "name": "Bash", "arguments": {"command": "id"}},
            (
                "call-2",
                INVALID,
                {},
                _reason("'Bash' is not the name of a tool"),
            ),
        ),
        (
            {"id": "call-2", "name": "", "arguments": {"address": "b"}},
            (
                "call-2",
                INVALID,
                {},
                _reason("the call did not name a tool"),
            ),
        ),
        (
            {"id": "call-2", "name": "lookup_asset", "arguments": '{"address": "b'},
            (
                "call-2",
                "lookup_asset",
                {},
                _reason("arguments were not valid JSON: Unterminated string"),
            ),
        ),
    ],
    ids=["empty-id", "mis-cased", "not-a-name", "no-name", "unterminated"],
)
def test_native_adapters_return_a_defective_call_instead_of_raising(
    monkeypatch, adapter, defect, expected
):
    good = {"id": "call-1", "name": "lookup_asset", "arguments": {"address": "a"}}

    result = NATIVE_ADAPTERS[adapter](monkeypatch, [good, defect])

    shapes = [_shape(call) for call in result.tool_calls]
    if adapter == "gemini" and expected[0] == "<synthetic>":
        # Gemini names every call Core cannot pair by id itself.
        expected = (shapes[1][0], *expected[1:])
    assert shapes == [("call-1", "lookup_asset", {"address": "a"}, None), expected]


def test_replayed_history_names_decode_like_declared_tools():
    request = _request(
        tools=[],
        tool_results=[
            ModelToolResult(
                call_id="old", name="tool_output.search", arguments={}, output="{}"
            )
        ],
    )

    result = _complete(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "tool_output_search", "arguments": "{}"},
                }
            ],
        },
        request=request,
    )

    assert [call.name for call in result.tool_calls] == ["tool_output.search"]
