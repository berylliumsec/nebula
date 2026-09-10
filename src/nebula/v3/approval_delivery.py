"""Durable approval delivery bookkeeping. Never executes or replays a tool."""

from sqlalchemy import func, or_, select

from .database import EntityRow, OperationEventRow
from .domain import Approval, ApprovalContinuation, HarnessTurn, ToolCall, utc_now
from .storage import ConflictError


def pending_deliveries(store):
    with store.database.session() as database:
        return [
            Approval.model_validate(row.payload)
            for row in database.scalars(
                select(EntityRow).where(
                    EntityRow.kind == Approval.entity_kind,
                    or_(
                        EntityRow.payload["continuation"]["status"].as_string()
                        == "pending",
                        EntityRow.payload["continuation"]["adapter_status"].as_string()
                        == "pending",
                    ),
                )
            )
        ]


def approval_harness_turn(store, approval):
    """Resolve an exact binding, rejecting cross-project/turn references."""
    if not approval.tool_call_id:
        return None
    call = store.get(ToolCall, approval.tool_call_id)
    turn_id = call.metadata.get("harness_turn_id")
    if not isinstance(turn_id, str):
        return None
    turn = store.get(HarnessTurn, turn_id)
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


def record_delivery(store, approval_id, turn_id, status, detail=None):
    current = store.get(Approval, approval_id)
    if current.continuation and current.continuation.harness_turn_id != turn_id:
        raise ConflictError("approval continuation belongs to another turn")
    continuation = current.continuation or ApprovalContinuation(harness_turn_id=turn_id)
    changes = {"status": status, "detail": detail, "updated_at": utc_now()}
    if status == "delivered" and continuation.progress_after_sequence is None:
        changes["progress_after_sequence"] = _last_sequence(store, turn_id)
    if status == "failed":
        # An interrupted continuation does not erase a known waiter delivery.
        if continuation.status == "delivered":
            changes["status"] = "delivered"
        if continuation.adapter_status == "pending":
            changes["adapter_status"] = "unknown"
            changes["adapter_detail"] = (
                "Adapter handoff was not acknowledged. No work was replayed."
            )
    return store.update(
        Approval,
        current.id,
        {
            "continuation": continuation.model_copy(update=changes).model_dump(
                mode="json"
            )
        },
        expected_revision=current.revision,
    )


def decision_delivery_intent(store, approval, turn):
    """Include adapter intent in the same durable update as the decision."""
    call = store.get(ToolCall, approval.tool_call_id) if approval.tool_call_id else None
    handoff = call.metadata.get("adapter_handoff") if call else None
    if handoff not in {"transport_write", "sdk_callback"}:
        handoff = None
    return ApprovalContinuation(
        harness_turn_id=turn.id,
        adapter_handoff=handoff,
        adapter_status="pending" if handoff else "not_required",
    ).model_dump(mode="json")


def _last_sequence(store, turn_id):
    with store.database.session() as database:
        return int(
            database.scalar(
                select(func.max(OperationEventRow.sequence)).where(
                    OperationEventRow.operation_id == turn_id,
                    OperationEventRow.operation_kind == "harness_turn",
                )
            )
            or 0
        )


def record_adapter_handoff(store, approval_id, turn_id, status):
    """Acknowledge only the original handoff, never retry or infer tool execution."""
    if status not in {"sent", "failed"}:
        raise ValueError("unsupported adapter handoff receipt")
    current = store.get(Approval, approval_id)
    continuation = current.continuation
    if continuation is None or continuation.harness_turn_id != turn_id:
        raise ConflictError("adapter handoff belongs to another turn")
    if continuation.adapter_status != "pending":
        return current
    if current.status.value == "pending":
        raise ConflictError("adapter handoff requires a recorded decision")
    detail = (
        "Decision sent at the adapter boundary; this is not proof of command execution."
        if status == "sent"
        else "The adapter handoff failed; delivery may be uncertain. No work was replayed."
    )
    return store.update(
        Approval,
        current.id,
        {
            "continuation": continuation.model_copy(
                update={
                    "status": "delivered",  # The adapter could only receive a fulfilled waiter.
                    "adapter_status": status,
                    "adapter_detail": detail,
                    "progress_after_sequence": _last_sequence(store, turn_id),
                    "updated_at": utc_now(),
                }
            ).model_dump(mode="json"),
        },
        expected_revision=current.revision,
    )
