"""Project-bound report discovery and interactive browser handoffs."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .domain import (
    Engagement,
    Finding,
    Observation,
    Report,
    ReportRender,
    ReportStatus,
    RiskClass,
)
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_REVISION = {"type": "integer", "minimum": 1}
_PAGE = {"type": "integer", "minimum": 0, "maximum": 1000000}
_SUMMARY_OFFSET = {"type": "integer", "minimum": 0, "maximum": 100000}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 500}
_SUMMARY = {"type": "string", "maxLength": 100000}
_REFERENCES = {"type": "array", "items": _ID, "maxItems": 500, "uniqueItems": True}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_REPORT = _schema({"report_id": _ID, "summary_offset": _SUMMARY_OFFSET}, "report_id")
_CURRENT = _schema(
    {"report_id": _ID, "expected_revision": _REVISION},
    "report_id",
    "expected_revision",
)
_CREATE = _schema(
    {
        "title": _TITLE,
        "executive_summary": _SUMMARY,
        "finding_ids": _REFERENCES,
        "observation_ids": _REFERENCES,
    },
    "title",
)
_UPDATE = _schema(
    {
        "report_id": _ID,
        "expected_revision": _REVISION,
        "title": _TITLE,
        "status": {"type": "string", "enum": ["draft", "review"]},
        "executive_summary": _SUMMARY,
        "finding_ids": _REFERENCES,
        "observation_ids": _REFERENCES,
    },
    "report_id",
    "expected_revision",
)


class ButlerReports:
    """Prepare actions without allowing the model to bypass Core's report API."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(report: Report) -> dict[str, Any]:
        return {
            "report_id": report.id,
            "title": report.title,
            "status": report.status.value,
            "revision": report.revision,
            "finding_count": len(report.finding_ids),
            "note_count": len(report.observation_ids),
            "url": f"/projects/{report.engagement_id}/reports/{report.id}",
        }

    def _report(self, report_id: str) -> Report:
        report = self.store.get(Report, report_id)
        if report.engagement_id != self.project_id:
            raise ValueError("report is outside the selected project")
        return report

    def _current(self, arguments: dict[str, Any]) -> Report:
        report = self._report(arguments["report_id"])
        if report.revision != arguments["expected_revision"]:
            raise ValueError("report changed; read its current revision before acting")
        return report

    def _references(self, changes: dict[str, Any]) -> None:
        for field in ("finding_ids", "observation_ids"):
            for entity_id in changes.get(field, []):
                entity: Finding | Observation
                if field == "finding_ids":
                    entity = self.store.get(Finding, entity_id)
                else:
                    entity = self.store.get(Observation, entity_id)
                if entity.engagement_id != self.project_id:
                    raise ValueError(
                        f"{field} includes an item outside the selected project"
                    )

    def list_reports(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.list_entities(
            Report, engagement_id=self.project_id, offset=offset, limit=51
        )
        return {
            "reports": [self._summary(item) for item in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def sources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = arguments["kind"]
        offset = arguments.get("offset", 0)
        rows: list[Finding | Observation] = []
        if kind == "finding":
            rows.extend(
                self.store.list_entities(
                    Finding, engagement_id=self.project_id, offset=offset, limit=51
                )
            )
        else:
            rows.extend(
                self.store.list_entities(
                    Observation, engagement_id=self.project_id, offset=offset, limit=51
                )
            )
        return {
            "kind": kind,
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "revision": item.revision,
                    **(
                        {"status": item.status.value}
                        if isinstance(item, Finding)
                        else {}
                    ),
                }
                for item in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = self._report(arguments["report_id"])
        offset = arguments.get("summary_offset", 0)
        summary = report.executive_summary
        end = min(offset + 8000, len(summary))
        return {
            **self._summary(report),
            "executive_summary": summary[offset:end],
            "summary_offset": offset,
            "next_summary_offset": end if end < len(summary) else None,
            "finding_ids": report.finding_ids,
            "observation_ids": report.observation_ids,
            "transformed_note_ids": [
                item.observation_id for item in report.note_transforms
            ],
            "signed_off_by": report.signed_off_by,
            "signed_off_at": (
                report.signed_off_at.isoformat() if report.signed_off_at else None
            ),
        }

    def create(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = arguments["title"].strip()
        if not title:
            raise ValueError("report title cannot be blank")
        findings = arguments.get("finding_ids")
        if findings is None:
            findings = []
            offset = 0
            while True:
                batch = self.store.list_entities(
                    Finding, engagement_id=self.project_id, offset=offset, limit=1000
                )
                findings.extend(
                    finding.id
                    for finding in batch
                    if finding.status.value in {"validated", "confirmed"}
                )
                if len(findings) > 500:
                    raise ValueError("choose at most 500 findings for the report")
                if len(batch) < 1000:
                    break
                offset += len(batch)
        changes = {
            "title": title,
            "executive_summary": arguments.get("executive_summary", ""),
            "finding_ids": findings,
            "observation_ids": arguments.get("observation_ids", []),
        }
        self._references(changes)
        return self._pending(
            {
                "kind": "report_create",
                "project_id": self.project_id,
                "report_id": str(uuid4()),
                **changes,
            }
        )

    def update(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = self._current(arguments)
        if report.status == ReportStatus.FINAL:
            raise ValueError("final reports are immutable; create a new draft")
        changes = {
            field: arguments[field]
            for field in (
                "title",
                "status",
                "executive_summary",
                "finding_ids",
                "observation_ids",
            )
            if field in arguments
        }
        if not changes:
            raise ValueError("provide at least one report field to change")
        if "title" in changes:
            changes["title"] = changes["title"].strip()
            if not changes["title"]:
                raise ValueError("report title cannot be blank")
        self._references(changes)
        return self._pending(
            {
                "kind": "report_update",
                "project_id": self.project_id,
                "report_id": report.id,
                "expected_revision": report.revision,
                **changes,
            }
        )

    def signoff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = self._current(arguments)
        if report.status != ReportStatus.REVIEW:
            raise ValueError("save a report in review before requesting signoff")
        return self._pending(
            {
                "kind": "report_signoff",
                "project_id": self.project_id,
                "report_id": report.id,
                "expected_revision": report.revision,
            }
        )

    def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = self._current(arguments)
        if report.status == ReportStatus.FINAL:
            raise ValueError("a signed final report cannot be deleted")
        if self.store.find_entities(
            ReportRender,
            {"report_id": report.id},
            engagement_id=self.project_id,
            limit=1,
        ):
            raise ValueError("a report with a saved PDF render cannot be deleted")
        return self._pending(
            {
                "kind": "report_delete",
                "project_id": self.project_id,
                "report_id": report.id,
                "expected_revision": report.revision,
                "title": report.title,
            }
        )

    def export_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = self._current(arguments)
        return self._pending(
            {
                "kind": "report_export_pdf",
                "project_id": self.project_id,
                "report_id": report.id,
                "expected_revision": report.revision,
            }
        )

    def export_bundle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self.store.get(Engagement, self.project_id)
        if project.revision != arguments["expected_project_revision"]:
            raise ValueError("project changed; read its current revision before export")
        return self._pending(
            {
                "kind": "report_export_bundle",
                "project_id": self.project_id,
                "expected_project_revision": project.revision,
            }
        )

    @staticmethod
    def _pending(action: dict[str, Any]) -> dict[str, Any]:
        return {"state": "pending_browser_apply", "client_action": action}

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.reports.list",
                "Page saved reports in the selected project.",
                _schema({"offset": _PAGE}),
                RiskClass.LOCAL_READ,
                self.list_reports,
            ),
            (
                "nebula.reports.sources",
                "Page exact project findings or notes before selecting report references.",
                _schema(
                    {
                        "kind": {"type": "string", "enum": ["finding", "note"]},
                        "offset": _PAGE,
                    },
                    "kind",
                ),
                RiskClass.LOCAL_READ,
                self.sources,
            ),
            (
                "nebula.reports.detail",
                "Read an exact report, its current revision, selected references and paged executive summary.",
                _REPORT,
                RiskClass.LOCAL_READ,
                self.detail,
            ),
            (
                "nebula.reports.create",
                "Create a draft report through the requesting browser and Core validation after an explicit operator request. Omitted finding IDs default to validated and confirmed project findings. Inspect reports after an uncertain response before retrying.",
                _CREATE,
                RiskClass.WORKSPACE_WRITE,
                self.create,
            ),
            (
                "nebula.reports.update",
                "Edit a non-final report through the requesting browser with its current revision; Core validates project references and reads back the saved fields.",
                _UPDATE,
                RiskClass.WORKSPACE_WRITE,
                self.update,
            ),
            (
                "nebula.reports.signoff",
                "Open the exact review report's existing signoff form. The operator must personally choose identity and attest; this action does not finalize the report.",
                _CURRENT,
                RiskClass.WORKSPACE_WRITE,
                self.signoff,
            ),
            (
                "nebula.reports.delete",
                "Ask the requesting browser to confirm permanent deletion of an exact non-final, unrendered report at its current revision. Core retains the source findings and notes; signed final reports and reports with a saved PDF render cannot be deleted. After an uncertain response, inspect the exact report before retrying.",
                _CURRENT,
                RiskClass.WORKSPACE_WRITE,
                self.delete,
            ),
            (
                "nebula.reports.export_pdf",
                "Render an exact saved report revision and start a PDF download on the requesting device. Butler receives no PDF bytes.",
                _CURRENT,
                RiskClass.WORKSPACE_WRITE,
                self.export_pdf,
            ),
            (
                "nebula.reports.export_bundle",
                "Ask the requesting browser to confirm a sensitive full-project bundle export at the current project revision. Butler receives no bundle bytes.",
                _schema(
                    {"expected_project_revision": _REVISION},
                    "expected_project_revision",
                ),
                RiskClass.WORKSPACE_WRITE,
                self.export_bundle,
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
