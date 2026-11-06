"""Bounded shared-Library discovery and requesting-browser mutation handoffs."""

from __future__ import annotations

from .async_utils import call_async

from typing import Any

from .domain import LibraryItem, RiskClass
from .storage import NebulaStore
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_REVISION = {"type": "integer", "minimum": 1}
_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_TARGET = {
    "type": "object",
    "properties": {"item_id": _ID},
    "required": ["item_id"],
    "additionalProperties": False,
}
_CHANGE = {
    "type": "object",
    "properties": {"item_id": _ID, "expected_revision": _REVISION},
    "required": ["item_id", "expected_revision"],
    "additionalProperties": False,
}
_ADD = {"type": "object", "properties": {}, "additionalProperties": False}


class ButlerLibrary:
    """Prepare exact global Library actions for one interactive Butler browser."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(item: LibraryItem) -> dict[str, Any]:
        return {
            "item_id": item.id,
            "name": item.name,
            "source_type": item.source_type,
            "status": item.status,
            "revision": item.revision,
            "document_count": item.document_count,
            "artifact_available": bool(item.artifact_id),
        }

    async def list_items(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset must be from 0 to 10000")
        rows = await self.store.list_entities(LibraryItem, offset=offset, limit=51)
        return {
            "items": [self._summary(item) for item in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
            "scope": "shared across every project",
        }

    async def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = await self.store.get(LibraryItem, arguments["item_id"])
        return {
            **self._summary(item),
            "citation": item.citation,
            "updated_at": item.updated_at.isoformat(),
            "scope": "shared across every project",
        }

    async def _pending(self, arguments: dict[str, Any], kind: str) -> dict[str, Any]:
        item = await self.store.get(LibraryItem, arguments["item_id"])
        if item.revision != arguments["expected_revision"]:
            raise ValueError(
                "Library item changed; read its current revision before acting"
            )
        if kind in {"library_reindex", "library_download"} and not item.artifact_id:
            raise ValueError("Library item has no original artifact for this action")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": kind,
                "project_id": self.project_id,
                "item_id": item.id,
                "expected_revision": item.revision,
                "name": item.name,
            },
        }

    async def reindex(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._pending(arguments, "library_reindex")

    async def remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._pending(arguments, "library_remove")

    def add(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "library_add",
                "project_id": self.project_id,
            },
        }

    async def download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._pending(arguments, "library_download")

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.library.list",
                "Page bounded metadata for the Library shared across every project. Choose an exact item before acting; no file bytes are returned.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.list_items,
            ),
            (
                "nebula.library.detail",
                "Read the current revision and status of one shared Library item without retrieving its original file.",
                _TARGET,
                RiskClass.LOCAL_READ,
                self.detail,
            ),
            (
                "nebula.library.add",
                "Ask the requesting browser to choose a supported local file for the Library shared by every project. Only the browser sends file bytes to Core; the model receives no file content.",
                _ADD,
                RiskClass.WORKSPACE_WRITE,
                self.add,
            ),
            (
                "nebula.library.download",
                "Ask the requesting browser to confirm and download one exact shared Library item's original artifact at its current revision. No file bytes are returned to the model.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.download,
            ),
            (
                "nebula.library.reindex",
                "Ask the requesting browser to confirm reindexing one exact shared Library item at its current revision. Core reports the saved item after rebuilding.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.reindex,
            ),
            (
                "nebula.library.remove",
                "Ask the requesting browser for explicit global-impact confirmation before removing one exact item from retrieval for every project. The immutable artifact remains.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.remove,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any],
                *,
                selected_handler: Any = handler,
                selected_read: bool = risk == RiskClass.LOCAL_READ,
            ) -> dict[str, Any]:
                if selected_read:
                    return await read_in_worker(selected_handler, arguments)
                return await call_async(selected_handler, arguments)

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=risk,
                        idempotency=(
                            IdempotencyBehavior.SAFE
                            if risk == RiskClass.LOCAL_READ
                            else IdempotencyBehavior.NON_IDEMPOTENT
                        ),
                    ),
                    run,
                )
            )
        return result
