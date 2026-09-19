"""Import and export MCP server profiles as ``mcpServers`` JSON.

Operators already keep MCP configuration in the format used by Claude Desktop,
Cursor, and VS Code. This module turns such a file into ``McpServerProfile``
records without weakening the profile's security contract:

* a file can never enable or trust a server by itself; new servers start
  disabled unless the operator enables them for this import, and a local
  (stdio) program is only trusted when the operator says so for this import;
* importing a server that already exists updates only what differs: the file
  owns the connection (command, args, env, cwd, url, headers), Nebula-side
  settings the file does not mention are kept, and a trusted program whose
  launch settings changed becomes untrusted again unless the operator
  re-confirms trust;
* ``${VAR}`` and ``${env:VAR}`` values become ``env:VAR`` references, and
  literal credentials are moved into the credential store (or rejected), so no
  secret is persisted in a profile;
* bare commands such as ``npx`` are resolved to an absolute path on the Core
  host, which is where stdio servers run.

Nothing is read from ambient configuration: the caller always supplies the
document.
"""

from __future__ import annotations

import hmac
import re
import shlex
import shutil
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError

from .diagnostics import record_caught_exception
from .credentials import CredentialCreateRequest, CredentialError, CredentialStore
from .domain import (
    McpApprovalMode,
    McpAuthMode,
    McpCapabilitySnapshot,
    McpCwdPolicy,
    McpServerProfile,
    McpTransport,
    NebulaModel,
    utc_now,
)
from .storage import NebulaStore

MAX_IMPORTED_SERVERS = 200

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")
_BEARER = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)
_SECRET_NAME_TOKENS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "auth",
    "cookie",
    "session",
)
_TRANSPORT_ALIASES = {
    "stdio": McpTransport.STDIO,
    "http": McpTransport.STREAMABLE_HTTP,
    "streamable-http": McpTransport.STREAMABLE_HTTP,
    "streamable_http": McpTransport.STREAMABLE_HTTP,
    "streamablehttp": McpTransport.STREAMABLE_HTTP,
}
# Fields other clients use that have no safe Nebula equivalent.
_IGNORED_FIELDS = {
    "disabled": "whether a server is enabled is chosen in Nebula when importing",
    "alwaysAllow": "tool approvals were not imported; review them in Nebula",
    "autoApprove": "tool approvals were not imported; review them in Nebula",
    "timeout": "use nebula.tool_timeout_seconds for tool timeouts",
    "envFile": "env files are not read; reference variables with ${NAME}",
    "description": None,
}
_PLACEHOLDER_REF = "vault:" + "0" * 32


class McpImportError(ValueError):
    """The document cannot be interpreted as an MCP server configuration."""


class McpImportOptions(NebulaModel):
    """Nebula-specific settings that may accompany one imported server."""

    default_approval: McpApprovalMode | None = Field(
        default=None,
        description="Approval for this server's tools; risk_based when omitted.",
    )
    tool_overrides: dict[str, McpApprovalMode] = Field(
        default_factory=dict,
        description="Approval for individual tools, keyed by tool name.",
    )
    enabled_tools: list[str] = Field(
        default_factory=list,
        max_length=2_000,
        description="Offer only these tools. Empty offers every tool.",
    )
    disabled_tools: list[str] = Field(
        default_factory=list,
        max_length=2_000,
        description="Never offer these tools.",
    )
    required: bool | None = Field(
        default=None,
        description="Fail new sessions when this server cannot start.",
    )
    startup_timeout_seconds: float | None = Field(
        default=None, gt=0, le=120, description="Startup timeout; 10 when omitted."
    )
    tool_timeout_seconds: float | None = Field(
        default=None, gt=0, le=900, description="Per-call timeout; 60 when omitted."
    )


class McpImportDefaults(NebulaModel):
    """Choices for the servers an import creates; a server's nebula block wins."""

    enabled: bool = Field(
        default=False,
        description="Enable new servers. Local programs also need trust.",
    )
    default_approval: McpApprovalMode = Field(
        default=McpApprovalMode.RISK_BASED,
        description="Approval for new servers whose file sets no default_approval.",
    )


class McpImportRequest(NebulaModel):
    config: dict[str, Any]
    dry_run: bool = True
    on_conflict: Literal["update", "skip", "replace"] = "update"
    literal_secrets: Literal["vault", "session", "reject"] = "vault"
    source_name: str | None = Field(default=None, max_length=200)
    defaults: McpImportDefaults = Field(default_factory=McpImportDefaults)
    trust_local_programs: bool = Field(
        default=False,
        description=(
            "Trust the local programs this import would otherwise leave disabled: "
            "new ones being enabled, and enabled ones whose launch settings changed."
        ),
    )


class McpImportSecret(NebulaModel):
    """Where one credential-bearing value will come from, never the value."""

    target: str = Field(max_length=300)
    source: Literal["environment", "vault", "session"]
    reference: str | None = Field(default=None, max_length=200)


class McpImportChange(NebulaModel):
    """One setting that differs from the saved server; secrets are described."""

    field: str = Field(max_length=300)
    before: str | None = None
    after: str | None = None


class McpImportEntry(NebulaModel):
    source_name: str = Field(max_length=300)
    name: str | None = Field(default=None, max_length=200)
    action: Literal["create", "update", "unchanged", "replace", "skip", "invalid"]
    transport: McpTransport | None = None
    command: str | None = None
    arguments: list[str] = Field(default_factory=list)
    url: str | None = None
    profile_id: str | None = None
    secrets: list[McpImportSecret] = Field(default_factory=list)
    changes: list[McpImportChange] = Field(default_factory=list)
    enabled: bool = Field(default=False, description="Enabled once saved.")
    default_approval: McpApprovalMode | None = None
    needs_trust: bool = Field(
        default=False,
        description=(
            "A local program that is enabled only if this import trusts it "
            "(trust_local_programs)."
        ),
    )
    needs_probe: bool = Field(
        default=False,
        description="Enabled once saved but without probed tools yet.",
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=2_000)


class McpImportReport(NebulaModel):
    dry_run: bool
    entries: list[McpImportEntry]
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    replaced: int = 0
    skipped: int = 0
    invalid: int = 0


class McpExportReport(NebulaModel):
    config: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _Draft:
    """A validated profile plus the literal secrets it still needs stored."""

    entry: McpImportEntry
    fields: dict[str, Any]
    literals: dict[str, str] = field(default_factory=dict, repr=False)
    options: dict[str, Any] = field(default_factory=dict)
    existing_id: str | None = None
    existing_revision: int | None = None


def import_mcp_config(
    request: McpImportRequest,
    *,
    store: NebulaStore,
    credential_store: CredentialStore,
    which: Callable[[str], str | None] | None = None,
) -> McpImportReport:
    """Preview or apply an ``mcpServers`` document."""

    which = which or shutil.which
    servers = _servers(request.config)
    existing: dict[str, list[McpServerProfile]] = {}
    for profile in _all_profiles(store):
        existing.setdefault(profile.name, []).append(profile)

    drafts: list[_Draft] = []
    entries: list[McpImportEntry] = []
    claimed: set[str] = set()
    for source_name, raw in servers.items():
        entry = McpImportEntry(source_name=source_name[:300], action="invalid")
        try:
            draft = _draft(source_name, raw, entry, request, which)
            if draft.entry.name in claimed:
                raise McpImportError(
                    f"another server in this file is also named {draft.entry.name!r}"
                )
            claimed.add(draft.entry.name or "")
            matches = existing.get(draft.entry.name or "", [])
            if len(matches) > 1:
                raise McpImportError(
                    f"{len(matches)} existing MCP servers are named "
                    f"{draft.entry.name!r}; rename them before importing"
                )
            current = matches[0] if matches else None
            if current and request.on_conflict == "skip":
                entry.action = "skip"
                entry.profile_id = current.id
                entry.warnings.append(
                    "an MCP server with this name already exists; choose update "
                    "or replace to change it"
                )
            elif current and request.on_conflict == "update":
                entry.profile_id = current.id
                _plan_update(draft, current, request, credential_store)
                if entry.action == "update":
                    drafts.append(draft)
            else:
                entry.action = "replace" if current else "create"
                draft.existing_id = current.id if current else None
                entry.profile_id = draft.existing_id
                _plan_new(draft, request)
                drafts.append(draft)
        except (McpImportError, ValidationError) as exc:
            # diagnostic-expected: an invalid entry is reported in the preview
            entry.action = "invalid"
            entry.error = _message(exc)
        entries.append(entry)

    if any(draft.literals for draft in drafts):
        vault_state = credential_store.vault_state
        if request.literal_secrets == "vault" and vault_state != "available":
            reason = (
                "vault is locked" if vault_state == "locked" else "vault is unavailable"
            )
            for draft in drafts:
                if draft.literals:
                    draft.entry.action = "invalid"
                    draft.entry.error = (
                        "this server has literal credentials and the host credential "
                        f"{reason}; replace them with ${{NAME}} environment "
                        "references or store them for this session only"
                    )
            drafts = [draft for draft in drafts if draft.entry.action != "invalid"]

    if not request.dry_run:
        for draft in drafts:
            _apply(draft, request, store, credential_store)

    return McpImportReport(
        dry_run=request.dry_run,
        entries=entries,
        created=sum(entry.action == "create" for entry in entries),
        updated=sum(entry.action == "update" for entry in entries),
        unchanged=sum(entry.action == "unchanged" for entry in entries),
        replaced=sum(entry.action == "replace" for entry in entries),
        skipped=sum(entry.action == "skip" for entry in entries),
        invalid=sum(entry.action == "invalid" for entry in entries),
    )


def export_mcp_config(profiles: list[McpServerProfile]) -> McpExportReport:
    """Render profiles as a portable ``mcpServers`` document without secrets.

    Environment references keep their variable name. Vault and session
    references cannot leave this host, so they become ``${NAME}`` placeholders
    that the receiving host must provide in its environment.
    """

    servers: dict[str, Any] = {}
    warnings: list[str] = []

    def placeholder(server: str, reference: str, fallback: str) -> str:
        if reference.startswith("env:"):
            return "${" + reference.removeprefix("env:") + "}"
        name = re.sub(r"[^A-Za-z0-9_]", "_", fallback).upper().strip("_") or "SECRET"
        if not _IDENTIFIER.fullmatch(name):
            name = f"MCP_{name}"
        warnings.append(
            f"{server}: a stored credential was exported as ${{{name}}}; "
            "set that environment variable where the file is imported"
        )
        return "${" + name + "}"

    for profile in profiles:
        entry: dict[str, Any] = {}
        if profile.transport == McpTransport.STDIO:
            entry["type"] = "stdio"
            entry["command"] = profile.command
            if profile.arguments:
                entry["args"] = list(profile.arguments)
            env: dict[str, str] = dict(profile.environment)
            for name, reference in profile.environment_secret_refs.items():
                env[name] = placeholder(profile.name, reference, name)
            if env:
                entry["env"] = env
            if profile.cwd_policy == McpCwdPolicy.FIXED and profile.cwd:
                entry["cwd"] = profile.cwd
        else:
            entry["type"] = "http"
            entry["url"] = profile.url
            headers: dict[str, str] = {}
            if profile.auth_mode == McpAuthMode.BEARER and profile.bearer_secret_ref:
                headers["Authorization"] = "Bearer " + placeholder(
                    profile.name, profile.bearer_secret_ref, f"{profile.name}_token"
                )
            for name, reference in profile.header_secret_refs.items():
                headers[name] = placeholder(
                    profile.name, reference, f"{profile.name}_{name}"
                )
            if headers:
                entry["headers"] = headers
        options: dict[str, Any] = {}
        if profile.default_approval != McpApprovalMode.RISK_BASED:
            options["default_approval"] = profile.default_approval.value
        if profile.tool_overrides:
            options["tool_overrides"] = {
                name: mode.value for name, mode in profile.tool_overrides.items()
            }
        if profile.enabled_tools:
            options["enabled_tools"] = list(profile.enabled_tools)
        if profile.disabled_tools:
            options["disabled_tools"] = list(profile.disabled_tools)
        if profile.required:
            options["required"] = True
        defaults = McpServerProfile.model_fields
        for key in ("startup_timeout_seconds", "tool_timeout_seconds"):
            if getattr(profile, key) != defaults[key].default:
                options[key] = getattr(profile, key)
        if options:
            entry["nebula"] = options
        servers[profile.name] = entry
    return McpExportReport(config={"mcpServers": servers}, warnings=warnings)


def mcp_config_json_schema() -> dict[str, Any]:
    """JSON Schema for the files ``import_mcp_config`` accepts.

    The ``nebula`` block comes from ``McpImportOptions`` so editor validation
    cannot drift from the importer. The rest mirrors ``_draft``: fields other
    clients use are allowed, because the importer skips them with a warning.
    """

    options = McpImportOptions.model_json_schema()
    definitions = options.pop("$defs", {})
    string_value = {"type": ["string", "number", "boolean"]}
    reference = "Use ${NAME} or ${env:NAME} for a variable of the Nebula Core process."
    server = {
        "type": "object",
        "description": "One MCP server: a local program (command) or an HTTP url.",
        "properties": {
            "type": {
                "enum": [
                    "stdio",
                    "http",
                    "streamable-http",
                    "streamable_http",
                    "streamableHttp",
                ],
                "description": "Inferred from command or url when omitted.",
            },
            "command": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Program on the Nebula host: a name on PATH such as npx, "
                    "or an absolute path."
                ),
                "pattern": "^(/.*|[^/~][^/]*)$",
            },
            "args": {
                "type": "array",
                "items": {"type": ["string", "number"]},
                "description": "Passed literally; ${...} is not expanded here.",
            },
            "env": {
                "type": "object",
                "propertyNames": {"pattern": _IDENTIFIER.pattern},
                "additionalProperties": string_value,
                "description": f"Environment for the program. {reference}",
            },
            "cwd": {
                "type": "string",
                "pattern": "^/",
                "description": "Absolute directory; omit to use the Project workspace.",
            },
            "url": {
                "type": "string",
                "pattern": "^https?://",
                "description": ("Streamable HTTP endpoint; https unless on localhost."),
            },
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Request headers, all treated as secrets. "
                    f"Authorization: Bearer ... becomes bearer auth. {reference}"
                ),
            },
            "nebula": {"$ref": "#/$defs/NebulaOptions"},
            **{
                name: {"description": f"Not imported: {note or 'ignored'}."}
                for name, note in _IGNORED_FIELDS.items()
            },
        },
        "anyOf": [
            {
                "title": "Local (stdio) server",
                "required": ["command"],
                "properties": {"type": {"const": "stdio"}},
                "not": {"anyOf": [{"required": ["url"]}, {"required": ["headers"]}]},
            },
            {
                "title": "Remote (HTTP) server",
                "required": ["url"],
                "properties": {"type": {"not": {"const": "stdio"}}},
                "not": {
                    "anyOf": [
                        {"required": [name]}
                        for name in ("command", "args", "env", "cwd")
                    ]
                },
            },
        ],
    }
    servers = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": MAX_IMPORTED_SERVERS,
        "additionalProperties": {"$ref": "#/$defs/Server"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Nebula MCP server configuration",
        "description": (
            "MCP servers in the Claude Desktop, Cursor, or VS Code format, as "
            "imported by nebula-core mcp import."
        ),
        "type": "object",
        "properties": {
            "$schema": {"type": "string"},
            "mcpServers": {"$ref": "#/$defs/Servers"},
            "servers": {"$ref": "#/$defs/Servers"},
            "mcp": {
                "type": "object",
                "properties": {"servers": {"$ref": "#/$defs/Servers"}},
                "required": ["servers"],
            },
        },
        "anyOf": [
            {"required": ["mcpServers"]},
            {"required": ["servers"]},
            {"required": ["mcp"]},
        ],
        "$defs": {
            **definitions,
            "Servers": servers,
            "Server": server,
            "NebulaOptions": {**options, "title": "Nebula settings"},
        },
    }


def _servers(config: Mapping[str, Any]) -> dict[str, Any]:
    if "mcpServers" in config:
        servers = config["mcpServers"]
    elif "servers" in config:
        servers = config["servers"]
    elif isinstance(config.get("mcp"), Mapping) and "servers" in config["mcp"]:
        servers = config["mcp"]["servers"]
    else:
        raise McpImportError(
            'the file must contain an "mcpServers" (or VS Code "servers") object'
        )
    if not isinstance(servers, Mapping):
        raise McpImportError("the MCP server list must be an object keyed by name")
    if not servers:
        raise McpImportError("the file does not define any MCP servers")
    if len(servers) > MAX_IMPORTED_SERVERS:
        raise McpImportError(
            f"a single import can contain at most {MAX_IMPORTED_SERVERS} servers"
        )
    return dict(servers)


def _draft(
    source_name: str,
    raw: Any,
    entry: McpImportEntry,
    request: McpImportRequest,
    which: Callable[[str], str | None],
) -> _Draft:
    if not isinstance(raw, Mapping):
        raise McpImportError("the server definition must be an object")
    name = _profile_name(source_name)
    entry.name = name
    if name != source_name:
        entry.warnings.append(f"renamed to {name!r} to fit Nebula server names")

    known = {"type", "transport", "command", "args", "env", "cwd", "url", "headers"}
    for key in raw:
        if key in known or key == "nebula":
            continue
        if key in _IGNORED_FIELDS:
            if _IGNORED_FIELDS[key]:
                entry.warnings.append(f"{key}: {_IGNORED_FIELDS[key]}")
        else:
            entry.warnings.append(f"ignored unsupported field {key!r}")

    options = McpImportOptions.model_validate(raw.get("nebula") or {})
    transport = _transport(raw)
    entry.transport = transport
    literals: dict[str, str] = {}
    fields: dict[str, Any] = {
        "name": name,
        "transport": transport,
        "enabled": False,
        "trusted_stdio": False,
    }

    def secret_ref(target: str, value: str, *, credential: bool) -> str | None:
        """Return a reference for ``value``, or None when it is a plain literal."""

        match = _REFERENCE.fullmatch(value)
        if match:
            reference = f"env:{match.group(1)}"
            entry.secrets.append(
                McpImportSecret(
                    target=target, source="environment", reference=reference
                )
            )
            return reference
        if "${" in value:
            if "${input:" in value:
                raise McpImportError(
                    f"{target} uses a prompted ${{input:...}} value; replace it with "
                    "${env:NAME} so Nebula can resolve it on the host"
                )
            raise McpImportError(
                f"{target} mixes text with a ${{...}} reference; only a whole-value "
                "reference such as ${NAME} is supported"
            )
        if not credential:
            return None
        if request.literal_secrets == "reject":
            raise McpImportError(
                f"{target} contains a literal credential; replace it with an "
                "environment reference such as ${NAME}"
            )
        literals[target] = value
        entry.secrets.append(
            McpImportSecret(target=target, source=request.literal_secrets)
        )
        return _PLACEHOLDER_REF

    if transport == McpTransport.STDIO:
        if "url" in raw or "headers" in raw:
            raise McpImportError("a stdio server cannot also define url or headers")
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpImportError("a stdio server requires a command")
        fields["command"] = _resolve_command(command.strip(), entry, which)
        entry.command = fields["command"]
        arguments = raw.get("args") or []
        if not isinstance(arguments, list) or not all(
            isinstance(item, (str, int, float)) for item in arguments
        ):
            raise McpImportError("args must be a list of strings")
        arguments = [str(item) for item in arguments]
        for index, item in enumerate(arguments):
            if "${" in item:
                raise McpImportError(
                    f"args[{index}] contains a ${{...}} reference; arguments are "
                    "passed literally, so move the value into env"
                )
        fields["arguments"] = arguments
        entry.arguments = arguments
        environment: dict[str, str] = {}
        environment_refs: dict[str, str] = {}
        raw_env = raw.get("env") or {}
        if not isinstance(raw_env, Mapping):
            raise McpImportError("env must be an object of strings")
        for key, value in raw_env.items():
            if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
                raise McpImportError(f"env name {key!r} is not a portable identifier")
            if isinstance(value, bool):
                value = "true" if value else "false"
            if not isinstance(value, (str, int, float)):
                raise McpImportError(f"env {key} must be a string")
            text = str(value)
            reference = secret_ref(f"env {key}", text, credential=_looks_secret(key))
            if reference is None:
                environment[key] = text
            else:
                environment_refs[key] = reference
        fields["environment"] = environment
        fields["environment_secret_refs"] = environment_refs
        cwd = raw.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.startswith("/"):
                raise McpImportError(
                    "cwd must be an absolute path on the Nebula host; omit it to run "
                    "in the project workspace"
                )
            fields["cwd_policy"] = McpCwdPolicy.FIXED
            fields["cwd"] = cwd
    else:
        if "command" in raw or "args" in raw or "env" in raw or "cwd" in raw:
            raise McpImportError(
                "an HTTP server cannot also define command, args, env, or cwd"
            )
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise McpImportError("an HTTP server requires a url")
        fields["url"] = url.strip()
        entry.url = fields["url"]
        raw_headers = raw.get("headers") or {}
        if not isinstance(raw_headers, Mapping):
            raise McpImportError("headers must be an object of strings")
        header_refs: dict[str, str] = {}
        auth_mode = McpAuthMode.NONE
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise McpImportError("headers must be an object of strings")
            bearer = _BEARER.fullmatch(value.strip())
            if key.lower() == "authorization" and bearer:
                fields["bearer_secret_ref"] = secret_ref(
                    "Authorization bearer token", bearer.group(1), credential=True
                )
                auth_mode = McpAuthMode.BEARER
                continue
            # Nebula stores no literal headers, so every header is a credential.
            header_refs[key] = secret_ref(f"header {key}", value, credential=True) or ""
        if header_refs and auth_mode == McpAuthMode.NONE:
            auth_mode = McpAuthMode.HEADERS
        fields["header_secret_refs"] = header_refs
        fields["auth_mode"] = auth_mode

    for key, value in options.model_dump(exclude_none=True).items():
        if value or key in {"required"}:
            fields[key] = value
    # Validate now so the preview reports the same errors an apply would.
    McpServerProfile.model_validate(fields)
    return _Draft(
        entry=entry,
        fields=fields,
        literals=literals,
        options={
            key: getattr(options, key)
            for key in options.model_fields_set
            if getattr(options, key) is not None
        },
    )


# Settings a file fully describes. Re-importing replaces them as a whole, so a
# variable or header removed from the file is removed from the server too.
_CONNECTION_FIELDS = (
    "transport",
    "command",
    "arguments",
    "url",
    "auth_mode",
    "bearer_secret_ref",
    "header_secret_refs",
    "environment",
    "environment_secret_refs",
    "cwd_policy",
    "cwd",
)
# What a trusted local program runs; changing any of it needs trust again.
_LAUNCH_FIELDS = (
    "transport",
    "command",
    "arguments",
    "environment",
    "cwd_policy",
    "cwd",
)


def _plan_new(draft: _Draft, request: McpImportRequest) -> None:
    """Apply this import's choices to a server it creates or replaces."""

    fields = draft.fields
    fields.setdefault("default_approval", request.defaults.default_approval)
    enabled = request.defaults.enabled
    trusted = False
    if enabled and fields["transport"] == McpTransport.STDIO:
        trusted = request.trust_local_programs
        enabled = trusted
        draft.entry.needs_trust = True
    fields["enabled"] = enabled
    fields["trusted_stdio"] = trusted
    draft.entry.enabled = enabled
    draft.entry.default_approval = fields["default_approval"]
    draft.entry.needs_probe = enabled


def _plan_update(
    draft: _Draft,
    current: McpServerProfile,
    request: McpImportRequest,
    credential_store: CredentialStore,
) -> None:
    """Turn ``draft`` into ``current`` with only what the file changes.

    Sets the entry to ``unchanged`` when nothing differs, otherwise to
    ``update`` with the list of changes and the resulting state.
    """

    entry = draft.entry
    _reuse_saved_secrets(draft, current, credential_store)
    reset = _reset_fields()
    merged = current.model_dump()
    for key in _CONNECTION_FIELDS:
        merged[key] = draft.fields[key] if key in draft.fields else reset[key]
    merged.update(draft.options)
    changes = _changes(current.model_dump(), merged)
    entry.enabled = current.enabled
    entry.default_approval = merged["default_approval"]
    if not changes:
        entry.action = "unchanged"
        return

    launch_changed = any(merged[key] != getattr(current, key) for key in _LAUNCH_FIELDS)
    launch_changed |= set(merged["environment_secret_refs"]) != set(
        current.environment_secret_refs
    )
    connection_changed = any(
        merged[key] != getattr(current, key) for key in _CONNECTION_FIELDS
    )
    stdio = merged["transport"] == McpTransport.STDIO
    trusted = stdio and current.trusted_stdio and not launch_changed
    enabled = current.enabled
    if enabled and stdio and not trusted:
        trusted = request.trust_local_programs
        enabled = trusted
        entry.needs_trust = True
    merged["trusted_stdio"] = trusted
    merged["enabled"] = enabled
    if connection_changed:
        merged["capabilities"] = McpCapabilitySnapshot()
    # Validate now so the preview reports the same errors an apply would.
    McpServerProfile.model_validate(merged)
    entry.action = "update"
    entry.changes = changes
    entry.enabled = enabled
    entry.needs_probe = enabled and connection_changed
    draft.fields = merged
    draft.existing_id = current.id
    draft.existing_revision = current.revision


def _reuse_saved_secrets(
    draft: _Draft, current: McpServerProfile, credential_store: CredentialStore
) -> None:
    """Keep a saved credential whose value the file repeats instead of a copy."""

    saved = {
        "Authorization bearer token": current.bearer_secret_ref,
        **{f"env {key}": ref for key, ref in current.environment_secret_refs.items()},
        **{f"header {key}": ref for key, ref in current.header_secret_refs.items()},
    }
    reused: dict[str, str] = {}
    for target, value in draft.literals.items():
        reference = saved.get(target)
        if not reference or not reference.startswith(("vault:", "session:")):
            continue
        try:
            stored = credential_store.resolve(reference).get_secret_value()
        except (CredentialError, ValueError):
            # diagnostic-expected: an unreadable credential counts as changed
            continue
        if hmac.compare_digest(stored.encode(), value.encode()):
            reused[target] = reference
    if not reused:
        return
    draft.fields = _fill_references(draft.fields, reused)
    for target in reused:
        del draft.literals[target]
    for secret in draft.entry.secrets:
        if secret.target in reused:
            secret.reference = reused[secret.target]
            secret.source = (
                "vault" if secret.reference.startswith("vault:") else "session"
            )


def _changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[McpImportChange]:
    old, new = _describe(before), _describe(after)
    return [
        McpImportChange(field=name, before=old.get(name), after=new.get(name))
        for name in dict.fromkeys([*old, *new])
        if old.get(name) != new.get(name)
    ]


def _describe(values: Mapping[str, Any]) -> dict[str, str]:
    """Flatten a profile into the file's field names, describing secrets."""

    def secret(reference: str | None) -> str | None:
        if not reference:
            return None
        if reference == _PLACEHOLDER_REF:
            return "new credential"
        if reference.startswith("env:"):
            return "${" + reference.removeprefix("env:") + "}"
        if reference.startswith("session:"):
            return "credential for this session"
        return "stored credential"

    transport = McpTransport(values["transport"])
    fields: dict[str, str | None] = {
        "type": "stdio" if transport == McpTransport.STDIO else "http",
        "command": values.get("command"),
        "args": shlex.join(values.get("arguments") or []) or None,
        "url": values.get("url"),
        "bearer token": secret(values.get("bearer_secret_ref")),
    }
    if transport == McpTransport.STDIO:
        fields["cwd"] = (
            values.get("cwd")
            if values.get("cwd_policy") == McpCwdPolicy.FIXED
            else "project workspace"
        )
    for key, value in sorted((values.get("environment") or {}).items()):
        fields[f"env {key}"] = value
    for key, ref in sorted((values.get("environment_secret_refs") or {}).items()):
        fields[f"env {key}"] = secret(ref)
    for key, ref in sorted((values.get("header_secret_refs") or {}).items()):
        fields[f"header {key}"] = secret(ref)
    approval = values.get("default_approval") or McpApprovalMode.RISK_BASED
    fields["nebula.default_approval"] = McpApprovalMode(approval).value
    overrides = values.get("tool_overrides") or {}
    fields["nebula.tool_overrides"] = (
        ", ".join(
            f"{name}={McpApprovalMode(mode).value}"
            for name, mode in sorted(overrides.items())
        )
        or None
    )
    for key in ("enabled_tools", "disabled_tools"):
        fields[f"nebula.{key}"] = ", ".join(values.get(key) or []) or None
    fields["nebula.required"] = "true" if values.get("required") else "false"
    for key in ("startup_timeout_seconds", "tool_timeout_seconds"):
        fields[f"nebula.{key}"] = f"{values[key]:g}"
    return {name: value for name, value in fields.items() if value is not None}


def _apply(
    draft: _Draft,
    request: McpImportRequest,
    store: NebulaStore,
    credential_store: CredentialStore,
) -> None:
    entry = draft.entry
    fields = dict(draft.fields)
    stored: list[str] = []
    try:
        references: dict[str, str] = {}
        for target, value in draft.literals.items():
            status = credential_store.create(
                CredentialCreateRequest(
                    secret=SecretStr(value),
                    persistence="session"
                    if request.literal_secrets == "session"
                    else "vault",
                )
            )
            references[target] = status.reference
            stored.append(status.reference)
        for secret in entry.secrets:
            if secret.target in references:
                secret.reference = references[secret.target]
        fields = _fill_references(fields, references)
        fields["metadata"] = {
            "imported": {
                "format": "mcpServers",
                "source_name": request.source_name,
                "server_name": entry.source_name,
                "imported_at": utc_now().isoformat(),
            }
        }
        if draft.existing_id is None:
            created = store.create(McpServerProfile.model_validate(fields))
            entry.profile_id = created.id
            return
        current = store.get(McpServerProfile, draft.existing_id)
        if entry.action == "update" and current.revision != draft.existing_revision:
            raise McpImportError(
                f"{current.name} changed while importing; preview the file again"
            )
        previous_refs = _owned_references(current)
        base = fields if entry.action == "update" else {**_reset_fields(), **fields}
        replacement = McpServerProfile.model_validate(
            {
                **base,
                "id": current.id,
                "created_at": current.created_at,
                "metadata": {**current.metadata, **fields["metadata"]},
            }
        )
        store.replace(
            McpServerProfile,
            current.id,
            replacement,
            expected_revision=current.revision,
        )
        entry.profile_id = current.id
        _release_orphaned(
            previous_refs - _owned_references(replacement),
            store,
            credential_store,
        )
    except Exception as exc:
        record_caught_exception(
            "harnesses",
            "harnesses.mcp_import.apply_failed",
            "An MCP server import entry could not be saved.",
            exc,
            stage="mcp_import",
        )
        for reference in stored:
            try:
                credential_store.delete(reference)
            except Exception:  # diagnostic-expected: best-effort rollback
                pass
        entry.action = "invalid"
        entry.error = _message(exc)


def _fill_references(fields: dict[str, Any], references: dict[str, str]) -> dict:
    """Swap placeholder refs for stored ones, matched by secret target."""

    if not references:
        return fields
    result = dict(fields)
    if "Authorization bearer token" in references:
        result["bearer_secret_ref"] = references["Authorization bearer token"]
    for name, prefix in (
        ("environment_secret_refs", "env "),
        ("header_secret_refs", "header "),
    ):
        refs = dict(result.get(name) or {})
        for key in refs:
            if prefix + key in references:
                refs[key] = references[prefix + key]
        result[name] = refs
    return result


def _reset_fields() -> dict[str, Any]:
    """Defaults a replaced profile returns to before the new file is applied."""

    return {
        "command": None,
        "arguments": [],
        "url": None,
        "auth_mode": McpAuthMode.NONE,
        "bearer_secret_ref": None,
        "header_secret_refs": {},
        "environment": {},
        "environment_secret_refs": {},
        "cwd_policy": McpCwdPolicy.WORKSPACE,
        "cwd": None,
        "enabled_tools": [],
        "disabled_tools": [],
        "default_approval": McpApprovalMode.RISK_BASED,
        "tool_overrides": {},
        "required": False,
        "capabilities": McpCapabilitySnapshot(),
    }


def _owned_references(profile: McpServerProfile) -> set[str]:
    references = {
        *profile.environment_secret_refs.values(),
        *profile.header_secret_refs.values(),
    }
    if profile.bearer_secret_ref:
        references.add(profile.bearer_secret_ref)
    return {item for item in references if item.startswith(("vault:", "session:"))}


def _release_orphaned(
    references: set[str], store: NebulaStore, credential_store: CredentialStore
) -> None:
    if not references:
        return
    in_use: set[str] = set()
    for profile in _all_profiles(store):
        in_use |= _owned_references(profile)
    for reference in references - in_use:
        try:
            credential_store.delete(reference)
        except Exception:  # diagnostic-expected: an orphan is harmless if kept
            pass


def _all_profiles(store: NebulaStore) -> list[McpServerProfile]:
    profiles: list[McpServerProfile] = []
    while True:
        batch = store.list_entities(McpServerProfile, offset=len(profiles), limit=1000)
        profiles.extend(batch)
        if len(batch) < 1000:
            return profiles


def _transport(raw: Mapping[str, Any]) -> McpTransport:
    declared = raw.get("type", raw.get("transport"))
    if declared is None:
        if "command" in raw:
            return McpTransport.STDIO
        if "url" in raw:
            return McpTransport.STREAMABLE_HTTP
        raise McpImportError("the server needs either a command or a url")
    if not isinstance(declared, str):
        raise McpImportError("type must be a string")
    normalized = declared.strip().lower()
    if normalized == "sse":
        raise McpImportError(
            "the legacy SSE transport is not supported; use the server's "
            "streamable HTTP endpoint instead"
        )
    if normalized not in _TRANSPORT_ALIASES:
        raise McpImportError(f"unsupported MCP transport {declared!r}")
    return _TRANSPORT_ALIASES[normalized]


def _resolve_command(
    command: str, entry: McpImportEntry, which: Callable[[str], str | None]
) -> str:
    if command.startswith("/"):
        if not Path(command).is_file():
            entry.warnings.append(
                f"{command} does not exist on the Nebula host yet; probing will "
                "fail until it is installed"
            )
        return command
    if "/" in command or command.startswith("~"):
        raise McpImportError(
            "command must be a program name on PATH or an absolute path on the "
            "Nebula host"
        )
    resolved = which(command)
    if not resolved:
        raise McpImportError(
            f"{command!r} is not installed on the Nebula host PATH; install it or "
            "use an absolute path"
        )
    resolved = str(Path(resolved).absolute())
    entry.warnings.append(f"resolved {command} to {resolved}")
    return resolved


def _profile_name(value: str) -> str:
    if _NAME_PATTERN.fullmatch(value) and len(value) <= 200:
        return value
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:200]
    if not cleaned:
        raise McpImportError("the server name must contain letters or digits")
    return cleaned


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith("_key") or any(
        token in lowered for token in _SECRET_NAME_TOKENS
    )


def _message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            location = ".".join(str(item) for item in error.get("loc", ()))
            message = str(error.get("msg", "invalid value")).removeprefix(
                "Value error, "
            )
            parts.append(f"{location}: {message}" if location else message)
        return "; ".join(parts)[:2_000]
    return (str(exc) or type(exc).__name__)[:2_000]
