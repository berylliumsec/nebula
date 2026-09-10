"""Derived session display state. Decisions are not execution progress.

Only a display watermark is persisted here; observing state never changes a
decision, a turn, a command or a policy. The watermark advances for semantic
changes, including expiry, deletion and connection transitions without a write
to the underlying entity, and is independent of the wall clock.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .database import EntityRow, SessionProjectionRow
from .domain import (
    Approval,
    ChatSession,
    ChatTurn,
    HarnessInteraction,
    HarnessProfile,
    HarnessTurn,
    utc_now,
)
from .storage import NebulaStore, NotFoundError

TERMINAL = {"complete", "failed", "cancelled", "interrupted"}


def session_state(
    store: NebulaStore, session: ChatSession, runtime=None
) -> dict[str, Any]:
    # Serialize the snapshot and watermark together. SQLite uses BEGIN IMMEDIATE;
    # PostgreSQL uses the existing per-key transaction advisory-lock boundary.
    # Always reread the selected session, never assign a new revision to stale
    # caller state. Competing readers of unchanged state share one revision.
    with store.database.engine.connect() as connection:
        store._begin_run_write(connection, f"session-state:{session.id}")
        try:
            with Session(bind=connection) as database:
                projected = _project(database, session.id, runtime)
                digest = hashlib.sha256(
                    json.dumps(
                        projected, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                cached = database.get(SessionProjectionRow, session.id)
                if cached is None:
                    cached = SessionProjectionRow(
                        session_id=session.id, revision=1, digest=digest
                    )
                    database.add(cached)
                elif cached.digest != digest:
                    cached.revision += 1
                    cached.digest = digest
                database.flush()
                projected["revision"] = cached.revision
            connection.commit()
            return projected
        except BaseException:
            connection.rollback()
            raise


def session_projection(store: NebulaStore, session: ChatSession) -> dict[str, Any]:
    """Pure view for catch-up: no connection observation or cached-state writes."""
    with store.database.session() as database:
        return _project(database, session.id)


def _project(database: Session, session_id: str, runtime=None) -> dict[str, Any]:
    row = database.get(EntityRow, session_id)
    if row is None or row.kind != ChatSession.entity_kind:
        raise NotFoundError(f"chat session not found: {session_id}")
    session = ChatSession.model_validate(row.payload)
    # The read uses the same transaction as the cached watermark assignment.
    turn_query = select(EntityRow).where(
        EntityRow.kind == ChatTurn.entity_kind,
        EntityRow.engagement_id == session.engagement_id,
        EntityRow.payload["session_id"].as_string() == session.id,
    )
    turns = [
        ChatTurn.model_validate(row.payload)
        for row in database.scalars(
            turn_query.order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
        )
    ]
    turn_ids = [turn.id for turn in turns]
    approvals = [
        Approval.model_validate(row.payload)
        for row in database.scalars(
            select(EntityRow).where(
                EntityRow.kind == Approval.entity_kind,
                EntityRow.engagement_id == session.engagement_id,
                or_(
                    EntityRow.payload["chat_session_id"].as_string() == session.id,
                    EntityRow.payload["chat_turn_id"].as_string().in_(turn_ids),
                    EntityRow.id.in_(
                        [turn.approval_id for turn in turns if turn.approval_id]
                    ),
                ),
            )
        )
    ]
    questions = [
        HarnessInteraction.model_validate(row.payload)
        for row in database.scalars(
            select(EntityRow).where(
                EntityRow.kind == HarnessInteraction.entity_kind,
                EntityRow.engagement_id == session.engagement_id,
                EntityRow.payload["chat_session_id"].as_string() == session.id,
            )
        )
    ]
    harnesses = {
        row.id: HarnessTurn.model_validate(row.payload)
        for row in database.scalars(
            select(EntityRow).where(
                EntityRow.kind == HarnessTurn.entity_kind,
                EntityRow.engagement_id == session.engagement_id,
                EntityRow.id.in_(
                    [item.harness_turn_id for item in turns if item.harness_turn_id]
                ),
            )
        )
    }
    active_ids = {
        item.id
        for item in turns
        if item.status.value not in TERMINAL
        and (
            item.harness_turn_id not in harnesses
            or harnesses[item.harness_turn_id].status.value not in TERMINAL
        )
    }
    turn = next(
        (item for item in reversed(turns) if item.id in active_ids),
        turns[0] if turns else None,
    )
    harness = (
        harnesses.get(turn.harness_turn_id) if turn and turn.harness_turn_id else None
    )

    active_harness = {item.harness_turn_id for item in turns if item.id in active_ids}
    pending: list[dict[str, Any]] = []
    now = utc_now()
    for approval in approvals:
        owner_id = approval.chat_turn_id or next(
            (item.id for item in turns if item.approval_id == approval.id), None
        )
        if (
            owner_id in active_ids
            and approval.status.value == "pending"
            and (approval.expires_at is None or approval.expires_at > now)
        ):
            pending.append(
                {
                    "id": approval.id,
                    "turn_id": owner_id,
                    "kind": "approval",
                    "text": "Review the requested action",
                    "at": approval.updated_at.isoformat(),
                }
            )
    for question in questions:
        if (
            question.status.value == "pending"
            and question.harness_turn_id in active_harness
        ):
            owner_id = next(
                (
                    item.id
                    for item in turns
                    if item.harness_turn_id == question.harness_turn_id
                ),
                None,
            )
            pending.append(
                {
                    "id": question.id,
                    "turn_id": owner_id,
                    "kind": "input",
                    "text": "A secret answer is required"
                    if question.contains_secret
                    else question.prompt,
                    "at": question.updated_at.isoformat(),
                }
            )
    pending.sort(key=lambda item: (item["at"], item["id"]))
    decisions: list[dict[str, Any]] = [
        {
            "approval_id": item.id,
            "status": item.status.value,
            "continuation": item.continuation.model_dump(mode="json")
            if item.continuation
            else None,
        }
        for item in sorted(approvals, key=lambda item: item.id)
        if item.status.value != "pending"
        and turn
        and (item.chat_turn_id == turn.id or item.id == turn.approval_id)
    ]
    execution = turn.status.value if turn else "idle"
    if harness and execution not in TERMINAL:
        execution = harness.status.value
    execution = {"routing": "running"}.get(execution, execution)
    if execution not in TERMINAL and pending:
        execution = "waiting_approval"
    elif execution == "waiting_approval":
        execution = "continuing" if decisions else "status_unavailable"
    if execution not in TERMINAL and any(
        item["continuation"] and item["continuation"]["status"] == "failed"
        for item in decisions
    ):
        execution = "interrupted"
    detail = {
        "idle": "Ready for your next message.",
        "running": "Working.",
        "queued": "Waiting to start.",
        "waiting_approval": "Review the pending action to continue.",
        "continuing": "Decision recorded; waiting for execution progress.",
        "status_unavailable": "The response is paused, but no actionable request is available. Check status or stop waiting.",
        "complete": "Response complete.",
        "cancelled": "Response stopped.",
        "interrupted": "Response interrupted. No work was replayed; start a new response.",
        "failed": "Response failed. Review the saved error before retrying.",
    }.get(execution, "Checking response status.")
    busy = execution not in TERMINAL and execution != "idle"
    can_stop = busy
    if session.harness_profile_id:
        profile_row = database.get(EntityRow, session.harness_profile_id)
        profile = (
            HarnessProfile.model_validate(profile_row.payload)
            if profile_row and profile_row.kind == HarnessProfile.entity_kind
            else None
        )
        can_stop = bool(busy and profile and profile.capabilities.interruption)
    # An idle but open harness transport is not disconnected. This observation
    # deliberately says nothing about the browser's connection to Core.
    connection = (
        runtime.connection_state(session.harness_session_id)
        if runtime and session.harness_session_id
        else "unknown"
    )
    return {
        "schema": "nebula.session-state/v1",
        "session_id": session.id,
        "turn_id": turn.id if turn else None,
        "harness_turn_id": harness.id if harness else None,
        "execution": execution,
        "pending": pending,
        "decisions": decisions,
        "busy": busy,
        "detail": detail,
        "connection": connection,
        "connection_scope": "harness_transport",
        "actions": [
            "check_status",
            *(["review"] if pending else []),
            *(["stop"] if can_stop else []),
        ],
    }
