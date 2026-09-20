"""Dedicated, revision-safe report sign-off workflow."""

from __future__ import annotations

from pydantic import Field
from sqlalchemy import text

from .database import EntityRow
from .domain import (
    Finding,
    FindingStatus,
    NebulaModel,
    OperatorProfile,
    Report,
    ReportStatus,
    utc_now,
)
from .storage import ConflictError, NebulaStore, StoreTransaction

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

    signed_at = utc_now()
    # The finalized record is the next revision; the report metadata, the audit
    # event and the idempotency key all name that same revision.
    signed_revision = report.revision + 1
    metadata = dict(report.metadata)
    metadata["signoff"] = {
        "attestation": request.attestation.strip(),
        "operator_display_name": operator.display_name,
        "report_revision": signed_revision,
    }
    with store.transaction() as transaction:
        _begin_signoff_write(store, transaction)
        _validate_findings(transaction, report)
        signed = transaction.update(
            Report,
            report.id,
            {
                "status": ReportStatus.FINAL,
                "signed_off_by": operator.id,
                "signed_off_at": signed_at,
                "metadata": metadata,
            },
            expected_revision=report.revision,
        )
        transaction.append_operation_event(
            report.id,
            Report.entity_kind,
            report.engagement_id,
            "report.signed_off",
            {
                "report_revision": signed_revision,
                "signed_off_at": signed_at.isoformat(),
                "operator_id": operator.id,
            },
            actor_id=operator.id,
            idempotency_key=f"report-signoff:{report.id}:{signed_revision}",
            occurred_at=signed_at,
        )
    return signed


def _begin_signoff_write(store: NebulaStore, transaction: StoreTransaction) -> None:
    """Take the write lock up front so the finding checks and the commit are one unit.

    SQLite only opens a transaction at the first write, so without this the
    finding reads below would run in autocommit mode ahead of the finalizing
    update and a concurrent finding change could slip between them.
    """

    if store.database.engine.dialect.name == "sqlite":
        transaction.session.execute(text("BEGIN IMMEDIATE"))


def _validate_findings(transaction: StoreTransaction, report: Report) -> None:
    """Re-check every selected finding inside the transaction that finalizes."""

    for finding_id in report.finding_ids:
        row = transaction.session.get(EntityRow, finding_id)
        if row is None or row.kind != Finding.entity_kind:
            raise ConflictError("report references a deleted finding")
        finding = Finding.model_validate(row.payload)
        if finding.engagement_id != report.engagement_id:
            raise ConflictError("report contains a finding from another project")
        if finding.status not in _SIGNABLE_FINDING_STATUSES:
            raise ConflictError(
                f"finding {finding.title!r} must be validated before report sign-off"
            )


__all__ = ["ReportSignoffRequest", "sign_off_report"]
