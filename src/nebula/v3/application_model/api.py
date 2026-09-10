"""Project-scoped schema, graph, evidence and transaction API."""

from fastapi import APIRouter, Query, HTTPException
from .graph import GraphTransaction, GraphReset
from .reset import preview, reset


def model_router(service):
    router = APIRouter(
        prefix="/engagements/{project}/application-model", tags=["application-model"]
    )

    @router.get("/schema")
    def schema(project: str, category: str | None = None, query: str = ""):
        try:
            return service.schema(project, category, query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/reset-preview")
    def reset_preview(project: str):
        return preview(service, project)

    @router.post("/reset")
    def start_over(project: str, request: GraphReset):
        return reset(service, project, request)

    @router.get("/graph")
    def graph(project: str):
        return service.workspace(project)

    @router.get("/view")
    def view(
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
            return service.view(
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
    def search(
        project: str,
        query: str = "",
        category: str | None = None,
        type: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        return service.search(project, query, category, type, offset, limit)

    @router.get("/objects/{identifier}/neighborhood")
    def neighborhood(
        project: str,
        identifier: str,
        depth: int = Query(1, ge=0, le=3),
        limit: int = Query(100, ge=1, le=100),
    ):
        return service.neighborhood(project, identifier, depth, limit)

    @router.get("/evidence")
    def evidence_list(
        project: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=100),
    ):
        return service.evidence_list(project, offset, limit)

    @router.get("/evidence/{kind}/{identifier}")
    def evidence(project: str, kind: str, identifier: str):
        try:
            return service.evidence(project, kind, identifier)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/updates")
    def updates(
        project: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)
    ):
        return service.history(project, after, limit)

    @router.post("/transactions")
    def transact(project: str, request: GraphTransaction):
        try:
            return service.transact(project, request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
