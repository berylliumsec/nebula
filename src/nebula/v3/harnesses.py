"""Shared stateful harness runtime for chat, missions, and isolated MCP servers.

The runtime deliberately keeps vendor envelopes at the adapter boundary.  Durable
records contain only normalized, bounded events and credential-free snapshots.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import claude_agent_sdk
from pydantic import Field

from .credentials import CredentialError, CredentialStore
from .domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatCitation,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessCapabilities,
    HarnessAuthMode,
    HarnessConnectionMode,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTransport,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    McpApprovalMode,
    McpAuthMode,
    McpCwdPolicy,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    NebulaModel,
    RiskClass,
    RunBackend,
    RunBudget,
    RunStatus,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from .redaction import redact_text
from .storage import NebulaStore, NotFoundError

MAX_NORMALIZED_TEXT = 200_000
MAX_TOOL_ARGUMENT_TEXT = 64_000
MAX_TOOL_RESULT_TEXT = 64_000
ADAPTER_CONTRACT_VERSION = "nebula-harness-v1"


class HarnessError(RuntimeError):
    """Base operator-safe harness failure."""


class HarnessConfigurationError(HarnessError):
    """A profile, model, session, or MCP selection is invalid."""


class HarnessUnavailableError(HarnessError):
    """The selected local harness cannot currently be reached."""


class HarnessStateError(HarnessError):
    """A requested session transition conflicts with active work."""


class HarnessTransportError(HarnessError):
    """A transport ended or returned a malformed message."""


class HarnessEvent(NebulaModel):
    type: Literal[
        "started",
        "message_delta",
        "item_started",
        "item_completed",
        "tool_started",
        "tool_completed",
        "approval_required",
        "usage",
        "interrupted",
        "completed",
        "error",
    ]
    origin: HarnessTurnOrigin | None = None
    harness_profile_id: str | None = None
    harness_session_id: str | None = None
    harness_turn_id: str | None = None
    model: str | None = None
    external_session_id: str | None = None
    external_turn_id: str | None = None
    delta: str | None = Field(default=None, max_length=MAX_NORMALIZED_TEXT)
    message: str | None = Field(default=None, max_length=MAX_NORMALIZED_TEXT)
    approval_id: str | None = None
    tool_call_id: str | None = None
    server_id: str | None = None
    tool_name: str | None = None
    usage: ChatTokenUsage | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class HarnessHealth(NebulaModel):
    profile_id: str
    healthy: bool
    kind: HarnessKind
    adapter_version: str = ADAPTER_CONTRACT_VERSION
    harness_version: str | None = None
    capabilities: HarnessCapabilities
    detail: str | None = Field(default=None, max_length=1_000)
    checked_at: Any = Field(default_factory=utc_now)


class HarnessCatalogItem(NebulaModel):
    kind: HarnessKind
    display_name: str
    connection_modes: list[HarnessConnectionMode]
    transports: list[HarnessTransport]
    mcp_transports: list[McpTransport]
    experimental_transports: list[HarnessTransport] = Field(default_factory=list)
    installed: bool
    detail: str | None = None


class HarnessPermissionRequest(NebulaModel):
    vendor_request_id: str
    category: Literal["mcp", "command", "file", "permission"]
    vendor_name: str
    server_name: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = Field(default=None, max_length=2_000)


class HarnessPermissionDecision(NebulaModel):
    allowed: bool
    approval_id: str | None = None
    reason: str | None = None


@dataclass
class PermissionTicket:
    approval_id: str | None
    tool_call_id: str | None
    decision: asyncio.Future[HarnessPermissionDecision]


PermissionHandler = Callable[[HarnessPermissionRequest], Awaitable[PermissionTicket]]


@dataclass(frozen=True)
class AdapterOpenRequest:
    profile: HarnessProfile
    session: HarnessSession
    workspace: Path
    mcp_profiles: tuple[McpServerProfile, ...]
    credential_store: CredentialStore
    permission_handler: PermissionHandler


class HarnessConnection(ABC):
    external_session_id: str | None
    adapter_version: str

    @abstractmethod
    def run_turn(
        self, prompt: str, *, model: str
    ) -> AsyncIterator[HarnessEvent]: ...

    @abstractmethod
    async def steer(self, text: str) -> None: ...

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class HarnessAdapter(ABC):
    kind: HarnessKind

    @abstractmethod
    async def probe(
        self, profile: HarnessProfile, credential_store: CredentialStore
    ) -> HarnessHealth: ...

    @abstractmethod
    async def open(self, request: AdapterOpenRequest) -> HarnessConnection: ...


def harness_catalog() -> list[HarnessCatalogItem]:
    return [
        HarnessCatalogItem(
            kind=HarnessKind.CODEX_APP_SERVER,
            display_name="Codex App Server",
            connection_modes=[
                HarnessConnectionMode.SPAWN,
                HarnessConnectionMode.ENDPOINT,
            ],
            transports=[
                HarnessTransport.STDIO,
                HarnessTransport.UNIX,
                HarnessTransport.WEBSOCKET,
            ],
            mcp_transports=[McpTransport.STDIO, McpTransport.STREAMABLE_HTTP],
            experimental_transports=[HarnessTransport.WEBSOCKET],
            installed=True,
            detail="Stable v2 threads/turns over stdio or Unix; loopback WebSocket is experimental.",
        ),
        HarnessCatalogItem(
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            display_name="Claude Agent SDK",
            connection_modes=[HarnessConnectionMode.SPAWN],
            transports=[HarnessTransport.STDIO],
            mcp_transports=[McpTransport.STDIO, McpTransport.STREAMABLE_HTTP],
            installed=_claude_sdk_installed(),
            detail="Packaged Python Agent SDK with strict MCP configuration.",
        ),
    ]


def _claude_sdk_installed() -> bool:
    return True


def _bounded(value: Any, *, limit: int) -> Any:
    """Bound and redact a JSON-compatible diagnostic value."""

    if isinstance(value, str):
        clean = redact_text(value)
        if len(clean) > limit:
            return clean[:limit] + "…[truncated]"
        return clean
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 256:
                result["_truncated"] = True
                break
            lowered = str(key).lower()
            if any(token in lowered for token in ("authorization", "token", "secret", "password")):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _bounded(item, limit=limit)
        return result
    if isinstance(value, list):
        return [_bounded(item, limit=limit) for item in value[:256]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded(str(value), limit=limit)


def _minimal_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    keep = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        }
    }
    keep.update(extra or {})
    return keep


def _resolve_secret(store: CredentialStore, reference: str) -> str:
    try:
        return store.resolve(reference).get_secret_value()
    except (CredentialError, ValueError) as exc:
        raise HarnessConfigurationError(str(exc)) from exc


def _mcp_runtime_config(
    profiles: tuple[McpServerProfile, ...],
    credentials: CredentialStore,
    workspace: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Build an isolated vendor-neutral config plus secret-only process env."""

    config: dict[str, dict[str, Any]] = {}
    secret_environment: dict[str, str] = {}
    for profile in profiles:
        if not profile.enabled:
            raise HarnessConfigurationError(
                f"MCP server {profile.id!r} is disabled; create a new session after enabling it"
            )
        item: dict[str, Any] = {
            "id": profile.id,
            "name": profile.name,
            "transport": profile.transport.value,
            "required": profile.required,
            "startup_timeout_seconds": profile.startup_timeout_seconds,
            "tool_timeout_seconds": profile.tool_timeout_seconds,
            "enabled_tools": list(profile.enabled_tools),
            "disabled_tools": list(profile.disabled_tools),
        }
        if profile.transport == McpTransport.STDIO:
            item.update(
                command=profile.command,
                args=list(profile.arguments),
                cwd=(
                    str(workspace)
                    if profile.cwd_policy == McpCwdPolicy.WORKSPACE
                    else profile.cwd
                ),
                env=dict(profile.environment),
            )
            for name, reference in profile.environment_secret_refs.items():
                value = _resolve_secret(credentials, reference)
                item["env"][name] = value
        else:
            item["url"] = profile.url
            headers: dict[str, str] = {}
            if profile.auth_mode == McpAuthMode.BEARER and profile.bearer_secret_ref:
                headers["Authorization"] = (
                    "Bearer " + _resolve_secret(credentials, profile.bearer_secret_ref)
                )
            for name, reference in profile.header_secret_refs.items():
                headers[name] = _resolve_secret(credentials, reference)
            item["headers"] = headers
        config[profile.name] = item
    return config, secret_environment


class _CodexRpc:
    """Small JSON-RPC-like client matching the Codex app-server wire format."""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process | None = None,
        websocket: Any = None,
    ) -> None:
        self.process = process
        self.websocket = websocket
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail = ""

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._reader(), name="codex-app-reader")
        if self.process is not None and self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(), name="codex-app-stderr"
            )

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"method": method, "id": request_id, "params": params})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def _write(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if self.websocket is not None:
            await self.websocket.send(encoded)
            return
        if self.process is None or self.process.stdin is None:
            raise HarnessTransportError("Codex app-server stdin is unavailable")
        self.process.stdin.write(encoded.encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def _reader(self) -> None:
        try:
            if self.websocket is not None:
                async for raw in self.websocket:
                    await self._dispatch(raw)
            elif self.process is not None and self.process.stdout is not None:
                while line := await self.process.stdout.readline():
                    await self._dispatch(line)
            else:
                raise HarnessTransportError("Codex app-server output is unavailable")
            raise HarnessTransportError("Codex app-server transport closed")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = (
                exc
                if isinstance(exc, HarnessTransportError)
                else HarnessTransportError(f"Codex transport failed: {type(exc).__name__}")
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.events.put(error)

    async def _dispatch(self, raw: Any) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise HarnessTransportError("Codex returned malformed JSON") from exc
        if not isinstance(message, dict):
            raise HarnessTransportError("Codex returned a non-object message")
        request_id = message.get("id")
        if "method" not in message and isinstance(request_id, int):
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if "error" in message:
                pending.set_exception(
                    HarnessTransportError(
                        "Codex request failed: "
                        + str(_bounded(message["error"], limit=1_000))
                    )
                )
            else:
                pending.set_result(message.get("result"))
            return
        if isinstance(message.get("method"), str):
            await self.events.put(message)
            return
        raise HarnessTransportError("Codex returned an uncorrelatable message")

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            text = redact_text(chunk.decode("utf-8", errors="replace"))
            self.stderr_tail = (self.stderr_tail + text)[-8_000:]

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )


class CodexAppServerConnection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/codex-v2"

    def __init__(
        self,
        rpc: _CodexRpc,
        *,
        external_session_id: str,
        permission_handler: PermissionHandler,
    ) -> None:
        self.rpc = rpc
        self.external_session_id = external_session_id
        self.permission_handler = permission_handler
        self.active_turn_id: str | None = None

    async def run_turn(
        self, prompt: str, *, model: str
    ) -> AsyncIterator[HarnessEvent]:
        result = await self.rpc.request(
            "turn/start",
            {
                "threadId": self.external_session_id,
                "input": [{"type": "text", "text": prompt}],
                "model": model,
                "approvalPolicy": "untrusted",
            },
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise HarnessTransportError("Codex turn/start omitted the turn id")
        self.active_turn_id = turn["id"]
        yield HarnessEvent(
            type="started",
            external_session_id=self.external_session_id,
            external_turn_id=self.active_turn_id,
        )
        message_parts: list[str] = []
        while True:
            raw = await self.rpc.events.get()
            if isinstance(raw, BaseException):
                raise raw
            method = raw.get("method")
            raw_params = raw.get("params")
            params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
                "execCommandApproval",
                "applyPatchApproval",
            }:
                async for event in self._approval(raw, method, params):
                    yield event
                continue
            if method == "mcpServer/elicitation/request":
                # Interactive elicitation/forms are intentionally unsupported.
                await self.rpc.respond(raw.get("id"), {"action": "decline"})
                yield HarnessEvent(
                    type="error",
                    message="MCP elicitation is unsupported; the request was declined",
                )
                continue
            if params.get("turnId") not in {None, self.active_turn_id}:
                continue
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                message_parts.append(delta)
                yield HarnessEvent(
                    type="message_delta",
                    delta=delta,
                    external_turn_id=self.active_turn_id,
                )
                continue
            if method in {"item/started", "item/completed"}:
                item = params.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "unknown")
                if item_type == "mcpToolCall":
                    yield HarnessEvent(
                        type=(
                            "tool_started" if method == "item/started" else "tool_completed"
                        ),
                        external_turn_id=self.active_turn_id,
                        server_id=str(item.get("server") or ""),
                        tool_name=str(item.get("tool") or ""),
                        payload=_bounded(item, limit=MAX_TOOL_RESULT_TEXT),
                    )
                else:
                    yield HarnessEvent(
                        type=(
                            "item_started" if method == "item/started" else "item_completed"
                        ),
                        external_turn_id=self.active_turn_id,
                        payload=_bounded(item, limit=MAX_TOOL_RESULT_TEXT),
                    )
                continue
            if method == "thread/tokenUsage/updated":
                usage = _codex_usage(params)
                yield HarnessEvent(type="usage", usage=usage, payload={})
                continue
            if method == "turn/completed":
                completed = params.get("turn")
                if not isinstance(completed, dict) or completed.get("id") != self.active_turn_id:
                    continue
                status = str(completed.get("status") or "failed")
                self.active_turn_id = None
                if status in {"interrupted", "cancelled"}:
                    yield HarnessEvent(type="interrupted", message=status)
                    return
                if status != "completed":
                    error = completed.get("error")
                    raise HarnessTransportError(
                        "Codex turn failed: " + str(_bounded(error or status, limit=1_000))
                    )
                yield HarnessEvent(type="completed", message="".join(message_parts))
                return

    async def _approval(
        self, raw: dict[str, Any], method: str, params: dict[str, Any]
    ) -> AsyncIterator[HarnessEvent]:
        category: Literal["command", "file", "permission"]
        if "command" in method.lower() or method == "execCommandApproval":
            category = "command"
            arguments = {"command": params.get("command"), "cwd": params.get("cwd")}
        elif "file" in method.lower() or method == "applyPatchApproval":
            category = "file"
            arguments = {
                "item_id": params.get("itemId") or params.get("callId"),
                "file_changes": params.get("fileChanges"),
            }
        else:
            category = "permission"
            arguments = {"permissions": params.get("permissions"), "cwd": params.get("cwd")}
        request = HarnessPermissionRequest(
            vendor_request_id=str(raw.get("id")),
            category=category,
            vendor_name=method,
            arguments=_bounded(arguments, limit=MAX_TOOL_ARGUMENT_TEXT),
            rationale=str(params.get("reason") or "") or None,
        )
        ticket = await self.permission_handler(request)
        if ticket.approval_id:
            yield HarnessEvent(
                type="approval_required",
                approval_id=ticket.approval_id,
                tool_call_id=ticket.tool_call_id,
                payload={"category": category, "arguments": request.arguments},
            )
        decision = await ticket.decision
        allowed = decision.allowed
        response: dict[str, Any]
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            response = {"decision": "accept" if allowed else "decline"}
        elif method == "item/permissions/requestApproval":
            response = {
                "permissions": params.get("permissions") if allowed else {"permissions": "deny"},
                "scope": "turn",
            }
        else:
            response = {"decision": "approved" if allowed else "denied"}
        await self.rpc.respond(raw.get("id"), response)

    async def steer(self, text: str) -> None:
        if not self.active_turn_id:
            raise HarnessStateError("Codex session has no active turn to steer")
        await self.rpc.request(
            "turn/steer",
            {
                "threadId": self.external_session_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": self.active_turn_id,
            },
        )

    async def interrupt(self) -> None:
        if self.active_turn_id:
            await self.rpc.request(
                "turn/interrupt",
                {"threadId": self.external_session_id, "turnId": self.active_turn_id},
            )

    async def close(self) -> None:
        await self.rpc.close()


def _codex_usage(params: dict[str, Any]) -> ChatTokenUsage:
    usage = params.get("tokenUsage") or params.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    raw_last = usage.get("last")
    last: dict[str, Any] = raw_last if isinstance(raw_last, dict) else usage
    input_tokens = int(last.get("inputTokens") or last.get("input_tokens") or 0)
    output_tokens = int(last.get("outputTokens") or last.get("output_tokens") or 0)
    return ChatTokenUsage(
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        total_tokens=max(0, input_tokens + output_tokens),
    )


class CodexAppServerAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    async def probe(
        self, profile: HarnessProfile, credential_store: CredentialStore
    ) -> HarnessHealth:
        rpc: _CodexRpc | None = None
        try:
            rpc = await self._connect(profile, credential_store, (), Path.cwd())
            initialize = await asyncio.wait_for(
                self._initialize(rpc), timeout=profile.metadata.get("probe_timeout", 15)
            )
            version = initialize.get("userAgent") if isinstance(initialize, dict) else None
            return HarnessHealth(
                profile_id=profile.id,
                healthy=True,
                kind=self.kind,
                harness_version=str(version) if version else None,
                capabilities=HarnessCapabilities(
                    sessions=True,
                    resume=True,
                    steering=True,
                    interruption=True,
                    approvals=True,
                    streaming=True,
                    mcp=True,
                    adapter_version=ADAPTER_CONTRACT_VERSION + "/codex-v2",
                    protocol_version="app-server-v2",
                    checked_at=utc_now(),
                ),
            )
        except Exception as exc:
            return HarnessHealth(
                profile_id=profile.id,
                healthy=False,
                kind=self.kind,
                capabilities=HarnessCapabilities(checked_at=utc_now()),
                detail=_safe_error(exc),
            )
        finally:
            if rpc is not None:
                await rpc.close()

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        rpc = await self._connect(
            request.profile,
            request.credential_store,
            request.mcp_profiles,
            request.workspace,
        )
        try:
            await self._initialize(rpc)
            if request.session.external_session_id:
                result = await rpc.request(
                    "thread/resume",
                    {
                        "threadId": request.session.external_session_id,
                        "model": request.session.model,
                        "cwd": str(request.workspace),
                        "approvalPolicy": "untrusted",
                        "sandbox": "workspace-write",
                    },
                )
            else:
                result = await rpc.request(
                    "thread/start",
                    {
                        "model": request.session.model,
                        "cwd": str(request.workspace),
                        "approvalPolicy": "untrusted",
                        "sandbox": "workspace-write",
                        "developerInstructions": (
                            "Work only inside the supplied workspace. Built-in web search and "
                            "unconfigured network access are disabled. Use only configured MCP "
                            "servers and honor every approval decision."
                        ),
                    },
                )
            thread = result.get("thread") if isinstance(result, dict) else None
            external_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(external_id, str) or not external_id:
                raise HarnessTransportError("Codex thread operation omitted the thread id")
            if (
                request.session.external_session_id
                and external_id != request.session.external_session_id
            ):
                raise HarnessTransportError("Codex resumed a different thread")
            return CodexAppServerConnection(
                rpc,
                external_session_id=external_id,
                permission_handler=request.permission_handler,
            )
        except Exception:
            await rpc.close()
            raise

    async def _initialize(self, rpc: _CodexRpc) -> Any:
        result = await rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "nebula-core",
                    "title": "Nebula Core",
                    "version": ADAPTER_CONTRACT_VERSION,
                },
                "capabilities": {
                    "experimentalApi": False,
                    "mcpServerOpenaiFormElicitation": False,
                    "requestAttestation": False,
                },
            },
        )
        await rpc.notify("initialized")
        return result

    async def _connect(
        self,
        profile: HarnessProfile,
        credentials: CredentialStore,
        mcp_profiles: tuple[McpServerProfile, ...],
        workspace: Path,
    ) -> _CodexRpc:
        if profile.connection_mode == HarnessConnectionMode.SPAWN:
            if not profile.executable:
                raise HarnessConfigurationError("Codex executable is required")
            executable = Path(profile.executable)
            if not executable.is_absolute() or not executable.is_file():
                raise HarnessConfigurationError("Codex executable must be an existing absolute file")
            mcp_config, _ = _mcp_runtime_config(mcp_profiles, credentials, workspace)
            argv = [str(executable), "app-server", "-c", "mcp_servers={}"]
            child_env: dict[str, str] = {}
            if profile.auth_mode == HarnessAuthMode.SECRET_REF and profile.secret_ref:
                child_env["OPENAI_API_KEY"] = _resolve_secret(
                    credentials, profile.secret_ref
                )
            argv.extend(_codex_mcp_overrides(mcp_config, child_env))
            argv.extend(["--strict-config", "--listen", "stdio://"])
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=_minimal_environment(child_env),
            )
            rpc = _CodexRpc(process=process)
            await rpc.start()
            return rpc

        import websockets

        endpoint = profile.endpoint or ""
        headers: dict[str, str] = {}
        if (
            profile.auth_mode == HarnessAuthMode.ENDPOINT_BEARER
            and profile.secret_ref
        ):
            headers["Authorization"] = "Bearer " + _resolve_secret(
                credentials, profile.secret_ref
            )
        if profile.transport == HarnessTransport.UNIX:
            parsed = urlsplit(endpoint)
            path = unquote(parsed.path)
            websocket = await websockets.unix_connect(
                path, uri="ws://localhost", additional_headers=headers or None
            )
        else:
            websocket = await websockets.connect(
                endpoint, additional_headers=headers or None
            )
        rpc = _CodexRpc(websocket=websocket)
        await rpc.start()
        return rpc


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_toml_key(str(key))}={_toml_value(item)}"
            for key, item in value.items()
        ) + "}"
    raise TypeError(f"unsupported TOML override value: {type(value).__name__}")


def _codex_mcp_overrides(
    config: dict[str, dict[str, Any]], child_env: dict[str, str]
) -> list[str]:
    argv: list[str] = []
    for ordinal, (name, item) in enumerate(config.items()):
        prefix = f"mcp_servers.{_toml_key(name)}"
        values: dict[str, Any] = {
            "enabled": True,
            "required": item["required"],
            "startup_timeout_sec": item["startup_timeout_seconds"],
            "tool_timeout_sec": item["tool_timeout_seconds"],
            # Codex must ask the client for every selected MCP tool.
            "default_tools_approval_mode": "prompt",
        }
        if item["enabled_tools"]:
            values["enabled_tools"] = item["enabled_tools"]
        if item["disabled_tools"]:
            values["disabled_tools"] = item["disabled_tools"]
        if item["transport"] == McpTransport.STDIO.value:
            values.update(command=item["command"], args=item["args"], cwd=item["cwd"])
            env_names: list[str] = []
            for env_name, env_value in item["env"].items():
                child_env[env_name] = env_value
                env_names.append(env_name)
            if env_names:
                values["env_vars"] = env_names
        else:
            values["url"] = item["url"]
            header_env: dict[str, str] = {}
            for header_index, (header, secret) in enumerate(item["headers"].items()):
                env_name = f"NEBULA_MCP_{ordinal}_{header_index}"
                child_env[env_name] = secret.removeprefix("Bearer ")
                if header.lower() == "authorization" and secret.startswith("Bearer "):
                    values["bearer_token_env_var"] = env_name
                else:
                    header_env[header] = env_name
            if header_env:
                values["env_http_headers"] = header_env
        for key, value in values.items():
            if value is None:
                continue
            argv.extend(["-c", f"{prefix}.{key}={_toml_value(value)}"])
    return argv


class ClaudeAgentSdkConnection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/claude-sdk"

    def __init__(
        self,
        client: Any,
        *,
        permission_handler: PermissionHandler,
        sdk: Any,
        external_session_id: str | None,
    ) -> None:
        self.client = client
        self.permission_handler = permission_handler
        self.sdk = sdk
        self.external_session_id = external_session_id
        self.active = False

    async def run_turn(
        self, prompt: str, *, model: str
    ) -> AsyncIterator[HarnessEvent]:
        del model  # Locked into ClaudeAgentOptions for the connected session.
        self.active = True
        await self.client.query(prompt)
        yield HarnessEvent(type="started", external_session_id=self.external_session_id)
        parts: list[str] = []
        fallback_parts: list[str] = []
        tool_identities: dict[str, tuple[str | None, str]] = {}
        usage = ChatTokenUsage()
        try:
            async for message in self.client.receive_response():
                class_name = type(message).__name__
                if class_name == "StreamEvent":
                    event = getattr(message, "event", None)
                    delta = _claude_delta(event)
                    if delta:
                        parts.append(delta)
                        yield HarnessEvent(type="message_delta", delta=delta)
                    continue
                if class_name in {"AssistantMessage", "UserMessage"}:
                    for block in getattr(message, "content", []) or []:
                        block_name = type(block).__name__
                        if block_name == "TextBlock" and class_name == "AssistantMessage":
                            text = str(getattr(block, "text", ""))
                            if text:
                                fallback_parts.append(text)
                        elif block_name == "ToolUseBlock":
                            vendor_name = str(getattr(block, "name", ""))
                            server_name, tool_name = _parse_claude_mcp_name(vendor_name)
                            tool_use_id = str(getattr(block, "id", ""))
                            tool_identities[tool_use_id] = (server_name, tool_name)
                            yield HarnessEvent(
                                type="tool_started",
                                server_id=server_name,
                                tool_name=tool_name,
                                payload=_bounded(
                                    {
                                        "id": tool_use_id,
                                        "vendor_name": vendor_name,
                                        "arguments": getattr(block, "input", {}),
                                    },
                                    limit=MAX_TOOL_ARGUMENT_TEXT,
                                ),
                            )
                        elif block_name == "ToolResultBlock":
                            tool_use_id = str(getattr(block, "tool_use_id", ""))
                            server_name, tool_name = tool_identities.get(
                                tool_use_id, (None, "unknown")
                            )
                            is_error = bool(getattr(block, "is_error", False))
                            yield HarnessEvent(
                                type="tool_completed",
                                server_id=server_name,
                                tool_name=tool_name,
                                payload=_bounded(
                                    {
                                        "id": tool_use_id,
                                        "result": getattr(block, "content", None),
                                        "error": (
                                            "Claude MCP tool reported an error"
                                            if is_error
                                            else None
                                        ),
                                    },
                                    limit=MAX_TOOL_RESULT_TEXT,
                                ),
                            )
                    continue
                if class_name == "ResultMessage":
                    session_id = getattr(message, "session_id", None)
                    if isinstance(session_id, str) and session_id:
                        self.external_session_id = session_id
                    raw_usage = getattr(message, "usage", None) or {}
                    usage = ChatTokenUsage(
                        input_tokens=max(0, int(raw_usage.get("input_tokens", 0))),
                        output_tokens=max(0, int(raw_usage.get("output_tokens", 0))),
                        total_tokens=max(
                            0,
                            int(raw_usage.get("input_tokens", 0))
                            + int(raw_usage.get("output_tokens", 0)),
                        ),
                    )
                    if getattr(message, "is_error", False):
                        raise HarnessTransportError(
                            "Claude turn failed: "
                            + str(_bounded(getattr(message, "result", "error"), limit=1_000))
                        )
            if not parts and fallback_parts:
                fallback = "".join(fallback_parts)
                parts.append(fallback)
                yield HarnessEvent(type="message_delta", delta=fallback)
            yield HarnessEvent(type="usage", usage=usage)
            yield HarnessEvent(
                type="completed",
                message="".join(parts),
                external_session_id=self.external_session_id,
            )
        finally:
            self.active = False

    async def steer(self, text: str) -> None:
        if not self.active:
            raise HarnessStateError("Claude session has no active turn to steer")
        await self.client.query(text)

    async def interrupt(self) -> None:
        if self.active:
            await self.client.interrupt()

    async def close(self) -> None:
        close = getattr(self.client, "disconnect", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def _claude_delta(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text") or "")
    return ""


class ClaudeAgentSdkAdapter(HarnessAdapter):
    kind = HarnessKind.CLAUDE_AGENT_SDK

    @staticmethod
    def _sdk() -> Any:
        return claude_agent_sdk

    async def probe(
        self, profile: HarnessProfile, credential_store: CredentialStore
    ) -> HarnessHealth:
        del credential_store
        try:
            sdk = self._sdk()
            version = getattr(sdk, "__version__", None)
            if profile.executable and not Path(profile.executable).is_file():
                raise HarnessConfigurationError(
                    "Claude CLI override must be an existing absolute executable"
                )
            return HarnessHealth(
                profile_id=profile.id,
                healthy=True,
                kind=self.kind,
                harness_version=str(version) if version else None,
                capabilities=HarnessCapabilities(
                    sessions=True,
                    resume=True,
                    steering=True,
                    interruption=True,
                    approvals=True,
                    streaming=True,
                    mcp=True,
                    adapter_version=ADAPTER_CONTRACT_VERSION + "/claude-sdk",
                    protocol_version="agent-sdk",
                    checked_at=utc_now(),
                ),
            )
        except Exception as exc:
            return HarnessHealth(
                profile_id=profile.id,
                healthy=False,
                kind=self.kind,
                capabilities=HarnessCapabilities(checked_at=utc_now()),
                detail=_safe_error(exc),
            )

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        sdk = self._sdk()
        if request.profile.executable and not Path(request.profile.executable).is_file():
            raise HarnessConfigurationError(
                "Claude CLI override must be an existing absolute executable"
            )
        mcp_config, _ = _mcp_runtime_config(
            request.mcp_profiles, request.credential_store, request.workspace
        )

        async def can_use_tool(
            tool_name: str, input_data: dict[str, Any], context: Any
        ) -> Any:
            del context
            server, tool = _parse_claude_mcp_name(tool_name)
            ticket = await request.permission_handler(
                HarnessPermissionRequest(
                    vendor_request_id=str(uuid4()),
                    category="mcp" if server else "command",
                    vendor_name=tool_name,
                    server_name=server,
                    tool_name=tool if server else tool_name,
                    arguments=_bounded(input_data, limit=MAX_TOOL_ARGUMENT_TEXT),
                )
            )
            decision = await ticket.decision
            if decision.allowed:
                allow = getattr(sdk, "PermissionResultAllow", None)
                return allow(updated_input=input_data) if allow else {"behavior": "allow", "updatedInput": input_data}
            deny = getattr(sdk, "PermissionResultDeny", None)
            return deny(message=decision.reason or "Denied by Nebula policy") if deny else {"behavior": "deny", "message": decision.reason or "Denied by Nebula policy"}

        options_kwargs: dict[str, Any] = {
            "model": request.session.model,
            "cwd": str(request.workspace),
            "resume": request.session.external_session_id,
            "mcp_servers": _claude_mcp_config(mcp_config),
            "strict_mcp_config": True,
            "setting_sources": [],
            "permission_mode": "default",
            "can_use_tool": can_use_tool,
            "include_partial_messages": True,
            "disallowed_tools": ["WebFetch", "WebSearch"],
            "sandbox": {
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
                "network": {
                    "allowedDomains": sorted(
                        {
                            urlsplit(profile.url or "").hostname
                            for profile in request.mcp_profiles
                            if profile.transport == McpTransport.STREAMABLE_HTTP
                            and urlsplit(profile.url or "").hostname
                        }
                    ),
                    "allowManagedDomainsOnly": True,
                    "allowUnixSockets": [],
                    "allowAllUnixSockets": False,
                    "allowLocalBinding": False,
                },
            },
        }
        if (
            request.profile.auth_mode == HarnessAuthMode.SECRET_REF
            and request.profile.secret_ref
        ):
            options_kwargs["env"] = {
                "ANTHROPIC_API_KEY": _resolve_secret(
                    request.credential_store, request.profile.secret_ref
                )
            }
        if request.profile.executable:
            options_kwargs["cli_path"] = request.profile.executable
        options = sdk.ClaudeAgentOptions(
            **{key: value for key, value in options_kwargs.items() if value is not None}
        )
        client = sdk.ClaudeSDKClient(options=options)
        await client.connect()
        required_servers = {
            profile.name: profile.startup_timeout_seconds
            for profile in request.mcp_profiles
            if profile.required
        }
        if required_servers:
            try:
                await _wait_for_required_claude_mcp(client, required_servers)
            except Exception:
                await client.disconnect()
                raise
        return ClaudeAgentSdkConnection(
            client,
            permission_handler=request.permission_handler,
            sdk=sdk,
            external_session_id=request.session.external_session_id,
        )


async def _wait_for_required_claude_mcp(
    client: Any, required_servers: dict[str, float]
) -> None:
    """Wait only on idempotent SDK MCP status reads before an objective starts."""

    deadline = asyncio.get_running_loop().time() + max(required_servers.values())
    while True:
        response = await client.get_mcp_status()
        raw_statuses = response.get("mcpServers", []) if isinstance(response, dict) else []
        statuses = {
            str(item.get("name")): item
            for item in raw_statuses
            if isinstance(item, dict) and item.get("name")
        }
        missing = []
        for name in required_servers:
            status = statuses.get(name)
            state = str(status.get("status")) if status else "missing"
            if state in {"failed", "needs-auth", "disabled"}:
                detail = status.get("error") if status else None
                raise HarnessUnavailableError(
                    f"required MCP server {name!r} is {state}: "
                    f"{_bounded(detail or 'connection failed', limit=1_000)}"
                )
            if state != "connected":
                missing.append(name)
        if not missing:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise HarnessUnavailableError(
                "required MCP servers did not become ready: " + ", ".join(missing)
            )
        await asyncio.sleep(0.1)


def _claude_mcp_config(config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, item in config.items():
        if item["transport"] == McpTransport.STDIO.value:
            result[name] = {
                "type": "stdio",
                "command": item["command"],
                "args": item["args"],
                "env": item["env"],
            }
        else:
            result[name] = {
                "type": "http",
                "url": item["url"],
                "headers": item["headers"],
            }
    return result


def _parse_claude_mcp_name(value: str) -> tuple[str | None, str]:
    match = re.fullmatch(r"mcp__([^_]+(?:_[^_]+)*)__([\s\S]+)", value)
    if not match:
        return None, value
    return match.group(1), match.group(2)


def _safe_error(exc: BaseException) -> str:
    detail = redact_text(str(exc)).strip()
    return (detail or type(exc).__name__)[:1_000]


AdapterFactory = Callable[[HarnessKind], HarnessAdapter]
WorkspaceResolver = Callable[[str], Path]


@dataclass
class _ActiveTurn:
    turn_id: str
    connection: HarnessConnection
    task: asyncio.Task[Any] | None = None


class HarnessRuntimeService:
    """Own live harness connections and the durable cross-surface session lock."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        credential_store: CredentialStore,
        workspace_resolver: WorkspaceResolver,
        adapter_factory: AdapterFactory | None = None,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.credential_store = credential_store
        self.workspace_resolver = workspace_resolver
        self.adapter_factory = adapter_factory or self._default_adapter
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._connections: dict[str, HarnessConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, _ActiveTurn] = {}
        self._approval_futures: dict[str, asyncio.Future[HarnessPermissionDecision]] = {}
        self._mission_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @staticmethod
    def _default_adapter(kind: HarnessKind) -> HarnessAdapter:
        if kind == HarnessKind.CODEX_APP_SERVER:
            return CodexAppServerAdapter()
        return ClaudeAgentSdkAdapter()

    async def startup(self) -> None:
        """Mark uncertain in-flight work interrupted; never replay objectives."""

        for turn in self.store.list_entities(HarnessTurn, limit=1_000):
            if turn.status not in {
                HarnessTurnStatus.RUNNING,
                HarnessTurnStatus.WAITING_APPROVAL,
            }:
                continue
            self.store.update(
                HarnessTurn,
                turn.id,
                {
                    "status": HarnessTurnStatus.INTERRUPTED,
                    "completed_at": utc_now(),
                    "error": "Nebula Core restarted while the harness outcome was uncertain",
                },
                expected_revision=turn.revision,
            )
            self._interrupt_owner(turn)
            session = self.store.get(HarnessSession, turn.harness_session_id)
            if session.status not in {
                HarnessSessionStatus.CLOSED,
                HarnessSessionStatus.FAILED,
            }:
                self.store.update(
                    HarnessSession,
                    session.id,
                    {"status": HarnessSessionStatus.INTERRUPTED, "last_activity_at": utc_now()},
                    expected_revision=session.revision,
                )

    async def shutdown(self) -> None:
        self._closed = True
        active = list(self._active.items())
        for _, item in active:
            try:
                await item.connection.interrupt()
            except Exception:
                pass
            turn = self.store.get(HarnessTurn, item.turn_id)
            if turn.status in {
                HarnessTurnStatus.RUNNING,
                HarnessTurnStatus.WAITING_APPROVAL,
            }:
                self.store.update(
                    HarnessTurn,
                    turn.id,
                    {
                        "status": HarnessTurnStatus.INTERRUPTED,
                        "completed_at": utc_now(),
                        "error": "Nebula Core shut down during the turn",
                    },
                    expected_revision=turn.revision,
                )
                self._interrupt_owner(turn)
        tasks = [task for task in self._mission_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                pass
        await asyncio.gather(
            *(connection.close() for connection in self._connections.values()),
            return_exceptions=True,
        )
        self._connections.clear()

    async def health(self, profile_id: str) -> HarnessHealth:
        profile = self.store.get(HarnessProfile, profile_id)
        result = await self.adapter_factory(profile.kind).probe(
            profile, self.credential_store
        )
        capabilities = result.capabilities.model_copy(
            update={
                "checked_at": result.checked_at,
                "detail": result.detail,
                "harness_version": result.harness_version,
            }
        )
        self.store.update(
            HarnessProfile,
            profile.id,
            {"capabilities": capabilities},
            expected_revision=profile.revision,
        )
        return result

    def create_session(
        self,
        *,
        engagement_id: str,
        profile_id: str,
        model: str | None,
        mcp_server_ids: list[str] | None = None,
    ) -> HarnessSession:
        if self._closed:
            raise HarnessUnavailableError("harness runtime is shut down")
        profile = self.store.get(HarnessProfile, profile_id)
        if not profile.enabled:
            raise HarnessConfigurationError(f"harness {profile.id!r} is disabled")
        self.store.get_by_kind("engagements", engagement_id)
        selected_model = (model or profile.default_model or "").strip()
        if not selected_model:
            raise HarnessConfigurationError(
                "harness sessions require an explicit model or profile default"
            )
        ids = list(dict.fromkeys(mcp_server_ids or []))
        profiles = tuple(self.store.get(McpServerProfile, item) for item in ids)
        for mcp in profiles:
            if not mcp.enabled:
                raise HarnessConfigurationError(f"MCP server {mcp.id!r} is disabled")
        snapshot = [item.model_dump(mode="json") for item in profiles]
        session = HarnessSession(
            id=str(uuid4()),
            engagement_id=engagement_id,
            harness_profile_id=profile.id,
            model=selected_model,
            status=HarnessSessionStatus.STARTING,
            mcp_server_ids=ids,
            mcp_snapshot=snapshot,
            metadata={"context_management": "runtime_managed"},
        )
        return self.store.create(session)

    async def close_session(self, session_id: str) -> HarnessSession:
        session = self.store.get(HarnessSession, session_id)
        if session_id in self._active:
            raise HarnessStateError("cannot close a harness session with an active turn")
        connection = self._connections.pop(session_id, None)
        if connection is not None:
            await connection.close()
        if session.status == HarnessSessionStatus.CLOSED:
            return session
        return self.store.update(
            HarnessSession,
            session.id,
            {"status": HarnessSessionStatus.CLOSED, "last_activity_at": utc_now()},
            expected_revision=session.revision,
        )

    def prepare_chat(
        self,
        *,
        engagement_id: str,
        profile_id: str,
        model: str | None,
        prompt: str,
        chat_session_id: str | None,
        harness_session_id: str | None,
        mcp_server_ids: list[str] | None,
        title: str | None = None,
        runtime_context: str | None = None,
        citations: list[ChatCitation] | None = None,
        allow_remote_mcp: bool = False,
    ) -> tuple[ChatSession, ChatTurn, HarnessTurn]:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise HarnessConfigurationError("chat prompt cannot be empty")
        if chat_session_id:
            chat = self.store.get(ChatSession, chat_session_id)
            if chat.backend != ChatBackend.HARNESS:
                raise HarnessStateError("provider chats cannot switch to a harness")
            if chat.engagement_id != engagement_id or chat.harness_profile_id != profile_id:
                raise HarnessStateError("chat harness identity cannot change")
            if harness_session_id and harness_session_id != chat.harness_session_id:
                raise HarnessStateError("chat is attached to a different harness session")
            session = self.store.get(HarnessSession, chat.harness_session_id or "")
            self._validate_harness_privacy(
                engagement_id,
                self.store.get(HarnessProfile, profile_id),
                session.mcp_server_ids,
                allow_remote_mcp=allow_remote_mcp,
            )
            if mcp_server_ids:
                raise HarnessConfigurationError(
                    "MCP selection is frozen; it is accepted only for a new harness session"
                )
        else:
            if harness_session_id:
                session = self._compatible_session(
                    harness_session_id, engagement_id, profile_id, model
                )
                self._validate_harness_privacy(
                    engagement_id,
                    self.store.get(HarnessProfile, profile_id),
                    session.mcp_server_ids,
                    allow_remote_mcp=allow_remote_mcp,
                )
                if mcp_server_ids:
                    raise HarnessConfigurationError(
                        "MCP selection cannot change when attaching an existing session"
                    )
            else:
                self._validate_harness_privacy(
                    engagement_id,
                    self.store.get(HarnessProfile, profile_id),
                    mcp_server_ids or [],
                    allow_remote_mcp=allow_remote_mcp,
                )
                session = self.create_session(
                    engagement_id=engagement_id,
                    profile_id=profile_id,
                    model=model,
                    mcp_server_ids=mcp_server_ids,
                )
            chat = self.store.create(
                ChatSession(
                    id=str(uuid4()),
                    engagement_id=engagement_id,
                    title=(title or clean_prompt[:80]).strip() or "Harness conversation",
                    backend=ChatBackend.HARNESS,
                    harness_profile_id=profile_id,
                    harness_session_id=session.id,
                    model=session.model,
                    metadata={"context_management": "runtime_managed"},
                )
            )
        self._assert_idle(session)
        chat_turn = ChatTurn(
            id=str(uuid4()),
            engagement_id=engagement_id,
            session_id=chat.id,
            backend=ChatBackend.HARNESS,
            model=session.model,
            tools_enabled=bool(session.mcp_server_ids),
            request_snapshot={
                "runtime": "harness",
                "harness_profile_id": profile_id,
                "harness_session_id": session.id,
                "context_management": "runtime_managed",
            },
        )
        harness_turn = HarnessTurn(
            id=str(uuid4()),
            engagement_id=engagement_id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=chat.id,
            chat_turn_id=chat_turn.id,
            prompt=clean_prompt + (runtime_context or ""),
            metadata={
                "user_prompt": clean_prompt,
                "citations": [
                    item.model_dump(mode="json") for item in (citations or [])
                ],
            },
        )
        chat_turn = chat_turn.model_copy(update={"harness_turn_id": harness_turn.id})
        prior_messages = [
            item
            for item in self.store.list_entities(
                ChatMessage, engagement_id=engagement_id, limit=1_000
            )
            if item.session_id == chat.id
        ]
        with self.store.transaction() as transaction:
            transaction.add(chat_turn)
            transaction.add(harness_turn)
            sequence = max((item.sequence for item in prior_messages), default=0) + 1
            transaction.add(
                ChatMessage(
                    id=str(uuid4()),
                    engagement_id=engagement_id,
                    session_id=chat.id,
                    sequence=sequence,
                    role=ChatRole.USER,
                    content=clean_prompt,
                    model=session.model,
                    metadata={"harness_turn_id": harness_turn.id},
                )
            )
        return chat, chat_turn, harness_turn

    async def stream_turn(self, turn_id: str) -> AsyncIterator[HarnessEvent]:
        turn = self.store.get(HarnessTurn, turn_id)
        if turn.status != HarnessTurnStatus.QUEUED:
            raise HarnessStateError(
                f"harness turn is not queued ({turn.status.value})"
            )
        session = self.store.get(HarnessSession, turn.harness_session_id)
        lock = self._locks.setdefault(session.id, asyncio.Lock())
        if lock.locked() or session.id in self._active:
            raise HarnessStateError("harness session already has active work")
        async with lock:
            connection = await self._connection(session, turn)
            active = _ActiveTurn(turn_id=turn.id, connection=connection)
            self._active[session.id] = active
            turn = self.store.update(
                HarnessTurn,
                turn.id,
                {"status": HarnessTurnStatus.RUNNING, "started_at": utc_now()},
                expected_revision=turn.revision,
            )
            session = self.store.get(HarnessSession, session.id)
            session = self.store.update(
                HarnessSession,
                session.id,
                {
                    "status": HarnessSessionStatus.RUNNING,
                    "last_turn_id": turn.id,
                    "last_activity_at": utc_now(),
                    "external_session_id": connection.external_session_id,
                    "adapter_version": connection.adapter_version,
                },
                expected_revision=session.revision,
            )
            self._start_owner(turn)
            final_message = ""
            usage = ChatTokenUsage()
            external_turn_id: str | None = None
            interrupted_reason: str | None = None
            terminal_error: str | None = None
            try:
                async for event in connection.run_turn(turn.prompt, model=session.model):
                    event = event.model_copy(
                        update={
                            "origin": turn.origin,
                            "harness_profile_id": session.harness_profile_id,
                            "harness_session_id": session.id,
                            "harness_turn_id": turn.id,
                            "model": session.model,
                        }
                    )
                    if event.external_turn_id:
                        external_turn_id = event.external_turn_id
                    if event.type == "message_delta" and event.delta:
                        remaining = MAX_NORMALIZED_TEXT - len(final_message)
                        if remaining > 0:
                            final_message += event.delta[:remaining]
                    elif event.type == "completed" and event.message is not None:
                        final_message = event.message or final_message
                    elif event.type == "interrupted":
                        interrupted_reason = event.message or "Harness interrupted the turn"
                    elif event.type == "error":
                        terminal_error = event.message or "Harness reported an error"
                    elif event.type == "usage" and event.usage is not None:
                        usage = event.usage
                    elif event.type in {"tool_started", "tool_completed"}:
                        event = self._record_tool_event(turn, session, event)
                    if turn.origin == HarnessTurnOrigin.CHAT:
                        self.store.append_operation_event(
                            turn.id,
                            "harness_turn",
                            turn.engagement_id,
                            f"harness.{event.type}",
                            self._activity_payload(turn, session, event),
                        )
                    yield event
                    if interrupted_reason or terminal_error:
                        break
                if interrupted_reason or terminal_error:
                    await connection.interrupt()
                    self._fail_turn(
                        turn.id,
                        HarnessTurnStatus.INTERRUPTED,
                        interrupted_reason or terminal_error or "Harness turn interrupted",
                    )
                    return
                session = self.store.get(HarnessSession, session.id)
                if connection.external_session_id != session.external_session_id:
                    session = self.store.update(
                        HarnessSession,
                        session.id,
                        {
                            "external_session_id": connection.external_session_id,
                            "last_activity_at": utc_now(),
                        },
                        expected_revision=session.revision,
                    )
                turn = self.store.get(HarnessTurn, turn.id)
                turn = self.store.update(
                    HarnessTurn,
                    turn.id,
                    {
                        "status": HarnessTurnStatus.COMPLETE,
                        "response": _bounded(final_message, limit=MAX_NORMALIZED_TEXT),
                        "usage": usage,
                        "external_turn_id": external_turn_id,
                        "completed_at": utc_now(),
                    },
                    expected_revision=turn.revision,
                )
                self._complete_owner(turn, final_message, usage)
                session = self.store.get(HarnessSession, session.id)
                self.store.update(
                    HarnessSession,
                    session.id,
                    {"status": HarnessSessionStatus.IDLE, "last_activity_at": utc_now()},
                    expected_revision=session.revision,
                )
            except asyncio.CancelledError:
                await connection.interrupt()
                self._fail_turn(turn.id, HarnessTurnStatus.CANCELLED, "Turn cancelled")
                raise
            except Exception as exc:
                self._fail_turn(turn.id, HarnessTurnStatus.INTERRUPTED, _safe_error(exc))
                yield HarnessEvent(
                    type="error",
                    harness_session_id=session.id,
                    harness_turn_id=turn.id,
                    message=_safe_error(exc),
                )
            finally:
                self._active.pop(session.id, None)

    async def start_mission(
        self,
        *,
        engagement_id: str,
        objective: str,
        profile_id: str,
        model: str | None,
        budget: RunBudget,
        harness_session_id: str | None = None,
        mcp_server_ids: list[str] | None = None,
        actor_id: str = "system",
        allow_remote_mcp: bool = False,
    ) -> AgentRun:
        profile = self.store.get(HarnessProfile, profile_id)
        if harness_session_id:
            session = self._compatible_session(
                harness_session_id, engagement_id, profile.id, model
            )
            self._validate_harness_privacy(
                engagement_id,
                profile,
                session.mcp_server_ids,
                allow_remote_mcp=allow_remote_mcp,
            )
            if mcp_server_ids:
                raise HarnessConfigurationError(
                    "MCP selection is frozen for an existing harness session"
                )
        else:
            self._validate_harness_privacy(
                engagement_id,
                profile,
                mcp_server_ids or [],
                allow_remote_mcp=allow_remote_mcp,
            )
            session = self.create_session(
                engagement_id=engagement_id,
                profile_id=profile.id,
                model=model,
                mcp_server_ids=mcp_server_ids,
            )
        self._assert_idle(session)
        run = AgentRun(
            id=str(uuid4()),
            engagement_id=engagement_id,
            objective=objective.strip(),
            status=RunStatus.QUEUED,
            backend=RunBackend.HARNESS,
            supervisor_model=session.model,
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            runtime_snapshot={
                "kind": profile.kind.value,
                "harness_profile_id": profile.id,
                "harness_session_id": session.id,
                "adapter_contract": ADAPTER_CONTRACT_VERSION,
                "mcp_server_ids": session.mcp_server_ids,
                "mcp_snapshot": session.mcp_snapshot,
                "remote_mcp_confirmed": allow_remote_mcp,
            },
            budget=budget,
            metadata={"origin": "api", "analysis_only": False},
        )
        turn = HarnessTurn(
            id=str(uuid4()),
            engagement_id=engagement_id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.MISSION,
            run_id=run.id,
            prompt=run.objective,
        )
        with self.store.transaction() as transaction:
            transaction.add(run)
            transaction.add(turn)
        for chat in self._attached_chats(session.id):
            self._append_chat_handoff(
                chat,
                role=ChatRole.USER,
                content=run.objective,
                run_id=run.id,
                usage=None,
            )
        self.store.append_event(
            run.id,
            "run.queued",
            {
                "backend": "harness",
                "harness_profile_id": profile.id,
                "harness_session_id": session.id,
                "harness_turn_id": turn.id,
                "model": session.model,
            },
            actor_id=actor_id,
            idempotency_key="run:queued",
        )
        task = asyncio.create_task(
            self._execute_mission(run.id, turn.id), name=f"harness-mission-{run.id}"
        )
        self._mission_tasks[run.id] = task
        return run

    async def _execute_mission(self, run_id: str, turn_id: str) -> None:
        try:
            run = self.store.get(AgentRun, run_id)
            durable_turn = self.store.get(HarnessTurn, turn_id)
            durable_session = self.store.get(
                HarnessSession, durable_turn.harness_session_id
            )
            try:
                async with asyncio.timeout(run.budget.max_duration_seconds):
                    async for event in self.stream_turn(turn_id):
                        self.store.append_event(
                            run_id,
                            f"harness.{event.type}",
                            self._activity_payload(durable_turn, durable_session, event),
                            idempotency_key=None,
                        )
            except TimeoutError:
                await self.cancel_turn(
                    turn_id, reason="Harness mission exceeded its duration limit"
                )
                latest = self.store.get(AgentRun, run_id)
                if latest.status not in {
                    RunStatus.COMPLETE,
                    RunStatus.CANCELLED,
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                }:
                    self.store.update(
                        AgentRun,
                        latest.id,
                        {"status": RunStatus.FAILED, "completed_at": utc_now()},
                        expected_revision=latest.revision,
                    )
                self.store.append_event(
                    run_id,
                    "run.failed",
                    {"reason": "duration_limit", "harness_turn_id": turn_id},
                )
        finally:
            self._mission_tasks.pop(run_id, None)

    async def stop(self, run_id: str, *, reason: str, actor_id: str) -> AgentRun:
        run = self.store.get(AgentRun, run_id)
        if run.backend != RunBackend.HARNESS:
            raise HarnessStateError("run is not owned by the harness runtime")
        if run.status in {
            RunStatus.COMPLETE,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            raise HarnessStateError(f"run is already terminal ({run.status.value})")
        session_id = run.harness_session_id or ""
        active = self._active.get(session_id)
        if active is not None:
            await active.connection.interrupt()
        task = self._mission_tasks.get(run.id)
        if task is not None and not task.done():
            task.cancel()
        latest = self.store.get(AgentRun, run.id)
        if latest.status not in {RunStatus.CANCELLED, RunStatus.COMPLETE}:
            latest, _ = self.store.update_with_event(
                AgentRun,
                latest.id,
                {"status": RunStatus.CANCELLED, "completed_at": utc_now()},
                expected_revision=latest.revision,
                run_id=latest.id,
                event_type="run.cancelled",
                event_payload={"reason": reason},
                actor_id=actor_id,
                idempotency_key="run:cancelled",
            )
        return latest

    async def steer(self, run_id: str, text: str, *, actor_id: str) -> HarnessTurn:
        run = self.store.get(AgentRun, run_id)
        if run.backend != RunBackend.HARNESS or not run.harness_session_id:
            raise HarnessStateError("only active harness runs can be steered")
        active = self._active.get(run.harness_session_id)
        if active is None:
            raise HarnessStateError("harness run has no active turn")
        await active.connection.steer(text.strip())
        self.store.append_event(
            run.id,
            "harness.steered",
            {"harness_turn_id": active.turn_id, "text": _bounded(text, limit=10_000)},
            actor_id=actor_id,
        )
        return self.store.get(HarnessTurn, active.turn_id)

    async def resolve_approval(self, approval: Approval) -> None:
        future = self._approval_futures.pop(approval.id, None)
        if future is None or future.done():
            raise HarnessStateError("harness permission request is no longer active")
        allowed = approval.status == ApprovalStatus.APPROVED
        if approval.tool_call_id:
            call = self.store.get(ToolCall, approval.tool_call_id)
            self.store.update(
                ToolCall,
                call.id,
                {
                    "status": (
                        ToolCallStatus.APPROVED if allowed else ToolCallStatus.DENIED
                    ),
                    "error": None if allowed else approval.decision_note or "Denied by operator",
                },
                expected_revision=call.revision,
            )
        future.set_result(
            HarnessPermissionDecision(
                allowed=allowed,
                approval_id=approval.id,
                reason=approval.decision_note,
            )
        )

    async def cancel_turn(self, harness_turn_id: str, *, reason: str) -> HarnessTurn:
        turn = self.store.get(HarnessTurn, harness_turn_id)
        active = self._active.get(turn.harness_session_id)
        if active is not None and active.turn_id == turn.id:
            await active.connection.interrupt()
        for approval_id, future in list(self._approval_futures.items()):
            try:
                approval = self.store.get(Approval, approval_id)
            except NotFoundError:
                continue
            if approval.run_id not in {turn.run_id, turn.chat_turn_id}:
                continue
            self._approval_futures.pop(approval_id, None)
            if not future.done():
                future.set_result(
                    HarnessPermissionDecision(allowed=False, reason=reason)
                )
        latest = self.store.get(HarnessTurn, turn.id)
        if latest.status not in {
            HarnessTurnStatus.COMPLETE,
            HarnessTurnStatus.FAILED,
            HarnessTurnStatus.CANCELLED,
            HarnessTurnStatus.INTERRUPTED,
        }:
            latest = self.store.update(
                HarnessTurn,
                latest.id,
                {
                    "status": HarnessTurnStatus.CANCELLED,
                    "error": reason[:1_000],
                    "completed_at": utc_now(),
                },
                expected_revision=latest.revision,
            )
        if latest.chat_turn_id:
            chat_turn = self.store.get(ChatTurn, latest.chat_turn_id)
            if chat_turn.status not in {
                ChatTurnStatus.COMPLETE,
                ChatTurnStatus.CANCELLED,
                ChatTurnStatus.FAILED,
                ChatTurnStatus.INTERRUPTED,
            }:
                self.store.update(
                    ChatTurn,
                    chat_turn.id,
                    {"status": ChatTurnStatus.CANCELLED, "error": reason[:1_000]},
                    expected_revision=chat_turn.revision,
                )
        return latest

    def attach_run_to_chat(self, run_id: str) -> ChatSession:
        run = self.store.get(AgentRun, run_id)
        if run.backend != RunBackend.HARNESS or not run.harness_session_id:
            raise HarnessStateError("only harness runs can be discussed in harness chat")
        existing = [
            item
            for item in self.store.list_entities(
                ChatSession, engagement_id=run.engagement_id, limit=1_000
            )
            if item.harness_session_id == run.harness_session_id
        ]
        if existing:
            return existing[0]
        session = self.store.get(HarnessSession, run.harness_session_id)
        chat = self.store.create(
            ChatSession(
                id=str(uuid4()),
                engagement_id=run.engagement_id,
                title=run.objective[:80] or "Mission discussion",
                backend=ChatBackend.HARNESS,
                harness_profile_id=run.harness_profile_id,
                harness_session_id=session.id,
                model=session.model,
                metadata={"attached_run_ids": [run.id], "context_management": "runtime_managed"},
            )
        )
        messages = [
            ChatMessage(
                id=str(uuid4()),
                engagement_id=run.engagement_id,
                session_id=chat.id,
                sequence=1,
                role=ChatRole.USER,
                content=run.objective,
                model=session.model,
                metadata={"run_id": run.id, "handoff": "mission_to_chat"},
            )
        ]
        turns = [
            turn
            for turn in self.store.list_entities(
                HarnessTurn, engagement_id=run.engagement_id, limit=1_000
            )
            if turn.run_id == run.id and turn.response
        ]
        if turns:
            messages.append(
                ChatMessage(
                    id=str(uuid4()),
                    engagement_id=run.engagement_id,
                    session_id=chat.id,
                    sequence=2,
                    role=ChatRole.ASSISTANT,
                    content=turns[-1].response or "",
                    model=session.model,
                    usage=turns[-1].usage,
                    metadata={"run_id": run.id, "handoff": "mission_to_chat"},
                )
            )
        with self.store.transaction() as transaction:
            for message in messages:
                transaction.add(message)
        return chat

    def _compatible_session(
        self,
        session_id: str,
        engagement_id: str,
        profile_id: str,
        model: str | None,
    ) -> HarnessSession:
        session = self.store.get(HarnessSession, session_id)
        if (
            session.engagement_id != engagement_id
            or session.harness_profile_id != profile_id
            or (model and session.model != model)
        ):
            raise HarnessConfigurationError(
                "existing harness session is not compatible with this engagement/profile/model"
            )
        if session.status == HarnessSessionStatus.CLOSED:
            raise HarnessStateError("harness session is closed")
        return session

    def _validate_harness_privacy(
        self,
        engagement_id: str,
        profile: HarnessProfile,
        mcp_server_ids: list[str],
        *,
        allow_remote_mcp: bool,
    ) -> None:
        engagement = self.store.get(Engagement, engagement_id)
        if engagement.scope_policy_id:
            scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
            if scope.engagement_id != engagement.id:
                raise HarnessConfigurationError(
                    "engagement scope policy belongs to a different engagement"
                )
            if scope.local_only and not profile.privacy.local_only:
                raise HarnessConfigurationError(
                    "engagement scope is local-only and cannot use this harness"
                )
        if mcp_server_ids and not profile.privacy.local_only:
            if not profile.privacy.permits_sensitive_data:
                raise HarnessConfigurationError(
                    "harness profile does not permit MCP results to reach its model"
                )
            if not allow_remote_mcp:
                raise HarnessConfigurationError(
                    "remote harness MCP use requires explicit operator confirmation"
                )

    def _assert_idle(self, session: HarnessSession) -> None:
        reserved = any(
            turn.harness_session_id == session.id
            and turn.status
            in {
                HarnessTurnStatus.QUEUED,
                HarnessTurnStatus.RUNNING,
                HarnessTurnStatus.WAITING_APPROVAL,
            }
            for turn in self.store.list_entities(
                HarnessTurn, engagement_id=session.engagement_id, limit=1_000
            )
        )
        if session.id in self._active or reserved or session.status in {
            HarnessSessionStatus.RUNNING,
            HarnessSessionStatus.WAITING_APPROVAL,
        }:
            raise HarnessStateError("harness session already has active work")

    async def _connection(
        self, session: HarnessSession, turn: HarnessTurn
    ) -> HarnessConnection:
        existing = self._connections.get(session.id)
        if existing is not None:
            return existing
        profile = self.store.get(HarnessProfile, session.harness_profile_id)
        mcp_profiles = tuple(
            McpServerProfile.model_validate(snapshot) for snapshot in session.mcp_snapshot
        )

        async def permission_handler(
            request: HarnessPermissionRequest,
        ) -> PermissionTicket:
            return await self._request_permission(turn.id, request)

        connection = await self.adapter_factory(profile.kind).open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=self.workspace_resolver(session.engagement_id),
                mcp_profiles=mcp_profiles,
                credential_store=self.credential_store,
                permission_handler=permission_handler,
            )
        )
        self._connections[session.id] = connection
        return connection

    async def _request_permission(
        self, turn_id: str, request: HarnessPermissionRequest
    ) -> PermissionTicket:
        turn = self.store.get(HarnessTurn, turn_id)
        session = self.store.get(HarnessSession, turn.harness_session_id)
        policy, server, tool, risk, rationale = self._permission_policy(session, request)
        if turn.run_id:
            run = self.store.get(AgentRun, turn.run_id)
            if len(turn.tool_call_ids) >= run.budget.max_tool_calls:
                policy = McpApprovalMode.DENY
                rationale = "Harness mission reached its durable tool-call limit"
        origin = (
            ToolCallOrigin.CHAT
            if turn.origin == HarnessTurnOrigin.CHAT
            else ToolCallOrigin.MISSION
        )
        call = ToolCall(
            id=str(uuid4()),
            engagement_id=turn.engagement_id,
            run_id=turn.chat_turn_id or turn.run_id or turn.id,
            origin=origin,
            chat_session_id=turn.chat_session_id,
            chat_turn_id=turn.chat_turn_id,
            tool_name=(
                f"mcp:{server.id}:{tool.name}" if server and tool else request.vendor_name
            ),
            mcp_server_id=server.id if server else None,
            mcp_tool_name=tool.name if tool else None,
            vendor_tool_name=request.vendor_name,
            status=ToolCallStatus.PROPOSED,
            risk_class=risk,
            arguments=_bounded(request.arguments, limit=MAX_TOOL_ARGUMENT_TEXT),
            idempotency_key=f"harness:{turn.id}:{request.vendor_request_id}",
            metadata={"harness_turn_id": turn.id, "category": request.category},
        )
        self.store.create(call)
        latest = self.store.get(HarnessTurn, turn.id)
        self.store.update(
            HarnessTurn,
            latest.id,
            {"tool_call_ids": [*latest.tool_call_ids, call.id]},
            expected_revision=latest.revision,
        )
        future: asyncio.Future[HarnessPermissionDecision] = (
            asyncio.get_running_loop().create_future()
        )
        if policy == McpApprovalMode.ALLOW:
            self.store.update(
                ToolCall,
                call.id,
                {"status": ToolCallStatus.APPROVED},
                expected_revision=call.revision,
            )
            future.set_result(HarnessPermissionDecision(allowed=True, reason=rationale))
            return PermissionTicket(None, call.id, future)
        if policy == McpApprovalMode.DENY:
            self.store.update(
                ToolCall,
                call.id,
                {"status": ToolCallStatus.DENIED, "error": rationale},
                expected_revision=call.revision,
            )
            future.set_result(HarnessPermissionDecision(allowed=False, reason=rationale))
            return PermissionTicket(None, call.id, future)
        approval = Approval(
            id=str(uuid4()),
            engagement_id=turn.engagement_id,
            run_id=turn.chat_turn_id or turn.run_id or turn.id,
            origin=origin,
            chat_session_id=turn.chat_session_id,
            chat_turn_id=turn.chat_turn_id,
            tool_call_id=call.id,
            risk_class=risk,
            exact_request={
                "category": request.category,
                "server_id": server.id if server else None,
                "tool": tool.name if tool else request.vendor_name,
                "arguments": _bounded(request.arguments, limit=MAX_TOOL_ARGUMENT_TEXT),
                "argument_editing": False,
            },
            policy_rationale=rationale,
            requested_by="harness",
        )
        with self.store.transaction() as transaction:
            transaction.add(approval)
            transaction.update(
                ToolCall,
                call.id,
                {
                    "status": ToolCallStatus.WAITING_APPROVAL,
                    "approval_id": approval.id,
                },
                expected_revision=call.revision,
            )
        self._approval_futures[approval.id] = future
        latest_turn = self.store.get(HarnessTurn, turn.id)
        self.store.update(
            HarnessTurn,
            latest_turn.id,
            {"status": HarnessTurnStatus.WAITING_APPROVAL},
            expected_revision=latest_turn.revision,
        )
        session = self.store.get(HarnessSession, session.id)
        self.store.update(
            HarnessSession,
            session.id,
            {"status": HarnessSessionStatus.WAITING_APPROVAL, "last_activity_at": utc_now()},
            expected_revision=session.revision,
        )
        self._waiting_owner(turn)

        def restore(_: asyncio.Future[HarnessPermissionDecision]) -> None:
            latest_turn = self.store.get(HarnessTurn, turn.id)
            if latest_turn.status == HarnessTurnStatus.WAITING_APPROVAL:
                self.store.update(
                    HarnessTurn,
                    turn.id,
                    {"status": HarnessTurnStatus.RUNNING},
                    expected_revision=latest_turn.revision,
                )
            latest_session = self.store.get(HarnessSession, session.id)
            if latest_session.status == HarnessSessionStatus.WAITING_APPROVAL:
                self.store.update(
                    HarnessSession,
                    session.id,
                    {"status": HarnessSessionStatus.RUNNING, "last_activity_at": utc_now()},
                    expected_revision=latest_session.revision,
                )
            self._start_owner(turn)

        future.add_done_callback(restore)
        return PermissionTicket(approval.id, call.id, future)

    def _permission_policy(
        self, session: HarnessSession, request: HarnessPermissionRequest
    ) -> tuple[
        McpApprovalMode,
        McpServerProfile | None,
        McpToolSnapshot | None,
        RiskClass,
        str,
    ]:
        if request.category != "mcp":
            risk = RiskClass.WORKSPACE_WRITE if request.category == "file" else RiskClass.ACTIVE_SCAN
            return (
                McpApprovalMode.ASK,
                None,
                None,
                risk,
                "Harness built-in actions require an exact operator decision",
            )
        profiles = [McpServerProfile.model_validate(item) for item in session.mcp_snapshot]
        server = next(
            (
                item
                for item in profiles
                if request.server_name in {item.name, _claude_server_name(item.name), item.id}
            ),
            None,
        )
        if server is None:
            return (
                McpApprovalMode.DENY,
                None,
                None,
                RiskClass.CREDENTIAL_USE,
                "Unknown MCP server failed closed",
            )
        tool = next(
            (item for item in server.capabilities.tools if item.name == request.tool_name),
            None,
        )
        if tool is None:
            return (
                McpApprovalMode.DENY,
                server,
                None,
                RiskClass.CREDENTIAL_USE,
                "Unknown MCP tool failed closed",
            )
        if server.enabled_tools and tool.name not in server.enabled_tools:
            return McpApprovalMode.DENY, server, tool, RiskClass.ACTIVE_SCAN, "Tool is outside the MCP allow list"
        if tool.name in server.disabled_tools:
            return McpApprovalMode.DENY, server, tool, RiskClass.ACTIVE_SCAN, "Tool is explicitly disabled"
        override = server.tool_overrides.get(tool.name)
        if override is not None:
            mode = override
            rationale = f"Exact per-tool override is {mode.value}"
        else:
            mode = server.default_approval
            rationale = f"Server default approval policy is {mode.value}"
        if mode == McpApprovalMode.ALLOW and override is None and tool.destructive:
            mode = McpApprovalMode.ASK
            rationale = "Destructive tools cannot be globally auto-approved"
        if mode == McpApprovalMode.RISK_BASED:
            trusted_annotations = server.capabilities.checked_at is not None
            safe = (
                trusted_annotations
                and tool.annotations_complete
                and tool.read_only
                and not tool.destructive
                and not tool.open_world
                and tool.credentialed is False
            )
            mode = McpApprovalMode.ALLOW if safe else McpApprovalMode.ASK
            rationale = (
                "Verified read-only, non-destructive, closed-world, credential-free MCP tool"
                if safe
                else "MCP tool has write, destructive, open-world, credential, or untrusted annotations"
            )
        if tool.credentialed:
            risk = RiskClass.CREDENTIAL_USE
        elif tool.destructive:
            risk = RiskClass.DESTRUCTIVE
        elif tool.read_only:
            risk = RiskClass.LOCAL_READ
        else:
            risk = RiskClass.WORKSPACE_WRITE
        return mode, server, tool, risk, rationale

    def _record_tool_event(
        self, turn: HarnessTurn, session: HarnessSession, event: HarnessEvent
    ) -> HarnessEvent:
        profiles = [McpServerProfile.model_validate(item) for item in session.mcp_snapshot]
        server = next(
            (
                item
                for item in profiles
                if event.server_id in {item.id, item.name, _claude_server_name(item.name)}
            ),
            None,
        )
        if server is None:
            return event
        existing = [
            call
            for call in self.store.list_entities(
                ToolCall, engagement_id=turn.engagement_id, limit=1_000
            )
            if call.metadata.get("harness_turn_id") == turn.id
            and call.mcp_server_id == server.id
            and call.mcp_tool_name == event.tool_name
            and call.status not in {
                ToolCallStatus.COMPLETE,
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
            }
        ]
        call = existing[-1] if existing else None
        if call is None:
            call = self.store.create(
                ToolCall(
                    id=str(uuid4()),
                    engagement_id=turn.engagement_id,
                    run_id=turn.chat_turn_id or turn.run_id or turn.id,
                    origin=(
                        ToolCallOrigin.CHAT
                        if turn.origin == HarnessTurnOrigin.CHAT
                        else ToolCallOrigin.MISSION
                    ),
                    chat_session_id=turn.chat_session_id,
                    chat_turn_id=turn.chat_turn_id,
                    tool_name=f"mcp:{server.id}:{event.tool_name}",
                    mcp_server_id=server.id,
                    mcp_tool_name=event.tool_name,
                    vendor_tool_name=f"{event.server_id}:{event.tool_name}",
                    status=ToolCallStatus.RUNNING,
                    risk_class=RiskClass.ACTIVE_SCAN,
                    arguments=_bounded(event.payload.get("arguments", {}), limit=MAX_TOOL_ARGUMENT_TEXT),
                    started_at=utc_now(),
                    metadata={"harness_turn_id": turn.id},
                )
            )
        elif event.type == "tool_started" and call.status == ToolCallStatus.APPROVED:
            call = self.store.update(
                ToolCall,
                call.id,
                {"status": ToolCallStatus.RUNNING, "started_at": utc_now()},
                expected_revision=call.revision,
            )
        if event.type == "tool_completed":
            failed = bool(event.payload.get("error"))
            call = self.store.get(ToolCall, call.id)
            call = self.store.update(
                ToolCall,
                call.id,
                {
                    "status": ToolCallStatus.FAILED if failed else ToolCallStatus.COMPLETE,
                    "result": _bounded(event.payload.get("result"), limit=MAX_TOOL_RESULT_TEXT),
                    "error": _safe_error(Exception(str(event.payload.get("error")))) if failed else None,
                    "completed_at": utc_now(),
                },
                expected_revision=call.revision,
            )
        return event.model_copy(update={"tool_call_id": call.id, "server_id": server.id})

    @staticmethod
    def _activity_payload(
        turn: HarnessTurn, session: HarnessSession, event: HarnessEvent
    ) -> dict[str, Any]:
        payload = _bounded(
            event.model_dump(mode="json"), limit=MAX_TOOL_RESULT_TEXT
        )
        if not isinstance(payload, dict):
            payload = {}
        identity = (
            f"MCP {event.server_id}/{event.tool_name}"
            if event.server_id and event.tool_name
            else "Harness"
        )
        if event.type == "message_delta" and event.delta:
            summary = f"{turn.origin.value} · streamed: {event.delta[:240]}"
        elif event.type == "approval_required":
            summary = f"{turn.origin.value} · {identity} waiting for approval"
        else:
            summary = f"{turn.origin.value} · {identity} {event.type.replace('_', ' ')}"
        payload.update(
            {
                "summary": summary,
                "originating_surface": turn.origin.value,
                "harness_profile_id": session.harness_profile_id,
                "harness_session_id": session.id,
                "harness_turn_id": turn.id,
            }
        )
        return payload

    def _start_owner(self, turn: HarnessTurn) -> None:
        if turn.origin == HarnessTurnOrigin.CHAT and turn.chat_turn_id:
            chat_turn = self.store.get(ChatTurn, turn.chat_turn_id)
            if chat_turn.status in {
                ChatTurnStatus.ROUTING,
                ChatTurnStatus.WAITING_APPROVAL,
            }:
                self.store.update(
                    ChatTurn,
                    chat_turn.id,
                    {"status": ChatTurnStatus.ROUTING},
                    expected_revision=chat_turn.revision,
                )
        elif turn.run_id:
            run = self.store.get(AgentRun, turn.run_id)
            if run.status in {RunStatus.QUEUED, RunStatus.WAITING_APPROVAL}:
                changes: dict[str, Any] = {"status": RunStatus.RUNNING}
                if run.started_at is None:
                    changes["started_at"] = utc_now()
                self.store.update(
                    AgentRun, run.id, changes, expected_revision=run.revision
                )

    def _waiting_owner(self, turn: HarnessTurn) -> None:
        if turn.origin == HarnessTurnOrigin.CHAT and turn.chat_turn_id:
            chat_owner = self.store.get(ChatTurn, turn.chat_turn_id)
            self.store.update(
                ChatTurn,
                chat_owner.id,
                {"status": ChatTurnStatus.WAITING_APPROVAL},
                expected_revision=chat_owner.revision,
            )
        elif turn.run_id:
            run_owner = self.store.get(AgentRun, turn.run_id)
            self.store.update(
                AgentRun,
                run_owner.id,
                {"status": RunStatus.WAITING_APPROVAL},
                expected_revision=run_owner.revision,
            )

    def _complete_owner(
        self, turn: HarnessTurn, final_message: str, usage: ChatTokenUsage
    ) -> None:
        if turn.origin == HarnessTurnOrigin.CHAT and turn.chat_turn_id and turn.chat_session_id:
            chat_turn = self.store.get(ChatTurn, turn.chat_turn_id)
            existing = [
                item
                for item in self.store.list_entities(
                    ChatMessage, engagement_id=turn.engagement_id, limit=1_000
                )
                if item.session_id == turn.chat_session_id
            ]
            message = ChatMessage(
                id=str(uuid4()),
                engagement_id=turn.engagement_id,
                session_id=turn.chat_session_id,
                sequence=max((item.sequence for item in existing), default=0) + 1,
                role=ChatRole.ASSISTANT,
                content=final_message or "Harness completed without a text response.",
                model=self.store.get(HarnessSession, turn.harness_session_id).model,
                usage=usage,
                citations=[
                    ChatCitation.model_validate(item)
                    for item in turn.metadata.get("citations", [])
                    if isinstance(item, dict)
                ],
                metadata={"harness_turn_id": turn.id},
            )
            with self.store.transaction() as transaction:
                transaction.add(message)
                transaction.update(
                    ChatTurn,
                    chat_turn.id,
                    {
                        "status": ChatTurnStatus.COMPLETE,
                        "usage": usage,
                        "final_message_id": message.id,
                    },
                    expected_revision=chat_turn.revision,
                )
        elif turn.run_id:
            run = self.store.get(AgentRun, turn.run_id)
            if run.status not in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
                run, _ = self.store.update_with_event(
                    AgentRun,
                    run.id,
                    {"status": RunStatus.COMPLETE, "completed_at": utc_now()},
                    expected_revision=run.revision,
                    run_id=run.id,
                    event_type="run.completed",
                    event_payload={"harness_turn_id": turn.id, "usage": usage.model_dump(mode="json")},
                    idempotency_key="run:completed",
                )
                for chat in self._attached_chats(turn.harness_session_id):
                    self._append_chat_handoff(
                        chat,
                        role=ChatRole.ASSISTANT,
                        content=final_message or "Harness mission completed without a text response.",
                        run_id=run.id,
                        usage=usage,
                    )

    def _interrupt_owner(self, turn: HarnessTurn) -> None:
        if turn.origin == HarnessTurnOrigin.CHAT and turn.chat_turn_id:
            chat_owner = self.store.get(ChatTurn, turn.chat_turn_id)
            if chat_owner.status not in {
                ChatTurnStatus.COMPLETE,
                ChatTurnStatus.CANCELLED,
                ChatTurnStatus.FAILED,
                ChatTurnStatus.INTERRUPTED,
            }:
                self.store.update(
                    ChatTurn,
                    chat_owner.id,
                    {"status": ChatTurnStatus.INTERRUPTED, "error": turn.error},
                    expected_revision=chat_owner.revision,
                )
        elif turn.run_id:
            run_owner = self.store.get(AgentRun, turn.run_id)
            if run_owner.status not in {
                RunStatus.COMPLETE,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.INTERRUPTED,
            }:
                self.store.update(
                    AgentRun,
                    run_owner.id,
                    {"status": RunStatus.INTERRUPTED, "completed_at": utc_now()},
                    expected_revision=run_owner.revision,
                )

    def _fail_turn(
        self, turn_id: str, status: HarnessTurnStatus, error: str
    ) -> None:
        turn = self.store.get(HarnessTurn, turn_id)
        if turn.status not in {
            HarnessTurnStatus.COMPLETE,
            HarnessTurnStatus.CANCELLED,
            HarnessTurnStatus.FAILED,
            HarnessTurnStatus.INTERRUPTED,
        }:
            turn = self.store.update(
                HarnessTurn,
                turn.id,
                {"status": status, "error": error, "completed_at": utc_now()},
                expected_revision=turn.revision,
            )
        self._interrupt_owner(turn)
        session = self.store.get(HarnessSession, turn.harness_session_id)
        if session.status != HarnessSessionStatus.CLOSED:
            self.store.update(
                HarnessSession,
                session.id,
                {"status": HarnessSessionStatus.INTERRUPTED, "last_activity_at": utc_now()},
                expected_revision=session.revision,
            )

    def _attached_chats(self, harness_session_id: str) -> list[ChatSession]:
        return [
            chat
            for chat in self.store.list_entities(ChatSession, limit=1_000)
            if chat.backend == ChatBackend.HARNESS
            and chat.harness_session_id == harness_session_id
        ]

    def _append_chat_handoff(
        self,
        chat: ChatSession,
        *,
        role: ChatRole,
        content: str,
        run_id: str,
        usage: ChatTokenUsage | None,
    ) -> ChatMessage:
        messages = [
            message
            for message in self.store.list_entities(
                ChatMessage, engagement_id=chat.engagement_id, limit=1_000
            )
            if message.session_id == chat.id
        ]
        return self.store.create(
            ChatMessage(
                id=str(uuid4()),
                engagement_id=chat.engagement_id,
                session_id=chat.id,
                sequence=max((message.sequence for message in messages), default=0) + 1,
                role=role,
                content=content,
                model=chat.model,
                usage=usage,
                metadata={"run_id": run_id, "handoff": "chat_mission_shared_session"},
            )
        )


def _claude_server_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


__all__ = [
    "AdapterOpenRequest",
    "ClaudeAgentSdkAdapter",
    "CodexAppServerAdapter",
    "HarnessAdapter",
    "HarnessCatalogItem",
    "HarnessConfigurationError",
    "HarnessConnection",
    "HarnessError",
    "HarnessEvent",
    "HarnessHealth",
    "HarnessPermissionDecision",
    "HarnessPermissionRequest",
    "HarnessRuntimeService",
    "HarnessStateError",
    "HarnessTransportError",
    "HarnessUnavailableError",
    "PermissionTicket",
    "harness_catalog",
]
