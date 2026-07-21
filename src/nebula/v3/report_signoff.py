"""Dedicated, revision-safe report sign-off workflow."""

from __future__ import annotations

from pydantic import Field

from .domain import (
    Finding,
    FindingStatus,
    NebulaModel,
    OperatorProfile,
    Report,
    ReportStatus,
    utc_now,
)
from .storage import ConflictError, NebulaStore

_SIGNABLE_FINDING_STATUSES = {
    FindingStatus.VALIDATED,
    FindingStatus.CONFIRMED,
    FindingStatus.ACCEPTED_RISK,
    FindingStatus.REMEDIATED,
    FindingStatus.RETEST_PASSED,
    FindingStatus.RETEST_FAILED,
}


class ReportSignoffRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    operator_id: str = Field(min_length=1, max_length=200)
    attestation: str = Field(
        default="I reviewed this report and approve it as the final record.",
        min_length=1,
        max_length=2_000,
    )


def sign_off_report(
    store: NebulaStore, report_id: str, request: ReportSignoffRequest
) -> Report:
    report = store.get(Report, report_id)
    if report.revision != request.expected_revision:
        raise ConflictError(
            f"revision conflict: expected {request.expected_revision}, "
            f"found {report.revision}"
        )
    if report.status != ReportStatus.REVIEW:
        raise ConflictError("only a report in review can be signed off")

    operator = store.get(OperatorProfile, request.operator_id)
    if not operator.active:
        raise ConflictError("report sign-off requires the active operator profile")

    for finding_id in report.finding_ids:
        finding = store.get(Finding, finding_id)
        if finding.engagement_id != report.engagement_id:
            raise ConflictError("report contains a finding from another project")
        if finding.status not in _SIGNABLE_FINDING_STATUSES:
            raise ConflictError(
                f"finding {finding.title!r} must be validated before report sign-off"
            )

    signed_at = utc_now()
    metadata = dict(report.metadata)
    metadata["signoff"] = {
        "attestation": request.attestation.strip(),
        "operator_display_name": operator.display_name,
        "report_revision": report.revision,
    }
    signed, _ = store.update_with_operation_event(
        Report,
        report.id,
        {
            "status": ReportStatus.FINAL,
            "signed_off_by": operator.id,
            "signed_off_at": signed_at,
            "metadata": metadata,
        },
        expected_revision=report.revision,
        operation_id=report.id,
        operation_kind=Report.entity_kind,
        engagement_id=report.engagement_id,
        event_type="report.signed_off",
        event_payload={
            "report_revision": report.revision + 1,
            "signed_off_at": signed_at.isoformat(),
            "operator_id": operator.id,
        },
        actor_id=operator.id,
        idempotency_key=f"report-signoff:{report.id}:{report.revision + 1}",
        occurred_at=signed_at,
    )
    return signed


__all__ = ["ReportSignoffRequest", "sign_off_report"]
