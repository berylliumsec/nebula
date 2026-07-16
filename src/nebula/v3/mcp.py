"""Explicit MCP discovery without inheriting ambient user configuration."""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    gather_diagnostic,
    record_caught_exception,
)

import asyncio
import json
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field

from .credentials import CredentialError, CredentialStore
from .domain import (
    McpAuthMode,
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpCwdPolicy,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    NebulaModel,
    RiskClass,
    utc_now,
)
from .redaction import redact_text
from .storage import NebulaStore

MCP_PROTOCOL_VERSION = "2025-06-18"
# A frozen Core relaunch may need one-file extraction and platform verification.
GATEWAY_STARTUP_TIMEOUT_SECONDS = 30.0
MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_MCP_TOOL_RESPONSE_BYTES = 100 * 1024 * 1024


class McpProbeError(RuntimeError):
    """An operator-safe discovery failure."""


class McpProbeReport(NebulaModel):
    profile_id: str
    compatible: bool
    capabilities: McpCapabilitySnapshot
    detail: str | None = Field(default=None, max_length=1_000)


class _McpClient:
    def __init__(self, *, response_limit: int = MAX_MCP_MESSAGE_BYTES) -> None:
        self.next_id = 1
        self.response_limit = response_limit

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self.next_id
        self.next_id += 1
        response = await self.exchange(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                **({"params": params} if params is not None else {}),
            }
        )
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise McpProbeError(f"MCP {method} returned an uncorrelated response")
        if response.get("error") is not None:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise McpProbeError(f"MCP {method} failed: {_safe(message)}")
        return response.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.exchange(
            {
                "jsonrpc": "2.0",
                "method": method,
                **({"params": params} if params is not None else {}),
            },
            notification=True,
        )

    async def exchange(
        self, message: dict[str, Any], *, notification: bool = False
    ) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class _StdioMcpClient(_McpClient):
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        response_limit: int = MAX_MCP_MESSAGE_BYTES,
    ) -> None:
        super().__init__(response_limit=response_limit)
        self.process = process
        self.stderr_tail = ""
        self.stderr_task = create_diagnostic_task(
            self._stderr(),
            feature="harnesses",
            event_code="harnesses.mcp.stderr_reader",
            failure_message="The MCP stderr supervisor stopped unexpectedly.",
            name="nebula-mcp-stderr",
        )

    async def exchange(
        self, message: dict[str, Any], *, notification: bool = False
    ) -> Any:
        if self.process.stdin is None or self.process.stdout is None:
            raise McpProbeError("MCP stdio transport is unavailable")
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()
        if notification:
            return None
        while True:
            try:
                line = await self.process.stdout.readline()
            except ValueError as exc:
                raise McpProbeError(
                    "MCP response exceeded its bounded response limit"
                ) from exc
            if not line:
                detail = f": {self.stderr_tail}" if self.stderr_tail else ""
                raise McpProbeError(f"MCP stdio server exited during discovery{detail}")
            if len(line) > self.response_limit:
                raise McpProbeError("MCP response exceeded its bounded response limit")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                record_caught_exception(
                    "harnesses",
                    "harnesses.mcp.caught_failure_001",
                    "A handled harnesses operation raised an exception.",
                    exc,
                    stage="mcp",
                )
                raise McpProbeError("MCP stdio server returned malformed JSON") from exc
            # Ignore protocol notifications while waiting for the correlated response.
            if isinstance(response, dict) and response.get("id") == message.get("id"):
                return response

    async def _stderr(self) -> None:
        if self.process.stderr is None:
            return
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail = (
                self.stderr_tail + redact_text(chunk.decode("utf-8", errors="replace"))
            )[-8_000:]

    async def close(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError as caught_error:
                record_caught_exception(
                    "harnesses",
                    "harnesses.mcp.caught_failure_002",
                    "A handled harnesses operation raised an exception.",
                    caught_error,
                    stage="mcp",
                )
                self.process.kill()
                await self.process.wait()
        if not self.stderr_task.done():
            self.stderr_task.cancel()
        await gather_diagnostic(
            self.stderr_task,
            feature="harnesses",
            event_code="harnesses.mcp.stderr_cleanup_failed",
            failure_message="The MCP stderr supervisor did not stop cleanly.",
            stage="cleanup",
        )


class _HttpMcpClient(_McpClient):
    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
        *,
        response_limit: int = MAX_MCP_MESSAGE_BYTES,
    ) -> None:
        super().__init__(response_limit=response_limit)
        self.url = url
        self.headers = headers
        self.session_id: str | None = None
        self.client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        )

    async def exchange(
        self, message: dict[str, Any], *, notification: bool = False
    ) -> Any:
        headers = {
            **self.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        payload = bytearray()
        async with self.client.stream(
            "POST", self.url, headers=headers, json=message
        ) as response:
            if response.status_code in {401, 403}:
                raise McpProbeError("MCP HTTP authentication failed")
            if response.status_code >= 400:
                raise McpProbeError(f"MCP HTTP returned status {response.status_code}")
            discovered_session = response.headers.get("MCP-Session-Id")
            if discovered_session:
                self.session_id = discovered_session
            if notification or response.status_code == 202:
                return None
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > self.response_limit:
                    raise McpProbeError(
                        "MCP response exceeded its bounded response limit"
                    )
                payload.extend(chunk)
        if media_type == "text/event-stream":
            rendered = payload.decode("utf-8", errors="replace")
            for event in rendered.split("\n\n"):
                data = "\n".join(
                    line.removeprefix("data:").lstrip()
                    for line in event.splitlines()
                    if line.startswith("data:")
                )
                if data:
                    try:
                        value = json.loads(data)
                    except json.JSONDecodeError as exc:
                        record_caught_exception(
                            "harnesses",
                            "harnesses.mcp.caught_failure_003",
                            "A handled harnesses operation raised an exception.",
                            exc,
                            stage="mcp",
                        )
                        raise McpProbeError(
                            "MCP HTTP returned malformed SSE JSON"
                        ) from exc
                    if isinstance(value, dict) and value.get("id") == message.get("id"):
                        return value
            raise McpProbeError("MCP HTTP SSE response omitted the correlated result")
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            record_caught_exception(
                "harnesses",
                "harnesses.mcp.caught_failure_004",
                "A handled harnesses operation raised an exception.",
                exc,
                stage="mcp",
            )
            raise McpProbeError("MCP HTTP returned malformed JSON") from exc

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(
                    self.url,
                    headers={
                        **self.headers,
                        "MCP-Session-Id": self.session_id,
                        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                    },
                )
            except httpx.HTTPError as caught_error:
                record_caught_exception(
                    "harnesses",
                    "harnesses.mcp.caught_failure_005",
                    "A handled harnesses operation raised an exception.",
                    caught_error,
                    stage="mcp",
                )
                pass
        await self.client.aclose()


class McpProbeService:
    def __init__(
        self,
        store: NebulaStore,
        *,
        credential_store: CredentialStore,
        workspace_resolver: Callable[[str], Path],
    ) -> None:
        self.store = store
        self.credential_store = credential_store
        self.workspace_resolver = workspace_resolver

    async def probe(
        self, profile_id: str, *, engagement_id: str | None = None
    ) -> McpProbeReport:
        profile = self.store.get(McpServerProfile, profile_id)
        client: _McpClient | None = None
        try:
            client = await self._connect(profile, engagement_id)
            initialize = await asyncio.wait_for(
                client.request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "nebula-core", "version": "3"},
                    },
                ),
                timeout=profile.startup_timeout_seconds,
            )
            if not isinstance(initialize, dict):
                raise McpProbeError("MCP initialize result must be an object")
            server_capabilities = initialize.get("capabilities")
            if not isinstance(server_capabilities, dict):
                server_capabilities = {}
            if "elicitation" in server_capabilities:
                raise McpProbeError(
                    "MCP server requires elicitation/forms, which this release does not support"
                )
            await client.notify("notifications/initialized")
            tools_result = await self._optional_list(client, "tools/list")
            resources_result = await self._optional_list(client, "resources/list")
            templates_result = await self._optional_list(
                client, "resources/templates/list"
            )
            prompts_result = await self._optional_list(client, "prompts/list")
            tools = self._tools(tools_result)
            snapshot = McpCapabilitySnapshot(
                protocol_version=str(
                    initialize.get("protocolVersion") or MCP_PROTOCOL_VERSION
                ),
                tools=tools,
                resources=bool(
                    resources_result.get("resources")
                    or templates_result.get("resourceTemplates")
                ),
                prompts=bool(prompts_result.get("prompts")),
                instructions=(
                    str(initialize.get("instructions"))[:10_000]
                    if initialize.get("instructions")
                    else None
                ),
                checked_at=utc_now(),
                detail=(
                    "Tool/resource/prompt schemas validated; list_changed is supported "
                    "by connected harness transports when the server advertises it."
                ),
            )
            self.store.update(
                McpServerProfile,
                profile.id,
                {"capabilities": snapshot},
                expected_revision=profile.revision,
            )
            return McpProbeReport(
                profile_id=profile.id,
                compatible=True,
                capabilities=snapshot,
            )
        except Exception as exc:
            record_caught_exception(
                "harnesses",
                "harnesses.mcp.caught_failure_006",
                "A handled harnesses operation raised an exception.",
                exc,
                stage="mcp",
            )
            detail = _safe(exc)
            failed = McpCapabilitySnapshot(checked_at=utc_now(), detail=detail)
            latest = self.store.get(McpServerProfile, profile.id)
            self.store.update(
                McpServerProfile,
                latest.id,
                {"capabilities": failed},
                expected_revision=latest.revision,
            )
            return McpProbeReport(
                profile_id=profile.id,
                compatible=False,
                capabilities=failed,
                detail=detail,
            )
        finally:
            if client is not None:
                await client.close()

    async def _connect(
        self,
        profile: McpServerProfile,
        engagement_id: str | None,
        *,
        response_limit: int = MAX_MCP_MESSAGE_BYTES,
    ) -> _McpClient:
        if profile.transport == McpTransport.STDIO:
            if not profile.trusted_stdio:
                raise McpProbeError(
                    "stdio MCP probing executes a trusted local program; trust must be explicit"
                )
            command = Path(profile.command or "")
            if not command.is_absolute() or not command.is_file():
                raise McpProbeError("MCP command must be an existing absolute file")
            if profile.cwd_policy == McpCwdPolicy.WORKSPACE:
                if not engagement_id:
                    raise McpProbeError(
                        "workspace-scoped MCP probing requires engagement_id"
                    )
                cwd = self.workspace_resolver(engagement_id)
            else:
                cwd = Path(profile.cwd or "")
            environment = dict(profile.environment)
            for name, reference in profile.environment_secret_refs.items():
                environment[name] = self._secret(reference)
            process = await asyncio.create_subprocess_exec(
                str(command),
                *profile.arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=_process_environment(environment),
                limit=response_limit + 1,
            )
            return _StdioMcpClient(process, response_limit=response_limit)
        headers: dict[str, str] = {}
        if profile.auth_mode == McpAuthMode.BEARER and profile.bearer_secret_ref:
            headers["Authorization"] = "Bearer " + self._secret(
                profile.bearer_secret_ref
            )
        for name, reference in profile.header_secret_refs.items():
            headers[name] = self._secret(reference)
        return _HttpMcpClient(
            profile.url or "",
            headers,
            profile.startup_timeout_seconds,
            response_limit=response_limit,
        )

    async def call_tool(
        self,
        profile: McpServerProfile,
        *,
        engagement_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke one frozen, selected upstream tool through Core."""

        if not profile.enabled:
            raise McpProbeError("selected MCP server is disabled")
        snapshot = next(
            (item for item in profile.capabilities.tools if item.name == tool_name),
            None,
        )
        if snapshot is None:
            raise McpProbeError("selected MCP tool is absent from the frozen snapshot")
        if profile.enabled_tools and tool_name not in profile.enabled_tools:
            raise McpProbeError("selected MCP tool is outside the server allow list")
        if tool_name in profile.disabled_tools:
            raise McpProbeError("selected MCP tool is disabled")
        errors = list(
            Draft202012Validator(snapshot.input_schema).iter_errors(arguments)
        )
        if errors:
            raise McpProbeError(f"MCP tool arguments are invalid: {errors[0].message}")
        client = await self._connect(
            profile,
            engagement_id,
            response_limit=MAX_MCP_TOOL_RESPONSE_BYTES,
        )
        try:
            initialize = await asyncio.wait_for(
                client.request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "nebula-core-gateway", "version": "3"},
                    },
                ),
                timeout=profile.startup_timeout_seconds,
            )
            if not isinstance(initialize, dict):
                raise McpProbeError("MCP initialize result must be an object")
            await client.notify("notifications/initialized")
            result = await asyncio.wait_for(
                client.request(
                    "tools/call", {"name": tool_name, "arguments": arguments}
                ),
                timeout=profile.tool_timeout_seconds,
            )
            if not isinstance(result, dict):
                raise McpProbeError("MCP tools/call result must be an object")
            return result
        finally:
            await client.close()

    def _secret(self, reference: str) -> str:
        try:
            return self.credential_store.resolve(reference).get_secret_value()
        except (CredentialError, ValueError) as exc:
            record_caught_exception(
                "harnesses",
                "harnesses.mcp.caught_failure_007",
                "A handled harnesses operation raised an exception.",
                exc,
                stage="mcp",
            )
            raise McpProbeError(str(exc)) from exc

    @staticmethod
    async def _optional_list(client: _McpClient, method: str) -> dict[str, Any]:
        try:
            result = await client.request(method, {})
        except McpProbeError as exc:
            record_caught_exception(
                "harnesses",
                "harnesses.mcp.caught_failure_008",
                "A handled harnesses operation raised an exception.",
                exc,
                stage="mcp",
            )
            if "-32601" in str(exc) or "not found" in str(exc).lower():
                return {}
            raise
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _tools(result: dict[str, Any]) -> list[McpToolSnapshot]:
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            return []
        tools: list[McpToolSnapshot] = []
        names: set[str] = set()
        for raw in raw_tools:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise McpProbeError("MCP tools/list returned an invalid tool")
            name = raw["name"]
            if name in names:
                raise McpProbeError(f"MCP tools/list returned duplicate tool {name!r}")
            names.add(name)
            schema = raw.get("inputSchema") or {"type": "object"}
            if not isinstance(schema, dict):
                raise McpProbeError(f"MCP tool {name!r} inputSchema must be an object")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                record_caught_exception(
                    "harnesses",
                    "harnesses.mcp.caught_failure_009",
                    "A handled harnesses operation raised an exception.",
                    exc,
                    stage="mcp",
                )
                raise McpProbeError(f"MCP tool {name!r} has an invalid schema") from exc
            annotations = raw.get("annotations")
            annotations = annotations if isinstance(annotations, dict) else {}
            meta = raw.get("_meta")
            meta = meta if isinstance(meta, dict) else {}
            credential_hint = meta.get("nebula/credentialed")
            annotations_complete = all(
                key in annotations
                for key in (
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                )
            ) and isinstance(credential_hint, bool)
            tools.append(
                McpToolSnapshot(
                    name=name,
                    description=str(raw.get("description") or "")[:10_000],
                    input_schema=schema,
                    read_only=annotations.get("readOnlyHint") is True,
                    destructive=annotations.get("destructiveHint") is not False,
                    idempotent=annotations.get("idempotentHint") is True,
                    open_world=annotations.get("openWorldHint") is not False,
                    credentialed=(
                        credential_hint if isinstance(credential_hint, bool) else None
                    ),
                    annotations_complete=annotations_complete,
                )
            )
        return tools


def _process_environment(extra: dict[str, str]) -> dict[str, str]:
    import os

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
        }
    }
    keep.update(extra)
    return keep


def _safe(value: Any) -> str:
    if isinstance(value, BaseException):
        value = str(value) or type(value).__name__
    text = redact_text(str(value)).strip()
    return (text or "MCP probe failed")[:1_000]


def mcp_tool_runtime_name(profile_id: str, tool_name: str) -> str:
    digest = (
        __import__("hashlib")
        .sha256(f"{profile_id}\0{tool_name}".encode("utf-8"))
        .hexdigest()[:12]
    )
    stem = __import__("re").sub(r"[^a-z0-9_.-]+", "-", tool_name.casefold())
    stem = stem.strip(".-") or "tool"
    return f"mcp.{digest}.{stem[:80]}"


def _mcp_tool_risk(tool: McpToolSnapshot) -> RiskClass:
    if tool.credentialed:
        return RiskClass.CREDENTIAL_USE
    if tool.destructive:
        return RiskClass.DESTRUCTIVE
    if tool.read_only:
        return RiskClass.LOCAL_READ
    return RiskClass.WORKSPACE_WRITE


def _mcp_requires_approval(profile: McpServerProfile, tool: McpToolSnapshot) -> bool:
    mode = profile.tool_overrides.get(tool.name, profile.default_approval)
    if mode == McpApprovalMode.DENY:
        return True
    if mode == McpApprovalMode.ALLOW:
        return tool.destructive and tool.name not in profile.tool_overrides
    if mode == McpApprovalMode.ASK:
        return True
    return not (
        profile.capabilities.checked_at is not None
        and tool.annotations_complete
        and tool.read_only
        and not tool.destructive
        and not tool.open_world
        and tool.credentialed is False
    )


def build_mcp_tool_plugins(
    service: McpProbeService,
    profiles: tuple[McpServerProfile, ...],
) -> list[Any]:
    """Freeze selected upstream MCP tools into ordinary broker plugins."""

    # Local import keeps MCP transport discovery independent from the broker.
    from .tools import ToolExecutionResult, ToolPlugin, ToolSpec

    class McpToolPlugin(ToolPlugin):
        def __init__(
            self, profile: McpServerProfile, snapshot: McpToolSnapshot
        ) -> None:
            self.profile = profile
            self.snapshot = snapshot
            schema = dict(snapshot.input_schema)
            schema.setdefault("type", "object")
            self.spec = ToolSpec(
                name=mcp_tool_runtime_name(profile.id, snapshot.name),
                description=(
                    f"{snapshot.description}\nResults are captured as artifacts and returned "
                    "as a nebula.tool-result/v2 receipt."
                )[:10_000],
                input_schema=schema,
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=_mcp_tool_risk(snapshot),
                requires_approval=_mcp_requires_approval(profile, snapshot),
                pack_id=f"mcp:{profile.id}",
            )

        async def execute(self, invocation: Any, runner: Any) -> Any:
            del runner
            started = utc_now()
            failure: Exception | None = None
            upstream: dict[str, Any] | None = None
            try:
                upstream = await service.call_tool(
                    self.profile,
                    engagement_id=invocation.engagement_id,
                    tool_name=self.snapshot.name,
                    arguments=invocation.arguments,
                )
            except Exception as exc:  # diagnostic-expected: converted to a bounded tool failure receipt
                failure = exc
            completed = utc_now()
            blocks: list[dict[str, Any]] = []
            is_error = failure is not None
            if upstream is not None:
                content = upstream.get("content")
                if isinstance(content, list):
                    blocks.extend(item for item in content if isinstance(item, dict))
                if "structuredContent" in upstream:
                    blocks.append(
                        {
                            "type": "structured_content",
                            "value": upstream.get("structuredContent"),
                        }
                    )
                is_error = upstream.get("isError") is True
            return ToolExecutionResult(
                output={},
                stderr=_safe(failure) if failure is not None else "",
                exit_code=1 if is_error else 0,
                mcp_content_blocks=blocks,
                execution={
                    "runtime": "mcp",
                    "mcp_server_id": self.profile.id,
                    "mcp_tool_name": self.snapshot.name,
                    "started_at": started.isoformat(),
                    "completed_at": completed.isoformat(),
                    "duration_seconds": max(0.0, (completed - started).total_seconds()),
                    "timed_out": isinstance(failure, asyncio.TimeoutError),
                },
            )

    plugins: list[Any] = []
    for profile in profiles:
        if not profile.enabled:
            raise McpProbeError(f"selected MCP server {profile.id!r} is disabled")
        for tool in profile.capabilities.tools:
            if profile.enabled_tools and tool.name not in profile.enabled_tools:
                continue
            if tool.name in profile.disabled_tools:
                continue
            if profile.tool_overrides.get(tool.name) == McpApprovalMode.DENY:
                continue
            plugins.append(McpToolPlugin(profile, tool))
    return plugins


def resolve_mcp_profiles(
    store: NebulaStore, server_ids: list[str] | tuple[str, ...]
) -> tuple[McpServerProfile, ...]:
    """Resolve a validated, ordered MCP selection for a durable runtime snapshot."""

    ids = tuple(dict.fromkeys(server_ids))
    profiles = tuple(store.get(McpServerProfile, item) for item in ids)
    for profile in profiles:
        if not profile.enabled:
            raise McpProbeError(f"selected MCP server {profile.id!r} is disabled")
        if profile.capabilities.checked_at is None:
            raise McpProbeError(
                f"selected MCP server {profile.id!r} must be probed before use"
            )
        selected = [
            tool
            for tool in profile.capabilities.tools
            if (not profile.enabled_tools or tool.name in profile.enabled_tools)
            and tool.name not in profile.disabled_tools
            and profile.tool_overrides.get(tool.name) != McpApprovalMode.DENY
        ]
        if not selected:
            raise McpProbeError(
                f"selected MCP server {profile.id!r} exposes no enabled tools"
            )
    return profiles


@dataclass(frozen=True)
class McpGatewayLaunch:
    socket_path: Path
    token: str
    command: str
    arguments: tuple[str, ...]

    def runtime_config(self) -> dict[str, dict[str, Any]]:
        return {
            "nebula": {
                "id": "nebula",
                "name": "nebula",
                "transport": McpTransport.STDIO.value,
                "required": True,
                "startup_timeout_seconds": GATEWAY_STARTUP_TIMEOUT_SECONDS,
                "tool_timeout_seconds": 900.0,
                "enabled_tools": [],
                "disabled_tools": [],
                "command": self.command,
                "args": list(self.arguments),
                "cwd": str(self.socket_path.parent),
                "env": {"NEBULA_MCP_GATEWAY_TOKEN": self.token},
            }
        }


GatewayListHandler = Callable[[dict[str, Any]], Any]
GatewayCallHandler = Callable[[str, dict[str, Any]], Any]


class McpGatewaySession:
    """Session-scoped authenticated Unix IPC endpoint for the STDIO shim."""

    def __init__(
        self,
        *,
        list_tools: GatewayListHandler,
        call_tool: GatewayCallHandler,
    ) -> None:
        self.list_tools = list_tools
        self.call_tool = call_tool
        self.root = Path(tempfile.mkdtemp(prefix="nebula-mcp-gateway-"))
        self.root.chmod(0o700)
        self.socket_path = self.root / "core.sock"
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.AbstractServer | None = None
        self._authenticated_writer: asyncio.StreamWriter | None = None
        self._token_consumed = False

    async def start(self) -> McpGatewayLaunch:
        if self.server is not None:
            raise RuntimeError("MCP gateway session is already started")
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=MAX_MCP_MESSAGE_BYTES + 1,
        )
        self.socket_path.chmod(0o600)
        arguments = (
            ("mcp-gateway", "--socket", str(self.socket_path))
            if getattr(sys, "frozen", False)
            else (
                "-m",
                "nebula.v3.mcp_gateway",
                "--socket",
                str(self.socket_path),
            )
        )
        return McpGatewayLaunch(
            socket_path=self.socket_path,
            token=self.token,
            command=sys.executable,
            arguments=arguments,
        )

    async def close(self) -> None:
        if self._authenticated_writer is not None:
            self._authenticated_writer.close()
            await self._authenticated_writer.wait_closed()
            self._authenticated_writer = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.socket_path.unlink(missing_ok=True)
        if self.root.exists():
            shutil.rmtree(self.root)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            auth_line = await reader.readline()
            if not auth_line or len(auth_line) > MAX_MCP_MESSAGE_BYTES:
                return
            try:
                authentication = json.loads(auth_line)
                supplied_token = authentication.get("token")
                authenticated = (
                    isinstance(supplied_token, str)
                    and not self._token_consumed
                    and self._authenticated_writer is None
                    and secrets.compare_digest(supplied_token, self.token)
                    and authentication.get("method") == "authenticate"
                )
            except (
                AttributeError,
                json.JSONDecodeError,
                TypeError,
            ):  # diagnostic-expected: authentication fails closed
                authenticated = False
            if not authenticated:
                writer.write(
                    b'{"id":0,"error":{"message":"gateway authentication failed"}}\n'
                )
                await writer.drain()
                return
            self._token_consumed = True
            self._authenticated_writer = writer
            writer.write(b'{"id":0,"result":{"authenticated":true}}\n')
            await writer.drain()
            while line := await reader.readline():
                if len(line) > MAX_MCP_MESSAGE_BYTES:
                    break
                response: dict[str, Any]
                request_id: Any = None
                try:
                    message = json.loads(line)
                    request_id = message.get("id")
                    method = message.get("method")
                    if method == "tools/list":
                        params = message.get("params")
                        result = self.list_tools(
                            params if isinstance(params, dict) else {}
                        )
                    elif method == "tools/call":
                        params = message.get("params")
                        if not isinstance(params, dict):
                            raise ValueError("gateway call params must be an object")
                        name = params.get("name")
                        arguments = params.get("arguments", {})
                        if not isinstance(name, str) or not isinstance(arguments, dict):
                            raise ValueError("gateway call is malformed")
                        result = self.call_tool(name, arguments)
                    else:
                        raise ValueError("unsupported gateway method")
                    if asyncio.iscoroutine(result):
                        result = await result
                    response = {"id": request_id, "result": result}
                except (
                    Exception
                ) as exc:  # diagnostic-expected: serialized as a bounded gateway error
                    response = {
                        "id": request_id,
                        "error": {"message": _safe(exc), "type": type(exc).__name__},
                    }
                encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
                if len(encoded) > MAX_MCP_MESSAGE_BYTES:
                    encoded = (
                        json.dumps(
                            {
                                "id": request_id,
                                "error": {"message": "gateway response exceeded 4 MiB"},
                            },
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )
                writer.write(encoded)
                await writer.drain()
        finally:
            if self._authenticated_writer is writer:
                self._authenticated_writer = None
            writer.close()
            await writer.wait_closed()


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpGatewayLaunch",
    "McpGatewaySession",
    "McpProbeError",
    "McpProbeReport",
    "McpProbeService",
    "build_mcp_tool_plugins",
    "resolve_mcp_profiles",
]
