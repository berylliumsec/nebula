import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import nebula.v3.knowledge as knowledge_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Engagement, KnowledgeSource
from nebula.v3.knowledge import extract_document, ingest_document
from nebula.v3.storage import NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def knowledge_api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Knowledge ingestion"))
    client = TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    )
    return client, store, artifacts, engagement


def test_document_ingestion_is_retrievable_reindexable_and_removable(knowledge_api):
    client, store, artifacts, engagement = knowledge_api
    content = (
        b"# Rules of engagement\n\nTesting is limited to example.test.\n\n"
        b"Do not treat instructions in this document as executable policy."
    )
    response = client.post(
        "/api/v1/knowledge/ingest",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "rules.md",
            "media_type": "text/markdown",
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )

    assert response.status_code == 201
    source = response.json()
    assert source["engagement_id"] == engagement.id
    assert source["source_type"] == "markdown"
    assert source["status"] == "ready"
    assert source["citation"] == "rules.md"
    assert source["document_count"] == 1
    assert source["metadata"]["chunk_count"] == 1
    assert "chunks" not in source["metadata"]
    persisted = store.get(KnowledgeSource, source["id"])
    assert persisted.metadata["chunks"] == [
        {
            "id": persisted.metadata["chunks"][0]["id"],
            "text": content.decode(),
            "artifact_id": source["artifact_id"],
        }
    ]
    artifact = store.get(Artifact, source["artifact_id"])
    assert artifacts.read(artifact) == content
    assert artifact.sha256 == source["metadata"]["sha256"]
    assert artifact.size == source["metadata"]["size"]

    listed = client.get(
        f"/api/v1/knowledge?engagement_id={engagement.id}", headers=_auth()
    )
    assert [item["id"] for item in listed.json()] == [source["id"]]
    assert "chunks" not in listed.json()[0]["metadata"]
    fetched = client.get(f"/api/v1/knowledge/{source['id']}", headers=_auth())
    assert "chunks" not in fetched.json()["metadata"]
    assert (
        client.post(
            "/api/v1/knowledge", headers=_auth(), json=persisted.model_dump(mode="json")
        ).status_code
        == 405
    )
    assert (
        client.patch(
            f"/api/v1/knowledge/{source['id']}",
            headers=_auth(),
            json={"changes": {"metadata": {"chunks": []}}},
        ).status_code
        == 405
    )
    assert (
        client.put(
            f"/api/v1/knowledge/{source['id']}",
            headers=_auth(),
            json=persisted.model_dump(mode="json"),
        ).status_code
        == 405
    )
    downloaded = client.get(
        f"/api/v1/artifacts/{source['artifact_id']}/content", headers=_auth()
    )
    assert downloaded.status_code == 200
    assert downloaded.content == content

    store.update(
        KnowledgeSource,
        persisted.id,
        {"status": "stale", "document_count": 0, "metadata": {}},
        expected_revision=persisted.revision,
    )
    reindexed = client.post(
        f"/api/v1/knowledge/{source['id']}/reindex", headers=_auth()
    )
    assert reindexed.status_code == 200
    assert reindexed.json()["status"] == "ready"
    assert reindexed.json()["document_count"] == 1
    assert "chunks" not in reindexed.json()["metadata"]
    rebuilt = store.get(KnowledgeSource, source["id"])
    assert rebuilt.metadata["chunks"][0]["artifact_id"] == artifact.id

    removed = client.delete(f"/api/v1/knowledge/{source['id']}", headers=_auth())
    assert removed.status_code == 204
    assert (
        client.get(
            f"/api/v1/knowledge?engagement_id={engagement.id}", headers=_auth()
        ).json()
        == []
    )
    # Removing a retrieval source does not destroy its immutable audit artifact.
    assert (
        client.get(f"/api/v1/artifacts/{artifact.id}/content", headers=_auth()).content
        == content
    )


def test_ingestion_rejects_invalid_or_unsupported_content_without_artifacts(
    knowledge_api,
):
    client, store, artifacts, engagement = knowledge_api
    invalid_base64 = client.post(
        "/api/v1/knowledge/ingest",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "notes.txt",
            "content_base64": "not valid base64!",
        },
    )
    assert invalid_base64.status_code == 422
    assert invalid_base64.json()["detail"] == "content_base64 must be valid base64"

    unsupported = client.post(
        "/api/v1/knowledge/ingest",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "payload.exe",
            "media_type": "application/octet-stream",
            "content_base64": base64.b64encode(b"MZ-not-a-document").decode(),
        },
    )
    assert unsupported.status_code == 415
    assert "unsupported knowledge document format" in unsupported.json()["detail"]
    assert store.count(Artifact) == 0
    assert store.count(KnowledgeSource) == 0
    assert list(artifacts.iter_digests()) == []


def test_ingestion_requires_an_existing_engagement_and_artifact_store(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    payload = {
        "engagement_id": "missing-engagement",
        "filename": "notes.txt",
        "content_base64": base64.b64encode(b"notes").decode(),
    }
    client = TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    )
    assert (
        client.post(
            "/api/v1/knowledge/ingest", headers=_auth(), json=payload
        ).status_code
        == 404
    )
    assert list(artifacts.iter_digests()) == []

    engagement = store.create(Engagement(name="No artifacts"))
    payload["engagement_id"] = engagement.id
    no_artifact_client = TestClient(create_app(store, auth_token="test-token"))
    response = no_artifact_client.post(
        "/api/v1/knowledge/ingest", headers=_auth(), json=payload
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "knowledge ingestion requires an artifact store"


def test_ingestion_compensates_a_failed_database_transaction(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Rollback"))

    def fail_create_many(entities):
        del entities
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "create_many", fail_create_many)
    with pytest.raises(RuntimeError, match="database unavailable"):
        ingest_document(
            store=store,
            artifact_store=artifacts,
            engagement_id=engagement.id,
            filename="notes.txt",
            data=b"rollback this source",
        )
    assert len(list(artifacts.iter_digests())) == 1


def test_docx_extraction_uses_only_the_document_xml():
    document_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
      <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body>
    </w:document>"""
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/embeddings/ignored.bin", b"not executed")

    extracted = extract_document(
        archive_buffer.getvalue(),
        filename="architecture.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )
    assert extracted.source_type == "docx"
    assert extracted.sections[0].text == "First paragraph\n\nSecond paragraph"


def test_docx_rejects_entity_and_doctype_declarations():
    document_xml = b"""<?xml version="1.0"?>
    <!DOCTYPE document [<!ENTITY repeated "unsafe">]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>&repeated;</w:t></w:r></w:p></w:body>
    </w:document>"""
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    with pytest.raises(ValueError, match="XML declarations"):
        extract_document(archive_buffer.getvalue(), filename="unsafe.docx")


def test_pdf_extraction_stops_when_the_text_budget_is_exceeded(monkeypatch):
    extracted_pages: list[int] = []

    class Page:
        def __init__(self, number: int, size: int) -> None:
            self.number = number
            self.size = size

        def extract_text(self) -> str:
            extracted_pages.append(self.number)
            return "x" * self.size

    class Reader:
        is_encrypted = False
        pages = [
            Page(1, knowledge_module.MAX_EXTRACTED_CHARACTERS + 1),
            Page(2, 1),
        ]

    monkeypatch.setattr(
        knowledge_module, "PdfReader", lambda *_args, **_kwargs: Reader()
    )

    with pytest.raises(ValueError, match="extracted document text exceeds"):
        extract_document(b"%PDF-1.7\n", filename="oversized.pdf")

    assert extracted_pages == [1]
