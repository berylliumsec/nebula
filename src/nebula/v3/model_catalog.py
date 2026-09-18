"""Display-only model metadata. Advertised capabilities never grant authority."""

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field


class ModelDescriptor(BaseModel):
    id: str
    name: str
    description: str | None = None
    canonical_slug: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supported_parameters: list[str] = Field(default_factory=list)
    # Preserve decimal strings and upstream billing keys/units, including zero.
    pricing: dict[str, str] = Field(default_factory=dict)
    expiration_date: str | None = None
    route_limits: list["ModelRouteDescriptor"] = Field(default_factory=list)
    route_limits_verified: bool = False
    route_limits_checked_at: str | None = None


class ModelRouteDescriptor(BaseModel):
    provider_name: str
    context_window: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    supported_parameters: list[str] = Field(default_factory=list)
    status: int = 0


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def openrouter_models(payload: Any) -> list[ModelDescriptor]:
    """Fail atomically on invalid identities; ignore malformed optional metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Invalid model catalog")
    if len(payload["data"]) > 10_000:
        raise ValueError("Model catalog exceeds discovery limit")
    models: dict[str, ModelDescriptor] = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Invalid model identity")
        identity = item["id"]
        if not identity.strip() or len(identity) > 500:
            raise ValueError("Invalid model identity")
        architecture = item.get("architecture")
        architecture = architecture if isinstance(architecture, dict) else {}
        top = item.get("top_provider")
        top = top if isinstance(top, dict) else {}
        pricing = {}
        raw_prices = item.get("pricing")
        for unit, raw in raw_prices.items() if isinstance(raw_prices, dict) else []:
            if not isinstance(raw, str) or len(raw) > 100:
                continue
            try:
                price = Decimal(raw)
            except (
                InvalidOperation
            ):  # diagnostic-expected: non-numeric catalog price is omitted
                continue
            if price.is_finite() and price >= 0:
                pricing[unit] = raw
        name = item.get("name")
        description = item.get("description")
        canonical_slug = item.get("canonical_slug")
        expiration_date = item.get("expiration_date")
        models.setdefault(
            identity,
            ModelDescriptor(
                id=identity,
                name=name[:500] if isinstance(name, str) and name.strip() else identity,
                description=(
                    description[:2_000]
                    if isinstance(description, str) and description.strip()
                    else None
                ),
                canonical_slug=(
                    canonical_slug[:500]
                    if isinstance(canonical_slug, str) and canonical_slug.strip()
                    else None
                ),
                context_window=_positive_int(item.get("context_length")),
                max_output_tokens=_positive_int(top.get("max_completion_tokens")),
                input_modalities=_strings(architecture.get("input_modalities")),
                output_modalities=_strings(architecture.get("output_modalities")),
                supported_parameters=_strings(item.get("supported_parameters")),
                pricing=pricing,
                expiration_date=(
                    expiration_date[:100]
                    if isinstance(expiration_date, str) and expiration_date.strip()
                    else None
                ),
            ),
        )
    return list(models.values())


def openrouter_model_routes(payload: Any, *, model: str) -> list[ModelRouteDescriptor]:
    """Parse the authoritative per-endpoint limits for one exact model."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("id") != model:
        raise ValueError("Invalid OpenRouter endpoint catalog identity")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or len(endpoints) > 1_000:
        raise ValueError("Invalid OpenRouter endpoint catalog")
    routes: list[ModelRouteDescriptor] = []
    for item in endpoints:
        if not isinstance(item, dict):
            raise ValueError("Invalid OpenRouter endpoint record")
        provider_name = item.get("provider_name")
        context_window = _positive_int(item.get("context_length"))
        max_input = _positive_int(item.get("max_prompt_tokens"))
        max_output = _positive_int(item.get("max_completion_tokens"))
        status = item.get("status", 0)
        if (
            not isinstance(provider_name, str)
            or not provider_name.strip()
            or len(provider_name) > 500
            or context_window is None
            or max_input is None
            or max_output is None
            or type(status) is not int
        ):
            raise ValueError("Incomplete OpenRouter endpoint limits")
        routes.append(
            ModelRouteDescriptor(
                provider_name=provider_name,
                context_window=context_window,
                max_input_tokens=max_input,
                max_output_tokens=max_output,
                supported_parameters=_strings(item.get("supported_parameters")),
                status=status,
            )
        )
    return routes
