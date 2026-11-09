"""Explicit authenticated host-memory protocol; workers open no local index."""

from __future__ import annotations

import builtins
from dataclasses import asdict

from pydantic import Field, ValidationError

from .butler_memory import (
    ButlerMemoryItem,
    ButlerMemoryList,
    ButlerMemoryService,
    MemoryConflictError,
    MemoryNotFoundError,
    UnsafeMemoryError,
)
from .domain import NebulaModel
from .execution_scope import current_execution_lease, execution_context
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError, RuntimeRpcClient, RuntimeRpcServer
from .shared_butler_memory import MemoryWrite, SharedButlerMemory

MEMORY_OPERATION = "host.memory/v1"


class MemoryAuthority(NebulaModel):
    project_id: str = Field(min_length=1, max_length=200)
    runtime_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1, strict=True)


class MemoryListArguments(NebulaModel):
    limit: int = Field(default=100, ge=1, le=200, strict=True)


class MemorySearchArguments(NebulaModel):
    query: str = Field(min_length=1, max_length=500, repr=False)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


class MemoryDeleteArguments(NebulaModel):
    item_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1, strict=True)


class MemoryEditArguments(MemoryDeleteArguments):
    content: str = Field(min_length=8, max_length=800, repr=False)


class MemoryCursorArguments(NebulaModel):
    sequence: int = Field(ge=0, lt=2**63, strict=True)


class EmptyArguments(NebulaModel):
    pass


_ERRORS = {
    "conflict": MemoryConflictError,
    "not_found": MemoryNotFoundError,
    "unsafe": UnsafeMemoryError,
    "lease_lost": LeaseLost,
    "coordination": CoordinationConflict,
    "invalid": ValueError,
}


def register_shared_memory(
    server: RuntimeRpcServer, memory: SharedButlerMemory, *, runtime_id: str
):
    """Bind one authenticated peer to one immutable process incarnation."""

    async def invoke(payload):
        try:
            if set(payload) != {"lease", "action", "arguments"}:
                raise ValueError("Invalid shared-memory envelope")
            authority = MemoryAuthority.model_validate(payload["lease"])
            if authority.runtime_id != runtime_id:
                raise LeaseLost("Shared-memory request belongs to another worker")
            lease = ProjectLease(**authority.model_dump())
            arguments, action = payload["arguments"], payload["action"]
            with execution_context(lease):
                if action == "save":
                    body = MemoryWrite.model_validate(arguments)
                    value = await memory._save(**body.model_dump())
                elif action == "list":
                    body = MemoryListArguments.model_validate(arguments)
                    value = await memory.list(lease.project_id, **body.model_dump())
                elif action == "search":
                    body = MemorySearchArguments.model_validate(arguments)
                    value = await memory.search(lease.project_id, **body.model_dump())
                elif action == "update":
                    body = MemoryEditArguments.model_validate(arguments)
                    value = await memory.update(lease.project_id, **body.model_dump())
                elif action == "delete":
                    body = MemoryDeleteArguments.model_validate(arguments)
                    value = await memory.delete(lease.project_id, **body.model_dump())
                elif action == "review_lessons":
                    EmptyArguments.model_validate(arguments)
                    value = await memory._review_lesson_contents(lease.project_id)
                elif action == "read_cursor":
                    EmptyArguments.model_validate(arguments)
                    value = await memory._read_event_cursor(lease.project_id)
                elif action == "advance_cursor":
                    body = MemoryCursorArguments.model_validate(arguments)
                    value = await memory._advance_event_cursor(
                        lease.project_id, body.sequence
                    )
                else:
                    raise ValueError("Unsupported shared-memory operation")
            if isinstance(value, (ButlerMemoryItem, ButlerMemoryList)):
                value = value.model_dump(mode="json")
            return {"ok": True, "value": value}
        except ValidationError:
            # Pydantic's input-rich exception can contain memory candidates.
            return {
                "ok": False,
                "error": "invalid",
                "detail": "Shared-memory arguments exceed the supported bounds",
            }
        except tuple(_ERRORS.values()) as error:
            kind = next(
                name for name, model in _ERRORS.items() if isinstance(error, model)
            )
            return {"ok": False, "error": kind, "detail": str(error)[:1000]}

    server.register(MEMORY_OPERATION, invoke)


class RemoteButlerMemory(ButlerMemoryService):
    """Retain capture/recollection semantics while delegating persistence/indexing."""

    def __init__(self, client: RuntimeRpcClient):
        # Deliberately do not construct the compatibility backend or its locks,
        # SQLite connection pool, local paths or Chroma collection.
        self.client = client
        self.embedding_index = None

    @staticmethod
    def _project(project_id: str):
        lease = current_execution_lease()
        if lease is None or lease.project_id != project_id:
            raise LeaseLost("Shared Butler memory requires exact project ownership")
        return lease

    async def _call(self, project_id: str, action: str, arguments: dict):
        lease = self._project(project_id)
        response = await self.client.call(
            MEMORY_OPERATION,
            {
                "lease": asdict(lease),
                "action": action,
                "arguments": arguments,
            },
        )
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise RuntimeIpcError("Invalid shared-memory result")
        if response["ok"] and set(response) == {"ok", "value"}:
            return response["value"]
        if (
            set(response) != {"ok", "error", "detail"}
            or response["error"] not in _ERRORS
            or not isinstance(response["detail"], str)
            or len(response["detail"]) > 1000
        ):
            raise RuntimeIpcError("Invalid shared-memory rejection")
        raise _ERRORS[response["error"]](response["detail"])

    async def _ensure_ready(self):
        return

    async def _save(
        self,
        *,
        source_project_id,
        scope,
        content,
        source_kind,
        source_label,
        source_ref,
        project_name="",
    ) -> ButlerMemoryItem:
        value = await self._call(
            source_project_id,
            "save",
            {
                "source_project_id": source_project_id,
                "scope": scope,
                "content": content,
                "source_kind": source_kind,
                "source_label": source_label,
                "source_ref": source_ref,
                "project_name": project_name,
            },
        )
        return ButlerMemoryItem.model_validate(value)

    async def list(self, engagement_id: str, *, limit=100) -> ButlerMemoryList:
        return ButlerMemoryList.model_validate(
            await self._call(engagement_id, "list", {"limit": limit})
        )

    async def search(
        self, engagement_id: str, query: str, *, limit=20
    ) -> ButlerMemoryList:
        return ButlerMemoryList.model_validate(
            await self._call(engagement_id, "search", {"query": query, "limit": limit})
        )

    async def update(
        self, engagement_id: str, item_id: str, content: str, *, expected_revision: int
    ) -> ButlerMemoryItem:
        return ButlerMemoryItem.model_validate(
            await self._call(
                engagement_id,
                "update",
                {
                    "item_id": item_id,
                    "content": content,
                    "expected_revision": expected_revision,
                },
            )
        )

    async def delete(
        self, engagement_id: str, item_id: str, *, expected_revision: int
    ) -> None:
        await self._call(
            engagement_id,
            "delete",
            {"item_id": item_id, "expected_revision": expected_revision},
        )

    async def _review_lesson_contents(self, project_id: str) -> builtins.list[str]:
        value = await self._call(project_id, "review_lessons", {})
        if (
            not isinstance(value, list)
            or len(value) > 100
            or any(not isinstance(item, str) or len(item) > 800 for item in value)
        ):
            raise RuntimeIpcError("Invalid shared-memory review page")
        return value

    async def _read_event_cursor(self, project_id: str) -> int:
        value = await self._call(project_id, "read_cursor", {})
        if type(value) is not int or not 0 <= value < 2**63:
            raise RuntimeIpcError("Invalid shared-memory cursor")
        return value

    async def _advance_event_cursor(self, project_id: str, sequence: int) -> None:
        await self._call(project_id, "advance_cursor", {"sequence": sequence})
