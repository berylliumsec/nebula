"""Project write locks taken by execution workers on SQLite and PostgreSQL."""

from unittest.mock import Mock

import pytest

from nebula.v3.butler_sessions import ButlerSessionReservation, reserve_butler_session
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    Engagement,
    Finding,
    FindingStatus,
    Report,
    ReportStatus,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.operators import OperatorProfileService
from nebula.v3.report_signoff import ReportSignoffRequest, sign_off_report
from nebula.v3.storage import NotFoundError
from tests.v3.test_execution_scope import (
    anyio_backend as anyio_backend,
    execution as execution,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


# SQLite workers already hold the writer lock: the execution scope begins every
# transaction with BEGIN IMMEDIATE and refuses textual statements such as a
# second BEGIN IMMEDIATE.


async def test_worker_opens_one_butler_session(execution):
    control, runtime, _, lease = execution
    reservation = ButlerSessionReservation(
        engagement_id="mine",
        backend=ChatBackend.PROVIDER,
        provider_id="provider",
        model="inert",
    )
    with execution_context(lease):
        butler = await reserve_butler_session(runtime, Mock(), reservation)
        again = await reserve_butler_session(runtime, Mock(), reservation)
    assert again.id == butler.id
    assert (await control.get(ChatSession, butler.id)).metadata["control_assistant"]


async def test_worker_signs_off_a_report(execution):
    control, runtime, _, lease = execution
    finding = await control.create(
        Finding(
            engagement_id="mine",
            title="Validated issue",
            status=FindingStatus.VALIDATED,
        )
    )
    report = await control.create(
        Report(
            engagement_id="mine",
            title="Final report",
            status=ReportStatus.REVIEW,
            finding_ids=[finding.id],
        )
    )
    operator = await OperatorProfileService(control).create_profile(
        display_name="Reviewer"
    )
    with execution_context(lease):
        signed = await sign_off_report(
            runtime,
            report.id,
            ReportSignoffRequest(
                expected_revision=report.revision, operator_id=operator.id
            ),
        )
    assert signed.status == ReportStatus.FINAL
    assert (await control.get(Report, report.id)).status == ReportStatus.FINAL


async def test_worker_deletes_an_archived_project(execution):
    control, runtime, coordinator, _ = execution
    archived = await control.create(
        Engagement(id="archived", name="archived", status="archived")
    )
    await control.create(
        ChatSession(
            id="archived-chat",
            engagement_id="archived",
            title="Old chat",
            model="inert",
            provider_profile_id="provider",
        )
    )
    lease = await coordinator.claim_project("archived", "execution")
    with execution_context(lease):
        await runtime.delete_archived_engagement(
            "archived", expected_revision=archived.revision
        )
    for model, identifier in ((Engagement, "archived"), (ChatSession, "archived-chat")):
        with pytest.raises(NotFoundError):
            await control.get(model, identifier)
    # The other projects and their history remain.
    assert (await control.get(Engagement, "other")).id == "other"
