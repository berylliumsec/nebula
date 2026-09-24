import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    HarnessKind,
    HarnessProfile,
    ProviderPrivacy,
    ProviderProfile,
    ScopeImport,
    ScopeImportClassification,
    ScopeImportStatus,
    ScopePolicy,
)
from nebula.v3.knowledge import DocumentTooLargeError
from nebula.v3.providers import ModelResponse, ModelUsage
from nebula.v3 import scope_import as scope_import_module
from nebula.v3.scope_import import (
    MAX_CHUNK_CHARACTERS,
    MAX_SCOPE_TEXT,
    ScopeImportApplyRequest,
    ScopeImportError,
    ScopeImportService,
)
from nebula.v3.storage import ConflictError, NebulaStore
from tests.v3.row_horizon_fixture import seed_older_copies


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


class StructuredHarnessRuntime:
    def __init__(self) -> None:
        self.requests = []

    async def analyze_structured(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            id="harness-turn-1",
            response=json.dumps(
                {
                    "candidates": [
                        {
                            "target_type": "domain",
                            "classification": "allowed",
                            "raw_value": "app.example.test",
                            "source_location": "line 1",
                            "source_excerpt": "In scope: app.example.test",
                        }
                    ],
                    "warnings": [],
                }
            ),
            usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
        )


def test_scope_import_supports_codex_harness_runtime(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Codex scope import"))
    harness = store.create(
        HarnessProfile(
            name="Local Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/usr/bin/codex",
            default_model="model-1",
            privacy=ProviderPrivacy(local_only=True),
            capabilities={"models": ["model-1"]},
        )
    )
    runtime = StructuredHarnessRuntime()
    service = ScopeImportService(
        store=store,
        artifact_store=artifacts,
        harness_runtime=runtime,  # type: ignore[arg-type]
    )

    created = asyncio.run(
        service.create(
            engagement_id=engagement.id,
            backend_kind="harness",
            provider_id=None,
            harness_profile_id=harness.id,
            model="model-1",
            filename="scope.txt",
            data=b"In scope: app.example.test",
            media_type="text/plain",
            cloud_confirmed=False,
        )
    )

    assert created.status == ScopeImportStatus.READY
    assert created.usage.total_tokens == 12
    assert created.provenance.backend_kind == "harness"
    assert created.provenance.provider_profile_id == harness.id
    assert created.provenance.harness_profile_id == harness.id
    assert created.provenance.provider_request_ids == ["harness-turn-1"]
    assert created.candidates[0].normalized_value == "app.example.test"
    assert runtime.requests[0]["profile_id"] == harness.id
    assert "app.example.test" in runtime.requests[0]["files"]["scope-chunk.json"]


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
    # A Project with a long import history still lists the import just made.
    seed_older_copies(store, store.get(ScopeImport, created["id"]))
    listed = client.get(
        f"/api/v1/engagements/{engagement.id}/scope-imports", headers=headers
    ).json()
    assert len(listed) == 1_000
    assert listed[-1]["id"] == created["id"]
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


class ScriptedProvider(StructuredProvider):
    """A structured provider that returns the same scripted candidates per chunk."""

    def __init__(self, provider_id: str, candidates: list[dict]) -> None:
        super().__init__(provider_id)
        self.candidates = candidates

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.provider_id,
            model=request.model or "model-1",
            text=json.dumps({"candidates": self.candidates, "warnings": []}),
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            provider_request_id=f"scope-request-{len(self.requests)}",
        )


def _structured_profile(store):
    return store.create(
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


def _create_import(service, engagement_id, profile_id, data: bytes):
    return asyncio.run(
        service.create(
            engagement_id=engagement_id,
            provider_id=profile_id,
            model="model-1",
            filename="scope.txt",
            data=data,
            media_type="text/plain",
            cloud_confirmed=False,
        )
    )


def test_scope_import_splits_an_oversized_section_into_bounded_chunks(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Large scope"))
    profile = _structured_profile(store)
    provider = StructuredProvider(profile.id)
    service = ScopeImportService(
        store=store, artifact_store=artifacts, provider_factory=lambda _: provider
    )
    line = (
        "In scope: 192.0.2.7\nhttps://app.example.test/login\n"
        "Do not test admin.example.test\n"
    )
    data = (line * 1200).encode()
    assert 80_000 < len(data) < MAX_SCOPE_TEXT

    created = _create_import(service, engagement.id, profile.id, data)

    assert created.status == ScopeImportStatus.READY
    assert len(provider.requests) >= 3
    covered = ""
    for request in provider.requests:
        chunk = request.messages[0].content
        assert len(chunk) <= MAX_CHUNK_CHARACTERS
        for section in json.loads(chunk)["sections"]:
            assert section["location"].startswith("section 1, part ")
            covered += section["text"]
    assert covered.count("192.0.2.7") == 1200
    assert [item.normalized_value for item in created.candidates] == [
        "192.0.2.7/32",
        "admin.example.test",
        "https://app.example.test/login",
    ]


def test_scope_import_reports_document_errors_as_document_errors(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Document error"))
    profile = _structured_profile(store)
    service = ScopeImportService(
        store=store,
        artifact_store=artifacts,
        provider_factory=lambda _: StructuredProvider(profile.id),
    )

    def explode(document):
        raise DocumentTooLargeError("one document section exceeds the AI chunk limit")

    monkeypatch.setattr(scope_import_module, "_document_chunks", explode)
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            scope_import_service=service,
            auth_token="token",
        )
    )
    headers = {"Authorization": "Bearer token"}

    response = client.post(
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

    assert response.status_code == 413
    assert "exceeds the AI chunk limit" in response.json()["detail"]
    assert "provider" not in response.json()["detail"]
    listed = client.get(
        f"/api/v1/engagements/{engagement.id}/scope-imports", headers=headers
    ).json()
    assert listed[0]["status"] == "failed"
    assert "exceeds the AI chunk limit" in listed[0]["error_detail"]


def test_scope_import_flags_a_cidr_whose_host_bits_were_masked(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Host bits"))
    profile = _structured_profile(store)
    provider = ScriptedProvider(
        profile.id,
        [
            {
                "target_type": "cidr",
                "classification": "allowed",
                "raw_value": "10.0.0.5/24",
                "source_location": "line 1",
                "source_excerpt": "In scope: 10.0.0.5/24",
            },
            {
                "target_type": "cidr",
                "classification": "allowed",
                "raw_value": "10.0.1.0/24",
                "source_location": "line 2",
                "source_excerpt": "In scope: 10.0.1.0/24",
            },
        ],
    )
    service = ScopeImportService(
        store=store, artifact_store=artifacts, provider_factory=lambda _: provider
    )

    created = _create_import(
        service,
        engagement.id,
        profile.id,
        b"In scope: 10.0.0.5/24\nIn scope: 10.0.1.0/24\n",
    )

    masked, clean = created.candidates
    assert masked.raw_value == "10.0.0.5/24"
    assert masked.normalized_value == "10.0.0.0/24"
    assert masked.classification == ScopeImportClassification.AMBIGUOUS
    assert any("host bits" in warning for warning in masked.warnings)
    assert any(
        "10.0.0.5/24" in warning and "10.0.0.0/24" in warning
        for warning in created.warnings
    )
    assert clean.normalized_value == "10.0.1.0/24"
    assert clean.classification == ScopeImportClassification.ALLOWED
    assert clean.warnings == []


def test_scope_import_warns_when_an_allowed_target_covers_an_exclusion(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Overlap"))
    profile = _structured_profile(store)

    def proposal(target_type, classification, raw_value):
        return {
            "target_type": target_type,
            "classification": classification,
            "raw_value": raw_value,
            "source_location": "document",
            "source_excerpt": raw_value,
        }

    provider = ScriptedProvider(
        profile.id,
        [
            proposal("cidr", "allowed", "10.0.0.0/8"),
            proposal("cidr", "excluded", "10.1.2.0/24"),
            proposal("cidr", "allowed", "192.0.2.0/24"),
            proposal("domain", "allowed", "*.example.test"),
            proposal("domain", "excluded", "admin.example.test"),
            proposal("domain", "allowed", "example.test"),
        ],
    )
    service = ScopeImportService(
        store=store, artifact_store=artifacts, provider_factory=lambda _: provider
    )

    created = _create_import(
        service,
        engagement.id,
        profile.id,
        (
            b"In scope: 10.0.0.0/8, 192.0.2.0/24, *.example.test, example.test\n"
            b"Excluded: 10.1.2.0/24 and admin.example.test\n"
        ),
    )

    by_value = {item.normalized_value: item for item in created.candidates}
    covering_cidr = by_value["10.0.0.0/8"]
    assert covering_cidr.classification == ScopeImportClassification.ALLOWED
    assert any("10.1.2.0/24" in warning for warning in covering_cidr.warnings)
    covering_domain = by_value["*.example.test"]
    assert covering_domain.classification == ScopeImportClassification.ALLOWED
    assert any("admin.example.test" in warning for warning in covering_domain.warnings)
    assert by_value["192.0.2.0/24"].warnings == []
    assert by_value["example.test"].warnings == []
    assert by_value["10.1.2.0/24"].warnings == []
    assert any(
        "10.0.0.0/8" in warning and "10.1.2.0/24" in warning
        for warning in created.warnings
    )
    assert any(
        "*.example.test" in warning and "admin.example.test" in warning
        for warning in created.warnings
    )


def test_stale_generating_scope_import_is_failed_on_startup_and_discardable(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Interrupted"))
    stranded = store.create(
        ScopeImport(
            engagement_id=engagement.id,
            artifact_id="artifact-1",
            filename="scope.txt",
            source_type="text",
            source_sha256="a" * 64,
        )
    )
    assert stranded.status == ScopeImportStatus.GENERATING
    service = ScopeImportService(store=store, artifact_store=artifacts)
    with pytest.raises(ScopeImportError, match="discarded"):
        service.discard(stranded.id)

    headers = {"Authorization": "Bearer token"}
    with TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            scope_import_service=service,
            auth_token="token",
        )
    ) as client:
        listed = client.get(
            f"/api/v1/engagements/{engagement.id}/scope-imports", headers=headers
        ).json()
        assert listed[0]["status"] == "failed"
        assert "restarted" in listed[0]["error_detail"]
        discarded = client.post(
            f"/api/v1/engagements/{engagement.id}/scope-imports/{stranded.id}/discard",
            headers=headers,
        )
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"
