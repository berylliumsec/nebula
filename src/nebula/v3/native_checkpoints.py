"""Conflict-aware workspace checkpoints under the shared .agents tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from .diagnostics import record_caught_exception
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW


def _checkpoint_parts(relative: str) -> tuple[str, ...]:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or not candidate.parts
        or not relative.strip()
    ):
        raise NativeCheckpointError(
            f"checkpoint path is not a bounded relative file: {relative}"
        )
    return candidate.parts


def _safe_file(workspace: Path, relative: str) -> Path:
    candidate = Path(*_checkpoint_parts(relative))
    root = workspace.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise NativeCheckpointError(
            f"checkpoint path escapes the workspace: {relative}"
        ) from exc
    if target.is_symlink() or not target.is_file():
        raise NativeCheckpointError(
            f"checkpoint path is not a regular file: {relative}"
        )
    return target


def _lstat_or_none(path: Path, relative: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        # diagnostic-expected: nothing exists at this component of the checkpointed path.
        return None
    except OSError as exc:
        raise NativeCheckpointError(
            f"checkpoint path cannot be inspected: {relative}"
        ) from exc


def _current_state(
    workspace: Path, relative: str
) -> Literal["missing", "conflict", "regular"]:
    """Classify the live workspace entry for a checkpointed path.

    Every component is inspected with ``lstat`` so a symlink anywhere on the
    path (dangling or not) or a non-directory parent is a ``conflict`` rather
    than a ``missing`` file that a restore would write through.
    """

    parts = _checkpoint_parts(relative)
    current = workspace
    for part in parts[:-1]:
        current = current / part
        info = _lstat_or_none(current, relative)
        if info is None:
            return "missing"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return "conflict"
    info = _lstat_or_none(current / parts[-1], relative)
    if info is None:
        return "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "conflict"
    return "regular"


def _restore_file(workspace: Path, relative: str, payload: bytes) -> None:
    """Write checkpoint bytes to ``relative`` without following any symlink.

    Directories are opened component by component with ``O_NOFOLLOW``
    (recreating the ones the workspace lost since capture), the bytes land in
    a private temporary file beside the destination, and ``os.replace`` swaps
    it in, so a link that raced in after the preview is replaced rather than
    written through.
    """

    parts = _checkpoint_parts(relative)
    try:
        directory = os.open(workspace, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise NativeCheckpointError(
            f"workspace is unavailable for restore: {relative}"
        ) from exc
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                # diagnostic-expected: restore recreates directories removed since capture.
                os.mkdir(part, dir_fd=directory)
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        temporary = f".{uuid4().hex}.restore"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o666,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            # diagnostic-expected: the private temporary file is removed and the failure re-raised unchanged.
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                # diagnostic-expected: the temporary file never reached the directory.
                pass
            raise
    except OSError as exc:
        raise NativeCheckpointError(
            f"checkpoint path cannot be restored: {relative}"
        ) from exc
    finally:
        os.close(directory)


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


def _discard(path: Path) -> None:
    """Remove a staged or record-less checkpoint directory during cleanup."""

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        # diagnostic-expected: the directory was never created or is already gone.
        return
    except OSError as caught_error:
        record_caught_exception(
            "chat",
            "chat.native_checkpoints.cleanup_failed",
            "A partial native checkpoint directory could not be removed.",
            caught_error,
            stage="checkpoint-cleanup",
        )


class NativeCheckpointService:
    def __init__(self, store: NebulaStore, workspace_resolver) -> None:
        self.store = store
        self.workspace_resolver = workspace_resolver

    def list_for_session(self, session_id: str) -> list[NativeCheckpoint]:
        items = self.store.list_session_entities(NativeCheckpoint, session_id)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def capture(
        self, session_id: str, *, label: str, paths: list[str]
    ) -> NativeCheckpoint:
        from .domain import ChatSession

        session = self.store.get(ChatSession, session_id)
        unique = list(dict.fromkeys(path.strip() for path in paths if path.strip()))
        if not unique:
            raise NativeCheckpointError(
                "select at least one project file to checkpoint"
            )
        if len(unique) > MAX_CHECKPOINT_FILES:
            raise NativeCheckpointError("checkpoint is limited to 64 files")
        workspace = Path(self.workspace_resolver(session.engagement_id)).resolve()
        # Validate every path and size before writing anything, then copy
        # into a staging directory that only becomes the checkpoint once all
        # files are in place. A rejected entry therefore never leaves a
        # partial copy behind in the workspace.
        sources: list[tuple[str, Path]] = []
        for relative in unique:
            source = _safe_file(workspace, relative)
            if source.stat().st_size > MAX_CHECKPOINT_FILE_BYTES:
                raise NativeCheckpointError(
                    f"{source.name} exceeds {MAX_CHECKPOINT_FILE_BYTES} bytes"
                )
            sources.append((relative, source))
        checkpoint_id = str(uuid4())
        checkpoint_root = workspace / CHECKPOINT_ROOT
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        staging = checkpoint_root / f".{checkpoint_id}.partial"
        staging.mkdir()
        stored: list[dict[str, Any]] = []
        try:
            for relative, source in sources:
                copied = _copy_bounded(source, staging / Path(relative))
                copied["path"] = relative
                stored.append(copied)
            staging.rename(checkpoint_root / checkpoint_id)
        except BaseException:
            # diagnostic-expected: the staged copy is removed and the failure re-raised unchanged.
            _discard(staging)
            raise
        try:
            return self.store.create(
                NativeCheckpoint(
                    id=checkpoint_id,
                    engagement_id=session.engagement_id,
                    chat_session_id=session.id,
                    label=label.strip() or utc_now().isoformat(),
                    files=stored,
                )
            )
        except BaseException:
            # diagnostic-expected: a checkpoint without its record is removed and the failure re-raised unchanged.
            _discard(checkpoint_root / checkpoint_id)
            raise

    def preview(self, checkpoint_id: str) -> list[CheckpointFileStatus]:
        checkpoint = self.store.get(NativeCheckpoint, checkpoint_id)
        workspace = Path(self.workspace_resolver(checkpoint.engagement_id)).resolve()
        statuses: list[CheckpointFileStatus] = []
        for item in checkpoint.files:
            relative = str(item["path"])
            captured = workspace / CHECKPOINT_ROOT / checkpoint.id / Path(relative)
            if not captured.is_file():
                raise NotFoundError(f"checkpoint file is missing: {relative}")
            state = _current_state(workspace, relative)
            if state != "regular":
                statuses.append(
                    CheckpointFileStatus(
                        path=relative,
                        sha256=str(item["sha256"]),
                        size=int(item["size"]),
                        status=state,
                    )
                )
                continue
            digest = hashlib.sha256(
                _safe_file(workspace, relative).read_bytes()
            ).hexdigest()
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
            _restore_file(workspace, relative, captured.read_bytes())
        return self.preview(checkpoint_id)
