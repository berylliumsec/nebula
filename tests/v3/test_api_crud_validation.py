import base64
import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from nebula.v3.api import _assert_unique_api_operations, create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    AgentRun,
    Approval,
    Asset,
    Engagement,
    Evidence,
    Finding,
    HarnessKind,
    HarnessProfile,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    return (
        TestClient(
            create_app(
                store,
                artifact_store=ArtifactStore(tmp_path / "artifacts"),
                auth_token="test-token",
            )
        ),
        store,
    )


def test_generic_create_rejects_orphans_and_cross_engagement_references(api):
    client, store = api
    orphan = client.post(
        "/api/v1/assets",
        headers=_auth(),
        json={"engagement_id": "missing", "name": "orphan"},
    )
    assert orphan.status_code == 422
    assert (
        "assets.engagement_id references missing engagements" in orphan.json()["detail"]
    )
    assert store.count(Asset) == 0

    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    first_asset = client.post(
        "/api/v1/assets",
        headers=_auth(),
        json={"engagement_id": first.id, "name": "first.example"},
    ).json()
    second_asset = client.post(
        "/api/v1/assets",
        headers=_auth(),
        json={"engagement_id": second.id, "name": "second.example"},
    ).json()

    cross_service = client.post(
        "/api/v1/services",
        headers=_auth(),
        json={
            "engagement_id": first.id,
            "asset_id": second_asset["id"],
            "port": 443,
        },
    )
    assert cross_service.status_code == 422
    assert "owned by engagement" in cross_service.json()["detail"]
    valid_service = client.post(
        "/api/v1/services",
        headers=_auth(),
        json={
            "engagement_id": first.id,
            "asset_id": first_asset["id"],
            "port": 443,
        },
    )
    assert valid_service.status_code == 201

    foreign_finding = store.create(
        Finding(
            engagement_id=second.id,
            title="Foreign finding",
        )
    )
    cross_report = client.post(
        "/api/v1/reports",
        headers=_auth(),
        json={
            "engagement_id": first.id,
            "title": "Cross-owned report",
            "finding_ids": [foreign_finding.id],
        },
    )
    assert cross_report.status_code == 422
    missing_report = client.post(
        "/api/v1/reports",
        headers=_auth(),
        json={
            "engagement_id": first.id,
            "title": "Missing reference",
            "finding_ids": ["missing-finding"],
        },
    )
    assert missing_report.status_code == 422

    # Store/import transactions remain deliberately unchanged by API validation.
    direct_orphan = store.create(Asset(engagement_id="legacy", name="imported"))
    assert store.get(Asset, direct_orphan.id) == direct_orphan


def test_project_create_atomically_attaches_a_deny_network_default_scope(api):
    client, store = api

    response = client.post(
        "/api/v1/engagements",
        headers=_auth(),
        json={"name": "Fresh project"},
    )

    assert response.status_code == 201
    project = response.json()
    assert project["scope_policy_id"] == f"scope:{project['id']}"
    scope = store.get(ScopePolicy, project["scope_policy_id"])
    assert scope.engagement_id == project["id"]
    assert scope.allowed_cidrs == []
    assert scope.allowed_domains == []
    assert scope.allowed_urls == []
    assert scope.allowed_ports == []
    assert scope.local_only is False
    assert scope.max_concurrency == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "chosen-id"),
        ("created_at", "2000-01-01T00:00:00Z"),
        ("updated_at", "2099-01-01T00:00:00Z"),
        ("revision", 77),
    ],
)
def test_generic_create_rejects_server_managed_fields(api, field, value):
    client, store = api
    payload = {"name": "Managed fields", field: value}

    response = client.post("/api/v1/engagements", headers=_auth(), json=payload)

    assert response.status_code == 422
    assert "server-managed" in response.json()["detail"]
    assert store.count(Engagement) == 0


def test_generic_harness_create_allows_only_provided_kinds(api):
    client, store = api

    unsupported = client.post(
        "/api/v1/harnesses",
        headers=_auth(),
        json={"name": "Claude", "kind": "claude_agent_sdk"},
    )

    assert unsupported.status_code == 422
    assert "harness kind 'claude_agent_sdk' is not provided" in unsupported.json()[
        "detail"
    ]
    assert store.count(HarnessProfile) == 0

    supported = client.post(
        "/api/v1/harnesses",
        headers=_auth(),
        json={
            "name": "Codex",
            "kind": HarnessKind.CODEX_APP_SERVER.value,
            "executable": "/usr/local/bin/codex",
        },
    )

    assert supported.status_code == 201
    assert supported.json()["kind"] == "codex_app_server"


def test_patch_and_replace_preserve_ownership_and_revision_precedence(api):
    client, store = api
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    asset = client.post(
        "/api/v1/assets",
        headers=_auth(),
        json={"engagement_id": first.id, "name": "asset"},
    ).json()

    moved = client.patch(
        f"/api/v1/assets/{asset['id']}",
        headers=_auth(),
        json={
            "changes": {"engagement_id": second.id},
            "expected_revision": 1,
        },
    )
    assert moved.status_code == 422
    assert "engagement ownership cannot be changed" in moved.json()["detail"]
    assert store.get(Asset, asset["id"]).revision == 1

    renamed = client.patch(
        f"/api/v1/assets/{asset['id']}",
        headers=_auth(),
        json={"changes": {"name": "renamed"}, "expected_revision": 1},
    )
    assert renamed.status_code == 200
    assert renamed.json()["revision"] == 2
    stale_invalid = client.patch(
        f"/api/v1/assets/{asset['id']}",
        headers=_auth(),
        json={
            "changes": {"engagement_id": "missing"},
            "expected_revision": 1,
        },
    )
    assert stale_invalid.status_code == 409

    replacement = dict(renamed.json())
    replacement["engagement_id"] = second.id
    cross_replace = client.put(
        f"/api/v1/assets/{asset['id']}",
        headers={**_auth(), "If-Match": "2"},
        json=replacement,
    )
    assert cross_replace.status_code == 422
    replacement["engagement_id"] = first.id
    replacement["name"] = "replacement"
    valid_replace = client.put(
        f"/api/v1/assets/{asset['id']}",
        headers={**_auth(), "If-Match": "2"},
        json=replacement,
    )
    assert valid_replace.status_code == 200
    assert valid_replace.json()["revision"] == 3
    replacement["engagement_id"] = second.id
    stale_replace = client.put(
        f"/api/v1/assets/{asset['id']}",
        headers={**_auth(), "If-Match": "2"},
        json=replacement,
    )
    assert stale_replace.status_code == 409


def test_finding_patch_updates_all_analyst_fields_and_preserves_revision_guards(api):
    client, store = api
    engagement = store.create(Engagement(name="Finding review"))
    other = store.create(Engagement(name="Other review"))
    first_asset = store.create(
        Asset(engagement_id=engagement.id, name="old.example.test")
    )
    second_asset = store.create(
        Asset(engagement_id=engagement.id, name="new.example.test")
    )
    foreign_asset = store.create(
        Asset(engagement_id=other.id, name="foreign.example.test")
    )
    evidence = store.create(
        Evidence(
            engagement_id=engagement.id,
            evidence_type="operator_upload",
            title="proof.txt",
        )
    )
    finding = store.create(
        Finding(
            engagement_id=engagement.id,
            title="Original finding",
            description="Original description",
            severity="high",
            severity_rationale="Original rationale",
            asset_ids=[first_asset.id],
            cve_ids=["CVE-2025-1111"],
            cwe_ids=["CWE-20"],
        )
    )

    updated = client.patch(
        f"/api/v1/findings/{finding.id}",
        headers=_auth(),
        json={
            "expected_revision": finding.revision,
            "changes": {
                "title": "Updated finding",
                "description": "Updated description",
                "severity": "critical",
                "severity_rationale": "Material external impact",
                "status": "accepted-risk",
                "asset_ids": [second_asset.id],
                "evidence_ids": [evidence.id],
                "cve_ids": ["cve-2026-1234"],
                "cwe_ids": ["cwe-79"],
            },
        },
    )

    assert updated.status_code == 200
    expected = {
        "title": "Updated finding",
        "description": "Updated description",
        "severity": "critical",
        "severity_rationale": "Material external impact",
        "status": "accepted-risk",
        "asset_ids": [second_asset.id],
        "evidence_ids": [evidence.id],
        "cve_ids": ["CVE-2026-1234"],
        "cwe_ids": ["CWE-79"],
        "revision": finding.revision + 1,
    }
    assert {key: updated.json()[key] for key in expected} == expected

    malformed = client.patch(
        f"/api/v1/findings/{finding.id}",
        headers=_auth(),
        json={
            "expected_revision": finding.revision + 1,
            "changes": {"cve_ids": ["not-a-cve"]},
        },
    )
    assert malformed.status_code == 422
    assert store.get(Finding, finding.id).revision == finding.revision + 1

    cross_engagement = client.patch(
        f"/api/v1/findings/{finding.id}",
        headers=_auth(),
        json={
            "expected_revision": finding.revision + 1,
            "changes": {"asset_ids": [foreign_asset.id]},
        },
    )
    assert cross_engagement.status_code == 422
    assert "owned by engagement" in cross_engagement.json()["detail"]

    stale = client.patch(
        f"/api/v1/findings/{finding.id}",
        headers=_auth(),
        json={
            "expected_revision": finding.revision,
            "changes": {"title": "Stale overwrite"},
        },
    )
    assert stale.status_code == 409
    assert store.get(Finding, finding.id).title == "Updated finding"


def test_engagement_scope_policy_must_belong_to_the_same_engagement(api):
    client, store = api
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    first_scope = client.post(
        "/api/v1/scope-policies",
        headers=_auth(),
        json={"engagement_id": first.id},
    ).json()
    second_scope = client.post(
        "/api/v1/scope-policies",
        headers=_auth(),
        json={"engagement_id": second.id},
    ).json()

    cross = client.patch(
        f"/api/v1/engagements/{first.id}",
        headers=_auth(),
        json={
            "changes": {"scope_policy_id": second_scope["id"]},
            "expected_revision": 1,
        },
    )
    assert cross.status_code == 422
    valid = client.patch(
        f"/api/v1/engagements/{first.id}",
        headers=_auth(),
        json={
            "changes": {"scope_policy_id": first_scope["id"]},
            "expected_revision": 1,
        },
    )
    assert valid.status_code == 200


def test_reports_reject_unknown_status_and_unsigned_or_forged_finalization(api):
    client, store = api
    engagement = store.create(Engagement(name="Reports"))

    unknown = client.post(
        "/api/v1/reports",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "title": "Unknown status",
            "status": "published",
        },
    )
    unsigned = client.post(
        "/api/v1/reports",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "title": "Unsigned final",
            "status": "final",
        },
    )
    operator = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Report reviewer"},
    ).json()
    forged = client.post(
        "/api/v1/reports",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "title": "Forged final",
            "status": "final",
            "signed_off_by": operator["id"],
            "signed_off_at": "2026-07-12T12:00:00Z",
        },
    )

    assert unknown.status_code == 422
    assert unsigned.status_code == 422
    assert forged.status_code == 422
    assert "dedicated signed workflow" in forged.json()["detail"]


@pytest.mark.parametrize(
    "changes, detail",
    [
        ({"provider_type": "unknown"}, "unknown provider type"),
        ({"is_local": False}, "is_local must match"),
        ({"secret_ref": "plaintext-secret"}, "env:NAME"),
        (
            {"metadata": {"default_model": "outside"}},
            "model_allowlist",
        ),
    ],
)
def test_provider_profiles_are_runtime_validated_before_persistence(
    api, changes, detail
):
    client, store = api
    payload = {
        "name": "Lab vLLM",
        "provider_type": "vllm",
        "endpoint": "http://127.0.0.1:8000/v1",
        "enabled": True,
        "is_local": True,
        "model_allowlist": ["security-model"],
        "metadata": {"default_model": "security-model"},
    }
    payload.update(changes)

    response = client.post("/api/v1/providers", headers=_auth(), json=payload)

    assert response.status_code == 422
    assert detail in str(response.json()["detail"])
    assert store.count(ProviderProfile) == 0


def test_provider_patch_validates_candidate_after_revision_check(api):
    client, store = api
    created = client.post(
        "/api/v1/providers",
        headers=_auth(),
        json={
            "name": "Lab vLLM",
            "provider_type": "vllm",
            "is_local": True,
            "model_allowlist": ["security-model"],
        },
    )
    assert created.status_code == 201
    profile = created.json()
    invalid = client.patch(
        f"/api/v1/providers/{profile['id']}",
        headers=_auth(),
        json={
            "changes": {"provider_type": "unknown"},
            "expected_revision": profile["revision"],
        },
    )
    assert invalid.status_code == 422
    valid = client.patch(
        f"/api/v1/providers/{profile['id']}",
        headers=_auth(),
        json={
            "changes": {"name": "Renamed"},
            "expected_revision": profile["revision"],
        },
    )
    assert valid.status_code == 200
    assert valid.json()["revision"] == profile["revision"] + 1
    stale_invalid = client.patch(
        f"/api/v1/providers/{profile['id']}",
        headers=_auth(),
        json={"changes": {"provider_type": "unknown"}, "expected_revision": 1},
    )
    assert stale_invalid.status_code == 409
    replacement = dict(valid.json())
    replacement["provider_type"] = "unknown"
    invalid_replace = client.put(
        f"/api/v1/providers/{profile['id']}",
        headers={**_auth(), "If-Match": str(valid.json()["revision"])},
        json=replacement,
    )
    assert invalid_replace.status_code == 422
    assert store.get(ProviderProfile, profile["id"]).provider_type == "vllm"


def test_generic_delete_honors_if_match_revision(api):
    client, store = api
    engagement = store.create(Engagement(name="Revision-safe delete"))
    asset = store.create(Asset(engagement_id=engagement.id, name="host"))
    updated = store.update(
        Asset,
        asset.id,
        {"name": "new host name"},
        expected_revision=asset.revision,
    )

    stale = client.delete(
        f"/api/v1/assets/{asset.id}",
        headers={**_auth(), "If-Match": str(asset.revision)},
    )
    current = client.delete(
        f"/api/v1/assets/{asset.id}",
        headers={**_auth(), "If-Match": str(updated.revision)},
    )

    assert stale.status_code == 409
    assert current.status_code == 204


def test_vertex_profile_requires_project_and_location_options(api):
    client, _ = api
    payload = {
        "name": "Vertex",
        "provider_type": "vertex",
        "endpoint": "https://us-central1-aiplatform.googleapis.com",
        "secret_ref": "env:GOOGLE_ACCESS_TOKEN",
        "model_allowlist": ["gemini-test"],
        "metadata": {"default_model": "gemini-test"},
    }

    missing = client.post("/api/v1/providers", headers=_auth(), json=payload)
    configured = client.post(
        "/api/v1/providers",
        headers=_auth(),
        json={
            **payload,
            "metadata": {
                "default_model": "gemini-test",
                "options": {
                    "project": "security-project",
                    "location": "us-central1",
                },
            },
        },
    )

    assert missing.status_code == 422
    assert "project and location" in missing.json()["detail"]
    assert configured.status_code == 201


def test_api_evidence_attribution_requires_a_persisted_operator(api):
    client, store = api
    engagement = store.create(Engagement(name="Attribution"))
    missing = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "note.txt",
            "evidence_type": "note",
            "title": "Analyst note",
            "content_base64": base64.b64encode(b"evidence").decode(),
            "captured_by": "missing-operator",
        },
    )
    assert missing.status_code == 422
    operator = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Jordan"},
    ).json()
    created = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "note.txt",
            "evidence_type": "note",
            "title": "Analyst note",
            "content_base64": base64.b64encode(b"evidence").decode(),
            "captured_by": operator["id"],
        },
    )
    assert created.status_code == 201


def test_approval_decision_uses_active_operator_attribution(api):
    client, store = api
    engagement = store.create(Engagement(name="Approval"))
    operator = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Jordan"},
    ).json()
    run = store.create(
        AgentRun(engagement_id=engagement.id, objective="Approval attribution")
    )
    approval = store.create(
        Approval(
            engagement_id=engagement.id,
            run_id=run.id,
            risk_class=RiskClass.ACTIVE_SCAN,
            exact_request={"tool_name": "scan", "arguments": {}},
            policy_rationale="active operation",
            requested_by="agent",
        )
    )

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["decided_by"] == operator["id"]
    assert store.replay_events(run.id)[0].actor_id == operator["id"]


def test_route_operations_are_unique_after_parameter_normalization(api):
    client, _ = api
    seen = set()
    for route in client.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        shape = re.sub(r"\{[^}]+\}", "{}", route.path)
        for method in route.methods:
            assert (method, shape) not in seen
            seen.add((method, shape))
    paths = [route.path for route in client.app.routes]
    assert paths.index("/api/v1/operator-profiles/active") < len(paths)
    assert "/api/v1/operator-profiles/{entity_id}" not in paths

    duplicate = FastAPI()

    @duplicate.get("/api/v1/items/{first}")
    async def first_item():
        return {}

    @duplicate.get("/api/v1/items/{second}")
    async def second_item():
        return {}

    with pytest.raises(RuntimeError, match="duplicate API operation"):
        _assert_unique_api_operations(duplicate)
