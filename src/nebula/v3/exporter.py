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
from .domain import (
    ENTITY_MODELS,
    Advisory,
    AgentAttempt,
    AgentRun,
    Approval,
    Artifact,
    ChatMessage,
    ChatSession,
    ContextSnapshot,
    Correlation,
    Engagement,
    Entity,
    Evidence,
    Finding,
    GeneratedDraft,
    KnowledgeSource,
    OperatorExecution,
    OperatorProfile,
    ProviderProfile,
    Report,
    ReportRender,
    SourceSnapshot,
    utc_now,
)
from .storage import NebulaStore, NotFoundError
from .terminal_history import TerminalCommandHistory


class ExportError(RuntimeError):
    pass


class ExportManifest(BaseModel):
    format: str = "nebula-engagement-bundle"
    format_version: int = 3
    engagement_id: str
    exported_at: str
    entity_counts: dict[str, int] = Field(default_factory=dict)
    event_count: int = 0
    run_event_count: int = 0
    operation_event_count: int = 0
    terminal_command_count: int = 0
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
    engagement_id: str | None = None,
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


def _add_entity(
    entities: dict[type[Entity], dict[str, Entity]], entity: Entity
) -> None:
    entities.setdefault(type(entity), {})[entity.id] = entity


def _get_reference(
    store: NebulaStore,
    model: type[Entity],
    entity_id: str,
    *,
    source: str,
) -> Entity:
    try:
        return store.get(model, entity_id)
    except NotFoundError as exc:
        raise ExportError(
            f"{source} references missing {model.entity_kind} entity: {entity_id}"
        ) from exc


def _include_referenced_globals(
    *,
    store: NebulaStore,
    entities: dict[type[Entity], dict[str, Entity]],
    events: list[Any],
) -> None:
    """Close explicit cross-engagement references needed to interpret a bundle."""

    provider_ids: set[str] = set()
    required_operator_ids: set[str] = set()
    possible_operator_ids: set[str] = set()
    advisory_ids: set[str] = set()
    artifact_ids: set[str] = set()

    for model_entities in entities.values():
        for entity in model_entities.values():
            if isinstance(entity, Engagement) and entity.owner_id:
                required_operator_ids.add(entity.owner_id)
            elif isinstance(entity, Evidence):
                if entity.captured_by:
                    possible_operator_ids.add(entity.captured_by)
                if entity.artifact_id:
                    artifact_ids.add(entity.artifact_id)
            elif isinstance(entity, Finding) and entity.verifier_id:
                required_operator_ids.add(entity.verifier_id)
            elif isinstance(entity, Correlation):
                advisory_ids.add(entity.advisory_id)
                if entity.analyst_id:
                    required_operator_ids.add(entity.analyst_id)
            elif isinstance(entity, Report):
                if entity.signed_off_by:
                    required_operator_ids.add(entity.signed_off_by)
                artifact_ids.update(entity.artifact_ids)
            elif isinstance(entity, Approval):
                possible_operator_ids.add(entity.requested_by)
                if entity.decided_by:
                    possible_operator_ids.add(entity.decided_by)
            elif isinstance(entity, AgentRun) and entity.supervisor_provider_id:
                provider_ids.add(entity.supervisor_provider_id)
            elif isinstance(entity, AgentAttempt) and entity.provider_profile_id:
                provider_ids.add(entity.provider_profile_id)
            elif isinstance(entity, OperatorExecution):
                possible_operator_ids.add(entity.operator_id)
                artifact_ids.add(entity.source_artifact_id)
                artifact_ids.update(
                    artifact_id
                    for artifact_id in (
                        entity.stdout_artifact_id,
                        entity.stderr_artifact_id,
                        entity.redacted_stdout_artifact_id,
                        entity.redacted_stderr_artifact_id,
                        entity.manifest_artifact_id,
                    )
                    if artifact_id
                )
            elif isinstance(entity, GeneratedDraft):
                provider_ids.add(entity.provider_profile_id)
            elif isinstance(entity, ReportRender):
                artifact_ids.update(
                    artifact_id
                    for artifact_id in (
                        entity.snapshot_artifact_id,
                        entity.pdf_artifact_id,
                    )
                    if artifact_id
                )
            elif isinstance(entity, ChatSession):
                provider_ids.add(entity.provider_profile_id)
            elif isinstance(entity, ContextSnapshot):
                provider_ids.add(entity.provider_profile_id)
            elif isinstance(entity, ChatMessage):
                if entity.provider_profile_id:
                    provider_ids.add(entity.provider_profile_id)
                artifact_ids.update(
                    citation.artifact_id
                    for citation in entity.citations
                    if citation.artifact_id
                )
            elif isinstance(entity, KnowledgeSource) and entity.artifact_id:
                artifact_ids.add(entity.artifact_id)
            if isinstance(entity, Artifact) and entity.parent_artifact_id:
                artifact_ids.add(entity.parent_artifact_id)

    possible_operator_ids.update(
        event.actor_id for event in events if event.actor_id is not None
    )
    for provider_id in sorted(provider_ids):
        _add_entity(
            entities,
            _get_reference(
                store,
                ProviderProfile,
                provider_id,
                source="engagement data",
            ),
        )
    for operator_id in sorted(required_operator_ids):
        _add_entity(
            entities,
            _get_reference(
                store,
                OperatorProfile,
                operator_id,
                source="engagement data",
            ),
        )
    # Approval requesters and event actors may intentionally be agent/system names.
    # Include them only when they resolve to an actual operator profile.
    for operator_id in sorted(possible_operator_ids - required_operator_ids):
        try:
            operator = store.get(OperatorProfile, operator_id)
        except NotFoundError:
            continue
        _add_entity(entities, operator)

    if advisory_ids:
        for advisory in _all_entities(store, Advisory):
            if isinstance(advisory, Advisory) and advisory.advisory_id in advisory_ids:
                _add_entity(entities, advisory)

    for advisory in entities.get(Advisory, {}).values():
        if isinstance(advisory, Advisory) and advisory.source_snapshot_id:
            snapshot = _get_reference(
                store,
                SourceSnapshot,
                advisory.source_snapshot_id,
                source=f"advisory {advisory.id}",
            )
            _add_entity(entities, snapshot)
            if isinstance(snapshot, SourceSnapshot) and snapshot.artifact_id:
                artifact_ids.add(snapshot.artifact_id)

    included_artifacts = entities.setdefault(Artifact, {})
    pending_artifact_ids = artifact_ids - included_artifacts.keys()
    while pending_artifact_ids:
        artifact_id = min(pending_artifact_ids)
        pending_artifact_ids.remove(artifact_id)
        artifact = _get_reference(
            store,
            Artifact,
            artifact_id,
            source="engagement data",
        )
        _add_entity(entities, artifact)
        if (
            isinstance(artifact, Artifact)
            and artifact.parent_artifact_id
            and artifact.parent_artifact_id not in included_artifacts
        ):
            pending_artifact_ids.add(artifact.parent_artifact_id)


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
    entities_by_model: dict[type[Entity], dict[str, Entity]] = {}
    for model in ENTITY_MODELS:
        entities = _all_entities(store, model, engagement_id)
        if not entities:
            continue
        entities_by_model[model] = {entity.id: entity for entity in entities}

    run_events = []
    for run in entities_by_model.get(AgentRun, {}).values():
        if not isinstance(run, AgentRun):
            continue
        cursor = 0
        while True:
            run_page = store.replay_events(run.id, after_sequence=cursor, limit=10_000)
            run_events.extend(run_page)
            if not run_page or len(run_page) < 10_000:
                break
            cursor = run_page[-1].sequence

    operation_events = []
    offset = 0
    while True:
        operation_page = store.list_operation_events(
            engagement_id, offset=offset, limit=10_000
        )
        operation_events.extend(operation_page)
        if len(operation_page) < 10_000:
            break
        offset += len(operation_page)

    terminal_records, terminal_artifact_ids = TerminalCommandHistory(
        store.database,
        store=store,
        artifact_store=artifact_store,
    ).export_payload(engagement_id)
    for artifact_id in sorted(terminal_artifact_ids):
        _add_entity(
            entities_by_model,
            _get_reference(
                store,
                Artifact,
                artifact_id,
                source="terminal audit record",
            ),
        )

    _include_referenced_globals(
        store=store,
        entities=entities_by_model,
        events=[*run_events, *operation_events],
    )
    for model in ENTITY_MODELS:
        model_entities = entities_by_model.get(model)
        if not model_entities:
            continue
        entities = sorted(
            model_entities.values(), key=lambda entity: (entity.created_at, entity.id)
        )
        counts[model.entity_kind] = len(entities)
        payloads[f"entities/{model.entity_kind}.json"] = _json_bytes(
            [entity.model_dump(mode="json") for entity in entities]
        )
    artifacts = [
        artifact
        for artifact in entities_by_model.get(Artifact, {}).values()
        if isinstance(artifact, Artifact)
    ]
    payloads["events.json"] = _json_bytes(
        [event.model_dump(mode="json") for event in run_events]
    )
    payloads["operation_events.json"] = _json_bytes(
        [event.model_dump(mode="json") for event in operation_events]
    )
    payloads["terminal_commands.json"] = _json_bytes(terminal_records)

    manifest = ExportManifest(
        engagement_id=engagement_id,
        exported_at=utc_now().isoformat(),
        entity_counts=counts,
        event_count=len(run_events) + len(operation_events),
        run_event_count=len(run_events),
        operation_event_count=len(operation_events),
        terminal_command_count=len(terminal_records),
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
