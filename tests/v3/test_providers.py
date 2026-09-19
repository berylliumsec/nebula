import asyncio
import re
import json

import httpx
import pytest

from nebula.v3.providers import (
    AnthropicProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderError,
    ProviderContextLengthError,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ProviderRegistry,
    StreamEventType,
    ToolDefinition,
    UnsupportedCapability,
    config_from_catalog,
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
                            "reasoning": "Private chain of thought.",
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "text": "Consider the token.",
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
    assert result.text == "FLASH_OK"
    assert "Private chain of thought." in result.reasoning
    assert "Consider the token." in result.reasoning


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
                        "top_provider": {"max_completion_tokens": 32_000},
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
    assert descriptor.pricing["prompt"] == "0.000003"
    assert descriptor.expiration_date == "2027-06-30"


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


def _openrouter_discovery(allowed, *, filtered_status=200):
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {}})
        if request.url.path == "/api/v1/providers":
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
