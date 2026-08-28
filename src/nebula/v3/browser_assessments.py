"""Durable, scope-bound Security Browser assessment workflows."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field, model_validator

from .browser_engine import BrowserEngineRegistry
from .browser_security import BrowserWorkflowError
from .domain import (
    BrowserAssessment,
    BrowserAssessmentBudget,
    BrowserAssessmentPhase,
    BrowserAssessmentProfile,
    BrowserAssessmentStatus,
    BrowserAssessmentStep,
    BrowserAssessmentStepStatus,
    BrowserEngineCapability,
    BrowserEngineState,
    BrowserIdentity,
    BrowserIssueCandidate,
    BrowserIssueValidationStatus,
    BrowserLoginFlow,
    BrowserRecipe,
    BrowserSession,
    BrowserValidationGrant,
    Engagement,
    Evidence,
    NebulaModel,
    RiskClass,
    ScopePolicy,
    Severity,
    utc_now,
)
from .policy import PolicyEffect, PolicyEngine, PolicyRequest
from .storage import ConflictError, NebulaStore, NotFoundError


class BrowserScanProfileDefinition(NebulaModel):
    id: BrowserAssessmentProfile
    name: str
    summary: str
    risk_classes: list[RiskClass]
    required_adapters: list[str]
    default_budget: BrowserAssessmentBudget
    validation_locked: bool = False


BUILTIN_PROFILES: tuple[BrowserScanProfileDefinition, ...] = (
    BrowserScanProfileDefinition(
        id=BrowserAssessmentProfile.EXPLORE,
        name="Explore",
        summary="Guided manual exploration and evidence capture without scanner traffic.",
        risk_classes=[RiskClass.PASSIVE],
        required_adapters=["managed-chromium"],
        default_budget=BrowserAssessmentBudget(
            max_requests=500, max_actions=250, max_duration_seconds=1_800
        ),
    ),
    BrowserScanProfileDefinition(
        id=BrowserAssessmentProfile.STANDARD,
        name="Standard",
        summary="Explore, crawl, and run passive analysis. Recommended for most tests.",
        risk_classes=[RiskClass.PASSIVE],
        required_adapters=["managed-chromium", "zap"],
        default_budget=BrowserAssessmentBudget(),
    ),
    BrowserScanProfileDefinition(
        id=BrowserAssessmentProfile.DEEP,
        name="Deep",
        summary="Add bounded active checks after crawling and passive analysis.",
        risk_classes=[RiskClass.PASSIVE, RiskClass.ACTIVE_SCAN],
        required_adapters=["managed-chromium", "zap"],
        default_budget=BrowserAssessmentBudget(
            max_requests=10_000,
            max_actions=2_000,
            max_duration_seconds=14_400,
            max_concurrency=4,
        ),
    ),
    BrowserScanProfileDefinition(
        id=BrowserAssessmentProfile.API,
        name="API",
        summary="Import and test OpenAPI, Postman, GraphQL, and observed API traffic.",
        risk_classes=[RiskClass.PASSIVE, RiskClass.ACTIVE_SCAN],
        required_adapters=["managed-chromium", "zap"],
        default_budget=BrowserAssessmentBudget(
            max_requests=8_000,
            max_actions=1_000,
            max_duration_seconds=10_800,
            max_concurrency=4,
        ),
    ),
    BrowserScanProfileDefinition(
        id=BrowserAssessmentProfile.VALIDATION,
        name="Validation",
        summary="Run one explicitly granted issue-validation technique and its controls.",
        risk_classes=[RiskClass.EXPLOITATION],
        required_adapters=["managed-chromium"],
        default_budget=BrowserAssessmentBudget(
            max_requests=25,
            max_actions=25,
            max_duration_seconds=900,
            max_concurrency=1,
        ),
        validation_locked=True,
    ),
)

PROFILE_BY_ID = {profile.id: profile for profile in BUILTIN_PROFILES}


class BrowserAssessmentWorkspace(NebulaModel):
    assessments: list[BrowserAssessment]
    steps: list[BrowserAssessmentStep]
    login_flows: list[BrowserLoginFlow]
    recipes: list[BrowserRecipe]
    candidates: list[BrowserIssueCandidate]
    validation_grants: list[BrowserValidationGrant]
    profiles: list[BrowserScanProfileDefinition]
    engines: list[BrowserEngineCapability]


class BrowserAssessmentCreateRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4_000)
    profile: BrowserAssessmentProfile = BrowserAssessmentProfile.STANDARD
    session_id: str = Field(min_length=1, max_length=200)
    identity_ids: list[str] = Field(min_length=1, max_length=32)
    primary_identity_id: str = Field(min_length=1, max_length=200)
    target_urls: list[str] = Field(min_length=1, max_length=100)
    credential_refs: list[str] = Field(default_factory=list, max_length=32)
    validation_grant_id: str | None = Field(default=None, max_length=200)
    budget: BrowserAssessmentBudget | None = None


class BrowserAssessmentTransitionRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    action: Literal[
        "start",
        "pause",
        "resume",
        "takeover",
        "return_control",
        "stop",
        "complete",
        "fail",
        "retry",
        "revoke",
    ]
    reason: str | None = Field(default=None, max_length=4_000)
    recovery_action: str | None = Field(default=None, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def assessment_failure_is_actionable(self) -> "BrowserAssessmentTransitionRequest":
        if self.action == "fail" and (not self.reason or not self.recovery_action):
            raise ValueError("failed assessments require a reason and recovery action")
        if self.action in {"pause", "takeover", "revoke"} and not self.reason:
            raise ValueError(f"{self.action} requires an operator-visible reason")
        return self


class BrowserIssueCandidateCreateRequest(NebulaModel):
    assessment_id: str = Field(min_length=1, max_length=200)
    rule_id: str = Field(min_length=1, max_length=200)
    check_family: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    cwe: str | None = Field(default=None, pattern=r"^CWE-[1-9][0-9]{0,4}$")
    target_url: str = Field(min_length=1, max_length=16_384)
    insertion_point: str | None = Field(default=None, max_length=1_000)
    severity: Severity
    confidence: Literal["tentative", "firm", "certain"] = "tentative"
    control_results: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)


class BrowserValidationGrantRequest(NebulaModel):
    expected_candidate_revision: int = Field(ge=1)
    technique: str = Field(min_length=1, max_length=1_000)
    max_requests: int = Field(ge=1, le=10_000)
    duration_seconds: int = Field(ge=30, le=3_600)
    idempotency_key: str = Field(min_length=1, max_length=300)


class BrowserValidationRevokeRequest(NebulaModel):
    expected_grant_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=300)


class BrowserValidationResultRequest(NebulaModel):
    expected_candidate_revision: int = Field(ge=1)
    expected_grant_revision: int = Field(ge=1)
    result: Literal["confirmed", "rejected", "inconclusive"]
    control_results: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=300)


_TRANSITIONS: dict[
    str, tuple[set[BrowserAssessmentStatus], BrowserAssessmentStatus]
] = {
    "start": ({BrowserAssessmentStatus.READY}, BrowserAssessmentStatus.RUNNING),
    "pause": ({BrowserAssessmentStatus.RUNNING}, BrowserAssessmentStatus.PAUSED),
    "resume": ({BrowserAssessmentStatus.PAUSED}, BrowserAssessmentStatus.RUNNING),
    "takeover": (
        {BrowserAssessmentStatus.RUNNING},
        BrowserAssessmentStatus.WAITING_OPERATOR,
    ),
    "return_control": (
        {BrowserAssessmentStatus.WAITING_OPERATOR},
        BrowserAssessmentStatus.RUNNING,
    ),
    "stop": (
        {
            BrowserAssessmentStatus.RUNNING,
            BrowserAssessmentStatus.PAUSED,
            BrowserAssessmentStatus.WAITING_OPERATOR,
        },
        BrowserAssessmentStatus.STOPPED,
    ),
    "complete": (
        {BrowserAssessmentStatus.RUNNING},
        BrowserAssessmentStatus.COMPLETE,
    ),
    "fail": (
        {
            BrowserAssessmentStatus.READY,
            BrowserAssessmentStatus.RUNNING,
            BrowserAssessmentStatus.PAUSED,
            BrowserAssessmentStatus.WAITING_OPERATOR,
        },
        BrowserAssessmentStatus.FAILED,
    ),
    "retry": (
        {BrowserAssessmentStatus.FAILED, BrowserAssessmentStatus.STOPPED},
        BrowserAssessmentStatus.READY,
    ),
    "revoke": (
        {
            BrowserAssessmentStatus.DRAFT,
            BrowserAssessmentStatus.READY,
            BrowserAssessmentStatus.RUNNING,
            BrowserAssessmentStatus.PAUSED,
            BrowserAssessmentStatus.WAITING_OPERATOR,
            BrowserAssessmentStatus.STOPPED,
            BrowserAssessmentStatus.FAILED,
        },
        BrowserAssessmentStatus.REVOKED,
    ),
}


class BrowserAssessmentService:
    def __init__(
        self,
        store: NebulaStore,
        engines: BrowserEngineRegistry | None = None,
    ) -> None:
        self.store = store
        self.engines = engines or BrowserEngineRegistry()
        self.policy = PolicyEngine()

    async def workspace(self, engagement_id: str) -> BrowserAssessmentWorkspace:
        self.store.get(Engagement, engagement_id)
        return BrowserAssessmentWorkspace(
            assessments=self.store.list_entities(
                BrowserAssessment, engagement_id=engagement_id, limit=1_000
            ),
            steps=self.store.list_entities(
                BrowserAssessmentStep, engagement_id=engagement_id, limit=1_000
            ),
            login_flows=self.store.list_entities(
                BrowserLoginFlow, engagement_id=engagement_id, limit=1_000
            ),
            recipes=self.store.list_entities(
                BrowserRecipe, engagement_id=engagement_id, limit=1_000
            ),
            candidates=self.store.list_entities(
                BrowserIssueCandidate, engagement_id=engagement_id, limit=1_000
            ),
            validation_grants=self.store.list_entities(
                BrowserValidationGrant, engagement_id=engagement_id, limit=1_000
            ),
            profiles=list(BUILTIN_PROFILES),
            engines=await self.engines.capabilities(),
        )

    async def create(
        self,
        engagement_id: str,
        request: BrowserAssessmentCreateRequest,
        actor_id: str,
    ) -> BrowserAssessment:
        scope = self._scope(engagement_id)
        session = self._owned(BrowserSession, request.session_id, engagement_id)
        for identity_id in request.identity_ids:
            identity = self._owned(BrowserIdentity, identity_id, engagement_id)
            if identity.revoked_at is not None:
                raise BrowserWorkflowError("a selected browser identity is revoked")
        if session.identity_id != request.primary_identity_id:
            raise BrowserWorkflowError(
                "the assessment primary identity must match its browser session"
            )
        profile = PROFILE_BY_ID[request.profile]
        for target in request.target_urls:
            for risk in profile.risk_classes:
                self._require_in_scope(scope, target, risk)

        if request.profile == BrowserAssessmentProfile.VALIDATION:
            self._validation_grant(
                request.validation_grant_id, engagement_id, request.target_urls
            )

        capabilities = await self.engines.capabilities()
        ready_adapters = {
            capability.adapter
            for capability in capabilities
            if capability.state == BrowserEngineState.READY
        }
        missing = [
            adapter
            for adapter in profile.required_adapters
            if adapter not in ready_adapters
        ]
        status = (
            BrowserAssessmentStatus.DRAFT if missing else BrowserAssessmentStatus.READY
        )
        assessment = BrowserAssessment(
            engagement_id=engagement_id,
            name=request.name,
            objective=request.objective,
            profile=request.profile,
            session_id=session.id,
            identity_ids=request.identity_ids,
            primary_identity_id=request.primary_identity_id,
            target_urls=request.target_urls,
            scope_policy_id=scope.id,
            scope_policy_revision=scope.revision,
            risk_classes=[risk.value for risk in profile.risk_classes],
            validation_grant_id=request.validation_grant_id,
            credential_refs=request.credential_refs,
            status=status,
            budget=request.budget or profile.default_budget.model_copy(deep=True),
            engines=capabilities,
            pause_reason=(
                f"Prepare required runtime: {', '.join(missing)}" if missing else None
            ),
            recovery_action=(
                "Use Prepare/Retry in preflight. Manual legacy browsing remains available."
                if missing
                else None
            ),
            created_by=actor_id,
        )
        created, _ = self.store.create_with_operation_event(
            assessment,
            operation_id=assessment.id,
            operation_kind="browser_assessment",
            engagement_id=engagement_id,
            event_type="browser_assessment.created",
            event_payload={
                "assessment_id": assessment.id,
                "status": assessment.status.value,
                "profile": assessment.profile.value,
            },
            actor_id=actor_id,
            idempotency_key=f"create:{assessment.id}",
        )
        for step in self._initial_steps(created):
            self.store.create_with_operation_event(
                step,
                operation_id=created.id,
                operation_kind="browser_assessment",
                engagement_id=engagement_id,
                event_type="browser_assessment.step.created",
                event_payload={
                    "assessment_id": created.id,
                    "step_id": step.id,
                    "sequence": step.sequence,
                    "title": step.title,
                },
                actor_id=actor_id,
                idempotency_key=f"create-step:{step.id}",
            )
        return created

    async def refresh_readiness(
        self, assessment_id: str, expected_revision: int, actor_id: str
    ) -> BrowserAssessment:
        assessment = self.store.get(BrowserAssessment, assessment_id)
        capabilities = await self.engines.capabilities()
        profile = PROFILE_BY_ID[assessment.profile]
        ready = {
            item.adapter
            for item in capabilities
            if item.state == BrowserEngineState.READY
        }
        missing = [item for item in profile.required_adapters if item not in ready]
        next_status = (
            BrowserAssessmentStatus.DRAFT if missing else BrowserAssessmentStatus.READY
        )
        if assessment.status not in {
            BrowserAssessmentStatus.DRAFT,
            BrowserAssessmentStatus.READY,
            BrowserAssessmentStatus.FAILED,
        }:
            next_status = assessment.status
        updated, _ = self.store.update_with_operation_event(
            BrowserAssessment,
            assessment.id,
            {
                "engines": capabilities,
                "status": next_status,
                "pause_reason": (
                    f"Prepare required runtime: {', '.join(missing)}"
                    if missing
                    else None
                ),
                "failure": None,
                "recovery_action": (
                    "Use Prepare/Retry in preflight. Manual legacy browsing remains available."
                    if missing
                    else None
                ),
            },
            expected_revision=expected_revision,
            operation_id=assessment.id,
            operation_kind="browser_assessment",
            engagement_id=assessment.engagement_id,
            event_type="browser_assessment.readiness",
            event_payload={"status": next_status.value, "missing": missing},
            actor_id=actor_id,
        )
        return updated

    async def transition(
        self,
        assessment_id: str,
        request: BrowserAssessmentTransitionRequest,
        actor_id: str,
    ) -> BrowserAssessment:
        assessment = self.store.get(BrowserAssessment, assessment_id)
        for event in self.store.replay_operation_events(
            assessment.id, after_sequence=0, limit=10_000
        ):
            if event.idempotency_key != request.idempotency_key:
                continue
            if event.event_type != f"browser_assessment.{request.action}":
                raise BrowserWorkflowError(
                    "assessment idempotency key was reused for a different action"
                )
            return assessment
        allowed, next_status = _TRANSITIONS[request.action]
        if assessment.status not in allowed:
            raise BrowserWorkflowError(
                f"cannot {request.action} an assessment in {assessment.status.value} state"
            )
        if request.action in {"start", "retry"}:
            self._require_frozen_scope(assessment)
        managed = await self.engines.adapter("managed-chromium")
        if request.action in {"start", "pause", "resume", "stop", "complete", "revoke"}:
            if managed is None:
                raise BrowserWorkflowError(
                    "Managed Chromium is unavailable; manual legacy browsing remains usable. Prepare the runtime and retry."
                )
            try:
                if request.action == "start":
                    await managed.ensure_identity(assessment.primary_identity_id)
                    await managed.resume(assessment.id)
                elif request.action == "pause":
                    await managed.pause(assessment.id)
                elif request.action == "resume":
                    await managed.resume(assessment.id)
                else:
                    await managed.stop(assessment.id)
            except Exception as exc:
                raise BrowserWorkflowError(
                    "Managed Chromium did not acknowledge the lifecycle change; saved assessment data remains usable. Retry readiness or use the desktop emergency stop."
                ) from exc
        changes: dict[str, Any] = {
            "status": next_status,
            "pause_reason": None,
            "failure": None,
            "recovery_action": None,
        }
        now = utc_now()
        if request.action == "start":
            changes.update(started_at=now, phase=BrowserAssessmentPhase.DISCOVERY)
        elif request.action == "pause":
            changes["pause_reason"] = request.reason
        elif request.action == "takeover":
            changes.update(control_owner="operator", pause_reason=request.reason)
        elif request.action == "return_control":
            changes["control_owner"] = "nebula"
        elif request.action in {"stop", "complete", "revoke"}:
            changes["completed_at"] = now
            if request.action == "complete":
                changes.update(phase=BrowserAssessmentPhase.COMPLETE, progress=1)
            if request.action == "revoke":
                changes["pause_reason"] = request.reason
        elif request.action == "fail":
            changes.update(
                failure=request.reason,
                recovery_action=request.recovery_action,
                pause_reason=request.reason,
            )
        updated, _ = self.store.update_with_operation_event(
            BrowserAssessment,
            assessment.id,
            changes,
            expected_revision=request.expected_revision,
            operation_id=assessment.id,
            operation_kind="browser_assessment",
            engagement_id=assessment.engagement_id,
            event_type=f"browser_assessment.{request.action}",
            event_payload={
                "assessment_id": assessment.id,
                "from": assessment.status.value,
                "to": next_status.value,
                "reason": request.reason,
            },
            actor_id=actor_id,
            idempotency_key=request.idempotency_key,
        )
        return updated

    def create_candidate(
        self, request: BrowserIssueCandidateCreateRequest, actor_id: str
    ) -> BrowserIssueCandidate:
        assessment = self.store.get(BrowserAssessment, request.assessment_id)
        self._require_frozen_scope(assessment)
        self._require_assessment_target(assessment, request.target_url)
        self._require_in_scope(
            self._scope(assessment.engagement_id),
            request.target_url,
            RiskClass.PASSIVE,
        )
        digest_payload = {
            "assessment_id": assessment.id,
            "rule_id": request.rule_id,
            "target_url": request.target_url,
            "insertion_point": request.insertion_point,
        }
        fingerprint = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        existing = self.store.list_entities(
            BrowserIssueCandidate,
            engagement_id=assessment.engagement_id,
            limit=1_000,
        )
        for candidate in existing:
            if candidate.deduplication_fingerprint == fingerprint:
                return candidate
        candidate = BrowserIssueCandidate(
            engagement_id=assessment.engagement_id,
            deduplication_fingerprint=fingerprint,
            **request.model_dump(),
        )
        event_payload = {
            "assessment_id": assessment.id,
            "candidate_id": candidate.id,
            "severity": candidate.severity.value,
        }
        try:
            with self.store.transaction() as transaction:
                transaction.add(candidate)
                transaction.update(
                    BrowserAssessment,
                    assessment.id,
                    {"candidate_ids": [*assessment.candidate_ids, candidate.id]},
                    expected_revision=assessment.revision,
                )
                transaction.append_operation_event(
                    assessment.id,
                    "browser_assessment",
                    assessment.engagement_id,
                    "browser_assessment.candidate.created",
                    event_payload,
                    actor_id=actor_id,
                    idempotency_key=f"candidate:{fingerprint}",
                )
        except ConflictError:
            duplicate = next(
                (
                    item
                    for item in self.store.list_entities(
                        BrowserIssueCandidate,
                        engagement_id=assessment.engagement_id,
                        limit=1_000,
                    )
                    if item.deduplication_fingerprint == fingerprint
                ),
                None,
            )
            if duplicate is not None:
                return duplicate
            raise
        return candidate

    def grant_validation(
        self,
        candidate_id: str,
        request: BrowserValidationGrantRequest,
        actor_id: str,
    ) -> BrowserValidationGrant:
        candidate = self.store.get(BrowserIssueCandidate, candidate_id)
        assessment = self.store.get(BrowserAssessment, candidate.assessment_id)
        prior = self._validation_event(
            assessment.id,
            request.idempotency_key,
            "browser_assessment.validation.granted",
        )
        if prior is not None:
            expected = {
                "candidate_id": candidate.id,
                "technique": request.technique,
                "max_requests": request.max_requests,
                "duration_seconds": request.duration_seconds,
            }
            if any(prior.payload.get(key) != value for key, value in expected.items()):
                raise BrowserWorkflowError(
                    "validation idempotency key was reused for different authority"
                )
            return self.store.get(
                BrowserValidationGrant, str(prior.payload["grant_id"])
            )
        self._require_frozen_scope(assessment)
        self._require_assessment_target(assessment, candidate.target_url)
        self._require_in_scope(
            self._scope(candidate.engagement_id),
            candidate.target_url,
            RiskClass.EXPLOITATION,
        )
        now = utc_now()
        prior_grants = [
            grant
            for grant in self.store.list_entities(
                BrowserValidationGrant,
                engagement_id=candidate.engagement_id,
                limit=1_000,
            )
            if grant.candidate_id == candidate.id and grant.status == "active"
        ]
        live_grants = [grant for grant in prior_grants if grant.expires_at > now]
        if live_grants:
            raise BrowserWorkflowError(
                "candidate already has an active validation grant; revoke or consume it before granting another"
            )
        if candidate.validation_status in {
            BrowserIssueValidationStatus.CONFIRMED,
            BrowserIssueValidationStatus.VALIDATING,
        }:
            raise BrowserWorkflowError(
                f"candidate in {candidate.validation_status.value} state cannot receive another grant"
            )
        grant = BrowserValidationGrant(
            engagement_id=candidate.engagement_id,
            assessment_id=assessment.id,
            candidate_id=candidate.id,
            target_url=candidate.target_url,
            technique=request.technique,
            max_requests=request.max_requests,
            duration_seconds=request.duration_seconds,
            granted_by=actor_id,
            granted_at=now,
            expires_at=now + timedelta(seconds=request.duration_seconds),
        )
        event_payload = {
            "assessment_id": assessment.id,
            "candidate_id": candidate.id,
            "grant_id": grant.id,
            "target_url": grant.target_url,
            "technique": grant.technique,
            "max_requests": grant.max_requests,
            "duration_seconds": grant.duration_seconds,
            "expires_at": grant.expires_at.isoformat(),
        }
        try:
            with self.store.transaction() as transaction:
                for expired in prior_grants:
                    transaction.update(
                        BrowserValidationGrant,
                        expired.id,
                        {"status": "expired"},
                        expected_revision=expired.revision,
                    )
                transaction.add(grant)
                transaction.update(
                    BrowserIssueCandidate,
                    candidate.id,
                    {
                        "validation_status": BrowserIssueValidationStatus.QUEUED,
                        "validation_grant_id": grant.id,
                    },
                    expected_revision=request.expected_candidate_revision,
                )
                transaction.append_operation_event(
                    assessment.id,
                    "browser_assessment",
                    assessment.engagement_id,
                    "browser_assessment.validation.granted",
                    event_payload,
                    actor_id=actor_id,
                    idempotency_key=request.idempotency_key,
                )
        except ConflictError:
            prior = self._validation_event(
                assessment.id,
                request.idempotency_key,
                "browser_assessment.validation.granted",
            )
            if prior is not None:
                return self.store.get(
                    BrowserValidationGrant, str(prior.payload["grant_id"])
                )
            raise
        return grant

    def revoke_validation(
        self,
        candidate_id: str,
        request: BrowserValidationRevokeRequest,
        actor_id: str,
    ) -> BrowserValidationGrant:
        candidate = self.store.get(BrowserIssueCandidate, candidate_id)
        grant = self.store.get(
            BrowserValidationGrant, candidate.validation_grant_id or ""
        )
        prior = self._validation_event(
            candidate.assessment_id,
            request.idempotency_key,
            "browser_assessment.validation.revoked",
        )
        if prior is not None:
            if (
                prior.payload.get("grant_id") != grant.id
                or prior.payload.get("reason") != request.reason
            ):
                raise BrowserWorkflowError(
                    "validation idempotency key was reused for a different revocation"
                )
            return self.store.get(BrowserValidationGrant, grant.id)
        if (
            grant.candidate_id != candidate.id
            or grant.assessment_id != candidate.assessment_id
        ):
            raise BrowserWorkflowError("validation grant ownership is inconsistent")
        if grant.status != "active":
            raise BrowserWorkflowError(f"validation grant is already {grant.status}")
        now = utc_now()
        event_payload = {
            "assessment_id": candidate.assessment_id,
            "candidate_id": candidate.id,
            "grant_id": grant.id,
            "reason": request.reason,
        }
        with self.store.transaction() as transaction:
            updated = transaction.update(
                BrowserValidationGrant,
                grant.id,
                {"status": "revoked", "revoked_at": now},
                expected_revision=request.expected_grant_revision,
            )
            if candidate.validation_status in {
                BrowserIssueValidationStatus.QUEUED,
                BrowserIssueValidationStatus.VALIDATING,
            }:
                transaction.update(
                    BrowserIssueCandidate,
                    candidate.id,
                    {"validation_status": BrowserIssueValidationStatus.INCONCLUSIVE},
                    expected_revision=candidate.revision,
                )
            transaction.append_operation_event(
                candidate.assessment_id,
                "browser_assessment",
                candidate.engagement_id,
                "browser_assessment.validation.revoked",
                event_payload,
                actor_id=actor_id,
                idempotency_key=request.idempotency_key,
            )
        return updated

    def finish_validation(
        self,
        candidate_id: str,
        request: BrowserValidationResultRequest,
        actor_id: str,
    ) -> BrowserIssueCandidate:
        candidate = self.store.get(BrowserIssueCandidate, candidate_id)
        grant = self.store.get(
            BrowserValidationGrant, candidate.validation_grant_id or ""
        )
        prior = self._validation_event(
            candidate.assessment_id,
            request.idempotency_key,
            "browser_assessment.validation.completed",
        )
        if prior is not None:
            if (
                prior.payload.get("candidate_id") != candidate.id
                or prior.payload.get("result") != request.result
            ):
                raise BrowserWorkflowError(
                    "validation idempotency key was reused for a different result"
                )
            return candidate
        assessment = self.store.get(BrowserAssessment, candidate.assessment_id)
        self._require_frozen_scope(assessment)
        self._require_assessment_target(assessment, candidate.target_url)
        if (
            grant.candidate_id != candidate.id
            or grant.assessment_id != candidate.assessment_id
            or grant.target_url != candidate.target_url
        ):
            raise BrowserWorkflowError("validation grant ownership is inconsistent")
        if grant.status != "active" or grant.expires_at <= utc_now():
            raise BrowserWorkflowError(
                "validation grant is expired or revoked; request a new bounded grant"
            )
        status = BrowserIssueValidationStatus(request.result)
        for evidence_id in request.evidence_ids:
            self._owned(Evidence, evidence_id, candidate.engagement_id)
        event_payload = {
            "assessment_id": candidate.assessment_id,
            "candidate_id": candidate.id,
            "grant_id": grant.id,
            "result": status.value,
            "evidence_ids": request.evidence_ids,
        }
        with self.store.transaction() as transaction:
            updated = transaction.update(
                BrowserIssueCandidate,
                candidate.id,
                {
                    "validation_status": status,
                    "control_results": request.control_results,
                    "evidence_ids": list(
                        dict.fromkeys([*candidate.evidence_ids, *request.evidence_ids])
                    ),
                },
                expected_revision=request.expected_candidate_revision,
            )
            transaction.update(
                BrowserValidationGrant,
                grant.id,
                {"status": "consumed"},
                expected_revision=request.expected_grant_revision,
            )
            transaction.append_operation_event(
                candidate.assessment_id,
                "browser_assessment",
                candidate.engagement_id,
                "browser_assessment.validation.completed",
                event_payload,
                actor_id=actor_id,
                idempotency_key=request.idempotency_key,
            )
        return updated

    def delete(self, assessment_id: str, expected_revision: int) -> None:
        assessment = self.store.get(BrowserAssessment, assessment_id)
        if assessment.status not in {
            BrowserAssessmentStatus.DRAFT,
            BrowserAssessmentStatus.STOPPED,
            BrowserAssessmentStatus.COMPLETE,
            BrowserAssessmentStatus.FAILED,
            BrowserAssessmentStatus.REVOKED,
        }:
            raise BrowserWorkflowError("stop the assessment before deleting it")
        self.store.delete(
            BrowserAssessment, assessment.id, expected_revision=expected_revision
        )

    def _scope(self, engagement_id: str) -> ScopePolicy:
        engagement = self.store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            raise BrowserWorkflowError("Project scope is not configured")
        scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
        if scope.engagement_id != engagement_id:
            raise BrowserWorkflowError("Project scope ownership is invalid")
        return scope

    def _require_frozen_scope(self, assessment: BrowserAssessment) -> None:
        scope = self._scope(assessment.engagement_id)
        if (
            scope.id != assessment.scope_policy_id
            or scope.revision != assessment.scope_policy_revision
        ):
            raise BrowserWorkflowError(
                "Project scope changed after preflight; review and create a new frozen assessment"
            )
        for target in assessment.target_urls:
            for risk in assessment.risk_classes:
                self._require_in_scope(scope, target, RiskClass(risk))

    def _require_in_scope(
        self, scope: ScopePolicy, target: str, risk: RiskClass
    ) -> None:
        decision = self.policy.evaluate(
            scope,
            PolicyRequest(
                tool_name="security_browser",
                risk_class=risk,
                target=target,
                action="browser_assessment",
                native_scope_authority=True,
            ),
        )
        if decision.effect == PolicyEffect.DENY:
            raise BrowserWorkflowError(decision.reason)

    @staticmethod
    def _require_assessment_target(assessment: BrowserAssessment, target: str) -> None:
        from .browser_automation import BrowserAutomationService

        if not BrowserAutomationService._target_in_lease(
            target, assessment.target_urls
        ):
            raise BrowserWorkflowError(
                "candidate target is outside the assessment's frozen target corridor"
            )

    def _validation_event(
        self, assessment_id: str, idempotency_key: str, event_type: str
    ) -> Any | None:
        for event in self.store.replay_operation_events(
            assessment_id, after_sequence=0, limit=10_000
        ):
            if event.idempotency_key != idempotency_key:
                continue
            if event.event_type != event_type:
                raise BrowserWorkflowError(
                    "validation idempotency key was reused for a different action"
                )
            return event
        return None

    def _owned(self, model: type[Any], entity_id: str, engagement_id: str) -> Any:
        entity = self.store.get(model, entity_id)
        if getattr(entity, "engagement_id", None) != engagement_id:
            raise BrowserWorkflowError("browser entity belongs to another Project")
        return entity

    def _validation_grant(
        self,
        grant_id: str | None,
        engagement_id: str,
        targets: list[str],
    ) -> BrowserValidationGrant:
        if not grant_id:
            raise BrowserWorkflowError("Validation requires an issue-specific grant")
        try:
            grant = self.store.get(BrowserValidationGrant, grant_id)
        except NotFoundError as exc:
            raise BrowserWorkflowError("Validation grant does not exist") from exc
        if grant.engagement_id != engagement_id or grant.target_url not in targets:
            raise BrowserWorkflowError("Validation grant does not cover this target")
        if grant.status != "active" or grant.expires_at <= utc_now():
            raise BrowserWorkflowError("Validation grant is expired or revoked")
        return grant

    @staticmethod
    def _initial_steps(assessment: BrowserAssessment) -> list[BrowserAssessmentStep]:
        stages: list[tuple[str, str, str]] = [
            ("Discovery", "Map the selected target with the chosen identity.", "crawl"),
            (
                "Evidence baseline",
                "Capture the initial trace and page state.",
                "capture",
            ),
        ]
        if assessment.profile != BrowserAssessmentProfile.EXPLORE:
            stages.append(
                (
                    "Passive analysis",
                    "Analyze observed traffic without mutation.",
                    "passive_scan",
                )
            )
        if assessment.profile in {
            BrowserAssessmentProfile.DEEP,
            BrowserAssessmentProfile.API,
        }:
            stages.append(
                (
                    "Active checks",
                    "Run the approved bounded active check families.",
                    "active_scan",
                )
            )
        if assessment.profile == BrowserAssessmentProfile.VALIDATION:
            stages = [
                (
                    "Issue validation",
                    "Execute the granted technique and negative controls.",
                    "validate",
                )
            ]
        return [
            BrowserAssessmentStep(
                engagement_id=assessment.engagement_id,
                assessment_id=assessment.id,
                sequence=index,
                title=title,
                intent=intent,
                capability=capability,
                target=assessment.target_urls[0],
                status=BrowserAssessmentStepStatus.QUEUED,
            )
            for index, (title, intent, capability) in enumerate(stages)
        ]
