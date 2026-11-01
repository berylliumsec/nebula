"""Project-scoped storage source discovery and requesting-browser handoffs."""

from __future__ import annotations

from typing import Any

from .domain import RiskClass, StorageSource
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_SOURCE_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_SOURCES = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_SOURCE = {
    "type": "object",
    "properties": {"source_id": _SOURCE_ID},
    "required": ["source_id"],
    "additionalProperties": False,
}
_CHANGE = {
    "type": "object",
    "properties": {
        "source_id": _SOURCE_ID,
        "expected_revision": {"type": "integer", "minimum": 1},
    },
    "required": ["source_id", "expected_revision"],
    "additionalProperties": False,
}
_ENROLL = {"type": "object", "properties": {}, "additionalProperties": False}


class ButlerStorage:
    """Keep source tokens and enrollment choices on the requesting device."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(source: StorageSource) -> dict[str, Any]:
        return {
            "source_id": source.id,
            "name": source.name,
            "enabled": source.enabled,
            "revision": source.revision,
            "reporting_interval_seconds": source.reporting_interval_seconds,
            "last_report_at": (
                source.last_report_at.isoformat() if source.last_report_at else None
            ),
        }

    def sources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.list_entities(
            StorageSource, engagement_id=self.project_id, offset=offset, limit=51
        )
        return {
            "sources": [self._summary(source) for source in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def _source(self, source_id: str) -> StorageSource:
        source = self.store.get(StorageSource, source_id)
        if source.engagement_id != self.project_id:
            raise ValueError("choose a storage source in the selected project")
        return source

    def source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._summary(self._source(arguments["source_id"]))

    def enroll(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {"kind": "storage_enroll", "project_id": self.project_id},
        }

    def _pending(self, arguments: dict[str, Any], kind: str) -> dict[str, Any]:
        source = self._source(arguments["source_id"])
        if source.revision != arguments["expected_revision"]:
            raise ValueError("storage source changed; read its current revision")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": kind,
                "project_id": self.project_id,
                "source_id": source.id,
                "expected_revision": source.revision,
                "name": source.name,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.storage.sources",
                "List bounded nonsecret storage-source metadata in the selected project. Read an exact source before changing it; no bearer tokens or credential references are returned.",
                _SOURCES,
                RiskClass.LOCAL_READ,
                self.sources,
            ),
            (
                "nebula.storage.source",
                "Read the current revision and nonsecret status of one storage source in the selected project.",
                _SOURCE,
                RiskClass.LOCAL_READ,
                self.source,
            ),
            (
                "nebula.storage.enroll",
                "Open the requesting device's Storage enrollment form for the selected project. The operator chooses a runtime, completes the form, and keeps the one-time source token in that browser. This prepares a handoff; it does not create a source.",
                _ENROLL,
                RiskClass.WORKSPACE_WRITE,
                self.enroll,
            ),
            (
                "nebula.storage.rotate",
                "Ask the requesting browser to confirm rotation of an exact selected-project source at its current revision. The new one-time token stays in that browser and never enters the model transcript.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                lambda arguments: self._pending(arguments, "storage_rotate"),
            ),
            (
                "nebula.storage.revoke",
                "Ask the requesting browser to confirm revocation and disablement of an exact selected-project source at its current revision. The old sender token stops working after Core confirms the change.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                lambda arguments: self._pending(arguments, "storage_revoke"),
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
