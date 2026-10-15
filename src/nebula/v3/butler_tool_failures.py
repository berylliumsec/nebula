"""Atomically observe an exact repeat of a Core-recorded read-only tool failure.

The signal is narrower than a research loop: it describes three failed calls
with the same name and arguments in separate completed provider chat turns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from .butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    _entity_in_transaction,
    incident_id,
)
from .database import EntityRow
from .domain import (
    ButlerIncident,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)

if TYPE_CHECKING:
    from .storage import StoreTransaction


_READ_ONLY_RETRIEVAL = frozenset(
    {"tool_output.read", "tool_output.search", "workspace.read", "workspace.search"}
)


def _same_failed_read(call: ToolCall, goal: ChatGoal, turn: ChatTurn) -> bool:
    return (
        call.engagement_id == goal.engagement_id
        and call.chat_session_id == goal.session_id
        and call.chat_turn_id == turn.id
        and call.origin == ToolCallOrigin.CHAT
        and call.status == ToolCallStatus.FAILED
        and call.tool_name in _READ_ONLY_RETRIEVAL
        and call.risk_class == RiskClass.LOCAL_READ
        and call.approval_id is None
        and call.started_at is not None
        and call.completed_at is not None
        and not (
            isinstance(call.result, dict)
            and call.result.get("side_effects") == "unknown"
        )
        and turn.engagement_id == goal.engagement_id
        and turn.session_id == goal.session_id
        and turn.goal_id == goal.id
        and turn.backend == ChatBackend.PROVIDER
        and turn.status == ChatTurnStatus.COMPLETE
    )


def record_completed_provider_turn(
    transaction: StoreTransaction, completed_turn: ChatTurn
) -> bool:
    """Write a concern in the turn-completion transaction, or write nothing.

    The caller already locked the project stream before updating the turn. A
    crash cannot commit the completed turn without also committing this signal.
    """

    if (
        completed_turn.backend != ChatBackend.PROVIDER
        or not completed_turn.goal_id
        or completed_turn.status != ChatTurnStatus.COMPLETE
    ):
        return False
    goal = _entity_in_transaction(transaction, ChatGoal, completed_turn.goal_id)
    session = _entity_in_transaction(transaction, ChatSession, completed_turn.session_id)
    if (
        goal is None
        or session is None
        or goal.engagement_id != completed_turn.engagement_id
        or session.engagement_id != completed_turn.engagement_id
        or goal.session_id != session.id
        or session.backend != ChatBackend.PROVIDER
        or goal.status != ChatGoalStatus.RUNNING
    ):
        return False
    rows = transaction.session.execute(
        select(EntityRow)
        .where(
            EntityRow.kind == ToolCall.entity_kind,
            EntityRow.chat_session_id == session.id,
        )
        .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
        .limit(4)
    ).scalars().all()
    if len(rows) < 3:
        return False
    calls = [ToolCall.model_validate(row.payload) for row in rows]
    if calls[0].chat_turn_id != completed_turn.id:
        return False
    turns: list[ChatTurn] = []
    for call in calls:
        if call.chat_turn_id == completed_turn.id:
            turns.append(completed_turn)
        elif call.chat_turn_id:
            previous = _entity_in_transaction(transaction, ChatTurn, call.chat_turn_id)
            if previous is None:
                return False
            turns.append(previous)
        else:
            return False
    first = calls[0]
    for call, turn in zip(calls[:3], turns[:3], strict=True):
        if (
            not _same_failed_read(call, goal, turn)
            or call.tool_name != first.tool_name
            or call.arguments != first.arguments
        ):
            return False
    if len({turn.id for turn in turns[:3]}) != 3:
        return False
    if any(
        older.completed_at > newer.started_at
        for newer, older in zip(calls[:2], calls[1:3], strict=True)
    ):
        return False
    # The fourth matching call means an earlier completion already crossed the
    # threshold and emitted the one incident for this contiguous streak.
    if len(calls) == 4 and _same_failed_read(calls[3], goal, turns[3]):
        if (
            calls[3].tool_name == first.tool_name
            and calls[3].arguments == first.arguments
            and calls[3].completed_at <= calls[2].started_at
            and turns[3].id not in {turn.id for turn in turns[:3]}
        ):
            return False
    project_id = goal.engagement_id
    key = f"read-only-repeat:{goal.id}:{first.id}"
    record_id = incident_id(project_id, key)
    if _entity_in_transaction(transaction, ButlerIncident, record_id) is not None:
        return False
    ordered = list(reversed(calls[:3]))
    refs = [
        ButlerEvidenceRef(kind="goal", id=goal.id, project_id=project_id),
        *(
            ButlerEvidenceRef(kind="tool_call", id=call.id, project_id=project_id)
            for call in ordered
        ),
    ]
    now = utc_now()
    transaction.add(
        ButlerIncident(
            id=record_id,
            engagement_id=project_id,
            incident_key=key,
            source="repeated_read_only_tool_failure",
            verification_state="core_observed",
            summary=f"The same {first.tool_name} call failed in three separate turns.",
            impact="Core observed three failures of this exact read-only invocation; broader goal progress is unknown.",
            next_action="Ask the parent main agent to inspect the saved outcomes before repeating this call.",
            evidence_refs=[ref.model_dump() for ref in refs],
            evidence_summary=[
                f"Attempt {index} ended with Core tool status failed."
                for index in range(1, 4)
            ],
            goal_ids=[goal.id],
            session_ids=[goal.session_id],
            observed_at=now,
        )
    )
    _append_project_event(
        transaction,
        engagement_id=project_id,
        event_type="goal.read_only_tool_failures_repeated",
        payload=ButlerEventPayload(
            project_id=project_id,
            goal_id=goal.id,
            session_id=goal.session_id,
            source="core",
            actor_role="subagent" if goal.parent_goal_id else "main_agent",
            parent_goal_id=goal.parent_goal_id,
            summary=f"Core recorded three sequential failures of the same {first.tool_name} call in separate turns.",
            metrics={
                "tool_name": first.tool_name,
                "attempts": 3,
                "goal_revision": goal.revision,
            },
            verification_state="core_observed",
            evidence_refs=refs,
            incident_key=key,
        ),
        idempotency_key=f"read-only-repeat:{goal.id}:{first.id}",
        now=now,
    )
    return True
