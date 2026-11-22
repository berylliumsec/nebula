"""Durable approval delivery bookkeeping. Never executes or replays a tool."""

from sqlalchemy import func, or_, select

from .database import EntityRow, OperationEventRow
from .domain import Approval, ApprovalContinuation, HarnessTurn, ToolCall, utc_now
from .storage import ConflictError, readable_entities


# An operator's finding about a delivery whose outcome nobody can confirm any
# more, mapped to the adapter receipt it stands in for. Both are existing
# receipt values, so older execution versions still read the approval.
OPERATOR_DELIVERY_RESOLUTIONS = {"delivered": "sent", "discarded": "failed"}
DELIVERY_RESOLVED_EVENT = "approval.adapter_delivery_resolved"
_RESOLUTION_DETAIL = {
    "delivered": (
        "The operator confirmed that the adapter received this decision. "
        "Nothing was sent again."
    ),
    "discarded": (
        "The operator discarded this unconfirmed delivery. Nothing was sent "
        "again; start a new response to redo the work."
    ),
}


async def pending_deliveries(store):
    # Read at startup: an unreadable approval is skipped and recorded, not
    # fatal to Core.
    async with store.database.session() as database:
        return readable_entities(
            (
                await database.scalars(
                    select(EntityRow).where(
                        EntityRow.kind == Approval.entity_kind,
                        or_(
                            EntityRow.payload["continuation"]["status"].as_string()
                            == "pending",
                            EntityRow.payload["continuation"][
                                "adapter_status"
                            ].as_string()
                            == "pending",
                        ),
                    )
                )
            ),
            Approval,
        )


async def approval_harness_turn(store, approval):
    """Resolve an exact binding, rejecting cross-project/turn references."""
    if not approval.tool_call_id:
        return None
    call = await store.get(ToolCall, approval.tool_call_id)
    turn_id = call.metadata.get("harness_turn_id")
    if not isinstance(turn_id, str):
        return None
    turn = await store.get(HarnessTurn, turn_id)
    if (
        call.engagement_id != approval.engagement_id
        or turn.engagement_id != approval.engagement_id
        or (approval.chat_turn_id and turn.chat_turn_id != approval.chat_turn_id)
        or (
            approval.chat_session_id
            and turn.chat_session_id != approval.chat_session_id
        )
    ):
        raise ConflictError("approval does not belong to this harness request")
    return turn


# Once a waiter is fulfilled, the decision task records the delivery while the
# adapter writes the decision and saves its handoff receipt. Both update the
# same approval, so either write can lose the revision race to the other.
_CONTINUATION_WRITE_ATTEMPTS = 3


async def _write_continuation(store, approval_id, plan):
    """Save ``plan(current)`` against the latest approval revision.

    ``plan`` returns the continuation changes to save, or ``None`` to keep the
    approval unchanged; it raises to refuse the write. A revision conflict
    rereads the approval and plans again, so a concurrent writer's change is
    kept rather than overwritten.
    """
    for attempt in range(1, _CONTINUATION_WRITE_ATTEMPTS + 1):
        current = await store.get(Approval, approval_id)
        planned = await plan(current)
        if planned is None:
            return current
        continuation, changes = planned
        try:
            return await store.update(
                Approval,
                current.id,
                {
                    "continuation": continuation.model_copy(update=changes).model_dump(
                        mode="json"
                    )
                },
                expected_revision=current.revision,
            )
        except ConflictError:  # diagnostic-expected: reread an approval another delivery write changed; the last attempt rethrows
            if attempt == _CONTINUATION_WRITE_ATTEMPTS:
                raise
    raise AssertionError("continuation write attempts are exhausted")


async def record_delivery(
    store, approval_id, turn_id, status, detail=None, *, sent=True
):
    async def plan(current):
        if current.continuation and current.continuation.harness_turn_id != turn_id:
            raise ConflictError("approval continuation belongs to another turn")
        continuation = current.continuation or ApprovalContinuation(
            harness_turn_id=turn_id
        )
        changes = {"status": status, "detail": detail, "updated_at": utc_now()}
        if status == "delivered" and continuation.progress_after_sequence is None:
            changes["progress_after_sequence"] = await _last_sequence(store, turn_id)
        if status == "failed":
            # An interrupted continuation does not erase a known waiter delivery.
            if continuation.status == "delivered":
                changes["status"] = "delivered"
            if continuation.adapter_status == "pending":
                if sent:
                    changes["adapter_status"] = "unknown"
                    changes["adapter_detail"] = (
                        "Adapter handoff was not acknowledged. No work was replayed."
                    )
                else:
                    # Core knows the decision never reached the adapter: there
                    # was no waiter to hand it to. Nothing is left to confirm.
                    changes["adapter_status"] = "failed"
                    changes["adapter_detail"] = (
                        "Not sent: no request was waiting for this decision. "
                        "No work was replayed."
                    )
        return continuation, changes

    return await _write_continuation(store, approval_id, plan)


async def decision_delivery_intent(store, approval, turn):
    """Include adapter intent in the same durable update as the decision."""
    call = (
        (await store.get(ToolCall, approval.tool_call_id))
        if approval.tool_call_id
        else None
    )
    handoff = call.metadata.get("adapter_handoff") if call else None
    if handoff not in {"transport_write", "sdk_callback"}:
        handoff = None
    return ApprovalContinuation(
        harness_turn_id=turn.id,
        adapter_handoff=handoff,
        adapter_status="pending" if handoff else "not_required",
    ).model_dump(mode="json")


async def _last_sequence(store, turn_id):
    async with store.database.session() as database:
        return int(
            (
                await database.scalar(
                    select(func.max(OperationEventRow.sequence)).where(
                        OperationEventRow.operation_id == turn_id,
                        OperationEventRow.operation_kind == "harness_turn",
                    )
                )
            )
            or 0
        )


async def record_adapter_handoff(store, approval_id, turn_id, status):
    """Acknowledge only the original handoff, never retry or infer tool execution.

    Only the receipt's own write is retried after a revision conflict; the
    decision is never sent to the adapter again.
    """
    if status not in {"sent", "failed"}:
        raise ValueError("unsupported adapter handoff receipt")
    detail = (
        "Decision sent at the adapter boundary; this is not proof of command execution."
        if status == "sent"
        else "The adapter handoff failed; delivery may be uncertain. No work was replayed."
    )

    async def plan(current):
        continuation = current.continuation
        if continuation is None or continuation.harness_turn_id != turn_id:
            raise ConflictError("adapter handoff belongs to another turn")
        if continuation.adapter_status != "pending":
            return None
        if current.status.value == "pending":
            raise ConflictError("adapter handoff requires a recorded decision")
        return continuation, {
            "status": "delivered",  # The adapter could only receive a fulfilled waiter.
            "adapter_status": status,
            "adapter_detail": detail,
            "progress_after_sequence": (await _last_sequence(store, turn_id)),
            "updated_at": utc_now(),
        }

    return await _write_continuation(store, approval_id, plan)


async def resolve_unknown_delivery(
    store,
    approval_id: str,
    *,
    expected_revision: int,
    resolution: str,
    reason: str | None,
    actor_id: str | None,
):
    """Record the operator's outcome for an adapter delivery left ``unknown``.

    A worker that died during the adapter handoff leaves the delivery
    ``unknown``: no live adapter can acknowledge it, and Core never sends the
    decision again by itself. Until it is settled the project's quiescence,
    handoff and shutdown refuse. The operator says what happened instead:
    ``delivered`` (the harness received it) or ``discarded`` (treat it as not
    delivered). This only records that finding with its audit event; it never
    sends, retries or executes anything.

    The write is fenced by the approval revision. Repeating the same
    resolution after a lost response returns the saved approval unchanged.
    """
    if resolution not in OPERATOR_DELIVERY_RESOLUTIONS:
        raise ValueError("choose delivered or discarded")
    approval = await store.get(Approval, approval_id)
    adapter_status = OPERATOR_DELIVERY_RESOLUTIONS[resolution]
    key = f"approval:{approval.id}:adapter-delivery"
    continuation = approval.continuation
    if continuation is None or continuation.adapter_status != "unknown":
        recorded = await store.replay_operation_events(
            approval.id, limit=1, event_types=(DELIVERY_RESOLVED_EVENT,)
        )
        if recorded and recorded[0].payload.get("resolution") == resolution:
            return approval
        raise ConflictError(
            "This approval has no unconfirmed adapter delivery to resolve"
        )
    payload = {
        "approval_id": approval.id,
        "harness_turn_id": continuation.harness_turn_id,
        "resolution": resolution,
        "previous_adapter_status": continuation.adapter_status,
        "adapter_status": adapter_status,
        "reason": reason,
        "replayed": False,
    }
    updated, _ = await store.update_with_operation_event(
        Approval,
        approval.id,
        {
            "continuation": continuation.model_copy(
                update={
                    "adapter_status": adapter_status,
                    "adapter_detail": _RESOLUTION_DETAIL[resolution],
                    "updated_at": utc_now(),
                }
            ).model_dump(mode="json")
        },
        expected_revision=expected_revision,
        # The approval's own audit stream: its run or turn may be gone.
        operation_id=approval.id,
        operation_kind="approval",
        engagement_id=approval.engagement_id,
        event_type=DELIVERY_RESOLVED_EVENT,
        event_payload=payload,
        actor_id=actor_id,
        idempotency_key=key,
    )
    return updated
