"""Claude routes are told where reusable prompt prefixes end; usage counts the prompt.

OpenAI, DeepSeek, Gemini 2.5+ and most other routes reuse a repeated prompt
prefix on their own. Claude reuses one only up to an explicit breakpoint, so
the Anthropic, Bedrock and OpenRouter adapters never cached anything: every
tool step of a long turn re-billed the whole conversation at the full input
rate. The adapters now mark the tools, the system prompt, the newest message
and the operator message before the current one (at most four breakpoints),
leave every other route alone, and send OpenAI its ``prompt_cache_key``.

Anthropic and Bedrock report cache reads and writes beside ``input_tokens``,
which then counts only the uncached rest. Goal budgets, context calibration
and cost estimates read ``input_tokens`` as the prompt size, so usage is
normalized to the whole prompt, with reads and writes recorded apart.
"""

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.domain import ChatTokenUsage
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    ModelUsage,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolDefinition,
    _mark_anthropic_message,
    _openai_usage,
)

KEY_ENV = "NEBULA_PROMPT_CACHE_TEST_KEY"
SESSION = "5f0c7d3e-2b8a-4c61-9e0f-7a1d2c3b4e5f"
INSTRUCTIONS = "You are a careful analyst."
LOOKUP = ToolDefinition(
    name="lookup_asset",
    description="Look up one asset",
    input_schema={
        "type": "object",
        "properties": {"address": {"type": "string"}},
        "required": ["address"],
        "additionalProperties": False,
    },
)
FINISH = ToolDefinition(
    name="finish_response",
    description="Finish tool routing.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
EARLIER = "Is 10.0.0.0/24 in scope?"
ANSWER = "Yes, it is in scope."
CURRENT = "Look up 10.0.0.1 and 10.0.0.2."


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")


def _config(kind, flavor, model, base_url="https://provider.invalid", **options):
    return ProviderConfig(
        id=f"cache-{flavor.value}",
        kind=kind,
        flavor=flavor,
        base_url=base_url,
        default_model=model,
        api_key_env=None if kind == ProviderKind.BEDROCK else KEY_ENV,
        capabilities=ModelCapabilities(tools=True, strict_tools=True),
        options={"retry_backoff_seconds": 0, **options},
    )


def _result(index: int, provider_id: str, model: str) -> ModelToolResult:
    return ModelToolResult(
        call_id=f"toolu_{index}",
        name="lookup_asset",
        arguments={"address": f"10.0.0.{index}"},
        output={"address": f"10.0.0.{index}", "open": [22, 443]},
        response_group=f"group-{index}",
        response_text=f"Looking up 10.0.0.{index}.",
        # Thinking a Claude route signed, replayed ahead of the call.
        reasoning_state={
            "provider_id": provider_id,
            "model": model,
            "thinking_blocks": [
                {"type": "thinking", "thinking": f"step {index}", "signature": "s"}
            ],
            "reasoning_details": [
                {"type": "reasoning.text", "text": f"step {index}", "signature": "s"}
            ],
        },
    )


def _step(steps: int, provider_id: str, model: str, **update: Any) -> ModelRequest:
    """One routing request of a tool turn after ``steps`` tool results."""

    return ModelRequest(
        model=model,
        instructions=INSTRUCTIONS,
        messages=[
            ModelMessage(role="user", content=EARLIER),
            ModelMessage(role="assistant", content=ANSWER),
            ModelMessage(role="user", content=CURRENT),
        ],
        tools=[LOOKUP, FINISH],
        tool_results=[
            _result(index, provider_id, model) for index in range(1, steps + 1)
        ],
        metadata={"chat_session_id": SESSION},
    ).model_copy(update=update)


def _one_shot(model: str) -> ModelRequest:
    """A compaction-style call: one message, no tools, sent once."""

    return ModelRequest(
        model=model,
        instructions=INSTRUCTIONS,
        messages=[ModelMessage(role="user", content="Summarize these sources.")],
    )


def _strip(value: Any) -> Any:
    """The prompt a payload sends, without its cache markers.

    A marker moves as a conversation grows but is not prompt content; a string
    and one text part holding it are the same prompt.
    """

    if isinstance(value, dict):
        return {
            key: _strip(item)
            for key, item in value.items()
            if key not in {"cache_control", "cachePoint"}
        }
    if isinstance(value, list):
        # A Converse cache point is a block of its own.
        items = [item for item in map(_strip, value) if item != {}]
        if (
            len(items) == 1
            and isinstance(items[0], dict)
            and set(items[0]) == {"type", "text"}
            and items[0]["type"] == "text"
        ):
            return items[0]["text"]
        return items
    return value


def _marked(value: Any) -> list[Any]:
    """Every object in a payload that carries a cache breakpoint."""

    found: list[Any] = []
    if isinstance(value, dict):
        if "cache_control" in value or "cachePoint" in value:
            found.append(value)
        for item in value.values():
            found.extend(_marked(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_marked(item))
    return found


def _message_marked(message: dict[str, Any]) -> bool:
    return bool(_marked(message.get("content")))


# --- Anthropic Messages API ----------------------------------------------------

CLAUDE = "claude-sonnet-4-6"


class _Anthropic:
    def __init__(self, usage: dict[str, Any] | None = None) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.usage = usage or {"input_tokens": 1, "output_tokens": 1}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": CLAUDE,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": self.usage,
            },
        )

    def provider(self, model: str = CLAUDE, **options: Any) -> AnthropicProvider:
        return AnthropicProvider(
            _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC, model, **options),
            transport=httpx.MockTransport(self.handler),
        )


def _anthropic(request: ModelRequest, model: str = CLAUDE, **options: Any):
    wire = _Anthropic()
    response = asyncio.run(wire.provider(model, **options).complete(request))
    return wire.payloads[0], response


def test_anthropic_marks_tools_system_and_two_conversation_anchors():
    payload, _ = _anthropic(_step(2, "cache-anthropic", CLAUDE))

    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["tools"][0]
    assert payload["system"] == [
        {"type": "text", "text": INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}
    ]
    messages = payload["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    # The newest tool results, and the operator message before the current
    # one: the four breakpoints Claude allows.
    assert len(_marked(payload)) == 4
    assert [index for index, m in enumerate(messages) if _message_marked(m)] == [0, 6]
    assert messages[0]["content"] == [
        {"type": "text", "text": EARLIER, "cache_control": {"type": "ephemeral"}}
    ]
    assert messages[6]["content"][-1]["type"] == "tool_result"
    assert messages[6]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Replayed thinking is sent exactly as it was signed.
    for message in messages[3::2]:
        assert message["content"][0]["type"] == "thinking"
        assert "cache_control" not in message["content"][0]


def test_anthropic_breakpoints_are_deterministic_and_leave_the_prefix_growing():
    first, _ = _anthropic(_step(2, "cache-anthropic", CLAUDE))
    again, _ = _anthropic(_step(2, "cache-anthropic", CLAUDE))
    later, _ = _anthropic(_step(3, "cache-anthropic", CLAUDE))

    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    earlier_prompt, later_prompt = _strip(first), _strip(later)
    assert earlier_prompt["tools"] == later_prompt["tools"]
    assert earlier_prompt["system"] == later_prompt["system"]
    shared = earlier_prompt["messages"]
    assert later_prompt["messages"][: len(shared)] == shared
    # The anchor on the earlier operator message stays where it was, so a
    # rewrite of the current message or the replay still reads it.
    assert _message_marked(later["messages"][0])
    assert _message_marked(later["messages"][-1])


def test_anthropic_marks_the_current_message_when_a_breakpoint_is_free():
    payload, _ = _anthropic(_step(1, "cache-anthropic", CLAUDE, instructions=None))

    assert "system" not in payload
    marked = [
        index
        for index, message in enumerate(payload["messages"])
        if _message_marked(message)
    ]
    # Tools, the newest results, the earlier and the current operator message.
    assert marked == [0, 2, 4]
    assert len(_marked(payload)) == 4


def test_one_shot_requests_mark_only_their_instructions():
    """A compaction or naming call is never re-sent as a prefix.

    Marking its message would pay the cache-write premium on tokens no later
    request reads.
    """

    payload, _ = _anthropic(_one_shot(CLAUDE))

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"] == [
        {"role": "user", "content": "Summarize these sources."}
    ]


@pytest.mark.parametrize("model", ["deepseek-chat", "glm-4.6", "kimi-k2"])
def test_other_models_behind_an_anthropic_endpoint_get_no_markers(model):
    payload, _ = _anthropic(_step(2, "cache-anthropic", model), model)

    assert _marked(payload) == []
    assert payload["system"] == INSTRUCTIONS


def test_an_operator_can_switch_markers_off_per_profile():
    payload, _ = _anthropic(
        _step(2, "cache-anthropic", CLAUDE), CLAUDE, prompt_caching=False
    )

    assert _marked(payload) == []


def test_thinking_and_empty_text_blocks_never_carry_a_breakpoint():
    message = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            {"type": "redacted_thinking", "data": "abc"},
            {"type": "text", "text": ""},
        ],
    }
    original = copy.deepcopy(message)

    _mark_anthropic_message(message)

    assert [("cache_control" in block) for block in message["content"]] == [
        False,
        True,
        False,
        False,
    ]
    # The blocks it was given are copied, not edited.
    assert original["content"][1] == {
        "type": "tool_use",
        "id": "t1",
        "name": "x",
        "input": {},
    }


def test_anthropic_usage_counts_the_whole_prompt_with_reads_and_writes_apart():
    """Anthropic's ``input_tokens`` excludes cache reads and writes."""

    wire = _Anthropic(
        {
            "input_tokens": 50,
            "cache_read_input_tokens": 9_000,
            "cache_creation_input_tokens": 400,
            "output_tokens": 20,
        }
    )
    response = asyncio.run(wire.provider().complete(_step(1, "x", CLAUDE)))

    assert response.usage == ModelUsage(
        input_tokens=9_450,
        output_tokens=20,
        total_tokens=9_470,
        cached_input_tokens=9_000,
        cache_creation_input_tokens=400,
    )
    # Chat records a response's usage as ChatTokenUsage field for field.
    recorded = ChatTokenUsage.model_validate(response.usage.model_dump())
    assert recorded.cache_creation_input_tokens == 400
    # Usage saved before the field existed still reads.
    assert (
        ChatTokenUsage.model_validate(
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        ).cache_creation_input_tokens
        == 0
    )


# --- Bedrock Converse ------------------------------------------------------------


class _Converse:
    def __init__(self, monkeypatch, usage: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.usage = usage or {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}
        monkeypatch.setattr(providers.boto3, "client", lambda *a, **k: self)

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": self.usage,
        }


def _bedrock(monkeypatch, request: ModelRequest, model: str, **options: Any):
    converse = _Converse(monkeypatch)
    provider = BedrockProvider(
        _config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK, model, **options)
    )
    asyncio.run(provider.complete(request.model_copy(update={"model": model})))
    return converse.calls[0]


CACHE_POINT = {"cachePoint": {"type": "default"}}


@pytest.mark.parametrize(
    "model",
    [
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-opus-5",
    ],
)
def test_bedrock_claude_gets_cache_points_in_the_same_places(monkeypatch, model):
    sent = _bedrock(monkeypatch, _step(2, "cache-bedrock", model), model)

    assert sent["toolConfig"]["tools"][-1] == CACHE_POINT
    assert sent["system"] == [{"text": INSTRUCTIONS}, CACHE_POINT]
    messages = sent["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[0]["content"] == [{"text": EARLIER}, CACHE_POINT]
    assert messages[-1]["content"][-1] == CACHE_POINT
    assert "toolResult" in messages[-1]["content"][-2]
    assert len(_marked(sent)) == 4
    # Marked before same-role messages merge: nothing else moved.
    assert [block for block in messages[2]["content"]] == [{"text": CURRENT}]


@pytest.mark.parametrize(
    "model",
    [
        # Nova caches every text prompt implicitly and refuses tool cache points.
        "amazon.nova-pro-v1:0",
        "us.amazon.nova-lite-v1:0",
        "meta.llama3-3-70b-instruct-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
        # An application inference profile hides the model family.
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc",
    ],
)
def test_bedrock_models_without_known_cache_points_are_left_alone(monkeypatch, model):
    sent = _bedrock(monkeypatch, _step(2, "cache-bedrock", model), model)

    assert _marked(sent) == []


def test_bedrock_respects_the_profile_switch(monkeypatch):
    model = "anthropic.claude-sonnet-4-5-20250929-v1:0"
    sent = _bedrock(
        monkeypatch, _step(1, "cache-bedrock", model), model, prompt_caching=False
    )

    assert _marked(sent) == []


def test_bedrock_usage_counts_cache_reads_and_writes_in_the_prompt(monkeypatch):
    _Converse(
        monkeypatch,
        {
            "inputTokens": 50,
            "cacheReadInputTokens": 9_000,
            "cacheWriteInputTokens": 400,
            "outputTokens": 20,
            "totalTokens": 70,
        },
    )
    model = "anthropic.claude-sonnet-4-5-20250929-v1:0"
    provider = BedrockProvider(
        _config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK, model)
    )

    response = asyncio.run(provider.complete(_step(1, "x", model)))

    assert response.usage == ModelUsage(
        input_tokens=9_450,
        output_tokens=20,
        total_tokens=9_470,
        cached_input_tokens=9_000,
        cache_creation_input_tokens=400,
    )


# --- OpenRouter and other OpenAI-compatible routes ------------------------------


def _chat(flavor, model, base_url="https://openrouter.ai/api/v1", **options):
    return OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, flavor, model, base_url, **options)
    )


def test_openrouter_claude_gets_content_part_breakpoints():
    model = "anthropic/claude-sonnet-4.6"
    provider = _chat(ProviderFlavor.OPENROUTER, model)

    payload = provider._payload(_step(2, provider.config.id, model), model)

    messages = payload["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert messages[0]["content"] == [
        {"type": "text", "text": INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}
    ]
    # OpenRouter documents none on tool definitions; the system breakpoint
    # covers them.
    assert _marked(payload["tools"]) == []
    marked = [
        index for index, message in enumerate(messages) if _message_marked(message)
    ]
    # System, the earlier operator message, the current operator message and
    # the newest tool result.
    assert marked == [0, 1, 3, 7]
    assert len(_marked(payload)) == 4
    assert messages[7]["content"] == [
        {
            "type": "text",
            "text": messages[7]["content"][0]["text"],
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Assistant turns, their calls and their signed reasoning are untouched.
    for message in messages[2::2][:3]:
        assert not _message_marked(message)
    # Identical requests, identical bytes; a later step extends the prompt.
    assert payload == provider._payload(_step(2, provider.config.id, model), model)
    later = provider._payload(_step(3, provider.config.id, model), model)
    assert _strip(later)["messages"][: len(messages)] == _strip(payload)["messages"]


def test_openrouter_alias_of_a_claude_model_is_marked():
    model = "~anthropic/claude-sonnet-latest"
    provider = _chat(ProviderFlavor.OPENROUTER, model)

    assert _marked(provider._payload(_step(1, provider.config.id, model), model))


@pytest.mark.parametrize(
    ("flavor", "model", "base_url"),
    [
        # Implicit caching with no write or storage charge.
        (ProviderFlavor.OPENROUTER, "google/gemini-2.5-pro", None),
        (ProviderFlavor.OPENROUTER, "openai/gpt-5.4", None),
        (ProviderFlavor.OPENROUTER, "deepseek/deepseek-v4.1-flash", None),
        # A strict OpenAI-compatible server answers 400 to unknown fields.
        (ProviderFlavor.CUSTOM, "claude-sonnet-4-6", "https://llm.example.com/v1"),
        (
            ProviderFlavor.LITELLM,
            "anthropic/claude-sonnet-4.6",
            "https://llm.example.com",
        ),
    ],
)
def test_routes_that_cache_on_their_own_are_sent_no_cache_fields(
    flavor, model, base_url
):
    provider = _chat(flavor, model, base_url or "https://openrouter.ai/api/v1")

    payload = provider._payload(_step(2, provider.config.id, model), model)

    assert _marked(payload) == []
    assert "prompt_cache_key" not in payload


def test_openrouter_one_shot_requests_mark_only_the_system_prompt():
    model = "anthropic/claude-haiku-4.5"
    provider = _chat(ProviderFlavor.OPENROUTER, model)

    payload = provider._payload(_one_shot(model), model)

    assert [_message_marked(message) for message in payload["messages"]] == [
        True,
        False,
    ]


def test_openrouter_usage_records_claude_cache_writes():
    usage = _openai_usage(
        {
            "prompt_tokens": 9_275,
            "completion_tokens": 16,
            "total_tokens": 9_291,
            "prompt_tokens_details": {
                "cached_tokens": 8_864,
                "cache_write_tokens": 406,
            },
        }
    )

    # ``prompt_tokens`` already counts the cached and written tokens.
    assert (usage.input_tokens, usage.total_tokens) == (9_275, 9_291)
    assert usage.cached_input_tokens == 8_864
    assert usage.cache_creation_input_tokens == 406


# --- OpenAI prompt_cache_key -----------------------------------------------------


def _responses(flavor=ProviderFlavor.OPENAI, **options):
    return OpenAIResponsesProvider(
        _config(
            ProviderKind.OPENAI_RESPONSES,
            flavor,
            "gpt-5.4-mini",
            "https://api.openai.com",
            **options,
        )
    )


def test_openai_gets_the_chat_session_as_its_prompt_cache_key():
    request = _step(1, "cache-openai", "gpt-5.4-mini")

    responses = _responses()._payload(request, "gpt-5.4-mini")
    chat = _chat(
        ProviderFlavor.OPENAI, "gpt-5.4-mini", "https://api.openai.com/v1"
    )._payload(request, "gpt-5.4-mini")

    assert responses["prompt_cache_key"] == SESSION
    assert chat["prompt_cache_key"] == SESSION
    # OpenAI caches implicitly: nothing else changes.
    assert _marked(responses) == [] and _marked(chat) == []


def test_prompt_cache_key_is_bounded_and_only_sent_with_a_session():
    long_session = "s" * 150
    request = _step(1, "x", "gpt-5.4-mini", metadata={"chat_session_id": long_session})

    key = _responses()._payload(request, "gpt-5.4-mini")["prompt_cache_key"]

    assert len(key) == 64 and long_session not in key
    assert "prompt_cache_key" not in _responses()._payload(
        _step(1, "x", "gpt-5.4-mini", metadata={}), "gpt-5.4-mini"
    )
    # Azure and switched-off profiles are not sent it.
    for provider in (
        _responses(ProviderFlavor.AZURE_OPENAI),
        _responses(prompt_caching=False),
    ):
        assert "prompt_cache_key" not in provider._payload(request, "gpt-5.4-mini")


def test_responses_usage_reads_its_cache_hits():
    """The Responses adapter dropped ``input_tokens_details`` entirely."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-5.4-mini",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {
                    "input_tokens": 2_634,
                    "input_tokens_details": {
                        "cached_tokens": 2_432,
                        "cache_write_tokens": 0,
                    },
                    "output_tokens": 2,
                    "total_tokens": 2_636,
                },
            },
        )

    provider = OpenAIResponsesProvider(
        _config(
            ProviderKind.OPENAI_RESPONSES,
            ProviderFlavor.OPENAI,
            "gpt-5.4-mini",
            "https://api.openai.com",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(provider.complete(_step(0, "x", "gpt-5.4-mini", tools=[])))

    assert response.usage == ModelUsage(
        input_tokens=2_634,
        output_tokens=2,
        total_tokens=2_636,
        cached_input_tokens=2_432,
    )
