"""Project note discovery, Core-saved note edits and confirmed deletion handoffs."""

from __future__ import annotations

from .async_utils import call_async

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .api_validation import ApiEntityValidator
from .domain import Asset, Evidence, Observation, RiskClass
from .relations import ResourceRelationService
from .storage import ConflictError, NebulaStore, NotFoundError
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
_REQUEST_ID = {
    "type": "string",
    "minLength": 8,
    "maxLength": 120,
    "pattern": r"^[A-Za-z0-9_.:-]+$",
    "description": "Choose one stable request key for this note; reuse it unchanged when retrying. Core derives the note identity.",
}
_DELETE_PENDING = (
    "Not deleted. Deleting a note needs the operator to confirm it in the Butler "
    "panel of the Nebula web app; terminal and Workbench chats cannot show that "
    "confirmation. Ask the operator to confirm there or delete it from Project "
    "Notes, and read the note list before saying it was deleted."
)


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
        self.validator = ApiEntityValidator(store)

    async def _note(self, note_id: str) -> Observation:
        note = await self.store.get(Observation, note_id)
        if note.engagement_id != self.project_id or note.observation_type not in {
            "note",
            "ai_tool_note",
            "ai_execution_note",
        }:
            raise ValueError("choose an editable note from the selected project")
        projected = await self.relations.project_legacy(note)
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

    async def notes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = await self.store.find_entities(
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

    async def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note = await self._note(arguments["note_id"])
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

    async def targets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows: list[Asset | Evidence] = []
        if arguments["kind"] == "asset":
            rows.extend(
                (
                    await self.store.list_entities(
                        Asset, engagement_id=self.project_id, offset=offset, limit=51
                    )
                )
            )
        else:
            rows.extend(
                (
                    await self.store.list_entities(
                        Evidence, engagement_id=self.project_id, offset=offset, limit=51
                    )
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

    async def _changes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        changes = {key: arguments[key] for key in _FIELDS if key in arguments}
        if "title" in changes:
            changes["title"] = changes["title"].strip()
            if not changes["title"]:
                raise ValueError("note title cannot be blank")
        for field in ("asset_ids", "evidence_ids"):
            for item_id in changes.get(field, []):
                item: Asset | Evidence = (
                    (await self.store.get(Asset, item_id))
                    if field == "asset_ids"
                    else (await self.store.get(Evidence, item_id))
                )
                if item.engagement_id != self.project_id:
                    raise ValueError("note links must belong to the selected project")
        return changes

    @staticmethod
    def _fields(note: Observation, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            field: sorted(getattr(note, field))
            if field in ("asset_ids", "evidence_ids")
            else getattr(note, field)
            for field in fields
        }

    async def _receipt(
        self,
        saved: Observation,
        fields: dict[str, Any],
        *,
        operation: str,
        replayed: bool = False,
    ) -> dict[str, Any]:
        observed = await self._note(saved.id)
        if observed.revision < saved.revision or self._fields(
            observed, fields
        ) != self._fields(saved, fields):
            raise ValueError(
                "Core wrote the note but its current fields differ; inspect it before retrying"
            )
        return {
            "state": "core_records_saved",
            "read_back": True,
            "replayed": replayed,
            "operation": operation,
            "result": {
                **self._summary(observed),
                "asset_ids": observed.asset_ids,
                "evidence_ids": observed.evidence_ids,
            },
            "message": (
                f"Core {operation} note \u201c{observed.title}\u201d at revision "
                f"{observed.revision} and read its saved fields back."
            ),
            "client_action": {
                "kind": "note_saved",
                "project_id": self.project_id,
                "note_id": observed.id,
                "title": observed.title,
                "revision": observed.revision,
                "operation": operation,
            },
        }

    async def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        values = await self._changes(arguments)
        note = Observation(
            id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"nebula:butler:{self.project_id}:notes:{arguments['request_id']}",
                )
            ),
            engagement_id=self.project_id,
            observation_type="note",
            title=values["title"],
            body=values.get("body", ""),
            asset_ids=values.get("asset_ids", []),
            evidence_ids=values.get("evidence_ids", []),
            source="operator-note",
        )
        fields = self._fields(note, _FIELDS)
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        note.metadata = {"butler_create_digest": digest}

        async def replay() -> dict[str, Any]:
            current = await self._note(note.id)
            if current.metadata.get("butler_create_digest") != digest:
                raise ValueError(
                    "this request key already names different note content; inspect it before retrying"
                )
            return await self._receipt(
                current, fields, operation="created", replayed=True
            )

        try:
            await self.store.get(Observation, note.id)
        except NotFoundError:
            # diagnostic-expected: the first call has no note at its derived identity.
            pass
        else:
            return await replay()
        (await self.validator.validate_create(note))
        try:
            saved = await self.relations.create_legacy_entity(note)
        except ConflictError:
            # diagnostic-expected: a concurrent retry may have created the same derived identity.
            return await replay()
        assert isinstance(saved, Observation)
        return await self._receipt(saved, fields, operation="created")

    async def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note = await self._note(arguments["note_id"])
        if note.revision != arguments["expected_revision"]:
            raise ValueError("note changed; read its current revision")
        changes = await self._changes(arguments)
        if not changes:
            raise ValueError("provide a note field to change")
        candidate = Observation.model_validate(
            {**note.model_dump(mode="python"), **changes}
        )
        (await self.validator.validate_update(note, candidate))
        saved = await self.relations.replace_legacy_entity(
            note, candidate, expected_revision=note.revision
        )
        assert isinstance(saved, Observation)
        return await self._receipt(saved, changes, operation="updated")

    async def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note = await self._note(arguments["note_id"])
        if note.revision != arguments["expected_revision"]:
            raise ValueError("note changed; read its current revision")
        return {
            "state": "pending_browser_apply",
            "deleted": False,
            "operator_confirmation_required": True,
            "message": _DELETE_PENDING,
            "client_action": {
                "kind": "note_delete",
                "project_id": self.project_id,
                "note_id": note.id,
                "expected_revision": note.revision,
                "name": note.title,
            },
        }

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
                "Create one selected-project note in Core with same-project links and exact readback. Reuse request_id for a retry; a changed note needs a new key. Say the note is saved only when the result has read_back true. Notes record claims without verifying them.",
                _schema({**_FIELDS, "request_id": _REQUEST_ID}, "title", "request_id"),
                False,
                self.create,
            ),
            (
                "nebula.notes.update",
                "Edit only supplied note fields in Core at the observed revision, keeping other note metadata, and read the saved note back. Say it is saved only when the result has read_back true.",
                _schema({**_FIELDS, **current}, "note_id", "expected_revision"),
                False,
                self.update,
            ),
            (
                "nebula.notes.delete",
                "Ask the operator to confirm deleting an exact project note at its current revision in the Nebula web app's Butler panel. This call deletes nothing; terminal and Workbench chats cannot confirm it, so report the returned message and never say the note was deleted until a later read shows it absent. Core refuses report-linked notes; source evidence is retained.",
                _schema(current, "note_id", "expected_revision"),
                False,
                self.delete,
            ),
        )
        result = []
        for name, description, schema, read, handler in operations:
            idempotent = read or name == "nebula.notes.create"

            async def run(
                arguments: dict[str, Any],
                *,
                selected_handler: Any = handler,
                selected_read: bool = read,
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
                        risk_class=RiskClass.LOCAL_READ
                        if read
                        else RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.SAFE
                        if idempotent
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return result
