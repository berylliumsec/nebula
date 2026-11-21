"""Approvals of ended harness turns settle instead of pinning their project.

On live Core, Codex command approvals stayed pending after their turns were
interrupted by a restart; approving them days later recorded an adapter
delivery that could never be confirmed (`unknown`), and every deployment then
had to evict the project because the handoff inventory treats that as an
unresolved effect.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import httpx

from nebula.v3.domain import Approval, HarnessTurn, ToolCall
from tests.v3.test_approval_continuation import fixture


async def _native(store, approval):
    call = await store.get(ToolCall, approval.tool_call_id)
    await store.update(
        ToolCall,
        call.id,
        {"metadata": {**call.metadata, "adapter_handoff": "transport_write"}},
        expected_revision=call.revision,
    )


async def _decide(app, approval_id):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer fixture"},
    ) as client:
        return await client.post(
            f"/api/v1/approvals/{approval_id}/decision", json={"decision": "approve"}
        )


async def test_decision_on_an_ended_harness_turn_expires_it_instead(tmp_path):
    app, store, _runtime, approval, turn, _owner = await fixture(
        tmp_path, "read_fixture"
    )
    await _native(store, approval)
    current = await store.get(HarnessTurn, turn.id)
    await store.update(
        HarnessTurn,
        turn.id,
        {"status": "interrupted"},
        expected_revision=current.revision,
    )

    response = await _decide(app, approval.id)

    assert response.status_code == 410, response.text
    settled = await store.get(Approval, approval.id)
    assert settled.status.value == "expired"
    assert settled.decided_by == "system"
    # No decision, so no delivery left for anyone to confirm.
    assert settled.continuation is None


async def test_decision_with_no_waiter_is_recorded_as_not_sent(tmp_path):
    # The turn still claims to wait, but this process holds no waiter for the
    # request (its connection did not survive): the decision cannot have been
    # handed to the adapter, so nothing is left unconfirmed.
    app, store, runtime, approval, _turn, _owner = await fixture(
        tmp_path, "read_fixture"
    )
    await _native(store, approval)
    assert approval.id not in runtime._approval_futures

    response = await _decide(app, approval.id)

    assert response.status_code == 200, response.text
    continuation = (await store.get(Approval, approval.id)).continuation
    assert continuation.status == "failed"
    assert continuation.adapter_status == "failed"
    assert continuation.adapter_status not in {"pending", "unknown"}


async def test_a_completed_waiter_still_leaves_the_delivery_unconfirmed(tmp_path):
    # A waiter that already received a result may have reached the adapter;
    # that case keeps the crash-safe `unknown` state for operator resolution.
    app, store, runtime, approval, _turn, _owner = await fixture(
        tmp_path, "read_fixture"
    )
    await _native(store, approval)
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    runtime._approval_futures[approval.id] = future

    response = await _decide(app, approval.id)

    assert response.status_code == 200, response.text
    continuation = (await store.get(Approval, approval.id)).continuation
    assert continuation.adapter_status == "unknown"


async def test_restart_expires_the_pending_approvals_of_the_turns_it_ends(tmp_path):
    _app, store, runtime, approval, turn, _owner = await fixture(
        tmp_path, "read_fixture"
    )
    # A restarted Core holds no waiter for the request.
    runtime._approval_futures.pop(approval.id, None)
    current = await store.get(HarnessTurn, turn.id)
    await store.update(
        HarnessTurn,
        turn.id,
        {"status": "running"},
        expected_revision=current.revision,
    )

    await runtime._settle_turn_after_restart(await store.get(HarnessTurn, turn.id))

    assert (await store.get(HarnessTurn, turn.id)).status.value == "interrupted"
    settled = await store.get(Approval, approval.id)
    assert settled.status.value == "expired"
    assert "restarted" in settled.decision_note
