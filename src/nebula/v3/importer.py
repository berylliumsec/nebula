"""Transactional, read-only importer for Nebula 2.x engagement folders."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field

from .artifacts import ArtifactStore, StoredArtifact
from .domain import (
    Asset,
    Engagement,
    Evidence,
    KnowledgeSource,
    NebulaModel,
    Observation,
    ScopePolicy,
    utc_now,
)
from .storage import NebulaStore

_UUID_DIRECTORY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ImportReport(NebulaModel):
    import_id: str = Field(default_factory=lambda: str(uuid4()))
    source_path: str
    target_engagement_id: str | None = None
    status: str = "running"
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    source_manifest_sha256: str | None = None
    source_file_checksums: dict[str, str] = Field(default_factory=dict)
    imported_counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    source_unchanged: bool = False


class LegacyImportError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest(root: Path) -> tuple[dict[str, str], list[str]]:
    checksums: dict[str, str] = {}
    skipped: list[str] = []
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        retained_directories = []
        for name in sorted(directories):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                checksums[f"{relative}@symlink"] = hashlib.sha256(
                    f"symlink:{target}".encode("utf-8", errors="surrogateescape")
                ).hexdigest()
                skipped.append(relative)
            else:
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in sorted(filenames):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                checksums[f"{relative}@symlink"] = hashlib.sha256(
                    f"symlink:{target}".encode("utf-8", errors="surrogateescape")
                ).hexdigest()
                skipped.append(relative)
            elif path.is_file():
                checksums[relative] = _sha256_file(path)
    return dict(sorted(checksums.items())), sorted(set(skipped))


def _manifest_digest(checksums: dict[str, str]) -> str:
    canonical = json.dumps(checksums, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _load_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise LegacyImportError(f"required file is missing: {path.name}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        record_caught_exception(
            "storage",
            "storage.importer.caught_failure_001",
            "A handled storage operation raised an exception.",
            exc,
            stage="importer",
        )
        raise LegacyImportError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise LegacyImportError(f"{path.name} must contain a JSON object")
    return value


def _safe_text(path: Path, limit: int = 2 * 1024 * 1024) -> tuple[str, bool]:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    truncated = len(data) > limit
    return data[:limit].decode("utf-8", errors="replace"), truncated


def _targets(details: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    cidrs: set[str] = set()
    domains: set[str] = set()
    urls: set[str] = set()

    raw_targets = details.get("ip_addresses", [])
    if not isinstance(raw_targets, list):
        raw_targets = []
    for raw in raw_targets:
        value = str(raw).strip()
        if not value:
            continue
        try:
            cidrs.add(str(ipaddress.ip_network(value, strict=False)))
        except ValueError as caught_error:
            record_caught_exception(
                "storage",
                "storage.importer.caught_failure_002",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="importer",
            )
            domains.add(value.lower().rstrip("."))

    raw_urls = details.get("urls", [])
    if not isinstance(raw_urls, list):
        raw_urls = []
    for raw in raw_urls:
        value = str(raw).strip()
        if not value:
            continue
        urls.add(value)
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        if not parsed.hostname:
            continue
        try:
            address = ipaddress.ip_address(parsed.hostname)
            cidrs.add(str(ipaddress.ip_network(address.exploded, strict=False)))
        except ValueError as caught_error:
            record_caught_exception(
                "storage",
                "storage.importer.caught_failure_003",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="importer",
            )
            domains.add(parsed.hostname.lower().rstrip("."))
    return sorted(cidrs), sorted(domains), sorted(urls)


def _iter_regular_files(root: Path) -> Iterable[Path]:
    if not root.exists() or not root.is_dir():
        return
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        directories[:] = sorted(
            name for name in directories if not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            path = current / name
            if path.is_file() and not path.is_symlink():
                yield path


def _chroma_files(chroma_root: Path, engagement_root: Path) -> Iterable[Path]:
    """Select raw Chroma files without misclassifying a whole engagement as Chroma."""

    if chroma_root == engagement_root:
        sqlite_path = chroma_root / "chroma.sqlite3"
        if sqlite_path.is_file():
            yield sqlite_path
        for child in sorted(chroma_root.iterdir()):
            if child.is_dir() and _UUID_DIRECTORY.fullmatch(child.name):
                yield from _iter_regular_files(child)
        return
    yield from _iter_regular_files(chroma_root)


def _read_chroma_documents(database_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Read legacy Chroma document metadata through an immutable SQLite handle."""

    if not database_path.is_file():
        return []
    uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "embedding_metadata" not in tables:
            return []
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(embedding_metadata)")
        }
        if not {"id", "key"}.issubset(columns):
            return []
        value_columns = [
            name
            for name in ("string_value", "int_value", "float_value", "bool_value")
            if name in columns
        ]
        if not value_columns:
            return []
        query = f"SELECT id, key, {', '.join(value_columns)} FROM embedding_metadata ORDER BY id, key"
        grouped: dict[Any, dict[str, Any]] = defaultdict(dict)
        for row in connection.execute(query):
            identifier, key, *values = row
            value = next((item for item in values if item is not None), None)
            grouped[identifier][key] = value
        documents = []
        for values in grouped.values():
            document = values.pop("chroma:document", None)
            if isinstance(document, str):
                documents.append((document, dict(values)))
        return documents
    finally:
        connection.close()


class LegacyEngagementImporter:
    """Import a 2.x folder as new v3 records while proving source immutability."""

    def __init__(
        self,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        *,
        allow_external_chroma: bool = False,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.allow_external_chroma = allow_external_chroma

    def import_engagement(self, source: str | Path) -> ImportReport:
        source_path = Path(source).expanduser().resolve()
        report = ImportReport(source_path=str(source_path))
        stored_blobs: list[StoredArtifact] = []
        try:
            self._validate_boundaries(source_path)
            before, symlinks = _source_manifest(source_path)
            report.source_file_checksums = before
            report.source_manifest_sha256 = _manifest_digest(before)
            report.skipped.extend(symlinks)
            if symlinks:
                report.warnings.append("symbolic links were recorded but not imported")

            details = _load_json(source_path / "engagement_details.json", required=True)
            config = _load_json(source_path / "config.json")
            engagement_id = str(uuid4())
            scope_id = str(uuid4())
            report.target_engagement_id = engagement_id
            cidrs, domains, urls = _targets(details)
            selected_tools = config.get("SELECTED_TOOLS", [])
            if not isinstance(selected_tools, list):
                selected_tools = []

            scope = ScopePolicy(
                id=scope_id,
                engagement_id=engagement_id,
                allowed_cidrs=cidrs,
                allowed_domains=domains,
                allowed_urls=urls,
            )
            engagement = Engagement(
                id=engagement_id,
                name=str(
                    details.get("engagement_name") or source_path.name or "Imported"
                ),
                description="Imported read-only from a Nebula 2.x engagement.",
                scope_policy_id=scope_id,
                metadata={
                    "legacy": {
                        "source_path": str(source_path),
                        "source_manifest_sha256": report.source_manifest_sha256,
                        "model": details.get("model"),
                        "ollama_url": details.get("ollama_url"),
                        "lookout_items": details.get("lookout_items", []),
                        "selected_tools": [str(tool) for tool in selected_tools],
                    }
                },
            )
            entities: list[Any] = [engagement, scope]
            counts: Counter[str] = Counter(
                {
                    "engagements": 1,
                    "scope_policies": 1,
                    "tool_selections": len(selected_tools),
                }
            )

            for cidr in cidrs:
                entities.append(
                    Asset(
                        engagement_id=engagement_id,
                        asset_type="network"
                        if "/32" not in cidr and "/128" not in cidr
                        else "host",
                        name=cidr,
                        address=cidr,
                        metadata={"legacy_import": True},
                    )
                )
                counts["assets"] += 1
            for domain in domains:
                entities.append(
                    Asset(
                        engagement_id=engagement_id,
                        asset_type="domain",
                        name=domain,
                        hostname=domain,
                        metadata={"legacy_import": True},
                    )
                )
                counts["assets"] += 1

            captured_paths: set[Path] = set()
            for filename in ("engagement_details.json", "config.json"):
                path = source_path / filename
                if path.is_file():
                    self._capture(
                        path,
                        "settings",
                        source_path,
                        engagement_id,
                        entities,
                        stored_blobs,
                        counts,
                    )
                    captured_paths.add(path.resolve())

            history = source_path / "history.txt"
            if history.is_file():
                artifact = self._capture(
                    history,
                    "command_history",
                    source_path,
                    engagement_id,
                    entities,
                    stored_blobs,
                    counts,
                    evidence=True,
                )
                text, truncated = _safe_text(history)
                entities.append(
                    Observation(
                        engagement_id=engagement_id,
                        observation_type="legacy_command_history",
                        title="Nebula 2.x command history",
                        body=text,
                        evidence_ids=[],
                        source=artifact.id,
                        metadata={"truncated_preview": truncated},
                    )
                )
                counts["observations"] += 1
                captured_paths.add(history.resolve())

            categories = {
                "command_output": ("tool_output", True),
                "screenshots": ("screenshot", True),
                "suggestions_notes": ("note", True),
            }
            for directory, (category, make_evidence) in categories.items():
                for path in _iter_regular_files(source_path / directory):
                    artifact = self._capture(
                        path,
                        category,
                        source_path,
                        engagement_id,
                        entities,
                        stored_blobs,
                        counts,
                        evidence=make_evidence,
                    )
                    captured_paths.add(path.resolve())
                    if category == "note":
                        text, truncated = _safe_text(path)
                        entities.append(
                            Observation(
                                engagement_id=engagement_id,
                                observation_type="legacy_note",
                                title=path.stem,
                                body=text,
                                source=artifact.id,
                                metadata={"truncated_preview": truncated},
                            )
                        )
                        counts["observations"] += 1

            chroma_value = details.get("chromadb_dir") or config.get("CHROMA_DB_PATH")
            if chroma_value:
                chroma_root = Path(str(chroma_value)).expanduser()
                if not chroma_root.is_absolute():
                    chroma_root = source_path / chroma_root
                chroma_root = chroma_root.resolve()
                inside_source = (
                    chroma_root == source_path or source_path in chroma_root.parents
                )
                if not inside_source and not self.allow_external_chroma:
                    report.warnings.append(
                        "external Chroma path was not imported without explicit approval"
                    )
                    chroma_root = source_path / ".nebula-external-chroma-disabled"
                if chroma_root.is_dir():
                    chroma_artifact_ids = []
                    for path in _chroma_files(chroma_root, source_path):
                        if path.resolve() in captured_paths:
                            continue
                        artifact = self._capture(
                            path,
                            "chroma_snapshot",
                            chroma_root,
                            engagement_id,
                            entities,
                            stored_blobs,
                            counts,
                        )
                        chroma_artifact_ids.append(artifact.id)
                        captured_paths.add(path.resolve())
                    sqlite_path = chroma_root / "chroma.sqlite3"
                    documents = _read_chroma_documents(sqlite_path)
                    for index, (document, metadata) in enumerate(documents, start=1):
                        entities.append(
                            Observation(
                                engagement_id=engagement_id,
                                observation_type="legacy_knowledge_document",
                                title=f"Imported Chroma document {index}",
                                body=document,
                                source="chroma.sqlite3",
                                metadata={"chroma_metadata": metadata},
                            )
                        )
                    counts["chroma_documents"] += len(documents)
                    entities.append(
                        KnowledgeSource(
                            engagement_id=engagement_id,
                            name="Nebula 2.x Chroma snapshot",
                            source_type="legacy_chroma",
                            artifact_id=chroma_artifact_ids[0]
                            if chroma_artifact_ids
                            else None,
                            document_count=len(documents),
                            metadata={"artifact_ids": chroma_artifact_ids},
                        )
                    )
                    counts["knowledge"] += 1
                else:
                    report.warnings.append(
                        f"configured Chroma directory was not found: {chroma_root}"
                    )

            after, _ = _source_manifest(source_path)
            report.source_unchanged = before == after
            if not report.source_unchanged:
                raise LegacyImportError(
                    "source changed during import; no database records were committed"
                )

            with self.store.transaction() as transaction:
                transaction.add_all(entities)
            report.imported_counts = dict(sorted(counts.items()))
            report.status = "completed"
        except Exception as exc:
            record_caught_exception(
                "storage",
                "storage.importer.caught_failure_004",
                "A handled storage operation raised an exception.",
                exc,
                stage="importer",
            )
            for stored in reversed(stored_blobs):
                try:
                    self.artifact_store.discard_new_blob(stored)
                except Exception as cleanup_error:
                    record_caught_exception(
                        "storage",
                        "storage.importer.caught_failure_005",
                        "A handled storage operation raised an exception.",
                        cleanup_error,
                        stage="importer",
                    )
                    report.warnings.append(f"artifact cleanup failed: {cleanup_error}")
            report.status = "failed"
            report.errors.append(str(exc))
            if source_path.is_dir():
                try:
                    current, _ = _source_manifest(source_path)
                    if report.source_file_checksums:
                        report.source_unchanged = (
                            current == report.source_file_checksums
                        )
                except Exception as verify_error:
                    record_caught_exception(
                        "storage",
                        "storage.importer.caught_failure_006",
                        "A handled storage operation raised an exception.",
                        verify_error,
                        stage="importer",
                    )
                    report.warnings.append(
                        f"could not verify source after failure: {verify_error}"
                    )
        report.completed_at = utc_now()
        return report

    def _validate_boundaries(self, source: Path) -> None:
        if not source.is_dir():
            raise LegacyImportError(
                f"source engagement directory does not exist: {source}"
            )
        if (
            self.artifact_store.root == source
            or source in self.artifact_store.root.parents
        ):
            raise LegacyImportError(
                "artifact destination must be outside the source engagement"
            )
        database_path = self.store.database.engine.url.database
        if database_path and database_path != ":memory:":
            destination = Path(database_path).expanduser().resolve()
            if destination == source or source in destination.parents:
                raise LegacyImportError(
                    "database destination must be outside the source engagement"
                )

    def _capture(
        self,
        path: Path,
        category: str,
        relative_root: Path,
        engagement_id: str,
        entities: list[Any],
        stored_blobs: list[StoredArtifact],
        counts: Counter[str],
        *,
        evidence: bool = False,
    ) -> Any:
        relative = path.relative_to(relative_root).as_posix()
        stored = self.artifact_store.put_file_with_status(
            path,
            engagement_id=engagement_id,
            source=f"nebula-2x:{relative}",
            metadata={
                "legacy_category": category,
                "legacy_relative_path": relative,
            },
        )
        stored_blobs.append(stored)
        artifact = stored.artifact
        entities.append(artifact)
        counts["artifacts"] += 1
        counts[f"artifact_{category}"] += 1
        if evidence:
            entities.append(
                Evidence(
                    engagement_id=engagement_id,
                    evidence_type=category,
                    title=path.name,
                    artifact_id=artifact.id,
                    sha256=artifact.sha256,
                    source_version="nebula-2.x",
                    metadata={"legacy_relative_path": relative},
                )
            )
            counts["evidence"] += 1
        return artifact


def import_2x_engagement(
    source: str | Path,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    *,
    allow_external_chroma: bool = False,
) -> ImportReport:
    """Functional entry point used by ``nebula import-2x``."""

    return LegacyEngagementImporter(
        store,
        artifact_store,
        allow_external_chroma=allow_external_chroma,
    ).import_engagement(source)
