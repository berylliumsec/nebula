"""Bounded Git workspace provenance for actor-scoped lifecycle hooks.

Only paths already reported dirty by Git are read.  This deliberately avoids a
workspace walk: repositories and non-Git folders remain usable even when
provenance cannot be established, and uncertainty is returned as data rather
than assigned to an arbitrary actor.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import (
    EntityRow,
    WorkspaceProvenanceIndexRow,
    WorkspaceProvenanceOwnerRow,
)
from .domain import (
    ChatSession,
    WorkspaceProvenanceObservation,
    utc_now,
)
from .storage import ConflictError, CorruptRecordError, NebulaStore, NotFoundError
from .workspace_provenance_ledger import (
    matching_owners,
    overlapping_scopes,
    path_key,
    remember_mutations,
    save_map,
    load_map,
    state_key,
    workspace_key,
)


PROVENANCE_SCHEMA = "nebula.workspace-provenance/v1"
# A wall-clock budget limits the cost without rejecting a checkout merely for
# containing many paths or large files. An over-budget receipt stays unsupported.
SNAPSHOT_TIME_LIMIT_SECONDS = 10.0


class _SnapshotTimeLimit(Exception):
    """The snapshot ran past its time limit."""


@dataclass(frozen=True)
class _Snapshot:
    supported: bool
    workspace_root: str
    paths: dict[str, dict[str, Any]]
    reason: str | None = None


def actor_id_for(
    store: NebulaStore,
    *,
    owner_kind: Literal["chat", "mission", "harness", "api"],
    owner_id: str | None,
    chat_session_id: str | None,
) -> str:
    """Resolve a stable execution actor without conflating operator identity."""

    if chat_session_id:
        try:
            session = store.get(ChatSession, chat_session_id)
        except NotFoundError:  # diagnostic-expected: non-chat owners and deleted sessions retain their explicit runtime identity
            pass
        else:
            subagent_id = session.metadata.get("subagent_id")
            if isinstance(subagent_id, str) and subagent_id.strip():
                return f"subagent:{subagent_id.strip()}"
            return f"chat:{session.id}"
    return f"{owner_kind}:{owner_id or chat_session_id or 'unknown'}"


def _observation_id(engagement_id: str, scope_kind: str, scope_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"nebula:workspace-provenance:{engagement_id}:{scope_kind}:{scope_id}",
        )
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _SnapshotTimeLimit()
    return remaining


def _run_git(
    workspace: Path, *arguments: str, deadline: float
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ("git", "-C", str(workspace), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
            timeout=_remaining(deadline),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _SnapshotTimeLimit() from exc


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _dirty_entries(output: bytes, *, deadline: float) -> dict[str, str]:
    fields = output.split(b"\0")
    entries: dict[str, str] = {}
    index = 0
    while index < len(fields):
        if index % 1024 == 0:
            _remaining(deadline)
        item = fields[index]
        index += 1
        if not item:
            continue
        if len(item) < 4 or item[2:3] != b" ":
            raise ValueError("Git returned malformed porcelain status")
        status = item[:2].decode("ascii", "replace")
        path = _decode_path(item[3:])
        entries[path] = status
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise ValueError("Git returned an incomplete rename record")
            entries[_decode_path(fields[index])] = status
            index += 1
    return entries


def _path_state(
    root: Path, relative: str, status: str, *, deadline: float
) -> dict[str, Any]:
    _remaining(deadline)
    path = root / relative
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"dirty path escaped the workspace: {relative!r}") from exc
    try:
        file_state = os.lstat(path)
        if stat.S_ISLNK(file_state.st_mode):
            data = os.readlink(path).encode("utf-8", "surrogateescape")
            kind = "symlink"
        elif stat.S_ISREG(file_state.st_mode):
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    _remaining(deadline)
                    digest.update(chunk)
                    size += len(chunk)
            return {
                "status": status,
                "kind": "file",
                "sha256": digest.hexdigest(),
                "size": size,
            }
        else:
            data = b"directory"
            kind = "directory"
    except FileNotFoundError:
        # diagnostic-expected: a deleted dirty path is recorded as absent
        return {"status": status, "kind": "absent", "sha256": None, "size": 0}
    except OSError as exc:  # diagnostic-expected: unreadable path state is represented in the receipt without aborting the hook
        return {
            "status": status,
            "kind": "unreadable",
            "sha256": None,
            "size": 0,
            "error": type(exc).__name__,
        }
    return {
        "status": status,
        "kind": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def snapshot(
    workspace: Path, *, time_limit: float = SNAPSHOT_TIME_LIMIT_SECONDS
) -> _Snapshot:
    """Fingerprint only the checkout paths Git already reports as dirty.

    Bounded by ``time_limit``; a snapshot past the budget is an unsupported
    receipt, never a partial one.
    """

    deadline = time.monotonic() + time_limit
    try:
        return _bounded_snapshot(workspace, deadline)
    except (
        _SnapshotTimeLimit
    ):  # diagnostic-expected: a slow repository becomes an explicit unsupported receipt
        return _Snapshot(
            False,
            str(workspace.resolve()),
            {},
            f"snapshot_time_limit_exceeded:{time_limit:g}s",
        )


def _bounded_snapshot(workspace: Path, deadline: float) -> _Snapshot:
    root_result = _run_git(workspace, "rev-parse", "--show-toplevel", deadline=deadline)
    if root_result.returncode:
        return _Snapshot(
            False,
            str(workspace.resolve()),
            {},
            "workspace_is_not_a_git_checkout",
        )
    try:
        root = Path(_decode_path(root_result.stdout).strip()).resolve(strict=True)
    except (
        OSError,
        ValueError,
    ):  # diagnostic-expected: an unavailable root becomes an explicit unsupported receipt
        return _Snapshot(False, str(workspace.resolve()), {}, "git_root_is_unavailable")
    status = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        deadline=deadline,
    )
    if status.returncode:
        detail = status.stderr.decode("utf-8", "replace").strip()[:500]
        return _Snapshot(
            False, str(root), {}, f"git_status_failed:{detail or status.returncode}"
        )
    try:
        entries = _dirty_entries(status.stdout, deadline=deadline)
    except ValueError as exc:  # diagnostic-expected: malformed Git output becomes an explicit unsupported receipt
        return _Snapshot(False, str(root), {}, f"git_status_invalid:{exc}")
    return _Snapshot(
        True,
        str(root),
        {
            relative: _path_state(root, relative, entries[relative], deadline=deadline)
            for relative in sorted(entries)
        },
    )


def _change_rows(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {"path": path, "before": before.get(path), "after": after.get(path)}
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]


class WorkspaceProvenanceService:
    """Persist and classify bounded workspace observations for local hooks."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def _hydrate(
        self,
        observation: WorkspaceProvenanceObservation,
        session: Session,
    ) -> WorkspaceProvenanceObservation:
        workspace = workspace_key(observation.engagement_id, observation.workspace_root)
        baseline = (
            load_map(session, workspace, "paths", observation.baseline_snapshot_id)
            if observation.baseline_snapshot_id
            else observation.baseline
        )
        current = (
            load_map(session, workspace, "paths", observation.current_snapshot_id)
            if observation.current_snapshot_id
            else observation.current
        )
        attribution = observation.attribution
        if observation.attribution_snapshot_id:
            labels = load_map(
                session, workspace, "attribution", observation.attribution_snapshot_id
            )
            attribution = self._expand_attribution(current, labels)
        return observation.model_copy(
            update={
                "baseline": baseline,
                "current": current,
                "attribution": attribution,
            }
        )

    @staticmethod
    def _lock_workspace(session: Session, workspace: str) -> None:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:workspace))"),
                {"workspace": f"workspace-provenance:{workspace}"},
            )

    @staticmethod
    def _stored_observation(
        session: Session, identity: str
    ) -> WorkspaceProvenanceObservation | None:
        row = session.get(EntityRow, identity)
        if row is None:
            return None
        if row.kind != WorkspaceProvenanceObservation.entity_kind:
            raise ConflictError(f"entity id belongs to {row.kind}: {identity}")
        return WorkspaceProvenanceObservation.model_validate(row.payload)

    def get(
        self,
        *,
        engagement_id: str,
        scope_kind: Literal["turn", "tool"],
        scope_id: str,
    ) -> WorkspaceProvenanceObservation:
        """Read one receipt, including its shared path chunks."""

        identity = _observation_id(engagement_id, scope_kind, scope_id)
        with self.store.database.session() as session:
            observation = self._stored_observation(session, identity)
            if observation is None:
                raise NotFoundError(
                    f"workspace provenance observation not found: {identity}"
                )
            return self._hydrate(observation, session)

    def begin(
        self,
        workspace: Path,
        *,
        engagement_id: str,
        scope_kind: Literal["turn", "tool"],
        scope_id: str,
        actor_id: str,
        owner_kind: Literal["chat", "mission", "harness", "api"],
        owner_id: str | None,
        chat_session_id: str | None,
        chat_turn_id: str | None,
    ) -> WorkspaceProvenanceObservation:
        identity = _observation_id(engagement_id, scope_kind, scope_id)
        try:
            return self.get(
                engagement_id=engagement_id, scope_kind=scope_kind, scope_id=scope_id
            )
        except NotFoundError:  # diagnostic-expected: first observation for this scope continues to creation
            pass
        observed = snapshot(workspace)
        workspace_id = workspace_key(engagement_id, observed.workspace_root)
        entity = WorkspaceProvenanceObservation(
            id=identity,
            engagement_id=engagement_id,
            workspace_root=observed.workspace_root,
            scope_kind=scope_kind,
            scope_id=scope_id,
            actor_id=actor_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            chat_turn_id=chat_turn_id,
            supported=observed.supported,
            unsupported_reason=observed.reason,
            confidence="exact" if observed.supported else "unsupported",
        )
        try:
            with self.store.transaction(immediate=True) as transaction:
                self._lock_workspace(transaction.session, workspace_id)
                existing = self._stored_observation(transaction.session, identity)
                if existing is not None:
                    return self._hydrate(existing, transaction.session)
                baseline_id = save_map(
                    transaction.session,
                    engagement_id,
                    workspace_id,
                    "paths",
                    observed.paths,
                )
                entity.baseline_snapshot_id = baseline_id
                transaction.add(entity)
                transaction.session.add(
                    WorkspaceProvenanceIndexRow(
                        observation_id=entity.id,
                        engagement_id=engagement_id,
                        workspace_key=workspace_id,
                        actor_id=entity.actor_id,
                        scope_id=entity.scope_id,
                        started_at=entity.started_at,
                        completed_at=None,
                    )
                )
        except (
            ConflictError
        ):  # diagnostic-expected: a concurrent begin won the durable scope id
            return self.get(
                engagement_id=engagement_id, scope_kind=scope_kind, scope_id=scope_id
            )
        return entity.model_copy(update={"baseline": observed.paths})

    def finish(
        self,
        workspace: Path,
        *,
        engagement_id: str,
        scope_kind: Literal["turn", "tool"],
        scope_id: str,
    ) -> WorkspaceProvenanceObservation:
        identity = _observation_id(engagement_id, scope_kind, scope_id)
        observation = self.get(
            engagement_id=engagement_id, scope_kind=scope_kind, scope_id=scope_id
        )
        if observation.status == "complete":
            return observation
        observed = snapshot(workspace)
        completed_at = utc_now()
        workspace_id = workspace_key(engagement_id, observation.workspace_root)
        with self.store.transaction(immediate=True) as transaction:
            session = transaction.session
            self._lock_workspace(session, workspace_id)
            stored = self._stored_observation(session, identity)
            if stored is None:
                raise NotFoundError(
                    f"workspace provenance observation not found: {identity}"
                )
            if stored.status == "complete":
                return self._hydrate(stored, session)
            observation = self._hydrate(stored, session)

            index = session.get(WorkspaceProvenanceIndexRow, identity)
            if index is None:
                # An in-flight observation from before the cutover can finish
                # using its inline baseline, without loading old history.
                index = WorkspaceProvenanceIndexRow(
                    observation_id=identity,
                    engagement_id=engagement_id,
                    workspace_key=workspace_id,
                    actor_id=observation.actor_id,
                    scope_id=observation.scope_id,
                    started_at=observation.started_at,
                )
                session.add(index)

            supported = observation.supported and observed.supported
            reason = observation.unsupported_reason or observed.reason
            if supported and observed.workspace_root != observation.workspace_root:
                supported = False
                reason = "workspace_root_changed"

            concurrent: list[str] = []
            confidence = "unsupported"
            mutations: list[dict[str, Any]] = []
            attribution: dict[str, list[dict[str, Any]]] = {}
            attribution_id: str | None = None
            owners = {}
            if supported:
                concurrent = overlapping_scopes(session, observation, completed_at)
                confidence = "uncertain_parallel_turns" if concurrent else "exact"
                mutations = [
                    {**row, "confidence": confidence}
                    for row in _change_rows(observation.baseline, observed.paths)
                ]
                owners = matching_owners(session, workspace_id, observed.paths)
                attribution = self._classify(
                    observation, observed.paths, mutations, confidence, owners
                )
                labels = self._compact_attribution(attribution)
                attribution_id = save_map(
                    session, engagement_id, workspace_id, "attribution", labels
                )

            baseline_id = stored.baseline_snapshot_id or save_map(
                session, engagement_id, workspace_id, "paths", observation.baseline
            )
            current_id = save_map(
                session, engagement_id, workspace_id, "paths", observed.paths
            )
            if supported:
                remember_mutations(
                    session, workspace_id, observation, mutations, completed_at, owners
                )
            updated = transaction.update(
                WorkspaceProvenanceObservation,
                identity,
                {
                    "status": "complete",
                    "supported": supported,
                    "unsupported_reason": None
                    if supported
                    else reason or "unsupported",
                    "baseline_snapshot_id": baseline_id,
                    "current_snapshot_id": current_id,
                    "attribution_snapshot_id": attribution_id,
                    "baseline": {},
                    "current": {},
                    "mutations": mutations,
                    "attribution": {},
                    "confidence": confidence,
                    "concurrent_scope_ids": concurrent,
                    "completed_at": completed_at,
                },
                expected_revision=stored.revision,
            )
            index.completed_at = completed_at
        return updated.model_copy(
            update={
                "baseline": observation.baseline,
                "current": observed.paths,
                "attribution": attribution,
            }
        )

    @staticmethod
    def _classify(
        observation: WorkspaceProvenanceObservation,
        current: dict[str, dict[str, Any]],
        mutations: list[dict[str, Any]],
        confidence: str,
        owners: dict[tuple[str, str], WorkspaceProvenanceOwnerRow],
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {
            "owned": [],
            "other": [],
            "preexisting": [],
            "unknown": [],
            "uncertain": [],
        }
        current_mutations = {row["path"]: row for row in mutations}
        for path, state in sorted(current.items()):
            row = current_mutations.get(path)
            if row is not None:
                item = {"path": path, "actor_id": observation.actor_id, "state": state}
                result["uncertain" if confidence != "exact" else "owned"].append(item)
                continue
            owner = owners.get((path_key(path), state_key(state)))
            if owner is not None and owner.path == path:
                item = {"path": path, "actor_id": owner.actor_id, "state": state}
                if owner.confidence != "exact":
                    result["uncertain"].append(item)
                elif owner.actor_id == observation.actor_id:
                    result["owned"].append(item)
                else:
                    result["other"].append(item)
            elif observation.baseline.get(path) == state:
                result["preexisting"].append({"path": path, "state": state})
            else:
                result["unknown"].append({"path": path, "state": state})
        return result

    @staticmethod
    def _compact_attribution(
        attribution: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, str]]:
        return {
            item["path"]: {
                "bucket": bucket,
                **({"actor_id": item["actor_id"]} if "actor_id" in item else {}),
            }
            for bucket, items in attribution.items()
            if bucket != "preexisting"
            for item in items
        }

    @staticmethod
    def _expand_attribution(
        current: dict[str, dict[str, Any]], labels: dict[str, dict[str, str]]
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {
            "owned": [],
            "other": [],
            "preexisting": [],
            "unknown": [],
            "uncertain": [],
        }
        if set(labels) - set(current):
            raise CorruptRecordError(
                "workspace attribution names a missing current path"
            )
        for path, state in sorted(current.items()):
            label = labels.get(path, {"bucket": "preexisting"})
            bucket = label.get("bucket")
            if bucket not in result:
                raise CorruptRecordError("workspace attribution has an invalid bucket")
            item: dict[str, Any] = {"path": path, "state": state}
            if actor_id := label.get("actor_id"):
                item["actor_id"] = actor_id
            result[bucket].append(item)
        return result

    @staticmethod
    def receipt(observation: WorkspaceProvenanceObservation) -> dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "observation_id": observation.id,
            "workspace_root": observation.workspace_root,
            "scope_kind": observation.scope_kind,
            "scope_id": observation.scope_id,
            "actor_id": observation.actor_id,
            "status": observation.status,
            "supported": observation.supported,
            "unsupported_reason": observation.unsupported_reason,
            "confidence": observation.confidence,
            "current_paths": sorted(observation.current),
            "mutations": observation.mutations,
            "attribution": observation.attribution,
            "concurrent_scope_ids": observation.concurrent_scope_ids,
        }


__all__ = [
    "PROVENANCE_SCHEMA",
    "SNAPSHOT_TIME_LIMIT_SECONDS",
    "WorkspaceProvenanceService",
    "actor_id_for",
    "snapshot",
]
