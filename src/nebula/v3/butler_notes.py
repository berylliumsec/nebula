"""Project note discovery and exact requesting-browser mutation handoffs."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .domain import Asset, Evidence, Observation, RiskClass
from .relations import ResourceRelationService
from .storage import NebulaStore
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {"type": "integer", "minimum": 0, "maximum": 1000000}
_REVISION = {"type": "integer", "minimum": 1}
_REFERENCES = {"type": "array", "items": _ID, "maxItems": 500, "uniqueItems": True}
_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "body": {"type": "string", "maxLength": 100000},
    "asset_ids": _REFERENCES,
    "evidence_ids": _REFERENCES,
}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ButlerNotes:
    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id
        self.relations = ResourceRelationService(store)

    def _note(self, note_id: str) -> Observation:
        note = self.store.get(Observation, note_id)
        if note.engagement_id != self.project_id or note.observation_type not in {
            "note",
            "ai_tool_note",
            "ai_execution_note",
        }:
            raise ValueError("choose an editable note from the selected project")
        projected = self.relations.project_legacy(note)
        assert isinstance(projected, Observation)
        return projected

    @staticmethod
    def _summary(note: Observation) -> dict[str, Any]:
        return {
            "note_id": note.id,
            "title": note.title,
            "revision": note.revision,
            "note_type": note.observation_type,
            "body_length": len(note.body),
        }

    def notes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.find_entities(
            Observation,
            {"observation_type": ["note", "ai_tool_note", "ai_execution_note"]},
            engagement_id=self.project_id,
            offset=offset,
            limit=51,
        )
        return {
            "notes": [self._summary(note) for note in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note = self._note(arguments["note_id"])
        offset = arguments.get("body_offset", 0)
        end = min(offset + 8000, len(note.body))
        return {
            **self._summary(note),
            "body": note.body[offset:end],
            "body_offset": offset,
            "next_body_offset": end if end < len(note.body) else None,
            "asset_ids": note.asset_ids,
            "evidence_ids": note.evidence_ids,
        }

    def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows: list[Asset | Evidence] = []
        if arguments["kind"] == "asset":
            rows.extend(
                self.store.list_entities(
                    Asset, engagement_id=self.project_id, offset=offset, limit=51
                )
            )
        else:
            rows.extend(
                self.store.list_entities(
                    Evidence, engagement_id=self.project_id, offset=offset, limit=51
                )
            )
        return {
            "kind": arguments["kind"],
            "items": [
                {
                    "id": row.id,
                    "title": row.name if isinstance(row, Asset) else row.title,
                    "revision": row.revision,
                }
                for row in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def _changes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        changes = {key: arguments[key] for key in _FIELDS if key in arguments}
        if "title" in changes:
            changes["title"] = changes["title"].strip()
            if not changes["title"]:
                raise ValueError("note title cannot be blank")
        for field in ("asset_ids", "evidence_ids"):
            for item_id in changes.get(field, []):
                item: Asset | Evidence = (
                    self.store.get(Asset, item_id)
                    if field == "asset_ids"
                    else self.store.get(Evidence, item_id)
                )
                if item.engagement_id != self.project_id:
                    raise ValueError("note links must belong to the selected project")
        return changes

    def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "note_create",
                "project_id": self.project_id,
                "note_id": str(uuid4()),
                "body": "",
                "asset_ids": [],
                "evidence_ids": [],
                **self._changes(arguments),
            },
        }

    def change(
        self, arguments: dict[str, Any], *, delete: bool = False
    ) -> dict[str, Any]:
        note = self._note(arguments["note_id"])
        if note.revision != arguments["expected_revision"]:
            raise ValueError("note changed; read its current revision")
        changes = {} if delete else self._changes(arguments)
        if not delete and not changes:
            raise ValueError("provide a note field to change")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "note_delete" if delete else "note_update",
                "project_id": self.project_id,
                "note_id": note.id,
                "expected_revision": note.revision,
                "name": note.title,
                **changes,
            },
        }

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.change(arguments)

    def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.change(arguments, delete=True)

    def actions(self) -> list[AnalysisTool]:
        current = {"note_id": _ID, "expected_revision": _REVISION}
        operations = (
            (
                "nebula.notes.list",
                "Page editable selected-project Notes by title and current revision; body text is read separately.",
                _schema({"offset": _PAGE}),
                True,
                self.notes,
            ),
            (
                "nebula.notes.detail",
                "Read one editable project note's bounded body chunk and exact link IDs before changing it.",
                _schema({"note_id": _ID, "body_offset": _PAGE}, "note_id"),
                True,
                self.detail,
            ),
            (
                "nebula.notes.link_targets",
                "Page same-project assets or evidence by title before selecting note links.",
                _schema(
                    {
                        "kind": {"type": "string", "enum": ["asset", "evidence"]},
                        "offset": _PAGE,
                    },
                    "kind",
                ),
                True,
                self.targets,
            ),
            (
                "nebula.notes.create",
                "Ask the requesting browser to create a project note and verify its exact saved ID, fields and links. Notes record claims without verifying them.",
                _schema(_FIELDS, "title"),
                False,
                self.create,
            ),
            (
                "nebula.notes.update",
                "Ask the requesting browser to edit only supplied note fields at its current revision and read Core back.",
                _schema({**_FIELDS, **current}, "note_id", "expected_revision"),
                False,
                self.update,
            ),
            (
                "nebula.notes.delete",
                "Ask the requesting browser to confirm deletion of an exact project note at its current revision. Core refuses report-linked notes; source evidence is retained.",
                _schema(current, "note_id", "expected_revision"),
                False,
                self.delete,
            ),
        )
        result = []
        for name, description, schema, read, handler in operations:

            async def run(
                arguments: dict[str, Any],
                *,
                selected_handler: Any = handler,
                selected_read: bool = read,
            ) -> dict[str, Any]:
                if selected_read:
                    return await read_in_worker(selected_handler, arguments)
                return selected_handler(arguments)

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=RiskClass.LOCAL_READ
                        if read
                        else RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.SAFE
                        if read
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return result
