"""Bounded shared-Library discovery and requesting-browser mutation handoffs."""

from __future__ import annotations

from typing import Any

from .domain import LibraryItem, RiskClass
from .storage import NebulaStore
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

    def list_items(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset must be from 0 to 10000")
        rows = self.store.list_entities(LibraryItem, offset=offset, limit=51)
        return {
            "items": [self._summary(item) for item in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
            "scope": "shared across every project",
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self.store.get(LibraryItem, arguments["item_id"])
        return {
            **self._summary(item),
            "citation": item.citation,
            "updated_at": item.updated_at.isoformat(),
            "scope": "shared across every project",
        }

    def _pending(self, arguments: dict[str, Any], kind: str) -> dict[str, Any]:
        item = self.store.get(LibraryItem, arguments["item_id"])
        if item.revision != arguments["expected_revision"]:
            raise ValueError(
                "Library item changed; read its current revision before acting"
            )
        if kind == "library_reindex" and not item.artifact_id:
            raise ValueError("Library item has no original artifact to reindex")
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

    def reindex(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._pending(arguments, "library_reindex")

    def remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._pending(arguments, "library_remove")

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
                arguments: dict[str, Any], *, selected_handler: Any = handler
            ) -> dict[str, Any]:
                return selected_handler(arguments)

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
