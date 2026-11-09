"""Authenticated Work metadata reads without a project execution owner."""

from __future__ import annotations

from contextvars import ContextVar

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from .domain import Engagement, WorkItem, WorkUpdate
from .harnesses import HarnessCatalogItem, harness_catalog
from .storage import NebulaStore, NotFoundError
from .work import WorkAgentActivity, WorkService


async def explicit_work_project(request: Request, store: NebulaStore) -> str | None:
    """Honor every explicit project value; a source chat is not its Work owner."""
    if len(request.scope.get("query_string", b"")) > 8192:
        raise HTTPException(422, "Work query exceeds the supported bounds")
    values = request.query_params.getlist(
        "engagement_id"
    ) + request.query_params.getlist("project_id")
    if len(values) > 32 or any(not value or len(value) > 200 for value in values):
        raise HTTPException(422, "Project filters must contain 1 to 200 characters")
    projects = set(values)
    if len(projects) > 1:
        raise HTTPException(409, "A request cannot span multiple execution projects")
    if not projects:
        return None
    project = projects.pop()
    try:
        await store.get(Engagement, project)
    except NotFoundError:
        raise HTTPException(409, "Project does not exist") from None
    return project


def mount_global_work(app: FastAPI, store: NebulaStore, actor: ContextVar[str]) -> None:
    """The public adapter authenticates before selecting the SQL read app."""
    work = WorkService(store)

    def authenticated() -> str:
        value = actor.get()
        if value != "operator" and not value.startswith("device:"):
            raise HTTPException(403, "An authenticated Core actor is required")
        return value

    dependencies = [Depends(authenticated)]

    @app.get("/api/v1/harness-catalog", dependencies=dependencies)
    async def catalog(response: Response) -> list[HarnessCatalogItem]:
        response.headers["Cache-Control"] = "no-store"
        return harness_catalog()

    @app.get(
        "/api/v1/work/items", response_model=list[WorkItem], dependencies=dependencies
    )
    async def items(
        request: Request,
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> list[WorkItem]:
        response.headers["Cache-Control"] = "no-store"
        return await store.list_entities(
            WorkItem,
            engagement_id=await explicit_work_project(request, store),
            offset=offset,
            limit=limit,
            newest_first=True,
        )

    @app.get(
        "/api/v1/work/updates",
        response_model=list[WorkUpdate],
        dependencies=dependencies,
    )
    async def updates(
        request: Request,
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        source_session_id: str | None = Query(
            default=None, min_length=1, max_length=200
        ),
    ) -> list[WorkUpdate]:
        response.headers["Cache-Control"] = "no-store"
        project = await explicit_work_project(request, store)
        if source_session_id is not None:
            return await store.find_entities(
                WorkUpdate,
                {"source_session_id": source_session_id},
                engagement_id=project,
                offset=offset,
                limit=limit,
                newest_first=True,
            )
        return await store.list_entities(
            WorkUpdate,
            engagement_id=project,
            offset=offset,
            limit=limit,
            newest_first=True,
        )

    @app.get(
        "/api/v1/work/agents",
        response_model=list[WorkAgentActivity],
        dependencies=dependencies,
    )
    async def agents(request: Request, response: Response) -> list[WorkAgentActivity]:
        response.headers["Cache-Control"] = "no-store"
        return await work.active_agents(await explicit_work_project(request, store))
