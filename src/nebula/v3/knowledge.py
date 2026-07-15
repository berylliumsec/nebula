"""Local, deterministic document ingestion for engagement knowledge sources.

The source artifact is authoritative. Extracted chunks are a rebuildable index
stored on ``KnowledgeSource.metadata`` so provider-backed chat can retrieve
bounded, cited context without trusting instructions embedded in a document.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pypdf import PdfReader

from .artifacts import ArtifactIntegrityError, ArtifactStore
from .domain import Artifact, Engagement, KnowledgeSource, utc_now
from .storage import NebulaStore

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 4 * 1024 * 1024
MAX_PDF_PAGES = 2_000
CHUNK_CHARACTERS = 1_800
CHUNK_OVERLAP_CHARACTERS = 180
INGESTION_VERSION = "nebula.knowledge.v1"


class KnowledgeIngestionError(ValueError):
    """Base class for safe, operator-visible ingestion failures."""


class DocumentTooLargeError(KnowledgeIngestionError):
    """The source or extracted representation exceeds a bounded limit."""


class UnsupportedDocumentError(KnowledgeIngestionError):
    """The document format cannot be safely extracted by this Core."""


class InvalidDocumentError(KnowledgeIngestionError):
    """The document is malformed or contains no usable text."""


@dataclass(frozen=True)
class ExtractedSection:
    text: str
    page: int | None = None


@dataclass(frozen=True)
class ExtractedDocument:
    source_type: str
    media_type: str
    sections: tuple[ExtractedSection, ...]


_EXTENSION_FORMATS: dict[str, tuple[str, str]] = {
    ".txt": ("text", "text/plain"),
    ".md": ("markdown", "text/markdown"),
    ".markdown": ("markdown", "text/markdown"),
    ".rst": ("text", "text/plain"),
    ".log": ("text", "text/plain"),
    ".csv": ("csv", "text/csv"),
    ".json": ("json", "application/json"),
    ".jsonl": ("jsonl", "application/x-ndjson"),
    ".ndjson": ("jsonl", "application/x-ndjson"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".pdf": ("pdf", "application/pdf"),
    ".docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}

_MEDIA_FORMATS: dict[str, tuple[str, str]] = {
    "text/plain": ("text", "text/plain"),
    "text/markdown": ("markdown", "text/markdown"),
    "text/x-markdown": ("markdown", "text/markdown"),
    "text/csv": ("csv", "text/csv"),
    "application/csv": ("csv", "text/csv"),
    "application/json": ("json", "application/json"),
    "application/jsonl": ("jsonl", "application/x-ndjson"),
    "application/x-jsonlines": ("jsonl", "application/x-ndjson"),
    "application/x-ndjson": ("jsonl", "application/x-ndjson"),
    "text/html": ("html", "text/html"),
    "application/xhtml+xml": ("html", "text/html"),
    "application/pdf": ("pdf", "application/pdf"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}


def safe_filename(filename: str) -> str:
    """Return a safe display-only basename."""

    name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or "\x00" in name:
        raise InvalidDocumentError("a valid filename is required")
    if len(name) > 255:
        raise InvalidDocumentError("filename must be at most 255 characters")
    return name


def extract_document(
    data: bytes,
    *,
    filename: str,
    media_type: str | None = None,
) -> ExtractedDocument:
    """Extract supported bytes without executing or resolving document content."""

    if not data:
        raise InvalidDocumentError("the uploaded document is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLargeError(
            f"document exceeds the {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB limit"
        )

    clean_name = safe_filename(filename)
    extension = Path(clean_name).suffix.lower()
    supplied_media_type = (media_type or "").partition(";")[0].strip().lower()
    detected = _EXTENSION_FORMATS.get(extension)
    from_media = _MEDIA_FORMATS.get(supplied_media_type)
    if detected and from_media and detected[0] != from_media[0]:
        raise UnsupportedDocumentError(
            f"filename extension {extension} does not match media type {supplied_media_type}"
        )
    document_format = detected or from_media
    if document_format is None:
        label = extension or supplied_media_type or "unknown"
        raise UnsupportedDocumentError(
            f"unsupported knowledge document format: {label}"
        )
    source_type, normalized_media_type = document_format

    if source_type == "pdf":
        sections = _extract_pdf(data)
    elif source_type == "docx":
        sections = _extract_docx(data)
    else:
        text = _decode_text(data)
        if source_type == "json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                record_caught_exception(
                    "knowledge",
                    "knowledge.knowledge.caught_failure_001",
                    "A handled knowledge operation raised an exception.",
                    exc,
                    stage="knowledge",
                )
                raise InvalidDocumentError(f"invalid JSON document: {exc.msg}") from exc
        elif source_type == "jsonl":
            _validate_json_lines(text)
        if source_type == "html":
            text = _extract_html(text)
        sections = (ExtractedSection(text=text),)

    total_characters = sum(len(section.text) for section in sections)
    if total_characters > MAX_EXTRACTED_CHARACTERS:
        raise DocumentTooLargeError(
            "extracted document text exceeds the 4 MiB index limit"
        )
    if not any(section.text.strip() for section in sections):
        raise InvalidDocumentError("the document contains no extractable text")
    return ExtractedDocument(
        source_type=source_type,
        media_type=normalized_media_type,
        sections=tuple(section for section in sections if section.text.strip()),
    )


def build_chunks(
    document: ExtractedDocument,
    *,
    source_id: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    """Build stable, bounded chunks that retain page and artifact provenance."""

    chunks: list[dict[str, Any]] = []
    for section in document.sections:
        for text in _split_text(section.text):
            identity = hashlib.sha256(
                f"{source_id}\0{section.page or 0}\0{text}".encode("utf-8")
            ).hexdigest()[:24]
            chunk: dict[str, Any] = {
                "id": identity,
                "text": text,
                "artifact_id": artifact_id,
            }
            if section.page is not None:
                chunk["page"] = section.page
            chunks.append(chunk)
    if not chunks:
        raise InvalidDocumentError("the document contains no indexable text")
    return chunks


def ingest_document(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    engagement_id: str,
    filename: str,
    data: bytes,
    media_type: str | None = None,
) -> KnowledgeSource:
    """Atomically persist an authoritative artifact and its retrieval source."""

    store.get(Engagement, engagement_id)
    clean_name = safe_filename(filename)
    extracted = extract_document(data, filename=clean_name, media_type=media_type)
    stored = artifact_store.put_bytes_with_status(
        data,
        engagement_id=engagement_id,
        filename=clean_name,
        media_type=extracted.media_type,
        source="knowledge-upload",
        metadata={"ingestion_version": INGESTION_VERSION},
    )
    source = KnowledgeSource(
        engagement_id=engagement_id,
        name=clean_name,
        source_type=extracted.source_type,
        artifact_id=stored.artifact.id,
        status="ready",
        citation=clean_name,
    )
    chunks = build_chunks(
        extracted,
        source_id=source.id,
        artifact_id=stored.artifact.id,
    )
    source = source.model_copy(
        update={
            "document_count": len(chunks),
            "metadata": _index_metadata(
                stored.artifact,
                chunks=chunks,
                indexed_at=utc_now().isoformat(),
            ),
        }
    )
    try:
        store.create_many([stored.artifact, source])
    except Exception as caught_error:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_002",
            "A handled knowledge operation raised an exception.",
            caught_error,
            stage="knowledge",
        )
        artifact_store.discard_new_blob(stored)
        raise
    return source


def reindex_document(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    source_id: str,
) -> KnowledgeSource:
    """Rebuild a knowledge index exclusively from its immutable artifact."""

    source = store.get(KnowledgeSource, source_id)
    if not source.artifact_id:
        raise InvalidDocumentError("knowledge source has no authoritative artifact")
    artifact = store.get(Artifact, source.artifact_id)
    if artifact.engagement_id != source.engagement_id:
        raise ArtifactIntegrityError(
            "knowledge artifact ownership does not match source"
        )
    if not artifact_store.verify(artifact):
        raise ArtifactIntegrityError("knowledge artifact failed integrity verification")
    data = artifact_store.read(artifact)
    extracted = extract_document(
        data,
        filename=artifact.filename or source.name,
        media_type=artifact.media_type,
    )
    chunks = build_chunks(
        extracted,
        source_id=source.id,
        artifact_id=artifact.id,
    )
    metadata = dict(source.metadata)
    metadata.update(
        _index_metadata(
            artifact,
            chunks=chunks,
            indexed_at=utc_now().isoformat(),
        )
    )
    return store.update(
        KnowledgeSource,
        source.id,
        {
            "source_type": extracted.source_type,
            "status": "ready",
            "citation": source.citation or source.name,
            "document_count": len(chunks),
            "metadata": metadata,
        },
        expected_revision=source.revision,
    )


def knowledge_summary(source: KnowledgeSource) -> KnowledgeSource:
    """Return an API-safe source without its potentially large internal chunks."""

    metadata = {key: value for key, value in source.metadata.items() if key != "chunks"}
    return source.model_copy(update={"metadata": metadata})


def _index_metadata(
    artifact: Artifact,
    *,
    chunks: list[dict[str, Any]],
    indexed_at: str,
) -> dict[str, Any]:
    return {
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "size": artifact.size,
        "sha256": artifact.sha256,
        "chunk_count": len(chunks),
        "indexed_at": indexed_at,
        "ingestion_version": INGESTION_VERSION,
        "chunks": chunks,
    }


def _decode_text(data: bytes) -> str:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_003",
            "A handled knowledge operation raised an exception.",
            exc,
            stage="knowledge",
        )
        raise InvalidDocumentError("text documents must use UTF-8 or UTF-16") from exc
    if "\x00" in text:
        raise InvalidDocumentError("text document contains binary NUL bytes")
    return text


def _validate_json_lines(text: str) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            record_caught_exception(
                "knowledge",
                "knowledge.knowledge.caught_failure_004",
                "A handled knowledge operation raised an exception.",
                exc,
                stage="knowledge",
            )
            raise InvalidDocumentError(
                f"invalid JSON Lines document at line {line_number}: {exc.msg}"
            ) from exc


class _VisibleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif not self._ignored_depth and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _extract_html(text: str) -> str:
    parser = _VisibleHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_005",
            "A handled knowledge operation raised an exception.",
            exc,
            stage="knowledge",
        )
        raise InvalidDocumentError("invalid HTML document") from exc
    return "".join(parser.parts)


def _extract_docx(data: bytes) -> tuple[ExtractedSection, ...]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_EXTRACTED_CHARACTERS * 4:
                raise DocumentTooLargeError("DOCX document XML exceeds the index limit")
            document_xml = archive.read(info)
    except DocumentTooLargeError as caught_error:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_006",
            "A handled knowledge operation raised an exception.",
            caught_error,
            stage="knowledge",
        )
        raise
    except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_007",
            "A handled knowledge operation raised an exception.",
            exc,
            stage="knowledge",
        )
        raise InvalidDocumentError("invalid or encrypted DOCX document") from exc
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", document_xml, flags=re.IGNORECASE):
        raise InvalidDocumentError("DOCX document XML declarations are not supported")
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_008",
            "A handled knowledge operation raised an exception.",
            exc,
            stage="knowledge",
        )
        raise InvalidDocumentError("invalid DOCX document XML") from exc

    paragraphs: list[str] = []
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            paragraphs.append(text)
    return (ExtractedSection(text="\n\n".join(paragraphs)),)


def _extract_pdf(data: bytes) -> tuple[ExtractedSection, ...]:
    if not data.lstrip().startswith(b"%PDF-"):
        raise InvalidDocumentError("invalid PDF header")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise InvalidDocumentError("encrypted PDF documents are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentTooLargeError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
        sections: list[ExtractedSection] = []
        extracted_characters = 0
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            extracted_characters += len(text)
            if extracted_characters > MAX_EXTRACTED_CHARACTERS:
                raise DocumentTooLargeError(
                    "extracted document text exceeds the 4 MiB index limit"
                )
            sections.append(ExtractedSection(text=text, page=index))
    except KnowledgeIngestionError as caught_error:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_009",
            "A handled knowledge operation raised an exception.",
            caught_error,
            stage="knowledge",
        )
        raise
    except Exception as exc:
        record_caught_exception(
            "knowledge",
            "knowledge.knowledge.caught_failure_010",
            "A handled knowledge operation raised an exception.",
            exc,
            stage="knowledge",
        )
        raise InvalidDocumentError("PDF text extraction failed") from exc
    return tuple(sections)


def _split_text(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[\t\f\v]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + CHUNK_CHARACTERS, len(normalized))
        if end < len(normalized):
            minimum = start + CHUNK_CHARACTERS // 2
            paragraph_break = normalized.rfind("\n\n", minimum, end)
            line_break = normalized.rfind("\n", minimum, end)
            word_break = normalized.rfind(" ", minimum, end)
            boundary = max(paragraph_break, line_break, word_break)
            if boundary > start:
                end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        next_start = max(end - CHUNK_OVERLAP_CHARACTERS, start + 1)
        whitespace = normalized.find(" ", next_start, end)
        start = whitespace + 1 if whitespace >= 0 else next_start
    return chunks


__all__ = [
    "CHUNK_CHARACTERS",
    "DocumentTooLargeError",
    "INGESTION_VERSION",
    "InvalidDocumentError",
    "KnowledgeIngestionError",
    "MAX_DOCUMENT_BYTES",
    "UnsupportedDocumentError",
    "build_chunks",
    "extract_document",
    "ingest_document",
    "knowledge_summary",
    "reindex_document",
    "safe_filename",
]
