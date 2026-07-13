"""Ephemeral human-operated terminals in the fixed official Kali container."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import AsyncIterator, Callable, Literal
from uuid import uuid4

from pydantic import Field

from .domain import (
    Engagement,
    ExecutionLimitsSnapshot,
    NebulaModel,
    OperationEvent,
    OperatorExecution,
    OperatorExecutionStatus,
    RunnerIsolation,
    RunnerRuntime,
    utc_now,
)
from .executions import (
    ExecutionService,
    _WorkspaceLimitError,
    _assert_workspace_limits,
    _digest_json,
)
from .sandbox import (
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxError,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRootFilesystem,
    SandboxTerminalProcess,
    SandboxWorkspaceAccess,
)
from .storage import NebulaStore
from .tool_platform import (
    DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE,
    HumanTerminalRuntimeResolution,
    ToolPlatform,
    ToolPlatformError,
)

PREVIEW_TTL_SECONDS = 300
TICKET_TTL_SECONDS = 60
MAX_TERMINAL_INPUT_BYTES = 1024 * 1024
TERMINAL_OUTPUT_CHUNK_BYTES = 32_768
TERMINAL_MAX_DURATION_SECONDS = 30 * 60
TERMINAL_IDLE_TIMEOUT_SECONDS = 15 * 60
TERMINAL_COMMAND = ("--noprofile", "--norc", "-i")


class ContainerTerminalError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class ContainerTerminalPreflightRequest(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    columns: int = Field(default=100, ge=1, le=1_000)
    rows: int = Field(default=30, ge=1, le=1_000)


class ContainerTerminalStartRequest(ContainerTerminalPreflightRequest):
    preview_token: str = Field(min_length=1, max_length=65_536)
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_idempotency_key: str = Field(min_length=1, max_length=300)


class ContainerTerminalRuntimeSnapshot(NebulaModel):
    source_image: str = Field(min_length=1, max_length=1_000)
    image: str = Field(min_length=1, max_length=1_000)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    interpreter: str = Field(min_length=1, max_length=500)
    arguments: list[str] = Field(default_factory=list, max_length=32)
    runner_profile_id: str = Field(min_length=1, max_length=200)
    runner_profile_revision: int = Field(ge=1)
    runner_runtime: RunnerRuntime
    runner_isolation: RunnerIsolation
    runner_executable: str = Field(min_length=1, max_length=2_048)
    runner_platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    runner_context: str | None = Field(default=None, max_length=500)


class ContainerTerminalNetworkSnapshot(NebulaModel):
    mode: Literal["unrestricted"] = "unrestricted"
    runtime_network: Literal["bridge"] = "bridge"
    published_ports: list[int] = Field(default_factory=list, max_length=0)


class ContainerTerminalSecuritySnapshot(NebulaModel):
    container_user: Literal["root"] = "root"
    root_filesystem: Literal["writable"] = "writable"
    linux_capabilities: list[str] = Field(default_factory=list, max_length=0)
    no_new_privileges: bool = True
    host_network: bool = False
    runtime_socket: bool = False
    host_shell: bool = False


class ContainerTerminalCapabilities(NebulaModel):
    engagement_id: str
    ready: bool
    detail: str | None = None
    source_image: str = DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE
    network: ContainerTerminalNetworkSnapshot = Field(
        default_factory=ContainerTerminalNetworkSnapshot
    )
    security: ContainerTerminalSecuritySnapshot = Field(
        default_factory=ContainerTerminalSecuritySnapshot
    )
    workspace: str = "/workspace"
    limits: ExecutionLimitsSnapshot = Field(
        default_factory=lambda: ExecutionLimitsSnapshot(
            timeout_seconds=TERMINAL_MAX_DURATION_SECONDS
        )
    )
    idle_timeout_seconds: int = TERMINAL_IDLE_TIMEOUT_SECONDS
    fresh_container: bool = True


class ContainerTerminalPreflightResponse(NebulaModel):
    allowed: bool
    error_code: str | None = None
    detail: str
    runtime: ContainerTerminalRuntimeSnapshot | None = None
    network: ContainerTerminalNetworkSnapshot = Field(
        default_factory=ContainerTerminalNetworkSnapshot
    )
    security: ContainerTerminalSecuritySnapshot = Field(
        default_factory=ContainerTerminalSecuritySnapshot
    )
    limits: ExecutionLimitsSnapshot = Field(
        default_factory=lambda: ExecutionLimitsSnapshot(
            timeout_seconds=TERMINAL_MAX_DURATION_SECONDS
        )
    )
    workspace: str = "/workspace"
    policy_rule: str | None = None
    preview_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    preview_token: str | None = None
    expires_at: datetime | None = None
    idle_timeout_seconds: int = TERMINAL_IDLE_TIMEOUT_SECONDS
    fresh_container: bool = True


class ContainerTerminalStartResponse(NebulaModel):
    session_id: str
    websocket_ticket: str
    ticket_expires_at: datetime
    websocket_path: str


@dataclass(frozen=True)
class _PreparedTerminal:
    resolution: HumanTerminalRuntimeResolution
    runtime: ContainerTerminalRuntimeSnapshot
    network: ContainerTerminalNetworkSnapshot
    security: ContainerTerminalSecuritySnapshot
    sandbox_request: SandboxRequest
    policy_rule: str
    policy_detail: str


@dataclass
class _TerminalReservation:
    id: str
    request: ContainerTerminalPreflightRequest
    request_fingerprint: str
    preview_fingerprint: str
    operator_id: str
    websocket_ticket: str
    ticket_expires_at: datetime
    created_at: datetime
    workspace_lock: asyncio.Lock
    state: Literal["pending", "claimed", "running"] = "pending"
    process: SandboxTerminalProcess | None = None
    expiry_task: asyncio.Task[None] | None = None
    last_activity: float = 0.0


class ContainerTerminalService:
    """Owns short-lived terminal review, tickets, capacity, and cleanup."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        tool_platform: ToolPlatform | None,
        execution_service: ExecutionService | None = None,
        operator_id: Callable[[], str] | None = None,
        max_active: int = 2,
    ) -> None:
        if not 1 <= max_active <= 32:
            raise ValueError("terminal concurrency must be between 1 and 32")
        self.store = store
        self.tool_platform = tool_platform
        self.execution_service = execution_service
        self.operator_id = operator_id or (lambda: "local-operator")
        self.max_active = max_active
        self._preview_secret = os.urandom(32)
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _TerminalReservation] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._starting_engagements: set[str] = set()
        self._shutting_down = False

    async def startup(self) -> None:
        if self.tool_platform is not None:
            await self.tool_platform.cleanup_operator_terminals()
        self._recover_interrupted_events()

    def bind_execution_service(self, service: ExecutionService) -> None:
        if service.store is not self.store:
            raise ValueError("execution service must share the terminal store")
        if self._sessions:
            raise ValueError("cannot bind execution service after terminal startup")
        self.execution_service = service

    def workspace_lock(self, engagement_id: str) -> asyncio.Lock:
        if self.execution_service is not None:
            return self.execution_service.engagement_lock(engagement_id)
        return self._workspace_locks.setdefault(engagement_id, asyncio.Lock())

    async def engagement_active(self, engagement_id: str) -> bool:
        async with self._lock:
            return engagement_id in self._starting_engagements or any(
                session.request.engagement_id == engagement_id
                for session in self._sessions.values()
            )

    @asynccontextmanager
    async def guard_workspace_operation(
        self, engagement_id: str
    ) -> AsyncIterator[None]:
        """Serialize reset with terminals and disposable code executions."""

        async with self._lock:
            if engagement_id in self._starting_engagements or any(
                session.request.engagement_id == engagement_id
                for session in self._sessions.values()
            ):
                raise ContainerTerminalError(
                    "workspace_busy",
                    "workspace cannot be reset while a terminal is pending or running",
                )
            self._starting_engagements.add(engagement_id)
        lock = self.workspace_lock(engagement_id)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._lock:
                self._starting_engagements.discard(engagement_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._lock:
            session_ids = list(self._sessions)
        await asyncio.gather(
            *(
                self.finish(
                    session_id,
                    outcome="interrupted",
                    detail="Core shut down before the terminal session ended",
                )
                for session_id in session_ids
            ),
            return_exceptions=True,
        )

    def capabilities(self, engagement_id: str) -> ContainerTerminalCapabilities:
        self.store.get(Engagement, engagement_id)
        ready = False
        detail: str | None = None
        if self.tool_platform is None:
            detail = "human terminal container execution is not configured"
        else:
            try:
                self.tool_platform.resolve_human_terminal_profile(engagement_id)
                ready = True
            except ToolPlatformError as exc:
                detail = str(exc)
        return ContainerTerminalCapabilities(
            engagement_id=engagement_id,
            ready=ready,
            detail=detail,
        )

    async def preflight(
        self, request: ContainerTerminalPreflightRequest
    ) -> ContainerTerminalPreflightResponse:
        try:
            response, _prepared = await self._create_preview(request)
            return response
        except ContainerTerminalError as exc:
            return ContainerTerminalPreflightResponse(
                allowed=False,
                error_code=exc.code,
                detail=exc.detail,
            )
        except ToolPlatformError as exc:
            return ContainerTerminalPreflightResponse(
                allowed=False,
                error_code="runtime_unavailable",
                detail=str(exc),
            )

    async def start(
        self, request: ContainerTerminalStartRequest
    ) -> ContainerTerminalStartResponse:
        base = ContainerTerminalPreflightRequest.model_validate(
            request.model_dump(
                exclude={
                    "preview_token",
                    "preview_fingerprint",
                    "client_idempotency_key",
                }
            )
        )
        request_fingerprint = _digest_json(base.model_dump(mode="json"))
        signed = self._verify_preview(
            request.preview_token, request.preview_fingerprint
        )
        if signed.get("request_fingerprint") != request_fingerprint:
            raise ContainerTerminalError(
                "preview_stale", "terminal preview does not match the request"
            )
        if signed.get("operator_id") != self.operator_id():
            raise ContainerTerminalError(
                "preview_stale", "active operator changed after terminal review"
            )
        preview, prepared = await self._create_preview(base)
        if not preview.allowed:
            raise ContainerTerminalError(
                preview.error_code or "policy_denied", preview.detail
            )
        if preview.preview_fingerprint != request.preview_fingerprint:
            raise ContainerTerminalError(
                "preview_stale",
                "runner, policy, target resolution, or limits changed after review",
            )
        if self._shutting_down:
            raise ContainerTerminalError(
                "runner_unavailable", "Core is shutting down", status_code=503
            )

        key = (request.engagement_id, request.client_idempotency_key)
        async with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                prior_fingerprint, prior_session_id = existing
                if prior_fingerprint != request_fingerprint:
                    raise ContainerTerminalError(
                        "idempotency_conflict",
                        "idempotency key was reused for different terminal input",
                    )
                prior = self._sessions.get(prior_session_id)
                if prior is None:
                    raise ContainerTerminalError(
                        "idempotency_conflict",
                        "that request already created a terminal session; use a new idempotency key",
                    )
                return self._start_response(prior)
            if request.engagement_id in self._starting_engagements or any(
                item.request.engagement_id == request.engagement_id
                for item in self._sessions.values()
            ):
                raise ContainerTerminalError(
                    "terminal_active",
                    "this engagement already has a pending or active terminal",
                )
            if len(self._sessions) + len(self._starting_engagements) >= self.max_active:
                raise ContainerTerminalError(
                    "terminal_capacity",
                    "container terminal capacity is currently full",
                    status_code=429,
                )
            self._starting_engagements.add(request.engagement_id)

        workspace_lock = self.workspace_lock(request.engagement_id)
        lock_acquired = False
        reservation: _TerminalReservation | None = None
        try:
            if self._execution_busy(request.engagement_id):
                raise ContainerTerminalError(
                    "workspace_busy",
                    "container terminal cannot start while code execution is queued or running",
                )
            await workspace_lock.acquire()
            lock_acquired = True
            async with self._lock:
                if any(
                    item.request.engagement_id == request.engagement_id
                    for item in self._sessions.values()
                ):
                    raise ContainerTerminalError(
                        "terminal_active",
                        "this engagement already has a pending or active terminal",
                    )
                session_id = str(uuid4())
                ticket = secrets.token_urlsafe(32)
                expires = utc_now() + timedelta(seconds=TICKET_TTL_SECONDS)
                reservation = _TerminalReservation(
                    id=session_id,
                    request=base,
                    request_fingerprint=request_fingerprint,
                    preview_fingerprint=request.preview_fingerprint,
                    operator_id=self.operator_id(),
                    websocket_ticket=ticket,
                    ticket_expires_at=expires,
                    created_at=utc_now(),
                    workspace_lock=workspace_lock,
                    last_activity=monotonic(),
                )
                self._sessions[session_id] = reservation
                self._idempotency[key] = (request_fingerprint, session_id)
                reservation.expiry_task = asyncio.create_task(
                    self._expire_ticket(session_id),
                    name=f"container-terminal-ticket-{session_id}",
                )
                lock_acquired = False
        finally:
            if lock_acquired:
                workspace_lock.release()
            async with self._lock:
                self._starting_engagements.discard(request.engagement_id)
        assert reservation is not None
        self._event(
            reservation,
            "container_terminal.pending",
            {
                "status": "pending",
                "preview_fingerprint": request.preview_fingerprint,
                "runtime": prepared.runtime.model_dump(mode="json"),
                "network": prepared.network.model_dump(mode="json"),
                "security": prepared.security.model_dump(mode="json"),
                "limits": _terminal_limits().model_dump(mode="json"),
                "workspace": "/workspace",
            },
            key="pending",
        )
        return self._start_response(reservation)

    async def claim(self, session_id: str, ticket: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ContainerTerminalError(
                    "terminal_not_found",
                    "terminal session was not found",
                    status_code=404,
                )
            if session.state != "pending":
                raise ContainerTerminalError(
                    "ticket_used", "terminal WebSocket ticket has already been used"
                )
            if session.ticket_expires_at <= utc_now():
                raise ContainerTerminalError(
                    "ticket_expired", "terminal WebSocket ticket has expired"
                )
            if not hmac.compare_digest(session.websocket_ticket, ticket):
                raise ContainerTerminalError(
                    "ticket_invalid",
                    "terminal WebSocket ticket is invalid",
                    status_code=401,
                )
            session.state = "claimed"
            if session.expiry_task is not None:
                session.expiry_task.cancel()
                session.expiry_task = None
        self._event(
            session,
            "container_terminal.claimed",
            {"status": "connecting"},
            key="claimed",
        )

    async def launch(self, session_id: str) -> SandboxTerminalProcess:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.state != "claimed":
                raise ContainerTerminalError(
                    "terminal_not_found",
                    "terminal session is not connectable",
                    status_code=404,
                )
        try:
            preview, prepared = await self._create_preview(session.request)
            if (
                not preview.allowed
                or preview.preview_fingerprint != session.preview_fingerprint
            ):
                raise ContainerTerminalError(
                    "preview_stale",
                    "runner, policy, DNS, or limits changed before container launch",
                )
            process = await prepared.resolution.runner.open_terminal(
                prepared.sandbox_request,
                container_name="nebula-terminal-" + session.id.replace("-", "")[:40],
                columns=session.request.columns,
                rows=session.request.rows,
            )
        except (SandboxError, ToolPlatformError) as exc:
            await self.finish(
                session_id,
                outcome="failed",
                detail=str(exc),
                error_code="runner_unavailable",
            )
            raise ContainerTerminalError(
                "runner_unavailable", str(exc), status_code=503
            ) from exc
        except ContainerTerminalError as exc:
            await self.finish(
                session_id,
                outcome="failed",
                detail=exc.detail,
                error_code=exc.code,
            )
            raise
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None or current.state != "claimed":
                await process.close()
                raise ContainerTerminalError(
                    "interrupted", "terminal launch was interrupted"
                )
            current.process = process
            current.state = "running"
            current.last_activity = monotonic()
            session = current
        self._event(
            session,
            "container_terminal.running",
            {
                "status": "running",
                "container_name": process.container_name,
                "workspace": "/workspace",
            },
            key="running",
        )
        return process

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_activity = monotonic()

    async def idle_seconds(self, session_id: str) -> float:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return float("inf")
            return max(0.0, monotonic() - session.last_activity)

    async def enforce_workspace_limits(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            engagement_id = session.request.engagement_id
        if self.tool_platform is None:
            raise ContainerTerminalError(
                "runner_unavailable", "human terminal execution is not configured"
            )
        try:
            await asyncio.to_thread(
                _assert_workspace_limits,
                self.tool_platform.workspace_for(engagement_id),
            )
        except _WorkspaceLimitError as exc:
            raise ContainerTerminalError("workspace_limit", str(exc)) from exc

    async def finish(
        self,
        session_id: str,
        *,
        outcome: str,
        exit_code: int | None = None,
        detail: str | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            task = session.expiry_task
            session.expiry_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        cleanup_error: Exception | None = None
        try:
            if session.process is not None:
                await session.process.close()
        except Exception as exc:
            cleanup_error = exc
        finally:
            if session.workspace_lock.locked():
                session.workspace_lock.release()
        if cleanup_error is not None and detail is None:
            detail = f"terminal cleanup reported: {cleanup_error}"
            error_code = error_code or "cleanup_failed"
        duration = max(0.0, (utc_now() - session.created_at).total_seconds())
        payload: dict[str, object] = {
            "status": outcome,
            "exit_code": exit_code,
            "error_code": error_code,
            "duration_seconds": duration,
        }
        if detail:
            payload["detail"] = detail[:1_000]
        self._event(
            session,
            "container_terminal.terminal",
            payload,
            key="terminal",
        )

    async def _expire_ticket(self, session_id: str) -> None:
        try:
            await asyncio.sleep(TICKET_TTL_SECONDS)
            await self.finish(
                session_id,
                outcome="expired",
                detail="terminal WebSocket ticket expired before use",
                error_code="ticket_expired",
            )
        except asyncio.CancelledError:
            return

    async def _create_preview(
        self, request: ContainerTerminalPreflightRequest
    ) -> tuple[ContainerTerminalPreflightResponse, _PreparedTerminal]:
        prepared = await self._prepare(request)
        binding = {
            "request_fingerprint": _digest_json(request.model_dump(mode="json")),
            "operator_id": self.operator_id(),
            "runtime": prepared.runtime.model_dump(mode="json"),
            "network": prepared.network.model_dump(mode="json"),
            "security": prepared.security.model_dump(mode="json"),
            "limits": _terminal_limits().model_dump(mode="json"),
            "workspace": "/workspace",
            "policy_rule": prepared.policy_rule,
            "fresh_container": True,
            "idle_timeout_seconds": TERMINAL_IDLE_TIMEOUT_SECONDS,
        }
        fingerprint = _digest_json(binding)
        expires = utc_now() + timedelta(seconds=PREVIEW_TTL_SECONDS)
        return (
            ContainerTerminalPreflightResponse(
                allowed=True,
                detail=prepared.policy_detail,
                runtime=prepared.runtime,
                network=prepared.network,
                security=prepared.security,
                policy_rule=prepared.policy_rule,
                preview_fingerprint=fingerprint,
                preview_token=self._sign_preview(binding, fingerprint, expires),
                expires_at=expires,
            ),
            prepared,
        )

    async def _prepare(
        self, request: ContainerTerminalPreflightRequest
    ) -> _PreparedTerminal:
        self.store.get(Engagement, request.engagement_id)
        if self.tool_platform is None:
            raise ContainerTerminalError(
                "runner_unavailable",
                "human terminal container execution is not configured",
                status_code=503,
            )
        try:
            _assert_workspace_limits(
                self.tool_platform.workspace_for(request.engagement_id)
            )
        except _WorkspaceLimitError as exc:
            raise ContainerTerminalError("workspace_limit", str(exc)) from exc
        try:
            self.tool_platform.resolve_human_terminal_profile(request.engagement_id)
        except ToolPlatformError as exc:
            raise ContainerTerminalError(
                "runner_unavailable", str(exc), status_code=503
            ) from exc
        try:
            resolution = await self.tool_platform.resolve_human_terminal_runtime(
                request.engagement_id
            )
        except ToolPlatformError as exc:
            raise ContainerTerminalError(
                "image_unavailable", str(exc), status_code=503
            ) from exc
        runtime = _runtime_snapshot(resolution)
        network = ContainerTerminalNetworkSnapshot()
        security = ContainerTerminalSecuritySnapshot()
        sandbox_request = SandboxRequest(
            image=resolution.image.resolved_reference,
            command=[runtime.interpreter, *runtime.arguments],
            workspace=resolution.workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            environment={"LANG": "C.UTF-8", "TERM": "xterm-256color"},
            network=SandboxNetwork.UNRESTRICTED,
            execution_kind=SandboxExecutionKind.HUMAN_TERMINAL,
            container_user=SandboxContainerUser.ROOT,
            root_filesystem=SandboxRootFilesystem.WRITABLE,
            limits=SandboxLimits(
                cpu_count=1,
                memory_mb=512,
                pids=128,
                timeout_seconds=TERMINAL_MAX_DURATION_SECONDS,
                output_bytes=2_000_000,
            ),
        )
        return _PreparedTerminal(
            resolution=resolution,
            runtime=runtime,
            network=network,
            security=security,
            sandbox_request=sandbox_request,
            policy_rule="human_terminal_unrestricted",
            policy_detail=(
                f"{resolution.image.detail}; human terminal has unrestricted outbound bridge networking"
            ),
        )

    def _execution_busy(self, engagement_id: str) -> bool:
        busy = {
            OperatorExecutionStatus.QUEUED,
            OperatorExecutionStatus.RUNNING,
            OperatorExecutionStatus.CANCELLING,
        }
        offset = 0
        while True:
            executions = self.store.list_entities(
                OperatorExecution,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            if any(execution.status in busy for execution in executions):
                return True
            if len(executions) < 1_000:
                return False
            offset += len(executions)

    def _recover_interrupted_events(self) -> None:
        offset = 0
        while True:
            engagements = self.store.list_entities(
                Engagement, offset=offset, limit=1_000
            )
            for engagement in engagements:
                event_offset = 0
                latest: dict[str, OperationEvent] = {}
                while True:
                    events = self.store.list_operation_events(
                        engagement.id, offset=event_offset, limit=10_000
                    )
                    for event in events:
                        if event.operation_kind == "container_terminal":
                            previous = latest.get(event.operation_id)
                            if previous is None or event.sequence > previous.sequence:
                                latest[event.operation_id] = event
                    if len(events) < 10_000:
                        break
                    event_offset += len(events)
                for operation_id, value in latest.items():
                    event = value
                    if event.event_type == "container_terminal.terminal":
                        continue
                    self.store.append_operation_event(
                        operation_id,
                        "container_terminal",
                        engagement.id,
                        "container_terminal.terminal",
                        {
                            "status": "interrupted",
                            "exit_code": None,
                            "error_code": "interrupted",
                            "detail": "Core restarted before the terminal session ended",
                        },
                        actor_id=event.actor_id,
                        idempotency_key=(f"container-terminal:{operation_id}:terminal"),
                    )
            if len(engagements) < 1_000:
                return
            offset += len(engagements)

    def _event(
        self,
        session: _TerminalReservation,
        event_type: str,
        payload: dict[str, object],
        *,
        key: str,
    ) -> None:
        self.store.append_operation_event(
            session.id,
            "container_terminal",
            session.request.engagement_id,
            event_type,
            payload,
            actor_id=session.operator_id,
            idempotency_key=f"container-terminal:{session.id}:{key}",
        )

    def _sign_preview(
        self, binding: dict[str, object], fingerprint: str, expires: datetime
    ) -> str:
        payload = json.dumps(
            {
                "binding": binding,
                "fingerprint": fingerprint,
                "expires": int(expires.timestamp()),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._preview_secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def _verify_preview(self, token: str, fingerprint: str) -> dict[str, object]:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = _unb64(encoded_payload)
            signature = _unb64(encoded_signature)
            expected = hmac.new(self._preview_secret, payload, hashlib.sha256).digest()
            decoded = json.loads(payload)
        except Exception as exc:
            raise ContainerTerminalError(
                "preview_stale", "terminal preview token is invalid"
            ) from exc
        if not hmac.compare_digest(signature, expected):
            raise ContainerTerminalError(
                "preview_stale", "terminal preview token is invalid"
            )
        if decoded.get("fingerprint") != fingerprint:
            raise ContainerTerminalError(
                "preview_stale", "terminal preview token does not match the request"
            )
        if int(decoded.get("expires", 0)) <= int(utc_now().timestamp()):
            raise ContainerTerminalError(
                "preview_stale", "terminal preview has expired"
            )
        binding = decoded.get("binding")
        if not isinstance(binding, dict) or _digest_json(binding) != fingerprint:
            raise ContainerTerminalError(
                "preview_stale", "terminal preview binding is invalid"
            )
        return binding

    @staticmethod
    def _start_response(
        session: _TerminalReservation,
    ) -> ContainerTerminalStartResponse:
        return ContainerTerminalStartResponse(
            session_id=session.id,
            websocket_ticket=session.websocket_ticket,
            ticket_expires_at=session.ticket_expires_at,
            websocket_path=f"/api/v1/container-terminals/{session.id}/ws",
        )


def _runtime_snapshot(
    resolution: HumanTerminalRuntimeResolution,
) -> ContainerTerminalRuntimeSnapshot:
    profile = resolution.profile
    return ContainerTerminalRuntimeSnapshot(
        source_image=resolution.image.source_reference,
        image=resolution.image.resolved_reference,
        image_digest=resolution.image.digest,
        interpreter="/bin/bash",
        arguments=list(TERMINAL_COMMAND),
        runner_profile_id=profile.id,
        runner_profile_revision=profile.revision,
        runner_runtime=profile.runtime,
        runner_isolation=profile.isolation,
        runner_executable=profile.executable,
        runner_platform=profile.platform,
        runner_context=profile.context,
    )


def _terminal_limits() -> ExecutionLimitsSnapshot:
    return ExecutionLimitsSnapshot(timeout_seconds=TERMINAL_MAX_DURATION_SECONDS)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "ContainerTerminalCapabilities",
    "ContainerTerminalError",
    "ContainerTerminalNetworkSnapshot",
    "ContainerTerminalPreflightRequest",
    "ContainerTerminalPreflightResponse",
    "ContainerTerminalRuntimeSnapshot",
    "ContainerTerminalSecuritySnapshot",
    "ContainerTerminalService",
    "ContainerTerminalStartRequest",
    "ContainerTerminalStartResponse",
    "MAX_TERMINAL_INPUT_BYTES",
    "TERMINAL_IDLE_TIMEOUT_SECONDS",
    "TERMINAL_MAX_DURATION_SECONDS",
    "TERMINAL_OUTPUT_CHUNK_BYTES",
]
