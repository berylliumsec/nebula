"""Provider discovery, health and capability checks without project controllers."""

from __future__ import annotations
import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from typing import Any
from pydantic import Field
from .context import (
    ContextCapacityError,
    estimate_model_request,
    resolve_context_limits,
)
from .diagnostics import record_caught_exception
from .domain import (
    NebulaModel,
    ProviderProfile,
    ProviderCapabilityVerification,
    ProviderVerificationStatus,
    utc_now,
)
from .model_catalog import (
    ROUTE_LIMIT_FIELDS,
    find_model_descriptor,
    route_discovery_model,
)
from .providers import (
    ModelMessage,
    ModelRequest,
    PROVIDER_CATALOG,
    ProviderError,
    ProviderFlavor,
    ProviderHealth,
    ToolChoice,
    ToolDefinition,
    provider_from_profile,
)
from .storage import NebulaStore

PROVIDER_CAPABILITY_PROBE_OUTPUT_TOKENS = 2048
PROVIDER_CAPABILITY_PROBE_TIMEOUT_SECONDS = 120


class ProviderCapabilityVerifyRequest(NebulaModel):
    model: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(ge=1)


class ProviderCapabilityVerifyResponse(NebulaModel):
    provider_id: str
    provider_revision: int
    verification: ProviderCapabilityVerification


class LocalProviderDetection(NebulaModel):
    flavor: ProviderFlavor
    display_name: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=2_048)
    models: list[str] = Field(default_factory=list, max_length=256)


async def _discover_local_provider_services(
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> list[LocalProviderDetection]:
    """Discover only fixed, known loopback services with bounded model probes."""

    flavors = (
        ProviderFlavor.OLLAMA,
        ProviderFlavor.VLLM,
        ProviderFlavor.LM_STUDIO,
    )

    async def detect(flavor: ProviderFlavor) -> LocalProviderDetection | None:
        entry = PROVIDER_CATALOG[flavor]
        if not entry.local or not entry.default_base_url:
            return None
        profile = ProviderProfile(
            id=f"detected-{flavor.value}",
            name=entry.display_name,
            provider_type=flavor.value,
            endpoint=entry.default_base_url,
            is_local=True,
        )
        try:
            health = await asyncio.wait_for(
                _provider_health(profile, provider_factory), timeout=2.0
            )
        except asyncio.TimeoutError as caught_error:
            record_caught_exception(
                "api",
                "api.api.caught_failure_056",
                "A handled api operation raised an exception.",
                caught_error,
                stage="api",
            )
            return None
        if not health.healthy:
            return None
        models = [
            model.strip()
            for model in health.models
            if isinstance(model, str) and model.strip() and len(model.strip()) <= 500
        ][:256]
        return LocalProviderDetection(
            flavor=flavor,
            display_name=entry.display_name,
            endpoint=entry.default_base_url,
            models=list(dict.fromkeys(models)),
        )

    discovered = await asyncio.gather(*(detect(flavor) for flavor in flavors))
    return [candidate for candidate in discovered if candidate is not None]


async def _provider_health(
    profile: ProviderProfile,
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> ProviderHealth:
    """Return bounded, allowlisted health without reviving disabled profiles.

    Discovered models outside the allowlist are reported as unlisted, not usable.
    """

    if not profile.enabled:
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail="provider profile is disabled",
        )
    try:
        health = await (provider_factory or provider_from_profile)(profile).health()
    except (ProviderError, ValueError) as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_057",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail=str(exc),
        )
    except Exception as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_058",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail=f"provider health check failed ({type(exc).__name__})",
        )
    models = list(dict.fromkeys(health.models))
    unlisted: list[str] = []
    if profile.model_allowlist:
        allowed = set(profile.model_allowlist)
        # Offered separately so the operator can add them; never routed to as-is.
        unlisted = [model for model in models if model not in allowed]
        models = [model for model in models if model in allowed]
    allowed_models = set(models)
    unlisted_models = set(unlisted)
    return health.model_copy(
        update={
            "models": models,
            "model_descriptors": [
                model
                for model in health.model_descriptors
                if model.id in allowed_models
            ],
            "unlisted_models": unlisted,
            "unlisted_model_descriptors": [
                model
                for model in health.model_descriptors
                if model.id in unlisted_models
            ],
        }
    )


def _safe_verification_failure(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        detail = str(exc)
        detail = re.sub(
            r"(?i)(authorization|api[_-]?key|token|secret)\s*[:=]?\s*\S+",
            r"\1 [redacted]",
            detail,
        )
        return detail[:1_000]
    return f"capability probe failed ({type(exc).__name__})"


def _capability_probe_output_tokens(
    profile: ProviderProfile, request: ModelRequest
) -> int:
    """The probe's output allowance, held to what the model can serve.

    The model's known or configured output limit binds, and a small served
    window keeps room for the probe's own prompt.
    """

    try:
        limits = resolve_context_limits(
            profile,
            model=request.model,
            requested_output_tokens=PROVIDER_CAPABILITY_PROBE_OUTPUT_TOKENS,
        )
    except ContextCapacityError:
        # diagnostic-expected: the probe itself reports a route that cannot serve it
        return PROVIDER_CAPABILITY_PROBE_OUTPUT_TOKENS
    room = limits.context_window - estimate_model_request(request)
    return max(1, min(limits.max_output_tokens, room))


async def _verify_provider_capability(
    store: NebulaStore,
    profile: ProviderProfile,
    model: str,
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> ProviderCapabilityVerifyResponse:
    """Perform and durably record a harmless exact-model required-call probe."""

    if profile.model_allowlist and model not in profile.model_allowlist:
        raise ValueError("verification model must be present in model_allowlist")
    nonce = secrets.token_urlsafe(18)
    probe_name = "nebula_capability_probe"
    probe_profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={
                    "tool_calling": True,
                    "strict_structured_output": True,
                    "parallel_tool_calls": False,
                }
            )
        }
    )
    route_limits: list[Any] | None = None
    route_limits_error: str | None = None
    # An alias carries no endpoints of its own; measure the model it redirects to.
    discovery_model = route_discovery_model(
        find_model_descriptor(profile.metadata.get("model_descriptors"), model), model
    )
    try:
        provider_runtime = (provider_factory or provider_from_profile)(probe_profile)
        probe_request = ModelRequest(
            model=model,
            instructions=(
                "Capability verification. Call the supplied function exactly once "
                "with the required nonce. Return no prose."
            ),
            messages=[
                ModelMessage(
                    role="user",
                    content="Make the required capability-verification call now.",
                )
            ],
            tools=[
                ToolDefinition(
                    name=probe_name,
                    description="Echo a harmless one-time verification nonce.",
                    input_schema={
                        "type": "object",
                        "properties": {"nonce": {"type": "string", "enum": [nonce]}},
                        "required": ["nonce"],
                        "additionalProperties": False,
                    },
                )
            ],
            tool_choice=ToolChoice.REQUIRED,
            parallel_tool_calls=False,
            temperature=0,
            # The call needs no thinking; a route that takes the control skips
            # it, and the allowance below still covers a model that thinks.
            reasoning_effort="none",
        )
        probe_request.max_output_tokens = _capability_probe_output_tokens(
            profile, probe_request
        )
        response = await asyncio.wait_for(
            provider_runtime.complete(probe_request),
            timeout=PROVIDER_CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
        # A preamble beside the one structured call is commentary, as it is in
        # a chat routing step; the call itself is what is verified.
        if not response.tool_calls and response.text.strip():
            raise ProviderError(
                "probe returned prose instead of a structured tool call"
            )
        if len(response.tool_calls) != 1:
            raise ProviderError("probe did not return exactly one structured tool call")
        call = response.tool_calls[0]
        if not call.id:
            raise ProviderError("probe tool call omitted its call ID")
        if call.name != probe_name:
            raise ProviderError("probe returned the wrong function name")
        if call.arguments != {"nonce": nonce}:
            raise ProviderError(
                "probe returned arguments that failed strict validation"
            )
        finish_reason = (response.finish_reason or "").lower()
        if finish_reason not in {"tool_calls", "tool_use", "completed", "stop"}:
            raise ProviderError("probe returned an invalid finish reason")
        verification = ProviderCapabilityVerification(
            model=model,
            status=ProviderVerificationStatus.VERIFIED,
        )
        if profile.provider_type == "openrouter":
            route_loader = getattr(provider_runtime, "openrouter_route_limits", None)
            if route_loader is None:
                route_limits_error = "endpoint discovery is unavailable"
            else:
                try:
                    route_limits = await asyncio.wait_for(
                        route_loader(discovery_model), timeout=15
                    )
                except Exception as route_exc:
                    record_caught_exception(
                        "api",
                        "api.api.openrouter_route_discovery_failed",
                        "OpenRouter endpoint limits could not be refreshed.",
                        route_exc,
                        stage="api",
                    )
                    route_limits_error = "endpoint limits could not be verified"
    except Exception as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_059",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        verification = ProviderCapabilityVerification(
            model=model,
            status=ProviderVerificationStatus.FAILED,
            failure_detail=_safe_verification_failure(exc),
        )

    verifications = dict(profile.capability_verifications)
    verifications[model] = verification
    has_verified_model = any(
        item.status == ProviderVerificationStatus.VERIFIED
        and item.contract_version == "required-tool-v1"
        for item in verifications.values()
    )
    metadata = dict(profile.metadata)
    if (
        profile.provider_type == "openrouter"
        and verification.status == ProviderVerificationStatus.VERIFIED
    ):
        descriptors = [
            dict(item)
            for item in metadata.get("model_descriptors", [])
            if isinstance(item, dict)
        ]
        descriptor = next(
            (item for item in descriptors if item.get("id") == model), None
        )
        if descriptor is None:
            descriptor = {"id": model, "name": model}
            descriptors.append(descriptor)
        checked_at = utc_now().isoformat()
        descriptor.update(
            {
                "route_limits": [
                    item.model_dump(mode="json") for item in (route_limits or [])
                ],
                "route_limits_verified": route_limits is not None,
                "route_limits_checked_at": checked_at,
                "route_limits_error": route_limits_error,
                "route_limits_source_model": discovery_model,
            }
        )
        metadata["model_descriptors"] = descriptors
        metadata["route_catalog_revision"] = hashlib.sha256(
            json.dumps(
                {
                    "model": model,
                    "measured": discovery_model,
                    "checked_at": checked_at,
                    "routes": descriptor["route_limits"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    updated = await store.update(
        ProviderProfile,
        profile.id,
        {
            "capability_verifications": verifications,
            "metadata": metadata,
            # Verification is recorded per model in capability_verifications.
            # It must never narrow model_allowlist: an empty allowlist means
            # every discovered model stays selectable (OpenRouter lists hundreds).
            "capabilities": profile.capabilities.model_copy(
                update={
                    "tool_calling": has_verified_model,
                    "strict_structured_output": has_verified_model,
                    "parallel_tool_calls": False,
                }
            ),
        },
        expected_revision=profile.revision,
    )
    return ProviderCapabilityVerifyResponse(
        provider_id=profile.id,
        provider_revision=updated.revision,
        verification=verification,
    )


async def persist_provider_model_catalog(
    store: NebulaStore, profile: ProviderProfile, health: ProviderHealth
) -> ProviderProfile:
    """Persist bounded exact-model limits without churning unchanged profiles."""

    if not health.healthy or not health.model_descriptors:
        return profile
    descriptors = [
        item.model_dump(mode="json")
        for item in sorted(health.model_descriptors, key=lambda item: item.id)
    ]
    encoded = json.dumps(
        descriptors, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    revision = hashlib.sha256(encoded).hexdigest()
    # Endpoint limits come from capability verification or context recovery,
    # never from the catalog. Dropping them re-imposes the unverified OpenRouter
    # cap (8K) on a model whose routes were already checked.
    previous = {
        item.get("id"): item
        for item in profile.metadata.get("model_descriptors", [])
        if isinstance(item, dict) and item.get("route_limits_checked_at")
    }
    for descriptor in descriptors:
        prior = previous.get(descriptor["id"])
        if prior is not None and not descriptor.get("route_limits_checked_at"):
            descriptor.update(
                {key: prior[key] for key in ROUTE_LIMIT_FIELDS if key in prior}
            )
    upstream = [item.model_dump(mode="json") for item in health.upstream_providers]
    upstream_changed = bool(upstream) and (
        profile.metadata.get("openrouter_provider_catalog") != upstream
    )
    if (
        profile.metadata.get("model_catalog_revision") == revision
        and profile.metadata.get("model_descriptors") == descriptors
        and not upstream_changed
    ):
        return profile
    return await store.update(
        ProviderProfile,
        profile.id,
        {
            "metadata": {
                **profile.metadata,
                "model_descriptors": descriptors,
                "model_catalog_revision": revision,
                "model_catalog_source": health.catalog_source,
                **({"openrouter_provider_catalog": upstream} if upstream else {}),
            }
        },
        expected_revision=profile.revision,
    )
