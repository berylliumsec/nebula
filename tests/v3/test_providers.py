import asyncio
import re
import json

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.model_catalog import openrouter_models
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderError,
    ProviderContextLengthError,
    ProviderOverloadedError,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ProviderRegistry,
    RetryPolicy,
    StreamEventType,
    ToolDefinition,
    UnsupportedCapability,
    config_from_catalog,
    retry_policy,
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


def _config(kind, *, capabilities=None, local=False, residency=None):
    return ProviderConfig(
        id=kind.value,
        kind=kind,
        base_url="https://provider.invalid",
        default_model="test-model",
        local=local,
        data_residency=residency,
        capabilities=capabilities or ModelCapabilities(),
    )


def test_openai_responses_translates_flattened_tools_and_parses_calls():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "model": "test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Ready."}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup_asset",
                        "arguments": '{"address":"10.0.0.8"}',
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    provider = OpenAIResponsesProvider(
        _config(
            ProviderKind.OPENAI_RESPONSES,
            capabilities=ModelCapabilities(
                tools=True, strict_tools=True, structured_output=True
            ),
        ),
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        provider.complete(
            ModelRequest(
                instructions="Use structured evidence.",
                messages=[ModelMessage(role="user", content="Inspect the asset")],
                tools=[TOOL],
                response_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                parallel_tool_calls=True,
                metadata={"engagement": "eng-1"},
            )
        )
    )

    assert observed["path"] == "/v1/responses"
    payload = observed["payload"]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup_asset",
            "description": "Look up one asset",
            "parameters": TOOL.input_schema,
            "strict": True,
        }
    ]
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert payload["parallel_tool_calls"] is True
    assert response.text == "Ready."
    assert response.tool_calls[0].model_dump() == {
        "id": "call_1",
        "name": "lookup_asset",
        "arguments": {"address": "10.0.0.8"},
    }
    assert response.usage.total_tokens == 8


def test_openai_compatible_uses_chat_completions_shape():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(path=request.url.path, payload=json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "model": "local-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "tool_1",
                                    "function": {
                                        "name": "lookup_asset",
                                        "arguments": {"address": "10.0.0.9"},
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="local",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:8001/",
            default_model="local-model",
            local=True,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.complete(
            ModelRequest(
                instructions="Stay in scope.",
                messages=[ModelMessage(role="user", content="continue")],
                tools=[TOOL],
            )
        )
    )

    assert observed["path"] == "/v1/chat/completions"
    assert observed["payload"]["messages"][0] == {
        "role": "system",
        "content": "Stay in scope.",
    }
    function = observed["payload"]["tools"][0]["function"]
    assert function["name"] == "lookup_asset"
    assert function["strict"] is True
    assert result.tool_calls[0].arguments == {"address": "10.0.0.9"}


def test_openai_compatible_reads_reasoning_when_content_is_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "id": "chat_reason",
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": None,
                            "reasoning_content": "GOAL_SEEN SKILL_MARKER_FLASH",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 6,
                    "total_tokens": 14,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4-flash",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="continue")])
        )
    )
    assert result.text == ""
    assert result.reasoning == "GOAL_SEEN SKILL_MARKER_FLASH"


def test_openai_compatible_keeps_reasoning_out_of_the_reply():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chat_split",
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "FLASH_OK",
                            # OpenRouter mirrors one thought across both channels.
                            "reasoning": "Private chain of thought.",
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "text": "Private chain of thought.",
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4-flash",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="continue")])
        )
    )
    assert observed["payload"]["reasoning"] == {"exclude": False}
    # Oversized prompts must fail loudly instead of losing their middle.
    assert observed["payload"]["plugins"] == [
        {"id": "context-compression", "enabled": False}
    ]
    assert result.text == "FLASH_OK"
    assert result.reasoning == "Private chain of thought."


def test_openai_compatible_reads_reasoning_details_without_a_plain_channel():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "id": "chat_details",
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "FLASH_OK",
                            "reasoning_details": [
                                {"type": "reasoning.encrypted", "data": "opaque"},
                                {"type": "reasoning.text", "text": "Consider it."},
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4-flash",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="continue")])
        )
    )
    assert result.text == "FLASH_OK"
    assert result.reasoning == "Consider it."


@pytest.mark.parametrize(
    "tool_call,detail",
    [
        (
            {
                "id": "tool_1",
                "function": {"name": "lookup_asset", "arguments": '{"address":'},
            },
            "malformed tool arguments",
        ),
        (
            {
                "id": "",
                "function": {
                    "name": "lookup_asset",
                    "arguments": {"address": "10.0.0.9"},
                },
            },
            "malformed tool call",
        ),
    ],
)
def test_openai_compatible_rejects_partial_or_unidentified_tool_calls(
    tool_call, detail
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chat_bad",
                "model": "local-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": None, "tool_calls": [tool_call]},
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="local",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:8001/",
            default_model="local-model",
            local=True,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderError, match=detail):
        asyncio.run(
            provider.complete(
                ModelRequest(
                    messages=[ModelMessage(role="user", content="continue")],
                    tools=[TOOL],
                )
            )
        )


@pytest.mark.parametrize(
    "provider_class,kind",
    [
        (AnthropicProvider, ProviderKind.ANTHROPIC),
        (GeminiProvider, ProviderKind.GEMINI),
    ],
)
def test_native_providers_translate_tool_definitions(monkeypatch, provider_class, kind):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret-not-in-payload")
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(path=request.url.path, payload=json.loads(request.content))
        if kind == ProviderKind.ANTHROPIC:
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "model": "test-model",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "use_1",
                            "name": "lookup_asset",
                            "input": {"address": "10.0.0.1"},
                        }
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "gemini_1",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "id": "fc_1",
                                        "name": "lookup_asset",
                                        "args": {"address": "10.0.0.1"},
                                    }
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {"totalTokenCount": 3},
            },
        )

    provider = provider_class(
        ProviderConfig(
            **_config(
                kind,
                capabilities=ModelCapabilities(tools=True, strict_tools=True),
            ).model_dump(exclude={"api_key_env"}),
            api_key_env="NEBULA_TEST_PROVIDER_KEY",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content="inspect")],
                tools=[TOOL],
            )
        )
    )

    assert result.tool_calls[0].name == "lookup_asset"
    serialized = json.dumps(observed["payload"])
    assert "secret-not-in-payload" not in serialized
    if kind == ProviderKind.ANTHROPIC:
        assert observed["payload"]["tools"][0]["input_schema"] == TOOL.input_schema
    else:
        declaration = observed["payload"]["tools"][0]["functionDeclarations"][0]
        assert declaration["parameters"] == TOOL.input_schema


def test_anthropic_health_discovers_models_without_a_configured_default(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(path=request.url.path, api_key=request.headers.get("x-api-key"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-opus-4-1"},
                    {"id": "claude-sonnet-4-5"},
                    {"display_name": "invalid"},
                ]
            },
        )

    provider = AnthropicProvider(
        ProviderConfig(
            id="anthropic",
            kind=ProviderKind.ANTHROPIC,
            flavor=ProviderFlavor.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key_env="NEBULA_TEST_PROVIDER_KEY",
        ),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert observed == {"path": "/v1/models", "api_key": "secret"}
    assert health.healthy is True
    assert health.models == ["claude-opus-4-1", "claude-sonnet-4-5"]


def test_capability_checks_and_registry_routing_are_explicit():
    cloud = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, residency="us")
    )
    local = OpenAICompatibleProvider(
        ProviderConfig(
            id="local",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:11434",
            default_model="local-model",
            local=True,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        )
    )
    registry = ProviderRegistry()
    registry.register(cloud)
    registry.register(local)

    assert registry.select(local_only=True, required=["tools"]) is local
    assert registry.select(residency="us") is cloud
    with pytest.raises(UnsupportedCapability, match="no provider"):
        registry.select(local_only=True, required=["vision"])
    with pytest.raises(UnsupportedCapability, match="strict_tools"):
        asyncio.run(
            cloud.complete(
                ModelRequest(
                    messages=[ModelMessage(role="user", content="unsafe")],
                    tools=[TOOL],
                )
            )
        )


def test_vllm_is_an_explicit_local_openai_compatible_provider():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "model": "served-model",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    config = config_from_catalog(
        provider_id="local-vllm",
        flavor=ProviderFlavor.VLLM,
        default_model="served-model",
        capabilities=ModelCapabilities(),
    )
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
    result = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="hello")])
        )
    )

    assert config.local is True
    assert config.kind == ProviderKind.OPENAI_COMPATIBLE
    assert config.base_url == "http://127.0.0.1:8000/v1"
    assert observed["path"] == "/v1/chat/completions"
    assert result.text == "ok"


def test_orcarouter_catalog_entry_maps_to_openai_compatible_wire():
    config = config_from_catalog(
        provider_id="orcarouter",
        flavor=ProviderFlavor.ORCAROUTER,
        default_model="openai/gpt-4o",
        capabilities=ModelCapabilities(),
    )

    assert config.kind == ProviderKind.OPENAI_COMPATIBLE
    assert config.flavor == ProviderFlavor.ORCAROUTER
    assert config.base_url == "https://api.orcarouter.ai/v1"
    assert config.api_key_env == "ORCAROUTER_API_KEY"
    assert config.local is False


def test_orcarouter_openai_compatible_chat_round_trip():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-orca",
                "object": "chat.completion",
                "model": "openai/gpt-4o",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="orcarouter",
            flavor=ProviderFlavor.ORCAROUTER,
            default_model="openai/gpt-4o",
            capabilities=ModelCapabilities(),
            api_key_value="test-orca-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="hello")])
        )
    )

    assert result.text == "ok"


def test_vllm_discovers_served_models_from_the_runtime():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "security-model", "object": "model"},
                    {"id": "vision-model", "object": "model"},
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="vllm-discovery",
            flavor=ProviderFlavor.VLLM,
        ),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["security-model", "vision-model"]


def test_openrouter_discovers_account_models_with_bounded_metadata():
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        if request.url.path == "/api/v1/key":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit_remaining": 74.5,
                        "expires_at": "2027-12-31T23:59:59Z",
                    }
                },
            )
        if request.url.path == "/api/v1/providers":
            return httpx.Response(
                200, json={"data": [{"name": "Anthropic", "slug": "anthropic"}]}
            )
        assert request.url.path == "/api/v1/models/user"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-sonnet-4.5",
                        "name": "Claude Sonnet 4.5",
                        "description": "A capable model",
                        "canonical_slug": "anthropic/claude-sonnet-4.5",
                        "context_length": 200_000,
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                        "supported_parameters": ["tools", "max_tokens"],
                        "top_provider": {
                            "max_completion_tokens": 32_000,
                            "context_length": 131_072,
                        },
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        "expiration_date": "2027-06-30",
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-discovery",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert observed == ["/api/v1/key", "/api/v1/models/user", "/api/v1/providers"]
    assert [item.slug for item in health.upstream_providers] == ["anthropic"]
    assert health.credential_verified is True
    assert health.catalog_source == "openrouter:/models/user"
    assert health.key_limit_remaining == 74.5
    assert health.key_expires_at == "2027-12-31T23:59:59Z"
    assert health.models == ["anthropic/claude-sonnet-4.5"]
    descriptor = health.model_descriptors[0]
    assert descriptor.name == "Claude Sonnet 4.5"
    assert descriptor.context_window == 200_000
    assert descriptor.max_output_tokens == 32_000
    assert descriptor.primary_route_context_window == 131_072
    assert descriptor.pricing["prompt"] == "0.000003"
    assert descriptor.expiration_date == "2027-06-30"


def _alias_catalog_handler(public: httpx.Response):
    """Account catalog with one alias, plus whatever the public catalog answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/v1/providers":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/api/v1/models":
            return public
        assert request.url.path == "/api/v1/models/user"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "~author/family-latest",
                        "name": "Author: Family Latest",
                        "context_length": 1_048_576,
                    },
                    {
                        "id": "author/model-a",
                        "name": "Author: Model A",
                        "context_length": 1_048_576,
                    },
                ]
            },
        )

    return handler


def test_openrouter_health_fills_alias_targets_from_the_public_catalog():
    # /models/user describes aliases without alias_target; only /models carries it.
    public = httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "~author/family-latest",
                    "name": "Author: Family Latest",
                    "context_length": 1_048_576,
                    "alias_target": {"name": "Model A", "slug": "author/model-a"},
                },
                {
                    "id": "author/model-b",
                    "name": "Author: Model B",
                    "context_length": 1_048_576,
                },
            ]
        },
    )
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-alias-targets",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(_alias_catalog_handler(public)),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    # The public catalog fills one field; it never widens account membership.
    assert health.models == ["~author/family-latest", "author/model-a"]
    targets = {item.id: item.alias_target for item in health.model_descriptors}
    assert targets == {
        "~author/family-latest": "author/model-a",
        "author/model-a": None,
    }


def test_openrouter_health_survives_an_unreadable_public_catalog():
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-alias-targets-down",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(
            _alias_catalog_handler(httpx.Response(500, json={"error": "down"}))
        ),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["~author/family-latest", "author/model-a"]
    # Without a target the alias keeps the conservative cap; discovery fails closed.
    assert all(item.alias_target is None for item in health.model_descriptors)


def test_openrouter_health_retries_a_refused_connection_before_recovering():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if request.url.path == "/api/v1/key" and attempts.count(request.url.path) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _alias_catalog_handler(httpx.Response(200, json={"data": []}))(request)

    config = config_from_catalog(
        provider_id="openrouter-health-retry",
        flavor=ProviderFlavor.OPENROUTER,
        api_key_value="test-key",
        options={"retry_attempts": 2, "retry_backoff_seconds": 0},
    )
    provider = OpenAICompatibleProvider(
        config,
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert attempts.count("/api/v1/key") == 2


def test_openai_compatible_health_retries_a_refused_connection_before_recovering():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    provider = OpenAICompatibleProvider(
        _retrying_config("compatible-health-retry", retry_attempts=2),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["model-a"]
    assert attempts == 2


def test_openrouter_health_reports_exhausted_connection_retries():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection refused", request=request)

    config = config_from_catalog(
        provider_id="openrouter-health-retry-exhausted",
        flavor=ProviderFlavor.OPENROUTER,
        api_key_value="test-key",
        options={"retry_attempts": 2, "retry_backoff_seconds": 0},
    )
    provider = OpenAICompatibleProvider(
        config,
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is False
    assert attempts == 2
    assert health.detail == (
        "OpenRouter model discovery failed after bounded retries. "
        "Check the connection and refresh."
    )


def test_openrouter_discovery_does_not_fall_back_to_public_catalog():
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        return httpx.Response(403, json={"error": {"message": "denied"}})

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-discovery",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is False
    assert observed == ["/api/v1/key"]
    assert health.credential_verified is False
    assert "HTTP 403" in (health.detail or "")


def test_openrouter_verified_key_does_not_hide_catalog_failure_or_use_public_models():
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"limit_remaining": None}})
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-discovery",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    health = asyncio.run(provider.health())

    assert health.healthy is False
    assert health.credential_verified is True
    assert health.catalog_source == "openrouter:/models/user"
    assert observed == ["/api/v1/key", "/api/v1/models/user"]
    assert "HTTP 429" in (health.detail or "")


def test_openrouter_tool_payload_requires_compatible_route():
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-payload",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="test/model",
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        )
    )
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="Use the tool")],
        tools=[TOOL],
        tool_choice="required",
    )

    payload = provider._payload(request, provider.require(request))

    assert payload["provider"] == {"require_parameters": True}
    # No OpenRouter route advertises parallel_tool_calls; sending it with
    # require_parameters leaves no endpoint (HTTP 404 on every model).
    assert "parallel_tool_calls" not in payload
    # Unknown model parameters: drop optional ones rather than fail routing.
    assert "reasoning" not in payload


def test_openrouter_payload_keeps_chat_requests_on_one_routing_session():
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-session",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="test/model",
        )
    )
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="Continue the turn")],
        metadata={"chat_session_id": "chat-session-a"},
    )

    payload = provider._payload(request, provider.require(request))

    assert payload["session_id"] == "chat-session-a"


def test_context_compression_is_disabled_only_for_openrouter():
    def payload(flavor: ProviderFlavor) -> dict:
        provider = OpenAICompatibleProvider(
            config_from_catalog(
                provider_id=f"payload-{flavor.value}",
                flavor=flavor,
                api_key_value="test-key",
                default_model="test/model",
            )
        )
        request = ModelRequest(messages=[ModelMessage(role="user", content="Hi")])
        return provider._payload(request, provider.require(request))

    assert payload(ProviderFlavor.OPENROUTER)["plugins"] == [
        {"id": "context-compression", "enabled": False}
    ]
    # The plugin is an OpenRouter concept; other wire-compatible hosts reject it.
    assert "plugins" not in payload(ProviderFlavor.OPENAI)


def test_openrouter_tool_payload_sends_only_parameters_the_model_advertises():
    config = config_from_catalog(
        provider_id="openrouter-payload",
        flavor=ProviderFlavor.OPENROUTER,
        api_key_value="test-key",
        default_model="reasoner/model",
        model_allowlist=["reasoner/model", "plain/model"],
        capabilities=ModelCapabilities(tools=True, strict_tools=True),
    ).model_copy(
        update={
            "model_parameters": {
                "reasoner/model": ["tools", "tool_choice", "max_tokens", "reasoning"],
                "plain/model": ["tools", "tool_choice", "max_tokens", "temperature"],
            }
        }
    )
    provider = OpenAICompatibleProvider(config)

    def payload(model: str, **extra):
        request = ModelRequest(
            model=model,
            messages=[ModelMessage(role="user", content="Use the tool")],
            tools=[TOOL],
            tool_choice="required",
            temperature=0.2,
            **extra,
        )
        return provider._payload(request, provider.require(request))

    reasoner = payload("reasoner/model")
    plain = payload("plain/model")
    assert reasoner["reasoning"] == {"exclude": False}
    assert "temperature" not in reasoner
    assert "reasoning" not in plain
    assert plain["temperature"] == 0.2
    # Without tools there is no require_parameters, so reasoning is requested.
    chat = provider._payload(
        ModelRequest(
            model="plain/model", messages=[ModelMessage(role="user", content="Hi")]
        ),
        "plain/model",
    )
    assert chat["reasoning"] == {"exclude": False}
    assert "provider" not in chat


def test_provider_from_openrouter_profile_carries_model_parameters():
    from nebula.v3.domain import ProviderProfile
    from nebula.v3.providers import provider_from_profile

    profile = ProviderProfile(
        name="OpenRouter",
        provider_type="openrouter",
        is_local=False,
        secret_ref="env:OPEN_ROUTER_TEST_KEY",
        model_allowlist=["openai/gpt-4.1-mini"],
        metadata={
            "model_descriptors": [
                {
                    "id": "openai/gpt-4.1-mini",
                    "supported_parameters": ["tools", "tool_choice"],
                }
            ]
        },
    )

    provider = provider_from_profile(profile)

    assert provider.config.model_parameters == {
        "openai/gpt-4.1-mini": ["tools", "tool_choice"]
    }


def test_openrouter_loads_exact_endpoint_limits_for_automatic_routing():
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "author/model:free",
                    "endpoints": [
                        {
                            "provider_name": "Provider A",
                            "context_length": 131_072,
                            "max_prompt_tokens": 120_000,
                            "max_completion_tokens": 8_192,
                            "supported_parameters": ["tools", "max_tokens"],
                            "status": 0,
                        },
                        {
                            "provider_name": "Provider B",
                            "context_length": 65_536,
                            "max_prompt_tokens": 60_000,
                            "max_completion_tokens": 4_096,
                            "supported_parameters": ["max_tokens"],
                            "status": 0,
                        },
                    ],
                }
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-routes",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    routes = asyncio.run(provider.openrouter_route_limits("author/model:free"))

    assert observed == ["/api/v1/models/author/model:free/endpoints"]
    assert len(routes) == 2
    assert routes[0].max_input_tokens == 120_000
    assert routes[1].context_window == 65_536
    assert routes[0].supported_parameters == ["tools", "max_tokens"]


def test_openrouter_catalog_records_the_model_an_alias_redirects_to():
    descriptors = openrouter_models(
        {
            "data": [
                {
                    "id": "~author/family-latest",
                    "name": "Author: Family Latest",
                    "canonical_slug": "~author/family-latest",
                    "context_length": 1_048_576,
                    "alias_target": {
                        "name": "Author: Model A",
                        "slug": "author/model-a",
                    },
                },
                {
                    "id": "author/model-a",
                    "name": "Author: Model A",
                    "context_length": 1_048_576,
                },
            ]
        }
    )

    alias, exact = descriptors
    assert alias.alias_target == "author/model-a"
    assert exact.alias_target is None


def test_openrouter_alias_endpoint_discovery_explains_the_empty_route_set():
    def handler(_request: httpx.Request) -> httpx.Response:
        # Aliases redirect to another model and publish no endpoints of their own.
        return httpx.Response(
            200, json={"data": {"id": "~author/family-latest", "endpoints": []}}
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-routes",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderError, match="alias models publish no endpoints"):
        asyncio.run(provider.openrouter_route_limits("~author/family-latest"))


def test_openrouter_rejects_incomplete_endpoint_limits():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "author/model",
                    "endpoints": [
                        {
                            "provider_name": "Missing context window",
                            "max_prompt_tokens": 60_000,
                            "max_completion_tokens": 4_096,
                        }
                    ],
                }
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-routes",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderError, match="endpoint discovery failed"):
        asyncio.run(provider.openrouter_route_limits("author/model"))


def test_openai_compatible_classifies_only_confirmed_context_rejections():
    responses = iter(
        [
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "maximum context window exceeded",
                    }
                },
            ),
            httpx.Response(
                401,
                json={"error": {"code": "invalid_api_key", "message": "denied"}},
            ),
        ]
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE),
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )
    request = ModelRequest(
        model="test-model", messages=[ModelMessage(role="user", content="hello")]
    )

    with pytest.raises(ProviderContextLengthError):
        asyncio.run(provider.complete(request))
    with pytest.raises(ProviderError) as denied:
        asyncio.run(provider.complete(request))
    assert not isinstance(denied.value, ProviderContextLengthError)


def test_vllm_openai_compatible_streaming_is_native_sse():
    body = "\n\n".join(
        [
            'data: {"id":"chat-vllm","model":"served-model","choices":[{"delta":{"content":"hel"}}]}',
            'data: {"id":"chat-vllm","model":"served-model","choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
            "data: [DONE]",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="vllm-stream",
            flavor=ProviderFlavor.VLLM,
            default_model="served-model",
            capabilities=ModelCapabilities(streaming=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        ]

    events = asyncio.run(collect())
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "hello"
    assert events[-1].response.usage.total_tokens == 3
    assert events[-1].response.reasoning == ""


def test_openai_compatible_streams_reasoning_apart_from_content():
    body = "\n\n".join(
        [
            'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}',
            'data: {"choices":[{"delta":{"content":"FLASH_OK"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4-flash",
            capabilities=ModelCapabilities(streaming=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        ]

    events = asyncio.run(collect())
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.REASONING_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    # Deltas keep their exact whitespace; the assembled reasoning is trimmed.
    assert events[1].delta == "think "
    assert events[2].delta == "FLASH_OK"
    assert events[-1].response.text == "FLASH_OK"
    assert events[-1].response.reasoning == "think"


def test_openai_compatible_stream_does_not_repeat_mirrored_reasoning():
    """OpenRouter repeats each thought fragment in `reasoning_details`."""

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning": "The",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "The", "index": 0}
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "reasoning": " user",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": " user", "index": 0}
                        ],
                    }
                }
            ]
        },
        {"choices": [{"delta": {"content": "Hi."}, "finish_reason": "stop"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4-flash",
            capabilities=ModelCapabilities(streaming=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        ]

    events = asyncio.run(collect())
    reasoning = [
        event.delta for event in events if event.type == StreamEventType.REASONING_DELTA
    ]
    assert reasoning == ["The", " user"]
    assert events[-1].response.reasoning == "The user"


def test_openai_compatible_stream_keeps_whitespace_between_deltas():
    chunks = [
        {"choices": [{"delta": {"reasoning": "Think"}}]},
        {"choices": [{"delta": {"reasoning": " step"}}]},
        {"choices": [{"delta": {"content": "TCP is"}}]},
        {"choices": [{"delta": {"content": " connection"}}]},
        {"choices": [{"delta": {"content": "-oriented.\n\n"}}]},
        {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="stream-spaces",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="test/model",
            capabilities=ModelCapabilities(streaming=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                ModelRequest(messages=[ModelMessage(role="user", content="Hi")])
            )
        ]

    events = asyncio.run(collect())
    final = events[-1].response
    assert final.text == "TCP is connection-oriented.\n\nDone"
    assert final.reasoning == "Think step"
    assert [
        event.delta for event in events if event.type == StreamEventType.TEXT_DELTA
    ][1] == " connection"


def test_openai_compatible_tool_names_are_wire_safe_and_decoded():
    from nebula.v3.providers import ModelToolResult, ToolDefinition, _wire_tool_names

    dotted = ToolDefinition(
        name="tool_output.search",
        description="Search tool output.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="Search")],
        tools=[TOOL, dotted],
        tool_choice="required",
        tool_results=[
            ModelToolResult(
                call_id="old", name="skill.read_resource", arguments={}, output="{}"
            )
        ],
    )
    names = _wire_tool_names(request)
    assert names["tool_output.search"] == "tool_output_search"
    assert names["skill.read_resource"] == "skill_read_resource"
    assert names[TOOL.name] == TOOL.name

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        sent = [tool["function"]["name"] for tool in body["tools"]]
        replayed = body["messages"][1]["tool_calls"][0]["function"]["name"]
        assert all(
            re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) for name in [*sent, replayed]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_output_search",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="wire-names",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="test/model",
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(provider.complete(request))

    assert response.tool_calls[0].name == "tool_output.search"


def test_wire_tool_names_never_collide():
    from nebula.v3.providers import ToolDefinition, _wire_tool_names

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="x")],
        tools=[
            ToolDefinition(name="a.b", description="d", input_schema=schema),
            ToolDefinition(name="a_b", description="d", input_schema=schema),
        ],
    )
    names = _wire_tool_names(request)
    assert names["a_b"] == "a_b"
    assert names["a.b"] != "a_b"
    assert len(set(names.values())) == 2


def _allowlisted_openrouter(handler=None, allowed=("anthropic", "Google-Vertex")):
    return OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-allowlist",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model="author/model",
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
            options={"openrouter_providers": list(allowed)},
        ),
        **({"transport": httpx.MockTransport(handler)} if handler else {}),
    )


def test_openrouter_allowlist_restricts_every_request_to_selected_providers():
    provider = _allowlisted_openrouter()
    chat = provider._payload(
        ModelRequest(messages=[ModelMessage(role="user", content="Hi")]), "author/model"
    )
    tools = provider._payload(
        ModelRequest(
            messages=[ModelMessage(role="user", content="Use the tool")],
            tools=[TOOL],
            tool_choice="required",
        ),
        "author/model",
    )

    assert chat["provider"] == {"only": ["anthropic", "google-vertex"]}
    assert tools["provider"] == {
        "require_parameters": True,
        "only": ["anthropic", "google-vertex"],
    }
    # Without an allowlist OpenRouter keeps choosing freely.
    open_provider = _allowlisted_openrouter(allowed=())
    assert "provider" not in open_provider._payload(
        ModelRequest(messages=[ModelMessage(role="user", content="Hi")]), "author/model"
    )


def _endpoints(*rows):
    return {
        "data": {
            "id": "author/model",
            "endpoints": [
                {
                    "provider_name": name,
                    "tag": tag,
                    "context_length": context,
                    "max_prompt_tokens": context - 1_000,
                    "max_completion_tokens": 4_096,
                    "supported_parameters": ["tools"],
                    "status": 0,
                }
                for name, tag, context in rows
            ],
        }
    }


def test_openrouter_route_limits_follow_the_provider_allowlist():
    payload = _endpoints(
        ("Anthropic", "anthropic", 200_000),
        ("Google", "google-vertex/us-east5", 150_000),
        ("Azure", "azure/global", 32_000),
    )
    provider = _allowlisted_openrouter(
        lambda _request: httpx.Response(200, json=payload)
    )

    routes = asyncio.run(provider.openrouter_route_limits("author/model"))

    assert [route.provider_slug for route in routes] == ["anthropic", "google-vertex"]
    assert min(route.context_window for route in routes) == 150_000


def test_openrouter_route_limits_explain_when_no_allowed_provider_serves_the_model():
    payload = _endpoints(("Azure", "azure/global", 32_000))
    provider = _allowlisted_openrouter(
        lambda _request: httpx.Response(200, json=payload)
    )

    with pytest.raises(ProviderError, match="None of the allowed OpenRouter providers"):
        asyncio.run(provider.openrouter_route_limits("author/model"))


def test_openrouter_upstream_directory_is_parsed_and_sorted():
    from nebula.v3.model_catalog import openrouter_upstream_providers

    providers = openrouter_upstream_providers(
        {
            "data": [
                {"name": "Google Vertex", "slug": "Google-Vertex"},
                {"name": "Anthropic", "slug": "anthropic"},
                {"name": "", "slug": "broken"},
                "not-a-row",
            ]
        }
    )

    assert [(item.slug, item.name) for item in providers] == [
        ("anthropic", "Anthropic"),
        ("google-vertex", "Google Vertex"),
    ]


def test_openrouter_null_prompt_limit_uses_the_context_window():
    """OpenRouter reports max_prompt_tokens as null on real endpoints."""

    payload = _endpoints(("Anthropic", "anthropic", 200_000))
    payload["data"]["endpoints"][0]["max_prompt_tokens"] = None
    provider = _allowlisted_openrouter(
        lambda _request: httpx.Response(200, json=payload), allowed=()
    )

    (route,) = asyncio.run(provider.openrouter_route_limits("author/model"))

    assert route.context_window == 200_000
    assert route.max_input_tokens == 200_000
    assert route.provider_slug == "anthropic"


def _openrouter_discovery(allowed, *, filtered_status=200, directory_status=200):
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/v1/providers":
            if directory_status != 200:
                return httpx.Response(directory_status, json={})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"name": "Together", "slug": "together"},
                        {"name": "Fireworks", "slug": "fireworks"},
                    ]
                },
            )
        if request.url.path == "/api/v1/models/user":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "us/served"},
                        {"id": "offshore/only"},
                        {"id": "us/paged"},
                    ]
                },
            )
        assert request.url.path == "/api/v1/models"
        if filtered_status != 200:
            return httpx.Response(filtered_status, json={})
        if request.url.params.get("offset") == "1":
            return httpx.Response(
                200, json={"data": [{"id": "us/paged"}], "links": {"next": None}}
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "us/served"}, {"id": "not/visible-to-account"}],
                "links": {"next": "/api/v1/models?providers=together&offset=1&limit=1"},
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-us",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            options={"openrouter_providers": list(allowed)},
        ),
        transport=httpx.MockTransport(handler),
    )
    return asyncio.run(provider.health()), observed


def test_openrouter_discovery_lists_only_models_the_allowed_providers_serve():
    health, observed = _openrouter_discovery(["together", "unknown-host"])

    assert health.healthy is True
    # Account visibility and upstream allowlist both apply; pagination is followed.
    assert health.models == ["us/served", "us/paged"]
    filtered = [params for path, params in observed if path == "/api/v1/models"]
    assert filtered == [
        {"providers": "together"},
        {"providers": "together", "offset": "1"},
    ]


def test_openrouter_discovery_lists_nothing_when_no_allowed_provider_is_known():
    # OpenRouter would ignore unknown slugs and return every model.
    health, observed = _openrouter_discovery(["unknown-host"])

    assert health.healthy is True
    assert health.models == []
    assert all(path != "/api/v1/models" for path, _params in observed)


def test_openrouter_discovery_fails_closed_when_the_provider_filter_fails():
    health, _observed = _openrouter_discovery(["together"], filtered_status=500)

    assert health.healthy is False
    assert health.models == []


OVERLOADED_BODY = {
    "error": {
        "message": "Provider returned error",
        "code": 503,
        "metadata": {
            "raw": '{"error":{"message":"service overloaded, please try again later"}}'
        },
    }
}


def _retrying_config(provider_id: str, **options):
    return ProviderConfig(
        id=provider_id,
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.invalid",
        default_model="test-model",
        # Zero backoff keeps the retry contract under test without real waiting.
        options={"retry_backoff_seconds": 0, **options},
    )


def _chat_request():
    return ModelRequest(messages=[ModelMessage(role="user", content="hello")])


def _completion_body():
    return {
        "model": "test-model",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_overloaded_upstream_is_retried_until_the_provider_answers():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) < 3:
            return httpx.Response(503, json=OVERLOADED_BODY)
        return httpx.Response(200, json=_completion_body())

    provider = OpenAICompatibleProvider(
        _retrying_config("retry-success"), transport=httpx.MockTransport(handler)
    )

    result = asyncio.run(provider.complete(_chat_request()))

    assert result.text == "ok"
    assert len(attempts) == 3


def test_retries_stop_at_the_attempt_limit_and_name_the_attempts():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(503)
        return httpx.Response(503, json=OVERLOADED_BODY)

    provider = OpenAICompatibleProvider(
        _retrying_config("retry-exhausted"), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderOverloadedError) as failure:
        asyncio.run(provider.complete(_chat_request()))

    assert len(attempts) == 3
    assert failure.value.status_code == 503
    # The operator sees that Nebula already retried before they retry by hand.
    assert str(failure.value) == (
        "provider returned HTTP 503: Provider returned error "
        "(upstream: service overloaded, please try again later) after 3 attempts"
    )


def test_request_defects_are_never_retried():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(401)
        return httpx.Response(401, json={"error": {"message": "denied"}})

    provider = OpenAICompatibleProvider(
        _retrying_config("retry-denied"), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(_chat_request()))

    assert attempts == [401]
    assert not isinstance(failure.value, ProviderOverloadedError)


def test_retries_can_be_disabled_for_one_provider():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(503)
        return httpx.Response(503, json=OVERLOADED_BODY)

    provider = OpenAICompatibleProvider(
        _retrying_config("retry-disabled", retry_attempts=1),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderOverloadedError) as failure:
        asyncio.run(provider.complete(_chat_request()))

    assert attempts == [503]
    assert "attempts" not in str(failure.value)


def test_streaming_retries_before_the_first_token_and_not_after():
    calls: list[int] = []
    body = "\n\n".join(
        [
            'data: {"id":"chat-1","model":"test-model","choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        if len(calls) == 1:
            return httpx.Response(503, json=OVERLOADED_BODY)
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("retry-stream"), transport=httpx.MockTransport(handler)
    )

    async def collect():
        return [event async for event in provider.stream(_chat_request())]

    events = asyncio.run(collect())

    assert len(calls) == 2
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "hi"


def test_a_stream_that_fails_after_output_is_not_replayed():
    calls: list[int] = []
    body = "\n\n".join(
        [
            'data: {"id":"chat-1","model":"test-model","choices":[{"delta":{"content":"hi"}}]}',
            "data: {not json}",
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("stream-midflight"), transport=httpx.MockTransport(handler)
    )

    async def collect():
        return [event async for event in provider.stream(_chat_request())]

    events = asyncio.run(collect())

    assert len(calls) == 1
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]


def test_retry_limits_come_from_provider_options_then_the_environment(monkeypatch):
    monkeypatch.setenv("NEBULA_PROVIDER_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("NEBULA_PROVIDER_RETRY_BACKOFF_SECONDS", "2")

    inherited = retry_policy(_config(ProviderKind.OPENAI_COMPATIBLE))
    overridden = retry_policy(_retrying_config("tuned", retry_attempts=2))

    assert inherited == RetryPolicy(attempts=5, backoff_seconds=2.0)
    assert overridden == RetryPolicy(attempts=2, backoff_seconds=0.0)

    monkeypatch.setenv("NEBULA_PROVIDER_RETRY_ATTEMPTS", "not-a-number")
    assert retry_policy(_config(ProviderKind.OPENAI_COMPATIBLE)).attempts == 3


def test_retry_after_is_honored_within_the_bounded_wait():
    policy = RetryPolicy(attempts=3, backoff_seconds=0.5)

    def _header(value: str) -> float | None:
        return providers._retry_after_seconds(
            httpx.Response(503, headers={"retry-after": value})
        )

    assert _header("7") == 7.0
    assert _header("600") == providers._MAX_RETRY_DELAY_SECONDS
    assert _header("Mon, 01 Jan 1990 00:00:00 GMT") == 0.0
    assert _header("soon") is None
    assert _header("") is None
    # Retry-After raises the floor; jitter never pushes past the hard ceiling.
    waited = providers._retry_delay(policy, 1, 7.0)
    assert 7.0 <= waited <= 7.0 * 1.25
    assert providers._retry_delay(policy, 8, None) <= (
        providers._MAX_RETRY_DELAY_SECONDS * 1.25
    )


def test_openai_compatible_stream_surfaces_an_in_band_error_frame():
    calls: list[int] = []
    body = "\n\n".join(
        [
            'data: {"id":"chat-1","model":"test-model","choices":[{"delta":{"content":"partial"}}]}',
            'data: {"id":"chat-1","error":{"message":"Provider returned error","code":502,'
            '"metadata":{"raw":"{\\"error\\":{\\"message\\":\\"upstream overloaded\\"}}"}},'
            '"choices":[{"delta":{},"finish_reason":"error"}]}',
            "data: [DONE]",
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("stream-error-frame"),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [event async for event in provider.stream(_chat_request())]

    events = asyncio.run(collect())

    assert len(calls) == 1
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert "Provider returned error" in (events[-1].error or "")
    assert "upstream overloaded" in (events[-1].error or "")


def test_openai_compatible_stream_error_before_any_token_is_not_an_empty_reply():
    body = "\n\n".join(
        [
            'data: {"error":{"message":"model is overloaded","code":503}}',
            "data: [DONE]",
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("stream-error-first"),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [event async for event in provider.stream(_chat_request())]

    events = asyncio.run(collect())

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.ERROR,
    ]
    assert "model is overloaded" in (events[-1].error or "")


def test_stream_error_frame_maps_context_length_to_the_typed_error():
    frame = {
        "error": {
            "message": "This model's maximum context length is 8192 tokens",
            "code": "context_length_exceeded",
        }
    }

    assert isinstance(
        providers._stream_error_frame(frame), providers.ProviderContextLengthError
    )
    assert (
        providers._stream_error_frame({"choices": [{"delta": {"content": "x"}}]})
        is None
    )
    assert providers._stream_error_frame({"error": None}) is None


def test_streaming_context_length_rejection_is_flagged_for_recovery():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "This model's maximum context length is 8192 tokens",
                    "code": "context_length_exceeded",
                }
            },
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("stream-context-length"),
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [event async for event in provider.stream(_chat_request())]

    events = asyncio.run(collect())

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.ERROR,
    ]
    assert events[-1].context_length_exceeded is True
    assert "context length" in (events[-1].error or "")


def test_bedrock_reports_the_client_error_code_and_message(monkeypatch):
    from botocore.exceptions import ClientError, NoCredentialsError

    class FailingClient:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        def converse(self, **kwargs):
            del kwargs
            raise self.failure

        def list_foundation_models(self):
            raise self.failure

    def install(failure: Exception) -> None:
        monkeypatch.setattr(
            providers.boto3, "client", lambda *args, **kwargs: FailingClient(failure)
        )

    provider = BedrockProvider(_config(ProviderKind.BEDROCK))
    request = ModelRequest(
        model="test-model", messages=[ModelMessage(role="user", content="hi")]
    )

    install(
        ClientError(
            {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "The provided model identifier is invalid.",
                }
            },
            "Converse",
        )
    )
    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(request))
    assert str(failure.value) == (
        "Bedrock request failed: ValidationException: "
        "The provided model identifier is invalid."
    )

    install(
        ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "not authorized to perform bedrock:ListFoundationModels",
                }
            },
            "ListFoundationModels",
        )
    )
    health = asyncio.run(provider.health())
    assert health.healthy is False
    assert health.detail == (
        "Bedrock health check failed: AccessDeniedException: "
        "not authorized to perform bedrock:ListFoundationModels"
    )

    # Anything that is not a botocore ClientError still reports only its type.
    install(NoCredentialsError())
    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(request))
    assert str(failure.value) == "Bedrock request failed: NoCredentialsError"
    health = asyncio.run(provider.health())
    assert health.detail == "Bedrock health check failed: NoCredentialsError"

    # Secrets inside a provider message are redacted before they are surfaced.
    install(
        ClientError(
            {
                "Error": {
                    "Code": "UnrecognizedClientException",
                    "Message": "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
                }
            },
            "Converse",
        )
    )
    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(request))
    assert "eyJhbGci" not in str(failure.value)
    assert str(failure.value).startswith(
        "Bedrock request failed: UnrecognizedClientException: "
    )


def test_stream_fallback_reports_thinking_before_the_reply():
    """A provider without native streaming still shows its thoughts as they land."""

    class NonStreamingProvider(providers.ModelProvider):
        async def complete(self, request: ModelRequest) -> providers.ModelResponse:
            del request
            return providers.ModelResponse(
                provider_id="fallback",
                model="model-a",
                text="FLASH_OK",
                reasoning="Private chain of thought.",
                finish_reason="stop",
            )

        async def health(self) -> providers.ProviderHealth:
            return providers.ProviderHealth(provider_id="fallback", healthy=True)

    provider = NonStreamingProvider(
        ProviderConfig(
            id="fallback",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:8000/v1",
            default_model="model-a",
            model_allowlist=["model-a"],
            local=True,
        )
    )

    async def scenario():
        return [
            event
            async for event in provider.stream(
                ModelRequest(
                    model="model-a",
                    messages=[ModelMessage(role="user", content="Reply.")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.REASONING_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[1].delta == "Private chain of thought."
    assert events[-1].response.reasoning == "Private chain of thought."


def _sse_provider(provider_id: str, body: str, calls: list[int] | None = None):
    def handler(_request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(len(calls))
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    return OpenAICompatibleProvider(
        _retrying_config(provider_id), transport=httpx.MockTransport(handler)
    )


def _collect_stream(provider, request: ModelRequest | None = None):
    async def collect():
        return [event async for event in provider.stream(request or _chat_request())]

    return asyncio.run(collect())


def test_stream_frames_are_split_only_on_sse_line_endings():
    # U+2028, U+2029 and U+0085 are str.splitlines() breaks but not SSE ones;
    # gateways that serialise with ensure_ascii=False send them raw inside JSON
    # strings. Comments, CRLF and multi-line data events are SSE grammar too.
    text = "line sep nelend"
    body = (
        ": OPENROUTER PROCESSING\r\n\r\n"
        'data: {"id":"chat-1","model":"served","choices":[{"delta":{"content":'
        + json.dumps(text, ensure_ascii=False)
        + "}}]}\r\n\r\n"
        'data: {"id":"chat-1",\n'
        'data: "choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    events = _collect_stream(_sse_provider("stream-unicode", body))

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[1].delta == text
    assert events[-1].response.text == text + "!"
    assert events[-1].response.model == "served"


def test_openrouter_discovery_reports_a_failed_provider_directory():
    # With an allowlist, an empty directory would filter out every model and
    # blame the operator's allowlist; the outage has to be named instead.
    health, observed = _openrouter_discovery(["together"], directory_status=503)

    assert health.healthy is False
    assert health.models == []
    assert "provider directory" in (health.detail or "")
    assert "503" in (health.detail or "")
    assert all(path != "/api/v1/models" for path, _params in observed)

    # Without an allowlist the directory is informational only.
    health, _observed = _openrouter_discovery([], directory_status=503)

    assert health.healthy is True
    assert health.models == ["us/served", "offshore/only", "us/paged"]


def test_stream_error_frame_maps_transient_codes_to_overload():
    overloaded = providers._stream_error_frame(
        {"error": {"message": "model is overloaded", "code": 503}}
    )
    assert isinstance(overloaded, ProviderOverloadedError)
    assert overloaded.status_code == 503

    rejected = providers._stream_error_frame(
        {"error": {"message": "bad request", "code": "invalid_request_error"}}
    )
    assert isinstance(rejected, ProviderError)
    assert not isinstance(rejected, ProviderOverloadedError)


def test_a_retryable_error_frame_before_any_token_is_replayed():
    calls: list[int] = []
    overload = (
        'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        "data: " + json.dumps(OVERLOADED_BODY) + "\n\n"
    )
    answer = (
        'data: {"id":"chat-2","model":"test-model","choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        return httpx.Response(
            200,
            text=overload if len(calls) == 1 else answer,
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("stream-frame-retry"), transport=httpx.MockTransport(handler)
    )

    events = _collect_stream(provider)

    assert len(calls) == 2
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.COMPLETED,
    ]
    assert events[-1].response.text == "hi"
    assert events[-1].response.provider_request_id == "chat-2"


def test_an_exhausted_error_frame_is_labelled_retryable():
    calls: list[int] = []
    body = 'data: {"error":{"message":"model is overloaded","code":503}}\n\ndata: [DONE]\n\n'

    events = _collect_stream(_sse_provider("stream-frame-exhausted", body, calls))

    assert len(calls) == 3
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.ERROR,
    ]
    assert events[-1].retryable is True
    assert events[-1].error == (
        "provider reported an error while streaming: model is overloaded "
        "after 3 attempts"
    )

    # After output began the frame is reported, never replayed.
    late_calls: list[int] = []
    late = (
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        'data: {"error":{"message":"model is overloaded","code":503}}\n\n'
    )

    events = _collect_stream(_sse_provider("stream-frame-late", late, late_calls))

    assert len(late_calls) == 1
    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert events[-1].retryable is True
    assert "attempts" not in (events[-1].error or "")


def test_gemini_context_window_rejection_is_typed(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": (
                        "The input token count (1234567) exceeds the maximum "
                        "number of tokens allowed (1048576)."
                    ),
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    provider = GeminiProvider(
        ProviderConfig(
            **_config(ProviderKind.GEMINI).model_dump(exclude={"api_key_env"}),
            api_key_env="NEBULA_TEST_PROVIDER_KEY",
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderContextLengthError):
        asyncio.run(provider.complete(_chat_request()))


def test_bedrock_context_window_rejection_is_typed(monkeypatch):
    from botocore.exceptions import ClientError

    class FailingClient:
        def converse(self, **kwargs):
            del kwargs
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Input is too long for requested model.",
                    }
                },
                "Converse",
            )

    monkeypatch.setattr(
        providers.boto3, "client", lambda *args, **kwargs: FailingClient()
    )
    provider = BedrockProvider(_config(ProviderKind.BEDROCK))

    with pytest.raises(ProviderContextLengthError) as failure:
        asyncio.run(
            provider.complete(
                ModelRequest(
                    model="test-model",
                    messages=[ModelMessage(role="user", content="hi")],
                )
            )
        )
    assert str(failure.value) == (
        "Bedrock request failed: ValidationException: "
        "Input is too long for requested model."
    )


def test_stream_null_model_and_id_frames_keep_the_known_values():
    body = (
        'data: {"id":"chat-1","model":"served-model","choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"id":null,"model":null,"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    events = _collect_stream(_sse_provider("stream-null-model", body))

    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.model == "served-model"
    assert events[-1].response.provider_request_id == "chat-1"
    assert events[-1].response.text == "hi"


def test_usage_without_total_tokens_is_summed_from_its_parts():
    body = (
        'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )

    events = _collect_stream(_sse_provider("stream-usage", body))

    assert events[-1].response.usage.total_tokens == 107

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 7},
            },
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("complete-usage"), transport=httpx.MockTransport(handler)
    )

    result = asyncio.run(provider.complete(_chat_request()))

    assert result.usage.total_tokens == 107


def test_stream_without_a_terminal_frame_is_not_a_clean_completion():
    # A proxy idle-timeout closes the body cleanly: no finish reason, no [DONE].
    cut = 'data: {"choices":[{"delta":{"content":"half"}}]}\n\n'

    events = _collect_stream(_sse_provider("stream-cut", cut))

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert "ended before" in (events[-1].error or "")
    assert events[-1].retryable is False

    # Either terminal marker makes a complete reply; truncation is reported.
    truncated = (
        'data: {"choices":[{"delta":{"content":"half"},"finish_reason":"length"}]}\n\n'
    )

    events = _collect_stream(_sse_provider("stream-length", truncated))

    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.finish_reason == "length"

    done_only = 'data: {"choices":[{"delta":{"content":"all"}}]}\n\ndata: [DONE]\n\n'

    events = _collect_stream(_sse_provider("stream-done-only", done_only))

    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.text == "all"


def test_quota_exhaustion_is_not_retried_or_called_an_overload():
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(429)
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "You exceeded your current quota, please check your plan "
                        "and billing details."
                    ),
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("quota-exhausted"), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(providers.ProviderQuotaError) as failure:
        asyncio.run(provider.complete(_chat_request()))

    assert attempts == [429]
    assert not isinstance(failure.value, ProviderOverloadedError)
    assert str(failure.value) == (
        "provider quota or billing limit reached (HTTP 429): You exceeded your "
        "current quota, please check your plan and billing details."
    )

    # A rate limit without a permanent marker is still a transient overload.
    limited: list[int] = []

    def limit(_request: httpx.Request) -> httpx.Response:
        limited.append(429)
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit reached", "code": "rate_limit"}},
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("rate-limited"), transport=httpx.MockTransport(limit)
    )

    with pytest.raises(ProviderOverloadedError):
        asyncio.run(provider.complete(_chat_request()))

    assert len(limited) == 3


def test_transport_failures_become_provider_errors_with_a_reason():
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    provider = OpenAICompatibleProvider(
        _retrying_config("read-timeout"), transport=httpx.MockTransport(timeout)
    )

    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(_chat_request()))
    assert not isinstance(failure.value, ProviderOverloadedError)
    assert str(failure.value) == "provider request timed out (ReadTimeout)"

    events = _collect_stream(provider)

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.ERROR,
    ]
    assert events[-1].error == "provider request timed out (ReadTimeout)"

    refused: list[int] = []

    def refuse(_request: httpx.Request) -> httpx.Response:
        refused.append(1)
        raise httpx.ConnectError("connection refused")

    provider = OpenAICompatibleProvider(
        _retrying_config("refused"), transport=httpx.MockTransport(refuse)
    )

    with pytest.raises(ProviderError) as failure:
        asyncio.run(provider.complete(_chat_request()))
    assert len(refused) == 3
    assert str(failure.value) == (
        "provider request failed (ConnectError): connection refused"
    )

    class TornStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )

    def tear(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=TornStream(), headers={"content-type": "text/event-stream"}
        )

    provider = OpenAICompatibleProvider(
        _retrying_config("torn"), transport=httpx.MockTransport(tear)
    )

    events = _collect_stream(provider)

    assert [event.type for event in events] == [
        StreamEventType.STARTED,
        StreamEventType.TEXT_DELTA,
        StreamEventType.ERROR,
    ]
    assert events[-1].error == (
        "provider request failed (RemoteProtocolError): peer closed connection "
        "without sending complete message body"
    )


def test_streaming_tool_calls_are_keyed_by_index_or_arrival_order():
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="inspect")], tools=[TOOL]
    )
    config = ProviderConfig(
        id="stream-tools",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.invalid",
        default_model="test-model",
        capabilities=ModelCapabilities(tools=True, strict_tools=True, streaming=True),
        options={"retry_backoff_seconds": 0},
    )

    def stream(body: str):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )

        provider = OpenAICompatibleProvider(
            config, transport=httpx.MockTransport(handler)
        )
        return _collect_stream(provider, request)

    # Two complete calls in one chunk without an index stay two calls.
    unindexed = (
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"id":"call_a","type":"function","function":{"name":"lookup_asset","arguments":"{\\"address\\":\\"a\\"}"}},'
        '{"id":"call_b","type":"function","function":{"name":"lookup_asset","arguments":"{\\"address\\":\\"b\\"}"}}'
        "]}}]}\n\n"
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    events = stream(unindexed)

    calls = [
        event.tool_call for event in events if event.type == StreamEventType.TOOL_CALL
    ]
    assert [(call.id, call.arguments["address"]) for call in calls] == [
        ("call_a", "a"),
        ("call_b", "b"),
    ]
    assert events[-1].type == StreamEventType.COMPLETED
    assert events[-1].response.finish_reason == "tool_calls"

    # A vendor that resends id and name on every indexed delta yields one call.
    indexed = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup_asset","arguments":"{\\"addr"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup_asset","arguments":"ess\\":\\"x\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )

    events = stream(indexed)

    calls = [
        event.tool_call for event in events if event.type == StreamEventType.TOOL_CALL
    ]
    assert [(call.id, call.name, call.arguments) for call in calls] == [
        ("call_1", "lookup_asset", {"address": "x"})
    ]


def _bedrock_client(monkeypatch, observed: dict, content: list[dict]):
    class Client:
        def converse(self, **kwargs):
            observed.update(kwargs)
            return {
                "output": {"message": {"role": "assistant", "content": content}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            }

    monkeypatch.setattr(providers.boto3, "client", lambda *args, **kwargs: Client())


def _dotted_request(**extra):
    from nebula.v3.providers import ModelToolResult

    return ModelRequest(
        messages=[ModelMessage(role="user", content="Search")],
        tools=[TOOL, _DOTTED],
        tool_results=[
            ModelToolResult(
                call_id="old", name="tool_output.search", arguments={}, output="{}"
            )
        ],
        **extra,
    )


def _keyed_config(kind, flavor=None, **capabilities):
    config = _config(kind, capabilities=ModelCapabilities(**capabilities))
    return ProviderConfig(
        **config.model_dump(exclude={"api_key_env", "flavor"}),
        api_key_env="NEBULA_TEST_PROVIDER_KEY",
        flavor=flavor or config.flavor,
    )


_WIRE_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")

_DOTTED = ToolDefinition(
    name="tool_output.search",
    description="Search tool output.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


def test_anthropic_health_follows_model_pagination(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")
    pages = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        pages.append(dict(http_request.url.params))
        if http_request.url.params.get("after_id") is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-a"}],
                    "has_more": True,
                    "first_id": "claude-a",
                    "last_id": "claude-a",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "claude-b"}, {"display_name": "invalid"}],
                "has_more": False,
                "first_id": "claude-b",
                "last_id": "claude-b",
            },
        )

    provider = AnthropicProvider(
        _keyed_config(ProviderKind.ANTHROPIC), transport=httpx.MockTransport(handler)
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["claude-a", "claude-b"]
    assert pages == [{"limit": "1000"}, {"limit": "1000", "after_id": "claude-a"}]


def test_anthropic_tool_names_are_wire_safe_and_decoded(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")
    observed = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "test-model",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "use_1",
                        "name": "tool_output_search",
                        "input": {},
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    provider = AnthropicProvider(
        _keyed_config(ProviderKind.ANTHROPIC, tools=True, strict_tools=True),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(provider.complete(_dotted_request()))

    payload = observed["payload"]
    sent = [tool["name"] for tool in payload["tools"]]
    replayed = payload["messages"][1]["content"][0]["name"]
    for name in [*sent, replayed]:
        assert _WIRE_NAME.fullmatch(name), name
    assert response.tool_calls[0].name == "tool_output.search"


def test_bedrock_sends_image_parts_as_converse_image_blocks(monkeypatch):
    import base64

    observed: dict = {}
    _bedrock_client(monkeypatch, observed, [{"text": "A login form."}])
    provider = BedrockProvider(_config(ProviderKind.BEDROCK))
    screenshot = base64.b64encode(b"\x89PNG-bytes").decode("ascii")
    request = ModelRequest(
        messages=[
            ModelMessage(role="system", content="Describe screenshots."),
            ModelMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What is on screen?"},
                    {"type": "image", "media_type": "image/png", "data": screenshot},
                ],
            ),
        ]
    )

    response = asyncio.run(provider.complete(request))

    assert response.text == "A login form."
    assert observed["messages"] == [
        {
            "role": "user",
            "content": [
                {"text": "What is on screen?"},
                {
                    "image": {
                        "format": "png",
                        "source": {"bytes": b"\x89PNG-bytes"},
                    }
                },
            ],
        }
    ]
    assert screenshot not in json.dumps(observed["messages"], default=str)
    assert observed["system"] == [{"text": "Describe screenshots."}]

    unsupported = ModelRequest(
        messages=[
            ModelMessage(
                role="user",
                content=[
                    {"type": "image", "media_type": "image/svg+xml", "data": "PHN2Zz4="}
                ],
            )
        ]
    )
    with pytest.raises(ProviderError, match="png, jpeg, gif or webp"):
        asyncio.run(provider.complete(unsupported))


def test_bedrock_tool_names_are_wire_safe_and_decoded(monkeypatch):
    observed: dict = {}
    _bedrock_client(
        monkeypatch,
        observed,
        [
            {
                "toolUse": {
                    "toolUseId": "use_1",
                    "name": "tool_output_search",
                    "input": {},
                }
            }
        ],
    )
    provider = BedrockProvider(
        _config(
            ProviderKind.BEDROCK,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        )
    )

    response = asyncio.run(provider.complete(_dotted_request()))

    sent = [tool["toolSpec"]["name"] for tool in observed["toolConfig"]["tools"]]
    replayed = observed["messages"][1]["content"][0]["toolUse"]["name"]
    for name in [*sent, replayed]:
        assert _WIRE_NAME.fullmatch(name), name
    assert response.tool_calls[0].name == "tool_output.search"


def test_gemini_function_calls_without_an_id_get_a_stable_one(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")
    from nebula.v3.providers import ModelToolResult

    observed = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "responseId": "gemini_7",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "lookup_asset",
                                        "args": {"address": "10.0.0.1"},
                                    }
                                },
                                {
                                    "functionCall": {
                                        "name": "lookup_asset",
                                        "args": {"address": "10.0.0.2"},
                                    }
                                },
                            ]
                        },
                    }
                ],
                "usageMetadata": {"totalTokenCount": 3},
            },
        )

    provider = GeminiProvider(
        _keyed_config(ProviderKind.GEMINI, tools=True, strict_tools=True),
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="inspect")], tools=[TOOL]
    )

    first = asyncio.run(provider.complete(request))
    second = asyncio.run(provider.complete(request))

    ids = [call.id for call in first.tool_calls]
    assert all(ids) and len(set(ids)) == 2
    assert ids == [call.id for call in second.tool_calls]
    assert [call.arguments["address"] for call in first.tool_calls] == [
        "10.0.0.1",
        "10.0.0.2",
    ]

    # A synthesised id is Nebula's own and is not echoed back to Gemini; an id
    # the model did send is replayed unchanged.
    replay = request.model_copy(
        update={
            "tool_results": [
                ModelToolResult(
                    call_id=ids[0], name="lookup_asset", arguments={}, output="{}"
                ),
                ModelToolResult(
                    call_id="fc_real", name="lookup_asset", arguments={}, output="{}"
                ),
            ]
        }
    )
    asyncio.run(provider.complete(replay))
    contents = observed["payload"]["contents"]
    calls = [
        part["functionCall"]
        for item in contents
        for part in item["parts"]
        if "functionCall" in part
    ]
    results = [
        part["functionResponse"]
        for item in contents
        for part in item["parts"]
        if "functionResponse" in part
    ]
    assert "id" not in calls[0] and "id" not in results[0]
    assert calls[1]["id"] == "fc_real" and results[1]["id"] == "fc_real"


def test_gemini_health_follows_page_tokens_and_skips_unnamed_rows(monkeypatch):
    monkeypatch.setenv("NEBULA_TEST_PROVIDER_KEY", "secret")
    pages = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        pages.append(dict(http_request.url.params))
        if http_request.url.params.get("pageToken") is None:
            return httpx.Response(
                200,
                json={"models": [{"name": "models/gemini-a"}], "nextPageToken": "t2"},
            )
        return httpx.Response(
            200,
            json={"models": [{"displayName": "no name"}, {"name": "models/gemini-b"}]},
        )

    provider = GeminiProvider(
        _keyed_config(ProviderKind.GEMINI), transport=httpx.MockTransport(handler)
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["gemini-a", "gemini-b"]
    assert pages == [{"pageSize": "1000"}, {"pageSize": "1000", "pageToken": "t2"}]


def test_openai_health_skips_model_rows_without_an_id(provider_class, kind):
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "served-a"},
                    {"object": "model"},
                    "junk",
                    {"id": 7},
                    {"id": "served-b"},
                ]
            },
        )

    provider = provider_class(_config(kind), transport=httpx.MockTransport(handler))

    health = asyncio.run(provider.health())

    assert health.healthy is True, health.detail
    assert health.models == ["served-a", "served-b"]


def test_openai_responses_omits_temperature_for_reasoning_models():
    provider = OpenAIResponsesProvider(_config(ProviderKind.OPENAI_RESPONSES))

    def payload(model: str) -> dict:
        request = ModelRequest(
            model=model,
            messages=[ModelMessage(role="user", content="Name this chat")],
            temperature=0,
        )
        return provider._payload(request, model)

    for model in (
        "o1",
        "o3-mini",
        "o4-mini-2025-04-16",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5.1",
    ):
        assert "temperature" not in payload(model), model
    for model in ("gpt-4.1", "gpt-4o-mini-2024-07-18", "test-model", "gpt-50"):
        assert payload(model)["temperature"] == 0, model


def test_openai_responses_tool_names_are_wire_safe_and_strict_is_schema_aware():
    optional = ToolDefinition(
        name="workspace.read",
        description="Read a workspace file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line_count": {"type": "integer", "default": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    observed = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "workspace_read",
                        "arguments": '{"path": "notes.md"}',
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = OpenAIResponsesProvider(
        _config(
            ProviderKind.OPENAI_RESPONSES,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handler),
    )
    request = _dotted_request()
    request = request.model_copy(update={"tools": [*request.tools, optional]})

    response = asyncio.run(provider.complete(request))

    payload = observed["payload"]
    tools = {tool["name"]: tool for tool in payload["tools"]}
    replayed = [
        item for item in payload["input"] if item.get("type") == "function_call"
    ]
    for name in [*tools, replayed[0]["name"]]:
        assert _WIRE_NAME.fullmatch(name), name
    assert tools["lookup_asset"]["strict"] is True
    # A schema with an optional property or a default is rejected by strict
    # mode, so strict is not requested for it.
    assert "strict" not in tools["workspace_read"]
    assert (
        "strict" not in tools["tool_output_search"]
        or tools["tool_output_search"]["strict"]
    )
    assert response.tool_calls[0].name == "workspace.read"
    assert response.tool_calls[0].arguments == {"path": "notes.md"}


def test_openai_strict_schema_check_covers_nested_objects():
    from nebula.v3.providers import _openai_strict_schema

    assert _openai_strict_schema(TOOL.input_schema) is True
    assert _openai_strict_schema(
        {"type": "object", "properties": {}, "additionalProperties": False}
    )
    assert not _openai_strict_schema({"type": "object", "properties": {}})
    assert not _openai_strict_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        }
    )
    assert not _openai_strict_schema(
        {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {"b": {"type": "string", "default": "x"}},
                    "required": ["b"],
                    "additionalProperties": False,
                }
            },
            "required": ["nested"],
            "additionalProperties": False,
        }
    )
    assert not _openai_strict_schema(
        {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
            "required": ["names"],
            "additionalProperties": False,
        }
    )
    # A property that happens to be called "default" is not the keyword.
    assert _openai_strict_schema(
        {
            "type": "object",
            "properties": {"default": {"type": "string"}},
            "required": ["default"],
            "additionalProperties": False,
        }
    )
