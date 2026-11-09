"""Stateless, SQL-only global APIs without a privileged execution project."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from starlette.routing import Match

from .api_crud import _register_crud_routes, StructuredConflictError
from .api_validation import ApiEntityValidator
from .domain import (
    Engagement,
    HarnessProfile,
    LibraryItem,
    McpServerProfile,
    OperatorProfile,
    ProviderProfile,
    RunnerProfile,
)
from .knowledge import library_item_summary
from .providers import PROVIDER_CATALOG
from .relations import ResourceRelationService
from .setup import SetupService
from .storage import ConflictError, NebulaStore, NotFoundError, StoreTransaction


class GlobalSqlStore(NebulaStore):
    """Join the request's SQL transaction; no controllers or background listeners."""

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False):
        current = self.database.task_session()
        if current is not None:
            yield StoreTransaction(current)
        else:
            async with super().transaction(immediate=immediate) as transaction:
                yield transaction


class RuntimeGlobalApi:
    """Only catalog/configuration SQL is eligible for atomic API-local delivery.

    Provider invocations, probes, files and every project mutation continue to
    its fenced owner. A read can enumerate global metadata without constructing
    an execution kernel or inventing a synthetic project.
    """

    def __init__(self, database):
        from contextvars import ContextVar
        from starlette.responses import JSONResponse

        self.store = GlobalSqlStore(database)
        self.actor: ContextVar[str] = ContextVar("global_sql_actor", default="operator")
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.setup = SetupService(self.store, None)
        validator = ApiEntityValidator(self.store)
        relations = ResourceRelationService(self.store)

        async def authenticated():
            # The public adapter authenticates before selecting this app. It is
            # never exposed on a socket or mounted outside that adapter.
            return self.actor.get()

        async def capacity():
            return None

        from .runtime_global_settings import mount_global_settings
        from .runtime_global_reads import mount_global_reads
        from .runtime_global_work import mount_global_work

        mount_global_settings(self.app, self.store, self.actor)
        mount_global_reads(self.app, self.store, self.actor)
        self.work_observer = mount_global_work(self.app, self.store, self.actor)

        for resource, model in (
            ("engagements", Engagement),
            ("providers", ProviderProfile),
            ("harnesses", HarnessProfile),
            ("mcp_servers", McpServerProfile),
        ):
            _register_crud_routes(
                self.app,
                self.store,
                relations,
                authenticated,
                validator,
                resource,
                model,
                listing_capacity=capacity,
                actor_id=authenticated,
            )

        @self.app.exception_handler(NotFoundError)
        async def missing(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=404)

        @self.app.exception_handler(ConflictError)
        async def conflict(_request, error):
            body = {"detail": str(error)}
            if isinstance(error, StructuredConflictError):
                body["code"] = error.code
            return JSONResponse(body, status_code=409)

        @self.app.exception_handler(ValueError)
        async def invalid(_request, error):
            return JSONResponse({"detail": str(error)}, status_code=422)

        @self.app.get("/api/v1/provider-catalog")
        async def catalog():
            return [
                entry.model_dump(mode="json")
                for entry in sorted(
                    PROVIDER_CATALOG.values(),
                    key=lambda item: item.display_name,
                )
            ]

        @self.app.get("/api/v1/operator-profiles")
        async def operators():
            from .operators import OperatorProfileService

            return await OperatorProfileService(self.store).list_profiles()

        @self.app.get("/api/v1/operator-profiles/active")
        async def active_operator():
            from .operators import OperatorProfileService

            return await OperatorProfileService(self.store).active_profile()

        @self.app.get("/api/v1/operator-profiles/{profile_id}")
        async def operator(profile_id: str):
            return await self.store.get(OperatorProfile, profile_id)

        @self.app.get("/api/v1/runner-profiles")
        async def runners():
            return await self.store.list_entities(RunnerProfile, limit=1000)

        @self.app.get("/api/v1/library/items")
        async def library(
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=100, ge=1, le=1000),
        ):
            return [
                library_item_summary(item)
                for item in await self.store.list_entities(
                    LibraryItem,
                    offset=offset,
                    limit=limit,
                )
            ]

        @self.app.get("/api/v1/library/items/{item_id}")
        async def library_item(item_id: str):
            return library_item_summary(await self.store.get(LibraryItem, item_id))

        @self.app.get("/api/v1/setup/status")
        async def setup():
            return await self.setup.status()

    def supports(self, scope: dict) -> bool:
        if scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}:
            path = scope["path"].split("/")
            # Project edits always require its exact execution fence. Creating a
            # project is the single global exception, before any owner exists.
            if len(path) > 4 and path[3] == "engagements":
                return False
        return any(route.matches(scope)[0] == Match.FULL for route in self.app.routes)

    async def __call__(self, scope, receive, send):
        if not self.supports(scope):
            raise HTTPException(404, "Global API route is unavailable")
        token = self.actor.set(scope.get("state", {}).get("global_actor", "operator"))
        try:
            await self.app(scope, receive, send)
        finally:
            self.actor.reset(token)

    async def close(self):
        await self.work_observer.close()
        await self.setup.shutdown()
        await self.store.event_history.close()
