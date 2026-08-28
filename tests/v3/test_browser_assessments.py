import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.browser_assessments import (
    BrowserAssessmentCreateRequest,
    BrowserAssessmentService,
    BrowserAssessmentTransitionRequest,
    BrowserIssueCandidateCreateRequest,
    BrowserValidationGrantRequest,
    BrowserValidationRevokeRequest,
)
from nebula.v3.browser_engine import (
    BrowserEngineAction,
    BrowserEngineAdapter,
    BrowserEngineReceipt,
    BrowserEngineRegistry,
)
from nebula.v3.browser_security import BrowserSecurityService, BrowserWorkflowError
from nebula.v3.domain import (
    BrowserAssessment,
    BrowserAssessmentProfile,
    BrowserAssessmentStatus,
    BrowserEngineCapability,
    BrowserEngineState,
    BrowserIssueCandidate,
    BrowserIssueValidationStatus,
    BrowserRecipe,
    Engagement,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


AUTH = {"Authorization": "Bearer test-token"}


def project(client: TestClient, store: NebulaStore) -> tuple[Engagement, ScopePolicy]:
    response = client.post(
        "/api/v1/engagements", headers=AUTH, json={"name": "Browser lab"}
    )
    assert response.status_code == 201, response.text
    engagement = store.get(Engagement, response.json()["id"])
    scope = store.get(ScopePolicy, engagement.scope_policy_id)
    scope = store.update(
        ScopePolicy,
        scope.id,
        {
            "allowed_domains": ["app.example.test"],
            "allowed_ports": [443],
        },
        expected_revision=scope.revision,
    )
    return engagement, scope


class ReadyManagedChromium(BrowserEngineAdapter):
    async def readiness(self) -> BrowserEngineCapability:
        return BrowserEngineCapability(
            adapter="managed-chromium",
            display_name="Managed Chromium",
            state=BrowserEngineState.READY,
            installed_version="canary",
            digest=f"sha256:{'a' * 64}",
            actions=["navigate", "click", "fill", "snapshot", "trace"],
            protocols=["http", "https", "websocket"],
        )

    async def ensure_identity(self, identity_id: str) -> None:
        assert identity_id

    async def execute(self, action: BrowserEngineAction) -> BrowserEngineReceipt:
        return BrowserEngineReceipt(action_token=action.action_token, state="complete")

    async def pause(self, assessment_id: str) -> None:
        assert assessment_id

    async def resume(self, assessment_id: str) -> None:
        assert assessment_id

    async def stop(self, assessment_id: str) -> None:
        assert assessment_id


def test_assessment_snapshot_is_durable_and_replayable(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    engagement, scope = project(client, store)
    browser = client.get(
        f"/api/v1/engagements/{engagement.id}/browser-workspace", headers=AUTH
    ).json()

    response = client.post(
        f"/api/v1/engagements/{engagement.id}/browser-assessments",
        headers=AUTH,
        json={
            "name": "Guided standard",
            "objective": "Map the authenticated surface and preserve evidence.",
            "profile": "standard",
            "session_id": browser["sessions"][0]["id"],
            "identity_ids": [browser["identities"][0]["id"]],
            "primary_identity_id": browser["identities"][0]["id"],
            "target_urls": ["https://app.example.test/"],
        },
    )
    assert response.status_code == 201, response.text
    assessment = response.json()
    assert assessment["status"] == "draft"
    assert assessment["scope_policy_id"] == scope.id
    assert assessment["scope_policy_revision"] == scope.revision
    assert (
        assessment["pause_reason"] == "Prepare required runtime: managed-chromium, zap"
    )
    assert all(item["state"] != "ready" for item in assessment["engines"])

    snapshot = client.get(
        f"/api/v1/engagements/{engagement.id}/browser-assessments", headers=AUTH
    )
    assert snapshot.status_code == 200, snapshot.text
    payload = snapshot.json()
    assert [item["id"] for item in payload["assessments"]] == [assessment["id"]]
    assert [item["sequence"] for item in payload["steps"]] == [0, 1, 2]
    assert [item["id"] for item in payload["profiles"]] == [
        "explore",
        "standard",
        "deep",
        "api",
        "validation",
    ]

    events = client.get(
        f"/api/v1/browser-assessments/{assessment['id']}/events?after=0",
        headers=AUTH,
    )
    assert events.status_code == 200, events.text
    event_types = [item["event_type"] for item in events.json()["events"]]
    assert event_types == [
        "browser_assessment.created",
        "browser_assessment.step.created",
        "browser_assessment.step.created",
        "browser_assessment.step.created",
    ]


def test_ready_assessment_lifecycle_is_idempotent_and_scope_frozen(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    engagement, scope = project(client, store)
    browser = BrowserSecurityService(store).workspace(engagement.id)
    service = BrowserAssessmentService(
        store, BrowserEngineRegistry([ReadyManagedChromium()])
    )
    request = BrowserAssessmentCreateRequest(
        name="Explore",
        objective="Explore the authorized target.",
        profile=BrowserAssessmentProfile.EXPLORE,
        session_id=browser.sessions[0].id,
        identity_ids=[browser.identities[0].id],
        primary_identity_id=browser.identities[0].id,
        target_urls=["https://app.example.test/"],
    )
    assessment = asyncio.run(service.create(engagement.id, request, "operator"))
    assert assessment.status == BrowserAssessmentStatus.READY

    start = BrowserAssessmentTransitionRequest(
        expected_revision=assessment.revision,
        action="start",
        idempotency_key="start-once",
    )
    running = asyncio.run(service.transition(assessment.id, start, "operator"))
    assert running.status == BrowserAssessmentStatus.RUNNING
    duplicate = asyncio.run(service.transition(assessment.id, start, "operator"))
    assert duplicate.id == running.id
    assert duplicate.revision == running.revision
    assert (
        len(
            [
                event
                for event in store.replay_operation_events(assessment.id, limit=100)
                if event.event_type == "browser_assessment.start"
            ]
        )
        == 1
    )

    second = asyncio.run(service.create(engagement.id, request, "operator"))
    store.update(
        ScopePolicy,
        scope.id,
        {"allowed_domains": []},
        expected_revision=scope.revision,
    )
    with pytest.raises(BrowserWorkflowError, match="scope changed"):
        asyncio.run(
            service.transition(
                second.id,
                BrowserAssessmentTransitionRequest(
                    expected_revision=second.revision,
                    action="start",
                    idempotency_key="stale-scope-start",
                ),
                "operator",
            )
        )
    assert (
        store.get(BrowserAssessment, second.id).status == BrowserAssessmentStatus.READY
    )


def test_recipe_contract_rejects_arbitrary_script_nodes():
    with pytest.raises(ValidationError):
        BrowserRecipe.model_validate(
            {
                "engagement_id": "project-1",
                "name": "Unsafe recipe",
                "stages": [
                    {
                        "id": "script",
                        "kind": "javascript",
                        "configuration": {"source": "fetch('https://outside.test')"},
                    }
                ],
            }
        )


def test_validation_authority_is_frozen_idempotent_and_revocable(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    engagement, _ = project(client, store)
    browser = BrowserSecurityService(store).workspace(engagement.id)
    service = BrowserAssessmentService(
        store, BrowserEngineRegistry([ReadyManagedChromium()])
    )
    assessment = asyncio.run(
        service.create(
            engagement.id,
            BrowserAssessmentCreateRequest(
                name="Portal validation source",
                objective="Collect candidates inside the frozen portal corridor.",
                profile=BrowserAssessmentProfile.EXPLORE,
                session_id=browser.sessions[0].id,
                identity_ids=[browser.identities[0].id],
                primary_identity_id=browser.identities[0].id,
                target_urls=["https://app.example.test/portal"],
            ),
            "operator",
        )
    )

    with pytest.raises(BrowserWorkflowError, match="frozen target corridor"):
        service.create_candidate(
            BrowserIssueCandidateCreateRequest(
                assessment_id=assessment.id,
                rule_id="outside-corridor",
                check_family="routing",
                title="Outside corridor",
                target_url="https://app.example.test/admin",
                severity="low",
            ),
            "engine",
        )

    candidate = service.create_candidate(
        BrowserIssueCandidateCreateRequest(
            assessment_id=assessment.id,
            rule_id="reflected-input",
            check_family="xss",
            title="Reflected input",
            cwe="CWE-79",
            target_url="https://app.example.test/portal/search?q=marker",
            insertion_point="query:q",
            severity="medium",
            confidence="firm",
        ),
        "engine",
    )
    request = BrowserValidationGrantRequest(
        expected_candidate_revision=candidate.revision,
        technique="Replay one inert marker and one negative encoding control.",
        max_requests=8,
        duration_seconds=300,
        idempotency_key="grant-candidate-once",
    )
    grant = service.grant_validation(candidate.id, request, "operator")
    duplicate = service.grant_validation(candidate.id, request, "operator")
    assert duplicate.id == grant.id
    assert (
        len(
            [
                event
                for event in store.replay_operation_events(assessment.id, limit=100)
                if event.event_type == "browser_assessment.validation.granted"
            ]
        )
        == 1
    )

    queued = store.get(BrowserIssueCandidate, candidate.id)
    assert queued.validation_status == BrowserIssueValidationStatus.QUEUED
    with pytest.raises(BrowserWorkflowError, match="already has an active"):
        service.grant_validation(
            candidate.id,
            BrowserValidationGrantRequest(
                expected_candidate_revision=queued.revision,
                technique="A second technique must not overlap.",
                max_requests=2,
                duration_seconds=60,
                idempotency_key="overlapping-grant",
            ),
            "operator",
        )

    revoke = BrowserValidationRevokeRequest(
        expected_grant_revision=grant.revision,
        reason="Operator emergency revocation.",
        idempotency_key="revoke-grant-once",
    )
    revoked = service.revoke_validation(candidate.id, revoke, "operator")
    assert revoked.status == "revoked"
    assert revoked.revoked_at is not None
    assert (
        store.get(BrowserIssueCandidate, candidate.id).validation_status
        == BrowserIssueValidationStatus.INCONCLUSIVE
    )
    assert service.revoke_validation(candidate.id, revoke, "operator").id == grant.id
