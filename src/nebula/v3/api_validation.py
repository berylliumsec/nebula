"""API-only entity ownership, reference, and provider-profile validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .domain import (
    ENTITY_MODELS,
    Advisory,
    AgentAttempt,
    AgentRun,
    Approval,
    Artifact,
    Asset,
    ChatMessage,
    ChatSession,
    Correlation,
    Engagement,
    Entity,
    Evidence,
    Finding,
    Identity,
    KnowledgeSource,
    Observation,
    OperatorProfile,
    ProviderProfile,
    Remediation,
    Report,
    ReportStatus,
    ScopePolicy,
    Service,
    SoftwareComponent,
    SourceSnapshot,
    Task,
    ToolCall,
    entity_engagement_id,
)
from .providers import provider_from_profile
from .storage import ConflictError, NebulaStore, NotFoundError


class ApiEntityValidationError(ValueError):
    """A REST mutation would persist an orphan or invalid ownership edge."""


@dataclass(frozen=True)
class ReferenceRule:
    field: str
    target: type[Entity]
    many: bool = False
    same_engagement: bool = True


_REFERENCE_RULES: dict[type[Entity], tuple[ReferenceRule, ...]] = {
    Engagement: (
        ReferenceRule("scope_policy_id", ScopePolicy),
        ReferenceRule("owner_id", OperatorProfile, same_engagement=False),
    ),
    Service: (ReferenceRule("asset_id", Asset),),
    Identity: (ReferenceRule("asset_ids", Asset, many=True),),
    SoftwareComponent: (
        ReferenceRule("asset_id", Asset),
        ReferenceRule("service_id", Service),
        ReferenceRule("source_evidence_ids", Evidence, many=True),
    ),
    Observation: (
        ReferenceRule("asset_ids", Asset, many=True),
        ReferenceRule("service_ids", Service, many=True),
        ReferenceRule("evidence_ids", Evidence, many=True),
    ),
    Evidence: (
        ReferenceRule("artifact_id", Artifact),
        ReferenceRule("finding_id", Finding),
        ReferenceRule("asset_ids", Asset, many=True),
        ReferenceRule("tool_call_id", ToolCall),
        ReferenceRule("captured_by", OperatorProfile, same_engagement=False),
    ),
    Artifact: (ReferenceRule("parent_artifact_id", Artifact),),
    Remediation: (ReferenceRule("finding_id", Finding),),
    Finding: (
        ReferenceRule("asset_ids", Asset, many=True),
        ReferenceRule("service_ids", Service, many=True),
        ReferenceRule("evidence_ids", Evidence, many=True),
        ReferenceRule("observation_ids", Observation, many=True),
        ReferenceRule("correlation_ids", Correlation, many=True),
        ReferenceRule("remediation_id", Remediation),
        ReferenceRule("verifier_id", OperatorProfile, same_engagement=False),
    ),
    Advisory: (
        ReferenceRule("source_snapshot_id", SourceSnapshot, same_engagement=False),
    ),
    SourceSnapshot: (ReferenceRule("artifact_id", Artifact, same_engagement=False),),
    Correlation: (
        ReferenceRule("component_id", SoftwareComponent),
        ReferenceRule("service_id", Service),
        ReferenceRule("supporting_evidence_ids", Evidence, many=True),
        ReferenceRule("conflicting_evidence_ids", Evidence, many=True),
        ReferenceRule("analyst_id", OperatorProfile, same_engagement=False),
    ),
    AgentRun: (
        ReferenceRule("supervisor_provider_id", ProviderProfile, same_engagement=False),
    ),
    ChatSession: (
        ReferenceRule("provider_profile_id", ProviderProfile, same_engagement=False),
    ),
    ChatMessage: (
        ReferenceRule("session_id", ChatSession),
        ReferenceRule("provider_profile_id", ProviderProfile, same_engagement=False),
    ),
    Task: (
        ReferenceRule("run_id", AgentRun),
        ReferenceRule("parent_task_id", Task),
        ReferenceRule("depends_on", Task, many=True),
    ),
    AgentAttempt: (
        ReferenceRule("run_id", AgentRun),
        ReferenceRule("task_id", Task),
        ReferenceRule("provider_profile_id", ProviderProfile, same_engagement=False),
    ),
    ToolCall: (
        ReferenceRule("run_id", AgentRun),
        ReferenceRule("task_id", Task),
        ReferenceRule("approval_id", Approval),
    ),
    Approval: (
        ReferenceRule("run_id", AgentRun),
        ReferenceRule("task_id", Task),
        ReferenceRule("tool_call_id", ToolCall),
    ),
    KnowledgeSource: (ReferenceRule("artifact_id", Artifact),),
    Report: (
        ReferenceRule("finding_ids", Finding, many=True),
        ReferenceRule("artifact_ids", Artifact, many=True),
    ),
}


class ApiEntityValidator:
    """Validate only generic REST writes; direct store/import paths stay unchanged."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def validate_create(self, entity: Entity) -> None:
        self._validate(entity)

    def validate_update(self, current: Entity, candidate: Entity) -> None:
        if type(current) is not type(candidate):
            raise ApiEntityValidationError("entity type cannot be changed")
        current_owner = entity_engagement_id(current)
        candidate_owner = entity_engagement_id(candidate)
        if current_owner != candidate_owner:
            raise ApiEntityValidationError(
                f"{current.entity_kind} engagement ownership cannot be changed"
            )
        self._validate(candidate)

    def validate_delete(self, target: Entity) -> None:
        """Reject deletion when any durable entity still references the target."""

        if isinstance(target, Report) and target.status == ReportStatus.FINAL:
            raise ConflictError("a signed final report cannot be deleted")
        for model in ENTITY_MODELS:
            offset = 0
            while True:
                page = self.store.list_entities(model, offset=offset, limit=1_000)
                for candidate in page:
                    if type(candidate) is type(target) and candidate.id == target.id:
                        continue
                    if (
                        isinstance(target, Engagement)
                        and entity_engagement_id(candidate) == target.id
                    ):
                        self._delete_conflict(target, candidate, "engagement_id")
                    if (
                        isinstance(target, Advisory)
                        and isinstance(candidate, Correlation)
                        and candidate.advisory_id == target.advisory_id
                    ):
                        self._delete_conflict(target, candidate, "advisory_id")
                    for rule in _REFERENCE_RULES.get(type(candidate), ()):
                        if rule.target is not type(target):
                            continue
                        raw = getattr(candidate, rule.field)
                        values = raw if rule.many else [raw]
                        if target.id in values:
                            self._delete_conflict(target, candidate, rule.field)
                if len(page) < 1_000:
                    break
                offset += len(page)

    def _validate(self, entity: Entity) -> None:
        owner_id = entity_engagement_id(entity)
        if owner_id is not None and not isinstance(entity, Engagement):
            self._referenced_entity(
                entity,
                field="engagement_id",
                target_model=Engagement,
                target_id=owner_id,
                expected_engagement=owner_id,
            )
        for rule in _REFERENCE_RULES.get(type(entity), ()):
            raw = getattr(entity, rule.field)
            values = raw if rule.many else [raw]
            for target_id in dict.fromkeys(value for value in values if value):
                self._referenced_entity(
                    entity,
                    field=rule.field,
                    target_model=rule.target,
                    target_id=target_id,
                    expected_engagement=owner_id if rule.same_engagement else None,
                )
        if isinstance(entity, ProviderProfile):
            self._validate_provider(entity)
        if isinstance(entity, Report) and (
            entity.status == ReportStatus.FINAL
            or entity.signed_off_by is not None
            or entity.signed_off_at is not None
        ):
            raise ApiEntityValidationError(
                "report finalization requires a dedicated signed workflow"
            )

    def _referenced_entity(
        self,
        entity: Entity,
        *,
        field: str,
        target_model: type[Entity],
        target_id: str,
        expected_engagement: str | None,
    ) -> Entity:
        try:
            target = self.store.get(target_model, target_id)
        except NotFoundError as exc:
            raise ApiEntityValidationError(
                f"{entity.entity_kind}.{field} references missing "
                f"{target_model.entity_kind} entity: {target_id}"
            ) from exc
        if expected_engagement is not None:
            actual_engagement = entity_engagement_id(target)
            if actual_engagement != expected_engagement:
                raise ApiEntityValidationError(
                    f"{entity.entity_kind}.{field} references "
                    f"{target_model.entity_kind} owned by engagement "
                    f"{actual_engagement!r}; expected {expected_engagement!r}"
                )
        return target

    @staticmethod
    def _delete_conflict(target: Entity, candidate: Entity, field: str) -> None:
        raise ConflictError(
            f"{target.entity_kind} entity {target.id!r} is still referenced by "
            f"{candidate.entity_kind}.{field}; remove the reference first"
        )

    @staticmethod
    def _validate_provider(profile: ProviderProfile) -> None:
        if not profile.name.strip():
            raise ApiEntityValidationError("provider name cannot be blank")
        if profile.provider_type != profile.provider_type.strip():
            raise ApiEntityValidationError(
                "provider_type cannot contain surrounding whitespace"
            )
        if profile.endpoint is not None and not profile.endpoint.strip():
            raise ApiEntityValidationError("provider endpoint cannot be blank")
        if profile.secret_ref and not re.fullmatch(
            r"env:[A-Za-z_][A-Za-z0-9_]*", profile.secret_ref
        ):
            raise ApiEntityValidationError(
                "provider secret_ref must use an env:NAME reference"
            )
        if any(
            model != model.strip() or not model for model in profile.model_allowlist
        ):
            raise ApiEntityValidationError(
                "provider model_allowlist entries must be non-blank and trimmed"
            )
        if len(profile.model_allowlist) != len(set(profile.model_allowlist)):
            raise ApiEntityValidationError(
                "provider model_allowlist entries must be unique"
            )
        default_model: Any = profile.metadata.get("default_model")
        if default_model is not None:
            if not isinstance(default_model, str) or not default_model.strip():
                raise ApiEntityValidationError(
                    "provider metadata.default_model must be a non-blank string"
                )
            if profile.model_allowlist and default_model not in profile.model_allowlist:
                raise ApiEntityValidationError(
                    "provider default_model is outside model_allowlist"
                )
        if profile.provider_type == "vertex":
            options = profile.metadata.get("options")
            if not isinstance(options, dict) or any(
                not isinstance(options.get(field), str) or not options[field].strip()
                for field in ("project", "location")
            ):
                raise ApiEntityValidationError(
                    "Vertex provider profiles require project and location options"
                )
        try:
            runtime = provider_from_profile(profile)
        except ValueError as exc:
            raise ApiEntityValidationError(str(exc)) from exc
        if profile.is_local != runtime.config.local:
            raise ApiEntityValidationError(
                "provider is_local must match the provider catalog locality"
            )


__all__ = ["ApiEntityValidationError", "ApiEntityValidator"]
