"""Contextless SQL reads without constructing a project or execution kernel."""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from sqlalchemy import select

from .database import EntityRow
from .domain import (
    ChatQueue,
    ChatSession,
    Entity,
    StorageAction,
    StorageDevice,
    StorageDispatch,
    StorageIncident,
    StorageSource,
    StorageVolume,
    utc_now,
)
from .guides import guide_progress_router
from .operators import OperatorProfileService
from .storage import NebulaStore, NotFoundError

MAX_CATALOG_ITEMS = 1000
MAX_DASHBOARD_BYTES = 8 * 1024 * 1024


async def _catalog(
    store: NebulaStore,
    model: type[Entity],
    *,
    offset: int,
    limit: int,
    exclude: set[str],
    budget: list[int],
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    # Actions can retain large callback receipts. Check each against the budget
    # before fetching another, rather than materializing a whole page of them.
    batch_size = 1 if model is StorageAction else 32
    while len(rows) <= limit:
        batch = await store.list_entities(
            model,
            offset=offset + len(rows),
            limit=min(batch_size, limit + 1 - len(rows)),
        )
        if not batch:
            return rows, False
        for item in batch:
            if len(rows) == limit:
                return rows, True
            value = item.model_dump(mode="json", exclude=exclude)
            budget[0] -= len(json.dumps(value, ensure_ascii=False).encode())
            if budget[0] < 0:
                raise HTTPException(
                    413, "Storage snapshot is too large; read a smaller catalog page."
                )
            rows.append(value)
        if len(batch) < batch_size:
            return rows, False
    return rows, True


async def _queue_observation(
    store: NebulaStore,
    source: StorageSource,
    incident: StorageIncident,
    dispatch_id: str,
) -> tuple[str | None, str | None]:
    """Read only a saved queue item's state, with its exact project boundary."""
    session_id = incident.chat_session_id
    async with store.database.session() as session:
        project_id = await session.scalar(
            select(EntityRow.engagement_id).where(
                EntityRow.id == session_id,
                EntityRow.kind == ChatSession.entity_kind,
            )
        )
        if project_id is None:
            return "missing", "The linked chat is missing. Retry the dispatch."
        if project_id != source.engagement_id:
            return "unavailable", "The linked chat belongs to another project."
        row = (
            await session.execute(
                select(EntityRow.engagement_id, EntityRow.payload["items"]).where(
                    EntityRow.id == "chat-queue-" + session_id,
                    EntityRow.kind == ChatQueue.entity_kind,
                )
            )
        ).one_or_none()
        if row is None:
            return None, None
        if row[0] != project_id:
            return "unavailable", "The linked queue belongs to another project."
        for item in row[1] if isinstance(row[1], list) else []:
            if isinstance(item, dict) and item.get("key") == dispatch_id:
                status, detail = item.get("status"), item.get("detail")
                return str(status)[:100] if status is not None else None, str(detail)[
                    :1000
                ] if detail else None
    return None, None


async def storage_dashboard(
    store: NebulaStore, *, offset: int = 0, limit: int = MAX_CATALOG_ITEMS
) -> dict[str, Any]:
    """Project durable reports; never contact a sender, launch or retry work."""
    if offset < 0 or not 1 <= limit <= MAX_CATALOG_ITEMS:
        raise ValueError("Storage catalog bounds are invalid")
    budget = [MAX_DASHBOARD_BYTES]
    result: dict[str, Any] = {}
    next_offsets: dict[str, int] = {}
    for name, model, excluded in (
        ("sources", StorageSource, {"token_sha256"}),
        ("devices", StorageDevice, set()),
        ("volumes", StorageVolume, set()),
        ("incidents", StorageIncident, set()),
        ("actions", StorageAction, set()),
        ("dispatches", StorageDispatch, {"prompt"}),
    ):
        values, more = await _catalog(
            store, model, offset=offset, limit=limit, exclude=excluded, budget=budget
        )
        result[name] = values
        if more:
            next_offsets[name] = offset + limit

    source_cache: dict[str, StorageSource | None] = {}

    async def source_for(identity: str) -> StorageSource | None:
        if identity not in source_cache:
            try:
                source_cache[identity] = await store.get(StorageSource, identity)
            except NotFoundError:
                # diagnostic-expected: deleted source metadata produces an unavailable dashboard entry.
                source_cache[identity] = None
        return source_cache[identity]

    now = utc_now()
    for volume in result["volumes"]:
        source = await source_for(volume["source_id"])
        # Missing configuration cannot establish a report's freshness.
        volume["stale"] = (
            source is None
            or (
                now
                - datetime.fromisoformat(volume["received_at"].replace("Z", "+00:00"))
            ).total_seconds()
            > source.reporting_interval_seconds
        )

    for dispatch in result["dispatches"]:
        dispatch["queue_status"] = None
        dispatch["queue_detail"] = None
        if dispatch["status"] != "queued":
            continue
        try:
            incident = await store.get(StorageIncident, dispatch["incident_id"])
        except NotFoundError:
            # diagnostic-expected: deleted incident metadata produces the explicit unavailable queue state.
            incident = None
        source = await source_for(dispatch["source_id"])
        if incident is None or source is None or incident.source_id != source.id:
            dispatch["queue_status"] = "unavailable"
            dispatch["queue_detail"] = "The linked storage metadata is unavailable."
            continue
        dispatch["queue_status"], dispatch["queue_detail"] = await _queue_observation(
            store, source, incident, dispatch["id"]
        )

    # Preserve the original six-array shape for a normal complete first read.
    if next_offsets or offset or limit != MAX_CATALOG_ITEMS:
        result.update(
            partial=bool(next_offsets),
            offset=offset,
            limit=limit,
            next_offset=offset + limit if next_offsets else None,
            next_offsets=next_offsets,
        )
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_DASHBOARD_BYTES:
        raise HTTPException(
            413, "Storage snapshot is too large; read a smaller catalog page."
        )
    return result


def mount_global_reads(
    app: FastAPI, store: NebulaStore, actor: ContextVar[str]
) -> None:
    """The public adapter authenticates before selecting these SQL routes."""

    def authenticated() -> str:
        value = actor.get()
        if value != "operator" and not value.startswith("device:"):
            raise HTTPException(403, "An authenticated Core actor is required")
        return value

    app.include_router(
        guide_progress_router(store, OperatorProfileService(store)),
        prefix="/api/v1",
        dependencies=[Depends(authenticated)],
    )

    @app.get("/api/v1/storage", dependencies=[Depends(authenticated)])
    async def read_storage(
        response: Response,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=MAX_CATALOG_ITEMS, ge=1, le=MAX_CATALOG_ITEMS),
    ) -> dict[str, Any]:
        result = await storage_dashboard(store, offset=offset, limit=limit)
        response.headers["Cache-Control"] = "no-store"
        if result.get("partial"):
            response.headers["X-Nebula-Partial"] = "true"
            response.headers["X-Nebula-Next-Offset"] = str(offset + limit)
        return result
