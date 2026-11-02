"""Selected-project inventory and finding records through their Core validators."""

from __future__ import annotations

import hashlib
import json
from functools import partial
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .api_validation import ApiEntityValidator
from .domain import Asset, Engagement, Finding, FindingStatus, RiskClass
from .relations import ResourceRelationService
from .storage import ConflictError, NebulaStore, NotFoundError
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {"type": "integer", "minimum": 0, "maximum": 1000000}
_REFERENCES = {"type": "array", "items": _ID, "uniqueItems": True, "maxItems": 500}
_SEVERITY = {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]}
_ASSET_FIELDS: dict[str, Any] = {
    "name": {"type": "string", "minLength": 1, "maxLength": 500},
    "asset_type": {"type": "string", "minLength": 1, "maxLength": 100},
    "address": {"type": ["string", "null"], "maxLength": 2048},
    "hostname": {"type": ["string", "null"], "maxLength": 2048},
    "criticality": _SEVERITY,
    "exposed": {"type": ["boolean", "null"]},
    "tags": {
        "type": "array",
        "items": {"type": "string", "maxLength": 256},
        "maxItems": 50,
        "uniqueItems": True,
    },
}
_FINDING_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "description": {"type": "string", "maxLength": 100000},
    "severity": _SEVERITY,
    "severity_rationale": {"type": "string", "maxLength": 10000},
    "asset_ids": _REFERENCES,
    "evidence_ids": _REFERENCES,
    "cve_ids": {
        "type": "array",
        "items": {"type": "string", "pattern": r"^[Cc][Vv][Ee]-[0-9]{4}-[0-9]{4,}$"},
        "maxItems": 500,
        "uniqueItems": True,
    },
    "cwe_ids": {
        "type": "array",
        "items": {"type": "string", "pattern": r"^[Cc][Ww][Ee]-[0-9]+$"},
        "maxItems": 500,
        "uniqueItems": True,
    },
}
_OPERATIONS = {
    "assets": {
        "list": "nebula.assets.list",
        "detail": "nebula.assets.detail",
        "create": "nebula.assets.create",
    },
    "findings": {
        "list": "nebula.findings.list",
        "detail": "nebula.findings.detail",
        "create": "nebula.findings.create",
        "update": "nebula.findings.update",
    },
}

_REQUEST_ID = {
    "type": "string",
    "minLength": 8,
    "maxLength": 120,
    "pattern": r"^[A-Za-z0-9_.:-]+$",
    "description": "Choose one stable request key for this creation; reuse it unchanged when retrying. Core derives the record identity.",
}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ButlerRecords:
    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id
        self.validator = ApiEntityValidator(store)
        self.relations = ResourceRelationService(store)

    def _row(self, kind: str, record_id: str) -> Asset | Finding:
        row: Asset | Finding = (
            self.store.get(Asset, record_id)
            if kind == "assets"
            else self.store.get(Finding, record_id)
        )
        if row.engagement_id != self.project_id:
            raise ValueError("choose a record from the selected project")
        projected = self.relations.project_legacy(row)
        assert isinstance(projected, (Asset, Finding))
        return projected

    @staticmethod
    def _summary(row: Asset | Finding) -> dict[str, Any]:
        result: dict[str, Any] = {"id": row.id, "revision": row.revision}
        if isinstance(row, Asset):
            result.update(
                name=row.name,
                asset_type=row.asset_type[:100],
                criticality=row.criticality.value,
            )
        else:
            result.update(
                title=row.title,
                status=row.status.value,
                severity=row.severity.value,
                description_length=len(row.description),
            )
        return result

    def list_records(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows: list[Asset] | list[Finding] = (
            self.store.list_entities(
                Asset, engagement_id=self.project_id, offset=offset, limit=51
            )
            if kind == "assets"
            else self.store.list_entities(
                Finding, engagement_id=self.project_id, offset=offset, limit=51
            )
        )
        return {
            "items": [self._summary(row) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        row = self._row(kind, arguments["record_id"])
        result = self._summary(row)
        if isinstance(row, Asset):
            result.update(
                address=row.address[:2048] if row.address else None,
                hostname=row.hostname[:2048] if row.hostname else None,
                exposed=row.exposed,
                tags=[tag[:256] for tag in row.tags[:50]],
                tags_truncated=len(row.tags) > 50,
            )
        else:
            offset = arguments.get("description_offset", 0)
            end = min(offset + 8000, len(row.description))
            result.update(
                description=row.description[offset:end],
                description_offset=offset,
                next_description_offset=end if end < len(row.description) else None,
                severity_rationale=row.severity_rationale[:10000],
            )
            for field in ("asset_ids", "evidence_ids", "cve_ids", "cwe_ids"):
                values = getattr(row, field)
                result[field] = values[:500]
                result[field + "_truncated"] = len(values) > 500
        return result

    def _may_write(self) -> None:
        if self.store.get(Engagement, self.project_id).status.value == "archived":
            raise ValueError("restore the selected project before changing its records")

    @staticmethod
    def _fields(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fields = _ASSET_FIELDS if kind == "assets" else _FINDING_FIELDS
        values = {key: arguments[key] for key in fields if key in arguments}
        for key in ("name", "title"):
            if key in values:
                values[key] = values[key].strip()
                if not values[key]:
                    raise ValueError("record title cannot be blank")
        return values

    def _receipt(
        self,
        kind: str,
        saved: Asset | Finding,
        fields: dict[str, Any],
        *,
        replayed: bool = False,
    ) -> dict[str, Any]:
        observed = self._row(kind, saved.id)
        actual = observed.model_dump(mode="json")
        if observed.revision < saved.revision or any(
            actual[field] != value for field, value in fields.items()
        ):
            raise ValueError(
                "Core wrote the record but its current fields differ; inspect it before retrying"
            )
        return {
            "state": "core_records_saved",
            "read_back": True,
            "replayed": replayed,
            "result": self._summary(observed),
            "client_action": {
                "kind": "records_refresh",
                "project_id": self.project_id,
                "resource": kind,
            },
        }

    def create(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._may_write()
        values = self._fields(kind, arguments)
        record_id = str(
            uuid5(
                NAMESPACE_URL,
                f"nebula:butler:{self.project_id}:{kind}:{arguments['request_id']}",
            )
        )
        payload = {"id": record_id, "engagement_id": self.project_id, **values}
        row: Asset | Finding = (
            Asset.model_validate(payload)
            if kind == "assets"
            else Finding.model_validate(payload)
        )
        keys = _ASSET_FIELDS if kind == "assets" else _FINDING_FIELDS
        fields = {
            key: value
            for key, value in row.model_dump(mode="json").items()
            if key in keys
        }
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        row.metadata = {"butler_create_digest": digest}
        self.validator.validate_create(row)
        current: Asset | Finding | None = None
        try:
            current = self._row(kind, record_id)
        except NotFoundError:
            # diagnostic-expected: the first call has no record at its derived identity.
            current = None
        if current is not None:
            if current.metadata.get("butler_create_digest") != digest:
                raise ValueError(
                    "this request key already names different record content; inspect it before retrying"
                )
            return self._receipt(kind, current, fields, replayed=True)
        try:
            saved = (
                self.store.create(row)
                if isinstance(row, Asset)
                else self.relations.create_legacy_entity(row)
            )
        except ConflictError:
            # diagnostic-expected: a concurrent retry may have created the same derived identity.
            concurrent = self._row(kind, record_id)
            if concurrent.metadata.get("butler_create_digest") != digest:
                raise ValueError(
                    "this request key already names different record content"
                )
            return self._receipt(kind, concurrent, fields, replayed=True)
        assert isinstance(saved, (Asset, Finding))
        return self._receipt(kind, saved, fields)

    def update_finding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._may_write()
        current = self._row("findings", arguments["record_id"])
        assert isinstance(current, Finding)
        if current.revision != arguments["expected_revision"]:
            raise ValueError("finding changed; read its current revision")
        if current.status == FindingStatus.CONFIRMED:
            raise ValueError(
                "confirmed findings retain operator attribution; open Findings to review this change"
            )
        fields = self._fields("findings", arguments)
        if "status" in arguments:
            if arguments["status"] == FindingStatus.CONFIRMED.value:
                raise ValueError(
                    "finding confirmation requires the operator's visible evidence and attribution control"
                )
            fields["status"] = arguments["status"]
        if not fields:
            raise ValueError("provide a finding field to change")
        candidate = Finding.model_validate(
            {**current.model_dump(mode="json"), **fields}
        )
        self.validator.validate_update(current, candidate)
        normalized = {
            field: candidate.model_dump(mode="json")[field] for field in fields
        }
        saved = self.relations.replace_legacy_entity(
            current, candidate, expected_revision=current.revision
        )
        assert isinstance(saved, Finding)
        return self._receipt("findings", saved, normalized)

    def actions(self) -> list[AnalysisTool]:
        result = []
        for kind in ("assets", "findings"):
            operations: list[tuple[str, str, dict[str, Any], bool, Any]] = [
                (
                    "list",
                    "Page selected-project record titles and current revisions; this does not start testing.",
                    _schema({"offset": _PAGE}),
                    True,
                    partial(self.list_records, kind),
                ),
                (
                    "detail",
                    "Read one selected-project record and bounded content before editing; a saved finding is an analyst record, not a validation run.",
                    _schema(
                        {
                            "record_id": _ID,
                            **(
                                {"description_offset": _PAGE}
                                if kind == "findings"
                                else {}
                            ),
                        },
                        "record_id",
                    ),
                    True,
                    partial(self.detail, kind),
                ),
                (
                    "create",
                    "Create one selected-project inventory asset or candidate finding using Core validation and exact readback. Reuse request_id for a retry; a changed request needs a new key. This records supplied information and never performs scanning or verification.",
                    _schema(
                        {
                            **(_ASSET_FIELDS if kind == "assets" else _FINDING_FIELDS),
                            "request_id": _REQUEST_ID,
                        },
                        "name" if kind == "assets" else "title",
                        "request_id",
                    ),
                    False,
                    partial(self.create, kind),
                ),
            ]
            if kind == "findings":
                operations.append(
                    (
                        "update",
                        "Edit only supplied fields of an unconfirmed finding at its observed revision, with same-project links and Core readback. Confirmed finding edits and personal verification stay in the visible Findings control.",
                        _schema(
                            {
                                **_FINDING_FIELDS,
                                "record_id": _ID,
                                "expected_revision": {"type": "integer", "minimum": 1},
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        status.value
                                        for status in FindingStatus
                                        if status != FindingStatus.CONFIRMED
                                    ],
                                },
                            },
                            "record_id",
                            "expected_revision",
                        ),
                        False,
                        self.update_finding,
                    )
                )
            for operation, description, schema, read, handler in operations:

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
                            name=_OPERATIONS[kind][operation],
                            description=description,
                            input_schema=schema,
                            output_schema={
                                "type": "object",
                                "additionalProperties": True,
                            },
                            risk_class=RiskClass.LOCAL_READ
                            if read
                            else RiskClass.WORKSPACE_WRITE,
                            idempotency=IdempotencyBehavior.SAFE
                            if read or operation == "create"
                            else IdempotencyBehavior.NON_IDEMPOTENT,
                        ),
                        run,
                    )
                )
        return result
