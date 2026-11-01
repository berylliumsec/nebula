"""Content-addressed path maps and indexed ownership for native hook receipts.

Path maps are split by a stable path hash. A one-path change writes one small
chunk and one manifest, while identical chunks remain shared across turns.
The ownership index answers attribution by path and fingerprint without reading
historical observation payloads.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import distinct, or_, select, tuple_
from sqlalchemy.orm import Session

from .database import (
    WorkspaceProvenanceBlobRow,
    WorkspaceProvenanceIndexRow,
    WorkspaceProvenanceOwnerRow,
)
from .domain import WorkspaceProvenanceObservation
from .storage import ConflictError, CorruptRecordError


_OWNER_QUERY_BATCH = 200


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def workspace_key(engagement_id: str, root: str) -> str:
    return hashlib.sha256(_canonical([engagement_id, root])).hexdigest()


def path_key(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()


def state_key(state: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(state)).hexdigest()


def _blob_id(workspace: str, kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        workspace.encode("ascii")
        + b":"
        + kind.encode("ascii")
        + b":"
        + _canonical(payload)
    ).hexdigest()


def save_map(
    session: Session,
    engagement_id: str,
    workspace: str,
    kind: str,
    values: dict[str, Any],
) -> str:
    """Persist a map with stable chunks; return its immutable manifest id."""

    buckets: dict[str, dict[str, Any]] = {}
    for path, value in values.items():
        buckets.setdefault(path_key(path)[:2], {})[path] = value
    candidates: dict[str, WorkspaceProvenanceBlobRow] = {}
    manifest: dict[str, str] = {}
    for prefix, payload in sorted(buckets.items()):
        blob_kind = f"{kind}:chunk"
        digest = _blob_id(workspace, blob_kind, payload)
        manifest[prefix] = digest
        candidates[digest] = WorkspaceProvenanceBlobRow(
            id=digest, engagement_id=engagement_id, kind=blob_kind, payload=payload
        )
    manifest_kind = f"{kind}:manifest"
    root_id = _blob_id(workspace, manifest_kind, manifest)
    candidates[root_id] = WorkspaceProvenanceBlobRow(
        id=root_id,
        engagement_id=engagement_id,
        kind=manifest_kind,
        payload=manifest,
    )
    existing = set(
        session.scalars(
            select(WorkspaceProvenanceBlobRow.id).where(
                WorkspaceProvenanceBlobRow.id.in_(list(candidates))
            )
        )
    )
    session.add_all(row for digest, row in candidates.items() if digest not in existing)
    session.flush()
    return root_id


def load_map(
    session: Session, workspace: str, kind: str, root_id: str | None
) -> dict[str, Any]:
    if root_id is None:
        return {}
    manifest = session.get(WorkspaceProvenanceBlobRow, root_id)
    manifest_kind = f"{kind}:manifest"
    if (
        manifest is None
        or manifest.kind != manifest_kind
        or _blob_id(workspace, manifest_kind, manifest.payload) != root_id
    ):
        raise CorruptRecordError(f"invalid workspace provenance {kind} manifest")
    if not manifest.payload:
        return {}
    rows = session.scalars(
        select(WorkspaceProvenanceBlobRow).where(
            WorkspaceProvenanceBlobRow.id.in_(list(manifest.payload.values()))
        )
    )
    by_id = {row.id: row for row in rows}
    values: dict[str, Any] = {}
    for prefix, digest in manifest.payload.items():
        row = by_id.get(digest)
        chunk_kind = f"{kind}:chunk"
        if (
            row is None
            or row.kind != chunk_kind
            or _blob_id(workspace, chunk_kind, row.payload) != digest
            or any(path_key(path)[:2] != prefix for path in row.payload)
        ):
            raise CorruptRecordError(f"invalid workspace provenance {kind} chunk")
        values.update(row.payload)
    return values


def overlapping_scopes(
    session: Session,
    observation: WorkspaceProvenanceObservation,
    completed_at: datetime,
) -> list[str]:
    workspace = workspace_key(observation.engagement_id, observation.workspace_root)
    statement = (
        select(distinct(WorkspaceProvenanceIndexRow.scope_id))
        .where(
            WorkspaceProvenanceIndexRow.workspace_key == workspace,
            WorkspaceProvenanceIndexRow.observation_id != observation.id,
            WorkspaceProvenanceIndexRow.actor_id != observation.actor_id,
            WorkspaceProvenanceIndexRow.started_at <= completed_at,
            or_(
                WorkspaceProvenanceIndexRow.completed_at.is_(None),
                WorkspaceProvenanceIndexRow.completed_at >= observation.started_at,
            ),
        )
        .order_by(WorkspaceProvenanceIndexRow.scope_id)
        .limit(128)
    )
    return list(session.scalars(statement))


def matching_owners(
    session: Session, workspace: str, current: dict[str, dict[str, Any]]
) -> dict[tuple[str, str], WorkspaceProvenanceOwnerRow]:
    pairs = [(path_key(path), state_key(state)) for path, state in current.items()]
    owners: dict[tuple[str, str], WorkspaceProvenanceOwnerRow] = {}
    for offset in range(0, len(pairs), _OWNER_QUERY_BATCH):
        batch = pairs[offset : offset + _OWNER_QUERY_BATCH]
        statement = select(WorkspaceProvenanceOwnerRow).where(
            WorkspaceProvenanceOwnerRow.workspace_key == workspace,
            tuple_(
                WorkspaceProvenanceOwnerRow.path_key,
                WorkspaceProvenanceOwnerRow.state_key,
            ).in_(batch),
        )
        for row in session.scalars(statement):
            owners[(row.path_key, row.state_key)] = row
    return owners


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def remember_mutations(
    session: Session,
    workspace: str,
    observation: WorkspaceProvenanceObservation,
    mutations: list[dict[str, Any]],
    completed_at: datetime,
    owners: dict[tuple[str, str], WorkspaceProvenanceOwnerRow],
) -> None:
    for mutation in mutations:
        after = mutation["after"]
        if after is None:
            continue
        path = mutation["path"]
        key = (path_key(path), state_key(after))
        row = owners.get(key)
        if row is not None and row.path != path:
            raise ConflictError("workspace provenance path hash collision")
        if row is None:
            row = WorkspaceProvenanceOwnerRow(
                engagement_id=observation.engagement_id,
                workspace_key=workspace,
                path_key=key[0],
                state_key=key[1],
                path=path,
                actor_id=observation.actor_id,
                confidence=mutation["confidence"],
                observation_id=observation.id,
                completed_at=completed_at,
            )
            session.add(row)
            owners[key] = row
        elif (completed_at, observation.id) > (
            _utc(row.completed_at),
            row.observation_id,
        ):
            row.actor_id = observation.actor_id
            row.confidence = mutation["confidence"]
            row.observation_id = observation.id
            row.completed_at = completed_at
