import asyncio
import base64

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    Finding,
    FindingStatus,
    Report,
    ReportRender,
    ReportRenderStatus,
    ReportStatus,
)
from nebula.v3.evidence import EvidenceUploadRequest, upload_evidence
from nebula.v3.operators import OperatorProfileService
from nebula.v3.reporting import ReportRenderService
from nebula.v3.report_signoff import ReportSignoffRequest, sign_off_report
from nebula.v3.storage import ConflictError, NebulaStore


def test_signoff_finalizes_review_with_active_operator_and_validated_findings(tmp_path):
    store = NebulaStore(tmp_path / "signoff.db")
    engagement = store.create(Engagement(name="Report project"))
    finding = store.create(
        Finding(
            engagement_id=engagement.id,
            title="Validated issue",
            status=FindingStatus.VALIDATED,
        )
    )
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="Final report",
            status=ReportStatus.REVIEW,
            finding_ids=[finding.id],
        )
    )
    operator = OperatorProfileService(store).create_profile(display_name="Alex Reviewer")

    signed = sign_off_report(
        store,
        report.id,
        ReportSignoffRequest(
            expected_revision=report.revision,
            operator_id=operator.id,
            attestation="Reviewed and approved.",
        ),
    )

    assert signed.status == ReportStatus.FINAL
    assert signed.signed_off_by == operator.id
    assert signed.signed_off_at is not None
    assert signed.metadata["signoff"]["operator_display_name"] == "Alex Reviewer"
    assert signed.metadata["signoff"]["attestation"] == "Reviewed and approved."


def test_signoff_rejects_draft_stale_and_candidate_finding(tmp_path):
    store = NebulaStore(tmp_path / "invalid-signoff.db")
    engagement = store.create(Engagement(name="Report project"))
    finding = store.create(Finding(engagement_id=engagement.id, title="Candidate"))
    operator = OperatorProfileService(store).create_profile(display_name="Reviewer")
    draft = store.create(
        Report(engagement_id=engagement.id, title="Draft", finding_ids=[finding.id])
    )

    with pytest.raises(ConflictError, match="in review"):
        sign_off_report(
            store,
            draft.id,
            ReportSignoffRequest(expected_revision=draft.revision, operator_id=operator.id),
        )

    review = store.update(
        Report,
        draft.id,
        {"status": ReportStatus.REVIEW},
        expected_revision=draft.revision,
    )
    with pytest.raises(ConflictError, match="revision conflict"):
        sign_off_report(
            store,
            review.id,
            ReportSignoffRequest(expected_revision=1, operator_id=operator.id),
        )
    with pytest.raises(ConflictError, match="must be validated"):
        sign_off_report(
            store,
            review.id,
            ReportSignoffRequest(expected_revision=review.revision, operator_id=operator.id),
        )


def test_signoff_rolls_back_finalization_when_audit_event_cannot_be_created(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "atomic-signoff.db")
    engagement = store.create(Engagement(name="Atomic report project"))
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="Atomic final report",
            status=ReportStatus.REVIEW,
        )
    )
    operator = OperatorProfileService(store).create_profile(display_name="Reviewer")

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected operation-event failure")

    monkeypatch.setattr(store, "_next_operation_event", fail_event)
    with pytest.raises(RuntimeError, match="injected operation-event failure"):
        sign_off_report(
            store,
            report.id,
            ReportSignoffRequest(
                expected_revision=report.revision,
                operator_id=operator.id,
            ),
        )

    unchanged = store.get(Report, report.id)
    assert unchanged.status == ReportStatus.REVIEW
    assert unchanged.revision == report.revision
    assert unchanged.signed_off_by is None
    assert store.replay_operation_events(report.id) == []


def test_report_signoff_api_is_revision_aware(tmp_path):
    store = NebulaStore(tmp_path / "signoff-api.db")
    engagement = store.create(Engagement(name="API report project"))
    finding = store.create(
        Finding(
            engagement_id=engagement.id,
            title="Validated API issue",
            status=FindingStatus.VALIDATED,
        )
    )
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="API final report",
            status=ReportStatus.REVIEW,
            finding_ids=[finding.id],
        )
    )
    operator = OperatorProfileService(store).create_profile(display_name="API Reviewer")
    client = TestClient(create_app(store, auth_token="test-token"))

    with client:
        response = client.post(
            f"/api/v1/reports/{report.id}/sign-off",
            headers={"Authorization": "Bearer test-token"},
            json={
                "expected_revision": report.revision,
                "operator_id": operator.id,
                "attestation": "Reviewed in the sign-off workflow.",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "final"
    assert response.json()["signed_off_by"] == operator.id


def test_evidence_validation_signoff_and_pdf_workflow(tmp_path):
    """Release gate: a real artifact reaches a signed, renderable final report."""

    store = NebulaStore(tmp_path / "workflow.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="End-to-end report project"))
    finding = store.create(
        Finding(
            engagement_id=engagement.id,
            title="Evidence-backed issue",
            description="A bounded validation workflow finding.",
        )
    )
    evidence = upload_evidence(
        store=store,
        artifact_store=artifacts,
        request=EvidenceUploadRequest(
            engagement_id=engagement.id,
            finding_id=finding.id,
            filename="validation.txt",
            title="Independent validation",
            evidence_type="operator-validation",
            media_type="text/plain",
            content_base64=base64.b64encode(b"validated result\n").decode("ascii"),
        ),
    )
    evidence_backed = store.get(Finding, finding.id)
    validated = store.update(
        Finding,
        finding.id,
        {"status": FindingStatus.VALIDATED},
        expected_revision=evidence_backed.revision,
    )
    assert validated.evidence_ids == [evidence.id]

    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="Signed validation report",
            status=ReportStatus.REVIEW,
            executive_summary="Validated through preserved evidence.",
            finding_ids=[validated.id],
            artifact_ids=[evidence.artifact_id],
        )
    )
    operator = OperatorProfileService(store).create_profile(
        display_name="Release Reviewer"
    )
    signed = sign_off_report(
        store,
        report.id,
        ReportSignoffRequest(
            expected_revision=report.revision,
            operator_id=operator.id,
        ),
    )

    async def render() -> tuple[ReportRender, str]:
        service = ReportRenderService(store=store, artifact_store=artifacts)
        queued = await service.request_render(
            signed.id, report_revision=signed.revision
        )
        await service._tasks[queued.id]
        completed = store.get(ReportRender, queued.id)
        artifact, path = service.pdf(completed.id)
        assert artifacts.verify(artifact)
        return completed, "\n".join(
            page.extract_text() or "" for page in PdfReader(path).pages
        )

    completed, pdf_text = asyncio.run(render())
    assert completed.status == ReportRenderStatus.COMPLETED
    assert "Signed validation report" in pdf_text
    assert "FINAL" in pdf_text
    assert "Evidence-backed issue" in pdf_text
