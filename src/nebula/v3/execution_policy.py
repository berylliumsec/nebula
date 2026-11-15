"""Resolve the operator's project-wide tool permission policy."""

from .domain import (
    AutomationApprovalPolicy,
    AutomationProjectPolicy,
    Engagement,
    ScopePolicy,
)


async def effective_scope(
    store, engagement_id: str, scope: ScopePolicy | None = None
) -> ScopePolicy:
    """Apply never as a transient bypass without rewriting the saved scope."""
    if scope is None:
        project = await store.get(Engagement, engagement_id)
        scope = (
            await store.get(ScopePolicy, project.scope_policy_id)
            if project.scope_policy_id
            else ScopePolicy(id=f"scope:{engagement_id}", engagement_id=engagement_id)
        )
    if scope.engagement_id != engagement_id:
        raise ValueError("Project scope ownership is invalid")
    policies = await store.list_entities(
        AutomationProjectPolicy, engagement_id=engagement_id, limit=1
    )
    if policies and policies[0].approval_policy == AutomationApprovalPolicy.NEVER:
        return scope.model_copy(update={"bypass_permissions": True})
    return scope
