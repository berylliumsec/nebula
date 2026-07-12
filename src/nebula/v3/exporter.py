"""Portable, integrity-manifested Nebula 3 engagement exports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .artifacts import ArtifactStore
from .domain import ENTITY_MODELS, AgentRun, Artifact, Engagement, Entity, utc_now
from .storage import NebulaStore


class ExportError(RuntimeError):
    pass


class ExportManifest(BaseModel):
    format: str = "nebula-engagement-bundle"
    format_version: int = 1
    engagement_id: str
    exported_at: str
    entity_counts: dict[str, int] = Field(default_factory=dict)
    event_count: int = 0
    files: dict[str, str] = Field(default_factory=dict)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            separators=(",", ": "),
        ).encode("utf-8")
        + b"\n"
    )


def _all_entities(
    store: NebulaStore,
    model: type[Entity],
    engagement_id: str,
) -> list[Entity]:
    result: list[Entity] = []
    offset = 0
    while True:
        page = store.list_entities(
            model,
            engagement_id=engagement_id,
            offset=offset,
            limit=1000,
        )
        result.extend(page)
        if len(page) < 1000:
            return result
        offset += len(page)


def export_engagement(
    *,
    engagement_id: str,
    destination: str | Path,
    store: NebulaStore,
    artifact_store: ArtifactStore,
    overwrite: bool = False,
) -> ExportManifest:
    """Atomically create a ZIP bundle with per-file SHA-256 verification data."""

    store.get(Engagement, engagement_id)
    destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists() and not overwrite:
        raise ExportError(f"destination already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    payloads: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    artifacts: list[Artifact] = []
    runs: list[AgentRun] = []
    for model in ENTITY_MODELS:
        entities = _all_entities(store, model, engagement_id)
        if not entities:
            continue
        counts[model.entity_kind] = len(entities)
        payloads[f"entities/{model.entity_kind}.json"] = _json_bytes(
            [entity.model_dump(mode="json") for entity in entities]
        )
        if model is Artifact:
            artifacts = [entity for entity in entities if isinstance(entity, Artifact)]
        if model is AgentRun:
            runs = [entity for entity in entities if isinstance(entity, AgentRun)]

    events = []
    for run in runs:
        cursor = 0
        while True:
            page = store.replay_events(run.id, after_sequence=cursor, limit=10_000)
            events.extend(page)
            if not page or len(page) < 10_000:
                break
            cursor = page[-1].sequence
    payloads["events.json"] = _json_bytes(
        [event.model_dump(mode="json") for event in events]
    )

    manifest = ExportManifest(
        engagement_id=engagement_id,
        exported_at=utc_now().isoformat(),
        entity_counts=counts,
        event_count=len(events),
        files={
            name: hashlib.sha256(content).hexdigest()
            for name, content in payloads.items()
        },
    )
    for artifact in artifacts:
        if not artifact_store.verify(artifact):
            raise ExportError(f"artifact failed integrity verification: {artifact.id}")
        manifest.files[f"blobs/sha256/{artifact.sha256}"] = artifact.sha256

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for name, content in sorted(payloads.items()):
                archive.writestr(name, content)
            written: set[str] = set()
            for artifact in artifacts:
                name = f"blobs/sha256/{artifact.sha256}"
                if name in written:
                    continue
                archive.write(artifact_store.path_for(artifact), name)
                written.add(name)
            archive.writestr(
                "manifest.json", _json_bytes(manifest.model_dump(mode="json"))
            )
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return manifest


__all__ = ["ExportError", "ExportManifest", "export_engagement"]
