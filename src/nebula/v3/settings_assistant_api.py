"""Authenticated, project-bound access to Core Settings operations for the control assistant."""

from __future__ import annotations

from .async_utils import async_take

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.routing import Match

from .domain import (
    HarnessProfile,
    McpServerProfile,
    ProviderProfile,
    RunnerProfile,
    SshEnvironment,
    SshEnvironmentApproval,
)
from .environments import SshEnvironmentSettings, environment_id
from .mcp_import import export_mcp_config
from .mcp_scope import mcp_metadata_is_visible, project_mcp_profiles
from .storage import NotFoundError
from .tools import GuidedToolError


_PROJECT_SETTING = re.compile(
    r"^/api/v1/engagements/\{project\}/(?:"
    r"scope(?:/tool-candidates)?|automation-policy|post-tool-assistant|"
    r"assistant-defaults|browser-security-approval|agent-security/(?:start|stop|status)"
    r")$"
)
_GLOBAL_SETTINGS = re.compile(
    r"^/api/v1/(?:"
    r"providers|provider-catalog|harnesses|mcp-servers|ssh-environments|"
    r"runner-profiles|vpn-profiles|operator-profiles|credentials/(?:vault|[A-Za-z0-9_.~:-]+/status)|skills|"
    r"integrations|automation/runtime|diagnostics/(?:settings|performance)|voice/settings|butler/(?:instructions|check-interval)|"
    r"hooks|project-instructions|runtime-usage|host-load|host-pressure|host-cpu-budget|setup|release"
    r")(?:/[A-Za-z0-9_.~%:-]+)*$"
)
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_BOOLEAN = TypeAdapter(bool)
_PROFILE_MODELS: dict[str, type[BaseModel]] = {
    "providers": ProviderProfile,
    "harnesses": HarnessProfile,
    "mcp-servers": McpServerProfile,
}
_PROFILE_PRIVATE_FIELDS = {
    "providers": ("secret_ref", "openrouter_management_secret_ref"),
    "harnesses": ("auth_mode", "secret_ref", "permission_mode"),
    "mcp-servers": (
        "auth_mode",
        "bearer_secret_ref",
        "header_secret_refs",
        "environment_secret_refs",
        "trusted_stdio",
    ),
}
_BROWSER_OWNED_OPERATIONS = (
    ("POST", re.compile(r"^/api/v1/mcp-servers/import$"), "mcp"),
    ("PUT", re.compile(r"^/api/v1/integrations/typesafe$"), "typesafe"),
    ("DELETE", re.compile(r"^/api/v1/integrations/typesafe$"), "typesafe"),
    ("POST", re.compile(r"^/api/v1/vpn-profiles$"), "vpn-egress"),
    ("DELETE", re.compile(r"^/api/v1/vpn-profiles/[^/]+$"), "vpn-egress"),
)
_REDACT_KEYS = {
    "secret",
    "password",
    "token",
    "api_key",
    "private_key",
    "recovery_code",
}


def _clean_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 600:
        raise ValueError("provide a bounded Settings API path")
    value = path if path.startswith("/api/v1/") else "/api/v1/" + path.lstrip("/")
    decoded = unquote(value)
    if (
        "?" in value
        or "#" in value
        or "\\" in value
        or any(segment in {"", ".", ".."} for segment in decoded.split("/")[1:])
        or "%" in value
        or not value.isascii()
        or (
            "{" in value.replace("{project}", "")
            or "}" in value.replace("{project}", "")
        )
        or not (_PROJECT_SETTING.fullmatch(value) or _GLOBAL_SETTINGS.fullmatch(value))
    ):
        raise ValueError("this path is outside the assistant's Settings API")
    return value


def _route_exists(app: FastAPI, method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    return any(
        method in getattr(route, "methods", ())
        and _operation_is_allowed(method, getattr(route, "path", ""))
        and route.matches(scope)[0] == Match.FULL
        for route in app.routes
    )


def _openapi_path(path: str) -> str:
    return path.replace("{project}", "{engagement_id}")


def _template_is_allowed(template: str) -> bool:
    if any(
        fragment in template
        for fragment in (
            "/auth/pairings/redeem",
            "/auth/devices/current/capabilities",
            "/test-turn",
            "/external-sessions",
        )
    ):
        return False
    candidate = template.replace("{engagement_id}", "{project}")
    candidate = re.sub(r"\{(?!project\})[^/}]+\}", "example", candidate)
    try:
        _clean_path(candidate)
    except (
        ValueError
    ):  # diagnostic-expected: routes outside Settings are omitted from discovery.
        return False
    return True


def _safe_result(value: Any) -> Any:
    if isinstance(value, list):
        return [_safe_result(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.casefold() in _REDACT_KEYS else _safe_result(item)
            for key, item in value.items()
        }
    return value


def _operation_is_allowed(method: str, template: str) -> bool:
    return (
        _template_is_allowed(template)
        and _browser_owned_destination(method, template) is None
    )


def _browser_owned_destination(method: str, path: str) -> str | None:
    return next(
        (
            destination
            for verb, pattern, destination in _BROWSER_OWNED_OPERATIONS
            if verb == method.upper() and pattern.fullmatch(path)
        ),
        None,
    )


def _require_public_input(method: str, path: str) -> None:
    destination = _browser_owned_destination(method, path)
    if destination:
        raise ValueError(
            "private input and credential removal belong in the requesting browser; "
            f"use nebula.settings.open setting_id={destination}"
        )


async def _check_profile_mutation(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None,
    expected_revision: int | None,
    project_id: str,
) -> None:
    route = re.fullmatch(r"/api/v1/(providers|harnesses|mcp-servers)(/[^/]+)?", path)
    if route is None or method not in {"POST", "PATCH", "PUT"}:
        return
    family, resource = route.groups()
    if (method == "POST") != (resource is None):
        return
    if not isinstance(body, dict):
        raise ValueError("provide the listed profile object")
    model = _PROFILE_MODELS[family]
    destination = {
        "providers": "providers",
        "harnesses": "harnesses",
        "mcp-servers": "mcp",
    }[family]
    current: dict[str, Any] | None = None
    if resource:
        response = await client.get(path, headers=headers)
        if not response.is_success:
            raise ValueError("read the exact current profile before changing it")
        current = response.json()
        revision = (
            body.get("expected_revision")
            if method == "PATCH"
            else expected_revision or body.get("revision")
        )
        if type(revision) is not int or revision < 1:
            raise ValueError("read the exact profile and supply its current revision")
        if revision != current["revision"]:
            raise ValueError(
                "Settings profile revision conflict (409): profile changed; read its current revision before retrying"
            )
        if method == "PUT":
            if "revision" in body and body["revision"] != revision:
                raise ValueError("replacement revision must match the observed profile")
            headers["If-Match"] = str(revision)
    if method == "PATCH":
        if not isinstance(body.get("changes"), dict):
            raise ValueError("provide the listed profile changes")
        payload = {**(current or {}), **body["changes"]}
    else:
        payload = body
    try:
        candidate = model.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        # diagnostic-expected: explain invalid model input without echoing private values.
        raise ValueError(
            "invalid profile input; read its current Settings schema"
        ) from exc
    defaults = model.model_construct().model_dump(mode="json")
    before = current if current is not None else defaults
    if family == "mcp-servers":
        _require_mcp_scope(candidate, project_id, before_execution=True)
        if current is not None and candidate["metadata"].get(
            "engagement_id"
        ) != current["metadata"].get("engagement_id"):
            raise ValueError("preserve the MCP server's project ownership")
    protected = _PROFILE_PRIVATE_FIELDS[family]
    changed = any(candidate.get(field) != before.get(field) for field in protected)
    if family in {"providers", "harnesses"}:
        changed |= any(
            candidate["privacy"][field] != before["privacy"][field]
            for field in ("permits_sensitive_data", "auto_share_tool_results")
        )
    if family == "mcp-servers" and before.get("trusted_stdio"):
        changed |= any(
            candidate.get(field) != before.get(field)
            for field in (
                "transport",
                "command",
                "arguments",
                "environment",
                "cwd",
                "cwd_policy",
                "url",
            )
        )
    if changed:
        raise ValueError(
            "credential attachment, private input, sharing consent and local-program trust "
            f"belong in the requesting browser; use nebula.settings.open setting_id={destination}. "
            "Preserve their current values for a nonsecret profile edit."
        )
    if (
        family == "harnesses"
        and (
            candidate.get("connection_mode") == "spawn"
            or (current is not None and current.get("connection_mode") == "spawn")
        )
        and (
            current is None
            or any(
                candidate.get(field) != current.get(field)
                for field in (
                    "kind",
                    "connection_mode",
                    "transport",
                    "executable",
                    "home_directory",
                    "endpoint",
                )
            )
        )
    ):
        raise GuidedToolError(
            "harness program trust belongs in the requesting browser",
            code="harness_trust_required",
            category="policy_denied",
            problem="A spawned Assistant harness or its launch configuration needs operator review.",
            next_action="Use nebula.settings.open setting_id=harnesses. Preserve the existing program, account home and connection configuration for metadata edits. Endpoint profiles remain subject to Core validation and existing credential controls.",
            before_execution=True,
        )


def _require_mcp_scope(
    profile: dict[str, Any], project_id: str, *, before_execution: bool = False
) -> None:
    if not mcp_metadata_is_visible(profile.get("metadata", {}), project_id):
        raise GuidedToolError(
            "MCP server is not in the selected project",
            code="mcp_project_scope",
            category="policy_denied",
            problem="MCP server is not in the selected project.",
            next_action="List MCP profiles for the selected project and choose a shared or selected-project profile. Do not retry this foreign identity.",
            before_execution=before_execution,
        )


async def _check_host_policy(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None,
    expected_revision: int | None,
) -> None:
    if method != "PUT" or not re.fullmatch(
        r"/api/v1/engagements/[A-Za-z0-9_.~:-]+/automation-policy", path
    ):
        return
    revision = body.get("expected_revision") if isinstance(body, dict) else None
    if type(revision) is not int or revision < 1:
        raise GuidedToolError(
            "policy PUT requires the observed body.expected_revision",
            code="settings_revision_required",
            category="invalid_arguments",
            problem="Read the current project policy before changing it.",
            next_action="GET the selected project's automation-policy, preserve unrelated fields, and supply its revision in body.expected_revision. If-Match alone is not this route's revision guard.",
            field="body.expected_revision",
            before_execution=True,
        )
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("policy body and request revisions must match")
    assert isinstance(body, dict)
    try:
        acknowledged = _BOOLEAN.validate_python(
            body.get("host_access_acknowledged", False)
        )
    except ValidationError as exc:
        # diagnostic-expected: reject invalid consent without echoing caller values.
        raise ValueError(
            "host_access_acknowledged must match the Core boolean schema"
        ) from exc
    response = await client.get(path, headers=headers)
    if not response.is_success:
        raise ValueError("read the current project policy before changing it")
    current = response.json()
    if revision != current["revision"]:
        raise GuidedToolError(
            "project policy revision conflict",
            code="settings_revision_conflict",
            category="invalid_arguments",
            problem="The project policy changed after it was read.",
            next_action="Read the policy again and use its current body.expected_revision; preserve the operator's other settings.",
            field="body.expected_revision",
            before_execution=True,
        )
    mode = body.get("execution_mode") or current.get("execution_mode")
    if not current.get("host_access_acknowledged") and (acknowledged or mode == "host"):
        raise GuidedToolError(
            "first Host access acknowledgment belongs in the requesting browser",
            code="host_consent_required",
            category="policy_denied",
            problem="Host access needs the operator's one-time browser acknowledgment.",
            next_action="Use nebula.settings.open setting_id=network-scope. The operator must acknowledge Host access in the requesting browser; then read the policy again before another edit.",
            field="body.host_access_acknowledged",
            before_execution=True,
        )


async def _check_post_tool_consent(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None,
) -> None:
    if method != "PUT" or not re.fullmatch(
        r"/api/v1/engagements/[^/]+/post-tool-assistant", path
    ):
        return
    if not isinstance(body, dict) or type(body.get("expected_revision")) is not int:
        raise ValueError(
            "read the post-tool settings and supply body.expected_revision"
        )
    from .execution_ai import PostToolAssistantRequest

    try:
        candidate = PostToolAssistantRequest.model_validate(body)
    except ValidationError as exc:
        # diagnostic-expected: do not echo configuration values in model errors.
        raise ValueError("invalid post-tool settings; read the current schema") from exc
    response = await client.get(path, headers=headers)
    if not response.is_success:
        raise ValueError("read the current post-tool settings before changing them")
    current = response.json()
    if candidate.expected_revision != current["revision"]:
        raise ValueError(
            "post-tool settings revision conflict (409): read current settings before retrying"
        )
    identity = (
        candidate.harness_profile_id
        if candidate.backend_kind == "harness"
        else candidate.provider_id
    )
    previous_identity = current.get(
        "harness_profile_id" if current["backend_kind"] == "harness" else "provider_id"
    )
    if candidate.cloud_confirmed and (
        not current["cloud_confirmed"]
        or candidate.backend_kind != current["backend_kind"]
        or identity != previous_identity
    ):
        raise GuidedToolError(
            "post-tool cloud consent belongs in the requesting browser",
            code="post_tool_consent_required",
            category="policy_denied",
            problem="Project data use needs the operator's consent for this follow-up backend.",
            next_action="Use nebula.settings.open setting_id=follow-up. Preserve existing consent only for the same backend, or revoke it; changing the backend requires fresh browser consent.",
            before_execution=True,
        )


async def _check_runtime_mutation(
    app: FastAPI,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | list[Any] | None:
    route = re.fullmatch(r"/api/v1/(runner-profiles|ssh-environments)/([^/]+)", path)
    if method != "PUT" or route is None:
        return body
    family, identity = route.groups()
    authorized = await client.get(
        f"/api/v1/{family}",
        headers=headers,
        params={"resolve": "false"} if family == "ssh-environments" else None,
    )
    if not authorized.is_success:
        raise ValueError("read the current runtime settings before changing them")
    if not isinstance(body, dict):
        raise ValueError("provide the listed runtime settings object")
    try:
        if family == "runner-profiles":
            current: RunnerProfile | SshEnvironment | None = await app.state.store.get(
                RunnerProfile, identity
            )
        else:
            current = await app.state.store.get(
                SshEnvironment, environment_id(identity)
            )
    except NotFoundError:
        # diagnostic-expected: first-time settings have no saved entity.
        current = None
    if family == "runner-profiles":
        # Match the route's effective defaults, including omitted fields that clear
        # an existing context/socket/seccomp selection. Import after API startup.
        from .api import RunnerProfileRequest

        try:
            candidate = RunnerProfileRequest.model_validate(body).model_dump(
                mode="json"
            )
        except ValidationError as exc:
            # diagnostic-expected: invalid model input must not echo host values.
            raise ValueError(
                "invalid runner settings; read the current schema"
            ) from exc
        protected = (
            "runtime",
            "executable",
            "context",
            "socket",
            "platform",
            "isolation",
            "seccomp_profile",
        )
        if current is None or any(
            candidate[field] != current.model_dump(mode="json")[field]
            for field in protected
        ):
            raise GuidedToolError(
                "runner trust belongs in the requesting browser",
                code="runner_trust_required",
                category="policy_denied",
                problem="A runner program or its isolation configuration needs operator review.",
                next_action="Use nebula.settings.open setting_id=runners. Preserve every existing runtime, executable, context, socket, platform, isolation and seccomp_profile value for a nonsecret edit.",
                before_execution=True,
            )
    else:
        try:
            settings = SshEnvironmentSettings.model_validate(body)
        except ValidationError as exc:
            # diagnostic-expected: invalid settings are described without values.
            raise ValueError("invalid SSH settings; read the current schema") from exc
        before = (
            current.command_approval
            if isinstance(current, SshEnvironment)
            else SshEnvironmentApproval.ASK
        )
        if (
            settings.command_approval == SshEnvironmentApproval.ALLOW
            and before != SshEnvironmentApproval.ALLOW
        ):
            raise GuidedToolError(
                "standing SSH command approval belongs in the requesting browser",
                code="ssh_approval_required",
                category="policy_denied",
                problem="Only the operator can grant standing command approval for an SSH host.",
                next_action="Use nebula.settings.open setting_id=ssh-environments. Preserve the current approval or tighten it to ask for ordinary metadata edits.",
                before_execution=True,
            )
        if current is None:
            if settings.expected_revision is not None:
                raise ValueError(
                    "SSH settings revision conflict (409): host settings no longer exist"
                )
            return {**body, "expected_absent": True}
        if settings.expected_absent:
            raise ValueError(
                "SSH settings revision conflict (409): host settings already exist"
            )
    revision = body.get("expected_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError(
            "read the exact runtime settings and supply body.expected_revision"
        )
    if current is None or revision != current.revision:
        raise ValueError(
            "runtime settings revision conflict (409): read current settings before retrying"
        )
    return body


def _action_readback(method: str, path: str) -> str | None:
    if method == "POST":
        if re.fullmatch(
            r"/api/v1/setup/(?:runtime/(?:select|refresh)|image/(?:prepare|retry|cancel))",
            path,
        ):
            return "/api/v1/setup/status"
        if path == "/api/v1/automation/runtime/prepare":
            return "/api/v1/automation/runtime"
        profile = re.fullmatch(
            r"(/api/v1/(?:providers|harnesses)/[^/]+)/(?:health|capabilities/verify)",
            path,
        )
        if profile:
            return profile[1]
        mcp = re.fullmatch(r"(/api/v1/mcp-servers/[^/]+)/probe", path)
        if mcp:
            return mcp[1]
        if re.fullmatch(r"/api/v1/operator-profiles/[^/]+/activate", path):
            return "/api/v1/operator-profiles/active"
    if method in {"POST", "PUT", "DELETE"} and re.fullmatch(
        r"/api/v1/integrations/web-search(?:/(?:install|start|stop|test|engines))?",
        path,
    ):
        return "/api/v1/integrations/web-search"
    return None


async def _settings_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None = None,
    expected_revision: int | None = None,
    *,
    before_execution: bool = True,
) -> httpx.Response:
    """Keep MCP profiles and setup image controls within the selected project."""

    if method == "POST" and path in {
        "/api/v1/setup/image/prepare",
        "/api/v1/setup/image/retry",
    }:
        if body is not None and not isinstance(body, dict):
            raise ValueError(
                "provide a setup preparation object for the selected project"
            )
        if isinstance(body, dict) and body.get("project_id") not in (None, project_id):
            raise ValueError("prepare an image only for the selected project")
        # Retry's public API inherits the previous operation's project when
        # omitted. An interactive Butler request must retain its own scope.
        body = {**(body or {}), "project_id": project_id}
    if method == "POST" and path == "/api/v1/setup/image/cancel":
        current = await client.get("/api/v1/setup/status", headers=headers)
        if not current.is_success:
            return current
        preparation = current.json()["terminal"]["image_preparation"]
        operation_id = body.get("operation_id") if isinstance(body, dict) else None
        if (
            operation_id is not None
            and preparation.get("operation_id") == operation_id
            and preparation.get("project_id") != project_id
        ):
            raise ValueError("cancel only an image operation in the selected project")
        # Core still checks the exact operation ID, including races and
        # idempotent cancellation; never substitute a newer operation here.
    if method == "GET" and path in {
        "/api/v1/mcp-servers",
        "/api/v1/mcp-servers/export",
    }:
        # Authenticate through Core before reading its store. Filtering must precede
        # serialization: exports omit ownership and can include names in warnings.
        authorized = await client.get("/api/v1/mcp-servers/schema", headers=headers)
        if not authorized.is_success:
            return authorized
        profiles = project_mcp_profiles(app.state.store, project_id)
        if path.endswith("/export"):
            result: Any = export_mcp_config(
                [profile async for _, profile in profiles]
            ).model_dump(mode="json")
        else:
            result = [
                profile.model_dump(mode="json")
                for _, profile in await async_take(profiles, 100)
            ]
        return httpx.Response(200, json=result)
    exact = re.fullmatch(r"(/api/v1/mcp-servers/([^/]+))(/probe)?", path)
    if exact and exact[2] != "schema":
        current = await client.get(exact[1], headers=headers)
        if not current.is_success:
            return current
        _require_mcp_scope(
            current.json(), project_id, before_execution=before_execution
        )
        if exact[3]:
            if not isinstance(body, dict) or body.get("engagement_id") not in (
                None,
                project_id,
            ):
                raise ValueError("probe MCP servers only in the selected project")
            body = {**body, "engagement_id": project_id}
    await _check_host_policy(client, headers, method, path, body, expected_revision)
    await _check_post_tool_consent(client, headers, method, path, body)
    operator = re.fullmatch(r"(/api/v1/operator-profiles/[^/]+)(/activate)?", path)
    if operator and (method == "PATCH" or (method == "POST" and operator[2])):
        revision = body.get("expected_revision") if isinstance(body, dict) else None
        if type(revision) is not int or revision < 1:
            raise ValueError(
                "read the exact operator profile and supply body.expected_revision"
            )
        current_operator = await client.get(operator[1], headers=headers)
        if (
            not current_operator.is_success
            or current_operator.json()["revision"] != revision
        ):
            raise ValueError(
                "operator profile revision conflict (409): read the current profile before retrying"
            )
    await _check_profile_mutation(
        client, headers, method, path, body, expected_revision, project_id
    )
    body = await _check_runtime_mutation(app, client, headers, method, path, body)
    params: dict[str, Any] = {}
    if method == "GET" and path in {
        "/api/v1/skills",
        "/api/v1/skills/catalog",
        "/api/v1/hooks",
        "/api/v1/project-instructions",
    }:
        params["engagement_id"] = project_id
    if method == "GET" and path == "/api/v1/automation/runtime" and before_execution:
        params["engagement_id"] = project_id
    if method == "GET" and path == "/api/v1/ssh-environments":
        params["resolve"] = "false"
    if method == "DELETE" and re.fullmatch(r"/api/v1/ssh-environments/[^/]+", path):
        params["expected_revision"] = expected_revision
    response = await client.request(
        method, path, json=body, headers=headers, params=params
    )
    if exact and not exact[3] and exact[2] != "schema" and response.is_success:
        if response.content:
            _require_mcp_scope(response.json(), project_id)
    return response


def _schema_with_refs(schema: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if depth > 5:
        return schema
    if isinstance(schema, list):
        return [_schema_with_refs(item, components, depth + 1) for item in schema]
    if not isinstance(schema, dict):
        return schema
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        return _schema_with_refs(components.get(name, schema), components, depth + 1)
    return {
        key: _schema_with_refs(value, components, depth + 1)
        for key, value in schema.items()
    }


def settings_api_schema(
    app: FastAPI, *, path: str | None = None, method: str | None = None
) -> dict[str, Any]:
    """Expose the current build's existing Settings routes and validated bodies."""

    openapi = app.openapi()
    paths = openapi["paths"]
    if path is None:
        operations = []
        for template, entries in paths.items():
            if not _template_is_allowed(template):
                continue
            candidate = template.replace("{engagement_id}", "{project}")
            for verb, operation in entries.items():
                if verb.upper() in _METHODS and _operation_is_allowed(verb, template):
                    operations.append(
                        {
                            "method": verb.upper(),
                            "path": candidate,
                            "summary": operation.get("summary", ""),
                        }
                    )
        return {"operations": operations}
    raw = path if path.startswith("/api/v1/") else "/api/v1/" + path.lstrip("/")
    if "{" in raw and raw.replace("{project}", "").count("{"):
        if not _template_is_allowed(raw):
            raise ValueError("this path is outside the assistant's Settings API")
        candidate = raw
    else:
        candidate = _clean_path(path)
    selected = (method or "GET").upper()
    if selected not in _METHODS:
        raise ValueError("choose a listed HTTP method")
    template = _openapi_path(candidate)
    _require_public_input(selected, template)
    if not _template_is_allowed(template):
        raise ValueError("this path is outside the assistant's Settings API")
    operation = paths.get(template, {}).get(selected.lower())
    if operation is None:
        # A concrete resource id matches one OpenAPI path template.
        for option, entries in paths.items():
            if not _operation_is_allowed(selected, option):
                continue
            pattern = "/".join(
                "[^/]+"
                if segment.startswith("{") and segment.endswith("}")
                else re.escape(segment)
                for segment in option.split("/")
            )
            if re.fullmatch(pattern, template):
                operation = entries.get(selected.lower())
                if operation is not None:
                    template = option
                    break
    if operation is None:
        raise ValueError("this Settings operation is not in the current Core build")
    return {
        "method": selected,
        "path": template.replace("{engagement_id}", "{project}"),
        "summary": operation.get("summary", ""),
        "request_body": _schema_with_refs(
            operation.get("requestBody", {}),
            openapi.get("components", {}).get("schemas", {}),
        ),
        "parameters": operation.get("parameters", []),
    }


async def execute_settings_api(
    app: FastAPI,
    token: str,
    project_id: str,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | list[Any] | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Call only an existing Settings route, with the active project supplied by Core."""

    selected = method.upper()
    if selected not in _METHODS:
        raise ValueError("choose GET, POST, PUT, PATCH, or DELETE")
    candidate = _clean_path(path)
    _require_public_input(selected, candidate)
    if "/engagements/" in candidate:
        if not re.fullmatch(r"[A-Za-z0-9_.~:-]+", project_id):
            raise ValueError("selected project has an invalid identifier")
        candidate = candidate.replace("{project}", project_id)
    if not _route_exists(app, selected, candidate):
        raise ValueError("this Settings operation is not in the current Core build")
    if selected == "GET" and body is not None:
        raise ValueError("GET Settings requests have no body")
    if expected_revision is not None and (
        selected not in {"DELETE", "PUT"}
        or type(expected_revision) is not int
        or expected_revision < 1
    ):
        raise ValueError(
            "expected_revision is a positive current revision for DELETE or PUT"
        )
    if (
        selected == "DELETE"
        and re.fullmatch(
            r"/api/v1/(?:providers|harnesses|mcp-servers|operator-profiles|ssh-environments)/[^/]+",
            candidate,
        )
        and expected_revision is None
    ):
        raise ValueError(
            "read the exact profile and supply expected_revision before deleting"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        timeout=30.0,
    ) as client:
        headers = {"Authorization": f"Bearer {token}"}
        if expected_revision is not None:
            headers["If-Match"] = str(expected_revision)
        response = await _settings_request(
            app,
            client,
            headers,
            project_id,
            selected,
            candidate,
            body,
            expected_revision,
        )
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except (
                ValueError,
                AttributeError,
            ):  # diagnostic-expected: Core may return a non-JSON error.
                detail = response.reason_phrase
            raise ValueError(
                f"Core rejected the Settings change ({response.status_code}): "
                f"{str(_safe_result(detail))[:500]}"
            )
        result = response.json() if response.content else None
        observed = None
        runtime_route = re.fullmatch(
            r"/api/v1/(runner-profiles|ssh-environments)/([^/]+)(/probe)?", candidate
        )
        if runtime_route and (
            selected in {"PUT", "DELETE"} or (selected == "POST" and runtime_route[3])
        ):
            family, identity = runtime_route[1], runtime_route[2]
            readback = await _settings_request(
                app,
                client,
                headers,
                project_id,
                "GET",
                f"/api/v1/{family}",
                before_execution=False,
            )
            if readback.is_success:
                value = readback.json()
                values = (
                    value
                    if family == "runner-profiles"
                    else [
                        host.get("environment")
                        for host in value["hosts"]
                        if host.get("alias") == identity
                    ]
                )
                saved = next(
                    (
                        item
                        for item in values
                        if item
                        and item.get("id")
                        == (
                            identity
                            if family == "runner-profiles"
                            else environment_id(identity)
                        )
                    ),
                    None,
                )
                if selected == "DELETE" and saved is None:
                    observed = {"deleted": True}
                elif selected in {"PUT", "POST"} and saved is not None:
                    observed = saved
        readback_path: str | None = candidate
        if selected == "POST" and isinstance(result, dict) and "id" in result:
            # A collection GET cannot verify the identity returned by a create.
            # Only use Core's new ID when that exact GET route is allowlisted.
            created_id = result["id"]
            readback_path = None
            if (
                isinstance(created_id, str)
                and re.fullmatch(r"[A-Za-z0-9_.~:-]{1,200}", created_id)
                and created_id not in {".", ".."}
            ):
                exact = f"{candidate}/{created_id}"
                if _route_exists(app, "GET", exact):
                    readback_path = exact
        action_readback = _action_readback(selected, candidate)
        if action_readback is not None:
            readback_path = action_readback
        if selected == "POST" and re.fullmatch(
            r"/api/v1/(?:providers/[^/]+/openrouter/credits/refresh|harnesses/[^/]+/quota/refresh)",
            candidate,
        ):
            # These fixed POSTs are authoritative account reads, not profile saves.
            observed = result
        if (
            selected != "GET"
            and readback_path is not None
            and _route_exists(app, "GET", readback_path)
        ):
            readback = await _settings_request(
                app,
                client,
                headers,
                project_id,
                "GET",
                readback_path,
                before_execution=False,
            )
            if readback.is_success:
                value = readback.json()
                if not (
                    selected == "POST" and isinstance(result, dict) and "id" in result
                ) or (isinstance(value, dict) and value.get("id") == result["id"]):
                    observed = value
            elif selected == "DELETE" and readback.status_code == 404:
                observed = {"deleted": True}
        rendered = json.dumps(
            _safe_result(
                result
                if action_readback is not None
                else observed
                if observed is not None
                else result
            ),
            default=str,
        )
        if len(rendered) > 30_000:
            outcome: dict[str, Any] = {
                "status": response.status_code,
                "read_back": observed is not None,
                "truncated": True,
                "result_excerpt": rendered[:30_000],
            }
        else:
            outcome = {
                "status": response.status_code,
                "read_back": observed is not None or selected == "GET",
                "result": json.loads(rendered),
            }
        profile_route = re.fullmatch(
            r"/api/v1/(providers|harnesses|mcp-servers|runner-profiles|ssh-environments|operator-profiles)(?:/[^/]+)?",
            action_readback
            or (candidate.removesuffix("/probe") if runtime_route else candidate),
        )
        if selected != "GET" and observed is not None and profile_route:
            outcome.update(
                state="core_settings_saved",
                client_action={
                    "kind": "settings_profile_refresh",
                    "project_id": project_id,
                    "resource": profile_route[1],
                },
            )
        if (
            selected == "PUT"
            and observed is not None
            and candidate == "/api/v1/butler/check-interval"
        ):
            outcome.update(
                state="core_settings_saved",
                client_action={
                    "kind": "butler_interval_refresh",
                    "project_id": project_id,
                },
            )
        if (
            selected == "PUT"
            and observed is not None
            and candidate == "/api/v1/host-pressure"
        ):
            outcome.update(
                state="core_settings_saved",
                client_action={
                    "kind": "host_pressure_refresh",
                    "project_id": project_id,
                },
            )
        if (
            selected == "PUT"
            and observed is not None
            and candidate == "/api/v1/host-cpu-budget"
        ):
            outcome.update(
                state="core_settings_saved",
                client_action={
                    "kind": "host_cpu_budget_refresh",
                    "project_id": project_id,
                },
            )
        if (
            selected == "PUT"
            and observed is not None
            and candidate.endswith("/post-tool-assistant")
        ):
            outcome.update(
                state="core_settings_saved",
                client_action={"kind": "post_tool_refresh", "project_id": project_id},
            )
        if action_readback is not None:
            observed_text = json.dumps(_safe_result(observed), default=str)
            if len(observed_text) <= 4096:
                outcome["observed_settings"] = json.loads(observed_text)
            else:
                outcome["observed_settings_excerpt"] = observed_text[:4096]
        if selected != "GET" and observed is not None:
            runtime_resource = (
                "web-search"
                if candidate.startswith("/api/v1/integrations/web-search")
                else "setup"
                if candidate.startswith("/api/v1/setup/")
                else "automation-runtime"
                if candidate == "/api/v1/automation/runtime/prepare"
                else None
            )
            if runtime_resource:
                outcome.update(
                    state="core_settings_saved",
                    client_action={
                        "kind": "runtime_settings_refresh",
                        "project_id": project_id,
                        "resource": runtime_resource,
                    },
                    operation_accepted=response.status_code == 202
                    or (isinstance(result, dict) and result.get("accepted") is True),
                )
        return outcome


class SettingsAssistantApi:
    """Core-owned gateway; bearer token and selected project remain server-side."""

    def __init__(self, app: FastAPI, token: str) -> None:
        self.app = app
        self.token = token

    def schema(
        self, *, path: str | None = None, method: str | None = None
    ) -> dict[str, Any]:
        return settings_api_schema(self.app, path=path, method=method)

    async def request(
        self,
        project_id: str,
        *,
        method: str | None,
        path: str | None,
        body: dict[str, Any] | list[Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("provide a Settings method and path")
        return await execute_settings_api(
            self.app,
            self.token,
            project_id,
            method=method,
            path=path,
            body=body,
            expected_revision=expected_revision,
        )
