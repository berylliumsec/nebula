import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from nebula.v3.domain import ProviderProfile
from nebula.v3.providers import (
    BedrockProvider,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    provider_from_profile,
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
