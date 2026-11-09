"""One authenticated host Settings API, also used by bound project Butler peers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
import re
from typing import Literal

from fastapi import FastAPI
from pydantic import Field, ValidationError
from starlette.responses import JSONResponse

from .authentication import CoreAuthentication
from .async_utils import finish_cleanup
from .domain import NebulaModel
from .execution_scope import current_execution_lease
from .memory_actor import memory_actor
from .memory_api import MemoryApi
from .memory_budget import MemoryBudgetService
from .memory_policy import (
    MemoryBudgetConflict,
    MemoryBudgetError,
    MemoryEnforcementUnavailable,
)
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .settings_assistant_api import execute_settings_api
from .shared_memory_rpc import MemoryAuthority
from .storage import NotFoundError

MEMORY_CONTROL = "host.memory-control/v1"
_authority: ContextVar[ProjectLease | None] = ContextVar(
    "host_memory_authority", default=None
)
_READ = re.compile(r"/api/v1/memory-budgets(?:/jobs(?:/[A-Za-z0-9_.:-]{1,200})?)?")
_PROBE = re.compile(r"/api/v1/memory-budgets/runners/[A-Za-z0-9_.:-]{1,200}/probe")


class MemoryControlBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 128 * 1024:
                await JSONResponse(
                    {"detail": "Memory Settings request exceeds 128 KiB"},
                    status_code=413,
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        supplied = False

        async def replay():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class _DelegatedBudgets(MemoryBudgetService):
    def __init__(self, coordinator, base):
        super().__init__(
            base.database,
            administrator=base.administrator,
            runtime_defaults_bytes=base.runtime_defaults_bytes,
        )
        self.coordinator, self.base = coordinator, base

    @asynccontextmanager
    async def _write(self):
        lease = _authority.get()
        if lease is None:
            async with self.base._write() as session:
                yield session
        else:
            # Validation, policy/job read or mutation and audit share the exact
            # peer's ownership transaction. A preflight alone cannot authorize it.
            async with self.coordinator.coordination.fenced(lease) as session:
                yield session


class MemorySettingsRequest(NebulaModel):
    lease: MemoryAuthority
    method: Literal["GET", "PUT", "POST"]
    path: str = Field(min_length=1, max_length=600)
    body: dict | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class HostMemoryControl:
    def __init__(self, host, recovery, token):
        self.host, self.store, self.token = host, recovery.store, token
        self.service = _DelegatedBudgets(host.coordinator, recovery.budgets)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_middleware(MemoryControlBodyLimit)
        self.app.state.store = self.store
        auth = CoreAuthentication(self.store, token)
        self.api = MemoryApi(
            self.app,
            self.store,
            self.service,
            auth.require_auth,
            runner_factory=recovery.runner_for_profile,
            register_running_apply=False,
        )

        @self.app.exception_handler(MemoryBudgetError)
        async def invalid(_request, error):
            status = (
                409
                if isinstance(error, MemoryBudgetConflict)
                else 503
                if isinstance(error, MemoryEnforcementUnavailable)
                else 422
            )
            return JSONResponse({"detail": str(error)}, status_code=status)

        @self.app.exception_handler(CoordinationConflict)
        async def conflict(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=409)

        @self.app.exception_handler(NotFoundError)
        async def missing(_request, _error):
            return JSONResponse(
                {"detail": "Memory resource is unavailable"}, status_code=404
            )

    def register(self, rpc, runtime_id):
        async def dispatch(payload):
            authority_token = actor_token = None
            try:
                request = MemorySettingsRequest.model_validate(payload)
                lease = ProjectLease(**request.lease.model_dump())
                if lease.runtime_id != runtime_id:
                    raise LeaseLost("Memory Settings request belongs to another worker")
                permitted = (
                    request.method == "GET"
                    and _READ.fullmatch(request.path)
                    or request.method == "PUT"
                    and request.path == "/api/v1/memory-budgets"
                    or request.method == "POST"
                    and _PROBE.fullmatch(request.path)
                )
                if not permitted:
                    raise ValueError(
                        "Choose an advertised host memory Settings operation"
                    )
                async with self.host.coordinator.coordination.fenced(lease):
                    pass
                authority_token = _authority.set(lease)
                actor_token = memory_actor.set("butler:" + lease.project_id)
                value = await execute_settings_api(
                    self.app,
                    self.token,
                    lease.project_id,
                    method=request.method,
                    path=request.path,
                    body=request.body,
                    expected_revision=request.expected_revision,
                )
                return {"ok": True, "value": value}
            except ValidationError:
                return {
                    "ok": False,
                    "error": "invalid",
                    "detail": "Memory Settings arguments exceed the supported bounds",
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
                    _authority.reset(authority_token)

        async def invoke(payload):
            return await finish_cleanup(dispatch(payload))

        rpc.register(MEMORY_CONTROL, invoke)


class RemoteMemorySettings:
    def __init__(self, client):
        self.client = client

    async def request(self, project_id, **arguments):
        lease = current_execution_lease()
        if lease is None or lease.project_id != project_id:
            raise LeaseLost("Memory Settings requires the bound project task")
        result = await self.client.call(
            MEMORY_CONTROL, {"lease": asdict(lease), **arguments}
        )
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise RuntimeIpcError("Invalid host memory Settings response")
        if result["ok"] and set(result) == {"ok", "value"}:
            return result["value"]
        if (
            set(result) != {"ok", "error", "detail"}
            or result["error"] not in {"lease_lost", "invalid"}
            or not isinstance(result["detail"], str)
            or len(result["detail"]) > 1000
        ):
            raise RuntimeIpcError("Invalid host memory Settings rejection")
        if result["error"] == "lease_lost":
            raise LeaseLost(result["detail"])
        raise ValueError(result["detail"])
