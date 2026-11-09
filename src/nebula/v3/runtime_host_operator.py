"""Host inspection and folder controls without project execution authority."""

from __future__ import annotations

import asyncio
import re

from fastapi import Depends, HTTPException, Query

from .domain import HarnessConnectionMode, HarnessKind, RunnerProfile, utc_now
from .host_workspace_folders import (
    HostWorkspaceFolderCreateRequest,
    create_host_workspace_folder,
    list_host_workspace_folders,
)
from .memory_policy import MemoryEnforcementUnavailable
from .runner_profiles import container_runner_profile
from .runtime_usage import HarnessQuotaRead
from .sandbox import ContainerSandboxRunner
from .setup import (
    RunnerSelectionRequest,
    SetupControlResponse,
    SetupService,
    SetupServiceError,
    SetupStatus,
)

HOST_OPERATOR_ROUTES = {
    ("GET", "/api/v1/setup/status"),
    ("POST", "/api/v1/setup/runtime/refresh"),
    ("POST", "/api/v1/setup/runtime/select"),
    ("GET", "/api/v1/workspace-folders"),
    ("POST", "/api/v1/workspace-folders"),
    ("POST", "/api/v1/harnesses/{profile_id}/quota/refresh"),
}
_QUOTA = re.compile(r"/api/v1/harnesses/[^/]{1,200}/quota/refresh")


def is_host_operator_route(method, path):
    return (method, path) in HOST_OPERATOR_ROUTES or (
        method == "POST" and _QUOTA.fullmatch(path) is not None
    )


class _RunnerInspection:
    """Setup's inspection contract deliberately has no image/workspace methods."""

    execution_enabled = True

    def __init__(self, owner):
        self.owner = owner

    async def verify_runner(self, profile_id):
        self.owner.ensure_active()
        settings = self.owner.settings
        profile = await settings.store.get(RunnerProfile, profile_id)
        runner = (
            settings.runner_factory(profile)
            if settings.runner_factory
            else ContainerSandboxRunner(profile=container_runner_profile(profile))
        )
        healthy, detail = await runner.available()
        self.owner.ensure_active()
        return await settings.store.update(
            RunnerProfile,
            profile.id,
            {
                "healthy": healthy,
                "last_health_at": utc_now(),
                "last_health_detail": detail,
            },
            expected_revision=profile.revision,
        )


class HostOperator:
    def __init__(self, settings):
        self.settings = settings
        self.closed = False
        self.setup = SetupService(settings.store, _RunnerInspection(self))

    def ensure_active(self):
        host = self.settings.host
        if self.closed or host.closed or host.lock.descriptor is None:
            raise HTTPException(503, "Host operator services are reconnecting")

    async def start(self):
        self.ensure_active()
        await self.setup.start()

    async def close(self):
        self.closed = True
        await self.setup.shutdown()

    async def read_harness_account(self, profile, credentials):
        """Never use a native process account reader without verified admission."""
        from .harnesses import HarnessRuntimeService

        self.ensure_active()
        adapter = (
            self.settings.harness_factory(profile.kind)
            if self.settings.harness_factory
            else HarnessRuntimeService._default_adapter(profile.kind)
        )
        if (
            profile.kind == HarnessKind.CODEX_APP_SERVER
            and profile.connection_mode == HarnessConnectionMode.ENDPOINT
        ):
            # Endpoint connections inspect an existing service, without a child
            # process, a project workspace, or a model turn.
            async with asyncio.timeout(45):
                return await adapter.read_account_usage(profile, credentials)
        controller = getattr(self.settings.observations, "memory_compute", None)
        if controller is None:
            raise MemoryEnforcementUnavailable(
                "Host memory enforcement is not initialized; retry account refresh after Core reconnects."
            )
        reader = getattr(adapter, "read_account_usage_memory_managed", None)
        if not callable(reader):
            await controller.reject_unsupported(
                "local",
                project_id=None,
                resource_kind="harness_account",
                resource_id=profile.id,
                detail="This harness account reader has no protected memory-runner/v1 launch contract. Connect a supported account endpoint or memory-managed harness reader, then retry. No account process was started.",
            )
        async with asyncio.timeout(45):
            return await reader(profile, credentials, controller)

    def mount(self, app, auth):
        async def authorized():
            actor = await auth()
            self.ensure_active()
            return actor

        dependencies = [Depends(authorized)]

        @app.exception_handler(SetupServiceError)
        async def setup_error(_request, error):
            from starlette.responses import JSONResponse

            return JSONResponse(
                {"detail": {"code": error.code, "message": str(error)}},
                status_code=error.status_code,
            )

        @app.get(
            "/api/v1/setup/status",
            response_model=SetupStatus,
            dependencies=dependencies,
        )
        async def status():
            return await self.setup.status()

        @app.post(
            "/api/v1/setup/runtime/refresh",
            response_model=SetupStatus,
            dependencies=dependencies,
        )
        async def refresh():
            return await self.setup.refresh()

        @app.post(
            "/api/v1/setup/runtime/select",
            response_model=SetupControlResponse,
            dependencies=dependencies,
        )
        async def select(body: RunnerSelectionRequest):
            return await self.setup.select_runner(body)

        @app.get("/api/v1/workspace-folders", dependencies=dependencies)
        async def folders(
            path: str | None = Query(None, max_length=4096),
            offset: int = Query(0, ge=0),
            limit: int = Query(500, ge=1, le=500),
            filter: str = Query("", max_length=255),
            include_sqlite_files: bool = False,
        ):
            return await list_host_workspace_folders(
                path,
                offset=offset,
                limit=limit,
                filter=filter,
                include_sqlite_files=include_sqlite_files,
            )

        @app.post(
            "/api/v1/workspace-folders", status_code=201, dependencies=dependencies
        )
        async def create_folder(body: HostWorkspaceFolderCreateRequest):
            return await create_host_workspace_folder(body)

        @app.post(
            "/api/v1/harnesses/{profile_id}/quota/refresh",
            response_model=HarnessQuotaRead,
            dependencies=dependencies,
        )
        async def quota(profile_id: str):
            # Only each fenced project owner may retain its Butler observations.
            observations = self.settings.observations
            async with observations._usage_lock:
                return await observations.runtime_usage().read_harness_quota(
                    profile_id, refresh=True
                )
