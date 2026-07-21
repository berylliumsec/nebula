import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    ProviderProfile,
    ScopeImportClassification,
    ScopeImportStatus,
    ScopePolicy,
)
from nebula.v3.providers import ModelResponse, ModelUsage
from nebula.v3.scope_import import (
    ScopeImportApplyRequest,
    ScopeImportError,
    ScopeImportService,
)
from nebula.v3.storage import ConflictError, NebulaStore


class StructuredProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.config = type("Config", (), {"local": True})()
        self.requests = []

    def require(self, request):
        self.requests.append(request)
        return request.model

    async def complete(self, request):
        return ModelResponse(
            provider_id=self.provider_id,
            model=request.model or "model-1",
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "target_type": "cidr",
                            "classification": "allowed",
                            "raw_value": "192.0.2.7",
                            "source_location": "line 1",
                            "source_excerpt": "In scope: 192.0.2.7",
                        },
                        {
                            "target_type": "url",
                            "classification": "allowed",
                            "raw_value": "HTTPS://App.Example.test/login",
                            "source_location": "line 2",
                            "source_excerpt": "https://app.example.test/login",
                        },
                        {
                            "target_type": "domain",
                            "classification": "excluded",
                            "raw_value": "admin.example.test",
                            "source_location": "line 3",
                            "source_excerpt": "Do not test admin.example.test",
                        },
                    ],
                    "warnings": [],
                }
            ),
            usage=ModelUsage(input_tokens=20, output_tokens=10, total_tokens=30),
            provider_request_id="scope-request-1",
        )


def test_scope_import_is_reviewed_additive_and_revision_safe(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    scope = store.create(
        ScopePolicy(
            id="scope:eng-1",
            engagement_id="eng-1",
            allowed_cidrs=["198.51.100.0/24"],
            allowed_ports=[443],
            prohibited_actions=["destructive changes"],
            max_concurrency=3,
        )
    )
    engagement = store.create(
        Engagement(id="eng-1", name="Scope import", scope_policy_id=scope.id)
    )
    profile = store.create(
        ProviderProfile(
            id="provider-1",
            name="Structured local provider",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
            model_allowlist=["model-1"],
            capabilities={"strict_structured_output": True},
        )
    )
    provider = StructuredProvider(profile.id)
    service = ScopeImportService(
        store=store,
        artifact_store=artifacts,
        provider_factory=lambda _: provider,
        operator_id=lambda: "operator-1",
    )

    created = asyncio.run(
        service.create(
            engagement_id=engagement.id,
            provider_id=profile.id,
            model="model-1",
            filename="scope.txt",
            data=(
                b"In scope: 192.0.2.7\nhttps://app.example.test/login\n"
                b"Do not test admin.example.test"
            ),
            media_type="text/plain",
            cloud_confirmed=False,
        )
    )

    assert created.status == ScopeImportStatus.READY
    assert created.usage.total_tokens == 30
    assert created.provenance.provider_request_ids == ["scope-request-1"]
    allowed = [
        candidate
        for candidate in created.candidates
        if candidate.classification == ScopeImportClassification.ALLOWED
    ]
    assert [item.normalized_value for item in allowed] == [
        "192.0.2.7/32",
        "https://app.example.test/login",
    ]
    excluded = next(
        item
        for item in created.candidates
        if item.classification == ScopeImportClassification.EXCLUDED
    )
    with pytest.raises(ScopeImportError, match="only valid allowed candidates"):
        service.apply(
            created.id,
            ScopeImportApplyRequest(
                candidate_ids=[excluded.id], expected_scope_revision=scope.revision
            ),
        )

    applied = service.apply(
        created.id,
        ScopeImportApplyRequest(
            candidate_ids=[item.id for item in allowed],
            expected_scope_revision=scope.revision,
        ),
    )
    assert applied.scope.allowed_cidrs == ["192.0.2.7/32", "198.51.100.0/24"]
    assert applied.scope.allowed_urls == ["https://app.example.test/login"]
    assert applied.scope.allowed_ports == [443]
    assert applied.scope.prohibited_actions == ["destructive changes"]
    assert applied.scope.max_concurrency == 3
    assert applied.scope_import.status == ScopeImportStatus.APPLIED
    assert applied.scope_import.applied_by == "operator-1"

    with pytest.raises(ScopeImportError, match="not ready"):
        service.apply(
            created.id,
            ScopeImportApplyRequest(
                candidate_ids=[], expected_scope_revision=scope.revision
            ),
        )


def test_scope_import_rejects_a_stale_policy_revision(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    scope = store.create(ScopePolicy(id="scope:eng-1", engagement_id="eng-1"))
    engagement = store.create(
        Engagement(id="eng-1", name="Scope import", scope_policy_id=scope.id)
    )
    profile = store.create(
        ProviderProfile(
            id="provider-1",
            name="Structured local provider",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
            model_allowlist=["model-1"],
            capabilities={"strict_structured_output": True},
        )
    )
    service = ScopeImportService(
        store=store,
        artifact_store=artifacts,
        provider_factory=lambda _: StructuredProvider(profile.id),
    )
    created = asyncio.run(
        service.create(
            engagement_id=engagement.id,
            provider_id=profile.id,
            model="model-1",
            filename="scope.txt",
            data=b"192.0.2.7",
            media_type="text/plain",
            cloud_confirmed=False,
        )
    )
    with pytest.raises(ConflictError, match="revision conflict"):
        service.apply(
            created.id,
            ScopeImportApplyRequest(candidate_ids=[], expected_scope_revision=99),
        )


def test_scope_import_api_create_list_and_apply(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="API import"))
    profile = store.create(
        ProviderProfile(
            id="provider-1",
            name="Structured local provider",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
            model_allowlist=["model-1"],
            capabilities={"strict_structured_output": True},
        )
    )
    service = ScopeImportService(
        store=store,
        artifact_store=artifacts,
        provider_factory=lambda _: StructuredProvider(profile.id),
    )
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            scope_import_service=service,
            auth_token="token",
        )
    )
    headers = {"Authorization": "Bearer token"}

    created_response = client.post(
        f"/api/v1/engagements/{engagement.id}/scope-imports",
        headers=headers,
        json={
            "engagement_id": engagement.id,
            "provider_id": profile.id,
            "model": "model-1",
            "filename": "scope.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"192.0.2.7").decode(),
            "cloud_confirmed": False,
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["status"] == "ready"
    assert (
        client.get(
            f"/api/v1/engagements/{engagement.id}/scope-imports", headers=headers
        ).json()[0]["id"]
        == created["id"]
    )
    selected = [
        item["id"]
        for item in created["candidates"]
        if item["classification"] == "allowed"
    ]
    applied = client.post(
        f"/api/v1/engagements/{engagement.id}/scope-imports/{created['id']}/apply",
        headers=headers,
        json={"candidate_ids": selected, "expected_scope_revision": 0},
    )
    assert applied.status_code == 200
    assert applied.json()["scope"]["allowed_cidrs"] == ["192.0.2.7/32"]
    assert applied.json()["scope_import"]["status"] == "applied"
