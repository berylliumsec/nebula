"""Authenticated linked-memory routes, used by the CLI and Butler.

There is no Settings screen: linked memory has no operator interface. These
routes are the only way to change a project's stage or read its status.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query

from .linked_memory import LinkedMemoryStore, LinkedMemoryWrite
from .memory_actor import memory_actor
from .storage import ConflictError


def linked_memory_router(
    memory: LinkedMemoryStore, curator: Callable[[], Any] | None = None
) -> APIRouter:
    router = APIRouter(tags=["linked-memory"])

    def current_curator() -> Any:
        return curator() if curator is not None else None

    @router.get("/engagements/{engagement_id}/linked-memory")
    async def get_linked_memory(engagement_id: str) -> dict[str, Any]:
        return (await memory.settings(engagement_id)).model_dump(mode="json")

    @router.put("/engagements/{engagement_id}/linked-memory")
    async def put_linked_memory(
        engagement_id: str, request: LinkedMemoryWrite
    ) -> dict[str, Any]:
        actor = memory_actor.get() or "operator"
        try:
            saved = await memory.write_settings(engagement_id, request, actor=actor)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        service = current_curator()
        if service is not None and saved.effective_stage != "off":
            service.wake(engagement_id)
        return saved.model_dump(mode="json")

    @router.get("/engagements/{engagement_id}/linked-memory/status")
    async def linked_memory_status(engagement_id: str) -> dict[str, Any]:
        status = await memory.status(engagement_id)
        service = current_curator()
        status["curation"] = (
            service.snapshot(engagement_id)
            if service is not None
            else {"curator": "unavailable", "paused_reason": None, "helper": None}
        )
        return status

    @router.get("/engagements/{engagement_id}/linked-memory/cards")
    async def linked_memory_cards(
        engagement_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        state: str | None = Query(default=None, pattern=r"^[a-z_]{1,20}$"),
    ) -> dict[str, Any]:
        await memory.settings(engagement_id)
        return {"items": await memory.cards(engagement_id, limit=limit, state=state)}

    return router
