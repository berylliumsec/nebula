"""Ephemeral human-operated terminals in the fixed official Kali container."""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    gather_diagnostic,
    record_caught_exception,
)

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from .terminal_history import (
    CapturedTerminalCommand,
    Osc633CommandParser,
    TerminalCommandHistory,
    TerminalCommandParseResult,
)
from .tool_platform import (
    DEFAULT_HUMAN_TERMINAL_SOURCE_IMAGE,
    HumanTerminalRuntimeResolution,
    ToolPlatform,
    ToolPlatformError,
)

PREVIEW_TTL_SECONDS = 300
TICKET_TTL_SECONDS = 60
TERMINAL_RECONNECT_GRACE_SECONDS = 10 * 60
TERMINAL_REPLAY_MAX_BYTES = 1024 * 1024
MAX_TERMINAL_INPUT_BYTES = 1024 * 1024
TERMINAL_OUTPUT_CHUNK_BYTES = 32_768
# Interactive terminals have no application-level lifetime cutoff. The 24-hour
# value is only the maximum representable sandbox safety limit; the watchdog
# closes sessions based on inactivity, explicit stop, or Core shutdown.
TERMINAL_MAX_DURATION_SECONDS = 24 * 60 * 60
TERMINAL_IDLE_TIMEOUT_SECONDS = 30 * 60
TERMINAL_COMMAND = ("--noprofile", "--norc", "-i")
LOGGER = logging.getLogger(__name__)


def terminal_ps0(nonce: str) -> str:
    """Emit a nonce-bound marker after input echo and before command output."""

    return (
        "${__nebula_in_ps0:=}$(HISTCONTROL= HISTIGNORE=; shopt -s cmdhist lithist; "
        "__nebula_line=\"$(HISTTIMEFORMAT=$'\\036' builtin history 1 2>/dev/null)\"; "
        "__nebula_command=\"${__nebula_line#*$'\\036'}\"; "
        '__nebula_cwd_b64="$(printf \'%s\' "$PWD" '
        "| base64 2>/dev/null | tr -d '\\n')\"; "
        '__nebula_command_b64="$(printf \'%s\' "$__nebula_command" '
        "| base64 2>/dev/null | tr -d '\\n')\"; "
        'if [ -n "$__nebula_command" ] && [ -n "$__nebula_cwd_b64" ] '
        '&& [ -n "$__nebula_command_b64" ]; then '
        "printf '\\033]633;NebulaCommandStart;%s;%s;%s;%s\\007' "
        f'\'{nonce}\' "${{HISTCMD:-0}}" "$__nebula_cwd_b64" '
        '"$__nebula_command_b64"; fi)'
    )


def terminal_prompt_command(nonce: str) -> str:
    """Emit the matching command completion marker before drawing the prompt."""

    return (
        "__nebula_exit=$?; __nebula_in_prompt=1; "
        "HISTCONTROL=; HISTIGNORE=; "
        "shopt -s cmdhist lithist; "
        '__nebula_classifier_ok=0; case "$(trap -p DEBUG 2>/dev/null)" in '
        "*__nebula_debug*) __nebula_classifier_ok=1;; esac; "
        'if [ "${__nebula_history_ready:-0}" = 1 ] '
        '&& [ "${HISTCMD:-0}" != "${__nebula_last_histcmd:-0}" ]; then '
        "printf '\\033]633;NebulaCommandEnd;%s;%s;%s;%s\\007' "
        f'\'{nonce}\' "${{HISTCMD:-0}}" "$__nebula_exit" '
        '"$__nebula_classifier_ok"; fi; '
        '__nebula_last_histcmd="${HISTCMD:-0}"; '
        "__nebula_history_ready=1; "
        "__nebula_debug() { "
        'local __nebula_status=$? __nebula_seen="$BASH_COMMAND"; '
        'if [ "${__nebula_in_ps0+x}" = x ] '
        '&& [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then '
        'return "$__nebula_status"; fi; '
        'if [ "${__nebula_in_ps0+x}" = x ]; then unset __nebula_in_ps0; fi; '
        'if [ "${__nebula_in_prompt:-0}" != 1 ] '
        '&& [ "${__nebula_in_debug:-0}" != 1 ]; then '
        "__nebula_in_debug=1; "
        '__nebula_seen_b64="$(printf \'%s\' "$__nebula_seen" '
        "| base64 2>/dev/null | tr -d '\\n')\"; "
        'if [ -n "$__nebula_seen_b64" ]; then '
        "printf '\\033]633;NebulaCommandExec;%s;%s;%s\\007' "
        f'\'{nonce}\' "${{HISTCMD:-0}}" "$__nebula_seen_b64"; fi; '
        "unset __nebula_in_debug __nebula_seen_b64; fi; "
        'return "$__nebula_status"; }; '
        "trap '__nebula_debug' DEBUG; set -o functrace; "
        "unset __nebula_exit __nebula_classifier_ok __nebula_in_prompt"
    )


# Stable examples retained for documentation and shell-integration tests. Live
# sessions replace the nonce immediately before the sandbox process is opened.
TERMINAL_AUDIT_PREVIEW_NONCE = "nebulaauditpreview"
TERMINAL_PS0 = terminal_ps0(TERMINAL_AUDIT_PREVIEW_NONCE)
TERMINAL_PROMPT_COMMAND = terminal_prompt_command(TERMINAL_AUDIT_PREVIEW_NONCE)


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
    base_image: str = Field(min_length=1, max_length=1_000)
    base_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image: str = Field(min_length=1, max_length=1_000)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    installed_packages: list[str] = Field(min_length=1, max_length=16)
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
    installed_packages: list[str] = Field(
        default_factory=lambda: ["kali-linux-headless", "iputils-ping"],
        min_length=1,
        max_length=16,
    )
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
    reconnect_grace_seconds: int = TERMINAL_RECONNECT_GRACE_SECONDS
    replay_max_bytes: int = TERMINAL_REPLAY_MAX_BYTES
    last_sequence: int = 0


class ContainerTerminalRecoveryResponse(NebulaModel):
    active: bool
    session: ContainerTerminalStartResponse | None = None
    runtime: ContainerTerminalRuntimeSnapshot | None = None


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
    runtime: ContainerTerminalRuntimeSnapshot
    operator_id: str
    websocket_ticket: str
    ticket_expires_at: datetime | None
    start_websocket_ticket: str
    start_ticket_expires_at: datetime
    created_at: datetime
    audit_nonce: str
    state: Literal["pending", "claimed", "launching", "running"] = "pending"
    process: SandboxTerminalProcess | None = None
    expiry_task: asyncio.Task[None] | None = None
    grace_task: asyncio.Task[None] | None = None
    reader_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    audit_task: asyncio.Task[None] | None = None
    audit_queue: asyncio.Queue[CapturedTerminalCommand | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=64)
    )
    last_activity: float = 0.0
    parser: Osc633CommandParser = field(default_factory=Osc633CommandParser)
    parser_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    replay: deque["ContainerTerminalOutput"] = field(default_factory=deque)
    replay_bytes: int = 0
    next_sequence: int = 1
    attachment: "ContainerTerminalAttachment | None" = None


@dataclass(frozen=True, slots=True)
class ContainerTerminalOutput:
    sequence: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ContainerTerminalExit:
    outcome: str
    exit_code: int | None = None
    error_code: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class ContainerTerminalAttachment:
    id: str
    session_id: str
    engagement_id: str
    reconnect_ticket: str
    reconnect_grace_seconds: int
    replay_max_bytes: int
    oldest_sequence: int
    latest_sequence: int
    replay_truncated: bool
    next_sequence: int
    terminal_replay: deque[ContainerTerminalOutput] = field(default_factory=deque)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_event: ContainerTerminalExit | None = None
    terminal_event_delivered: bool = False
    terminal_finishing: bool = False
    detached: bool = False


class ContainerTerminalService:
    """Owns short-lived terminal review, tickets, capacity, and cleanup."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        tool_platform: ToolPlatform | None,
        execution_service: ExecutionService | None = None,
        command_history: TerminalCommandHistory | None = None,
        operator_id: Callable[[], str] | None = None,
        audit_nonce_factory: Callable[[], str] | None = None,
        max_active: int = 2,
        reconnect_grace_seconds: float = TERMINAL_RECONNECT_GRACE_SECONDS,
        idle_timeout_seconds: float = TERMINAL_IDLE_TIMEOUT_SECONDS,
        watchdog_interval_seconds: float = 1.0,
        replay_max_bytes: int = TERMINAL_REPLAY_MAX_BYTES,
    ) -> None:
        if not 1 <= max_active <= 32:
            raise ValueError("terminal concurrency must be between 1 and 32")
        if reconnect_grace_seconds <= 0:
            raise ValueError("terminal reconnect grace must be positive")
        if idle_timeout_seconds <= 0:
            raise ValueError("terminal idle timeout must be positive")
        if watchdog_interval_seconds <= 0:
            raise ValueError("terminal watchdog interval must be positive")
        if not 1 <= replay_max_bytes <= TERMINAL_REPLAY_MAX_BYTES:
            raise ValueError(
                f"terminal replay must be between 1 and {TERMINAL_REPLAY_MAX_BYTES} bytes"
            )
        self.store = store
        self.tool_platform = tool_platform
        self.execution_service = execution_service
        self.command_history = command_history
        self.operator_id = operator_id or (lambda: "system")
        self.audit_nonce_factory = audit_nonce_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self.max_active = max_active
        self.reconnect_grace_seconds = reconnect_grace_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.replay_max_bytes = replay_max_bytes
        self._preview_secret = os.urandom(32)
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _TerminalReservation] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._starting_engagements: set[str] = set()
        self._shutting_down = False

    async def startup(self) -> None:
        if self.command_history is not None:
            recovered = await asyncio.to_thread(self.command_history.recover_spools)
            if recovered:
                LOGGER.warning(
                    "recovered %d interrupted terminal audit spool(s)", recovered
                )
        if self.tool_platform is not None:
            await self.tool_platform.cleanup_operator_terminals()
        self._recover_interrupted_events()

    def bind_execution_service(self, service: ExecutionService) -> None:
        if service.store is not self.store:
            raise ValueError("execution service must share the terminal store")
        if self._sessions:
            raise ValueError("cannot bind execution service after terminal startup")
        self.execution_service = service

    def bind_command_history(self, history: TerminalCommandHistory) -> None:
        if history.database is not self.store.database:
            raise ValueError("command history must share the terminal database")
        if self._sessions:
            raise ValueError("cannot bind command history after terminal startup")
        self.command_history = history

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
        """Serialize workspace mutations with terminals and code executions."""

        async with self._lock:
            if engagement_id in self._starting_engagements or any(
                session.request.engagement_id == engagement_id
                for session in self._sessions.values()
            ):
                raise ContainerTerminalError(
                    "workspace_busy",
                    "workspace cannot be changed while a terminal is pending or running",
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
        await gather_diagnostic(
            *(
                self.finish(
                    session_id,
                    outcome="interrupted",
                    detail="Core shut down before the terminal session ended",
                )
                for session_id in session_ids
            ),
            feature="terminal",
            event_code="terminal.shutdown.session_failed",
            failure_message="A terminal session did not shut down cleanly.",
            stage="shutdown",
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
                record_caught_exception(
                    "terminal",
                    "terminal.container_terminal.caught_failure_001",
                    "A handled terminal operation raised an exception.",
                    exc,
                    stage="container_terminal",
                )
                detail = str(exc)
        return ContainerTerminalCapabilities(
            engagement_id=engagement_id,
            ready=ready,
            detail=detail,
            idle_timeout_seconds=int(self.idle_timeout_seconds),
        )

    async def preflight(
        self, request: ContainerTerminalPreflightRequest
    ) -> ContainerTerminalPreflightResponse:
        try:
            response, _prepared = await self._create_preview(request)
            return response
        except ContainerTerminalError as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_002",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            return ContainerTerminalPreflightResponse(
                allowed=False,
                error_code=exc.code,
                detail=exc.detail,
            )
        except ToolPlatformError as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_003",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
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

        reservation: _TerminalReservation | None = None
        try:
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
                audit_nonce = self.audit_nonce_factory()
                operator_id = self.operator_id()
                expires = utc_now() + timedelta(seconds=TICKET_TTL_SECONDS)
                parser = (
                    self.command_history.new_parser(
                        nonce=audit_nonce,
                        engagement_id=base.engagement_id,
                        session_id=session_id,
                        operator_id=operator_id,
                        runtime_image_digest=prepared.resolution.image.digest,
                        manifest_sha256=(
                            prepared.resolution.image.security_tool_manifest_sha256
                        ),
                        default_tools=prepared.resolution.image.security_tools,
                    )
                    if self.command_history is not None
                    else Osc633CommandParser(nonce=audit_nonce)
                )
                reservation = _TerminalReservation(
                    id=session_id,
                    request=base,
                    request_fingerprint=request_fingerprint,
                    preview_fingerprint=request.preview_fingerprint,
                    runtime=prepared.runtime,
                    operator_id=operator_id,
                    websocket_ticket=ticket,
                    ticket_expires_at=expires,
                    start_websocket_ticket=ticket,
                    start_ticket_expires_at=expires,
                    created_at=utc_now(),
                    audit_nonce=audit_nonce,
                    last_activity=monotonic(),
                    parser=parser,
                )
                self._sessions[session_id] = reservation
                self._idempotency[key] = (request_fingerprint, session_id)
                reservation.expiry_task = create_diagnostic_task(
                    self._expire_ticket(session_id),
                    feature="terminal",
                    event_code="terminal.ticket_expiry",
                    failure_message="Terminal ticket expiry supervision failed.",
                    name=f"container-terminal-ticket-{session_id}",
                )
        finally:
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

    async def recover(
        self,
        engagement_id: str,
    ) -> ContainerTerminalRecoveryResponse:
        """Issue a fresh one-use ticket for this Project's active terminal.

        Recovery never creates a process or extends a running session's
        disconnect grace. Repeated authenticated calls rotate the outstanding
        ticket, so a response lost during a webview restart can be retried
        without retaining multiple valid credentials. If the old route has
        not detached yet, the fresh ticket remains outstanding while attach()
        rejects a second live socket; the bounded client retry can claim it
        after the old transport closes.
        """

        self.store.get(Engagement, engagement_id)
        expired_session_id: str | None = None
        async with self._lock:
            session = next(
                (
                    item
                    for item in self._sessions.values()
                    if item.request.engagement_id == engagement_id
                ),
                None,
            )
            if session is None:
                return ContainerTerminalRecoveryResponse(active=False)
            if session.state == "claimed":
                raise ContainerTerminalError(
                    "terminal_connecting",
                    "terminal is already being connected",
                )
            now = utc_now()
            if (
                session.state == "running"
                and session.attachment is None
                and session.ticket_expires_at is not None
                and session.ticket_expires_at <= now
            ):
                expired_session_id = session.id
            else:
                return self._recover_session_locked(session, now)
        assert expired_session_id is not None
        await self.finish(
            expired_session_id,
            outcome="reconnect_timeout",
            detail="terminal reconnect grace expired",
            error_code="reconnect_timeout",
        )
        return ContainerTerminalRecoveryResponse(active=False)

    def _recover_session_locked(
        self,
        session: _TerminalReservation,
        now: datetime,
    ) -> ContainerTerminalRecoveryResponse:
        if session.state == "pending":
            ticket_expires_at = now + timedelta(seconds=TICKET_TTL_SECONDS)
            self._cancel_session_task_locked(session, "expiry_task")
            session.expiry_task = create_diagnostic_task(
                self._expire_ticket(session.id),
                feature="terminal",
                event_code="terminal.ticket_expiry",
                failure_message="Terminal ticket expiry supervision failed.",
                name=f"container-terminal-ticket-{session.id}",
            )
        elif session.attachment is None and session.ticket_expires_at is not None:
            # Preserve the original disconnect deadline. Merely viewing a
            # route must not keep an unattended container alive forever.
            ticket_expires_at = session.ticket_expires_at
        else:
            ticket_expires_at = now + timedelta(seconds=self.reconnect_grace_seconds)

        ticket = secrets.token_urlsafe(32)
        session.websocket_ticket = ticket
        session.ticket_expires_at = ticket_expires_at
        if session.attachment is not None:
            session.attachment.reconnect_ticket = ticket
        elif session.state == "running" and session.grace_task is None:
            session.grace_task = create_diagnostic_task(
                self._expire_reconnect_grace(session.id),
                feature="terminal",
                event_code="terminal.reconnect_grace",
                failure_message="Terminal reconnect-grace supervision failed.",
                name=f"container-terminal-grace-{session.id}",
            )

        recovered = ContainerTerminalStartResponse(
            session_id=session.id,
            websocket_ticket=ticket,
            ticket_expires_at=ticket_expires_at,
            websocket_path=f"/api/v1/container-terminals/{session.id}/ws",
            reconnect_grace_seconds=max(1, int(self.reconnect_grace_seconds)),
            replay_max_bytes=self.replay_max_bytes,
            # A recovered view owns a new terminal surface. Replaying from zero
            # restores as much bounded viewport history as Core still holds.
            last_sequence=0,
        )
        return ContainerTerminalRecoveryResponse(
            active=True,
            session=recovered,
            runtime=session.runtime,
        )

    async def claim(self, session_id: str, ticket: str) -> str:
        """Compatibility seam for direct service users.

        WebSocket callers should use :meth:`attach`, which atomically claims a
        ticket, launches when needed, and establishes replay state.
        """

        async with self._lock:
            session = self._require_session_locked(session_id)
            if session.state != "pending":
                raise ContainerTerminalError(
                    "ticket_used", "terminal WebSocket ticket has already been used"
                )
            self._validate_ticket_locked(session, ticket)
            session.state = "claimed"
            self._rotate_ticket_locked(session)
            self._cancel_session_task_locked(session, "expiry_task")
        self._event(
            session,
            "container_terminal.claimed",
            {"status": "connecting"},
            key="claimed",
        )
        return session.request.engagement_id

    async def launch(self, session_id: str) -> SandboxTerminalProcess:
        """Compatibility seam that launches a previously claimed session."""

        return await self._launch_session(session_id, expected_state="claimed")

    async def attach(
        self,
        session_id: str,
        ticket: str,
        *,
        after_sequence: int = 0,
    ) -> ContainerTerminalAttachment:
        if after_sequence < 0:
            raise ContainerTerminalError(
                "invalid_sequence", "terminal replay sequence cannot be negative"
            )
        launch_required = False
        async with self._lock:
            session = self._require_session_locked(session_id)
            if session.attachment is not None:
                raise ContainerTerminalError(
                    "terminal_attached",
                    "terminal already has an active WebSocket attachment",
                )
            if session.state not in {"pending", "running"}:
                raise ContainerTerminalError(
                    "terminal_connecting",
                    "terminal is already being connected",
                )
            self._validate_ticket_locked(session, ticket)
            latest_sequence = session.next_sequence - 1
            if after_sequence > latest_sequence:
                raise ContainerTerminalError(
                    "invalid_sequence",
                    "terminal replay sequence is newer than available output",
                )
            oldest_sequence = (
                session.replay[0].sequence if session.replay else session.next_sequence
            )
            next_sequence = max(after_sequence + 1, oldest_sequence)
            reconnect_ticket = secrets.token_urlsafe(32)
            attachment = ContainerTerminalAttachment(
                id=str(uuid4()),
                session_id=session.id,
                engagement_id=session.request.engagement_id,
                reconnect_ticket=reconnect_ticket,
                reconnect_grace_seconds=max(1, int(self.reconnect_grace_seconds)),
                replay_max_bytes=self.replay_max_bytes,
                oldest_sequence=oldest_sequence,
                latest_sequence=latest_sequence,
                replay_truncated=(after_sequence < oldest_sequence - 1),
                next_sequence=next_sequence,
            )
            session.websocket_ticket = reconnect_ticket
            session.ticket_expires_at = None
            session.attachment = attachment
            self._cancel_session_task_locked(session, "expiry_task")
            self._cancel_session_task_locked(session, "grace_task")
            if session.state == "pending":
                session.state = "launching"
                launch_required = True
        if launch_required:
            self._event(
                session,
                "container_terminal.claimed",
                {"status": "connecting"},
                key="claimed",
            )
            try:
                await self._launch_session(session_id, expected_state="launching")
            except Exception as caught_error:
                record_caught_exception(
                    "terminal",
                    "terminal.container_terminal.caught_failure_004",
                    "A handled terminal operation raised an exception.",
                    caught_error,
                    stage="container_terminal",
                )
                await self.detach(attachment)
                raise
        return attachment

    async def next_event(
        self, attachment: ContainerTerminalAttachment
    ) -> ContainerTerminalOutput | ContainerTerminalExit:
        while True:
            async with self._lock:
                if attachment.detached:
                    raise ContainerTerminalError(
                        "terminal_detached", "terminal attachment is closed"
                    )
                if attachment.terminal_replay:
                    output = attachment.terminal_replay.popleft()
                    attachment.next_sequence = output.sequence + 1
                    return output
                session = self._sessions.get(attachment.session_id)
                if session is not None and session.attachment is attachment:
                    for output in session.replay:
                        if output.sequence < attachment.next_sequence:
                            continue
                        attachment.next_sequence = output.sequence + 1
                        return output
                if (
                    attachment.terminal_event is not None
                    and not attachment.terminal_event_delivered
                ):
                    attachment.terminal_event_delivered = True
                    return attachment.terminal_event
                if session is None and attachment.terminal_finishing:
                    attachment.wakeup.clear()
                elif session is None or session.attachment is not attachment:
                    raise ContainerTerminalError(
                        "terminal_detached", "terminal attachment is closed"
                    )
                else:
                    attachment.wakeup.clear()
            await attachment.wakeup.wait()

    async def detach(self, attachment: ContainerTerminalAttachment) -> None:
        async with self._lock:
            session = self._sessions.get(attachment.session_id)
            if (
                session is None
                or session.attachment is not attachment
                or attachment.detached
            ):
                attachment.detached = True
                attachment.wakeup.set()
                return
            session.attachment = None
            attachment.detached = True
            attachment.wakeup.set()
            if session.state == "running":
                session.ticket_expires_at = utc_now() + timedelta(
                    seconds=self.reconnect_grace_seconds
                )
                session.grace_task = create_diagnostic_task(
                    self._expire_reconnect_grace(session.id),
                    feature="terminal",
                    event_code="terminal.reconnect_grace",
                    failure_message="Terminal reconnect-grace supervision failed.",
                    name=f"container-terminal-grace-{session.id}",
                )

    async def write_input(
        self, attachment: ContainerTerminalAttachment, data: bytes
    ) -> None:
        async with self._lock:
            session = self._require_attachment_locked(attachment)
            process = session.process
            if process is None or session.state != "running":
                raise ContainerTerminalError(
                    "terminal_not_ready", "terminal process is not running"
                )
        await process.write(data)
        await self.touch(attachment.session_id)

    async def resize(
        self,
        attachment: ContainerTerminalAttachment,
        columns: int,
        rows: int,
    ) -> None:
        async with self._lock:
            session = self._require_attachment_locked(attachment)
            process = session.process
            if process is None or session.state != "running":
                raise ContainerTerminalError(
                    "terminal_not_ready", "terminal process is not running"
                )
        process.resize(columns, rows)

    async def close_attachment(self, attachment: ContainerTerminalAttachment) -> None:
        async with self._lock:
            self._require_attachment_locked(attachment)
        await self.finish(attachment.session_id, outcome="closed")

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
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_005",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
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
        tasks: list[asyncio.Task[None]] = []
        unfinished_capture = None
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            for attribute in (
                "expiry_task",
                "grace_task",
                "reader_task",
                "monitor_task",
                "watchdog_task",
            ):
                task = getattr(session, attribute)
                setattr(session, attribute, None)
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    tasks.append(task)
            if session.attachment is not None:
                session.attachment.terminal_finishing = True
        if tasks:
            await gather_diagnostic(
                *tasks,
                feature="terminal",
                event_code="terminal.session.cleanup_task_failed",
                failure_message="A terminal supervision task failed during cleanup.",
                stage="cleanup",
            )
        async with session.parser_lock:
            tail, unfinished_capture = await asyncio.to_thread(
                _finish_terminal_parser,
                session.parser,
                exit_code,
                outcome,
                detail,
            )
        if tail.passthrough:
            self._publish_output_locked(session, tail.passthrough)
        if session.attachment is not None:
            session.attachment.terminal_replay.extend(
                output
                for output in session.replay
                if output.sequence >= session.attachment.next_sequence
            )
        cleanup_error: Exception | None = None
        try:
            if session.process is not None:
                await session.process.close()
        except Exception as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_006",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            cleanup_error = exc
        if cleanup_error is not None and detail is None:
            detail = f"terminal cleanup reported: {type(cleanup_error).__name__}"
            error_code = error_code or "cleanup_failed"
        if session.audit_task is not None:
            if unfinished_capture is not None:
                await session.audit_queue.put(unfinished_capture)
            await session.audit_queue.put(None)
            await gather_diagnostic(
                session.audit_task,
                feature="terminal-audit",
                event_code="terminal-audit.writer.cleanup_failed",
                failure_message="The terminal audit writer did not stop cleanly.",
                stage="cleanup",
            )
            session.audit_task = None
        elif unfinished_capture is not None:
            await self._persist_capture(session, unfinished_capture)
        if session.attachment is not None:
            session.attachment.terminal_event = ContainerTerminalExit(
                outcome=outcome,
                exit_code=exit_code,
                error_code=error_code,
                detail=detail,
            )
            session.attachment.terminal_finishing = False
            session.attachment.wakeup.set()
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

    async def _launch_session(
        self,
        session_id: str,
        *,
        expected_state: Literal["claimed", "launching"],
    ) -> SandboxTerminalProcess:
        async with self._lock:
            session = self._require_session_locked(session_id)
            if session.state != expected_state:
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
            audit_environment = {
                **prepared.sandbox_request.environment,
                "PS0": terminal_ps0(session.audit_nonce),
                "PROMPT_COMMAND": terminal_prompt_command(session.audit_nonce),
            }
            sandbox_request = prepared.sandbox_request.model_copy(
                update={"environment": audit_environment}
            )
            process = await prepared.resolution.runner.open_terminal(
                sandbox_request,
                container_name="nebula-terminal-" + session.id.replace("-", "")[:40],
                columns=session.request.columns,
                rows=session.request.rows,
            )
        except (SandboxError, ToolPlatformError) as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_007",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
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
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_008",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            await self.finish(
                session_id,
                outcome="failed",
                detail=exc.detail,
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_009",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            detail = f"terminal launch failed ({type(exc).__name__})"
            await self.finish(
                session_id,
                outcome="failed",
                detail=detail,
                error_code="runner_unavailable",
            )
            raise ContainerTerminalError(
                "runner_unavailable",
                detail,
                status_code=503,
            ) from exc
        interrupted = False
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None or current.state != expected_state:
                interrupted = True
            else:
                current.process = process
                current.state = "running"
                current.last_activity = monotonic()
                current.reader_task = create_diagnostic_task(
                    self._read_process_output(current.id, process),
                    feature="terminal",
                    event_code="terminal.output_reader",
                    failure_message="Terminal output supervision stopped unexpectedly.",
                    name=f"container-terminal-reader-{current.id}",
                )
                current.audit_task = create_diagnostic_task(
                    self._audit_writer(current),
                    feature="terminal-audit",
                    event_code="terminal-audit.writer",
                    failure_message="Terminal audit persistence stopped unexpectedly.",
                    name=f"container-terminal-audit-{current.id}",
                )
                current.monitor_task = create_diagnostic_task(
                    self._monitor_process(current.id, process),
                    feature="terminal",
                    event_code="terminal.process_monitor",
                    failure_message="Terminal process monitoring stopped unexpectedly.",
                    name=f"container-terminal-monitor-{current.id}",
                )
                current.watchdog_task = create_diagnostic_task(
                    self._watchdog(current.id),
                    feature="terminal",
                    event_code="terminal.idle_watchdog",
                    failure_message="Terminal idle supervision stopped unexpectedly.",
                    name=f"container-terminal-watchdog-{current.id}",
                )
                session = current
        if interrupted:
            await process.close()
            raise ContainerTerminalError(
                "interrupted", "terminal launch was interrupted"
            )
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

    async def _read_process_output(
        self, session_id: str, process: SandboxTerminalProcess
    ) -> None:
        try:
            while True:
                data = await process.read(TERMINAL_OUTPUT_CHUNK_BYTES)
                if not data:
                    async with self._lock:
                        session = self._sessions.get(session_id)
                        if session is None or session.process is not process:
                            return
                    async with session.parser_lock:
                        tail = session.parser.flush()
                    if tail.passthrough:
                        self._publish_output_locked(session, tail.passthrough)
                    return
                async with self._lock:
                    session = self._sessions.get(session_id)
                    if session is None or session.process is not process:
                        return
                    session.last_activity = monotonic()
                async with session.parser_lock:
                    parsed = await _feed_terminal_parser(session.parser, data)
                async with self._lock:
                    still_active = (
                        self._sessions.get(session_id) is session
                        and session.process is process
                    )
                    if parsed.passthrough:
                        self._publish_output_locked(session, parsed.passthrough)
                if self.command_history is not None:
                    for capture in parsed.captures:
                        if session.audit_task is not None:
                            await session.audit_queue.put(capture)
                        else:
                            await self._persist_capture(session, capture)
                    for command_record in parsed.records:
                        try:
                            await asyncio.to_thread(
                                self.command_history.record,
                                engagement_id=session.request.engagement_id,
                                session_id=session.id,
                                command=command_record.command,
                                cwd=command_record.cwd,
                                exit_code=command_record.exit_code,
                            )
                        except Exception as exc:
                            record_caught_exception(
                                "terminal",
                                "terminal.container_terminal.caught_failure_010",
                                "A handled terminal operation raised an exception.",
                                exc,
                                stage="container_terminal",
                            )
                            LOGGER.warning(
                                "terminal command history write failed (%s)",
                                type(exc).__name__,
                            )
                if not still_active:
                    return
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_011",
                "A handled terminal operation raised an exception.",
                caught_error,
                stage="container_terminal",
            )
            raise
        except Exception as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_012",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            await self.finish(
                session_id,
                outcome="failed",
                error_code="terminal_io",
                detail=f"terminal output reader failed ({type(exc).__name__})",
            )

    async def _audit_writer(self, session: _TerminalReservation) -> None:
        while True:
            capture = await session.audit_queue.get()
            try:
                if capture is None:
                    return
                await self._persist_capture(session, capture)
            finally:
                session.audit_queue.task_done()

    async def _persist_capture(
        self,
        session: _TerminalReservation,
        capture: CapturedTerminalCommand,
    ) -> None:
        if self.command_history is None:
            return
        try:
            await asyncio.to_thread(
                self.command_history.record_capture,
                engagement_id=session.request.engagement_id,
                session_id=session.id,
                operator_id=session.operator_id,
                capture=capture,
            )
        except Exception as exc:
            record_caught_exception(
                "terminal-audit",
                "terminal-audit.persistence.caught_failure_013",
                "A handled terminal audit operation raised an exception.",
                exc,
                stage="audit-persistence",
            )
            LOGGER.error(
                "terminal audit persistence failed for session %s (%s)",
                session.id,
                type(exc).__name__,
            )
            try:
                self._event(
                    session,
                    "container_terminal.audit_gap",
                    {
                        "status": "capture_failed",
                        "shell_sequence": capture.shell_sequence,
                        "command_sha256": hashlib.sha256(
                            capture.command.encode("utf-8")
                        ).hexdigest(),
                        "output_sha256": capture.output_sha256,
                        "error": type(exc).__name__,
                    },
                    key=f"audit-gap-{capture.shell_sequence}",
                )
            except Exception as caught_error:
                record_caught_exception(
                    "terminal-audit",
                    "terminal-audit.gap_event.caught_failure_014",
                    "A terminal audit integrity gap could not be persisted.",
                    caught_error,
                    stage="audit-integrity",
                )
                LOGGER.exception("terminal audit-gap event persistence also failed")

    def _publish_output_locked(
        self,
        session: _TerminalReservation,
        data: bytes,
    ) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + self.replay_max_bytes]
            offset += len(chunk)
            output = ContainerTerminalOutput(
                sequence=session.next_sequence,
                data=chunk,
            )
            session.next_sequence += 1
            session.replay.append(output)
            session.replay_bytes += len(chunk)
            while session.replay_bytes > self.replay_max_bytes:
                removed = session.replay.popleft()
                session.replay_bytes -= len(removed.data)
            if session.attachment is not None:
                session.attachment.wakeup.set()

    async def _monitor_process(
        self, session_id: str, process: SandboxTerminalProcess
    ) -> None:
        try:
            exit_code = await process.wait()
            async with self._lock:
                session = self._sessions.get(session_id)
                reader = session.reader_task if session is not None else None
            if reader is not None and reader is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout=1)
                except asyncio.TimeoutError as caught_error:
                    record_caught_exception(
                        "terminal",
                        "terminal.container_terminal.caught_failure_015",
                        "A handled terminal operation raised an exception.",
                        caught_error,
                        stage="container_terminal",
                    )
                    pass
            await self.finish(
                session_id,
                outcome="completed",
                exit_code=exit_code,
            )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_016",
                "A handled terminal operation raised an exception.",
                caught_error,
                stage="container_terminal",
            )
            raise
        except Exception as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_017",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            await self.finish(
                session_id,
                outcome="failed",
                error_code="terminal_wait",
                detail=f"terminal process wait failed ({type(exc).__name__})",
            )

    async def _watchdog(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.watchdog_interval_seconds)
                try:
                    await self.enforce_workspace_limits(session_id)
                except ContainerTerminalError as exc:
                    record_caught_exception(
                        "terminal",
                        "terminal.container_terminal.caught_failure_018",
                        "A handled terminal operation raised an exception.",
                        exc,
                        stage="container_terminal",
                    )
                    await self.finish(
                        session_id,
                        outcome="workspace_limit",
                        error_code=exc.code,
                        detail=exc.detail,
                    )
                    return
                if await self.idle_seconds(session_id) >= self.idle_timeout_seconds:
                    await self.finish(
                        session_id,
                        outcome="idle_timeout",
                        error_code="idle_timeout",
                        detail="terminal closed after 30 minutes without input or output",
                    )
                    return
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_019",
                "A handled terminal operation raised an exception.",
                caught_error,
                stage="container_terminal",
            )
            raise

    async def _expire_ticket(self, session_id: str) -> None:
        try:
            await asyncio.sleep(TICKET_TTL_SECONDS)
            await self.finish(
                session_id,
                outcome="expired",
                detail="terminal WebSocket ticket expired before use",
                error_code="ticket_expired",
            )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_020",
                "A handled terminal operation raised an exception.",
                caught_error,
                stage="container_terminal",
            )
            return

    async def _expire_reconnect_grace(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self.reconnect_grace_seconds)
            await self.finish(
                session_id,
                outcome="reconnect_timeout",
                detail="terminal reconnect grace expired",
                error_code="reconnect_timeout",
            )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_021",
                "A handled terminal operation raised an exception.",
                caught_error,
                stage="container_terminal",
            )
            return

    def _require_session_locked(self, session_id: str) -> _TerminalReservation:
        session = self._sessions.get(session_id)
        if session is None:
            raise ContainerTerminalError(
                "terminal_not_found",
                "terminal session was not found",
                status_code=404,
            )
        return session

    def _require_attachment_locked(
        self, attachment: ContainerTerminalAttachment
    ) -> _TerminalReservation:
        session = self._require_session_locked(attachment.session_id)
        if attachment.detached or session.attachment is not attachment:
            raise ContainerTerminalError(
                "terminal_detached", "terminal attachment is closed"
            )
        return session

    @staticmethod
    def _validate_ticket_locked(session: _TerminalReservation, ticket: str) -> None:
        if session.ticket_expires_at is None:
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

    @staticmethod
    def _rotate_ticket_locked(session: _TerminalReservation) -> None:
        session.websocket_ticket = secrets.token_urlsafe(32)
        session.ticket_expires_at = None

    @staticmethod
    def _cancel_session_task_locked(
        session: _TerminalReservation,
        attribute: Literal["expiry_task", "grace_task"],
    ) -> None:
        task = getattr(session, attribute)
        setattr(session, attribute, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

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
            "idle_timeout_seconds": int(self.idle_timeout_seconds),
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
                idle_timeout_seconds=int(self.idle_timeout_seconds),
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
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_022",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            raise ContainerTerminalError("workspace_limit", str(exc)) from exc
        try:
            self.tool_platform.resolve_human_terminal_profile(request.engagement_id)
        except ToolPlatformError as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_023",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            raise ContainerTerminalError(
                "runner_unavailable", str(exc), status_code=503
            ) from exc
        try:
            resolution = await self.tool_platform.resolve_human_terminal_runtime(
                request.engagement_id
            )
        except ToolPlatformError as exc:
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_024",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
            raise ContainerTerminalError(
                "image_unavailable", str(exc), status_code=503
            ) from exc
        if (
            self.command_history is not None
            and resolution.image.security_tools
            and resolution.image.security_tool_manifest_sha256 is not None
        ):
            self.command_history.register_tool_inventory(
                runtime_image_digest=resolution.image.digest,
                manifest_sha256=resolution.image.security_tool_manifest_sha256,
                default_tools=resolution.image.security_tools,
            )
        runtime = _runtime_snapshot(resolution)
        network = ContainerTerminalNetworkSnapshot()
        security = ContainerTerminalSecuritySnapshot()
        sandbox_request = SandboxRequest(
            image=resolution.image.resolved_reference,
            command=[runtime.interpreter, *runtime.arguments],
            workspace=resolution.workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            environment={
                "HISTFILE": "/dev/null",
                "LANG": "C.UTF-8",
                "PS0": TERMINAL_PS0,
                "PROMPT_COMMAND": TERMINAL_PROMPT_COMMAND,
                "TERM": "xterm-256color",
            },
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
            record_caught_exception(
                "terminal",
                "terminal.container_terminal.caught_failure_025",
                "A handled terminal operation raised an exception.",
                exc,
                stage="container_terminal",
            )
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

    def _start_response(
        self,
        session: _TerminalReservation,
    ) -> ContainerTerminalStartResponse:
        return ContainerTerminalStartResponse(
            session_id=session.id,
            websocket_ticket=session.start_websocket_ticket,
            ticket_expires_at=session.start_ticket_expires_at,
            websocket_path=f"/api/v1/container-terminals/{session.id}/ws",
            reconnect_grace_seconds=max(1, int(self.reconnect_grace_seconds)),
            replay_max_bytes=self.replay_max_bytes,
            last_sequence=session.next_sequence - 1,
        )


async def _feed_terminal_parser(
    parser: Osc633CommandParser,
    data: bytes,
) -> TerminalCommandParseResult:
    """Finish an in-flight durable spool write before honoring cancellation."""

    # diagnostic-expected: this filesystem worker is shielded, awaited, and
    # cancellation is classified below before the durable write completes.
    worker = asyncio.create_task(asyncio.to_thread(parser.feed, data))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as caught_error:
        # ``to_thread`` cannot stop the underlying filesystem operation. Wait
        # for it so the parser is never mutated after its session lock is freed.
        record_caught_exception(
            "terminal",
            "terminal.container_terminal.caught_failure_026",
            "A handled terminal operation raised an exception.",
            caught_error,
            stage="container_terminal",
        )
        return await worker


def _finish_terminal_parser(
    parser: Osc633CommandParser,
    exit_code: int | None,
    outcome: str,
    detail: str | None,
) -> tuple[TerminalCommandParseResult, CapturedTerminalCommand | None]:
    tail = parser.flush()
    capture = parser.finish_active(
        exit_code=exit_code,
        status="interrupted",
        detail=(detail or f"terminal session ended with status {outcome}")[:1_000],
    )
    return tail, capture


def _runtime_snapshot(
    resolution: HumanTerminalRuntimeResolution,
) -> ContainerTerminalRuntimeSnapshot:
    profile = resolution.profile
    return ContainerTerminalRuntimeSnapshot(
        source_image=resolution.image.source_reference,
        base_image=resolution.image.base_resolved_reference,
        base_image_digest=resolution.image.base_digest,
        image=resolution.image.resolved_reference,
        image_digest=resolution.image.digest,
        installed_packages=list(resolution.image.installed_packages),
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
    "ContainerTerminalAttachment",
    "ContainerTerminalCapabilities",
    "ContainerTerminalError",
    "ContainerTerminalExit",
    "ContainerTerminalNetworkSnapshot",
    "ContainerTerminalOutput",
    "ContainerTerminalPreflightRequest",
    "ContainerTerminalPreflightResponse",
    "ContainerTerminalRecoveryResponse",
    "ContainerTerminalRuntimeSnapshot",
    "ContainerTerminalSecuritySnapshot",
    "ContainerTerminalService",
    "ContainerTerminalStartRequest",
    "ContainerTerminalStartResponse",
    "MAX_TERMINAL_INPUT_BYTES",
    "TERMINAL_IDLE_TIMEOUT_SECONDS",
    "TERMINAL_MAX_DURATION_SECONDS",
    "TERMINAL_OUTPUT_CHUNK_BYTES",
    "TERMINAL_RECONNECT_GRACE_SECONDS",
    "TERMINAL_REPLAY_MAX_BYTES",
]
