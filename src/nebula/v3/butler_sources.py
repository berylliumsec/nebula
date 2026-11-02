"""Project source discovery and browser-owned document transfers."""

from __future__ import annotations

from typing import Any

from .domain import Engagement, EngagementStatus, KnowledgeSource, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ButlerSources:
    def __init__(
        self, store: NebulaStore, project_id: str, knowledge_index: Any = None
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.knowledge_index = knowledge_index

    def index_status(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        if self.knowledge_index is None:
            return {
                "backend": "none",
                "state": "disabled",
                "model": "none",
                "total_bytes": 0,
            }
        return self.knowledge_index.status.model_dump(mode="json")

    @staticmethod
    def _summary(source: KnowledgeSource) -> dict[str, Any]:
        # Original bytes, index chunks and source URLs belong to their existing
        # retrieval/transfer owners, never the control-plane action arguments.
        return {
            "source_id": source.id,
            "name": source.name,
            "revision": source.revision,
            "status": source.status,
            "source_type": source.source_type,
            "document_count": source.document_count,
            "has_original": bool(source.artifact_id),
        }

    def list_sources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.list_entities(
            KnowledgeSource, engagement_id=self.project_id, offset=offset, limit=51
        )
        return {
            "sources": [self._summary(row) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def _source(self, source_id: str) -> KnowledgeSource:
        source = self.store.get(KnowledgeSource, source_id)
        if source.engagement_id != self.project_id:
            raise ValueError("choose a source from the selected project")
        return source

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._summary(self._source(arguments["source_id"]))

    def prepare(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self.store.get(Engagement, self.project_id)
        if operation != "download" and project.status == EngagementStatus.ARCHIVED:
            raise ValueError("restore the project before changing its sources")
        action: dict[str, Any] = {
            "kind": f"source_{operation}",
            "project_id": self.project_id,
        }
        if operation == "add":
            action["input_kind"] = arguments.get("input_kind", "file")
        else:
            source = self._source(arguments["source_id"])
            if source.revision != arguments["expected_revision"]:
                raise ValueError("source changed; read its current revision")
            if operation in {"download", "reindex"} and not source.artifact_id:
                raise ValueError("source has no original artifact for this action")
            action.update(
                source_id=source.id, expected_revision=source.revision, name=source.name
            )
        return {"state": "pending_browser_apply", "client_action": action}

    def actions(self) -> list[AnalysisTool]:
        target = {"source_id": _ID}
        revision = {**target, "expected_revision": {"type": "integer", "minimum": 1}}
        definitions = (
            (
                "nebula.sources.index_status",
                "Read Core's operator-safe local knowledge embedding and reranker preparation status without downloading or changing models.",
                _schema({}),
                True,
                self.index_status,
            ),
            (
                "nebula.sources.list",
                "Page selected-project Sources identities, index status and revisions without original bytes, URLs or index chunks.",
                _schema(
                    {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}}
                ),
                True,
                self.list_sources,
            ),
            (
                "nebula.sources.detail",
                "Read one selected-project source's metadata and current revision before a transfer or mutation.",
                _schema(target, "source_id"),
                True,
                self.detail,
            ),
            (
                "nebula.sources.add",
                "Ask the requesting browser to choose a file or enter a source URL for this project. Core verifies the saved record and list; this action never receives file bytes or URLs. Separate Knowledge retrieval can later use the indexed content.",
                _schema({"input_kind": {"type": "string", "enum": ["file", "url"]}}),
                False,
                None,
            ),
            (
                "nebula.sources.download",
                "Ask the requesting browser to confirm and download an exact source's original file. Bytes stay on that device.",
                _schema(revision, "source_id", "expected_revision"),
                False,
                None,
            ),
            (
                "nebula.sources.reindex",
                "Ask the requesting browser to confirm reindexing one exact project source at its current revision and verify Core's saved index status.",
                _schema(revision, "source_id", "expected_revision"),
                False,
                None,
            ),
            (
                "nebula.sources.remove",
                "Ask the requesting browser to confirm removal of one exact project source at its current revision. Core retains the immutable original artifact and verifies list absence.",
                _schema(revision, "source_id", "expected_revision"),
                False,
                None,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, read_only, handler in definitions:

            async def run(
                arguments: dict[str, Any],
                *,
                selected: str = name.rsplit(".", 1)[-1],
                selected_handler: Any = handler,
            ) -> dict[str, Any]:
                return (
                    selected_handler(arguments)
                    if selected_handler
                    else self.prepare(selected, arguments)
                )

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=RiskClass.LOCAL_READ
                        if read_only
                        else RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.SAFE
                        if read_only
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return result
