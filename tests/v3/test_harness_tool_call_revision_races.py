"""A native tool call's row has two concurrent writers.

The permission handler reserves the ToolCall (revision 1) and then settles it
(approved or denied). Meanwhile the adapter's ``tool_use`` events are recorded
by ``_record_tool_event``, which starts the same row (PROPOSED -> RUNNING).
Each writer used to save over the revision it had read, so whichever wrote
second failed with ``revision conflict on tool_calls <id>: expected 1, found 2``
and the turn (and its goal) stopped.
"""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.domain import HarnessSession, ToolCall, ToolCallStatus
from nebula.v3.harnesses import HarnessEvent, HarnessPermissionRequest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_harness_prepare_revision_races import _gate
from tests.v3.test_harnesses import _runtime

pytestmark = pytest.mark.anyio


async def _turn(tmp_path, approval_policy: str = "never"):
    store, engagement, profile, mcp, _, runtime = await _runtime(tmp_path)
    _, _, turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Run a synthetic command",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[mcp.id],
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    session = await store.update(
        HarnessSession,
        session.id,
        {
            "metadata": {
                **session.metadata,
                "approval_policy": approval_policy,
                "execution_mode": "host",
                "native_capabilities": {"shell": True},
            }
        },
        expected_revision=session.revision,
    )
    return store, runtime, session, turn


def _request(item_id: str) -> HarnessPermissionRequest:
    return HarnessPermissionRequest(
        vendor_request_id=f"request-{item_id}",
        category="command",
        vendor_name="Bash",
        tool_name="Bash",
        arguments={"command": "printf synthetic"},
        annotations={"vendor_item_id": item_id},
    )


def _event(kind: str, item_id: str) -> HarnessEvent:
    return HarnessEvent(
        type=kind,
        server_id="claude",
        tool_name="Bash",
        item_id=item_id,
        payload={"id": item_id, "result": "synthetic output"},
    )


async def _wait(gate) -> None:
    await asyncio.wait_for(gate["paused"].wait(), timeout=5)


async def test_tool_use_start_between_reserve_and_approval_does_not_conflict(
    tmp_path,
):
    """Event path writes revision 2 while the permission path holds revision 1."""

    store, runtime, session, turn = await _turn(tmp_path)
    gate = _gate(runtime, "_attach_gateway_tool_call", after=True)
    permission = asyncio.create_task(
        runtime._request_permission(turn.id, _request("toolu-a"))
    )
    await _wait(gate)
    reserved = gate["result"]
    assert reserved.revision == 1
    assert reserved.status == ToolCallStatus.PROPOSED

    started = await runtime._record_tool_event(
        turn, session, _event("tool_started", "toolu-a")
    )
    assert started.tool_call_id == reserved.id
    assert (await store.get(ToolCall, reserved.id)).revision == 2

    gate["resume"].set()
    ticket = await asyncio.wait_for(permission, timeout=5)
    assert (await ticket.decision).allowed is True
    # Approval never moves a started call back.
    assert (await store.get(ToolCall, reserved.id)).status == ToolCallStatus.RUNNING

    await runtime._record_tool_event(turn, session, _event("tool_completed", "toolu-a"))
    assert (await store.get(ToolCall, reserved.id)).status == ToolCallStatus.COMPLETE


async def test_approval_between_listing_and_start_does_not_conflict(tmp_path):
    """Permission path writes revision 2 after the event path listed revision 1."""

    store, runtime, session, turn = await _turn(tmp_path)
    attach = _gate(runtime, "_attach_gateway_tool_call", after=True)
    permission = asyncio.create_task(
        runtime._request_permission(turn.id, _request("toolu-b"))
    )
    await _wait(attach)
    reserved = attach["result"]

    listing = _gate(runtime, "_turn_tool_calls", after=True)
    recording = asyncio.create_task(
        runtime._record_tool_event(turn, session, _event("tool_started", "toolu-b"))
    )
    await _wait(listing)
    assert [item.revision for item in listing["result"]] == [1]

    attach["resume"].set()
    ticket = await asyncio.wait_for(permission, timeout=5)
    assert (await ticket.decision).allowed is True
    assert (await store.get(ToolCall, reserved.id)).status == ToolCallStatus.APPROVED

    listing["resume"].set()
    started = await asyncio.wait_for(recording, timeout=5)
    assert started.tool_call_id == reserved.id
    latest = await store.get(ToolCall, reserved.id)
    assert latest.status == ToolCallStatus.RUNNING
    assert latest.metadata["vendor_item_id"] == "toolu-b"


async def test_cancel_after_completion_keeps_the_settled_call(tmp_path):
    store, runtime, session, turn = await _turn(tmp_path)
    ticket = await runtime._request_permission(turn.id, _request("toolu-c"))
    call_id = ticket.tool_call_id or ""
    await runtime._record_tool_event(turn, session, _event("tool_started", "toolu-c"))
    await runtime._record_tool_event(turn, session, _event("tool_completed", "toolu-c"))
    await runtime._cancel_gateway_tool_call(call_id, "stopped waiting")
    assert (await store.get(ToolCall, call_id)).status == ToolCallStatus.COMPLETE
