"""Standard API text-token prices used for Codex API-equivalent estimates.

Update each official source entry and ``CATALOG_VERIFIED_ON`` together. These
estimates intentionally exclude ChatGPT subscription billing, service-tier or
regional adjustments, tool-call fees, and cache-write charges that the Codex
token-usage event does not identify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal


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


__all__ = [
    "CATALOG_VERIFIED_ON",
    "CODEX_MODEL_PRICING",
    "ModelTokenPricing",
    "codex_model_pricing",
]
