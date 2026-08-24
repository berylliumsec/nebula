"""Symlink-safe engagement workspace browsing, promotion, and reset."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import errno
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shlex
import shutil
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, field_validator

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
from .runtime_platform import RuntimePlatform

MAX_PREVIEW_BYTES = 256 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_SCANNED_FILES = 5_000
MAX_SOURCE_CONTROL_FILES = 500
MAX_SOURCE_CONTROL_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_CONTROL_DIFF_BYTES = 512 * 1024
SOURCE_CONTROL_TIMEOUT_SECONDS = 8.0
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


class WorkspaceSearchMatch(NebulaModel):
    path: str
    kind: Literal["path", "content"]
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    preview: str = Field(default="", max_length=500)


class WorkspaceSearchResult(NebulaModel):
    engagement_id: str
    query: str
    mode: Literal["files", "text"]
    matches: list[WorkspaceSearchMatch]
    scanned_files: int = Field(ge=0)
    truncated: bool = False


class WorkspaceTask(NebulaModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str = Field(min_length=1, max_length=300)
    command: str = Field(min_length=1, max_length=20_000)
    kind: Literal["test", "build", "run", "lint", "custom"]
    source: Literal["package.json", "Makefile", "pytest", "go.mod", "Cargo.toml"]
    detail: str = Field(max_length=1_000)
    path: str | None = Field(default=None, max_length=4096)


class WorkspaceTaskList(NebulaModel):
    engagement_id: str
    tasks: list[WorkspaceTask] = Field(default_factory=list, max_length=300)
    scanned_entries: int = Field(ge=0)
    truncated: bool = False


class SourceControlFile(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    index_status: Literal[
        "unmodified", "modified", "added", "deleted", "renamed", "copied",
        "unmerged", "untracked", "ignored", "unknown",
    ]
    worktree_status: Literal[
        "unmodified", "modified", "added", "deleted", "renamed", "copied",
        "unmerged", "untracked", "ignored", "unknown",
    ]
    original_path: str | None = Field(default=None, max_length=4096)


class SourceControlStatus(NebulaModel):
    engagement_id: str
    state: Literal["ready", "not_repository", "unavailable"]
    branch: str | None = Field(default=None, max_length=500)
    head: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    files: list[SourceControlFile] = Field(default_factory=list)
    truncated: bool = False
    detail: str = Field(max_length=2_000)


class SourceControlDiff(NebulaModel):
    engagement_id: str
    path: str = Field(min_length=1, max_length=4096)
    staged: bool = False
    text: str
    truncated: bool = False
    head: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")


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


class WorkspaceResetStatus(NebulaModel):
    engagement_id: str
    can_reset: bool
    active_terminal_count: int = Field(ge=0)
    active_execution_count: int = Field(ge=0)
    reason_code: Literal["workspace_busy", "linked_workspace"] | None = None
    detail: str


class WorkspaceRenameRequest(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    new_name: str = Field(min_length=1, max_length=255)

    @field_validator("new_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if (
            not value.strip()
            or "/" in value
            or "\\" in value
            or "\0" in value
            or value in {".", ".."}
        ):
            raise ValueError("new name must be one non-empty workspace path segment")
        return value


class WorkspaceMutationResult(NebulaModel):
    engagement_id: str
    path: str
    previous_path: str | None = None


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
        tool_platform: RuntimePlatform,
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

    def tasks(self, engagement_id: str) -> WorkspaceTaskList:
        """Discover declarative project tasks without evaluating project code."""
        self.store.get(Engagement, engagement_id)
        tasks: list[WorkspaceTask] = []

        def add(
            label: str,
            command: str,
            kind: str,
            source: str,
            detail: str,
            path: str | None = None,
        ) -> None:
            identity = hashlib.sha256(
                f"{source}\0{path or ''}\0{command}".encode()
            ).hexdigest()
            tasks.append(
                WorkspaceTask(
                    id=identity,
                    label=label,
                    command=command,
                    kind=kind,
                    source=source,
                    detail=detail,
                    path=path,
                )
            )

        package = self._read_task_manifest(engagement_id, "package.json")
        if package is not None:
            try:
                parsed = json.loads(package)
            except json.JSONDecodeError:
                parsed = None
            scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
            if isinstance(scripts, dict):
                for name, value in list(scripts.items())[:200]:
                    if (
                        not isinstance(name, str)
                        or not isinstance(value, str)
                        or not name
                        or len(name) > 200
                    ):
                        continue
                    kind = (
                        "test"
                        if name == "test" or name.startswith("test:")
                        else "build"
                        if name == "build" or name.startswith("build:")
                        else "lint"
                        if name == "lint" or name.startswith("lint:")
                        else "run"
                        if name in {"start", "dev"}
                        else "custom"
                    )
                    add(
                        f"npm: {name}",
                        f"npm run {shlex.quote(name)}",
                        kind,
                        "package.json",
                        value[:1_000],
                    )

        makefile = self._read_task_manifest(engagement_id, "Makefile")
        if makefile is not None:
            for target in re.findall(
                r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)", makefile
            ):
                if target.startswith(".") or any(
                    task.command == f"make {target}" for task in tasks
                ):
                    continue
                kind = (
                    "test"
                    if "test" in target
                    else "build"
                    if target in {"all", "build", "dist", "release"}
                    else "lint"
                    if target in {"lint", "check"}
                    else "custom"
                )
                add(
                    f"make: {target}",
                    f"make {target}",
                    kind,
                    "Makefile",
                    "Declared Make target",
                )

        root = self._workspace_root(engagement_id)
        scanned = 0
        truncated = False
        pytest_files: list[str] = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [
                name
                for name in names
                if name
                not in {
                    ".git",
                    "node_modules",
                    ".venv",
                    "venv",
                    "dist",
                    "build",
                    "__pycache__",
                }
                and not (Path(directory) / name).is_symlink()
            ]
            for filename in files:
                scanned += 1
                if scanned > 5_000:
                    truncated = True
                    break
                candidate = Path(directory) / filename
                if candidate.is_symlink():
                    continue
                relative = candidate.relative_to(root).as_posix()
                if (
                    filename.startswith("test_") or filename.endswith("_test.py")
                ) and filename.endswith(".py"):
                    pytest_files.append(relative)
            if truncated:
                break
        if pytest_files:
            add(
                "pytest: all discovered tests",
                "python -m pytest",
                "test",
                "pytest",
                f"{len(pytest_files)} test files discovered",
            )
            for path in sorted(pytest_files)[:100]:
                add(
                    f"pytest: {path}",
                    f"python -m pytest {shlex.quote(path)}",
                    "test",
                    "pytest",
                    "Run this discovered test file",
                    path,
                )
        if self._read_task_manifest(engagement_id, "go.mod") is not None:
            add(
                "Go: test workspace",
                "go test ./...",
                "test",
                "go.mod",
                "Go module test suite",
            )
        if self._read_task_manifest(engagement_id, "Cargo.toml") is not None:
            add(
                "Cargo: test workspace",
                "cargo test",
                "test",
                "Cargo.toml",
                "Cargo workspace test suite",
            )
        return WorkspaceTaskList(
            engagement_id=engagement_id,
            tasks=tasks[:300],
            scanned_entries=scanned,
            truncated=truncated or len(tasks) > 300,
        )

    def _read_task_manifest(self, engagement_id: str, path: str) -> str | None:
        try:
            stream, metadata = self._open_regular(engagement_id, (path,))
        except ExecutionServiceError as exc:
            if exc.status_code == 404:
                return None
            raise
        with stream:
            if metadata.st_size > 512 * 1024:
                return None
            payload = stream.read(512 * 1024 + 1)
        if len(payload) > 512 * 1024 or b"\0" in payload:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return None

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

    def search(
        self,
        engagement_id: str,
        query: str,
        *,
        mode: Literal["files", "text"] = "files",
        path: str = "",
        limit: int = 100,
    ) -> WorkspaceSearchResult:
        """Search one workspace without following links or escaping its root."""

        self.store.get(Engagement, engagement_id)
        normalized_query = query.strip()
        if not normalized_query:
            raise ExecutionServiceError(
                "workspace_search_invalid",
                "workspace search requires a non-empty query",
                status_code=422,
            )
        relative = _relative_parts(path)
        root_descriptor = self._open_directory(engagement_id, relative)
        needle = normalized_query.casefold()
        matches: list[WorkspaceSearchMatch] = []
        scanned_files = 0
        truncated = False
        pending: list[tuple[tuple[str, ...], int]] = [(relative, root_descriptor)]

        try:
            while pending and not truncated:
                directory_parts, descriptor = pending.pop()
                try:
                    with os.scandir(descriptor) as entries:
                        rows = sorted(entries, key=lambda entry: entry.name.casefold())
                    for entry in rows:
                        try:
                            metadata = os.stat(
                                entry.name, dir_fd=descriptor, follow_symlinks=False
                            )
                        except OSError:
                            # diagnostic-expected: an entry can disappear while a live
                            # Terminal is mutating the same workspace.
                            continue
                        entry_parts = (*directory_parts, entry.name)
                        entry_path = PurePosixPath(*entry_parts).as_posix()
                        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                            metadata.st_mode
                        ):
                            try:
                                child = os.open(
                                    entry.name,
                                    os.O_RDONLY
                                    | os.O_DIRECTORY
                                    | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=descriptor,
                                )
                            except OSError:
                                # diagnostic-expected: skip raced or unreadable folders.
                                continue
                            pending.append((entry_parts, child))
                            continue
                        if not stat.S_ISREG(metadata.st_mode):
                            continue
                        scanned_files += 1
                        if scanned_files > MAX_SEARCH_SCANNED_FILES:
                            truncated = True
                            break
                        if mode == "files":
                            if needle in entry_path.casefold():
                                matches.append(
                                    WorkspaceSearchMatch(
                                        path=entry_path,
                                        kind="path",
                                        preview=entry_path,
                                    )
                                )
                        elif metadata.st_size <= MAX_SEARCH_FILE_BYTES:
                            file_descriptor: int | None = None
                            try:
                                file_descriptor = os.open(
                                    entry.name,
                                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=descriptor,
                                )
                                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                                    continue
                                with os.fdopen(file_descriptor, "rb") as stream:
                                    file_descriptor = None
                                    payload = stream.read(MAX_SEARCH_FILE_BYTES + 1)
                                if len(payload) > MAX_SEARCH_FILE_BYTES or b"\x00" in payload:
                                    continue
                                content = payload.decode("utf-8", errors="strict")
                            except (OSError, UnicodeDecodeError):
                                # diagnostic-expected: binary, invalid UTF-8, or raced
                                # files are omitted from bounded text search.
                                continue
                            finally:
                                if file_descriptor is not None:
                                    os.close(file_descriptor)
                            for line_number, line in enumerate(content.splitlines(), 1):
                                column = line.casefold().find(needle)
                                if column < 0:
                                    continue
                                matches.append(
                                    WorkspaceSearchMatch(
                                        path=entry_path,
                                        kind="content",
                                        line=line_number,
                                        column=column + 1,
                                        preview=line.strip()[:500],
                                    )
                                )
                                if len(matches) >= limit:
                                    break
                        if len(matches) >= limit:
                            truncated = True
                            break
                finally:
                    os.close(descriptor)
        finally:
            for _parts, descriptor in pending:
                os.close(descriptor)

        return WorkspaceSearchResult(
            engagement_id=engagement_id,
            query=normalized_query,
            mode=mode,
            matches=matches[:limit],
            scanned_files=min(scanned_files, MAX_SEARCH_SCANNED_FILES),
            truncated=truncated,
        )

    async def source_control_status(self, engagement_id: str) -> SourceControlStatus:
        """Return bounded Git status without executing repository-configured helpers."""

        self.store.get(Engagement, engagement_id)
        root = self._workspace_root(engagement_id).resolve(strict=True)
        executable = shutil.which("git")
        if executable is None:
            return SourceControlStatus(
                engagement_id=engagement_id,
                state="unavailable",
                detail="Git is not installed on the Nebula Core host. The workspace remains editable.",
            )

        repository = await _run_git(
            executable,
            root,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
        )
        if repository.returncode != 0:
            return SourceControlStatus(
                engagement_id=engagement_id,
                state="not_repository",
                detail="This project folder is not a Git repository. Initialize it from Nebula Terminal if source control is needed.",
            )
        try:
            repository_root = Path(
                repository.output.decode("utf-8", errors="strict").strip()
            ).resolve()
        except (OSError, UnicodeDecodeError):
            repository_root = Path("/")
        if repository_root != root:
            return SourceControlStatus(
                engagement_id=engagement_id,
                state="not_repository",
                detail="This project folder is nested inside a parent Git repository. Nebula will not expose source-control paths outside the selected project boundary.",
            )

        status_result = await _run_git(
            executable,
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
        )
        if status_result.returncode != 0:
            return SourceControlStatus(
                engagement_id=engagement_id,
                state="unavailable",
                detail=_safe_git_detail(status_result.output, "Git status is unavailable for this project."),
            )

        branch_result, head_result = await asyncio.gather(
            _run_git(executable, root, "symbolic-ref", "--quiet", "--short", "HEAD"),
            _run_git(executable, root, "rev-parse", "--verify", "--short=12", "HEAD"),
        )
        branch = _decode_git_scalar(branch_result.output) if branch_result.returncode == 0 else None
        head = _decode_git_scalar(head_result.output) if head_result.returncode == 0 else None
        files, truncated = _parse_porcelain_status(status_result.output)
        return SourceControlStatus(
            engagement_id=engagement_id,
            state="ready",
            branch=branch,
            head=head,
            files=files,
            truncated=truncated,
            detail=(
                "Working tree clean."
                if not files
                else f"{len(files)} changed path{'s' if len(files) != 1 else ''}."
            ),
        )

    async def source_control_diff(
        self, engagement_id: str, path: str, *, staged: bool = False
    ) -> SourceControlDiff:
        """Render a bounded patch while disabling external diff and textconv drivers."""

        relative = _relative_parts(path, require_value=True)
        normalized = PurePosixPath(*relative).as_posix()
        status = await self.source_control_status(engagement_id)
        if status.state != "ready":
            raise ExecutionServiceError(
                "source_control_unavailable", status.detail, status_code=409
            )
        root = self._workspace_root(engagement_id).resolve(strict=True)
        executable = shutil.which("git")
        if executable is None:  # guarded by source_control_status; retain fail-closed behavior
            raise ExecutionServiceError(
                "source_control_unavailable",
                "Git is not installed on the Nebula Core host.",
                status_code=409,
            )
        arguments = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=3",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        if staged:
            arguments.append("--cached")
        arguments.extend(("--", normalized))
        result = await _run_git(
            executable,
            root,
            *arguments,
            output_limit=MAX_SOURCE_CONTROL_DIFF_BYTES,
        )
        if result.returncode != 0:
            raise ExecutionServiceError(
                "source_control_diff_failed",
                _safe_git_detail(result.output, "Git could not render this diff."),
                status_code=409,
            )
        return SourceControlDiff(
            engagement_id=engagement_id,
            path=normalized,
            staged=staged,
            text=result.output.decode("utf-8", errors="replace"),
            truncated=result.truncated,
            head=status.head,
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
        expected_sha256: str | None = None,
    ) -> WorkspaceUploadResult:
        """Atomically stream one regular file into an engagement workspace."""

        lock = self._upload_locks.setdefault(engagement_id, asyncio.Lock())
        async with lock:
            return await self._upload_locked(
                engagement_id,
                path,
                chunks,
                overwrite=overwrite,
                expected_sha256=expected_sha256,
            )

    async def _upload_locked(
        self,
        engagement_id: str,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        overwrite: bool,
        expected_sha256: str | None,
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
                if expected_sha256 is not None:
                    current = hashlib.sha256()
                    current_descriptor = os.open(
                        relative[-1],
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent,
                    )
                    try:
                        while payload := os.read(current_descriptor, 64 * 1024):
                            current.update(payload)
                    finally:
                        os.close(current_descriptor)
                    if not hmac.compare_digest(current.hexdigest(), expected_sha256):
                        raise ExecutionServiceError(
                            "workspace_file_changed",
                            "workspace file changed after it was opened",
                            status_code=412,
                        )
                replaced = True
            elif expected_sha256 is not None:
                raise ExecutionServiceError(
                    "workspace_file_changed",
                    "workspace file no longer exists",
                    status_code=412,
                )

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
        if engagement.workspace_path:
            raise ExecutionServiceError(
                "linked_workspace",
                "linked host folders cannot be reset from Nebula",
                status_code=409,
            )
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

    def reset_status(
        self, engagement_id: str, *, active_terminal_count: int = 0
    ) -> WorkspaceResetStatus:
        engagement = self.store.get(Engagement, engagement_id)
        if engagement.workspace_path:
            return WorkspaceResetStatus(
                engagement_id=engagement_id,
                can_reset=False,
                active_terminal_count=active_terminal_count,
                active_execution_count=0,
                reason_code="linked_workspace",
                detail="This project uses a linked host folder. Nebula will never reset or bulk-delete it.",
            )
        active_execution_count = 0
        offset = 0
        while True:
            executions = self.store.list_entities(
                OperatorExecution,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            active_execution_count += sum(
                execution.status in _BUSY_STATUSES for execution in executions
            )
            if len(executions) < 1_000:
                break
            offset += len(executions)
        can_reset = active_terminal_count == 0 and active_execution_count == 0
        if active_terminal_count:
            detail = (
                f"Stop {active_terminal_count} active Project terminal"
                f"{'s' if active_terminal_count != 1 else ''} before resetting the workspace."
            )
        elif active_execution_count:
            detail = (
                f"Wait for or cancel {active_execution_count} active reviewed execution"
                f"{'s' if active_execution_count != 1 else ''} before resetting the workspace."
            )
        else:
            detail = "No active terminal or reviewed execution is using the workspace."
        return WorkspaceResetStatus(
            engagement_id=engagement_id,
            can_reset=can_reset,
            active_terminal_count=active_terminal_count,
            active_execution_count=active_execution_count,
            reason_code=None if can_reset else "workspace_busy",
            detail=detail,
        )

    def rename(
        self, engagement_id: str, request: WorkspaceRenameRequest
    ) -> WorkspaceMutationResult:
        self.store.get(Engagement, engagement_id)
        relative = _relative_parts(request.path)
        if (
            "/" in request.new_name
            or "\\" in request.new_name
            or request.new_name in {".", ".."}
        ):
            raise ExecutionServiceError(
                "workspace_name_invalid",
                "new name must be one workspace path segment",
                status_code=422,
            )
        parent = self._open_directory(engagement_id, relative[:-1])
        try:
            os.stat(relative[-1], dir_fd=parent, follow_symlinks=False)
            try:
                os.stat(request.new_name, dir_fd=parent, follow_symlinks=False)
            except (
                FileNotFoundError
            ):  # diagnostic-expected: absence confirms the rename target is available
                pass
            else:
                raise ExecutionServiceError(
                    "workspace_file_exists",
                    "a workspace entry already has that name",
                    status_code=409,
                )
            os.rename(
                relative[-1], request.new_name, src_dir_fd=parent, dst_dir_fd=parent
            )
            os.fsync(parent)
        except FileNotFoundError as exc:
            raise ExecutionServiceError(
                "workspace_path_missing",
                "workspace entry does not exist",
                status_code=404,
            ) from exc
        finally:
            os.close(parent)
        path = PurePosixPath(*relative[:-1], request.new_name).as_posix()
        result = WorkspaceMutationResult(
            engagement_id=engagement_id, path=path, previous_path=request.path
        )
        self._record_mutation(engagement_id, "renamed", result)
        return result

    def delete(self, engagement_id: str, path: str) -> WorkspaceMutationResult:
        self.store.get(Engagement, engagement_id)
        relative = _relative_parts(path)
        parent = self._open_directory(engagement_id, relative[:-1])
        try:
            metadata = os.stat(relative[-1], dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    os.rmdir(relative[-1], dir_fd=parent)
                except OSError as exc:
                    if exc.errno == errno.ENOTEMPTY:
                        raise ExecutionServiceError(
                            "workspace_directory_not_empty",
                            "only empty workspace directories can be deleted",
                            status_code=409,
                        ) from exc
                    raise
            else:
                os.unlink(relative[-1], dir_fd=parent)
            os.fsync(parent)
        except FileNotFoundError as exc:
            raise ExecutionServiceError(
                "workspace_path_missing",
                "workspace entry does not exist",
                status_code=404,
            ) from exc
        finally:
            os.close(parent)
        result = WorkspaceMutationResult(engagement_id=engagement_id, path=path)
        self._record_mutation(engagement_id, "deleted", result)
        return result

    def _record_mutation(
        self, engagement_id: str, action: str, result: WorkspaceMutationResult
    ) -> None:
        operation_id = str(uuid4())
        self.store.append_operation_event(
            operation_id,
            f"workspace_{action}",
            engagement_id,
            f"workspace.{action}",
            result.model_dump(mode="json"),
            actor_id=self.operator_id(),
            idempotency_key=f"workspace-{action}:{operation_id}",
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


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    output: bytes
    truncated: bool = False


async def _run_git(
    executable: str,
    root: Path,
    *arguments: str,
    output_limit: int = MAX_SOURCE_CONTROL_OUTPUT_BYTES,
) -> _GitResult:
    """Run a bounded, non-interactive Git query without a shell or global config."""

    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    }
    command = (
        executable,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "commit.gpgSign=false",
        "-C",
        str(root),
        *arguments,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
    except OSError as exc:
        return _GitResult(127, str(exc).encode("utf-8", errors="replace"))

    output = bytearray()

    async def consume() -> bool:
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            remaining = output_limit + 1 - len(output)
            output.extend(chunk[:remaining])
            if len(output) > output_limit:
                return True
        return False

    try:
        truncated = await asyncio.wait_for(consume(), SOURCE_CONTROL_TIMEOUT_SECONDS)
        if truncated and process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 1.0)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        return _GitResult(124, b"Git query timed out before it completed.")
    return _GitResult(
        0 if truncated else process.returncode or 0,
        bytes(output[:output_limit]),
        truncated,
    )


def _decode_git_scalar(payload: bytes) -> str | None:
    try:
        value = payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    return value[:500] or None


def _safe_git_detail(payload: bytes, fallback: str) -> str:
    detail = payload.decode("utf-8", errors="replace").strip()
    return detail[:2_000] or fallback


_GIT_STATUS_NAMES = {
    " ": "unmodified",
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
    "?": "untracked",
    "!": "ignored",
}


def _parse_porcelain_status(payload: bytes) -> tuple[list[SourceControlFile], bool]:
    records = payload.split(b"\0")
    files: list[SourceControlFile] = []
    truncated = False
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            truncated = True
            continue
        try:
            x = record[0:1].decode("ascii")
            y = record[1:2].decode("ascii")
            path = record[3:].decode("utf-8", errors="strict")
            _relative_parts(path, require_value=True)
        except (UnicodeDecodeError, ExecutionServiceError):
            truncated = True
            continue
        original_path: str | None = None
        if x in {"R", "C"} or y in {"R", "C"}:
            if index >= len(records):
                truncated = True
                break
            try:
                original_path = records[index].decode("utf-8", errors="strict")
                _relative_parts(original_path, require_value=True)
            except (UnicodeDecodeError, ExecutionServiceError):
                truncated = True
                original_path = None
            index += 1
        files.append(
            SourceControlFile(
                path=path,
                index_status=_GIT_STATUS_NAMES.get(x, "unknown"),
                worktree_status=_GIT_STATUS_NAMES.get(y, "unknown"),
                original_path=original_path,
            )
        )
        if len(files) >= MAX_SOURCE_CONTROL_FILES:
            truncated = index < len(records) - 1
            break
    return files, truncated


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
    "SourceControlDiff",
    "SourceControlFile",
    "SourceControlStatus",
    "WorkspaceDownload",
    "WorkspaceEntry",
    "WorkspaceListing",
    "WorkspacePreview",
    "WorkspacePromotionRequest",
    "WorkspaceMutationResult",
    "WorkspaceRenameRequest",
    "WorkspaceResetRequest",
    "WorkspaceResetResult",
    "WorkspaceResetStatus",
    "WorkspaceSearchMatch",
    "WorkspaceSearchResult",
    "WorkspaceUploadResult",
    "WorkspaceService",
]
