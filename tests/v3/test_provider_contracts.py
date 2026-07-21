import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from nebula.v3.domain import ProviderProfile
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    ToolChoice,
    ToolDefinition,
    provider_from_profile,
)


def _required_request() -> ModelRequest:
    return ModelRequest(
        model="model-a",
        messages=[ModelMessage(role="user", content="route")],
        tools=[
            ToolDefinition(
                name="safe_probe",
                description="Probe",
                input_schema={
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice=ToolChoice.REQUIRED,
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="safe_probe",
                arguments={"nonce": "abc"},
                output={"ok": True},
            )
        ],
    )


def test_profile_runtime_preserves_locality_allowlist_and_legacy_flavor():
    runtime = provider_from_profile(
        ProviderProfile(
            id="legacy-local",
            name="Legacy compatible",
            provider_type="openai-compatible",
            endpoint="http://127.0.0.1:9000/v1",
            is_local=True,
            model_allowlist=["allowed-model"],
            metadata={"default_model": "allowed-model"},
        )
    )

    assert runtime.config.flavor == ProviderFlavor.CUSTOM
    assert runtime.config.local is True
    assert runtime.config.model_allowlist == ["allowed-model"]
    with pytest.raises(ProviderError, match="not allowed"):
        asyncio.run(
            runtime.complete(
                ModelRequest(
                    model="different-model",
                    messages=[ModelMessage(role="user", content="hello")],
                )
            )
        )


def test_disabled_provider_is_rejected_before_network_access():
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="disabled",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
            enabled=False,
        )
    )

    with pytest.raises(ProviderError, match="disabled"):
        asyncio.run(
            provider.complete(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        )


def test_simple_compatible_chat_omits_tool_only_parameters():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "model-a",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="compatible",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
        ),
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        provider.complete(
            ModelRequest(messages=[ModelMessage(role="user", content="hello")])
        )
    )

    assert "parallel_tool_calls" not in observed
    assert "tools" not in observed


def test_compatible_required_tool_choice_and_paired_history_wire_contract():
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="compatible",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
            capabilities={"tools": True, "strict_tools": True},
        )
    )
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="route")],
        tools=[
            ToolDefinition(
                name="safe_probe",
                description="Probe",
                input_schema={
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice=ToolChoice.REQUIRED,
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="safe_probe",
                arguments={"nonce": "abc"},
                output={"ok": True},
            )
        ],
    )

    payload = provider._payload(request, "model-a")

    assert payload["tool_choice"] == "required"
    assert payload["parallel_tool_calls"] is False
    assert payload["messages"][-2]["tool_calls"][0]["function"] == {
        "name": "safe_probe",
        "arguments": '{"nonce": "abc"}',
    }
    assert payload["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"ok": true}',
    }


def test_vllm_payload_removes_unsupported_unique_items_without_mutating_schema():
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="vllm",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.VLLM,
            base_url="http://127.0.0.1:8001/v1",
            default_model="model-a",
            local=True,
        )
    )
    schema = {
        "type": "object",
        "properties": {
            "ports": {
                "type": "array",
                "items": {"type": "integer"},
                "uniqueItems": True,
            }
        },
        "additionalProperties": False,
    }
    request = ModelRequest(
        messages=[ModelMessage(role="user", content="route")],
        tools=[
            ToolDefinition(
                name="network_probe",
                description="Probe ports",
                input_schema=schema,
            )
        ],
    )

    payload = provider._payload(request, "model-a")

    parameters = payload["tools"][0]["function"]["parameters"]
    assert "uniqueItems" not in parameters["properties"]["ports"]
    assert schema["properties"]["ports"]["uniqueItems"] is True


def test_responses_required_tool_choice_and_paired_history_wire_contract():
    provider = OpenAIResponsesProvider(
        ProviderConfig(
            id="responses",
            kind=ProviderKind.OPENAI_RESPONSES,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
            capabilities={"tools": True, "strict_tools": True},
        )
    )

    payload = provider._payload(_required_request(), "model-a")

    assert payload["tool_choice"] == "required"
    assert payload["input"][-2]["type"] == "function_call"
    assert payload["input"][-2]["arguments"] == '{"nonce": "abc"}'
    assert payload["input"][-1]["type"] == "function_call_output"


def test_anthropic_and_gemini_required_tool_wire_contracts(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content))
        if "anthropic-version" in request.headers:
            return httpx.Response(
                200,
                json={
                    "id": "a-1",
                    "model": "model-a",
                    "content": [],
                    "stop_reason": "tool_use",
                },
            )
        return httpx.Response(200, json={"candidates": []})

    common = {
        "base_url": "https://provider.invalid/v1",
        "default_model": "model-a",
        "api_key_env": "TEST_PROVIDER_KEY",
        "capabilities": {"tools": True, "strict_tools": True},
    }
    transport = httpx.MockTransport(handler)
    asyncio.run(
        AnthropicProvider(
            ProviderConfig(
                id="anthropic",
                kind=ProviderKind.ANTHROPIC,
                flavor=ProviderFlavor.ANTHROPIC,
                **common,
            ),
            transport=transport,
        ).complete(_required_request())
    )
    asyncio.run(
        GeminiProvider(
            ProviderConfig(
                id="gemini",
                kind=ProviderKind.GEMINI,
                flavor=ProviderFlavor.GEMINI,
                **common,
            ),
            transport=transport,
        ).complete(_required_request())
    )

    assert observed[0]["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }
    assert observed[0]["messages"][-2]["content"][0]["type"] == "tool_use"
    assert observed[1]["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert observed[1]["contents"][-2]["parts"][0]["functionCall"]["id"] == "call-1"


def test_bedrock_required_tool_wire_contract(monkeypatch):
    observed = {}

    class RuntimeClient:
        def converse(self, **kwargs):
            observed.update(kwargs)
            return {"output": {"message": {"content": []}}, "stopReason": "tool_use"}

    monkeypatch.setattr(
        "nebula.v3.providers.boto3.client",
        lambda service, **kwargs: RuntimeClient(),
    )
    provider = BedrockProvider(
        ProviderConfig(
            id="bedrock",
            kind=ProviderKind.BEDROCK,
            flavor=ProviderFlavor.BEDROCK,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
            capabilities={"tools": True, "strict_tools": True},
        )
    )

    asyncio.run(provider.complete(_required_request()))

    assert observed["toolConfig"]["toolChoice"] == {"any": {}}
    assert observed["messages"][-2]["content"][0]["toolUse"]["toolUseId"] == "call-1"
    assert observed["messages"][-1]["content"][0]["toolResult"]["toolUseId"] == "call-1"


def test_provider_secrets_and_cleartext_remote_endpoints_are_rejected():
    with pytest.raises(ValidationError, match="env:NAME"):
        ProviderProfile(
            name="Bad secret",
            provider_type="custom",
            endpoint="https://provider.invalid/v1",
            secret_ref="literal-secret",
        )
    with pytest.raises(ValidationError, match="unencrypted provider endpoints"):
        ProviderConfig(
            id="cleartext",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://provider.invalid/v1",
        )


def test_public_provider_endpoint_cannot_be_labeled_local():
    with pytest.raises(ValidationError, match="local provider endpoints"):
        ProviderConfig(
            id="false-local",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://api.openai.com/v1",
            local=True,
        )
    with pytest.raises(ValidationError, match="local provider endpoints"):
        provider_from_profile(
            ProviderProfile(
                name="False local cloud",
                provider_type="openai",
                endpoint="https://api.openai.com",
                is_local=True,
                privacy={"local_only": True},
            )
        )


def test_bedrock_health_verifies_runtime_credentials_and_discovers_models(
    monkeypatch,
):
    observed = {}

    class BedrockClient:
        def list_foundation_models(self):
            return {
                "modelSummaries": [
                    {"modelId": "anthropic.claude-test"},
                    {"modelId": "amazon.nova-test"},
                ]
            }

    def client(service, *, region_name=None):
        observed.update(service=service, region=region_name)
        return BedrockClient()

    monkeypatch.setattr("nebula.v3.providers.boto3.client", client)
    provider = BedrockProvider(
        ProviderConfig(
            id="bedrock",
            kind=ProviderKind.BEDROCK,
            flavor=ProviderFlavor.BEDROCK,
            base_url="https://bedrock-runtime.amazonaws.com",
            default_model="anthropic.claude-test",
            options={"region": "us-east-1"},
        )
    )

    health = asyncio.run(provider.health())

    assert health.healthy is True
    assert health.models == ["anthropic.claude-test", "amazon.nova-test"]
    assert observed == {"service": "bedrock", "region": "us-east-1"}
