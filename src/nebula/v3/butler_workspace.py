"""Bounded workspace file actions for an interactive project Butler."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any, AsyncIterator

from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec
from .workspace import WorkspaceRenameRequest, WorkspaceService


_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_TEXT = {"type": "string", "maxLength": 1_048_576}
_MAX_TEXT_BYTES = 1_048_576
_PAGE_CHARS = 12_000


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_SCHEMAS = {
    "nebula.workspace.list": _schema(
        {
            "path": {"type": "string", "maxLength": 4096},
            "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
        }
    ),
    "nebula.workspace.read": _schema(
        {
            "path": _PATH,
            "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
        },
        "path",
    ),
    "nebula.workspace.create_text": _schema(
        {"path": _PATH, "content": _TEXT}, "path", "content"
    ),
    "nebula.workspace.replace_text": _schema(
        {"path": _PATH, "content": _TEXT, "expected_sha256": _SHA256},
        "path",
        "content",
        "expected_sha256",
    ),
    "nebula.workspace.rename_file": _schema(
        {
            "path": _PATH,
            "new_name": {"type": "string", "minLength": 1, "maxLength": 255},
            "expected_sha256": _SHA256,
        },
        "path",
        "new_name",
        "expected_sha256",
    ),
    "nebula.workspace.delete_file": _schema(
        {"path": _PATH, "expected_sha256": _SHA256}, "path", "expected_sha256"
    ),
    "nebula.workspace.download_file": _schema({"path": _PATH}, "path"),
}

_DESCRIPTIONS = {
    "list": "Page exact entries in one selected-project workspace directory before choosing a file. Symlink entries are listed but cannot be read or changed through these actions.",
    "read": "Read up to 12,000 UTF-8 characters from a selected-project regular file and its full SHA-256. Files above 1 MiB are available through the existing bounded workspace.read tool, not Butler text editing.",
    "create_text": "Create a UTF-8 file in the selected project after explicit operator instruction. Existing paths are never overwritten. Read the exact path after an uncertain result before retrying.",
    "replace_text": "Replace one selected-project UTF-8 file after reading its full SHA-256. Core refuses a stale or missing file and reports a matching saved read-back.",
    "rename_file": "Rename a regular file within its current directory using the full SHA-256 from a current read. Core refuses stale content and reads the new path back.",
    "delete_file": "Ask the requesting browser for the same destructive confirmation as Files. Core checks the selected project and full-file SHA-256 before deletion; the browser reports read-back.",
    "download_file": "Ask the requesting browser to download a selected-project regular file. The model receives only path, size, and SHA-256; the file bytes remain on the requesting device.",
}

_READS = frozenset({"list", "read", "download_file"})


class ButlerWorkspace:
    def __init__(self, workspace: WorkspaceService, project_id: str) -> None:
        self.workspace = workspace
        self.project_id = project_id

    def _file_digest(self, path: str) -> dict[str, Any]:
        download = self.workspace.download(self.project_id, path)
        digest = hashlib.sha256()
        size = 0
        for chunk in download.chunks():
            digest.update(chunk)
            size += len(chunk)
        if size != download.size:
            raise ValueError("file changed while reading; select it again")
        return {"path": path, "size": size, "sha256": digest.hexdigest()}

    def _read(self, path: str, offset: int) -> dict[str, Any]:
        download = self.workspace.download(self.project_id, path)
        try:
            if download.size > _MAX_TEXT_BYTES:
                raise ValueError("file exceeds Butler's 1 MiB text-edit limit")
            payload = download.stream.read(_MAX_TEXT_BYTES + 1)
        finally:
            download.stream.close()
        if len(payload) > _MAX_TEXT_BYTES or len(payload) != download.size:
            raise ValueError("file changed or exceeds Butler's 1 MiB text-edit limit")
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("file is not UTF-8 text; use the device download") from exc
        if "\0" in source:
            raise ValueError("binary file cannot be edited as text")
        chunk = source[offset : offset + _PAGE_CHARS]
        return {
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "text": chunk,
            "text_length": len(source),
            "next_offset": offset + len(chunk)
            if offset + len(chunk) < len(source)
            else None,
        }

    async def _write(
        self, path: str, content: str, expected: str | None
    ) -> dict[str, Any]:
        payload = content.encode("utf-8")
        if len(payload) > _MAX_TEXT_BYTES:
            raise ValueError("UTF-8 file exceeds Butler's 1 MiB text-edit limit")

        async def chunks() -> AsyncIterator[bytes]:
            yield payload

        saved = await self.workspace.upload(
            self.project_id,
            path,
            chunks(),
            overwrite=expected is not None,
            expected_sha256=expected,
            expected_content_sha256=hashlib.sha256(payload).hexdigest(),
        )
        observed = await asyncio.to_thread(self._file_digest, saved.path)
        if observed["sha256"] != saved.sha256 or observed["size"] != saved.size:
            raise ValueError(
                "file was saved but changed before read-back; read it before retrying"
            )
        return {**observed, "overwritten": saved.overwritten}

    async def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path", "")
        if name == "list":
            listing = await asyncio.to_thread(
                self.workspace.list,
                self.project_id,
                path,
                offset=arguments.get("offset", 0),
                limit=50,
            )
            return listing.model_dump(mode="json")
        if name == "read":
            return await asyncio.to_thread(self._read, path, arguments.get("offset", 0))
        if name in {"create_text", "replace_text"}:
            return await self._write(
                path, arguments["content"], arguments.get("expected_sha256")
            )
        if name == "rename_file":
            result = await asyncio.to_thread(
                self.workspace.rename,
                self.project_id,
                WorkspaceRenameRequest(
                    path=path,
                    new_name=arguments["new_name"],
                    expected_sha256=arguments["expected_sha256"],
                ),
            )
            observed = await asyncio.to_thread(self._file_digest, result.path)
            if not hmac.compare_digest(
                observed["sha256"], arguments["expected_sha256"]
            ):
                raise ValueError("file was renamed but changed before read-back")
            return {**observed, "previous_path": path}
        observed = await asyncio.to_thread(self._file_digest, path)
        if name == "delete_file":
            if not hmac.compare_digest(
                observed["sha256"], arguments["expected_sha256"]
            ):
                raise ValueError("file changed; read its current hash before deleting")
            return {
                "state": "pending_browser_apply",
                "client_action": {
                    "kind": "workspace_delete",
                    "project_id": self.project_id,
                    "path": path,
                    "expected_sha256": observed["sha256"],
                },
            }
        if name == "download_file":
            return {
                "state": "pending_browser_apply",
                "client_action": {
                    "kind": "workspace_download",
                    "project_id": self.project_id,
                    "path": path,
                    "expected_sha256": observed["sha256"],
                    "size": observed["size"],
                },
            }
        raise ValueError("unknown workspace action")

    def actions(self) -> list[AnalysisTool]:
        actions = []
        for operation, schema in _SCHEMAS.items():
            name = operation.removeprefix("nebula.workspace.")

            async def run(
                arguments: dict[str, Any], *, selected: str = name
            ) -> dict[str, Any]:
                return await self.invoke(selected, arguments)

            read_only = name in _READS
            actions.append(
                AnalysisTool(
                    ToolSpec(
                        name=operation,
                        description=_DESCRIPTIONS[name],
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=(
                            RiskClass.LOCAL_READ
                            if read_only
                            else RiskClass.WORKSPACE_WRITE
                        ),
                        filesystem_access="read" if read_only else "workspace_write",
                        idempotency=(
                            IdempotencyBehavior.SAFE
                            if read_only
                            else IdempotencyBehavior.NON_IDEMPOTENT
                        ),
                    ),
                    run,
                )
            )
        return actions
