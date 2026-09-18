"""Conflict-aware workspace checkpoints under the shared .agents tree."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from .domain import NativeCheckpoint, NebulaModel, utc_now
from .storage import ConflictError, NebulaStore, NotFoundError


CHECKPOINT_ROOT = ".agents/checkpoints"
MAX_CHECKPOINT_FILES = 64
MAX_CHECKPOINT_FILE_BYTES = 1_048_576


class NativeCheckpointError(RuntimeError):
    """A safe, operator-actionable checkpoint failure."""


class CheckpointFileStatus(NebulaModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    status: str


def _safe_file(workspace: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not relative.strip():
        raise NativeCheckpointError(f"checkpoint path is not a bounded relative file: {relative}")
    root = workspace.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise NativeCheckpointError(f"checkpoint path escapes the workspace: {relative}") from exc
    if target.is_symlink() or not target.is_file():
        raise NativeCheckpointError(f"checkpoint path is not a regular file: {relative}")
    return target


def _copy_bounded(source: Path, destination: Path) -> dict[str, Any]:
    size = source.stat().st_size
    if size > MAX_CHECKPOINT_FILE_BYTES:
        raise NativeCheckpointError(
            f"{source.name} exceeds {MAX_CHECKPOINT_FILE_BYTES} bytes"
        )
    data = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {
        "path": str(source),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


class NativeCheckpointService:
    def __init__(self, store: NebulaStore, workspace_resolver) -> None:
        self.store = store
        self.workspace_resolver = workspace_resolver

    def list_for_session(self, session_id: str) -> list[NativeCheckpoint]:
        items: list[NativeCheckpoint] = []
        offset = 0
        while page := self.store.list_entities(
            NativeCheckpoint, offset=offset, limit=1_000
        ):
            items.extend(item for item in page if item.chat_session_id == session_id)
            offset += len(page)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def capture(
        self, session_id: str, *, label: str, paths: list[str]
    ) -> NativeCheckpoint:
        from .domain import ChatSession

        session = self.store.get(ChatSession, session_id)
        unique = list(dict.fromkeys(path.strip() for path in paths if path.strip()))
        if not unique:
            raise NativeCheckpointError("select at least one project file to checkpoint")
        if len(unique) > MAX_CHECKPOINT_FILES:
            raise NativeCheckpointError("checkpoint is limited to 64 files")
        workspace = Path(self.workspace_resolver(session.engagement_id)).resolve()
        checkpoint_id = str(uuid4())
        stored: list[dict[str, Any]] = []
        for relative in unique:
            source = _safe_file(workspace, relative)
            copied = _copy_bounded(
                source,
                workspace / CHECKPOINT_ROOT / checkpoint_id / Path(relative),
            )
            copied["path"] = relative
            stored.append(copied)
        return self.store.create(
            NativeCheckpoint(
                id=checkpoint_id,
                engagement_id=session.engagement_id,
                chat_session_id=session.id,
                label=label.strip() or utc_now().isoformat(),
                files=stored,
            )
        )

    def preview(self, checkpoint_id: str) -> list[CheckpointFileStatus]:
        checkpoint = self.store.get(NativeCheckpoint, checkpoint_id)
        workspace = Path(self.workspace_resolver(checkpoint.engagement_id)).resolve()
        statuses: list[CheckpointFileStatus] = []
        for item in checkpoint.files:
            relative = str(item["path"])
            current = workspace / relative
            captured = workspace / CHECKPOINT_ROOT / checkpoint.id / Path(relative)
            if not captured.is_file():
                raise NotFoundError(f"checkpoint file is missing: {relative}")
            if not current.exists():
                statuses.append(
                    CheckpointFileStatus(
                        path=relative, sha256=str(item["sha256"]), size=int(item["size"]),
                        status="missing",
                    )
                )
                continue
            digest = hashlib.sha256(_safe_file(workspace, relative).read_bytes()).hexdigest()
            statuses.append(
                CheckpointFileStatus(
                    path=relative,
                    sha256=digest,
                    size=int(item["size"]),
                    status="unchanged" if digest == item["sha256"] else "conflict",
                )
            )
        return statuses

    def restore(self, checkpoint_id: str) -> list[CheckpointFileStatus]:
        preview = self.preview(checkpoint_id)
        conflicts = [item.path for item in preview if item.status == "conflict"]
        if conflicts:
            raise ConflictError(
                "checkpoint restore rejected because later edits conflict: "
                + ", ".join(conflicts)
            )
        checkpoint = self.store.get(NativeCheckpoint, checkpoint_id)
        workspace = Path(self.workspace_resolver(checkpoint.engagement_id)).resolve()
        for item in checkpoint.files:
            relative = str(item["path"])
            captured = workspace / CHECKPOINT_ROOT / checkpoint.id / Path(relative)
            destination = workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(captured.read_bytes())
        return self.preview(checkpoint_id)
