import base64
import io
import socket
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

import nebula.v3.knowledge as knowledge_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import Artifact, Engagement, KnowledgeSource
from nebula.v3.knowledge import (
    BrowserRuntimeUnavailableError,
    FetchedUrlDocument,
    InvalidSourceUrlError,
    SourceFetchError,
    extract_document,
    fetch_url_document,
    ingest_document,
)
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


def test_url_source_is_stored_as_an_immutable_query_free_artifact(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="URL knowledge"))
    requested: list[str] = []

    def fetcher(url: str) -> FetchedUrlDocument:
        requested.append(url)
        return FetchedUrlDocument(
            data=b"<html><body>Public deployment guide</body></html>",
            filename="guide.html",
            media_type="text/html; charset=utf-8",
            source_url="https://docs.example.com/guide.html",
        )

    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_url_fetcher=fetcher,
        )
    )
    response = client.post(
        "/api/v1/knowledge/ingest-url",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "url": "https://docs.example.com/guide.html?token=do-not-store",
        },
    )

    assert response.status_code == 201, response.text
    source = response.json()
    assert requested == ["https://docs.example.com/guide.html?token=do-not-store"]
    assert source["name"] == "guide.html"
    assert source["source_type"] == "html"
    assert source["citation"] == "https://docs.example.com/guide.html"
    assert source["metadata"]["capture_method"] == "http"
    assert source["metadata"]["origin"] == "url"
    assert source["metadata"]["source_url"] == "https://docs.example.com/guide.html"
    assert "token" not in str(source["metadata"])
    artifact = store.get(Artifact, source["artifact_id"])
    assert artifact.source == "knowledge-url"
    assert artifacts.read(artifact).startswith(b"<html>")
    assert "token" not in str(artifact.metadata)

    reindexed = client.post(
        f"/api/v1/knowledge/{source['id']}/reindex", headers=_auth()
    )
    assert reindexed.status_code == 200
    assert (
        reindexed.json()["metadata"]["source_url"] == source["metadata"]["source_url"]
    )
    assert requested == ["https://docs.example.com/guide.html?token=do-not-store"]


def test_url_source_validates_engagement_before_fetching(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    called = False

    def fetcher(url: str) -> FetchedUrlDocument:
        nonlocal called
        called = True
        raise AssertionError(url)

    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_url_fetcher=fetcher,
        )
    )
    response = client.post(
        "/api/v1/knowledge/ingest-url",
        headers=_auth(),
        json={
            "engagement_id": "missing",
            "url": "https://example.com/guide.txt",
        },
    )
    assert response.status_code == 404
    assert called is False


@pytest.mark.parametrize(
    ("failure", "status_code"),
    [
        (InvalidSourceUrlError("source URL must be public"), 422),
        (BrowserRuntimeUnavailableError("Chromium is unavailable"), 503),
        (SourceFetchError("source URL timed out"), 502),
    ],
)
def test_url_source_returns_bounded_fetch_errors(tmp_path, failure, status_code):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="URL errors"))

    def fetcher(url: str) -> FetchedUrlDocument:
        del url
        raise failure

    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            knowledge_url_fetcher=fetcher,
        )
    )
    response = client.post(
        "/api/v1/knowledge/ingest-url",
        headers=_auth(),
        json={
            "engagement_id": engagement.id,
            "url": "https://example.com/source.txt",
        },
    )
    assert response.status_code == status_code
    assert response.json()["detail"] == str(failure)
    assert store.count(Artifact) == 0
    assert store.count(KnowledgeSource) == 0


def test_url_fetch_revalidates_public_redirects_and_strips_query_metadata(
    monkeypatch,
):
    rendered: list[tuple[bytes, str]] = []

    def render(document: bytes, *, base_url: str) -> bytes:
        rendered.append((document, base_url))
        return b"<html><body>Rendered guide</body></html>"

    class Peer:
        @staticmethod
        def get_extra_info(name: str):
            return ("93.184.216.34", 443) if name == "server_addr" else None

    responses = [
        httpx.Response(
            302,
            headers={"location": "/guide.html?signature=secret"},
            request=httpx.Request("GET", "https://example.com/start"),
            extensions={"network_stream": Peer()},
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<main>Bounded public guide</main>",
            request=httpx.Request("GET", "https://example.com/guide.html"),
            extensions={"network_stream": Peer()},
        ),
    ]

    class Client:
        def __init__(self, **options):
            assert options["follow_redirects"] is False
            assert options["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, method: str, url: str, *, headers, extensions):
            assert method == "GET"
            assert url.startswith("https://93.184.216.34/")
            assert headers["Host"] == "example.com"
            assert extensions["sni_hostname"] == "example.com"
            response = responses.pop(0)

            class Stream:
                def __enter__(self):
                    return response

                def __exit__(self, *args):
                    response.close()
                    return None

            return Stream()

    monkeypatch.setattr(knowledge_module.httpx, "Client", Client)
    monkeypatch.setattr(knowledge_module, "_render_html_snapshot", render)
    monkeypatch.setattr(
        knowledge_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
    )

    fetched = fetch_url_document(
        "https://example.com/start?temporary=credential#fragment"
    )

    assert fetched.filename == "guide.html"
    assert fetched.media_type == "text/html; charset=utf-8"
    assert fetched.data == b"<html><body>Rendered guide</body></html>"
    assert fetched.source_url == "https://example.com/guide.html"
    assert fetched.capture_method == "playwright"
    assert rendered == [
        (
            b"<main>Bounded public guide</main>",
            "https://example.com/guide.html",
        )
    ]
    assert responses == []


def test_playwright_snapshot_captures_dynamic_text_through_pinned_routes(
    monkeypatch,
):
    requested: list[str] = []

    def fetch_resource(url: str, *, max_bytes: int):
        requested.append(url)
        assert max_bytes == knowledge_module.MAX_RENDER_RESOURCE_BYTES
        return knowledge_module._FetchedUrlResource(
            data=b'{"message":"Rendered by JavaScript"}',
            media_type="application/json",
            source_url=url,
        )

    monkeypatch.setattr(knowledge_module, "_fetch_url_resource", fetch_resource)
    snapshot = knowledge_module._render_html_snapshot(
        b"""
        <html><head><title>Dynamic guide</title></head>
        <body><main id="content">Loading</main>
        <script>
          fetch("/content.json").then(response => response.json()).then(data => {
            document.querySelector("#content").textContent = data.message;
          });
        </script></body></html>
        """,
        base_url="https://docs.example.com/guide?ephemeral=secret",
    )

    assert requested == ["https://docs.example.com/content.json"]
    assert b"Rendered by JavaScript" in snapshot
    assert b"Loading" not in snapshot
    assert b"ephemeral" not in snapshot
    assert b"<script" not in snapshot


def test_missing_playwright_disables_url_rendering_without_breaking_core(monkeypatch):
    def missing_import(name: str):
        assert name == "playwright.sync_api"
        raise ModuleNotFoundError("missing", name="playwright")

    monkeypatch.setattr(knowledge_module.importlib, "import_module", missing_import)

    with pytest.raises(
        BrowserRuntimeUnavailableError,
        match="Playwright is not installed",
    ):
        knowledge_module._load_playwright_runtime()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file.txt",
        "http://user:password@example.com/file.txt",
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/private",
    ],
)
def test_url_fetch_rejects_non_http_credentials_and_private_networks(url):
    with pytest.raises(InvalidSourceUrlError):
        fetch_url_document(url)


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


def test_xlsx_extraction_preserves_sheet_rows_and_never_executes_formulas():
    workbook = b"""<?xml version="1.0"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Targets" sheetId="1" r:id="rId1"/>
      <sheet name="Excluded" state="hidden" sheetId="2" r:id="rId2"/></sheets>
    </workbook>"""
    relationships = b"""<?xml version="1.0"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
      <Relationship Id="rId2" Target="worksheets/sheet2.xml"/>
    </Relationships>"""
    sheet1 = b"""<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="7"><c r="A7" t="inlineStr"><is><t>In scope</t></is></c>
      <c r="B7" t="inlineStr"><is><t>192.0.2.7</t></is></c>
      <c r="C7"><f>HYPERLINK(&quot;https://example.test&quot;)</f><v>0</v></c></row></sheetData>
    </worksheet>"""
    sheet2 = b"""<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>admin.example.test</t></is></c></row></sheetData>
    </worksheet>"""
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet1)
        archive.writestr("xl/worksheets/sheet2.xml", sheet2)

    extracted = extract_document(archive_buffer.getvalue(), filename="scope.xlsx")

    assert extracted.source_type == "xlsx"
    assert extracted.sections[0].location == "Targets, row 7"
    assert "B7: 192.0.2.7" in extracted.sections[0].text
    assert '=HYPERLINK("https://example.test")' in extracted.sections[0].text
    assert extracted.sections[1].location == "Excluded (hidden), row 2"


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
