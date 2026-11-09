"""Exact-grant Settings assistant dispatch through canonical host services."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import re
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI
from pydantic import Field, ValidationError
from starlette.requests import Request

from .async_utils import finish_cleanup
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .domain import NebulaModel
from .execution_scope import current_execution_lease
from .memory_actor import memory_actor
from .memory_runtime import compute_context
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_delivery import HttpIntent
from .runtime_global_api import RuntimeGlobalApi
from .runtime_global_delivery import GlobalSqlDelivery
from .runtime_ipc import RuntimeIpcError
from .settings_assistant_api import _operation_is_allowed, execute_settings_api
from .shared_memory_rpc import MemoryAuthority
from .tools import GuidedToolError

HOST_SETTINGS = "host.settings/v1"


class HostSettingsRequest(NebulaModel):
    lease: MemoryAuthority
    operation_id: str = Field(min_length=1, max_length=200, pattern=r"^[!-~]+$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=600)
    body: dict | list | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class SettingsGuidance(NebulaModel):
    code: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=200)
    problem: str = Field(min_length=1, max_length=2000)
    next_action: str = Field(min_length=1, max_length=4000)
    field: str | None = Field(default=None, max_length=200)
    before_execution: bool = False


class HostSettingsBridge:
    def __init__(self, settings):
        self.settings = settings
        self.coordination = settings.host.coordinator.coordination
        self.global_api = RuntimeGlobalApi(settings.store.database)
        self.schema = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.schema.router.routes = list(settings.app.routes) + list(
            self.global_api.app.routes
        )
        self.schema.state.store = self.global_api.store

    def routes(self):
        return [
            {"method": method, "path": path}
            for method, path in sorted(
                {
                    (method, route.path)
                    for route in self.schema.routes
                    for method in getattr(route, "methods", ())
                    if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                    and _operation_is_allowed(method, route.path)
                }
            )
        ]

    def register(self, rpc, runtime_id):
        async def dispatch(payload):
            authority_token = actor_token = None
            try:
                request = HostSettingsRequest.model_validate(payload)
                lease = ProjectLease(**request.lease.model_dump())
                if (
                    lease.runtime_id != runtime_id
                    or lease.project_id == GLOBAL_EXECUTION_PROJECT
                ):
                    raise LeaseLost(
                        "Host Settings requires this worker's exact project grant"
                    )
                async with self.coordination.fenced(lease):
                    pass
                token = self.settings.host.operator_token
                if not token:
                    raise ValueError(
                        "Host Settings requires normal Core authentication"
                    )
                authority_token = self.settings.delegated_authority.set(lease)
                actor_token = memory_actor.set("butler:" + lease.project_id)
                transport = _HostSettingsTransport(self, lease, request.operation_id)
                result = await execute_settings_api(
                    self.schema,
                    token,
                    lease.project_id,
                    method=request.method,
                    path=request.path,
                    body=request.body,
                    expected_revision=request.expected_revision,
                    transport_app=transport,
                )
                async with self.coordination.fenced(lease):
                    pass
                if transport.pending is not None:
                    result.update(
                        state="accepted",
                        pending=True,
                        read_back=False,
                        operation_accepted=True,
                        command_id=transport.pending,
                    )
                    result.pop("client_action", None)
                return {"ok": True, "value": result}
            except ValidationError:
                return {
                    "ok": False,
                    "error": "invalid",
                    "detail": "Host Settings arguments exceed the supported bounds",
                }
            except GuidedToolError as error:
                # diagnostic-expected: preserve Core-authored policy recovery
                # and known-before-effect refusal semantics across this hop.
                guidance = SettingsGuidance(
                    code=error.code,
                    category=error.category,
                    problem=error.problem,
                    next_action=error.next_action,
                    field=error.field,
                    before_execution=getattr(error, "_nebula_before_execution", False),
                )
                return {
                    "ok": False,
                    "error": "guided",
                    "detail": str(error)[:1000],
                    "guidance": guidance.model_dump(mode="json"),
                }
            except (CoordinationConflict, ValueError) as error:
                return {
                    "ok": False,
                    "error": "lease_lost"
                    if isinstance(error, LeaseLost)
                    else "invalid",
                    "detail": str(error)[:1000],
                }
            finally:
                if actor_token is not None:
                    memory_actor.reset(actor_token)
                if authority_token is not None:
                    self.settings.delegated_authority.reset(authority_token)

        async def invoke(payload):
            return await finish_cleanup(dispatch(payload))

        rpc.register(HOST_SETTINGS, invoke)

    async def close(self):
        await self.global_api.close()


class _HostSettingsTransport:
    def __init__(self, bridge, lease, operation_id):
        self.bridge, self.lease, self.pending = bridge, lease, None
        self.key = (
            "butler-host:"
            + hashlib.sha256(
                (lease.project_id + "\0" + operation_id).encode()
            ).hexdigest()
        )

    async def __call__(self, scope, receive, send):
        from .runtime_host_settings import _ReadIntent, is_host_settings_route

        request = Request(scope, receive)
        coordinator = self.bridge.coordination
        async with coordinator.fenced(self.lease):
            pass
        host = is_host_settings_route(request.method, request.url.path)
        if not host and not self.bridge.global_api.supports(scope):
            raise ValueError("This Settings route is unavailable on the canonical host")
        settings = self.bridge.settings
        if request.method == "GET":
            intent = _ReadIntent.capture(
                request.url.path, scope.get("query_string", b""), request.headers.raw
            )
            capture = (
                settings.effect
                if host
                else GlobalSqlDelivery(settings.effect.delivery, self.bridge.global_api)
            )
            status, headers, body = await capture._response(intent, "operator")
            from starlette.responses import Response

            response = Response(body, status_code=status)
            response.raw_headers = [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in headers
                if name.lower()
                not in {"content-length", "transfer-encoding", "connection"}
            ]
        else:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 128 * 1024:
                    raise ValueError("Host Settings request exceeds 128 KiB")
                body.extend(chunk)
            intent = HttpIntent.capture(
                request.method,
                request.url.path,
                scope.get("query_string", b""),
                request.headers.raw,
                bytes(body),
            )
            if host:
                receipt = await settings.effect.execute(
                    intent,
                    actor_id="operator",
                    idempotency_key=self.key,
                    authority=self.lease,
                )
            else:
                receipt = await GlobalSqlDelivery(
                    settings.effect.delivery, self.bridge.global_api
                ).execute(
                    intent,
                    actor_id="operator",
                    idempotency_key=self.key,
                    authority=self.lease,
                )
            response = await settings.effect.response(
                "operator", receipt.command_id, receipt=receipt, wait_seconds=15
            )
            if response.status_code == 202:
                self.pending = receipt.command_id
        await response(scope, receive, send)


class RemoteHostSettings:
    def __init__(self, client, routes):
        self.client = client
        if not isinstance(routes, list) or len(routes) > 256:
            raise RuntimeIpcError("Invalid host Settings route catalog")
        self.routes = []
        for route in routes:
            if not isinstance(route, dict) or set(route) != {"method", "path"}:
                raise RuntimeIpcError("Invalid host Settings route catalog")
            method, path = route["method"], route["path"]
            if (
                method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                or not isinstance(path, str)
                or len(path) > 600
                or not _operation_is_allowed(method, path)
            ):
                raise RuntimeIpcError("Invalid host Settings route catalog")
            pattern = "/".join(
                "[^/]+"
                if part.startswith("{") and part.endswith("}")
                else re.escape(part)
                for part in path.split("/")
            )
            self.routes.append((method, re.compile(pattern)))

    def supports(self, method, path):
        return any(
            method == verb and pattern.fullmatch(path) is not None
            for verb, pattern in self.routes
        )

    async def request(self, project_id, **arguments):
        lease = current_execution_lease()
        if lease is None or lease.project_id != project_id:
            raise LeaseLost("Host Settings requires the bound project task")
        context = compute_context.get()
        operation = (
            context.resource_id
            if context is not None
            and context.project_id == project_id
            and context.resource_kind == "tool"
            else str(uuid4())
        )
        result = await self.client.call(
            HOST_SETTINGS,
            {"lease": asdict(lease), "operation_id": operation, **arguments},
            timeout=180,
        )
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise RuntimeIpcError("Invalid host Settings response")
        if (
            result["ok"]
            and set(result) == {"ok", "value"}
            and isinstance(result["value"], dict)
        ):
            return result["value"]
        if (
            set(result) == {"ok", "error", "detail", "guidance"}
            and result["error"] == "guided"
            and isinstance(result["detail"], str)
            and len(result["detail"]) <= 1000
        ):
            try:
                guidance = SettingsGuidance.model_validate(result["guidance"])
            except ValidationError as error:
                raise RuntimeIpcError("Invalid host Settings guidance") from error
            raise GuidedToolError(result["detail"], **guidance.model_dump())
        if (
            set(result) != {"ok", "error", "detail"}
            or result["error"] not in {"invalid", "lease_lost"}
            or not isinstance(result["detail"], str)
            or len(result["detail"]) > 1000
        ):
            raise RuntimeIpcError("Invalid host Settings rejection")
        if result["error"] == "lease_lost":
            raise LeaseLost(result["detail"])
        raise ValueError(result["detail"])
