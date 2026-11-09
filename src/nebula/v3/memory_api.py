"""Authenticated memory settings shared by the browser and Butler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from .domain import NebulaModel, RunnerProfile
from .memory_actor import memory_actor
from .memory_budget import MemoryBudgetService, MemoryBudgetStatus, MemoryJob
from .memory_policy import (
    ExecutionEnvironment,
    MemoryBudgetConflict,
    MemoryBudgetError,
    MemoryBudgetUpdate,
    MemoryEnforcementUnavailable,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
)
from .memory_runtime import probe_memory_runner
from .storage import NebulaStore


class MemoryRunnerChoice(NebulaModel):
    id: str
    name: str
    enabled: bool
    environment: ExecutionEnvironment = "container"
    capabilities: MemoryRunnerCapabilities | None = None


class MemoryBudgetSettings(MemoryBudgetStatus):
    profiles: list[MemoryRunnerChoice]


class MemoryJobList(NebulaModel):
    items: list[MemoryJob]
    display_limit: int
    at_limit: bool


class MemoryApi:
    def __init__(
        self,
        app: FastAPI,
        store: NebulaStore,
        service: MemoryBudgetService,
        require_auth,
        *,
        runner_factory: Callable[[RunnerProfile], Any] | None = None,
        apply_running: Callable[[str, MemoryRunningUpdate, str], Awaitable[MemoryJob]]
        | None = None,
    ):
        self.store, self.service, self.runner_factory, self.apply_running = (
            store,
            service,
            runner_factory,
            apply_running,
        )
        self.capabilities: dict[str, MemoryRunnerCapabilities] = {}
        self.runners: dict[str, Any] = {}
        prefix = "/api/v1/memory-budgets"
        dependencies = [Depends(require_auth)]

        @app.get(
            prefix,
            response_model=MemoryBudgetSettings,
            tags=["system"],
            dependencies=dependencies,
        )
        async def read(response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            profiles = await self._profiles()
            result = await self.service.status(self._capabilities())
            return {**result.model_dump(mode="json"), "profiles": profiles}

        @app.put(
            prefix,
            response_model=MemoryBudgetSettings,
            tags=["system"],
            dependencies=dependencies,
        )
        async def save(body: MemoryBudgetUpdate, request: Request, response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            profiles = await self._profiles()
            try:
                result = await self.service.save(
                    body,
                    actor_id=self._actor(request),
                    permitted_profiles=[item.id for item in profiles if item.enabled],
                    capabilities=self._capabilities(),
                )
                return {**result.model_dump(mode="json"), "profiles": profiles}
            except MemoryBudgetConflict as error:
                # diagnostic-expected: revision conflicts require explicit readback.
                raise HTTPException(409, str(error)) from error
            except MemoryBudgetError as error:
                # diagnostic-expected: invalid limits never reach a runner.
                raise HTTPException(422, str(error)) from error

        @app.get(
            prefix + "/jobs",
            response_model=MemoryJobList,
            tags=["system"],
            dependencies=dependencies,
        )
        async def jobs(
            response: Response, limit: int = Query(default=100, ge=1, le=256)
        ):
            response.headers["Cache-Control"] = "private, no-store"
            items = await self.service.list_jobs(limit=limit)
            return {
                "items": items,
                "display_limit": limit,
                "at_limit": len(items) == limit,
            }

        @app.get(
            prefix + "/jobs/{job_id}",
            response_model=MemoryJob,
            tags=["system"],
            dependencies=dependencies,
        )
        async def job(job_id: str, response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            try:
                return await self.service.get_job(job_id)
            except MemoryBudgetError as error:
                # diagnostic-expected: a deleted or unknown job has no application proof.
                raise HTTPException(404, str(error)) from error

        @app.post(
            prefix + "/runners/{profile_id}/probe",
            response_model=MemoryRunnerCapabilities,
            tags=["system"],
            dependencies=dependencies,
        )
        async def probe(profile_id: str, response: Response):
            response.headers["Cache-Control"] = "private, no-store"
            profiles = await self._profiles()
            if not any(item.id == profile_id and item.enabled for item in profiles):
                raise HTTPException(
                    404, "Choose an enabled execution profile from the list"
                )
            runner = self.runners.get(profile_id)
            if runner is None:
                if self.runner_factory is None:
                    raise HTTPException(
                        503,
                        "Connect a verified container execution profile before checking enforcement",
                    )
                profile = await self.store.get(RunnerProfile, profile_id)
                runner = self.runner_factory(profile)
            capability = await probe_memory_runner(runner, profile_id=profile_id)
            self.capabilities[profile_id], self.runners[profile_id] = capability, runner
            return capability

        @app.post(
            prefix + "/jobs/{job_id}/apply",
            response_model=MemoryJob,
            tags=["system"],
            dependencies=dependencies,
        )
        async def apply(job_id: str, body: MemoryRunningUpdate, request: Request):
            if self.apply_running is None:
                raise HTTPException(
                    503,
                    "This runner cannot change running limits. Save budgets for new jobs and explicitly retry instead.",
                )
            try:
                return await self.apply_running(job_id, body, self._actor(request))
            except MemoryBudgetConflict as error:
                # diagnostic-expected: no stale application may change a job.
                raise HTTPException(409, str(error)) from error
            except MemoryEnforcementUnavailable as error:
                # diagnostic-expected: unsupported limits never fall back to unrestricted execution.
                raise HTTPException(503, str(error)) from error
            except MemoryBudgetError as error:
                # diagnostic-expected: explicit impact acknowledgement and bounds are validated before application.
                raise HTTPException(422, str(error)) from error

    @staticmethod
    def _actor(request: Request) -> str:
        return (
            memory_actor.get()
            or getattr(request.state, "auth_device_id", None)
            or "operator"
        )

    async def _profiles(self) -> list[MemoryRunnerChoice]:
        profiles = await self.store.list_entities(RunnerProfile)
        return [
            MemoryRunnerChoice(
                id=profile.id,
                name=profile.name,
                enabled=profile.enabled,
                capabilities=self.capabilities.get(profile.id),
            )
            for profile in profiles
        ]

    def _capabilities(self) -> list[MemoryRunnerCapabilities]:
        items = list(self.capabilities.values())
        known = {item.environment for item in items}
        environments: tuple[ExecutionEnvironment, ...] = (
            "local",
            "container",
            "ssh",
            "remote",
            "cpu-worker",
        )
        for environment in environments:
            if environment not in known:
                items.append(
                    MemoryRunnerCapabilities(
                        environment=environment,
                        detail="Choose a verified memory-managed runner. Unrestricted execution is unavailable for required budgets.",
                    )
                )
        return items
