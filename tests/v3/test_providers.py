import asyncio
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
