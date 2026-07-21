import base64
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import nebula.v3.evidence as evidence_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Asset, Engagement, Evidence, Finding
from nebula.v3.evidence import (
    EvidenceReferenceError,
    EvidenceTooLargeError,
    EvidenceUploadRequest,
    InvalidEvidenceUploadError,
    upload_evidence,
)
from nebula.v3.operators import OperatorProfileService
from nebula.v3.storage import NebulaStore, StoreTransaction

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _raster_bytes(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), (20, 40, 60)).save(output, format=image_format)
    return output.getvalue()


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


def test_derived_evidence_preserves_parent_lineage_and_edit_recipe(tmp_path):
    store = NebulaStore(tmp_path / "derived.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="Derived"))
    parent = artifacts.put_bytes(
        b"original", engagement_id=engagement.id, filename="original.png"
    )
    store.create(parent)

    evidence = upload_evidence(
        store=store,
        artifact_store=artifacts,
        request=EvidenceUploadRequest(
            engagement_id=engagement.id,
            filename="annotated.png",
            title="Annotated terminal",
            evidence_type="terminal-screenshot",
            content_base64=base64.b64encode(PNG_1X1).decode(),
            media_type="image/png",
            parent_artifact_id=parent.id,
            source_context={"terminal_session_id": "terminal-1"},
            edit_recipe={
                "version": 1,
                "source_width": 1,
                "source_height": 1,
                "output_width": 1,
                "output_height": 1,
                "operations": [
                    {
                        "id": "redact-1",
                        "type": "redact",
                        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "color": "#000000",
                    }
                ],
            },
        ),
    )

    derived = store.get(Artifact, evidence.artifact_id)
    assert derived.parent_artifact_id == parent.id
    assert derived.metadata["source_context"]["terminal_session_id"] == "terminal-1"
    assert derived.metadata["edit_recipe"]["operations"][0]["type"] == "redact"
    assert evidence.metadata["edit_recipe"]["version"] == 1


def test_derived_evidence_rejects_cross_engagement_parent(tmp_path):
    store = NebulaStore(tmp_path / "cross-parent.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="One"))
    other = store.create(Engagement(id="eng-b", name="Two"))
    parent = artifacts.put_bytes(b"original", engagement_id=other.id)
    store.create(parent)

    with pytest.raises(EvidenceReferenceError, match="parent artifact"):
        upload_evidence(
            store=store,
            artifact_store=artifacts,
            request=EvidenceUploadRequest(
                engagement_id=engagement.id,
                filename="bad.png",
                title="Bad lineage",
                evidence_type="terminal-screenshot",
                content_base64=base64.b64encode(b"derived").decode(),
                parent_artifact_id=parent.id,
            ),
        )


def test_image_upload_rejects_svg_corrupt_signatures_and_decoded_bombs():
    svg = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="proof.svg",
        title="Unsafe vector",
        evidence_type="image",
        media_type="image/svg+xml",
        content_base64=base64.b64encode(
            b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        ).decode(),
    )
    with pytest.raises(InvalidEvidenceUploadError, match="SVG"):
        svg.decoded_content()

    disguised = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="proof.png",
        title="Disguised image",
        evidence_type="image",
        media_type="image/png",
        content_base64=base64.b64encode(b"not a png").decode(),
    )
    with pytest.raises(InvalidEvidenceUploadError, match="unsupported or corrupt"):
        disguised.decoded_content()

    huge_header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (20_000).to_bytes(4, "big")
        + (20_000).to_bytes(4, "big")
    )
    huge = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="huge.png",
        title="Huge decoded image",
        evidence_type="image",
        media_type="image/png",
        content_base64=base64.b64encode(huge_header).decode(),
    )
    with pytest.raises(InvalidEvidenceUploadError, match="dimensions"):
        huge.decoded_content()


@pytest.mark.parametrize(
    ("image_format", "filename", "media_type"),
    [
        ("PNG", "proof.png", "image/png"),
        ("JPEG", "proof.jpg", "image/jpeg"),
        ("WEBP", "proof.webp", "image/webp"),
    ],
)
def test_image_upload_fully_decodes_supported_rasters(
    image_format, filename, media_type
):
    content = _raster_bytes(image_format)
    request = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename=filename,
        title="Verified raster",
        evidence_type="image",
        media_type=media_type,
        content_base64=base64.b64encode(content).decode(),
    )

    assert request.decoded_content() == content


@pytest.mark.parametrize(
    ("image_format", "filename", "media_type", "removed_bytes"),
    [
        ("PNG", "truncated.png", "image/png", 12),
        ("JPEG", "truncated.jpg", "image/jpeg", 32),
        ("WEBP", "truncated.webp", "image/webp", 8),
    ],
)
def test_image_upload_rejects_header_valid_truncated_rasters(
    image_format, filename, media_type, removed_bytes
):
    content = _raster_bytes(image_format)[:-removed_bytes]
    request = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename=filename,
        title="Truncated raster",
        evidence_type="image",
        media_type=media_type,
        content_base64=base64.b64encode(content).decode(),
    )

    with pytest.raises(InvalidEvidenceUploadError, match="truncated or corrupt"):
        request.decoded_content()


def test_edit_recipe_accepts_bounded_canvas_manifest():
    recipe = {
        "version": 1,
        "source_width": 10,
        "source_height": 8,
        "output_width": 6,
        "output_height": 5,
        "operations": [
            {
                "id": "crop-1",
                "type": "crop",
                "rect": {"x": 1, "y": 1, "width": 6, "height": 5},
            },
            {
                "id": "rectangle-1",
                "type": "rectangle",
                "rect": {"x": 0, "y": 0, "width": 2, "height": 2},
                "color": "#ff00AA",
                "thickness": 2,
            },
            {
                "id": "arrow-1",
                "type": "arrow",
                "from": {"x": 0, "y": 0},
                "to": {"x": 6, "y": 5},
                "color": "#00ff00",
                "thickness": 3,
            },
            {
                "id": "blur-1",
                "type": "blur",
                "rect": {"x": 1, "y": 1, "width": 2, "height": 2},
                "radius": 8,
            },
            {
                "id": "redact-1",
                "type": "redact",
                "rect": {"x": 2, "y": 2, "width": 2, "height": 2},
                "color": "#000000",
            },
            {
                "id": "text-1",
                "type": "text",
                "at": {"x": 1, "y": 1},
                "text": "Review",
                "color": "#ffffff",
                "fontSize": 16,
            },
        ],
    }

    request = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="edited.png",
        title="Edited screenshot",
        evidence_type="terminal-screenshot",
        content_base64=base64.b64encode(PNG_1X1).decode(),
        media_type="image/png",
        edit_recipe=recipe,
    )

    assert request.edit_recipe == recipe


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": 2}, "version is unsupported"),
        ({"version": 1.0}, "version is unsupported"),
        ({"unexpected": True}, "must contain only"),
        ({"output_width": 2}, "output dimensions"),
        (
            {
                "operations": [
                    {
                        "id": "bad-1",
                        "type": "redact",
                        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "color": "black",
                    }
                ]
            },
            "colors must be opaque",
        ),
        (
            {"operations": [{"id": "bad-1", "type": "rotate"}]},
            "unsupported operation",
        ),
        (
            {
                "operations": [
                    {
                        "id": "duplicate",
                        "type": "redact",
                        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "color": "#000000",
                    },
                    {
                        "id": "duplicate",
                        "type": "redact",
                        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "color": "#000000",
                    },
                ]
            },
            "operation ids must be unique",
        ),
    ],
)
def test_edit_recipe_rejects_unversioned_unknown_or_invalid_manifests(changes, message):
    recipe = {
        "version": 1,
        "source_width": 1,
        "source_height": 1,
        "output_width": 1,
        "output_height": 1,
        "operations": [],
    }
    recipe.update(changes)

    with pytest.raises(ValueError, match=message):
        EvidenceUploadRequest(
            engagement_id="eng-a",
            filename="edited.png",
            title="Edited screenshot",
            evidence_type="terminal-screenshot",
            content_base64=base64.b64encode(PNG_1X1).decode(),
            media_type="image/png",
            edit_recipe=recipe,
        )


def test_edit_recipe_rejects_excess_operations_and_mismatched_uploaded_image():
    recipe = {
        "version": 1,
        "source_width": 1,
        "source_height": 1,
        "output_width": 1,
        "output_height": 1,
        "operations": [{}] * 201,
    }
    with pytest.raises(ValueError, match="at most 200 operations"):
        EvidenceUploadRequest(
            engagement_id="eng-a",
            filename="edited.png",
            title="Edited screenshot",
            evidence_type="terminal-screenshot",
            content_base64=base64.b64encode(PNG_1X1).decode(),
            media_type="image/png",
            edit_recipe=recipe,
        )

    mismatched = {
        **recipe,
        "source_width": 2,
        "source_height": 2,
        "output_width": 2,
        "output_height": 2,
        "operations": [],
    }
    request = EvidenceUploadRequest(
        engagement_id="eng-a",
        filename="edited.png",
        title="Edited screenshot",
        evidence_type="terminal-screenshot",
        content_base64=base64.b64encode(PNG_1X1).decode(),
        media_type="image/png",
        edit_recipe=mismatched,
    )
    with pytest.raises(InvalidEvidenceUploadError, match="output dimensions"):
        request.decoded_content()


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
