"""Write-once values split out of chat records Core rewrites on every step.

Storage rewrites a whole entity on each update. A provider turn is updated
several times per routing step and its goal once per model call, yet the
largest parts of both are fixed when they are written: the turn's request
snapshot (model request, MCP catalogs, host and skill snapshots) and the
goal's attached skill instructions. Those values are kept in
``ChatSnapshotPart`` records instead, one per distinct value in a
conversation, and the owning row keeps a reference to each.

Readers that need a split-out value resolve it here. Rows written before the
split keep their values inline and resolve unchanged, and a value written
inline after the split takes precedence over its part.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .domain import ChatSnapshotPart
from .storage import CorruptRecordError, NebulaStore, NotFoundError, StoreTransaction

SNAPSHOT_PARTS_KEY = "snapshot_parts"
"""Request-snapshot entry mapping each split-out key to its part id."""

REQUEST_SNAPSHOT_PART_KEYS = (
    "model_request",
    "mcp_snapshot",
    "mcp_catalog_snapshot",
    "ssh_environment_snapshot",
    "skill_snapshots",
    "hook_snapshots",
    "citations",
)
"""Write-once request-snapshot keys that only turn resume reads back."""

REASONING_PARTS_KEY = "reasoning_parts"
"""Request-snapshot entry listing a turn's sealed reasoning, oldest first.

Each entry is ``{"part_id": ..., "chars": ...}``; the turn row keeps only the
thinking written since the last seal.
"""

SKILL_PART_KEY = "part_id"
"""Key of the part holding a goal skill snapshot's full content."""

SKILL_PART_FIELDS = ("instructions",)
"""Skill snapshot fields a goal keeps only in the part: the bulk of each."""

MIN_PART_BYTES = 1024
"""Smaller values stay inline: a lookup would cost more than it saves."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def snapshot_part_id(session_id: str, sha256: str) -> str:
    """One conversation's id for a value, so repeated values share a record."""

    return str(uuid5(NAMESPACE_URL, f"nebula:chat-snapshot-part:{session_id}:{sha256}"))


def _part(encoded: bytes, *, engagement_id: str, session_id: str) -> ChatSnapshotPart:
    digest = hashlib.sha256(encoded).hexdigest()
    return ChatSnapshotPart(
        id=snapshot_part_id(session_id, digest),
        engagement_id=engagement_id,
        session_id=session_id,
        sha256=digest,
        value=json.loads(encoded),
    )


def snapshot_part(
    value: Any, *, engagement_id: str, session_id: str
) -> ChatSnapshotPart:
    """The part holding ``value`` in one conversation."""

    return _part(_canonical(value), engagement_id=engagement_id, session_id=session_id)


def split_request_snapshot(
    snapshot: Mapping[str, Any], *, engagement_id: str, session_id: str
) -> tuple[dict[str, Any], list[ChatSnapshotPart]]:
    """Return ``snapshot`` with its large write-once values replaced by part ids.

    The parts must be stored with (or before) the row that references them.
    """

    compact = dict(snapshot)
    references = dict(compact.get(SNAPSHOT_PARTS_KEY) or {})
    parts: list[ChatSnapshotPart] = []
    for key in REQUEST_SNAPSHOT_PART_KEYS:
        if key not in compact:
            continue
        encoded = _canonical(compact[key])
        if len(encoded) < MIN_PART_BYTES:
            continue
        part = _part(encoded, engagement_id=engagement_id, session_id=session_id)
        parts.append(part)
        references[key] = part.id
        del compact[key]
    if references:
        compact[SNAPSHOT_PARTS_KEY] = references
    return compact, parts


def load_snapshot_part(store: NebulaStore, part_id: str) -> Any:
    """The verified value of one part; a missing or altered part is corruption."""

    try:
        part = store.get(ChatSnapshotPart, part_id)
    except NotFoundError as exc:
        raise CorruptRecordError(f"chat snapshot part is missing: {part_id}") from exc
    if hashlib.sha256(_canonical(part.value)).hexdigest() != part.sha256:
        raise CorruptRecordError(
            f"chat snapshot part failed its integrity check: {part_id}"
        )
    return part.value


def resolve_request_snapshot(
    store: NebulaStore, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """A turn's request snapshot with every split-out value read back."""

    resolved = {
        key: value for key, value in snapshot.items() if key != SNAPSHOT_PARTS_KEY
    }
    references = snapshot.get(SNAPSHOT_PARTS_KEY)
    if not isinstance(references, Mapping):
        return resolved
    for key, part_id in references.items():
        if key not in resolved:
            resolved[key] = load_snapshot_part(store, str(part_id))
    return resolved


def split_skill_snapshots(
    snapshots: Iterable[Mapping[str, Any]], *, engagement_id: str, session_id: str
) -> tuple[list[dict[str, Any]], list[ChatSnapshotPart]]:
    """Return goal skill entries that keep everything but instructions inline.

    Each entry keeps the snapshot's identity, digest and resource list, which
    the goal panel and API show, plus the id of the part holding the whole
    snapshot. Entries must be full snapshots; resolve another conversation's
    entries before splitting them into this one.
    """

    entries: list[dict[str, Any]] = []
    parts: list[ChatSnapshotPart] = []
    for snapshot in snapshots:
        full = dict(snapshot)
        if "instructions" not in full:
            raise ValueError("a skill snapshot must be resolved before it is split")
        part = _part(
            _canonical(full), engagement_id=engagement_id, session_id=session_id
        )
        parts.append(part)
        entries.append(
            {
                **{
                    key: value
                    for key, value in full.items()
                    if key not in SKILL_PART_FIELDS
                },
                SKILL_PART_KEY: part.id,
            }
        )
    return entries, parts


def resolve_skill_snapshots(
    store: NebulaStore, entries: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Full skill snapshots for goal entries written split or inline."""

    resolved: list[dict[str, Any]] = []
    for entry in entries:
        part_id = entry.get(SKILL_PART_KEY)
        if "instructions" in entry or not isinstance(part_id, str):
            resolved.append(dict(entry))
            continue
        value = load_snapshot_part(store, part_id)
        if not isinstance(value, dict):
            raise CorruptRecordError(f"skill snapshot part is not an object: {part_id}")
        resolved.append(value)
    return resolved


def stage_snapshot_parts(
    transaction: StoreTransaction, parts: Iterable[ChatSnapshotPart]
) -> None:
    """Add each part not yet stored; a stored id already holds the same value."""

    for part in parts:
        transaction.add_absent(part)


__all__ = [
    "MIN_PART_BYTES",
    "REASONING_PARTS_KEY",
    "REQUEST_SNAPSHOT_PART_KEYS",
    "SKILL_PART_FIELDS",
    "SKILL_PART_KEY",
    "SNAPSHOT_PARTS_KEY",
    "load_snapshot_part",
    "resolve_request_snapshot",
    "resolve_skill_snapshots",
    "snapshot_part",
    "snapshot_part_id",
    "split_request_snapshot",
    "split_skill_snapshots",
    "stage_snapshot_parts",
]
