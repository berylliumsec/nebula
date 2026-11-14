"""Project-scoped schema, graph, evidence and transaction API."""

from fastapi import APIRouter, Query, HTTPException
from .graph import GraphTransaction, GraphReset
from .reset import preview, reset


def model_router(service):
    router = APIRouter(
        prefix="/engagements/{project}/application-model", tags=["application-model"]
    )

    @router.get("/schema")
    async def schema(project: str, category: str | None = None, query: str = ""):
        try:
            return await service.schema(project, category, query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/reset-preview")
    async def reset_preview(project: str):
        return await preview(service, project)

    @router.post("/reset")
    async def start_over(project: str, request: GraphReset):
        return await reset(service, project, request)

    @router.get("/graph")
    async def graph(project: str):
        return await service.workspace(project)

    @router.get("/view")
    async def view(
        project: str,
        query: str = Query("", max_length=200),
        category: str | None = None,
        offset: int | None = Query(None, ge=0),
        object_id: str = "",
        relationship_id: str = "",
        depth: int = Query(1, ge=1, le=3),
        relationship_offset: int = Query(0, ge=0),
    ):
        try:
            return await service.view(
                project,
                query,
                category,
                offset,
                object_id,
                relationship_id,
                depth,
                relationship_offset,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/search")
    async def search(
        project: str,
        query: str = "",
        category: str | None = None,
        type: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        return await service.search(project, query, category, type, offset, limit)

    @router.get("/objects/{identifier}/neighborhood")
    async def neighborhood(
        project: str,
        identifier: str,
        depth: int = Query(1, ge=0, le=3),
        limit: int = Query(100, ge=1, le=100),
    ):
        return await service.neighborhood(project, identifier, depth, limit)

    @router.get("/evidence")
    async def evidence_list(
        project: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        return await service.evidence_list(project, offset, limit)

    @router.get("/evidence/{kind}/{identifier}")
    async def evidence(project: str, kind: str, identifier: str):
        try:
            return await service.evidence(project, kind, identifier)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/updates")
    async def updates(
        project: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)
    ):
        return await service.history(project, after, limit)

    @router.post("/transactions")
    async def transact(project: str, request: GraphTransaction):
        try:
            return await service.transact(project, request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
