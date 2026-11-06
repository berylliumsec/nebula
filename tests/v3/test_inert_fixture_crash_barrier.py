"""The inert acceptance peer crashes after committed progress, never a queued delta."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import pytest

from nebula.v3.harnesses import HarnessEvent, _coalesce_activity_deltas
from nebula.v3.storage import NebulaStore
from ui.tests.fixtures.approval_core import wait_for_committed_progress


async def test_inert_crash_barrier_waits_for_coalesced_progress_commit(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    earlier = await store.append_operation_event(
        "inert-turn",
        "harness_turn",
        "inert-project",
        "harness.message_delta",
        {"delta": "exact inert answer"},
    )
    checkpoints = []

    async def source():
        yield HarnessEvent(type="message_delta", delta="exact inert answer")
        checkpoint = await wait_for_committed_progress(
            store, "inert-turn", earlier.sequence, "exact inert answer"
        )
        checkpoints.append(checkpoint)
        yield HarnessEvent(type="status", payload={"phase": "fixture_barrier_reached"})

    async def consume():
        observed = []
        async for event in _coalesce_activity_deltas(source()):
            observed.append(event.type)
            if event.type == "message_delta":
                assert checkpoints == []
                (
                    await store.append_operation_event(
                        "inert-turn",
                        "harness_turn",
                        "inert-project",
                        "harness.message_delta",
                        {"delta": event.delta},
                    )
                )
        return observed

    assert (await asyncio.wait_for(consume(), timeout=2)) == [
        "message_delta",
        "status",
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0].sequence > earlier.sequence
    assert checkpoints[0].payload["delta"] == "exact inert answer"


async def test_inert_crash_barrier_rejects_missing_or_wrong_progress(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    (
        await store.append_operation_event(
            "inert-turn",
            "harness_turn",
            "inert-project",
            "harness.message_delta",
            {"delta": "a different answer"},
        )
    )
    with pytest.raises(TimeoutError):
        (
            await wait_for_committed_progress(
                store, "inert-turn", 0, "exact inert answer", timeout=0.03
            )
        )


pytestmark = pytest.mark.anyio
