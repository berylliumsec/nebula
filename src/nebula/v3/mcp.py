"""Explicit MCP discovery without inheriting ambient user configuration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field

from .credentials import CredentialError, CredentialStore
from .domain import (
    McpAuthMode,
    McpCapabilitySnapshot,
    McpCwdPolicy,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    NebulaModel,
    utc_now,
)
from .redaction import redact_text
from .storage import NebulaStore

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024


class McpProbeError(RuntimeError):
    """An operator-safe discovery failure."""


class McpProbeReport(NebulaModel):
    profile_id: str
    compatible: bool
    capabilities: McpCapabilitySnapshot
    detail: str | None = Field(default=None, max_length=1_000)


class _McpClient:
    def __init__(self) -> None:
        self.next_id = 1

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
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        super().__init__()
        self.process = process
        self.stderr_tail = ""
        self.stderr_task = asyncio.create_task(self._stderr())

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
            line = await self.process.stdout.readline()
            if not line:
                detail = f": {self.stderr_tail}" if self.stderr_tail else ""
                raise McpProbeError(f"MCP stdio server exited during discovery{detail}")
            if len(line) > MAX_MCP_MESSAGE_BYTES:
                raise McpProbeError("MCP response exceeded the 4 MiB discovery limit")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise McpProbeError("MCP stdio server returned malformed JSON") from exc
            # Ignore protocol notifications while waiting for the correlated response.
            if isinstance(response, dict) and response.get("id") == message.get("id"):
                return response

    async def _stderr(self) -> None:
        if self.process.stderr is None:
            return
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail = (
                self.stderr_tail
                + redact_text(chunk.decode("utf-8", errors="replace"))
            )[-8_000:]

    async def close(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if not self.stderr_task.done():
            self.stderr_task.cancel()
        await asyncio.gather(self.stderr_task, return_exceptions=True)


class _HttpMcpClient(_McpClient):
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        super().__init__()
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
        response = await self.client.post(self.url, headers=headers, json=message)
        if response.status_code in {401, 403}:
            raise McpProbeError("MCP HTTP authentication failed")
        if response.status_code >= 400:
            raise McpProbeError(f"MCP HTTP returned status {response.status_code}")
        discovered_session = response.headers.get("MCP-Session-Id")
        if discovered_session:
            self.session_id = discovered_session
        if notification or response.status_code == 202:
            return None
        if len(response.content) > MAX_MCP_MESSAGE_BYTES:
            raise McpProbeError("MCP response exceeded the 4 MiB discovery limit")
        media_type = response.headers.get("content-type", "").split(";", 1)[0]
        if media_type == "text/event-stream":
            for event in response.text.split("\n\n"):
                data = "\n".join(
                    line.removeprefix("data:").lstrip()
                    for line in event.splitlines()
                    if line.startswith("data:")
                )
                if data:
                    try:
                        value = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise McpProbeError("MCP HTTP returned malformed SSE JSON") from exc
                    if isinstance(value, dict) and value.get("id") == message.get("id"):
                        return value
            raise McpProbeError("MCP HTTP SSE response omitted the correlated result")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
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
            except httpx.HTTPError:
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
        self, profile: McpServerProfile, engagement_id: str | None
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
            )
            return _StdioMcpClient(process)
        headers: dict[str, str] = {}
        if profile.auth_mode == McpAuthMode.BEARER and profile.bearer_secret_ref:
            headers["Authorization"] = "Bearer " + self._secret(
                profile.bearer_secret_ref
            )
        for name, reference in profile.header_secret_refs.items():
            headers[name] = self._secret(reference)
        return _HttpMcpClient(
            profile.url or "", headers, profile.startup_timeout_seconds
        )

    def _secret(self, reference: str) -> str:
        try:
            return self.credential_store.resolve(reference).get_secret_value()
        except (CredentialError, ValueError) as exc:
            raise McpProbeError(str(exc)) from exc

    @staticmethod
    async def _optional_list(client: _McpClient, method: str) -> dict[str, Any]:
        try:
            result = await client.request(method, {})
        except McpProbeError as exc:
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


__all__ = ["MCP_PROTOCOL_VERSION", "McpProbeError", "McpProbeReport", "McpProbeService"]
