"""Authenticated, project-bound access to Core Settings operations for the control assistant."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError
from starlette.routing import Match

from .domain import HarnessProfile, McpServerProfile, ProviderProfile


_PROJECT_SETTING = re.compile(
    r"^/api/v1/engagements/\{project\}/(?:"
    r"scope(?:/tool-candidates)?|automation-policy|post-tool-assistant|"
    r"assistant-defaults|browser-security-approval|agent-security/(?:start|stop|status)"
    r")$"
)
_GLOBAL_SETTINGS = re.compile(
    r"^/api/v1/(?:"
    r"providers|harnesses|mcp-servers|ssh-environments|"
    r"runner-profiles|vpn-profiles|operator-profiles|credentials/(?:vault|[A-Za-z0-9_.~:-]+/status)|skills|"
    r"integrations|automation/runtime|diagnostics/settings|voice/settings|butler/(?:instructions|check-interval)|"
    r"setup|release"
    r")(?:/[A-Za-z0-9_.~%:-]+)*$"
)
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PROFILE_MODELS: dict[str, type[BaseModel]] = {
    "providers": ProviderProfile,
    "harnesses": HarnessProfile,
    "mcp-servers": McpServerProfile,
}
_PROFILE_PRIVATE_FIELDS = {
    "providers": ("secret_ref", "openrouter_management_secret_ref"),
    "harnesses": ("auth_mode", "secret_ref"),
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
            r"/api/v1/(?:providers|harnesses|mcp-servers|operator-profiles)/[^/]+",
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
        await _check_profile_mutation(
            client, headers, selected, candidate, body, expected_revision
        )
        response = await client.request(selected, candidate, json=body, headers=headers)
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
        if (
            selected != "GET"
            and readback_path is not None
            and _route_exists(app, "GET", readback_path)
        ):
            readback = await client.get(readback_path, headers=headers)
            if readback.is_success:
                value = readback.json()
                if not (
                    selected == "POST" and isinstance(result, dict) and "id" in result
                ) or (isinstance(value, dict) and value.get("id") == result["id"]):
                    observed = value
            elif selected == "DELETE" and readback.status_code == 404:
                observed = {"deleted": True}
        rendered = json.dumps(
            _safe_result(observed if observed is not None else result), default=str
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
            r"/api/v1/(providers|harnesses|mcp-servers)(?:/[^/]+)?", candidate
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
