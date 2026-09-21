"""Chat Completions request parameters each OpenAI-compatible route accepts.

Covers what ``OpenAICompatibleProvider._payload`` sends for OpenAI reasoning
models (token ceiling name, sampling, strict tools), for OpenRouter routes
under ``require_parameters``, and for Mistral's tool-call id format.
"""

import asyncio
import json
import re

import httpx
import pytest

from nebula.v3.domain import ContextMemory
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolChoice,
    ToolDefinition,
    _openai_strict_schema,
)

STRICT_TOOL = ToolDefinition(
    name="lookup_asset",
    description="Look up one asset",
    input_schema={
        "type": "object",
        "properties": {"address": {"type": "string"}},
        "required": ["address"],
        "additionalProperties": False,
    },
)

# An MCP-style schema: an optional property with a default. OpenAI strict
# mode rejects it, so the whole request would fail with strict: true.
LOOSE_TOOL = ToolDefinition(
    name="run_scan",
    description="Scan one target",
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "ports": {"type": "string", "default": "1-1024"},
        },
        "required": ["target"],
    },
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_MISTRAL_ID = re.compile(r"[a-zA-Z0-9]{9}")


def _provider(
    flavor: ProviderFlavor,
    model: str,
    *,
    model_parameters: dict[str, list[str]] | None = None,
    transport: httpx.MockTransport | None = None,
) -> OpenAICompatibleProvider:
    local = flavor in {ProviderFlavor.OLLAMA, ProviderFlavor.VLLM}
    return OpenAICompatibleProvider(
        ProviderConfig(
            id=f"{flavor.value}-profile",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=flavor,
            base_url=(
                "http://127.0.0.1:8001/v1" if local else "https://provider.invalid/v1"
            ),
            default_model=model,
            local=local,
            capabilities=ModelCapabilities(
                tools=True, strict_tools=True, structured_output=True
            ),
            model_parameters=model_parameters or {},
        ),
        **({"transport": transport} if transport else {}),
    )


def _payload(
    flavor: ProviderFlavor,
    model: str,
    *,
    model_parameters: dict[str, list[str]] | None = None,
    **fields,
) -> dict:
    provider = _provider(flavor, model, model_parameters=model_parameters)
    request = ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Inspect the asset")],
        **fields,
    )
    return provider._payload(request, provider.require(request))


def _replayed_ids(payload: dict) -> list[tuple[str, str]]:
    """(assistant tool_call id, tool result tool_call_id) per replayed call."""

    messages = payload["messages"]
    pairs = []
    for index, message in enumerate(messages):
        if message["role"] == "assistant" and message.get("tool_calls"):
            result = messages[index + 1]
            assert result["role"] == "tool"
            pairs.append((message["tool_calls"][0]["id"], result["tool_call_id"]))
    return pairs


# --- OAC-13: OpenAI reasoning models over Chat Completions ------------------


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.MICROSOFT_FOUNDRY, "gpt-5-mini"),
        (ProviderFlavor.AZURE_OPENAI, "o4-mini"),
        (ProviderFlavor.OPENAI, "o3-mini"),
        (ProviderFlavor.LITELLM, "openai/gpt-5"),
        (ProviderFlavor.CUSTOM, "gpt-5.1"),
    ],
)
def test_chat_completions_reasoning_model_uses_max_completion_tokens_without_temperature(
    flavor, model
):
    payload = _payload(
        flavor,
        model,
        max_output_tokens=256,
        temperature=0,
        reasoning_effort="low",
    )

    # OpenAI rejects max_tokens and any temperature for the o-series and gpt-5
    # with HTTP 400; the Responses adapter already omits temperature for them.
    assert payload["max_completion_tokens"] == 256
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert payload["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.OPENAI, "gpt-4.1"),
        (ProviderFlavor.MICROSOFT_FOUNDRY, "gpt-4o-mini"),
        (ProviderFlavor.DEEPSEEK, "deepseek-reasoner"),
        (ProviderFlavor.VLLM, "Qwen/Qwen3-32B"),
        (ProviderFlavor.OLLAMA, "gpt-oss:20b"),
        (ProviderFlavor.MISTRAL, "mistral-large-latest"),
    ],
)
def test_chat_completions_other_models_keep_max_tokens_and_temperature(flavor, model):
    payload = _payload(flavor, model, max_output_tokens=256, temperature=0.2)

    assert payload["max_tokens"] == 256
    assert "max_completion_tokens" not in payload
    assert payload["temperature"] == 0.2


def test_openrouter_openai_reasoning_model_drops_temperature_keeps_max_tokens():
    payload = _payload(
        ProviderFlavor.OPENROUTER,
        "openai/gpt-5-mini",
        max_output_tokens=256,
        temperature=0,
    )

    # OpenRouter's normalized ceiling is max_tokens (the name its routes
    # advertise, translated upstream); the sampling parameter is not taken.
    assert payload["max_tokens"] == 256
    assert "max_completion_tokens" not in payload
    assert "temperature" not in payload


def test_foundry_reasoning_request_on_the_wire():
    observed = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Asset scan"},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    provider = _provider(
        ProviderFlavor.MICROSOFT_FOUNDRY,
        "gpt-5-mini",
        transport=httpx.MockTransport(handler),
    )
    # Session naming sends exactly this: a small ceiling at temperature 0.
    response = asyncio.run(
        provider.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content="Name this chat")],
                tools=[LOOSE_TOOL],
                max_output_tokens=256,
                temperature=0,
            )
        )
    )

    sent = observed["payload"]
    assert sent["max_completion_tokens"] == 256
    assert "max_tokens" not in sent
    assert "temperature" not in sent
    assert sent["tools"][0]["function"]["strict"] is False
    assert response.text == "Asset scan"


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.OPENAI, "gpt-4.1"),
        (ProviderFlavor.MICROSOFT_FOUNDRY, "gpt-5-mini"),
        (ProviderFlavor.OPENROUTER, "openai/gpt-5-mini"),
    ],
)
def test_chat_completions_strict_only_for_strict_schemas(flavor, model):
    # Only OpenAI routes are sent strict at all; the others are covered by
    # test_deepseek_glm_compat.py::test_strict_is_not_sent_outside_openai_routes.
    payload = _payload(flavor, model, tools=[STRICT_TOOL, LOOSE_TOOL])

    functions = {
        tool["function"]["name"]: tool["function"] for tool in payload["tools"]
    }
    assert functions["lookup_asset"]["strict"] is True
    # Strict is request-wide: one schema strict mode rejects fails every tool
    # in the call, so a non-strict schema is sent without it.
    assert functions["run_scan"]["strict"] is False
    assert functions["run_scan"]["parameters"] == LOOSE_TOOL.input_schema


def test_chat_completions_honours_a_tool_that_opts_out_of_strict():
    opted_out = STRICT_TOOL.model_copy(update={"strict": False})

    payload = _payload(ProviderFlavor.OPENAI, "gpt-4.1", tools=[opted_out])

    assert payload["tools"][0]["function"]["strict"] is False


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.OPENAI, "gpt-5.2"),
        (ProviderFlavor.MICROSOFT_FOUNDRY, "gpt-4.1"),
        (ProviderFlavor.OPENROUTER, "openai/gpt-5-mini"),
    ],
)
def test_chat_completions_response_format_is_strict_only_for_strict_schemas(
    flavor, model
):
    # Context compaction sends ContextMemory's schema: optional properties
    # with defaults, which strict mode rejects with HTTP 400, so every
    # compaction on a strict-honouring route failed.
    memory_schema = ContextMemory.model_json_schema()
    assert not _openai_strict_schema(memory_schema)

    compaction = _payload(flavor, model, response_schema=memory_schema)
    strict = _payload(flavor, model, response_schema=RESPONSE_SCHEMA)

    assert compaction["response_format"]["json_schema"]["strict"] is False
    assert compaction["response_format"]["json_schema"]["schema"] == memory_schema
    assert strict["response_format"]["json_schema"]["strict"] is True


# --- OAC-14: OpenRouter require_parameters -----------------------------------


def _openrouter_tool_payload(supported: list[str], **fields) -> dict:
    return _payload(
        ProviderFlavor.OPENROUTER,
        "vendor/model",
        model_parameters={"vendor/model": supported},
        tools=[STRICT_TOOL],
        tool_choice=ToolChoice.REQUIRED,
        max_output_tokens=512,
        **fields,
    )


def test_openrouter_prunes_unadvertised_tool_choice_under_require_parameters():
    payload = _openrouter_tool_payload(["tools", "max_tokens", "temperature"])

    assert payload["provider"] == {"require_parameters": True}
    # With require_parameters, an unadvertised parameter leaves no eligible
    # endpoint (OpenRouter 404 "No endpoints found that can handle the
    # requested parameters").
    assert "tool_choice" not in payload
    assert payload["max_tokens"] == 512
    assert payload["tools"][0]["function"]["name"] == "lookup_asset"


def test_openrouter_prunes_unadvertised_max_tokens_under_require_parameters():
    payload = _openrouter_tool_payload(["tools", "tool_choice"])

    assert payload["provider"] == {"require_parameters": True}
    assert "max_tokens" not in payload
    assert payload["tool_choice"] == "required"


def test_openrouter_prunes_unadvertised_structured_outputs_under_require_parameters():
    payload = _openrouter_tool_payload(
        ["tools", "tool_choice", "max_tokens"], response_schema=RESPONSE_SCHEMA
    )

    assert "response_format" not in payload
    assert payload["tool_choice"] == "required"

    # response_format alone is JSON mode, not json_schema structured output:
    # the route gets json_object and the schema travels in the instructions.
    json_mode_only = _openrouter_tool_payload(
        ["tools", "tool_choice", "max_tokens", "response_format"],
        response_schema=RESPONSE_SCHEMA,
    )
    assert json_mode_only["response_format"] == {"type": "json_object"}

    advertised = _openrouter_tool_payload(
        ["tools", "tool_choice", "max_tokens", "response_format", "structured_outputs"],
        response_schema=RESPONSE_SCHEMA,
    )
    assert advertised["response_format"]["json_schema"]["schema"] == RESPONSE_SCHEMA


def test_openrouter_keeps_advertised_and_unknown_route_parameters():
    advertised = _openrouter_tool_payload(["tools", "tool_choice", "max_tokens"])
    assert advertised["tool_choice"] == "required"
    assert advertised["max_tokens"] == 512

    # No catalog entry for the model: nothing is known to be unsupported, so
    # the tool contract and the ceiling are still sent.
    unknown = _payload(
        ProviderFlavor.OPENROUTER,
        "vendor/model",
        tools=[STRICT_TOOL],
        tool_choice=ToolChoice.REQUIRED,
        max_output_tokens=512,
        response_schema=RESPONSE_SCHEMA,
    )
    assert unknown["tool_choice"] == "required"
    assert unknown["max_tokens"] == 512
    assert "response_format" in unknown

    # Without tools there is no require_parameters, so nothing is pruned.
    chat = _payload(
        ProviderFlavor.OPENROUTER,
        "vendor/model",
        model_parameters={"vendor/model": ["tools"]},
        max_output_tokens=512,
    )
    assert "provider" not in chat
    assert chat["max_tokens"] == 512


# --- OAC-16: Mistral tool-call ids -------------------------------------------


_FOREIGN_HISTORY = [
    ModelToolResult(
        call_id="dsml-0123456789abcdef01234567",
        name="lookup_asset",
        arguments={"address": "a"},
        output="ok",
    ),
    ModelToolResult(
        call_id="call_7f2a9c1e5b3d4a8f",
        name="lookup_asset",
        arguments={"address": "b"},
        output={"ok": True},
    ),
    ModelToolResult(
        call_id="call_7f2a9c1e5b3d4a8e",
        name="lookup_asset",
        arguments={"address": "c"},
        output="ok",
    ),
    ModelToolResult(
        call_id="a1B2c3D4e",
        name="lookup_asset",
        arguments={"address": "d"},
        output="ok",
    ),
]


@pytest.mark.parametrize(
    ("flavor", "model"),
    [
        (ProviderFlavor.MISTRAL, "mistral-large-latest"),
        (ProviderFlavor.MISTRAL, "open-model"),
        (ProviderFlavor.VLLM, "mistralai/Mistral-Small-3.1-24B-Instruct-2503"),
        (ProviderFlavor.OPENROUTER, "mistralai/devstral-small"),
        (ProviderFlavor.CUSTOM, "codestral-latest"),
    ],
)
def test_mistral_replayed_tool_ids_are_nine_alphanumerics(flavor, model):
    payload = _payload(
        flavor, model, tools=[STRICT_TOOL], tool_results=_FOREIGN_HISTORY
    )

    pairs = _replayed_ids(payload)
    assert len(pairs) == len(_FOREIGN_HISTORY)
    for call_id, result_id in pairs:
        assert _MISTRAL_ID.fullmatch(call_id), call_id
        # The call and its result must still pair up.
        assert call_id == result_id
    # Distinct source ids stay distinct, even ones sharing a long prefix.
    assert len({call_id for call_id, _ in pairs}) == len(pairs)
    # An id Mistral issued itself round-trips unchanged.
    assert pairs[-1][0] == "a1B2c3D4e"
    # The mapping is deterministic, so every hop of a turn replays the same ids.
    again = _payload(flavor, model, tools=[STRICT_TOOL], tool_results=_FOREIGN_HISTORY)
    assert _replayed_ids(again) == pairs


def test_non_mistral_routes_replay_tool_ids_verbatim():
    payload = _payload(
        ProviderFlavor.OPENAI,
        "gpt-4.1",
        tools=[STRICT_TOOL],
        tool_results=_FOREIGN_HISTORY,
    )

    assert _replayed_ids(payload) == [
        (result.call_id, result.call_id) for result in _FOREIGN_HISTORY
    ]
