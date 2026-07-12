import base64

import pytest
from fastapi.testclient import TestClient

import nebula.v3.evidence as evidence_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Asset, Engagement, Evidence, Finding
from nebula.v3.evidence import (
    EvidenceTooLargeError,
    EvidenceUploadRequest,
    upload_evidence,
)
from nebula.v3.operators import OperatorProfileService
from nebula.v3.storage import NebulaStore, StoreTransaction


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def evidence_api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="Evidence upload"))
    asset = store.create(
        Asset(id="asset-a", engagement_id=engagement.id, name="Gateway")
    )
    finding = store.create(
        Finding(id="finding-a", engagement_id=engagement.id, title="TLS issue")
    )
    client = TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    )
    return client, store, artifacts, engagement, asset, finding


def test_upload_persists_immutable_artifact_and_listable_evidence(evidence_api):
    client, store, artifacts, engagement, asset, finding = evidence_api
    operator = OperatorProfileService(store).create_profile(
        display_name="Evidence operator"
    )
    content = b"immutable analyst evidence\n"
    payload = {
        "engagement_id": engagement.id,
        "filename": "../../gateway-proof.txt",
        "title": "Gateway TLS proof",
        "evidence_type": "Command Output",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "media_type": "text/plain; charset=utf-8",
        "description": "Captured from the approved verification command.",
        "source": "human-terminal",
        "finding_id": finding.id,
        "asset_ids": [asset.id, asset.id],
        "captured_by": operator.id,
        "source_version": "openssl 3.0",
        "metadata": {"command": "openssl s_client", "size": 999999},
    }

    assert client.post("/api/v1/evidence/upload", json=payload).status_code == 401
    response = client.post("/api/v1/evidence/upload", headers=_auth(), json=payload)

    assert response.status_code == 201
    evidence = response.json()
    assert evidence["engagement_id"] == engagement.id
    assert evidence["evidence_type"] == "command-output"
    assert evidence["title"] == "Gateway TLS proof"
    assert evidence["finding_id"] == finding.id
    assert evidence["asset_ids"] == [asset.id]
    assert evidence["sha256"]
    assert evidence["metadata"] == {
        "command": "openssl s_client",
        "size": len(content),
        "artifact_id": evidence["artifact_id"],
        "filename": "gateway-proof.txt",
        "media_type": "text/plain",
        "source": "human-terminal",
        "upload_version": "nebula.evidence-upload.v1",
    }
    persisted = store.get(Evidence, evidence["id"])
    artifact = store.get(Artifact, evidence["artifact_id"])
    assert persisted.artifact_id == artifact.id
    assert persisted.sha256 == artifact.sha256
    assert artifacts.read(artifact) == content
    assert artifacts.verify(artifact) is True
    assert artifact.filename == "gateway-proof.txt"
    assert artifact.source == "human-terminal"
    assert artifact.metadata["evidence_id"] == evidence["id"]
    assert artifact.metadata["evidence_metadata"] == {
        "command": "openssl s_client",
        "size": 999999,
    }
    linked_finding = store.get(Finding, finding.id)
    assert linked_finding.evidence_ids == [evidence["id"]]
    assert linked_finding.revision == finding.revision + 1

    listed = client.get(
        f"/api/v1/evidence?engagement_id={engagement.id}", headers=_auth()
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [evidence["id"]]
    artifacts_list = client.get(
        f"/api/v1/artifacts?engagement_id={engagement.id}", headers=_auth()
    )
    assert artifacts_list.status_code == 200
    assert [item["id"] for item in artifacts_list.json()] == [artifact.id]
    downloaded = client.get(f"/api/v1/artifacts/{artifact.id}/content", headers=_auth())
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["content-type"].startswith("text/plain")
    assert "gateway-proof.txt" in downloaded.headers["content-disposition"]
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["content-security-policy"].startswith("sandbox")
    assert downloaded.headers["cache-control"] == "private, no-store"

    # Evidence and Artifact entities are immutable through the public CRUD API.
    assert (
        client.post("/api/v1/evidence", headers=_auth(), json=evidence).status_code
        == 405
    )
    assert (
        client.patch(
            f"/api/v1/evidence/{evidence['id']}",
            headers=_auth(),
            json={"changes": {"title": "rewritten"}},
        ).status_code
        == 405
    )
    assert (
        client.delete(f"/api/v1/evidence/{evidence['id']}", headers=_auth()).status_code
        == 405
    )


def test_upload_rejects_invalid_content_before_writing_blobs(evidence_api):
    client, store, artifacts, engagement, _, _ = evidence_api
    base_payload = {
        "engagement_id": engagement.id,
        "filename": "proof.bin",
        "title": "Proof",
        "evidence_type": "binary",
    }

    invalid = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={**base_payload, "content_base64": "not base64!"},
    )
    empty = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={**base_payload, "content_base64": base64.b64encode(b"").decode()},
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "content_base64 must be valid base64"
    assert empty.status_code == 422
    assert empty.json()["detail"] == "evidence content cannot be empty"
    assert store.count(Artifact) == 0
    assert store.count(Evidence) == 0
    assert list(artifacts.iter_digests()) == []


def test_upload_requires_existing_same_engagement_references(evidence_api):
    client, store, artifacts, engagement, _, _ = evidence_api
    other = store.create(Engagement(id="eng-b", name="Other"))
    other_asset = store.create(
        Asset(id="asset-b", engagement_id=other.id, name="Other host")
    )
    other_finding = store.create(
        Finding(id="finding-b", engagement_id=other.id, title="Other finding")
    )
    payload = {
        "engagement_id": engagement.id,
        "filename": "proof.txt",
        "title": "Proof",
        "evidence_type": "manual",
        "content_base64": base64.b64encode(b"proof").decode(),
    }

    missing = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={**payload, "engagement_id": "missing"},
    )
    wrong_finding = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={**payload, "finding_id": other_finding.id},
    )
    wrong_asset = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={**payload, "asset_ids": [other_asset.id]},
    )

    assert missing.status_code == 404
    assert wrong_finding.status_code == 409
    assert "finding does not belong" in wrong_finding.json()["detail"]
    assert wrong_asset.status_code == 409
    assert "does not belong" in wrong_asset.json()["detail"]
    assert list(artifacts.iter_digests()) == []
    assert store.count(Artifact) == 0
    assert store.count(Evidence) == 0


def test_database_failure_compensates_only_a_new_blob(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="Rollback"))
    request = EvidenceUploadRequest(
        engagement_id=engagement.id,
        filename="proof.txt",
        title="Proof",
        evidence_type="manual",
        content_base64=base64.b64encode(b"same bytes").decode(),
    )

    def fail_create_many(entities):
        del entities
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "create_many", fail_create_many)
    with pytest.raises(RuntimeError, match="database unavailable"):
        upload_evidence(
            store=store,
            artifact_store=artifacts,
            request=request,
        )
    assert len(list(artifacts.iter_digests())) == 1

    existing = artifacts.put_bytes(b"same bytes", engagement_id=engagement.id)
    with pytest.raises(RuntimeError, match="database unavailable"):
        upload_evidence(
            store=store,
            artifact_store=artifacts,
            request=request,
        )
    assert list(artifacts.iter_digests()) == [existing.sha256]


def test_linked_finding_update_failure_rolls_back_entities_and_new_blob(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "linked-rollback.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="Rollback"))
    finding = store.create(
        Finding(id="finding-a", engagement_id=engagement.id, title="Finding")
    )
    request = EvidenceUploadRequest(
        engagement_id=engagement.id,
        filename="proof.txt",
        title="Proof",
        evidence_type="manual",
        content_base64=base64.b64encode(b"linked proof").decode(),
        finding_id=finding.id,
    )

    def fail_update(self, model, entity_id, changes, *, expected_revision=None):
        del self, model, entity_id, changes, expected_revision
        raise RuntimeError("optimistic update failed")

    monkeypatch.setattr(StoreTransaction, "update", fail_update)
    with pytest.raises(RuntimeError, match="optimistic update failed"):
        upload_evidence(
            store=store,
            artifact_store=artifacts,
            request=request,
        )

    assert store.count(Artifact) == 0
    assert store.count(Evidence) == 0
    assert store.get(Finding, finding.id).evidence_ids == []
    assert len(list(artifacts.iter_digests())) == 1


def test_decoded_size_limit_and_missing_artifact_store(tmp_path, monkeypatch):
    request = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="proof.bin",
        title="Proof",
        evidence_type="binary",
        content_base64=base64.b64encode(b"four").decode(),
    )
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_BYTES", 3)
    with pytest.raises(EvidenceTooLargeError, match="evidence exceeds"):
        request.decoded_content()

    store = NebulaStore(tmp_path / "no-artifacts.db")
    engagement = store.create(Engagement(id="eng-a", name="No store"))
    client = TestClient(create_app(store, auth_token="test-token"))
    response = client.post(
        "/api/v1/evidence/upload",
        headers=_auth(),
        json={
            **request.model_dump(mode="json"),
            "engagement_id": engagement.id,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "evidence upload requires an artifact store"
