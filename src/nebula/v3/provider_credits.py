"""OpenRouter account credits, read with a separate management credential.

The ordinary OpenRouter key endpoint reports a per-key spending limit. It is
not an account balance and must not drive Butler's low-credit incident.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx
from pydantic import Field, model_validator

from .credentials import CredentialError, CredentialStore
from .domain import NebulaModel, ProviderProfile, utc_now


OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_MAX_CREDIT_VALUE = Decimal("1000000000000")


class OpenRouterCreditSnapshot(NebulaModel):
    provider_id: str
    status: Literal["observed", "unavailable"]
    checked_at: datetime = Field(default_factory=utc_now)
    observed_at: datetime | None = None
    total_credits_usd: float | None = None
    total_usage_usd: float | None = None
    remaining_usd: float | None = None
    reason_code: (
        Literal[
            "management_credential_missing",
            "management_credential_unavailable",
            "management_credential_rejected",
            "provider_disabled",
            "upstream_unavailable",
            "invalid_upstream_response",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def complete_when_observed(self) -> "OpenRouterCreditSnapshot":
        amounts = (
            self.total_credits_usd,
            self.total_usage_usd,
            self.remaining_usd,
        )
        if self.status == "observed":
            if self.observed_at is None or any(value is None for value in amounts):
                raise ValueError(
                    "observed credits require amounts and observation time"
                )
            if self.reason_code is not None:
                raise ValueError("observed credits cannot have an unavailable reason")
        elif self.reason_code is None or any(value is not None for value in amounts):
            raise ValueError("unavailable credits require a reason and no amounts")
        return self


def _amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError("credit amount must be a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("credit amount is invalid") from exc
    if not amount.is_finite() or amount < 0 or amount > _MAX_CREDIT_VALUE:
        raise ValueError("credit amount is outside the supported range")
    return amount


async def read_openrouter_credits(
    profile: ProviderProfile,
    credentials: CredentialStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenRouterCreditSnapshot:
    """Read account credits; never fall back to the inference key or its limit.

    The management secret is sent only to the fixed OpenRouter HTTPS origin,
    even when a provider profile has a custom inference endpoint. The result
    contains no secret or upstream response body.
    """

    if profile.provider_type != "openrouter":
        raise ValueError("account credits are available only for OpenRouter profiles")
    checked_at = utc_now()

    def unavailable(reason: str) -> OpenRouterCreditSnapshot:
        return OpenRouterCreditSnapshot(
            provider_id=profile.id,
            status="unavailable",
            checked_at=checked_at,
            reason_code=reason,
        )

    if not profile.enabled:
        return unavailable("provider_disabled")
    reference = profile.openrouter_management_secret_ref
    if reference is None:
        return unavailable("management_credential_missing")
    try:
        secret = await asyncio.to_thread(credentials.resolve, reference)
    except CredentialError:
        return unavailable("management_credential_unavailable")

    try:
        async with httpx.AsyncClient(
            timeout=10.0, transport=transport, follow_redirects=False
        ) as client:
            response = await client.get(
                OPENROUTER_CREDITS_URL,
                headers={"Authorization": "Bearer " + secret.get_secret_value()},
            )
        if response.status_code in {401, 403}:
            return unavailable("management_credential_rejected")
        if response.status_code != 200:
            return unavailable("upstream_unavailable")
    except httpx.HTTPError:
        return unavailable("upstream_unavailable")
    try:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return unavailable("invalid_upstream_response")
        total_credits = _amount(data.get("total_credits"))
        total_usage = _amount(data.get("total_usage"))
    except (ValueError, TypeError):
        return unavailable("invalid_upstream_response")

    observed_at = utc_now()
    return OpenRouterCreditSnapshot(
        provider_id=profile.id,
        status="observed",
        checked_at=checked_at,
        observed_at=observed_at,
        total_credits_usd=float(total_credits),
        total_usage_usd=float(total_usage),
        remaining_usd=float(total_credits - total_usage),
    )


__all__ = ["OpenRouterCreditSnapshot", "read_openrouter_credits"]
