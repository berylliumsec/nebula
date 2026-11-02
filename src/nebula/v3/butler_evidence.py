"""Selected-project evidence discovery and requesting-browser file handoffs."""

from __future__ import annotations

from typing import Any

from .domain import Evidence, RiskClass
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
    "properties": {"evidence_id": _ID},
    "required": ["evidence_id"],
    "additionalProperties": False,
}
_CURRENT = {
    "type": "object",
    "properties": {"evidence_id": _ID, "expected_revision": _REVISION},
    "required": ["evidence_id", "expected_revision"],
    "additionalProperties": False,
}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


class ButlerEvidence:
    """Keep evidence bytes on the requesting device and durable state in Core."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(item: Evidence) -> dict[str, Any]:
        return {
            "evidence_id": item.id,
            "title": item.title,
            "evidence_type": item.evidence_type,
            "revision": item.revision,
            "sha256": item.sha256,
            "filename": item.metadata.get("filename"),
            "size": item.metadata.get("size"),
            "artifact_available": bool(item.artifact_id),
            "finding_id": item.finding_id,
            "asset_count": len(item.asset_ids),
        }

    def _project_evidence(self, evidence_id: str) -> Evidence:
        item = self.store.get(Evidence, evidence_id)
        if item.engagement_id != self.project_id:
            raise ValueError("evidence does not belong to the selected project")
        return item

    def list_items(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset must be from 0 to 10000")
        rows = self.store.list_entities(
            Evidence, engagement_id=self.project_id, offset=offset, limit=51
        )
        return {
            "evidence": [self._summary(item) for item in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self._project_evidence(arguments["evidence_id"])
        return {
            **self._summary(item),
            "description": item.description[:20_000],
            "asset_ids": item.asset_ids,
            "captured_by": item.captured_by,
            "captured_at": item.captured_at.isoformat(),
        }

    def upload(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "evidence_upload",
                "project_id": self.project_id,
            },
        }

    def download(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self._project_evidence(arguments["evidence_id"])
        if item.revision != arguments["expected_revision"]:
            raise ValueError(
                "evidence changed; read its current revision before acting"
            )
        if not item.artifact_id or not item.sha256:
            raise ValueError("evidence has no verified original artifact to download")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "evidence_download",
                "project_id": self.project_id,
                "evidence_id": item.id,
                "expected_revision": item.revision,
                "title": item.title,
                "expected_sha256": item.sha256,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.evidence.list",
                "Page bounded metadata for immutable evidence in the selected project. File bytes are not returned.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.list_items,
            ),
            (
                "nebula.evidence.detail",
                "Read one selected-project evidence record, its current revision, hash and links without retrieving original bytes.",
                _TARGET,
                RiskClass.LOCAL_READ,
                self.detail,
            ),
            (
                "nebula.evidence.upload",
                "Ask the requesting browser to choose a local file, review its evidence metadata and links, and send it to Core. Butler receives no file bytes. Inspect the project evidence list after an uncertain response before retrying.",
                _EMPTY,
                RiskClass.WORKSPACE_WRITE,
                self.upload,
            ),
            (
                "nebula.evidence.download",
                "Ask the requesting browser to confirm and download an exact evidence original after Core revision and SHA-256 checks. Butler receives no file bytes.",
                _CURRENT,
                RiskClass.WORKSPACE_WRITE,
                self.download,
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
