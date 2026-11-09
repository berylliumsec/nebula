"""Canonical host Settings services, separate from project controllers."""

from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, Query, Response

from .butler_instructions_management import butler_instructions_router
from .diagnostic_requests import BrowserDiagnosticBatch, DiagnosticsSettingsRequest
from .diagnostics import DiagnosticsError, get_diagnostics
from .host_cpu_budget import HostCpuBudget, HostCpuConflict, HostCpuUpdate
from .host_load import HostLoadGate
from .host_pressure import (
    HostPressureConflict,
    HostPressureSettings,
    HostPressureUpdate,
)
from .runtime_host_automation import HostAutomationRuntime
from .memory_policy import (
    MemoryBudgetError,
    MemoryBudgetConflict,
    MemoryEnforcementUnavailable,
)
from .sandbox import SandboxUnavailable
from .runtime_integration_effects import HOST_INTEGRATION_WRITES, HostIntegrationEffects

HOST_OBSERVATION_ROUTES = {
    ("GET", "/api/v1/host-cpu-budget"),
    ("PUT", "/api/v1/host-cpu-budget"),
    ("GET", "/api/v1/host-pressure"),
    ("PUT", "/api/v1/host-pressure"),
    ("GET", "/api/v1/host-load"),
    ("GET", "/api/v1/diagnostics/settings"),
    ("PUT", "/api/v1/diagnostics/settings"),
    ("GET", "/api/v1/diagnostics/files"),
    ("GET", "/api/v1/diagnostics/errors"),
    ("POST", "/api/v1/diagnostics/events"),
    ("GET", "/api/v1/butler/instructions"),
    ("PUT", "/api/v1/butler/instructions"),
    ("GET", "/api/v1/butler/check-interval"),
    ("PUT", "/api/v1/butler/check-interval"),
    ("GET", "/api/v1/automation/runtime"),
    ("POST", "/api/v1/automation/runtime/prepare"),
    ("GET", "/api/v1/vpn-profiles"),
    ("GET", "/api/v1/integrations/typesafe"),
    ("GET", "/api/v1/integrations/web-search"),
}
HOST_OBSERVATION_ROUTES |= HOST_INTEGRATION_WRITES


class HostObservations:
    def __init__(self, settings):
        self.settings, self.host = settings, settings.host
        self.gate = HostLoadGate()
        self.pressure = HostPressureSettings(
            self.host.data_root / "host-pressure.json", self.gate
        )

        async def capacity():
            return (await self.gate.snapshot()).cpu_capacity

        # No affinity-only PDF pool may claim memory-managed execution support.
        self.cpus = HostCpuBudget(
            self.host.data_root / "host-cpu-budget.json", capacity
        )
        self.automation = HostAutomationRuntime(settings.store, self.host.data_root)
        self.search = None
        self.integrations = HostIntegrationEffects(self)
        self.diagnostics = None
        self.allow_browser_diagnostics = False

    async def start(self):
        await self.pressure.startup()
        if self.search is None:
            from .managed_search_runtime import ManagedSearchRuntime
            from .memory_runtime import MemoryComputeController

            self.memory_compute = MemoryComputeController(
                self.host.shared_services.recovery.budgets, self.host.data_root
            )
            self.search = ManagedSearchRuntime(
                store=self.settings.store,
                data_root=self.host.data_root,
                memory_compute=self.memory_compute,
            )
            self.automation.memory_compute = self.memory_compute

    async def close(self):
        close = getattr(self.search, "close", None)
        if callable(close):
            await close()
            return
        task = getattr(self.search, "_idle_task", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def manager(self):
        manager = self.diagnostics or get_diagnostics()
        if manager is None:
            raise HTTPException(503, "Host diagnostics are not initialized")
        return manager

    async def concurrency(self):
        observed = await self.host.shared_services.capacity.status()
        limits, used = observed["limits"], observed["used"]
        return {
            "provider_limit": limits.get("provider", 0),
            "background_limit": limits.get("background", 0),
            "active": used.get("provider", 0),
            "active_background": used.get("background", 0),
            "capacity": observed,
        }

    def mount(self, app, auth):
        def cpu_status(status):
            return status.model_copy(
                update={
                    "activation_unavailable_reason": "Background PDF execution requires a memory-managed CPU runner; this Core has no supported PDF executor."
                }
            )

        dependencies = [Depends(auth)]
        self.integrations.mount(app, auth)

        @app.exception_handler(SandboxUnavailable)
        async def sandbox_unavailable(_request, error):
            # diagnostic-expected: unsupported image enforcement is a recorded
            # capability refusal; the memory ledger retains its blocked job.
            from starlette.responses import JSONResponse

            return JSONResponse({"detail": str(error)}, status_code=503)

        @app.exception_handler(MemoryBudgetError)
        async def memory_unavailable(_request, error):
            # diagnostic-expected: expose budget/admission refusals truthfully.
            from starlette.responses import JSONResponse

            return JSONResponse(
                {"detail": str(error)},
                status_code=409
                if isinstance(error, MemoryBudgetConflict)
                else 503
                if isinstance(error, MemoryEnforcementUnavailable)
                else 422,
            )

        app.include_router(
            butler_instructions_router(self.host.data_root, self.settings.store),
            prefix="/api/v1",
            dependencies=dependencies,
        )

        @app.get("/api/v1/host-cpu-budget", dependencies=dependencies)
        async def cpu(response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            return cpu_status(await self.cpus.status())

        @app.put("/api/v1/host-cpu-budget", dependencies=dependencies)
        async def save_cpu(body: HostCpuUpdate):
            try:
                return cpu_status(await self.cpus.update(body))
            except HostCpuConflict as error:
                raise HTTPException(409, str(error)) from error

        @app.get("/api/v1/host-pressure", dependencies=dependencies)
        async def pressure(response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            value = await self.pressure.status()
            value["concurrency"] = await self.concurrency()
            return value

        @app.put("/api/v1/host-pressure", dependencies=dependencies)
        async def save_pressure(body: HostPressureUpdate, response: Response):
            try:
                await self.pressure.update(body)
            except HostPressureConflict as error:
                raise HTTPException(409, str(error)) from error
            return await pressure(response)

        @app.get("/api/v1/host-load", dependencies=dependencies)
        async def load():
            return {
                **(await self.gate.snapshot()).public(),
                "concurrency": await self.concurrency(),
            }

        @app.exception_handler(DiagnosticsError)
        async def diagnostics_error(_request, error):
            from starlette.responses import JSONResponse

            return JSONResponse({"detail": str(error)}, status_code=503)

        @app.get("/api/v1/diagnostics/settings", dependencies=dependencies)
        async def diagnostic_settings():
            return self.manager().settings.as_dict()

        @app.put("/api/v1/diagnostics/settings", dependencies=dependencies)
        async def save_diagnostic_settings(body: DiagnosticsSettingsRequest):
            return (
                await asyncio.to_thread(
                    self.manager().update_settings,
                    body.model_dump(mode="json", by_alias=True),
                )
            ).as_dict()

        @app.get("/api/v1/diagnostics/files", dependencies=dependencies)
        async def files():
            manager = self.manager()
            return await asyncio.to_thread(
                lambda: {"files": manager.list_files(), "health": manager.status()}
            )

        @app.get("/api/v1/diagnostics/errors", dependencies=dependencies)
        async def errors(
            feature: str | None = Query(None, min_length=1, max_length=64),
            after: str | None = Query(None, min_length=1, max_length=64),
            limit: int = Query(100, ge=1, le=500),
        ):
            return {
                "errors": await asyncio.to_thread(
                    self.manager().recent_errors,
                    feature=feature,
                    after=after,
                    limit=limit,
                )
            }

        @app.post("/api/v1/diagnostics/events", dependencies=dependencies)
        async def browser_events(body: BrowserDiagnosticBatch):
            if not self.allow_browser_diagnostics:
                raise HTTPException(
                    403,
                    "browser diagnostic ingress is disabled outside development mode",
                )
            manager, identities = self.manager(), []
            for event in body.events:
                fields = event.model_dump(
                    exclude={
                        "schema_",
                        "level",
                        "feature",
                        "event_code",
                        "message",
                        "sensitive_detail_available",
                        "sensitive_detail_expires_at",
                    }
                )
                identity = manager.record(
                    event.level,
                    event.feature,
                    event.event_code,
                    event.message,
                    source="browser",
                    **fields,
                )
                if identity:
                    identities.append(identity)
            return {"accepted": len(body.events), "error_ids": identities}

        @app.get("/api/v1/automation/runtime", dependencies=dependencies)
        async def automation(
            engagement_id: str | None = Query(None, min_length=1, max_length=200),
        ):
            return await self.automation.runtime_info(engagement_id=engagement_id)

        @app.post("/api/v1/automation/runtime/prepare", dependencies=dependencies)
        async def prepare():
            return await self.automation.prepare()

        @app.get("/api/v1/vpn-profiles", dependencies=dependencies)
        async def vpns(
            response: Response,
            offset: int = Query(0, ge=0),
            limit: int = Query(1000, ge=1, le=1000),
        ):
            from .integration_settings import public_vpn_profiles

            rows, next_offset = await public_vpn_profiles(
                self.settings.store,
                self.settings.credentials,
                offset=offset,
                limit=limit,
            )
            if next_offset is not None:
                response.headers["X-Nebula-Next-Offset"] = str(next_offset)
                response.headers["X-Nebula-Partial"] = "true"
            return rows

        @app.get("/api/v1/integrations/typesafe", dependencies=dependencies)
        async def typesafe():
            from .integration_settings import typesafe_integration_status

            return await typesafe_integration_status(
                self.settings.store, self.settings.credentials
            )

        @app.get("/api/v1/integrations/web-search", dependencies=dependencies)
        async def search():
            return await self.integrations.search_status()
