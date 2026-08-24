"""Bounded Debug Adapter Protocol sessions for the Nebula code workbench."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .automation_runtime import (
    ContainerRuntimeSession,
    RuntimeBackendProcess,
    SessionLaunch,
)
from .domain import utc_now
from .runtime_platform import HumanTerminalRuntimeResolution
from .sandbox import SandboxLimits, SandboxWorkspaceAccess
from .workspace import WorkspaceService


DEBUG_PROTOCOL = "nebula.debug.v1"
MAX_DAP_MESSAGE_BYTES = 1_048_576
MAX_DEBUG_DURATION_SECONDS = 3_600
MAX_DEBUG_SESSIONS = 4
_ALLOWED_REQUESTS = {
    "initialize",
    "launch",
    "setBreakpoints",
    "configurationDone",
    "threads",
    "stackTrace",
    "scopes",
    "variables",
    "continue",
    "next",
    "stepIn",
    "stepOut",
    "pause",
    "evaluate",
    "disconnect",
}


class DebuggerError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class DebugStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("path")
    @classmethod
    def python_workspace_path(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if (
            candidate.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.suffix != ".py"
        ):
            raise ValueError("debugging requires a workspace-relative Python file")
        return candidate.as_posix()

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: list[str]) -> list[str]:
        if any("\x00" in item or len(item) > 4096 for item in value):
            raise ValueError(
                "debug arguments must be bounded strings without NUL bytes"
            )
        return value


class DebugStartResponse(BaseModel):
    session_id: str
    websocket_path: str
    websocket_ticket: str
    protocol: str = DEBUG_PROTOCOL
    path: str
    source_sha256: str
    image_digest: str
    workspace_access: str = "read-only"
    network: str = "none"
    expires_at: datetime


@dataclass
class _DebugSession:
    id: str
    engagement_id: str
    path: str
    source_sha256: str
    arguments: tuple[str, ...]
    image_digest: str
    ticket: str
    expires_at: datetime
    backend: ContainerRuntimeSession
    process: RuntimeBackendProcess
    attached: bool = False


RuntimeResolver = Callable[[str], Awaitable[HumanTerminalRuntimeResolution]]


class DebugService:
    """Own isolated debugpy adapter processes and their one-use attachments."""

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        runtime_resolver: RuntimeResolver,
    ) -> None:
        self.workspace_service = workspace_service
        self.runtime_resolver = runtime_resolver
        self._sessions: dict[str, _DebugSession] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            expiry_tasks = list(self._expiry_tasks.values())
            self._expiry_tasks.clear()
        for task in expiry_tasks:
            task.cancel()
        await asyncio.gather(
            *(self._close_session(session) for session in sessions),
            return_exceptions=True,
        )

    async def start(
        self, engagement_id: str, request: DebugStartRequest
    ) -> DebugStartResponse:
        async with self._start_lock:
            async with self._lock:
                if len(self._sessions) >= MAX_DEBUG_SESSIONS:
                    raise DebuggerError(
                        "capacity_reached",
                        "Nebula already has the maximum number of active debug sessions.",
                        status_code=429,
                    )
                if any(
                    session.engagement_id == engagement_id
                    for session in self._sessions.values()
                ):
                    raise DebuggerError(
                        "project_busy",
                        "This project already has an active debug session.",
                    )
            return await self._start(engagement_id, request)

    async def _start(
        self, engagement_id: str, request: DebugStartRequest
    ) -> DebugStartResponse:
        source = self.workspace_service.download(engagement_id, request.path)
        try:
            digest = hashlib.sha256()
            while chunk := source.stream.read(64 * 1024):
                digest.update(chunk)
        finally:
            source.stream.close()
        actual_sha256 = digest.hexdigest()
        if not hmac.compare_digest(actual_sha256, request.expected_sha256):
            raise DebuggerError(
                "source_changed",
                "The saved Python file changed. Reload it before starting the debugger.",
            )

        resolution = await self.runtime_resolver(engagement_id)
        if "python3-debugpy" not in resolution.image.installed_packages:
            raise DebuggerError(
                "adapter_unavailable",
                "The prepared Kali runtime does not include the verified Python debugger.",
                status_code=503,
            )
        session_id = str(uuid4())
        backend = await ContainerRuntimeSession.start(
            SessionLaunch(
                session_id=f"debug-{session_id}",
                runner=resolution.runner,
                image=resolution.image.resolved_reference,
                workspace=resolution.workspace,
                limits=SandboxLimits(
                    cpu_count=2,
                    memory_mb=1024,
                    pids=128,
                    timeout_seconds=MAX_DEBUG_DURATION_SECONDS,
                    output_bytes=MAX_DAP_MESSAGE_BYTES,
                ),
                workspace_access=SandboxWorkspaceAccess.READ,
            )
        )
        try:
            process = await backend.run(
                f"adapter-{session_id}",
                "exec /usr/bin/python3 -m debugpy.adapter",
                ".",
            )
        except BaseException:
            await backend.close()
            raise
        ticket = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(seconds=MAX_DEBUG_DURATION_SECONDS)
        session = _DebugSession(
            id=session_id,
            engagement_id=engagement_id,
            path=request.path,
            source_sha256=actual_sha256,
            arguments=tuple(request.arguments),
            image_digest=resolution.image.digest,
            ticket=ticket,
            expires_at=expires_at,
            backend=backend,
            process=process,
        )
        async with self._lock:
            self._sessions[session_id] = session
            self._expiry_tasks[session_id] = asyncio.create_task(
                self._expire(session_id), name=f"debug-expiry-{session_id}"
            )
        return DebugStartResponse(
            session_id=session_id,
            websocket_path=f"/api/v1/debug-sessions/{session_id}/ws",
            websocket_ticket=ticket,
            path=request.path,
            source_sha256=actual_sha256,
            image_digest=resolution.image.digest,
            expires_at=expires_at,
        )

    async def attach(self, session_id: str, ticket: str) -> _DebugSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise DebuggerError(
                    "session_not_found", "Debug session was not found.", status_code=404
                )
            if session.expires_at <= utc_now():
                self._sessions.pop(session_id, None)
                expiry_task = self._expiry_tasks.pop(session_id, None)
                expired = session
            else:
                expiry_task = None
                expired = None
            if expired is None:
                if session.attached:
                    raise DebuggerError(
                        "already_attached", "Debug session is already attached."
                    )
                if not hmac.compare_digest(session.ticket, ticket):
                    raise DebuggerError(
                        "ticket_invalid",
                        "Debug session ticket is invalid.",
                        status_code=401,
                    )
                session.attached = True
                session.ticket = secrets.token_urlsafe(32)
                return session
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()
        await self._close_session(expired)
        raise DebuggerError(
            "session_expired", "Debug session expired.", status_code=410
        )

    async def close(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            expiry = self._expiry_tasks.pop(session_id, None)
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
        if session is not None:
            await self._close_session(session)

    async def _expire(self, session_id: str) -> None:
        await asyncio.sleep(MAX_DEBUG_DURATION_SECONDS)
        await self.close(session_id)

    async def send(self, session: _DebugSession, message: dict[str, Any]) -> None:
        self._validate_message(session, message)
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_DAP_MESSAGE_BYTES:
            raise DebuggerError(
                "message_too_large",
                "Debug protocol message is too large.",
                status_code=413,
            )
        await session.process.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )

    async def receive(self, session: _DebugSession) -> dict[str, Any] | None:
        header = await session.process.stdout.readline()
        if not header:
            return None
        if not header.lower().startswith(b"content-length:"):
            raise DebuggerError(
                "adapter_protocol", "The debug adapter returned an invalid frame."
            )
        try:
            length = int(header.split(b":", 1)[1].strip())
        except ValueError as exc:
            raise DebuggerError(
                "adapter_protocol", "The debug adapter returned an invalid length."
            ) from exc
        if length < 2 or length > MAX_DAP_MESSAGE_BYTES:
            raise DebuggerError(
                "adapter_protocol", "The debug adapter frame exceeded its limit."
            )
        while True:
            line = await session.process.stdout.readline()
            if line in {b"\r\n", b"\n"}:
                break
            if not line or len(line) > 8192:
                raise DebuggerError(
                    "adapter_protocol", "The debug adapter returned invalid headers."
                )
        try:
            payload = json.loads(await session.process.stdout.readexactly(length))
        except (asyncio.IncompleteReadError, json.JSONDecodeError) as exc:
            raise DebuggerError(
                "adapter_protocol", "The debug adapter returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise DebuggerError(
                "adapter_protocol", "The debug adapter returned a non-object message."
            )
        return payload

    def _validate_message(
        self, session: _DebugSession, message: dict[str, Any]
    ) -> None:
        if (
            message.get("type") != "request"
            or message.get("command") not in _ALLOWED_REQUESTS
        ):
            raise DebuggerError(
                "request_denied",
                "This debug adapter request is not allowed.",
                status_code=422,
            )
        arguments = message.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise DebuggerError(
                "request_invalid",
                "Debug request arguments must be an object.",
                status_code=422,
            )
        arguments = arguments or {}
        command = message["command"]
        expected_path = f"/workspace/{session.path}"
        if command == "launch" and (
            arguments.get("program") != expected_path
            or arguments.get("cwd") != "/workspace"
            or arguments.get("args", []) != list(session.arguments)
            or "module" in arguments
            or "code" in arguments
        ):
            raise DebuggerError(
                "launch_changed",
                "The launch request does not match the reviewed file and arguments.",
                status_code=422,
            )
        if command == "setBreakpoints":
            source = arguments.get("source")
            if not isinstance(source, dict) or source.get("path") != expected_path:
                raise DebuggerError(
                    "source_outside_review",
                    "Breakpoints must target the reviewed workspace file.",
                    status_code=422,
                )

    @staticmethod
    async def _close_session(session: _DebugSession) -> None:
        try:
            await session.process.terminate()
        finally:
            await session.backend.close()
