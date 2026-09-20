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
    # OpenRouter's `top_provider.context_length`: the window the primary route
    # serves, where `context_window` is the maximum across every endpoint.
    primary_route_context_window: int | None = None
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supported_parameters: list[str] = Field(default_factory=list)
    # Preserve decimal strings and upstream billing keys/units, including zero.
    pricing: dict[str, str] = Field(default_factory=dict)
    expiration_date: str | None = None
    # Exact model an alias ("~author/family-latest") currently redirects to.
    alias_target: str | None = None
    route_limits: list["ModelRouteDescriptor"] = Field(default_factory=list)
    route_limits_verified: bool = False
    route_limits_checked_at: str | None = None
    # Slug whose endpoints were measured; an alias is measured through its target.
    route_limits_source_model: str | None = None


# Descriptor keys owned by endpoint verification, not by catalog discovery.
ROUTE_LIMIT_FIELDS = (
    "route_limits",
    "route_limits_verified",
    "route_limits_checked_at",
    "route_limits_error",
    "route_limits_source_model",
)


def _slug(value: Any) -> str | None:
    return (
        value.strip()
        if isinstance(value, str) and value.strip() and len(value) <= 500
        else None
    )


def find_model_descriptor(descriptors: Any, model: str | None) -> dict[str, Any] | None:
    """Locate the stored descriptor for one exact model id."""

    if model is None or not isinstance(descriptors, list):
        return None
    return next(
        (
            item
            for item in descriptors
            if isinstance(item, dict) and item.get("id") == model
        ),
        None,
    )


def route_discovery_model(descriptor: Any, model: str) -> str:
    """Endpoint discovery targets the concrete model, never the alias.

    OpenRouter publishes an empty endpoint set for alias models: they carry no
    routes of their own and redirect to whichever model the catalog names as
    their target.
    """

    if not isinstance(descriptor, dict):
        return model
    return _slug(descriptor.get("alias_target")) or model


def route_limits_verified(descriptor: Any, model: str) -> bool:
    """Endpoint limits hold only while they describe the slug routing serves.

    An alias that has moved on to a newer model keeps its recorded routes, but
    they no longer prove anything, so the conservative cap applies until the
    new target is measured.
    """

    if (
        not isinstance(descriptor, dict)
        or descriptor.get("route_limits_verified") is not True
    ):
        return False
    measured = _slug(descriptor.get("route_limits_source_model")) or model
    return measured == route_discovery_model(descriptor, model)


class ModelRouteDescriptor(BaseModel):
    provider_name: str
    # OpenRouter provider slug (the endpoint tag before "/"), used for allowlists.
    provider_slug: str | None = None
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
        alias_target = item.get("alias_target")
        alias_target = alias_target if isinstance(alias_target, dict) else {}
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
                primary_route_context_window=_positive_int(top.get("context_length")),
                input_modalities=_strings(architecture.get("input_modalities")),
                output_modalities=_strings(architecture.get("output_modalities")),
                supported_parameters=_strings(item.get("supported_parameters")),
                pricing=pricing,
                expiration_date=(
                    expiration_date[:100]
                    if isinstance(expiration_date, str) and expiration_date.strip()
                    else None
                ),
                alias_target=_slug(alias_target.get("slug")),
            ),
        )
    return list(models.values())


OPENROUTER_PROVIDER_DIRECTORY_URL = "https://openrouter.ai/api/v1/providers"


class UpstreamProvider(BaseModel):
    slug: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    # ISO country codes as OpenRouter publishes them; display-only.
    headquarters: str | None = None
    datacenters: list[str] = Field(default_factory=list)


def _country(value: Any) -> str | None:
    return (
        value.strip().upper()
        if isinstance(value, str) and 2 <= len(value.strip()) <= 3
        else None
    )


def openrouter_upstream_providers(payload: Any) -> list[UpstreamProvider]:
    """Parse OpenRouter's public provider directory; skip malformed rows."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) > 2_000:
        raise ValueError("Invalid OpenRouter provider directory")
    providers: dict[str, UpstreamProvider] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        slug, name = item.get("slug"), item.get("name")
        if (
            isinstance(slug, str)
            and isinstance(name, str)
            and slug.strip()
            and name.strip()
        ):
            key = slug.strip().lower()[:200]
            raw_centers = item.get("datacenters")
            centers = [
                code
                for code in (
                    _country(value)
                    for value in (raw_centers if isinstance(raw_centers, list) else [])
                )
                if code
            ]
            providers[key] = UpstreamProvider(
                slug=key,
                name=name.strip()[:200],
                headquarters=_country(item.get("headquarters")),
                datacenters=list(dict.fromkeys(centers))[:50],
            )
    return sorted(providers.values(), key=lambda item: item.name.lower())


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
        # OpenRouter publishes max_prompt_tokens as null for most endpoints; the
        # prompt may then use the whole context window.
        max_input = _positive_int(item.get("max_prompt_tokens")) or context_window
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
        tag = item.get("tag")
        slug = tag.split("/", 1)[0].strip().lower() if isinstance(tag, str) else ""
        routes.append(
            ModelRouteDescriptor(
                provider_name=provider_name,
                provider_slug=slug or None,
                context_window=context_window,
                max_input_tokens=max_input,
                max_output_tokens=max_output,
                supported_parameters=_strings(item.get("supported_parameters")),
                status=status,
            )
        )
    return routes
