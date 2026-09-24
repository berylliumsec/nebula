"""Bounded Git workspace provenance for actor-scoped lifecycle hooks.

Only paths already reported dirty by Git are read.  This deliberately avoids a
workspace walk: repositories and non-Git folders remain usable even when
provenance cannot be established, and uncertainty is returned as data rather
than assigned to an arbitrary actor.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from .domain import (
    ChatSession,
    WorkspaceProvenanceObservation,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError


PROVENANCE_SCHEMA = "nebula.workspace-provenance/v1"
MAX_DIRTY_PATHS = 2_000


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


def _run_git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "-C", str(workspace), *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
        timeout=30,
        check=False,
    )


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _dirty_entries(output: bytes) -> dict[str, str]:
    fields = output.split(b"\0")
    entries: dict[str, str] = {}
    index = 0
    while index < len(fields):
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


def _path_state(root: Path, relative: str, status: str) -> dict[str, Any]:
    path = root / relative
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"dirty path escaped the workspace: {relative!r}") from exc
    try:
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8", "surrogateescape")
            kind = "symlink"
        elif path.is_file():
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            return {
                "status": status,
                "kind": "file",
                "sha256": digest.hexdigest(),
                "size": size,
            }
        elif path.exists():
            data = b"directory"
            kind = "directory"
        else:
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


def snapshot(workspace: Path) -> _Snapshot:
    """Fingerprint only the checkout paths Git already reports as dirty."""

    root_result = _run_git(workspace, "rev-parse", "--show-toplevel")
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
    )
    if status.returncode:
        detail = status.stderr.decode("utf-8", "replace").strip()[:500]
        return _Snapshot(
            False, str(root), {}, f"git_status_failed:{detail or status.returncode}"
        )
    try:
        entries = _dirty_entries(status.stdout)
    except ValueError as exc:  # diagnostic-expected: malformed Git output becomes an explicit unsupported receipt
        return _Snapshot(False, str(root), {}, f"git_status_invalid:{exc}")
    if len(entries) > MAX_DIRTY_PATHS:
        return _Snapshot(
            False,
            str(root),
            {},
            f"dirty_path_limit_exceeded:{len(entries)}>{MAX_DIRTY_PATHS}",
        )
    return _Snapshot(
        True,
        str(root),
        {
            relative: _path_state(root, relative, entries[relative])
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
            return self.store.get(WorkspaceProvenanceObservation, identity)
        except NotFoundError:  # diagnostic-expected: first observation for this scope continues to creation
            pass
        observed = snapshot(workspace)
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
            baseline=observed.paths,
            confidence="exact" if observed.supported else "unsupported",
        )
        try:
            return self.store.create(entity)
        except ConflictError:  # diagnostic-expected: a concurrent begin won; return its durable observation
            return self.store.get(WorkspaceProvenanceObservation, identity)

    def finish(
        self,
        workspace: Path,
        *,
        engagement_id: str,
        scope_kind: Literal["turn", "tool"],
        scope_id: str,
    ) -> WorkspaceProvenanceObservation:
        identity = _observation_id(engagement_id, scope_kind, scope_id)
        observation = self.store.get(WorkspaceProvenanceObservation, identity)
        if observation.status == "complete":
            return observation
        observed = snapshot(workspace)
        completed_at = utc_now()
        if not observation.supported or not observed.supported:
            reason = observation.unsupported_reason or observed.reason or "unsupported"
            return self.store.update(
                WorkspaceProvenanceObservation,
                observation.id,
                {
                    "status": "complete",
                    "supported": False,
                    "unsupported_reason": reason,
                    "current": observed.paths,
                    "confidence": "unsupported",
                    "completed_at": completed_at,
                },
                expected_revision=observation.revision,
            )
        if observed.workspace_root != observation.workspace_root:
            return self.store.update(
                WorkspaceProvenanceObservation,
                observation.id,
                {
                    "status": "complete",
                    "supported": False,
                    "unsupported_reason": "workspace_root_changed",
                    "current": observed.paths,
                    "confidence": "unsupported",
                    "completed_at": completed_at,
                },
                expected_revision=observation.revision,
            )
        observations = self.store.find_entities(
            WorkspaceProvenanceObservation,
            {"workspace_root": observation.workspace_root},
            engagement_id=engagement_id,
            limit=1_000,
            newest_first=True,
        )
        concurrent = sorted(
            {
                item.scope_id
                for item in observations
                if item.id != observation.id
                and item.actor_id != observation.actor_id
                and item.started_at <= completed_at
                and (
                    item.completed_at is None
                    or item.completed_at >= observation.started_at
                )
            }
        )[:128]
        confidence = "uncertain_parallel_turns" if concurrent else "exact"
        mutations = [
            {**row, "confidence": confidence}
            for row in _change_rows(observation.baseline, observed.paths)
        ]
        attribution = self._classify(
            observation,
            observed.paths,
            mutations,
            confidence,
            observations,
        )
        return self.store.update(
            WorkspaceProvenanceObservation,
            observation.id,
            {
                "status": "complete",
                "current": observed.paths,
                "mutations": mutations,
                "attribution": attribution,
                "confidence": confidence,
                "concurrent_scope_ids": concurrent,
                "completed_at": completed_at,
            },
            expected_revision=observation.revision,
        )

    @staticmethod
    def _classify(
        observation: WorkspaceProvenanceObservation,
        current: dict[str, dict[str, Any]],
        mutations: list[dict[str, Any]],
        confidence: str,
        prior: list[WorkspaceProvenanceObservation],
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {
            "owned": [],
            "other": [],
            "preexisting": [],
            "unknown": [],
            "uncertain": [],
        }
        completed = sorted(
            (item for item in prior if item.status == "complete"),
            key=lambda item: (item.completed_at or item.started_at, item.id),
            reverse=True,
        )
        current_mutations = {row["path"]: row for row in mutations}
        for path, state in sorted(current.items()):
            row = current_mutations.get(path)
            if row is not None:
                item = {"path": path, "actor_id": observation.actor_id, "state": state}
                result["uncertain" if confidence != "exact" else "owned"].append(item)
                continue
            matched: tuple[WorkspaceProvenanceObservation, dict[str, Any]] | None = None
            for candidate in completed:
                mutation = next(
                    (
                        entry
                        for entry in reversed(candidate.mutations)
                        if entry.get("path") == path and entry.get("after") == state
                    ),
                    None,
                )
                if mutation is not None:
                    matched = candidate, mutation
                    break
            if matched is not None:
                owner, mutation = matched
                item = {"path": path, "actor_id": owner.actor_id, "state": state}
                if mutation.get("confidence") != "exact":
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
            "mutations": observation.mutations,
            "attribution": observation.attribution,
            "concurrent_scope_ids": observation.concurrent_scope_ids,
        }


__all__ = [
    "PROVENANCE_SCHEMA",
    "WorkspaceProvenanceService",
    "actor_id_for",
    "snapshot",
]
