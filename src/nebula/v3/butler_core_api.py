"""Butler's raw access to Nebula's own HTTP API.

Butler's typed actions cover what someone thought to wrap. In unrestricted
mode (``NEBULA_BUTLER_SUPERVISOR_UNRESTRICTED``) the operator wants Butler to
be able to do anything the operator can do, so this tool sends one request to
Core's public API as the operator: any method, any route, through the same
API worker and authentication the browser uses. ``routes`` lists the routes
this release declares, so Butler can find one without guessing.

The endpoint is ``NEBULA_BUTLER_CORE_API_URL`` when set, otherwise the
endpoint Core published in its private runtime discovery file. The operator
token is read from Core's private token file on each call and never returned.
Every call is audited like the other supervisor tools.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import weakref
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import httpx

from .butler_supervisor_policy import (
    MAX_OUTPUT_BYTES,
    UNRESTRICTED_ENV,
    SupervisorPolicy,
    load_policy,
    write_audit,
)
from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec

URL_ENV = "NEBULA_BUTLER_CORE_API_URL"
TOOL_NAME = "nebula.supervisor.core_api"
API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Headers Butler may not set: Core sets authentication and framing itself.
_RESERVED_HEADERS = frozenset(
    {"authorization", "cookie", "host", "content-length", "transfer-encoding"}
)
_RETURNED_HEADERS = ("location", "etag", "retry-after", "content-type")
_ROUTE = re.compile(
    r"@(?:app|\w*router)\.(get|post|put|patch|delete)\(\s*f?[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": [*_METHODS, "ROUTES"]},
        "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2_000,
            "description": (
                "Route path such as /chat/sessions/{id}/goal/notes/{note_id}; "
                "/api/v1 is added when missing. For ROUTES, a filter substring."
            ),
        },
        "query": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "boolean"]},
        },
        "body": {"description": "JSON request body"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": 2_000},
        },
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT},
        "reason": {"type": "string", "minLength": 3, "maxLength": 500},
    },
    "required": ["method", "reason"],
    "additionalProperties": False,
}


class CoreApiUnavailable(RuntimeError):
    """Core's endpoint or operator token cannot be found."""


def _data_root() -> Path:
    return Path(
        os.environ.get(
            "NEBULA_V3_DATA_DIR", str(Path.home() / ".local/share/nebula/v3")
        )
    ).expanduser()


def _connection(data_root: Path) -> tuple[str, str]:
    from .runtime_launcher import discovery, read_private

    runtime = data_root / "runtime"
    try:
        token = read_private(runtime / "operator-token").decode().strip()
    except (OSError, ValueError) as exc:
        raise CoreApiUnavailable(
            f"Core's operator token is unreadable under {runtime}: {exc}"
        ) from None
    configured = os.environ.get(URL_ENV, "").strip()
    if configured:
        endpoint = configured.rstrip("/")
    else:
        try:
            found = discovery(data_root)
        except (OSError, ValueError) as exc:
            raise CoreApiUnavailable(
                f"Core's discovery file is unreadable: {exc}"
            ) from None
        if found is None:
            raise CoreApiUnavailable(
                f"Core published no endpoint; the operator sets {URL_ENV}"
            )
        endpoint = str(found["endpoint"]).rstrip("/")
    endpoint = endpoint.removesuffix(API_PREFIX)
    return endpoint, token


# The execution kernel this process serves, registered once its routes are
# complete. A frozen release has no .py sources to scan, so its live route
# table is the authoritative list there.
_KERNEL_APP: "weakref.ReferenceType[Any] | None" = None


def register_kernel_routes(app: Any) -> None:
    """Let ROUTES read the route table of the kernel this worker serves."""

    global _KERNEL_APP
    _KERNEL_APP = weakref.ref(app)


def _kernel_routes() -> set[tuple[str, str]]:
    app = _KERNEL_APP() if _KERNEL_APP is not None else None
    if app is None:
        return set()
    found: set[tuple[str, str]] = set()
    for route in getattr(app, "routes", ()):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or ()
        if not isinstance(path, str) or not path.startswith(("/api/", "/health")):
            continue
        for method in methods:
            if method in _METHODS:
                found.add((method, path))
    return found


def declared_routes() -> tuple[tuple[str, str], ...]:
    """(method, path) for every route this worker serves or its source declares."""

    found = set(_source_routes()) | _kernel_routes()
    return tuple(sorted(found, key=lambda item: (item[1], item[0])))


@lru_cache(maxsize=1)
def _source_routes() -> tuple[tuple[str, str], ...]:
    """Routes declared in this release's .py sources, plus the generic CRUD."""

    found: set[tuple[str, str]] = set()
    for source in Path(__file__).parent.glob("*.py"):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:  # diagnostic-expected: an unreadable module adds no routes.
            continue
        for method, raw in _ROUTE.findall(text):
            path = (
                raw.replace("{API_PREFIX}", API_PREFIX)
                .replace("{{", "{")
                .replace("}}", "}")
            )
            if path.startswith("/"):
                found.add((method.upper(), _api_path(path)))
    found |= _crud_routes()
    return tuple(found)


def _crud_routes() -> set[tuple[str, str]]:
    """The generic entity routes the kernel registers from its resource tables."""

    from .execution_kernel import (
        APPEND_ONLY_RESOURCES,
        GENERIC_CRUD_RESOURCES,
        READ_ONLY_RESOURCES,
    )

    routes: set[tuple[str, str]] = set()
    for resource in {*GENERIC_CRUD_RESOURCES, *READ_ONLY_RESOURCES}:
        base = f"{API_PREFIX}/{resource.replace('_', '-')}"
        item = f"{base}/{{entity_id}}"
        routes |= {("GET", base), ("GET", item)}
        if resource in READ_ONLY_RESOURCES:
            continue
        routes.add(("POST", base))
        if resource not in APPEND_ONLY_RESOURCES:
            routes |= {("PUT", item), ("PATCH", item), ("DELETE", item)}
    return routes


def _api_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if path == "/health" or path.startswith(("/api/", "/health/")):
        return path
    return API_PREFIX + path


class ButlerCoreApi:
    """One audited request to Core's public API as the operator."""

    def __init__(
        self,
        *,
        project_id: str,
        actor: str,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
        data_root: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.project_id = project_id
        self.actor = actor
        self._policy_loader = policy_loader
        self._data_root = data_root
        self._transport = transport

    def _policy(self) -> SupervisorPolicy:
        policy = self._policy_loader()
        policy.require_enabled()
        if not policy.unrestricted:
            raise PermissionError(
                f"the Core API tool needs {UNRESTRICTED_ENV}=1 from the operator"
            )
        return policy

    async def request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        method = arguments["method"]
        reason = arguments["reason"]
        if method == "ROUTES":
            needle = (arguments.get("path") or "").casefold()
            routes = [
                {"method": verb, "path": path}
                for verb, path in declared_routes()
                if needle in path.casefold()
            ]
            return {
                "routes": routes[:400],
                "total": len(routes),
                "note": "Routes this worker serves plus its release source; a filter narrows it.",
            }
        if not arguments.get("path"):
            raise ValueError("path is required")
        path = _api_path(arguments["path"])
        endpoint, token = _connection(self._data_root or _data_root())
        headers = {
            key: value
            for key, value in (arguments.get("headers") or {}).items()
            if key.casefold() not in _RESERVED_HEADERS
        }
        if method != "GET" and not any(
            key.casefold() == "idempotency-key" for key in headers
        ):
            headers["Idempotency-Key"] = f"butler-{uuid.uuid4()}"
        headers["Authorization"] = f"Bearer {token}"
        timeout = int(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT)
        started = time.monotonic()
        record: dict[str, Any] = {
            "project_id": self.project_id,
            "actor": self.actor,
            "tool": "core_api",
            "command": f"{method} {path}",
            "reason": reason,
        }
        try:
            async with httpx.AsyncClient(
                base_url=endpoint,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                # Core's own endpoint, read from its private discovery file or
                # the operator's setting; its certificate may be self-signed.
                verify=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    method,
                    path,
                    params=arguments.get("query") or None,
                    json=arguments["body"] if "body" in arguments else None,
                    headers=headers,
                ) as response:
                    data = b""
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_OUTPUT_BYTES:
                            data = data[:MAX_OUTPUT_BYTES]
                            truncated = True
                            break
        except httpx.HTTPError as exc:
            write_audit(policy, {**record, "outcome": "failed", "detail": str(exc)})
            raise
        text = data.decode("utf-8", "replace").replace(token, "[operator-token]")
        write_audit(
            policy,
            {
                **record,
                "outcome": "completed",
                "status": response.status_code,
                "duration_seconds": round(time.monotonic() - started, 3),
                "response_bytes": len(data),
            },
        )
        body: Any = text
        if not truncated and "json" in response.headers.get("content-type", ""):
            try:
                body = json.loads(text)
            except (
                ValueError
            ):  # diagnostic-expected: a malformed JSON body is returned as text.
                body = text
        return {
            "status": response.status_code,
            "ok": response.is_success,
            "method": method,
            "path": path,
            "headers": {
                name: response.headers[name]
                for name in _RETURNED_HEADERS
                if name in response.headers
            }
            | {
                name: value
                for name, value in response.headers.items()
                if name.casefold().startswith("x-nebula-")
            },
            "body": body,
            "truncated": truncated,
        }

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=TOOL_NAME,
                    description=(
                        "Send one request to Nebula Core's own HTTP API as the "
                        "operator: any route the browser can call, in any "
                        "project. method ROUTES lists the declared routes "
                        "(path = filter substring, for example 'goal/notes'). "
                        "Paths without /api/v1 get it. Writes get an "
                        "Idempotency-Key unless you pass one. Returns status, "
                        "selected headers and the JSON body. Audited; state the "
                        "reason. Read the result: a 4xx/5xx is not done."
                    ),
                    input_schema=_SCHEMA,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.request,
            )
        ]
