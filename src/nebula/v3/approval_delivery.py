"""Durable approval delivery bookkeeping. Never executes or replays a tool."""

from .domain import Approval, ApprovalContinuation, HarnessTurn, utc_now
from .storage import ConflictError


def pending_deliveries(store):
    from sqlalchemy import select
    from .database import EntityRow

    with store.database.session() as database:
        return [
            Approval.model_validate(row.payload)
            for row in database.scalars(
                select(EntityRow).where(
                    EntityRow.kind == Approval.entity_kind,
                    EntityRow.payload["continuation"]["status"].as_string()
                    == "pending",
                )
            )
        ]


def approval_harness_turn(store, approval):
    """Resolve an exact binding, rejecting cross-project/turn references."""
    from .domain import ToolCall

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
    return store.update(
        Approval,
        current.id,
        {
            "continuation": ApprovalContinuation(
                harness_turn_id=turn_id,
                status=status,
                detail=detail,
                updated_at=utc_now(),
            ).model_dump(mode="json")
        },
        expected_revision=current.revision,
    )
