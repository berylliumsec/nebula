"""Shared fail-closed engagement/provider privacy checks."""

from __future__ import annotations

from .diagnostics import record_caught_exception

from .domain import Engagement, ScopePolicy
from .providers import ModelProvider
from .storage import NebulaStore, NotFoundError


class ProviderPrivacyViolation(ValueError):
    """A provider would cross the engagement's declared data boundary."""


def validate_engagement_provider_privacy(
    store: NebulaStore,
    engagement: Engagement,
    provider: ModelProvider,
) -> None:
    """Validate scope-policy ownership and its local-only boundary."""

    if not engagement.scope_policy_id:
        return
    try:
        policy = store.get(ScopePolicy, engagement.scope_policy_id)
    except NotFoundError as exc:
        record_caught_exception(
            "providers",
            "providers.privacy.caught_failure_001",
            "A handled providers operation raised an exception.",
            exc,
            stage="privacy",
        )
        raise ProviderPrivacyViolation(
            "engagement references a missing scope policy"
        ) from exc
    if policy.engagement_id != engagement.id:
        raise ProviderPrivacyViolation(
            "engagement scope policy belongs to a different engagement"
        )
    if policy.local_only and not provider.config.local:
        raise ProviderPrivacyViolation(
            "engagement scope is local-only and cannot use a cloud provider"
        )


__all__ = ["ProviderPrivacyViolation", "validate_engagement_provider_privacy"]
