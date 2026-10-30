"""Project-scoped evidence discovery and device-file upload handoff."""

from __future__ import annotations

from typing import Any

from .domain import Asset, Evidence, Finding, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {"type": "integer", "minimum": 0, "maximum": 10000}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ButlerEvidence:
    """Keep device bytes in the requesting browser and links within one project."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    def _project_record(
        self, model: type[Asset] | type[Finding] | type[Evidence], entity_id: str
    ) -> Asset | Finding | Evidence:
        record = self.store.get(model, entity_id)
        if record.engagement_id != self.project_id:
            raise ValueError("record is outside the selected project")
        return record

    def list_evidence(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.list_entities(
            Evidence, engagement_id=self.project_id, offset=offset, limit=51
        )
        return {
            "evidence": [
                {
                    "evidence_id": item.id,
                    "title": item.title,
                    "evidence_type": item.evidence_type,
                    "sha256": item.sha256,
                    "artifact_available": bool(item.artifact_id),
                    "finding_id": item.finding_id,
                    "asset_ids": item.asset_ids,
                    "revision": item.revision,
                }
                for item in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self._project_record(Evidence, arguments["evidence_id"])
        assert isinstance(item, Evidence)
        return {
            "evidence_id": item.id,
            "title": item.title,
            "description": item.description[:20000],
            "evidence_type": item.evidence_type,
            "sha256": item.sha256,
            "size": item.metadata.get("size"),
            "filename": item.metadata.get("filename"),
            "artifact_available": bool(item.artifact_id),
            "finding_id": item.finding_id,
            "asset_ids": item.asset_ids,
            "revision": item.revision,
        }

    def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = arguments["kind"]
        offset = arguments.get("offset", 0)
        rows: list[Finding] | list[Asset]
        if kind == "finding":
            rows = self.store.list_entities(
                Finding, engagement_id=self.project_id, offset=offset, limit=51
            )
        else:
            rows = self.store.list_entities(
                Asset, engagement_id=self.project_id, offset=offset, limit=51
            )
        return {
            "kind": kind,
            "items": [
                {
                    "id": item.id,
                    "name": item.title if isinstance(item, Finding) else item.name,
                    "revision": item.revision,
                }
                for item in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments.get("title")
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("evidence title cannot be blank")
        finding_id = arguments.get("finding_id")
        if finding_id:
            self._project_record(Finding, finding_id)
        asset_ids = arguments.get("asset_ids", [])
        for asset_id in asset_ids:
            self._project_record(Asset, asset_id)
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "evidence_upload",
                "project_id": self.project_id,
                "title": title,
                "description": arguments.get("description", ""),
                "evidence_type": arguments.get("evidence_type", "operator_upload"),
                "finding_id": finding_id,
                "asset_ids": asset_ids,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.evidence.list",
                "Page bounded metadata for immutable evidence in the selected project; no artifact bytes are returned.",
                _schema({"offset": _PAGE}),
                RiskClass.LOCAL_READ,
                self.list_evidence,
            ),
            (
                "nebula.evidence.detail",
                "Read one selected-project evidence record, hash and links without its file bytes.",
                _schema({"evidence_id": _ID}, "evidence_id"),
                RiskClass.LOCAL_READ,
                self.detail,
            ),
            (
                "nebula.evidence.targets",
                "Page project findings or assets before linking an uploaded evidence file.",
                _schema(
                    {
                        "kind": {"type": "string", "enum": ["finding", "asset"]},
                        "offset": _PAGE,
                    },
                    "kind",
                ),
                RiskClass.LOCAL_READ,
                self.targets,
            ),
            (
                "nebula.evidence.upload_from_device",
                "Ask the requesting browser to choose and upload a local file as immutable evidence in the selected project. The model supplies optional title and same-project links, never file bytes. Core's saved hash, size and links must be read back; inspect Evidence before retrying an uncertain upload.",
                _schema(
                    {
                        "title": {"type": "string", "maxLength": 500},
                        "description": {"type": "string", "maxLength": 20000},
                        "evidence_type": {
                            "type": "string",
                            "enum": [
                                "operator_upload",
                                "scanner_output",
                                "http_exchange",
                                "certificate",
                                "image",
                                "other",
                            ],
                        },
                        "finding_id": _ID,
                        "asset_ids": {
                            "type": "array",
                            "items": _ID,
                            "maxItems": 500,
                            "uniqueItems": True,
                        },
                    }
                ),
                RiskClass.WORKSPACE_WRITE,
                self.add,
            ),
        )
        plugins: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any], *, selected_handler: Any = handler
            ) -> dict[str, Any]:
                return selected_handler(arguments)

            plugins.append(
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
        return plugins
