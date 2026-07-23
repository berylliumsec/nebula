"""Local, deterministic document ingestion for engagement knowledge sources.

The source artifact is authoritative. Extracted chunks are a rebuildable index
stored on ``KnowledgeSource.metadata`` so provider-backed chat can retrieve
bounded, cited context without trusting instructions embedded in a document.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception

import hashlib
import json
import posixpath
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
from .knowledge_index import KnowledgeIndex, KnowledgeIndexError
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
    location: str | None = None


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
    ".xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    elif source_type == "xlsx":
        sections = _extract_xlsx(data)
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
            if section.location is not None:
                chunk["location"] = section.location
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
    knowledge_index: KnowledgeIndex | None = None,
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
        status="indexing" if knowledge_index is not None else "ready",
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
                chunks=chunks if knowledge_index is None else None,
                chunk_count=len(chunks),
                indexed_at=utc_now().isoformat(),
                index_descriptor=(
                    knowledge_index.descriptor if knowledge_index is not None else None
                ),
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
    if knowledge_index is None:
        return source
    try:
        knowledge_index.upsert_source(source, chunks)
    except KnowledgeIndexError:
        current = store.get(KnowledgeSource, source.id)
        store.update(
            KnowledgeSource,
            source.id,
            {"status": "error"},
            expected_revision=current.revision,
        )
        raise
    current = store.get(KnowledgeSource, source.id)
    source = store.update(
        KnowledgeSource,
        source.id,
        {"status": "ready"},
        expected_revision=current.revision,
    )
    return source


def reindex_document(
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    source_id: str,
    knowledge_index: KnowledgeIndex | None = None,
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
            chunks=chunks if knowledge_index is None else None,
            chunk_count=len(chunks),
            indexed_at=utc_now().isoformat(),
            index_descriptor=(
                knowledge_index.descriptor if knowledge_index is not None else None
            ),
        )
    )
    if knowledge_index is not None:
        metadata.pop("chunks", None)
    indexing = store.update(
        KnowledgeSource,
        source.id,
        {
            "source_type": extracted.source_type,
            "status": "indexing" if knowledge_index is not None else "ready",
            "citation": source.citation or source.name,
            "document_count": len(chunks),
            "metadata": metadata,
        },
        expected_revision=source.revision,
    )
    if knowledge_index is None:
        return indexing
    try:
        knowledge_index.upsert_source(indexing, chunks)
    except KnowledgeIndexError:
        current = store.get(KnowledgeSource, indexing.id)
        store.update(
            KnowledgeSource,
            indexing.id,
            {"status": "error"},
            expected_revision=current.revision,
        )
        raise
    current = store.get(KnowledgeSource, indexing.id)
    return store.update(
        KnowledgeSource,
        indexing.id,
        {"status": "ready"},
        expected_revision=current.revision,
    )


def knowledge_summary(source: KnowledgeSource) -> KnowledgeSource:
    """Return an API-safe source without its potentially large internal chunks."""

    metadata = {key: value for key, value in source.metadata.items() if key != "chunks"}
    return source.model_copy(update={"metadata": metadata})


def migrate_inline_knowledge_indexes(
    *, store: NebulaStore, knowledge_index: KnowledgeIndex
) -> int:
    """Move pre-Chroma inline chunks into the persistent vector index."""

    sources: list[KnowledgeSource] = []
    offset = 0
    while True:
        page = store.list_entities(KnowledgeSource, offset=offset, limit=1_000)
        sources.extend(page)
        if len(page) < 1_000:
            break
        offset += len(page)
    migrated = 0
    for source in sources:
        if source.status.casefold() != "ready":
            continue
        chunks = source.metadata.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            continue
        valid_chunks = [chunk for chunk in chunks if isinstance(chunk, dict)]
        if not valid_chunks:
            continue
        knowledge_index.upsert_source(source, valid_chunks)
        metadata = dict(source.metadata)
        metadata.pop("chunks", None)
        metadata.update(knowledge_index.descriptor)
        store.update(
            KnowledgeSource,
            source.id,
            {"metadata": metadata},
            expected_revision=source.revision,
        )
        migrated += 1
    return migrated


def _index_metadata(
    artifact: Artifact,
    *,
    chunks: list[dict[str, Any]] | None,
    chunk_count: int | None = None,
    indexed_at: str,
    index_descriptor: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "size": artifact.size,
        "sha256": artifact.sha256,
        "chunk_count": len(chunks or []) if chunk_count is None else chunk_count,
        "indexed_at": indexed_at,
        "ingestion_version": INGESTION_VERSION,
    }
    if chunks is not None:
        metadata["chunks"] = chunks
    if index_descriptor is not None:
        metadata.update(index_descriptor)
    return metadata


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


def _safe_archive_xml(
    archive: zipfile.ZipFile, name: str, *, required: bool = True
) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise InvalidDocumentError(f"spreadsheet is missing {name}")
        return None
    if info.file_size > MAX_EXTRACTED_CHARACTERS * 4:
        raise DocumentTooLargeError(
            f"spreadsheet part {name} exceeds the extraction limit"
        )
    payload = archive.read(info)
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", payload, flags=re.IGNORECASE):
        raise InvalidDocumentError("spreadsheet XML declarations are not supported")
    return payload


def _extract_xlsx(data: bytes) -> tuple[ExtractedSection, ...]:
    """Read cell text and formulas from XLSX XML without executing workbook content."""

    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            workbook_xml = _safe_archive_xml(archive, "xl/workbook.xml")
            relationships_xml = _safe_archive_xml(archive, "xl/_rels/workbook.xml.rels")
            shared_xml = _safe_archive_xml(
                archive, "xl/sharedStrings.xml", required=False
            )
            assert workbook_xml is not None and relationships_xml is not None
            workbook = ElementTree.fromstring(workbook_xml)
            relationships = ElementTree.fromstring(relationships_xml)
            targets = {
                relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
                for relation in relationships.findall(
                    f"{{{package_rel_ns}}}Relationship"
                )
                if relation.attrib.get("TargetMode") != "External"
            }
            shared: list[str] = []
            if shared_xml is not None:
                shared_root = ElementTree.fromstring(shared_xml)
                for item in shared_root.findall(f"{{{spreadsheet_ns}}}si"):
                    shared.append(
                        "".join(
                            node.text or ""
                            for node in item.iter(f"{{{spreadsheet_ns}}}t")
                        )
                    )

            sections: list[ExtractedSection] = []
            sheets = workbook.find(f"{{{spreadsheet_ns}}}sheets")
            if sheets is None:
                raise InvalidDocumentError("spreadsheet contains no worksheets")
            for sheet in sheets:
                title = sheet.attrib.get("name", "Sheet")
                state = sheet.attrib.get("state", "visible")
                relation_id = sheet.attrib.get(f"{{{office_rel_ns}}}id", "")
                target = targets.get(relation_id, "")
                if not target:
                    continue
                part = posixpath.normpath(target.lstrip("/"))
                if not part.startswith("xl/"):
                    part = posixpath.normpath(f"xl/{part}")
                if not part.startswith("xl/worksheets/"):
                    raise InvalidDocumentError("spreadsheet worksheet path is invalid")
                sheet_xml = _safe_archive_xml(archive, part)
                assert sheet_xml is not None
                root = ElementTree.fromstring(sheet_xml)
                for row in root.iter(f"{{{spreadsheet_ns}}}row"):
                    values: list[str] = []
                    for cell in row.findall(f"{{{spreadsheet_ns}}}c"):
                        formula = cell.find(f"{{{spreadsheet_ns}}}f")
                        if formula is not None and formula.text:
                            value = f"={formula.text}"
                        elif cell.attrib.get("t") == "inlineStr":
                            value = "".join(
                                node.text or ""
                                for node in cell.iter(f"{{{spreadsheet_ns}}}t")
                            )
                        else:
                            value_node = cell.find(f"{{{spreadsheet_ns}}}v")
                            value = (
                                value_node.text or "" if value_node is not None else ""
                            )
                            if cell.attrib.get("t") == "s" and value:
                                try:
                                    value = shared[int(value)]
                                except (ValueError, IndexError) as exc:
                                    raise InvalidDocumentError(
                                        "spreadsheet contains an invalid shared string reference"
                                    ) from exc
                        if value.strip():
                            reference = cell.attrib.get("r", "")
                            values.append(
                                f"{reference}: {value}" if reference else value
                            )
                    if values:
                        row_number = row.attrib.get("r", "?")
                        visibility = "" if state == "visible" else f" ({state})"
                        sections.append(
                            ExtractedSection(
                                text=" | ".join(values),
                                location=f"{title}{visibility}, row {row_number}",
                            )
                        )
    except (zipfile.BadZipFile, RuntimeError, ElementTree.ParseError) as exc:
        raise InvalidDocumentError("invalid or encrypted XLSX document") from exc
    if not sections:
        raise InvalidDocumentError("spreadsheet contains no extractable cells")
    return tuple(sections)


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
    "migrate_inline_knowledge_indexes",
    "reindex_document",
    "safe_filename",
]
