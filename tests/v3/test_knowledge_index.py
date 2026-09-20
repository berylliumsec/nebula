import base64
import io
import re
import zipfile
from typing import Any

import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.domain import Artifact, Engagement, KnowledgeSource, LibraryItem
from nebula.v3.knowledge import GLOBAL_LIBRARY_ARTIFACT_OWNER
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.storage import NebulaStore


class SecurityEmbeddingFunction(EmbeddingFunction[Documents]):
    """Small deterministic embedding used to exercise Chroma without downloads."""

    @staticmethod
    def name() -> str:
        return "nebula-test-security-embedding"

    @staticmethod
    def get_config() -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "SecurityEmbeddingFunction":
        assert config == {}
        return SecurityEmbeddingFunction()

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        vectors: Embeddings = []
        concepts = (
            {"authentication", "login", "credential", "password"},
            {"database", "sql", "postgres", "storage"},
            {"network", "port", "listener", "tls"},
        )
        for document in input:
            terms = set(re.findall(r"[a-z0-9]+", document.casefold()))
            values = [float(len(terms & concept)) for concept in concepts]
            if not any(values):
                values.append(1.0)
            else:
                values.append(0.0)
            vectors.append(np.asarray(values, dtype=np.float32))
        return vectors


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_chroma_index_persists_semantic_chunks_and_isolates_engagements(tmp_path):
    embedding = SecurityEmbeddingFunction()
    index_path = tmp_path / "knowledge-index"
    index = ChromaKnowledgeIndex(index_path, embedding_function=embedding)
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="A"))
    other = store.create(Engagement(id="eng-b", name="B"))
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_index=index,
        )
    )
    status = client.get("/api/v1/knowledge/index-status", headers=_auth())
    assert status.status_code == 200
    assert status.json()["state"] == "ready"
    assert status.json()["model"] == SecurityEmbeddingFunction.name()

    def upload(engagement_id: str, filename: str, content: bytes) -> dict[str, Any]:
        response = client.post(
            "/api/v1/knowledge/ingest",
            headers=_auth(),
            json={
                "engagement_id": engagement_id,
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        assert response.status_code == 201
        return response.json()

    source = upload(
        engagement.id,
        "access.txt",
        b"Operators authenticate with a hardware credential before access.",
    )
    upload(
        other.id,
        "private.txt",
        b"The unrelated login secret is CROSS_ENGAGEMENT_SECRET.",
    )
    persisted = store.get(KnowledgeSource, source["id"])
    assert persisted.status == "ready"
    assert persisted.metadata["index_backend"] == "chromadb"
    assert persisted.metadata["chunk_count"] == 1
    assert "chunks" not in persisted.metadata

    # A new client proves the collection is on disk rather than process memory.
    reopened = ChromaKnowledgeIndex(index_path, embedding_function=embedding)
    matches = reopened.query(engagement.id, ["How does a user log in?"], limit=8)
    assert [match.source_id for match in matches] == [source["id"]]
    assert "hardware credential" in matches[0].text
    assert "CROSS_ENGAGEMENT_SECRET" not in [match.text for match in matches]

    chat = ChatService(store, knowledge_index=reopened)
    search = chat.harness_knowledge_search(
        engagement.id,
        "How does a user log in?",
        allow_local_only=True,
    )
    assert "hardware credential" in search.matches[0].text
    assert search.matches[0].citation.source_id == source["id"]
    context = chat.harness_knowledge_context(engagement.id, "How does a user log in?")
    assert "hardware credential" in context.text
    assert [citation.source_id for citation in context.citations] == [source["id"]]

    removed = client.delete(f"/api/v1/knowledge/{source['id']}", headers=_auth())
    assert removed.status_code == 204
    assert reopened.query(engagement.id, ["login"], limit=8) == []


def test_global_library_persists_scripts_and_retrieves_across_engagements(tmp_path):
    embedding = SecurityEmbeddingFunction()
    index_path = tmp_path / "knowledge-index"
    index = ChromaKnowledgeIndex(index_path, embedding_function=embedding)
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    first = store.create(Engagement(id="eng-a", name="A"))
    second = store.create(Engagement(id="eng-b", name="B"))
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_index=index,
        )
    )
    content = b"def check_login():\n    return 'hardware credential authentication'\n"
    response = client.post(
        "/api/v1/library/items/ingest",
        headers=_auth(),
        json={
            "filename": "auth_check.py",
            "media_type": "text/x-python",
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert response.status_code == 201
    item_payload = response.json()
    assert item_payload["source_type"] == "script"
    assert item_payload["metadata"]["scope"] == "library"
    assert item_payload["metadata"]["collection"] == "nebula-library-v1"
    assert "chunks" not in item_payload["metadata"]

    item = store.get(LibraryItem, item_payload["id"])
    artifact = store.get(Artifact, item.artifact_id or "")
    assert artifact.engagement_id == GLOBAL_LIBRARY_ARTIFACT_OWNER
    assert artifact.source == "library-upload"
    assert artifacts.read(artifact) == content

    reopened = ChromaKnowledgeIndex(index_path, embedding_function=embedding)
    matches = reopened.query_library(["How is login authenticated?"], limit=8)
    assert [match.source_id for match in matches] == [item.id]
    assert matches[0].scope == "library"

    for engagement in (first, second):
        search = ChatService(
            store,
            knowledge_index=reopened,
        ).harness_knowledge_search(
            engagement.id,
            "How is login authenticated?",
            allow_local_only=True,
        )
        assert "hardware credential" in search.matches[0].text
        assert search.matches[0].citation.source_id == item.id
        assert search.matches[0].citation.citation == "Library: auth_check.py"

    listed = client.get("/api/v1/library/items", headers=_auth())
    assert [entry["id"] for entry in listed.json()] == [item.id]
    downloaded = client.get(
        f"/api/v1/artifacts/{artifact.id}/content",
        headers=_auth(),
    )
    assert downloaded.content == content
    current = store.get(LibraryItem, item.id)
    store.update(
        LibraryItem,
        item.id,
        {"status": "stale", "document_count": 0},
        expected_revision=current.revision,
    )
    reindexed = client.post(
        f"/api/v1/library/items/{item.id}/reindex",
        headers=_auth(),
    )
    assert reindexed.status_code == 200
    assert reindexed.json()["status"] == "ready"
    assert reindexed.json()["document_count"] == 1

    removed = client.delete(
        f"/api/v1/library/items/{item.id}",
        headers=_auth(),
    )
    assert removed.status_code == 204
    assert reopened.query_library(["authentication"], limit=8) == []
    assert artifacts.read(artifact) == content


def test_startup_migrates_inline_chunks_to_chroma(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="A"))
    source = store.create(
        KnowledgeSource(
            id="source-a",
            engagement_id=engagement.id,
            name="legacy.txt",
            source_type="text",
            metadata={
                "chunks": [
                    {
                        "id": "legacy-chunk",
                        "text": "The PostgreSQL database stores the findings.",
                    }
                ]
            },
        )
    )
    index = ChromaKnowledgeIndex(
        tmp_path / "knowledge-index",
        embedding_function=SecurityEmbeddingFunction(),
    )

    with TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_index=index,
        )
    ):
        pass

    migrated = store.get(KnowledgeSource, source.id)
    assert migrated.metadata["index_backend"] == "chromadb"
    assert "chunks" not in migrated.metadata
    assert index.query(engagement.id, ["SQL storage"], limit=8)[0].id == "legacy-chunk"


def test_spreadsheet_with_repeated_rows_indexes_and_reindexes(tmp_path):
    embedding = SecurityEmbeddingFunction()
    index = ChromaKnowledgeIndex(
        tmp_path / "knowledge-index", embedding_function=embedding
    )
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-a", name="A"))
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_index=index,
        )
    )
    workbook = b"""<?xml version="1.0"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="EMEA" sheetId="1" r:id="rId1"/>
      <sheet name="APAC" sheetId="2" r:id="rId2"/></sheets>
    </workbook>"""
    relationships = b"""<?xml version="1.0"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
      <Relationship Id="rId2" Target="worksheets/sheet2.xml"/>
    </Relationships>"""
    sheet = b"""<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Host</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Port</t></is></c></row>
      <row r="2"><c r="A2" t="inlineStr"><is><t>db.example.test</t></is></c>
      <c r="B2" t="inlineStr"><is><t>5432 postgres listener</t></is></c></row></sheetData>
    </worksheet>"""
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/worksheets/sheet2.xml", sheet)

    response = client.post(
        "/api/v1/knowledge/ingest",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "filename": "assets.xlsx",
            "content_base64": base64.b64encode(archive_buffer.getvalue()).decode(),
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["metadata"]["chunk_count"] == 4
    matches = index.query(engagement.id, ["postgres database port"], limit=8)
    listener_rows = [match for match in matches if "5432" in match.text]
    # The identical EMEA and APAC rows are both retrievable under distinct ids.
    assert len(listener_rows) == 2
    assert len({match.id for match in listener_rows}) == 2

    reindexed = client.post(
        f"/api/v1/knowledge/{response.json()['id']}/reindex", headers=_auth()
    )
    assert reindexed.status_code == 200, reindexed.text
    assert reindexed.json()["status"] == "ready"
    assert reindexed.json()["document_count"] == 4


def test_startup_migrates_inline_library_chunks_to_chroma(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    item = store.create(
        LibraryItem(
            id="library-a",
            name="legacy-playbook.txt",
            source_type="text",
            document_count=1,
            metadata={
                "scope": "library",
                "chunks": [
                    {
                        "id": "legacy-library-chunk",
                        "text": "Rotate the PostgreSQL database credential after access.",
                    }
                ],
            },
        )
    )
    index = ChromaKnowledgeIndex(
        tmp_path / "knowledge-index",
        embedding_function=SecurityEmbeddingFunction(),
    )

    with TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_index=index,
        )
    ):
        pass

    migrated = store.get(LibraryItem, item.id)
    assert migrated.metadata["index_backend"] == "chromadb"
    assert migrated.metadata["collection"] == "nebula-library-v1"
    assert "chunks" not in migrated.metadata
    matches = index.query_library(["SQL storage"], limit=8)
    assert [match.id for match in matches] == ["legacy-library-chunk"]
    assert matches[0].scope == "library"


def test_default_chroma_index_reports_first_use_download_before_ingestion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", tmp_path / "model-cache")
    index = ChromaKnowledgeIndex(tmp_path / "knowledge-index")
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
            knowledge_index=index,
        )
    )

    assert index.status.model == "all-MiniLM-L6-v2"
    assert index.status.state == "required"
    assert index.status.downloaded_bytes == 0
    assert index.status.total_bytes == 83_178_821
    response = client.get("/api/v1/knowledge/index-status", headers=_auth())
    assert response.status_code == 200
    assert response.json() == {
        "backend": "chromadb",
        "state": "required",
        "model": "all-MiniLM-L6-v2",
        "downloaded_bytes": 0,
        "total_bytes": 83_178_821,
        "detail": None,
    }
