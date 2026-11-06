"""Nonsecret MCP discovery and browser-owned credential attachment."""

from __future__ import annotations

from .async_utils import call_async

from .async_utils import async_take

from itertools import islice
from typing import Any

from .domain import McpServerProfile, McpTransport, RiskClass
from .mcp_scope import mcp_metadata_is_visible, project_mcp_profiles
from .storage import NebulaStore
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_SERVER = {
    "type": "object",
    "properties": {"server_id": _ID},
    "required": ["server_id"],
    "additionalProperties": False,
}
_ATTACH = {
    "type": "object",
    "properties": {
        "server_id": _ID,
        "expected_revision": {"type": "integer", "minimum": 1},
        "credential_ref": {
            "type": "string",
            "pattern": r"^(?:vault|session):[0-9a-f]{32}$",
        },
        "slot": {"type": "string", "enum": ["bearer", "header", "environment"]},
        "key": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["server_id", "expected_revision", "credential_ref", "slot"],
    "additionalProperties": False,
}


class ButlerMcp:
    """Prepare an exact MCP reference change without exposing secret values."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(profile: McpServerProfile) -> dict[str, Any]:
        return {
            "server_id": profile.id,
            "name": profile.name,
            "transport": profile.transport.value,
            "auth_mode": profile.auth_mode.value,
            "revision": profile.revision,
            "enabled": profile.enabled,
            "has_bearer_credential": bool(profile.bearer_secret_ref),
            "header_names": sorted(profile.header_secret_refs),
            "environment_names": sorted(profile.environment_secret_refs),
        }

    async def servers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        visible = await async_take(
            project_mcp_profiles(
                self.store, self.project_id, offset=arguments.get("offset", 0)
            ),
            51,
        )
        return {
            "servers": [self._summary(profile) for _, profile in visible[:50]],
            "next_offset": visible[50][0] if len(visible) > 50 else None,
        }

    def _visible(self, profile: McpServerProfile) -> bool:
        return mcp_metadata_is_visible(profile.metadata, self.project_id)

    async def _profile(self, server_id: str) -> McpServerProfile:
        profile = await self.store.get(McpServerProfile, server_id)
        if not self._visible(profile):
            raise ValueError("MCP server is not in the selected project")
        return profile

    async def server(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._summary((await self._profile(arguments["server_id"])))

    async def attach(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = await self._profile(arguments["server_id"])
        if profile.revision != arguments["expected_revision"]:
            raise ValueError("MCP server changed; read its current revision")
        if profile.transport == McpTransport.BUILTIN:
            raise ValueError("built-in MCP servers cannot accept credentials")
        slot = arguments["slot"]
        key = arguments.get("key")
        if (slot == "environment") != (profile.transport == McpTransport.STDIO):
            raise ValueError("MCP credential slot does not match server transport")
        if slot == "bearer" and key is not None:
            raise ValueError("bearer MCP credentials do not have a key name")
        if slot != "bearer":
            if not key:
                raise ValueError("header and environment credentials need a key name")
            import re

            pattern = (
                r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
                if slot == "header"
                else r"[A-Za-z_][A-Za-z0-9_]*"
            )
            if not re.fullmatch(pattern, key):
                raise ValueError("invalid MCP credential key name")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "mcp_attach_credential",
                "project_id": self.project_id,
                "server_id": profile.id,
                "name": profile.name,
                "transport": profile.transport.value,
                "expected_revision": profile.revision,
                "credential_ref": arguments["credential_ref"],
                "slot": slot,
                "key": key,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.mcp.list",
                "Page MCP server identities, transports, current revisions and occupied credential key names without reference or secret values.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.servers,
            ),
            (
                "nebula.mcp.server",
                "Read one MCP server's nonsecret identity, transport, revision and occupied credential key names.",
                _SERVER,
                RiskClass.LOCAL_READ,
                self.server,
            ),
            (
                "nebula.mcp.attach_credential",
                "Ask the requesting browser to confirm an available managed credential reference for one exact MCP server bearer, HTTP header or stdio environment slot. The browser preserves other keys, patches at the current revision and reads Core back. Never request the secret value.",
                _ATTACH,
                RiskClass.WORKSPACE_WRITE,
                self.attach,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any],
                *,
                selected_handler: Any = handler,
                selected_read: bool = risk == RiskClass.LOCAL_READ,
            ) -> dict[str, Any]:
                if selected_read:
                    return await read_in_worker(selected_handler, arguments)
                return await call_async(selected_handler, arguments)

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=risk,
                        idempotency=(
                            IdempotencyBehavior.SAFE
                            if risk == RiskClass.LOCAL_READ
                            else IdempotencyBehavior.NON_IDEMPOTENT
                        ),
                    ),
                    run,
                )
            )
        return result
