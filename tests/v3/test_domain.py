from datetime import timedelta

import pytest
from pydantic import ValidationError

from nebula.v3.domain import (
    ENTITY_MODELS,
    Correlation,
    CorrelationMethod,
    CorrelationStatus,
    Finding,
    FindingStatus,
    MissionGrant,
    ModelCapabilities,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    utc_now,
)


def test_entity_kinds_are_unique_and_complete():
    kinds = [model.entity_kind for model in ENTITY_MODELS]
    assert len(kinds) == len(set(kinds))
    assert {
        "engagements",
        "scope_policies",
        "assets",
        "services",
        "findings",
        "evidence",
        "artifacts",
        "advisories",
        "correlations",
        "runs",
        "tasks",
        "tool_calls",
        "approvals",
        "providers",
        "source_snapshots",
    }.issubset(kinds)


def test_scope_normalizes_targets_and_validates_window():
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_cidrs=["10.0.0.12", "10.0.0.0/24"],
        allowed_domains=["Example.COM.", "*.api.example.com"],
        allowed_ports=[443, 80, 443],
        not_before=utc_now(),
        not_after=utc_now() + timedelta(hours=1),
    )
    assert scope.allowed_cidrs == ["10.0.0.0/24", "10.0.0.12/32"]
    assert scope.allowed_domains == ["*.api.example.com", "example.com"]
    assert scope.allowed_ports == [80, 443]

    with pytest.raises(ValidationError):
        ScopePolicy(engagement_id="eng-1", allowed_ports=[0])
    with pytest.raises(ValidationError):
        ScopePolicy(engagement_id="eng-1", allowed_domains=["bad domain"])


def test_mission_grants_are_typed_and_expire_after_grant():
    granted_at = utc_now()
    grant = MissionGrant(
        risk_classes=[RiskClass.PASSIVE, RiskClass.ACTIVE_SCAN],
        targets=["10.0.0.0/24"],
        granted_at=granted_at,
        expires_at=granted_at + timedelta(minutes=30),
        granted_by="operator-1",
    )
    assert grant.risk_classes == [RiskClass.PASSIVE, RiskClass.ACTIVE_SCAN]
    with pytest.raises(ValidationError):
        MissionGrant(
            risk_classes=[RiskClass.PASSIVE],
            granted_at=granted_at,
            expires_at=granted_at,
            granted_by="operator-1",
        )


def test_confirmed_findings_require_evidence_and_verifier():
    with pytest.raises(ValidationError):
        Finding(
            engagement_id="eng-1",
            title="Unbacked claim",
            status=FindingStatus.CONFIRMED,
        )
    finding = Finding(
        engagement_id="eng-1",
        title="Backed claim",
        status=FindingStatus.CONFIRMED,
        evidence_ids=["evidence-1"],
        verifier_id="reviewer-1",
        verified_at=utc_now(),
    )
    assert finding.status is FindingStatus.CONFIRMED


def test_fuzzy_correlation_requires_human_confirmation():
    with pytest.raises(ValidationError):
        Correlation(
            engagement_id="eng-1",
            advisory_id="CVE-2026-0001",
            method=CorrelationMethod.FUZZY_BANNER,
            status=CorrelationStatus.CONFIRMED,
            confidence=0.75,
            rationale="similar product banner",
        )


def test_provider_capabilities_are_explicit_not_model_name_inference():
    provider = ProviderProfile(
        name="local",
        provider_type="openai-compatible",
        endpoint="http://127.0.0.1:8000/v1",
        is_local=True,
        capabilities=ModelCapabilities(tool_calling=False, streaming=True),
    )
    assert provider.is_local is True
    assert provider.capabilities.streaming is True
    assert provider.capabilities.tool_calling is False
