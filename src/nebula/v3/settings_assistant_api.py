"""Authenticated, project-bound access to Core Settings operations for the control assistant."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI
from starlette.routing import Match


_PROJECT_SETTING = re.compile(
    r"^/api/v1/engagements/\{project\}/(?:"
    r"scope(?:/tool-candidates)?|automation-policy|post-tool-assistant|"
    r"assistant-defaults|browser-security-approval|agent-security/(?:start|stop|status)"
    r")$"
)
_GLOBAL_SETTINGS = re.compile(
    r"^/api/v1/(?:"
    r"providers|harnesses|mcp-servers|ssh-environments|"
    r"runner-profiles|vpn-profiles|operator-profiles|credentials|skills|"
    r"integrations|automation/runtime|diagnostics/settings|voice/settings|"
    r"auth/(?:pairings|devices)|setup|release"
    r")(?:/[A-Za-z0-9_.~%:-]+)*$"
)
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
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
        and _template_is_allowed(getattr(route, "path", ""))
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
                if verb.upper() in _METHODS:
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
    if not _template_is_allowed(template):
        raise ValueError("this path is outside the assistant's Settings API")
    operation = paths.get(template, {}).get(selected.lower())
    if operation is None:
        # A concrete resource id matches one OpenAPI path template.
        for option, entries in paths.items():
            if not _template_is_allowed(option):
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
) -> dict[str, Any]:
    """Call only an existing Settings route, with the active project supplied by Core."""

    selected = method.upper()
    if selected not in _METHODS:
        raise ValueError("choose GET, POST, PUT, PATCH, or DELETE")
    candidate = _clean_path(path)
    if "/engagements/" in candidate:
        if not re.fullmatch(r"[A-Za-z0-9_.~:-]+", project_id):
            raise ValueError("selected project has an invalid identifier")
        candidate = candidate.replace("{project}", project_id)
    if not _route_exists(app, selected, candidate):
        raise ValueError("this Settings operation is not in the current Core build")
    if selected == "GET" and body is not None:
        raise ValueError("GET Settings requests have no body")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        timeout=30.0,
    ) as client:
        headers = {"Authorization": f"Bearer {token}"}
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
        if selected != "GET" and _route_exists(app, "GET", candidate):
            readback = await client.get(candidate, headers=headers)
            if readback.is_success:
                observed = readback.json()
            elif selected == "DELETE" and readback.status_code == 404:
                observed = {"deleted": True}
        rendered = json.dumps(
            _safe_result(observed if observed is not None else result), default=str
        )
        if len(rendered) > 30_000:
            return {
                "status": response.status_code,
                "read_back": observed is not None,
                "truncated": True,
                "result_excerpt": rendered[:30_000],
            }
        return {
            "status": response.status_code,
            "read_back": observed is not None or selected == "GET",
            "result": json.loads(rendered),
        }


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
    ) -> dict[str, Any]:
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("provide a Settings method and path")
        return await execute_settings_api(
            self.app, self.token, project_id, method=method, path=path, body=body
        )
