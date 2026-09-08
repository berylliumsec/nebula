"""Project-scoped API for recorded knowledge and offline solver queries."""

from fastapi import APIRouter
from pydantic import Field, ConfigDict
from ..domain import NebulaModel
from .domain import (
    ModelSession,
    KnowledgeState,
    SolverQuery,
    Formula,
    Value,
    Object,
    ObjectVersion,
    Observation,
    Assertion,
    semantic_hash,
)


class CreateCollection(NebulaModel):
    model_config = ConfigDict(extra="forbid")
    browser_session_id: str = Field(min_length=1, max_length=200)
    import_history: bool = False


class QueryRequest(NebulaModel):
    model_config = ConfigDict(extra="forbid")
    state_id: str = Field(min_length=1, max_length=200)
    formula: Formula
    assertion_ids: list[str] = Field(default_factory=list, max_length=100)
    timeout_ms: int = Field(default=5000, ge=1, le=5000)


class AssertionRequest(NebulaModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    value: Value
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    formula: Formula | None = None


class ForkRequest(NebulaModel):
    assertion_ids: list[str] = Field(default_factory=list, max_length=100)


def model_router(service):
    router = APIRouter(
        prefix="/engagements/{project}/application-model", tags=["application-model"]
    )

    @router.get("/status")
    def status(project: str):
        return service.status(project)

    @router.get("/sessions")
    def sessions(project: str):
        return service.list(ModelSession, project)

    @router.post("/sessions", status_code=201)
    def create(project: str, request: CreateCollection):
        return service.create(
            project, request.browser_session_id, request.import_history
        )

    @router.get("/sessions/{collection}/workspace")
    def workspace(project: str, collection: str):
        return service.workspace(project, collection)

    @router.post("/sessions/{collection}/pause")
    def pause(project: str, collection: str):
        return service.transition(project, collection, "paused")

    @router.post("/sessions/{collection}/resume")
    def resume(project: str, collection: str):
        return service.transition(project, collection, "active")

    @router.post("/sessions/{collection}/import")
    def history(project: str, collection: str):
        return service.import_history(project, collection)

    @router.post("/sessions/{collection}/retry")
    def retry(project: str, collection: str):
        return service.retry_projection(project, collection)

    @router.delete("/sessions/{collection}")
    async def remove(project: str, collection: str):
        service.remove(project, collection)
        return {"deleted": collection, "source_evidence_retained": True}

    @router.get("/sessions/{collection}/states/{state}/fields")
    def fields(project: str, collection: str, state: str):
        return service.fields(project, collection, state)

    @router.get("/sessions/{collection}/states")
    def states(project: str, collection: str):
        service.get(ModelSession, project, collection)
        return service.list(KnowledgeState, project, collection)

    @router.get("/sessions/{collection}/states/{state}")
    def state_detail(project: str, collection: str, state: str):
        return service.get(KnowledgeState, project, state, collection)

    @router.get("/sessions/{collection}/objects")
    def objects(project: str, collection: str):
        service.get(ModelSession, project, collection)
        return service.list(Object, project, collection)

    @router.get("/sessions/{collection}/objects/{object_id}/history")
    def object_history(project: str, collection: str, object_id: str):
        item = service.get(Object, project, object_id, collection)
        return {
            "object": item,
            "versions": [
                v
                for v in service.list(ObjectVersion, project, collection)
                if v.object_id == object_id
            ],
        }

    @router.get("/sessions/{collection}/observations/{observation}")
    def observation_detail(project: str, collection: str, observation: str):
        return service.get(Observation, project, observation, collection)

    @router.get("/sessions/{collection}/assertions")
    def assertions(project: str, collection: str):
        service.get(ModelSession, project, collection)
        return service.list(Assertion, project, collection)

    @router.get("/sessions/{collection}/diff")
    def diff(project: str, collection: str, left: str, right: str):
        return service.diff(project, collection, left, right)

    @router.get("/sessions/{collection}/states/{state}/lineage")
    def lineage(project: str, collection: str, state: str):
        result, pending, seen = [], [state], set()
        while pending and len(result) < 100:
            identifier = pending.pop(0)
            if identifier in seen:
                continue
            seen.add(identifier)
            item = service.get(KnowledgeState, project, identifier, collection)
            result.append(item)
            pending.extend(item.parent_state_ids)
        return {"states": result, "remaining": pending}

    @router.post("/sessions/{collection}/assertions")
    def propose(project: str, collection: str, request: AssertionRequest):
        service.check_enabled()
        return service.propose(project, collection, request)

    @router.post("/sessions/{collection}/states/{state}/fork")
    def fork(project: str, collection: str, state: str, request: ForkRequest):
        from .domain import Assertion

        service.check_enabled()
        original = service.get(KnowledgeState, project, state, collection)
        assertion_hashes = []
        for identifier in request.assertion_ids:
            assertion = service.get(Assertion, project, identifier, collection)
            assertion_hashes.append(semantic_hash(assertion.semantic_content()))
        versions = [
            service.get(ObjectVersion, project, identifier, collection)
            for identifier in original.object_version_ids
        ]
        data = original.model_dump(
            exclude={
                "id",
                "created_at",
                "updated_at",
                "revision",
                "semantic_hash",
                "parent_state_ids",
                "assertion_ids",
                "interpretation",
            }
        )
        return service.store.create(
            KnowledgeState(
                **data,
                parent_state_ids=[state],
                assertion_ids=request.assertion_ids,
                interpretation=True,
                semantic_hash=semantic_hash(
                    {
                        "objects": sorted(
                            [[v.object_id, v.semantic_hash] for v in versions]
                        ),
                        "assertions": sorted(assertion_hashes),
                    }
                ),
            )
        )

    @router.post("/sessions/{collection}/queries", status_code=202)
    async def submit(project: str, collection: str, request: QueryRequest):
        return await service.submit(project, collection, request)

    @router.get("/sessions/{collection}/queries/{query}")
    def query(project: str, collection: str, query: str):
        return service.get(SolverQuery, project, query, collection)

    @router.post("/sessions/{collection}/queries/{query}/cancel")
    async def cancel(project: str, collection: str, query: str):
        return service.cancel_query(project, collection, query)

    return router
