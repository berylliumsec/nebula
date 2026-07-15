"""Deterministic, offline PDF rendering for saved report revisions."""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    gather_diagnostic,
    record_caught_exception,
)

import asyncio
import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .artifacts import ArtifactStore
from .domain import (
    Artifact,
    Asset,
    Engagement,
    Evidence,
    Finding,
    Observation,
    Remediation,
    Report,
    ReportRender,
    ReportRenderStatus,
    ScopePolicy,
    utc_now,
)
from .storage import NebulaStore, NotFoundError

TEMPLATE_VERSION = "nebula-report/v1"
RENDERER_VERSION = f"reportlab-{reportlab.Version}/nebula-1"
FONT_FILES = {
    "regular": "NotoSans-Regular.ttf",
    "bold": "NotoSans-Bold.ttf",
    "mono": "NotoSansMono-Regular.ttf",
    "mono_bold": "NotoSansMono-Bold.ttf",
}


class ReportRenderError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class ReportRenderService:
    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        operator_id: Callable[[], str] | None = None,
        font_root: str | Path | None = None,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.operator_id = operator_id or (lambda: "system")
        self.font_root = Path(
            font_root or Path(__file__).parent / "report_assets" / "fonts"
        )
        self.font_paths = {
            key: self.font_root / name for key, name in FONT_FILES.items()
        }
        missing = [str(path) for path in self.font_paths.values() if not path.is_file()]
        if missing:
            raise ReportRenderError(
                "renderer_unavailable",
                "bundled report fonts are missing: " + ", ".join(missing),
                status_code=503,
            )
        self.font_hashes = {
            key: _file_sha256(path) for key, path in self.font_paths.items()
        }
        suffix = self.font_hashes["regular"][:12]
        self.font_names = {
            "regular": f"NebulaNotoSans-{suffix}",
            "bold": f"NebulaNotoSansBold-{suffix}",
            "mono": f"NebulaNotoSansMono-{suffix}",
            "mono_bold": f"NebulaNotoSansMonoBold-{suffix}",
        }
        for key, name in self.font_names.items():
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(self.font_paths[key])))
        regular_font = pdfmetrics.getFont(self.font_names["regular"])
        self.supported_codepoints = set(regular_font.face.charToGlyph)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    async def startup(self) -> None:
        for render in self._all_renders():
            if render.status not in {
                ReportRenderStatus.QUEUED,
                ReportRenderStatus.RENDERING,
            }:
                continue
            updated = self.store.update(
                ReportRender,
                render.id,
                {
                    "status": ReportRenderStatus.INTERRUPTED,
                    "error_detail": "Core restarted while the report was rendering",
                },
                expected_revision=render.revision,
            )
            self._event(updated, "report_render.interrupted", {"status": "interrupted"})

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await gather_diagnostic(
                *tasks,
                feature="reports",
                event_code="reports.shutdown.render_failed",
                failure_message="A report render task failed during shutdown.",
                stage="shutdown",
            )
        self._tasks.clear()

    async def request_render(
        self, report_id: str, *, report_revision: int
    ) -> ReportRender:
        async with self._lock:
            report = self.store.get(Report, report_id)
            if report.revision != report_revision:
                raise ReportRenderError(
                    "report_revision_stale",
                    "save the report revision before exporting PDF",
                )
            snapshot = self._canonical_snapshot(report)
            snapshot_bytes = _canonical_json(snapshot)
            fingerprint = hashlib.sha256(snapshot_bytes).hexdigest()
            for prior in self._all_renders(report.engagement_id):
                if (
                    prior.input_fingerprint == fingerprint
                    and prior.status == ReportRenderStatus.COMPLETED
                    and prior.pdf_artifact_id
                ):
                    artifact = self.store.get(Artifact, prior.pdf_artifact_id)
                    if self.artifact_store.verify(artifact):
                        return prior
            snapshot_artifact = self.artifact_store.put_bytes_with_status(
                snapshot_bytes,
                engagement_id=report.engagement_id,
                filename=f"report-{report.id}-r{report.revision}-snapshot.json",
                media_type="application/json",
                source="report-render-snapshot",
                metadata={
                    "report_id": report.id,
                    "report_revision": report.revision,
                    "input_fingerprint": fingerprint,
                },
            )
            render = ReportRender(
                engagement_id=report.engagement_id,
                report_id=report.id,
                report_revision=report.revision,
                input_fingerprint=fingerprint,
                template_version=TEMPLATE_VERSION,
                renderer_version=RENDERER_VERSION,
                font_hashes=self.font_hashes,
                snapshot_artifact_id=snapshot_artifact.artifact.id,
            )
            try:
                self.store.create_many([snapshot_artifact.artifact, render])
            except Exception as caught_error:
                record_caught_exception(
                    "reports",
                    "reports.reporting.caught_failure_001",
                    "A handled reports operation raised an exception.",
                    caught_error,
                    stage="reporting",
                )
                self.artifact_store.discard_new_blob(snapshot_artifact)
                raise
            self._event(
                render,
                "report_render.queued",
                {
                    "status": render.status.value,
                    "report_id": report.id,
                    "report_revision": report.revision,
                    "input_fingerprint": fingerprint,
                },
            )
            task = create_diagnostic_task(
                self._render(render.id, snapshot),
                feature="reports",
                event_code="reports.pdf_render",
                failure_message="The report renderer stopped unexpectedly.",
                name=f"report-render-{render.id}",
            )
            self._tasks[render.id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(render.id, None))
            return render

    def pdf(self, render_id: str) -> tuple[Artifact, Path]:
        render = self.store.get(ReportRender, render_id)
        if render.status != ReportRenderStatus.COMPLETED or not render.pdf_artifact_id:
            raise ReportRenderError(
                "render_not_ready", "report PDF is not ready", status_code=409
            )
        artifact = self.store.get(Artifact, render.pdf_artifact_id)
        if not self.artifact_store.verify(artifact):
            raise ReportRenderError(
                "render_integrity", "report PDF failed integrity verification"
            )
        return artifact, self.artifact_store.path_for(artifact)

    async def _render(self, render_id: str, snapshot: dict[str, Any]) -> None:
        render = self.store.get(ReportRender, render_id)
        try:
            render = self.store.update(
                ReportRender,
                render.id,
                {"status": ReportRenderStatus.RENDERING},
                expected_revision=render.revision,
            )
            self._event(render, "report_render.rendering", {"status": "rendering"})
            pdf_bytes, warnings = await asyncio.to_thread(self._build_pdf, snapshot)
            stored = self.artifact_store.put_bytes_with_status(
                pdf_bytes,
                engagement_id=render.engagement_id,
                filename=f"report-{render.report_id}-r{render.report_revision}.pdf",
                media_type="application/pdf",
                source="server-rendered-report",
                metadata={
                    "report_id": render.report_id,
                    "report_revision": render.report_revision,
                    "input_fingerprint": render.input_fingerprint,
                    "template_version": TEMPLATE_VERSION,
                    "renderer_version": RENDERER_VERSION,
                    "font_hashes": self.font_hashes,
                },
            )
            with self.store.transaction() as transaction:
                transaction.add(stored.artifact)
                completed = transaction.update(
                    ReportRender,
                    render.id,
                    {
                        "status": ReportRenderStatus.COMPLETED,
                        "pdf_artifact_id": stored.artifact.id,
                        "warnings": warnings,
                        "generated_at": utc_now(),
                        "error_detail": None,
                    },
                    expected_revision=render.revision,
                )
            self._event(
                completed,
                "report_render.completed",
                {
                    "status": "completed",
                    "pdf_artifact_id": stored.artifact.id,
                    "warnings": warnings,
                },
            )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "reports",
                "reports.reporting.caught_failure_002",
                "A handled reports operation raised an exception.",
                caught_error,
                stage="reporting",
            )
            current = self.store.get(ReportRender, render_id)
            if current.status in {
                ReportRenderStatus.QUEUED,
                ReportRenderStatus.RENDERING,
            }:
                interrupted = self.store.update(
                    ReportRender,
                    current.id,
                    {
                        "status": ReportRenderStatus.INTERRUPTED,
                        "error_detail": (
                            "Core shut down while rendering"
                            if self._shutting_down
                            else "report rendering was interrupted"
                        ),
                    },
                    expected_revision=current.revision,
                )
                self._event(
                    interrupted,
                    "report_render.interrupted",
                    {"status": "interrupted"},
                )
            raise
        except Exception as exc:
            record_caught_exception(
                "reports",
                "reports.reporting.caught_failure_003",
                "A handled reports operation raised an exception.",
                exc,
                stage="reporting",
            )
            current = self.store.get(ReportRender, render_id)
            if current.status in {
                ReportRenderStatus.QUEUED,
                ReportRenderStatus.RENDERING,
            }:
                failed = self.store.update(
                    ReportRender,
                    current.id,
                    {
                        "status": ReportRenderStatus.FAILED,
                        "error_detail": str(exc)[:4000],
                    },
                    expected_revision=current.revision,
                )
                self._event(
                    failed,
                    "report_render.failed",
                    {"status": "failed", "detail": str(exc)[:1000]},
                )

    def _canonical_snapshot(self, report: Report) -> dict[str, Any]:
        engagement = self.store.get(Engagement, report.engagement_id)
        scope = (
            self.store.get(ScopePolicy, engagement.scope_policy_id)
            if engagement.scope_policy_id
            else None
        )
        findings = [
            self._owned(Finding, finding_id, engagement.id, source="report finding")
            for finding_id in report.finding_ids
        ]
        observations = [
            self._owned(
                Observation, observation_id, engagement.id, source="report observation"
            )
            for observation_id in report.observation_ids
        ]
        asset_ids = {
            asset_id
            for item in [*findings, *observations]
            for asset_id in item.asset_ids
        }
        assets = [
            self._owned(Asset, asset_id, engagement.id, source="report content")
            for asset_id in sorted(asset_ids)
        ]
        remediation_ids = {
            finding.remediation_id for finding in findings if finding.remediation_id
        }
        remediations = [
            self._owned(
                Remediation, remediation_id, engagement.id, source="report finding"
            )
            for remediation_id in sorted(remediation_ids)
        ]
        evidence_ids = {
            evidence_id
            for item in [*findings, *observations]
            for evidence_id in item.evidence_ids
        }
        evidence = [
            self._owned(Evidence, evidence_id, engagement.id, source="report content")
            for evidence_id in sorted(evidence_ids)
        ]
        artifact_ids = set(report.artifact_ids)
        artifact_ids.update(item.artifact_id for item in evidence if item.artifact_id)
        artifacts: list[Artifact] = []
        for artifact_id in sorted(artifact_ids):
            artifact = self._owned(
                Artifact, artifact_id, engagement.id, source="report content"
            )
            if not self.artifact_store.verify(artifact):
                raise ReportRenderError(
                    "artifact_integrity",
                    f"required artifact failed integrity verification: {artifact.id}",
                )
            artifacts.append(artifact)
        artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
        for item in evidence:
            if item.artifact_id:
                artifact = artifacts_by_id[item.artifact_id]
                if item.sha256 and item.sha256 != artifact.sha256:
                    raise ReportRenderError(
                        "artifact_integrity",
                        f"evidence hash does not match artifact: {item.id}",
                    )
        return {
            "protocol": "nebula.report-snapshot/v1",
            "template_version": TEMPLATE_VERSION,
            "renderer_version": RENDERER_VERSION,
            "font_hashes": self.font_hashes,
            "report": report.model_dump(mode="json"),
            "engagement": engagement.model_dump(mode="json"),
            "scope_policy": scope.model_dump(mode="json") if scope else None,
            "findings": [item.model_dump(mode="json") for item in findings],
            "observations": [item.model_dump(mode="json") for item in observations],
            "assets": [item.model_dump(mode="json") for item in assets],
            "remediations": [item.model_dump(mode="json") for item in remediations],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "artifacts": [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "sha256": item.sha256,
                    "size": item.size,
                    "filename": item.filename,
                    "media_type": item.media_type,
                }
                for item in artifacts
            ],
        }

    def _owned(
        self,
        model: type[Any],
        entity_id: str,
        engagement_id: str,
        *,
        source: str,
    ) -> Any:
        try:
            entity = self.store.get(model, entity_id)
        except NotFoundError as exc:
            record_caught_exception(
                "reports",
                "reports.reporting.caught_failure_004",
                "A handled reports operation raised an exception.",
                exc,
                stage="reporting",
            )
            raise ReportRenderError(
                "missing_report_content",
                f"{source} references missing {model.entity_kind}: {entity_id}",
            ) from exc
        if entity.engagement_id != engagement_id:
            raise ReportRenderError(
                "missing_report_content",
                f"{source} belongs to a different engagement: {entity_id}",
            )
        return entity

    def _build_pdf(self, snapshot: dict[str, Any]) -> tuple[bytes, list[str]]:
        warnings: list[str] = []

        def safe(value: Any) -> str:
            text = "" if value is None else str(value)
            replaced = []
            unsupported: set[int] = set()
            for character in text:
                codepoint = ord(character)
                if (
                    character in {"\n", "\r", "\t"}
                    or codepoint in self.supported_codepoints
                ):
                    replaced.append(character)
                else:
                    unsupported.add(codepoint)
                    replaced.append("□")
            if unsupported:
                label = ", ".join(f"U+{value:04X}" for value in sorted(unsupported))
                warning = f"Unsupported glyphs replaced: {label}"
                if warning not in warnings:
                    warnings.append(warning)
            return "".join(replaced)

        def markup(value: Any) -> str:
            return (
                escape(safe(value))
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\n", "<br/>")
            )

        report = snapshot["report"]
        engagement = snapshot["engagement"]
        scope = snapshot["scope_policy"]
        findings = snapshot["findings"]
        observations = snapshot["observations"]
        assets = {item["id"]: item for item in snapshot["assets"]}
        remediations = {item["id"]: item for item in snapshot["remediations"]}
        evidence = snapshot["evidence"]
        artifacts = {item["id"]: item for item in snapshot["artifacts"]}
        styles = getSampleStyleSheet()
        body = ParagraphStyle(
            "NebulaBody",
            parent=styles["BodyText"],
            fontName=self.font_names["regular"],
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#263342"),
            spaceAfter=7,
        )
        heading = ParagraphStyle(
            "NebulaHeading",
            parent=styles["Heading2"],
            fontName=self.font_names["bold"],
            fontSize=15,
            leading=18,
            textColor=colors.HexColor("#10243A"),
            spaceBefore=14,
            spaceAfter=8,
        )
        subheading = ParagraphStyle(
            "NebulaSubheading",
            parent=heading,
            fontSize=11,
            leading=14,
            spaceBefore=9,
            spaceAfter=5,
        )
        title_style = ParagraphStyle(
            "NebulaTitle",
            parent=styles["Title"],
            fontName=self.font_names["bold"],
            fontSize=24,
            leading=29,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#10243A"),
        )
        label = ParagraphStyle(
            "NebulaLabel",
            parent=body,
            fontName=self.font_names["bold"],
            fontSize=7,
            leading=9,
            textColor=colors.HexColor("#58687A"),
        )
        mono = ParagraphStyle(
            "NebulaMono",
            parent=body,
            fontName=self.font_names["mono"],
            fontSize=7,
            leading=9,
            wordWrap="CJK",
        )
        story: list[Any] = [
            Paragraph(markup(report["title"]), title_style),
            Spacer(1, 8),
            Paragraph(
                markup(
                    f"Revision {report['revision']} · {report['status'].upper()} · "
                    f"Template {TEMPLATE_VERSION}"
                ),
                label,
            ),
            Spacer(1, 16),
            Paragraph("Engagement", heading),
        ]
        engagement_rows = [
            [Paragraph("Name", label), Paragraph(markup(engagement["name"]), body)],
            [
                Paragraph("Client", label),
                Paragraph(markup(engagement.get("client_name") or "—"), body),
            ],
            [Paragraph("Status", label), Paragraph(markup(engagement["status"]), body)],
            [
                Paragraph("Owner", label),
                Paragraph(markup(engagement.get("owner_id") or "—"), body),
            ],
        ]
        story.append(_fact_table(engagement_rows))
        story.append(Paragraph("Scope", heading))
        if scope:
            scope_lines = [
                f"CIDRs: {', '.join(scope.get('allowed_cidrs') or []) or 'none'}",
                f"Domains: {', '.join(scope.get('allowed_domains') or []) or 'none'}",
                f"URLs: {', '.join(scope.get('allowed_urls') or []) or 'none'}",
                f"Ports: {', '.join(str(value) for value in scope.get('allowed_ports') or []) or 'any in-scope'}",
                f"Execution window: {scope.get('not_before') or 'open'} to {scope.get('not_after') or 'open'}",
                f"Prohibited actions: {', '.join(scope.get('prohibited_actions') or []) or 'none recorded'}",
            ]
            story.append(Paragraph(markup("\n".join(scope_lines)), body))
        else:
            story.append(Paragraph("No scope policy is attached.", body))
        story.extend(
            [
                Paragraph("Executive summary", heading),
                Paragraph(
                    markup(report.get("executive_summary") or "No executive summary."),
                    body,
                ),
                Paragraph("Findings", heading),
            ]
        )
        if not findings:
            story.append(Paragraph("No findings selected for this revision.", body))
        for index, finding in enumerate(findings, start=1):
            asset_names = [
                assets.get(asset_id, {}).get("name", asset_id)
                for asset_id in finding.get("asset_ids", [])
            ]
            remediation = remediations.get(finding.get("remediation_id"))
            finding_story = [
                Paragraph(markup(f"{index}. {finding['title']}"), subheading),
                _fact_table(
                    [
                        [
                            Paragraph("Severity", label),
                            Paragraph(markup(finding["severity"]), body),
                        ],
                        [
                            Paragraph("Status", label),
                            Paragraph(markup(finding["status"]), body),
                        ],
                        [
                            Paragraph("Affected assets", label),
                            Paragraph(markup(", ".join(asset_names) or "—"), body),
                        ],
                    ]
                ),
                Paragraph(
                    markup(finding.get("description") or "No description."), body
                ),
                Paragraph("Remediation", label),
                Paragraph(
                    markup(
                        (remediation or {}).get("details")
                        or (remediation or {}).get("summary")
                        or "No remediation recorded."
                    ),
                    body,
                ),
            ]
            story.append(KeepTogether(finding_story))
        story.append(Paragraph("Selected notes", heading))
        if not observations:
            story.append(Paragraph("No notes selected for this revision.", body))
        for observation in observations:
            story.extend(
                [
                    Paragraph(markup(observation["title"]), subheading),
                    Paragraph(markup(observation.get("body") or ""), body),
                ]
            )
        story.append(PageBreak())
        story.append(Paragraph("Evidence index", heading))
        if not evidence:
            story.append(Paragraph("No evidence referenced by selected content.", body))
        for index, item in enumerate(evidence, start=1):
            artifact = artifacts.get(item.get("artifact_id"))
            digest = (
                item.get("sha256")
                or (artifact or {}).get("sha256")
                or "No artifact hash"
            )
            story.extend(
                [
                    Paragraph(markup(f"{index}. {item['title']}"), subheading),
                    Paragraph(markup(item.get("description") or ""), body),
                    Paragraph(markup(f"Evidence ID: {item['id']}"), mono),
                    Paragraph(markup(f"SHA-256: {digest}"), mono),
                ]
            )
        story.append(Paragraph("Signoff", heading))
        story.append(
            _fact_table(
                [
                    [
                        Paragraph("Report status", label),
                        Paragraph(markup(report["status"]), body),
                    ],
                    [
                        Paragraph("Signed off by", label),
                        Paragraph(
                            markup(report.get("signed_off_by") or "Not signed"), body
                        ),
                    ],
                    [
                        Paragraph("Signed off at", label),
                        Paragraph(markup(report.get("signed_off_at") or "—"), body),
                    ],
                    [
                        Paragraph("Snapshot fingerprint", label),
                        Paragraph(markup(_snapshot_fingerprint(snapshot)), mono),
                    ],
                ]
            )
        )
        output = io.BytesIO()
        page_width, page_height = LETTER
        document = BaseDocTemplate(
            output,
            pagesize=LETTER,
            leftMargin=0.7 * inch,
            rightMargin=0.7 * inch,
            topMargin=0.72 * inch,
            bottomMargin=0.65 * inch,
            title=safe(report["title"]),
            author="Nebula",
            subject=f"Nebula report revision {report['revision']}",
        )
        frame = Frame(
            document.leftMargin,
            document.bottomMargin,
            document.width,
            document.height,
            id="report-body",
        )

        def decorate(page_canvas: canvas.Canvas, doc: BaseDocTemplate) -> None:
            page_canvas.saveState()
            page_canvas.setFont(self.font_names["regular"], 7)
            page_canvas.setFillColor(colors.HexColor("#66778A"))
            page_canvas.drawString(
                document.leftMargin, 0.35 * inch, safe(engagement["name"])
            )
            page_canvas.drawRightString(
                page_width - document.rightMargin,
                0.35 * inch,
                f"Page {doc.page}",
            )
            if report["status"] in {"draft", "review"}:
                page_canvas.setFillColor(colors.Color(0.65, 0.1, 0.1, alpha=0.12))
                page_canvas.setFont(self.font_names["bold"], 52)
                page_canvas.translate(page_width / 2, page_height / 2)
                page_canvas.rotate(35)
                page_canvas.drawCentredString(0, 0, report["status"].upper())
            page_canvas.restoreState()

        document.addPageTemplates(
            [PageTemplate(id="report", frames=[frame], onPage=decorate)]
        )

        class InvariantCanvas(canvas.Canvas):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["invariant"] = 1
                kwargs["pageCompression"] = 1
                super().__init__(*args, **kwargs)

        document.build(story, canvasmaker=InvariantCanvas)
        return output.getvalue(), warnings

    def _event(
        self, render: ReportRender, event_type: str, payload: dict[str, Any]
    ) -> None:
        self.store.append_operation_event(
            render.id,
            "report_render",
            render.engagement_id,
            event_type,
            payload,
            actor_id=self.operator_id(),
            idempotency_key=(
                f"report-render:{render.id}:{event_type}"
                if event_type not in {"report_render.failed"}
                else None
            ),
        )

    def _all_renders(self, engagement_id: str | None = None) -> list[ReportRender]:
        result: list[ReportRender] = []
        offset = 0
        while True:
            page = self.store.list_entities(
                ReportRender,
                engagement_id=engagement_id,
                offset=offset,
                limit=1000,
            )
            result.extend(page)
            if len(page) < 1000:
                return result
            offset += len(page)


def _fact_table(rows: list[list[Any]]) -> Table:
    table = Table(rows, colWidths=[1.35 * inch, None], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF3F8")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D5DEE8")),
            ]
        )
    )
    return table


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(snapshot)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "FONT_FILES",
    "RENDERER_VERSION",
    "ReportRenderError",
    "ReportRenderService",
    "TEMPLATE_VERSION",
]
