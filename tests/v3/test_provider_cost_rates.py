"""Cost estimates bill cache reads and writes at their own rates.

Every provider-backed cost (mission specialists, mission context compaction,
chat compaction snapshots and their budget checks) multiplied the whole
prompt by ``input_cost_per_million``. Since #602 Claude routes serve most of
a repeated prompt from cache, and OpenAI, DeepSeek and Gemini did already, so
estimates overstated the bill and a mission cost budget stopped work that was
still inside it. Rates only ever came from profile options no screen sets, so
most profiles estimated nothing at all.

Rates now resolve per kind (uncached input, cache read, cache write, output)
from the profile's options, else the prices the provider's catalog publishes
for the exact model (OpenRouter), through one shared cost function. An
unpublished cache price never lowers an estimate below the bill.
"""

import asyncio
from decimal import Decimal

import httpx
import pytest

from nebula.v3.context import (
    ContextCallBudget,
    ContextCompactionError,
    ContextCompactor,
)
from nebula.v3.domain import ChatRole, ChatTokenUsage, ProviderProfile
from nebula.v3.model_catalog import openrouter_model_routes
from nebula.v3.model_pricing import (
    TokenRates,
    combined_billed_cost,
    provider_token_rates,
)
from nebula.v3.orchestration import (
    MissionRuntime,
    ModelSpecialist,
    PlannedTask,
    SpecialistContext,
    SpecialistRole,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
    ToolCall,
    _openai_usage,
    provider_from_profile,
    usage_cost_usd,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_context import (
    MemoryProvider,
    _chat_history,
    _compact,
    _owner,
    _profile,
)
from tests.v3.test_specialist_tool_batches import (
    FINISH,
    RecordingBroker,
    _context,
    _specialist,
)

# OpenRouter's published prices for anthropic/claude-haiku-4.5, USD per token.
HAIKU_PRICES = {
    "prompt": "0.000001",
    "completion": "0.000005",
    "input_cache_read": "0.0000001",
    "input_cache_write": "0.00000125",
}
# A cached turn: 9,000 of 10,000 prompt tokens read, 400 written.
CACHED = ModelUsage(
    input_tokens=10_000,
    output_tokens=200,
    total_tokens=10_200,
    cached_input_tokens=9_000,
    cache_creation_input_tokens=400,
)
# 600 uncached at $1/M + 9,000 at $0.10/M + 400 at $1.25/M + 200 at $5/M.
CACHED_HAIKU_COST = 0.0006 + 0.0009 + 0.0005 + 0.001


def _config(**update) -> ProviderConfig:
    return ProviderConfig(
        id="priced",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:8000/v1",
        default_model="model-a",
        local=True,
        capabilities=ModelCapabilities(tools=True),
    ).model_copy(update=update)


# --- Rate resolution and the shared formula ------------------------------------


def test_catalog_prices_become_per_million_rates_for_each_kind():
    rates = provider_token_rates({}, HAIKU_PRICES)

    assert rates == TokenRates(
        input=Decimal(1),
        output=Decimal(5),
        cache_read=Decimal("0.1"),
        cache_write=Decimal("1.25"),
    )


def test_cache_tokens_are_billed_at_their_own_rates():
    rates = provider_token_rates({}, HAIKU_PRICES)

    assert rates.cost_usd(
        input_tokens=10_000,
        output_tokens=200,
        cached_input_tokens=9_000,
        cache_creation_input_tokens=400,
    ) == pytest.approx(CACHED_HAIKU_COST)
    # Before: every prompt token at the input rate.
    assert CACHED_HAIKU_COST < 10_000 * 1e-6 + 200 * 5e-6


def test_profile_options_override_the_catalog_one_rate_at_a_time():
    rates = provider_token_rates(
        {"input_cost_per_million": 2, "cache_write_cost_per_million": "3.5"},
        HAIKU_PRICES,
    )

    assert rates == TokenRates(
        input=Decimal(2),
        output=Decimal(5),
        cache_read=Decimal("0.1"),
        cache_write=Decimal("3.5"),
    )


def test_unpublished_cache_prices_never_lower_an_estimate():
    """An unknown read costs the input rate; an unknown write the usual premium."""

    rates = provider_token_rates(
        {"input_cost_per_million": 4, "output_cost_per_million": 20}
    )

    assert rates.cache_read == Decimal(4)
    assert rates.cache_write == Decimal(5)
    assert rates.cost_usd(
        input_tokens=1_000,
        output_tokens=0,
        cached_input_tokens=500,
        cache_creation_input_tokens=500,
    ) >= rates.cost_usd(input_tokens=1_000, output_tokens=0)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"input_cost_per_million": "not a number", "output_cost_per_million": -1},
        {"input_cost_per_million": True, "output_cost_per_million": float("nan")},
    ],
)
def test_a_route_without_usable_prices_still_costs_nothing(options):
    rates = provider_token_rates(options, {"prompt": "-1", "completion": "abc"})

    assert rates.cost_usd(input_tokens=1_000_000, output_tokens=1_000_000) == 0


def test_cache_counts_larger_than_the_prompt_are_clamped():
    rates = provider_token_rates({}, HAIKU_PRICES)

    # Reads first, then writes, never more than the prompt holds.
    assert rates.cost_usd(
        input_tokens=100,
        output_tokens=0,
        cached_input_tokens=80,
        cache_creation_input_tokens=80,
    ) == pytest.approx(80 * 1e-7 + 20 * 1.25e-6)


def test_usage_cost_prices_the_exact_model_or_the_default():
    config = _config(model_prices={"model-a": HAIKU_PRICES})

    assert usage_cost_usd(config, None, CACHED) == pytest.approx(CACHED_HAIKU_COST)
    assert usage_cost_usd(config, "model-a", CACHED) == pytest.approx(CACHED_HAIKU_COST)
    # Durable chat usage prices the same.
    chat_usage = ChatTokenUsage.model_validate(CACHED.model_dump())
    assert usage_cost_usd(config, "model-a", chat_usage) == pytest.approx(
        CACHED_HAIKU_COST
    )
    # A model the catalog does not price has no estimate.
    assert usage_cost_usd(config, "model-b", CACHED) == 0


def test_profiles_carry_catalog_prices_to_the_adapter():
    profile = ProviderProfile(
        id="openrouter",
        name="OpenRouter",
        provider_type="openrouter",
        secret_ref="env:NEBULA_COST_TEST_KEY",
        model_allowlist=["anthropic/claude-haiku-4.5"],
        metadata={
            "default_model": "anthropic/claude-haiku-4.5",
            "model_descriptors": [
                {
                    "id": "anthropic/claude-haiku-4.5",
                    "name": "Haiku",
                    "pricing": HAIKU_PRICES,
                },
                # An alias carries no prices of its own here: its target's apply.
                {
                    "id": "~anthropic/claude-haiku-latest",
                    "name": "Haiku latest",
                    "alias_target": "anthropic/claude-haiku-4.5",
                },
                {"id": "unpriced/model", "name": "Unpriced"},
                # Malformed metadata never breaks the profile.
                {"id": "odd/model", "name": "Odd", "pricing": ["0.1"]},
            ],
        },
    )

    config = provider_from_profile(profile).config

    assert config.model_prices == {
        "anthropic/claude-haiku-4.5": HAIKU_PRICES,
        "~anthropic/claude-haiku-latest": HAIKU_PRICES,
    }
    assert usage_cost_usd(
        config, "~anthropic/claude-haiku-latest", CACHED
    ) == pytest.approx(CACHED_HAIKU_COST)
    # A profile saved before descriptors carried prices still loads.
    legacy = provider_from_profile(
        profile.model_copy(update={"metadata": {"default_model": "m"}})
    )
    assert legacy.config.model_prices == {}


def test_measured_endpoints_raise_the_catalog_price_to_the_dearest():
    """OpenRouter's model price is its cheapest endpoint's; routing may not be."""

    endpoints = openrouter_model_routes(
        {
            "data": {
                "id": "deepseek/deepseek-v4.1-flash",
                "endpoints": [
                    {
                        "provider_name": "InferenceNet",
                        "tag": "inference-net",
                        "context_length": 1_000_000,
                        "pricing": {
                            "prompt": "0.000000035",
                            "completion": "0.00000029",
                        },
                    },
                    {
                        "provider_name": "DeepSeek",
                        "tag": "deepseek",
                        "context_length": 1_000_000,
                        "pricing": {
                            "prompt": "0.00000015",
                            "completion": "0.0000006",
                            "discount": 0,
                            # Peak hours cost double.
                            "overrides": [
                                {"prompt": "0.0000003", "completion": "0.0000012"}
                            ],
                        },
                    },
                    {
                        "provider_name": "Together",
                        "tag": "together",
                        "context_length": 1_000_000,
                        "pricing": {"prompt": "0.0000003", "completion": "0.0000012"},
                    },
                ],
            }
        },
        model="deepseek/deepseek-v4.1-flash",
    )
    assert endpoints[1].pricing == {"prompt": "0.0000003", "completion": "0.0000012"}
    descriptor = {
        "id": "deepseek/deepseek-v4.1-flash",
        "name": "DeepSeek",
        "pricing": {
            "prompt": "0.000000035",
            "completion": "0.00000029",
            "input_cache_read": "0.000000001",
        },
        "route_limits": [route.model_dump(mode="json") for route in endpoints],
        "route_limits_verified": True,
    }

    def config(item):
        return provider_from_profile(
            ProviderProfile(
                id="openrouter",
                name="OpenRouter",
                provider_type="openrouter",
                secret_ref="env:NEBULA_COST_TEST_KEY",
                model_allowlist=[item["id"]],
                metadata={"default_model": item["id"], "model_descriptors": [item]},
            )
        ).config

    assert config(descriptor).model_prices[descriptor["id"]] == {
        "prompt": "0.0000003",
        "completion": "0.0000012",
        "input_cache_read": "0.000000001",
    }
    # Unmeasured endpoints prove nothing: the model-level price stands.
    unverified = {**descriptor, "route_limits_verified": False}
    assert config(unverified).model_prices[descriptor["id"]] == descriptor["pricing"]


def test_openrouter_reports_what_each_generation_was_billed():
    usage = {
        "prompt_tokens": 7_795,
        "completion_tokens": 16,
        "total_tokens": 7_811,
        "cost": 0.0023577,
        "prompt_tokens_details": {"cached_tokens": 0},
    }

    assert _openai_usage(usage, billed=True).cost_usd == pytest.approx(0.0023577)
    # Other gateways' cost fields are not trusted.
    assert _openai_usage(usage).cost_usd is None
    # With the operator's own upstream key, OpenRouter's fee is only part of it.
    byok = {
        **usage,
        "cost": 0.0001,
        "is_byok": True,
        "cost_details": {"upstream_inference_cost": 0.0023577},
    }
    assert _openai_usage(byok, billed=True).cost_usd == pytest.approx(0.0024577)
    for unusable in (None, -1, float("nan"), True, "0.01"):
        assert _openai_usage({**usage, "cost": unusable}, billed=True).cost_usd is None


def test_openrouter_adapter_carries_the_billed_cost(monkeypatch):
    monkeypatch.setenv("NEBULA_COST_TEST_KEY", "not-a-real-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "deepseek/deepseek-v4.1-flash",
                "provider": "Together",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7_795,
                    "completion_tokens": 16,
                    "total_tokens": 7_811,
                    "cost": 0.0023577,
                },
            },
        )

    def adapter(flavor):
        return OpenAICompatibleProvider(
            ProviderConfig(
                id="gateway",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                flavor=flavor,
                base_url="https://gateway.example.com/v1",
                default_model="deepseek/deepseek-v4.1-flash",
                api_key_env="NEBULA_COST_TEST_KEY",
                model_prices={
                    "deepseek/deepseek-v4.1-flash": {"prompt": "0.000000035"}
                },
            ),
            transport=httpx.MockTransport(handler),
        )

    request = ModelRequest(messages=[ModelMessage(role="user", content="ok?")])
    openrouter = adapter(ProviderFlavor.OPENROUTER)
    routed = asyncio.run(openrouter.complete(request))
    generic = asyncio.run(adapter(ProviderFlavor.CUSTOM).complete(request))

    # The bill, not the cheapest endpoint's price (7,795 x $0.035/M).
    assert usage_cost_usd(openrouter.config, None, routed.usage) == pytest.approx(
        0.0023577
    )
    assert generic.usage.cost_usd is None
    assert usage_cost_usd(openrouter.config, None, generic.usage) == pytest.approx(
        7_795 * 0.035e-6
    )


def test_billed_costs_sum_only_while_every_part_was_billed():
    billed = ChatTokenUsage(
        input_tokens=10, output_tokens=1, total_tokens=11, cost_usd=0.25
    )
    unbilled = ChatTokenUsage(input_tokens=10, output_tokens=1, total_tokens=11)

    assert combined_billed_cost(ChatTokenUsage(), billed) == 0.25
    assert combined_billed_cost(billed, billed) == 0.5
    assert combined_billed_cost(billed, unbilled) is None
    assert combined_billed_cost(ChatTokenUsage(), ChatTokenUsage()) is None
    # The summing paths carry it.
    assert ContextCompactor._add_usage(billed, billed).cost_usd == 0.5
    assert MissionRuntime._add_context_usage(billed, billed).cost_usd == 0.5
    assert MissionRuntime._add_context_usage(billed, unbilled).cost_usd is None


# --- Each cost path ---------------------------------------------------------------


class _PricedProvider(ModelProvider):
    def __init__(self, usage: ModelUsage, tool_calls=None) -> None:
        super().__init__(_config(model_prices={"model-a": HAIKU_PRICES}))
        self.usage = usage
        self.tool_calls = list(tool_calls or [])

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text="The service is mapped.",
            tool_calls=self.tool_calls.pop(0) if self.tool_calls else [],
            usage=self.usage,
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def test_mission_analysis_specialist_prices_cache_reads(tmp_path):
    specialist = ModelSpecialist(_PricedProvider(CACHED), model="model-a")

    result = asyncio.run(
        specialist.run(
            SpecialistContext(
                engagement_id="engagement-1",
                run_id="run-1",
                task=PlannedTask(
                    id="scope",
                    role=SpecialistRole.SCOPE_PLANNING,
                    title="Review scope",
                    instructions="Review the scope",
                ),
                objective="Review the scope",
                prior_results={},
                allowed_tools=frozenset(),
            )
        )
    )

    assert result.cost_usd == pytest.approx(CACHED_HAIKU_COST)


def test_mission_tool_specialist_prices_its_routing_call(tmp_path):
    finish = ToolCall(
        id="call-1",
        name=FINISH,
        arguments={
            "status": "complete",
            "summary": "The service is mapped",
            "rationale": "Enough observations",
        },
    )
    provider = _PricedProvider(CACHED, tool_calls=[[finish]])
    specialist = _specialist(tmp_path, provider, RecordingBroker())

    result = asyncio.run(specialist.run(_context()))

    assert (result.input_tokens, result.output_tokens) == (10_000, 200)
    assert result.cost_usd == pytest.approx(CACHED_HAIKU_COST)


def test_mission_context_spend_keeps_its_cache_counts_and_prices_them():
    specialist = ModelSpecialist(_PricedProvider(CACHED), model="model-a")
    usage = ChatTokenUsage.model_validate(CACHED.model_dump())

    total = MissionRuntime._add_context_usage(usage, usage)

    assert (total.cached_input_tokens, total.cache_creation_input_tokens) == (
        18_000,
        800,
    )
    assert MissionRuntime._context_usage_cost(specialist, total) == pytest.approx(
        2 * CACHED_HAIKU_COST
    )


def test_compaction_snapshots_price_cache_reads(tmp_path):
    class CachingProvider(MemoryProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            response.usage = CACHED
            return response

    store = NebulaStore(tmp_path / "priced-context.db")
    profile = _profile()
    session = _owner(store, profile)
    sources = _chat_history(store, session, {1: (ChatRole.USER, "Keep port 8443.")})
    provider = CachingProvider(profile.id)
    provider.config = provider.config.model_copy(
        update={"model_prices": {"model-a": HAIKU_PRICES}}
    )

    result = _compact(store, session, profile, provider, sources)

    assert result.snapshot.cost_usd == pytest.approx(CACHED_HAIKU_COST)


def test_compaction_budget_counts_what_was_spent_at_cache_rates():
    """A cost budget the cached spend fits in no longer stops the compactor."""

    provider = _PricedProvider(CACHED)
    request = ModelRequest(
        model="model-a", messages=[ModelMessage(role="user", content="x" * 30)]
    )
    spent = ChatTokenUsage(
        input_tokens=100_000,
        output_tokens=0,
        total_tokens=100_000,
        cached_input_tokens=95_000,
    )
    # 5,000 uncached at $1/M + 95,000 read at $0.10/M = $0.0145, where the
    # whole prompt at the input rate was $0.10.
    budget = ContextCallBudget(max_cost_usd=0.02)

    ContextCompactor._enforce_call_budget(
        provider=provider,
        request=request,
        prior_usage=spent,
        attempt_usage=ChatTokenUsage(),
        budget=budget,
    )
    with pytest.raises(ContextCompactionError, match="cost budget"):
        ContextCompactor._enforce_call_budget(
            provider=provider,
            request=request,
            prior_usage=spent.model_copy(update={"cached_input_tokens": 0}),
            attempt_usage=ChatTokenUsage(),
            budget=budget,
        )
