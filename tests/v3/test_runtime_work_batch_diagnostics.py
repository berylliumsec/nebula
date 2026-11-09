"""Private capability faults remain diagnosable without accepting a Work batch."""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from nebula.v3.database import RuntimeCommandRow, RuntimeWorkBatchProjectRow
from nebula.v3.runtime_ipc import RuntimeIpcError
from tests.v3.test_runtime_work_batches import batch as batch, intent
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("failed_transport", [False, True])
async def test_capability_fault_records_cause_without_accepting_work(
    batch, monkeypatch, failed_transport
):
    captured = []
    failure = RuntimeIpcError("synthetic-sensitive-protocol-detail")

    async def unavailable():
        if failed_transport:
            raise failure
        return {
            "protocol": "nebula.work-batches/v1",
            "feature": "project.work-batch/v1",
            "ready": False,
        }

    def record(feature, event, message, error, **fields):
        captured.append((feature, event, message, error, fields))

    batch.acceptor.availability = unavailable
    monkeypatch.setattr(
        "nebula.v3.runtime_work_batches.record_caught_exception", record
    )
    with pytest.raises(HTTPException) as refused:
        await batch.acceptor.accept(
            intent("PATCH", {"project_ids": ["one", "two"], "work_enabled": True}),
            actor_id="operator",
            idempotency_key="capability-unavailable",
        )
    assert refused.value.status_code == 503
    assert "synthetic-sensitive" not in str(refused.value.detail)
    assert len(captured) == int(failed_transport)
    if captured:
        feature, event, message, error, fields = captured[0]
        assert (feature, event, error, fields) == (
            "runtime",
            "work.batch_capability_unavailable",
            failure,
            {"stage": "admission"},
        )
        assert "synthetic-sensitive" not in message
    async with batch.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(RuntimeWorkBatchProjectRow)
            )
            == 0
        )
