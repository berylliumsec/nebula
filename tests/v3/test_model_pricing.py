from decimal import Decimal

import pytest

from nebula.v3.model_pricing import (
    CATALOG_VERIFIED_ON,
    CODEX_MODEL_PRICING,
    ModelTokenPricing,
    codex_model_pricing,
)


def test_codex_catalog_resolves_aliases_snapshots_and_unknown_models():
    assert CATALOG_VERIFIED_ON == "2026-07-22"
    assert len(CODEX_MODEL_PRICING) == 10
    assert codex_model_pricing(" GPT-5.6 ").model == "gpt-5.6-sol"
    assert codex_model_pricing("gpt-5.4-mini-2026-03-17").model == "gpt-5.4-mini"
    assert codex_model_pricing("future-codex-model") is None


def test_catalog_prices_cached_and_uncached_tokens():
    pricing = codex_model_pricing("gpt-5.3-codex")
    assert pricing is not None
    assert pricing.estimate_cost_usd(
        input_tokens=1_000_000,
        cached_input_tokens=250_000,
        output_tokens=100_000,
    ) == pytest.approx(2.75625)


def test_catalog_applies_long_context_rates_and_sanitizes_counts():
    pricing = codex_model_pricing("gpt-5.6-terra")
    assert pricing is not None
    assert pricing.estimate_cost_usd(
        input_tokens=300_000,
        output_tokens=100_000,
    ) == pytest.approx(3.75)
    assert (
        pricing.estimate_cost_usd(
            input_tokens=-1,
            cached_input_tokens=10,
            output_tokens=-2,
        )
        == 0
    )


def test_catalog_entry_without_long_context_surcharge_uses_standard_rates():
    pricing = ModelTokenPricing(
        model="fixture",
        aliases=("alias",),
        input_per_million_usd=Decimal("1"),
        cached_input_per_million_usd=Decimal("0.1"),
        output_per_million_usd=Decimal("2"),
        source_url="https://example.test/pricing",
    )
    assert pricing.matches("alias-2026-07-22")
    assert not pricing.matches("alias-preview")
    assert pricing.estimate_cost_usd(
        input_tokens=1_000_000,
        cached_input_tokens=2_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(2.1)
