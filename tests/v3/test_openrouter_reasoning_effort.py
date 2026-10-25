"""OpenRouter effort requests follow the exact model catalog."""

from nebula.v3.domain import ProviderProfile
from nebula.v3.model_catalog import openrouter_models
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderFlavor,
    config_from_catalog,
    provider_from_profile,
)


DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"


def _effort(provider: OpenAICompatibleProvider, model: str) -> str:
    request = ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Hi")],
        reasoning_effort="xhigh",
    )
    return provider._payload(request, provider.require(request))["reasoning"]["effort"]


def test_openrouter_uses_exact_models_advertised_max_for_xhigh():
    descriptors = openrouter_models(
        {
            "data": [
                {
                    "id": DEEPSEEK,
                    "supported_parameters": ["reasoning"],
                    "reasoning": {
                        "supported_efforts": ["max", "high", "low", "none"],
                    },
                },
                {
                    "id": "other/xhigh-model",
                    "supported_parameters": ["reasoning"],
                    "reasoning": {"supported_efforts": ["xhigh", "high", "none"]},
                },
            ]
        }
    )
    assert descriptors[0].reasoning_supported_efforts == ["max", "high", "low", "none"]
    profile = ProviderProfile(
        name="OpenRouter",
        provider_type="openrouter",
        is_local=False,
        secret_ref="env:OPEN_ROUTER_TEST_KEY",
        model_allowlist=[DEEPSEEK, "other/xhigh-model"],
        metadata={"model_descriptors": [item.model_dump() for item in descriptors]},
    )
    provider = provider_from_profile(profile)

    assert provider.config.model_reasoning_efforts[DEEPSEEK] == [
        "max",
        "high",
        "low",
        "none",
    ]
    assert _effort(provider, DEEPSEEK) == "max"
    assert _effort(provider, "other/xhigh-model") == "xhigh"


def test_openrouter_existing_deepseek_profile_maps_xhigh_before_catalog_refresh():
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-legacy-effort",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model=DEEPSEEK,
        )
    )

    assert _effort(provider, DEEPSEEK) == "max"


def test_openrouter_unknown_model_without_effort_catalog_keeps_requested_value():
    model = "other/model"
    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter-unknown-effort",
            flavor=ProviderFlavor.OPENROUTER,
            api_key_value="test-key",
            default_model=model,
        )
    )

    assert _effort(provider, model) == "xhigh"
