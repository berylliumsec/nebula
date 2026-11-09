"""Authenticated SQL resource discovery without a synthetic execution project."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from .action_registry import ActionRegistry
from .domain import (
    ActionDescriptor,
    ActionResolutionRequest,
    ResourceKind,
    ResourceRef,
    ResourceResolution,
    SearchResponse,
    SearchScope,
)
from .relations import resolve_resource_ref
from .search import FederatedSearch, SearchCursorError
from .storage import NebulaStore


def mount_global_resources(app: FastAPI, store: NebulaStore) -> None:
    actions = ActionRegistry(store)
    search = FederatedSearch(store, actions)

    @app.get("/api/v1/search", response_model=SearchResponse)
    async def search_resources(
        query: str = Query(default="", max_length=500),
        active_project: str | None = Query(default=None, max_length=200),
        scope: SearchScope = SearchScope.ACTIVE,
        resource_kind: list[ResourceKind] = Query(default=[]),
        cursor: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> SearchResponse:
        try:
            return await search.search(
                query=query,
                active_project=active_project,
                scope=scope,
                kinds=resource_kind,
                cursor=cursor,
                limit=limit,
            )
        except SearchCursorError as exc:
            raise HTTPException(422, "invalid search cursor") from exc

    @app.post("/api/v1/resources/resolve", response_model=ResourceResolution)
    async def resolve_resource(ref: ResourceRef) -> ResourceResolution:
        return await resolve_resource_ref(store, ref)

    @app.post("/api/v1/actions/resolve", response_model=list[ActionDescriptor])
    async def resolve_actions(
        request: ActionResolutionRequest,
    ) -> list[ActionDescriptor]:
        return await actions.resolve(request)
