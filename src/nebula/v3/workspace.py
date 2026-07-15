"""Symlink-safe engagement workspace browsing, promotion, and reset."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import hashlib
import mimetypes
import os
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field

from .artifacts import ArtifactStore
from .domain import (
    Engagement,
    Evidence,
    NebulaModel,
    OperatorExecution,
    OperatorExecutionStatus,
)
from .executions import (
    WORKSPACE_MAX_BYTES,
    WORKSPACE_MAX_ENTRIES,
    WORKSPACE_MAX_FILE_BYTES,
    ExecutionServiceError,
)
from .storage import NebulaStore
from .tool_platform import ToolPlatform

MAX_PREVIEW_BYTES = 256 * 1024
_BUSY_STATUSES = {
    OperatorExecutionStatus.QUEUED,
    OperatorExecutionStatus.RUNNING,
    OperatorExecutionStatus.CANCELLING,
}


class WorkspaceEntry(NebulaModel):
    path: str
    name: str
    kind: Literal["file", "directory", "symlink", "other"]
    size: int = Field(ge=0)
    modified_at: datetime


class WorkspaceListing(NebulaModel):
    engagement_id: str
    path: str
    entries: list[WorkspaceEntry]
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    total: int = Field(ge=0)


class WorkspacePreview(NebulaModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        protected_namespaces=(),
        str_strip_whitespace=False,
    )

    engagement_id: str
    path: str
    text: str
    bytes_returned: int = Field(ge=0)
    truncated: bool
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspacePromotionRequest(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=500)
    description: str = Field(default="", max_length=20_000)


class WorkspaceResetRequest(NebulaModel):
    engagement_name: str = Field(min_length=1, max_length=300)


class WorkspaceResetResult(NebulaModel):
    engagement_id: str
    removed_entries: int = Field(ge=0)


class WorkspaceUploadResult(NebulaModel):
    engagement_id: str
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overwritten: bool = False


class WorkspaceDownload:
    def __init__(
        self, stream: BinaryIO, *, filename: str, media_type: str, size: int
    ) -> None:
        self.stream = stream
        self.filename = filename
        self.media_type = media_type
        self.size = size

    def chunks(self, size: int = 64 * 1024) -> Iterator[bytes]:
        try:
            while True:
                chunk = self.stream.read(size)
                if not chunk:
                    break
                yield chunk
        finally:
            self.stream.close()


class WorkspaceService:
    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        tool_platform: ToolPlatform,
        operator_id: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.tool_platform = tool_platform
        self.operator_id = operator_id or (lambda: "system")
        self._upload_locks: dict[str, asyncio.Lock] = {}

    def list(
        self,
        engagement_id: str,
        path: str = "",
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> WorkspaceListing:
        self.store.get(Engagement, engagement_id)
        relative = _relative_parts(path)
        descriptor = self._open_directory(engagement_id, relative)
        try:
            rows: list[WorkspaceEntry] = []
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    mode = metadata.st_mode
                    kind: Literal["file", "directory", "symlink", "other"]
                    if stat.S_ISLNK(mode):
                        kind = "symlink"
                    elif stat.S_ISDIR(mode):
                        kind = "directory"
                    elif stat.S_ISREG(mode):
                        kind = "file"
                    else:
                        kind = "other"
                    entry_path = PurePosixPath(*relative, entry.name).as_posix()
                    rows.append(
                        WorkspaceEntry(
                            path=entry_path,
                            name=entry.name,
                            kind=kind,
                            size=metadata.st_size,
                            modified_at=datetime.fromtimestamp(
                                metadata.st_mtime, tz=timezone.utc
                            ),
                        )
                    )
        finally:
            os.close(descriptor)
        rows.sort(
            key=lambda row: (row.kind != "directory", row.name.casefold(), row.name)
        )
        page = rows[offset : offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(rows) else None
        return WorkspaceListing(
            engagement_id=engagement_id,
            path=PurePosixPath(*relative).as_posix() if relative else "",
            entries=page,
            offset=offset,
            next_offset=next_offset,
            total=len(rows),
        )

    def preview(self, engagement_id: str, path: str) -> WorkspacePreview:
        relative = _relative_parts(path, require_value=True)
        stream, metadata = self._open_regular(engagement_id, relative)
        try:
            payload = stream.read(MAX_PREVIEW_BYTES + 1)
        finally:
            stream.close()
        visible = payload[:MAX_PREVIEW_BYTES]
        if b"\x00" in visible:
            raise ExecutionServiceError(
                "unsupported_preview",
                "binary files cannot be previewed",
                status_code=415,
            )
        try:
            text = visible.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            record_caught_exception(
                "workspace",
                "workspace.workspace.caught_failure_001",
                "A handled workspace operation raised an exception.",
                exc,
                stage="workspace",
            )
            raise ExecutionServiceError(
                "unsupported_preview",
                "file is not valid UTF-8 plain text",
                status_code=415,
            ) from exc
        return WorkspacePreview(
            engagement_id=engagement_id,
            path=PurePosixPath(*relative).as_posix(),
            text=text,
            bytes_returned=len(visible),
            truncated=metadata.st_size > len(visible),
            preview_sha256=hashlib.sha256(visible).hexdigest(),
        )

    def download(self, engagement_id: str, path: str) -> WorkspaceDownload:
        relative = _relative_parts(path, require_value=True)
        stream, metadata = self._open_regular(engagement_id, relative)
        filename = relative[-1]
        return WorkspaceDownload(
            stream,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size=metadata.st_size,
        )

    def promote(
        self, engagement_id: str, request: WorkspacePromotionRequest
    ) -> Evidence:
        engagement = self.store.get(Engagement, engagement_id)
        relative = _relative_parts(request.path, require_value=True)
        stream, _metadata = self._open_regular(engagement_id, relative)
        path = PurePosixPath(*relative).as_posix()
        try:
            stored = self.artifact_store.put_stream_with_status(
                stream,
                engagement_id=engagement.id,
                filename=relative[-1],
                media_type=mimetypes.guess_type(relative[-1])[0],
                source="engagement-workspace-promotion",
                metadata={"workspace_path": path},
            )
        finally:
            stream.close()
        if not self.artifact_store.verify(stored.artifact):
            self.artifact_store.discard_new_blob(stored)
            raise ExecutionServiceError(
                "artifact_integrity", "promoted file failed hash verification"
            )
        evidence = Evidence(
            engagement_id=engagement.id,
            evidence_type="workspace-file",
            title=request.title or relative[-1],
            description=request.description,
            artifact_id=stored.artifact.id,
            sha256=stored.artifact.sha256,
            captured_by=self.operator_id(),
            source_version="nebula.workspace-promotion/v1",
            metadata={"workspace_path": path},
        )
        try:
            self.store.create_many([stored.artifact, evidence])
        except Exception as caught_error:
            record_caught_exception(
                "workspace",
                "workspace.workspace.caught_failure_002",
                "A handled workspace operation raised an exception.",
                caught_error,
                stage="workspace",
            )
            self.artifact_store.discard_new_blob(stored)
            raise
        self.store.append_operation_event(
            evidence.id,
            "workspace_promotion",
            engagement.id,
            "workspace.promoted",
            {
                "path": path,
                "artifact_id": stored.artifact.id,
                "evidence_id": evidence.id,
                "sha256": stored.artifact.sha256,
            },
            actor_id=self.operator_id(),
            idempotency_key=f"workspace-promotion:{evidence.id}",
        )
        return evidence

    async def upload(
        self,
        engagement_id: str,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        overwrite: bool = False,
    ) -> WorkspaceUploadResult:
        """Atomically stream one regular file into an engagement workspace."""

        lock = self._upload_locks.setdefault(engagement_id, asyncio.Lock())
        async with lock:
            return await self._upload_locked(
                engagement_id, path, chunks, overwrite=overwrite
            )

    async def _upload_locked(
        self,
        engagement_id: str,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        overwrite: bool,
    ) -> WorkspaceUploadResult:
        """Write an upload while serializing only other API uploads."""

        self.store.get(Engagement, engagement_id)
        relative = _relative_parts(path, require_value=True)
        parent = self._open_directory(engagement_id, relative[:-1])
        temporary_name = f".nebula-upload-{uuid4().hex}.tmp"
        descriptor: int | None = None
        size = 0
        digest = hashlib.sha256()
        replaced = False
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > WORKSPACE_MAX_FILE_BYTES:
                    raise ExecutionServiceError(
                        "workspace_file_limit",
                        f"workspace uploads may not exceed {WORKSPACE_MAX_FILE_BYTES} bytes",
                        status_code=413,
                    )
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            try:
                destination = os.stat(
                    relative[-1], dir_fd=parent, follow_symlinks=False
                )
            except FileNotFoundError as caught_error:
                record_caught_exception(
                    "workspace",
                    "workspace.workspace.caught_failure_003",
                    "A handled workspace operation raised an exception.",
                    caught_error,
                    stage="workspace",
                )
                destination = None
            if destination is not None:
                if not stat.S_ISREG(destination.st_mode):
                    raise ExecutionServiceError(
                        "workspace_path_invalid",
                        "upload destination must be a regular file",
                        status_code=422,
                    )
                if not overwrite:
                    raise ExecutionServiceError(
                        "workspace_file_exists",
                        "workspace file already exists; confirm overwrite to replace it",
                        status_code=409,
                    )
                replaced = True

            root = self._workspace_root(engagement_id)
            temporary_path = PurePosixPath(*relative[:-1], temporary_name).as_posix()
            allocated, entries = _workspace_usage(
                root,
                exclude={PurePosixPath(*relative).as_posix(), temporary_path},
            )
            uploaded = os.stat(temporary_name, dir_fd=parent, follow_symlinks=False)
            allocated += uploaded.st_blocks * 512
            entries += 1
            if entries > WORKSPACE_MAX_ENTRIES:
                raise ExecutionServiceError(
                    "workspace_entry_limit",
                    f"workspace may not contain more than {WORKSPACE_MAX_ENTRIES} entries",
                    status_code=413,
                )
            if allocated > WORKSPACE_MAX_BYTES:
                raise ExecutionServiceError(
                    "workspace_size_limit",
                    f"workspace may not exceed {WORKSPACE_MAX_BYTES} allocated bytes",
                    status_code=413,
                )

            if replaced:
                os.replace(
                    temporary_name,
                    relative[-1],
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
            else:
                os.link(
                    temporary_name,
                    relative[-1],
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.unlink(temporary_name, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError as exc:
            record_caught_exception(
                "workspace",
                "workspace.workspace.caught_failure_004",
                "A handled workspace operation raised an exception.",
                exc,
                stage="workspace",
            )
            raise ExecutionServiceError(
                "workspace_file_exists",
                "workspace file already exists; confirm overwrite to replace it",
                status_code=409,
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError as caught_error:
                record_caught_exception(
                    "workspace",
                    "workspace.workspace.caught_failure_005",
                    "A handled workspace operation raised an exception.",
                    caught_error,
                    stage="workspace",
                )
                pass
            os.close(parent)

        normalized = PurePosixPath(*relative).as_posix()
        result = WorkspaceUploadResult(
            engagement_id=engagement_id,
            path=normalized,
            size=size,
            sha256=digest.hexdigest(),
            overwritten=replaced,
        )
        self.store.append_operation_event(
            str(uuid4()),
            "workspace_upload",
            engagement_id,
            "workspace.uploaded",
            result.model_dump(mode="json"),
            actor_id=self.operator_id(),
            idempotency_key=f"workspace-upload:{engagement_id}:{normalized}:{result.sha256}",
        )
        return result

    def reset(
        self, engagement_id: str, request: WorkspaceResetRequest
    ) -> WorkspaceResetResult:
        engagement = self.store.get(Engagement, engagement_id)
        if request.engagement_name != engagement.name:
            raise ExecutionServiceError(
                "confirmation_mismatch",
                "engagement name does not match",
                status_code=422,
            )
        offset = 0
        while True:
            executions = self.store.list_entities(
                OperatorExecution,
                engagement_id=engagement_id,
                offset=offset,
                limit=1000,
            )
            if any(execution.status in _BUSY_STATUSES for execution in executions):
                raise ExecutionServiceError(
                    "workspace_busy",
                    "workspace cannot be reset while execution is queued or running",
                )
            if len(executions) < 1000:
                break
            offset += len(executions)
        root = self._workspace_root(engagement_id)
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            removed = _remove_directory_contents(descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        operation_id = str(uuid4())
        self.store.append_operation_event(
            operation_id,
            "workspace_reset",
            engagement.id,
            "workspace.reset",
            {"removed_entries": removed},
            actor_id=self.operator_id(),
            idempotency_key=f"workspace-reset:{operation_id}",
        )
        return WorkspaceResetResult(
            engagement_id=engagement.id, removed_entries=removed
        )

    def _workspace_root(self, engagement_id: str) -> Path:
        root = self.tool_platform.workspace_for(engagement_id)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def _open_directory(self, engagement_id: str, parts: tuple[str, ...]) -> int:
        root = self._workspace_root(engagement_id)
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in parts:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except (OSError, ValueError) as exc:
            record_caught_exception(
                "workspace",
                "workspace.workspace.caught_failure_006",
                "A handled workspace operation raised an exception.",
                exc,
                stage="workspace",
            )
            os.close(descriptor)
            raise ExecutionServiceError(
                "workspace_path_invalid",
                "workspace directory is missing, invalid, or a symlink",
                status_code=404,
            ) from exc

    def _open_regular(
        self, engagement_id: str, parts: tuple[str, ...]
    ) -> tuple[BinaryIO, os.stat_result]:
        parent = self._open_directory(engagement_id, parts[:-1])
        try:
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            record_caught_exception(
                "workspace",
                "workspace.workspace.caught_failure_007",
                "A handled workspace operation raised an exception.",
                exc,
                stage="workspace",
            )
            raise ExecutionServiceError(
                "workspace_path_invalid",
                "workspace file is missing, invalid, or a symlink",
                status_code=404,
            ) from exc
        finally:
            os.close(parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ExecutionServiceError(
                "workspace_path_invalid",
                "workspace path is not a regular file",
                status_code=422,
            )
        return os.fdopen(descriptor, "rb"), metadata


def _relative_parts(path: str, *, require_value: bool = False) -> tuple[str, ...]:
    if "\x00" in path or "\\" in path:
        raise ExecutionServiceError(
            "workspace_path_invalid",
            "workspace paths must use safe POSIX syntax",
            status_code=422,
        )
    candidate = PurePosixPath(path)
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if candidate.is_absolute() or any(part == ".." for part in parts):
        raise ExecutionServiceError(
            "workspace_path_invalid",
            "workspace path escapes /workspace",
            status_code=422,
        )
    if require_value and not parts:
        raise ExecutionServiceError(
            "workspace_path_invalid", "workspace file path is required", status_code=422
        )
    return parts


def _remove_directory_contents(descriptor: int) -> int:
    removed = 0
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                removed += _remove_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)
        removed += 1
    return removed


def _workspace_usage(root: Path, *, exclude: set[str]) -> tuple[int, int]:
    allocated = 0
    entries = 0
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            metadata = path.lstat()
            entries += 1
            allocated += metadata.st_blocks * 512
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
    return allocated, entries


__all__ = [
    "MAX_PREVIEW_BYTES",
    "WorkspaceDownload",
    "WorkspaceEntry",
    "WorkspaceListing",
    "WorkspacePreview",
    "WorkspacePromotionRequest",
    "WorkspaceResetRequest",
    "WorkspaceResetResult",
    "WorkspaceUploadResult",
    "WorkspaceService",
]
