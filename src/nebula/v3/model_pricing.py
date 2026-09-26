"""Token prices: Codex API-equivalent estimates and provider-profile rates.

Codex: update each official source entry and ``CATALOG_VERIFIED_ON``
together. These estimates intentionally exclude ChatGPT subscription billing,
service-tier or regional adjustments, tool-call fees, and cache-write charges
that the Codex token-usage event does not identify.

Provider profiles: :func:`provider_token_rates` resolves what one model's
uncached input, cache reads, cache writes and output cost, from rates the
operator configured on the profile or else the prices the provider's own
catalog publishes (OpenRouter's ``pricing``), and :meth:`TokenRates.cost_usd`
is the one cost formula every provider-backed path uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


CATALOG_VERIFIED_ON = "2026-07-22"
_PER_MILLION = Decimal(1_000_000)
_SNAPSHOT_SUFFIX = r"-\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True, slots=True)
class ModelTokenPricing:
    model: str
    input_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    source_url: str
    aliases: tuple[str, ...] = ()
    long_context_threshold: int | None = None
    long_context_input_multiplier: Decimal = Decimal(1)
    long_context_output_multiplier: Decimal = Decimal(1)

    def matches(self, model: str) -> bool:
        normalized = model.strip().casefold()
        identifiers = (self.model, *self.aliases)
        return normalized in identifiers or any(
            re.fullmatch(re.escape(identifier) + _SNAPSHOT_SUFFIX, normalized)
            for identifier in identifiers
        )

    def estimate_cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        total_input = max(0, input_tokens)
        cached_input = min(total_input, max(0, cached_input_tokens))
        uncached_input = total_input - cached_input
        output = max(0, output_tokens)
        input_multiplier = Decimal(1)
        output_multiplier = Decimal(1)
        if (
            self.long_context_threshold is not None
            and total_input > self.long_context_threshold
        ):
            input_multiplier = self.long_context_input_multiplier
            output_multiplier = self.long_context_output_multiplier
        cost = (
            (
                Decimal(uncached_input) * self.input_per_million_usd
                + Decimal(cached_input) * self.cached_input_per_million_usd
            )
            * input_multiplier
            + Decimal(output) * self.output_per_million_usd * output_multiplier
        ) / _PER_MILLION
        return float(cost)


_OPENAI_MODELS = "https://developers.openai.com/api/docs/models"
CODEX_MODEL_PRICING: tuple[ModelTokenPricing, ...] = (
    ModelTokenPricing(
        model="gpt-5.6-sol",
        aliases=("gpt-5.6",),
        input_per_million_usd=Decimal("5.00"),
        cached_input_per_million_usd=Decimal("0.50"),
        output_per_million_usd=Decimal("30.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.6-sol",
        long_context_threshold=272_000,
        long_context_input_multiplier=Decimal("2"),
        long_context_output_multiplier=Decimal("1.5"),
    ),
    ModelTokenPricing(
        model="gpt-5.6-terra",
        input_per_million_usd=Decimal("2.50"),
        cached_input_per_million_usd=Decimal("0.25"),
        output_per_million_usd=Decimal("15.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.6-terra",
        long_context_threshold=272_000,
        long_context_input_multiplier=Decimal("2"),
        long_context_output_multiplier=Decimal("1.5"),
    ),
    ModelTokenPricing(
        model="gpt-5.6-luna",
        input_per_million_usd=Decimal("1.00"),
        cached_input_per_million_usd=Decimal("0.10"),
        output_per_million_usd=Decimal("6.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.6-luna",
        long_context_threshold=272_000,
        long_context_input_multiplier=Decimal("2"),
        long_context_output_multiplier=Decimal("1.5"),
    ),
    ModelTokenPricing(
        model="gpt-5.4",
        input_per_million_usd=Decimal("2.50"),
        cached_input_per_million_usd=Decimal("0.25"),
        output_per_million_usd=Decimal("15.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.4",
        long_context_threshold=272_000,
        long_context_input_multiplier=Decimal("2"),
        long_context_output_multiplier=Decimal("1.5"),
    ),
    ModelTokenPricing(
        model="gpt-5.4-mini",
        input_per_million_usd=Decimal("0.75"),
        cached_input_per_million_usd=Decimal("0.075"),
        output_per_million_usd=Decimal("4.50"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.4-mini",
    ),
    ModelTokenPricing(
        model="gpt-5.4-nano",
        input_per_million_usd=Decimal("0.20"),
        cached_input_per_million_usd=Decimal("0.02"),
        output_per_million_usd=Decimal("1.25"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.4-nano",
    ),
    ModelTokenPricing(
        model="gpt-5.3-codex",
        input_per_million_usd=Decimal("1.75"),
        cached_input_per_million_usd=Decimal("0.175"),
        output_per_million_usd=Decimal("14.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.3-codex",
    ),
    ModelTokenPricing(
        model="gpt-5.2-codex",
        input_per_million_usd=Decimal("1.75"),
        cached_input_per_million_usd=Decimal("0.175"),
        output_per_million_usd=Decimal("14.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.2-codex",
    ),
    ModelTokenPricing(
        model="gpt-5.1-codex",
        input_per_million_usd=Decimal("1.25"),
        cached_input_per_million_usd=Decimal("0.125"),
        output_per_million_usd=Decimal("10.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.1-codex",
    ),
    ModelTokenPricing(
        model="gpt-5.1-codex-max",
        input_per_million_usd=Decimal("1.25"),
        cached_input_per_million_usd=Decimal("0.125"),
        output_per_million_usd=Decimal("10.00"),
        source_url=f"{_OPENAI_MODELS}/gpt-5.1-codex-max",
    ),
)


def codex_model_pricing(model: str) -> ModelTokenPricing | None:
    return next(
        (pricing for pricing in CODEX_MODEL_PRICING if pricing.matches(model)), None
    )


# A route that reports cache writes bills them above the uncached rate: 1.25x
# for Claude's 5-minute cache (the one Nebula marks) and OpenAI's GPT-5.6.
# An unpublished write price is assumed to carry that premium, so an estimate
# is never below what the route charges.
_UNKNOWN_CACHE_WRITE_PREMIUM = Decimal("1.25")
# Profile options, USD per million tokens, that override the catalog.
_RATE_OPTIONS = {
    "input": "input_cost_per_million",
    "output": "output_cost_per_million",
    "cache_read": "cached_input_cost_per_million",
    "cache_write": "cache_write_cost_per_million",
}
# OpenRouter catalog units, USD per token.
_CATALOG_UNITS = {
    "input": "prompt",
    "output": "completion",
    "cache_read": "input_cache_read",
    "cache_write": "input_cache_write",
}


@dataclass(frozen=True, slots=True)
class TokenRates:
    """USD per million tokens of each kind a route bills."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> float:
        """Cost of one usage record whose ``input_tokens`` is the whole prompt.

        Cache reads and writes are parts of ``input_tokens`` (every adapter
        normalizes usage that way), so only the rest is billed at the
        uncached rate.
        """

        prompt = max(0, input_tokens)
        read = min(prompt, max(0, cached_input_tokens))
        written = min(prompt - read, max(0, cache_creation_input_tokens))
        uncached = prompt - read - written
        cost = (
            Decimal(uncached) * self.input
            + Decimal(read) * self.cache_read
            + Decimal(written) * self.cache_write
            + Decimal(max(0, output_tokens)) * self.output
        ) / _PER_MILLION
        return float(cost)


def _rate(value: Any, *, scale: Decimal = Decimal(1)) -> Decimal | None:
    """A finite, non-negative price, or ``None`` when absent or unusable."""

    if value is None or isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value))
    except (
        InvalidOperation
    ):  # diagnostic-expected: an unusable price is treated as unpublished
        return None
    if not rate.is_finite() or rate < 0:
        return None
    return rate * scale


def combined_billed_cost(left: Any, right: Any) -> float | None:
    """What two usage records were billed together, when every part says.

    A record with no tokens adds nothing. A part the route did not bill makes
    the sum unknown, so the combined usage is priced from rates instead.
    """

    costs: list[float] = []
    for usage in (left, right):
        cost = getattr(usage, "cost_usd", None)
        if cost is None:
            if getattr(usage, "input_tokens", 0) or getattr(usage, "output_tokens", 0):
                return None
            continue
        costs.append(float(cost))
    # Rounded below any route's billing precision, so sums carry no float noise.
    return round(sum(costs), 12) if costs else None


def provider_token_rates(
    options: Mapping[str, Any], catalog: Mapping[str, Any] | None = None
) -> TokenRates:
    """The rates one provider-profile model bills, each resolved on its own.

    A rate the operator configured on the profile wins; otherwise the price
    the provider's catalog publishes for the exact model. An unknown input or
    output rate is zero, so a route with no published prices costs nothing,
    as before. An unknown cache-read rate is the full input rate and an
    unknown cache-write rate carries the usual write premium over it, so
    caching never makes an estimate lower than the bill.
    """

    prices = catalog or {}

    def resolve(kind: str) -> Decimal | None:
        configured = _rate(options.get(_RATE_OPTIONS[kind]))
        if configured is not None:
            return configured
        return _rate(prices.get(_CATALOG_UNITS[kind]), scale=_PER_MILLION)

    input_rate = resolve("input") or Decimal(0)
    cache_read = resolve("cache_read")
    cache_write = resolve("cache_write")
    return TokenRates(
        input=input_rate,
        output=resolve("output") or Decimal(0),
        cache_read=input_rate if cache_read is None else cache_read,
        cache_write=(
            input_rate * _UNKNOWN_CACHE_WRITE_PREMIUM
            if cache_write is None
            else cache_write
        ),
    )


__all__ = [
    "CATALOG_VERIFIED_ON",
    "CODEX_MODEL_PRICING",
    "ModelTokenPricing",
    "TokenRates",
    "codex_model_pricing",
    "combined_billed_cost",
    "provider_token_rates",
]
