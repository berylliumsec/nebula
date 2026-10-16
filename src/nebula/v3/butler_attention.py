"""Durable Butler attention for work that is waiting on a review or answer.

The source record is authoritative. Incidents follow its pending state and the
project event contains IDs and a short label, never an approval's arguments or
a harness question's answer (which may contain a secret).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from .butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    _entity_in_transaction,
    incident_id,
)
from .domain import (
    Approval,
    ApprovalStatus,
    ButlerIncident,
    ChatGoal,
    ChatQueue,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    HarnessInteraction,
    HarnessInteractionStatus,
    ToolCallOrigin,
    utc_now,
)

if TYPE_CHECKING:
    from .storage import NebulaStore, StoreTransaction


def _attention_items(
    entity: Approval | HarnessInteraction | ChatQueue,
) -> dict[str, dict]:
    if isinstance(entity, Approval):
        return (
            {entity.id: {"kind": "approval", "entity": entity}}
            if entity.status == ApprovalStatus.PENDING
            else {}
        )
    if isinstance(entity, HarnessInteraction):
        return (
            {entity.id: {"kind": "question", "entity": entity}}
            if entity.status == HarnessInteractionStatus.PENDING
            else {}
        )
    return {
        str(item["id"]): {"kind": "queue", "entity": entity, "item": item}
        for item in entity.items
        if item.get("status") == "needs_review" and isinstance(item.get("id"), str)
    }


def _incident_key(kind: str, entity_id: str, item_id: str) -> str:
    digest = hashlib.sha256(f"{entity_id}\0{item_id}".encode()).hexdigest()
    return f"review:{kind}:{digest}"


def _details(
    kind: str, entity: Approval | HarnessInteraction | ChatQueue, item_id: str
) -> tuple[str, str, str, list[ButlerEvidenceRef], list[str]]:
    project_id = entity.engagement_id
    if kind == "approval":
        assert isinstance(entity, Approval)
        refs = [ButlerEvidenceRef(kind="approval", id=entity.id, project_id=project_id)]
        session_ids = (
            [entity.chat_session_id]
            if entity.origin == ToolCallOrigin.CHAT and entity.chat_session_id
            else []
        )
        if entity.origin == ToolCallOrigin.MISSION:
            refs.append(
                ButlerEvidenceRef(
                    kind="mission", id=entity.run_id, project_id=project_id
                )
            )
        return (
            "An agent request needs approval.",
            "A chat or mission is waiting for a decision on an exact saved request.",
            "Review the request and its effects before deciding.",
            refs,
            session_ids,
        )
    if kind == "question":
        assert isinstance(entity, HarnessInteraction)
        refs = [
            ButlerEvidenceRef(
                kind="harness_interaction", id=entity.id, project_id=project_id
            )
        ]
        if entity.run_id:
            refs.append(
                ButlerEvidenceRef(
                    kind="mission", id=entity.run_id, project_id=project_id
                )
            )
        return (
            "An agent needs an answer.",
            "A chat or mission is waiting for operator input.",
            "Read the question; secret answers belong in the private work screen.",
            refs,
            [entity.chat_session_id] if entity.chat_session_id else [],
        )
    assert isinstance(entity, ChatQueue)
    refs = [ButlerEvidenceRef(kind="chat_queue", id=entity.id, project_id=project_id)]
    return (
        "A chat follow-up needs review.",
        "A queued follow-up stopped and needs its saved outcome checked.",
        "Inspect the parent chat before retrying or changing its work.",
        refs,
        [entity.session_id],
    )


def record_attention_transition(
    transaction: StoreTransaction,
    before: Approval | HarnessInteraction | ChatQueue | None,
    after: Approval | HarnessInteraction | ChatQueue,
) -> None:
    """Record pending and resolved states in the source write transaction."""

    if before is not None and (
        before.id != after.id or before.engagement_id != after.engagement_id
    ):
        raise ValueError("attention record changed identity or project")
    previous = _attention_items(before) if before is not None else {}
    current = _attention_items(after)
    if not (current.keys() - previous.keys() or previous.keys() - current.keys()):
        return
    now = utc_now()
    for item_id in current.keys() - previous.keys():
        kind = current[item_id]["kind"]
        key = _incident_key(kind, after.id, item_id)
        summary, impact, next_action, refs, sessions = _details(kind, after, item_id)
        record_id = incident_id(after.engagement_id, key)
        existing = _entity_in_transaction(transaction, ButlerIncident, record_id)
        if existing is None:
            transaction.add(
                ButlerIncident(
                    id=record_id,
                    engagement_id=after.engagement_id,
                    incident_key=key,
                    source=f"pending_{kind}",
                    verification_state="core_observed",
                    summary=summary,
                    impact=impact,
                    next_action=next_action,
                    evidence_refs=[ref.model_dump() for ref in refs],
                    session_ids=sessions,
                    observed_at=now,
                )
            )
        elif existing.status == "resolved":
            transaction.update(
                ButlerIncident,
                record_id,
                {
                    "status": "open",
                    "observed_at": now,
                    "resolved_at": None,
                    "acknowledged_at": None,
                },
                expected_revision=existing.revision,
            )
        else:
            continue
        _append_project_event(
            transaction,
            engagement_id=after.engagement_id,
            event_type=f"review.{kind}_requested",
            payload=ButlerEventPayload(
                project_id=after.engagement_id,
                session_id=sessions[0] if sessions else None,
                source="core",
                actor_role="core",
                summary=summary,
                metrics={
                    "attention_id": item_id[:200],
                    "source_revision": after.revision,
                },
                verification_state="core_observed",
                evidence_refs=refs,
                incident_key=key,
            ),
            idempotency_key=f"{key}:{after.revision}:pending",
            now=now,
        )
    for item_id in previous.keys() - current.keys():
        kind = previous[item_id]["kind"]
        key = _incident_key(kind, after.id, item_id)
        record_id = incident_id(after.engagement_id, key)
        existing = _entity_in_transaction(transaction, ButlerIncident, record_id)
        if existing is None or existing.status == "resolved":
            continue
        transaction.update(
            ButlerIncident,
            record_id,
            {
                "status": "resolved",
                "resolved_at": now,
            },
            expected_revision=existing.revision,
        )
        _append_project_event(
            transaction,
            engagement_id=after.engagement_id,
            event_type="review.resolved",
            payload=ButlerEventPayload(
                project_id=after.engagement_id,
                source="core",
                actor_role="core",
                summary="A pending review or question was resolved.",
                verification_state="core_observed",
                incident_key=key,
                evidence_refs=[
                    ButlerEvidenceRef(
                        kind="incident", id=record_id, project_id=after.engagement_id
                    )
                ],
            ),
            idempotency_key=f"{key}:{after.revision}:resolved",
            now=now,
        )


def reconcile_attention(store: NebulaStore) -> None:
    """Recover pending work created before this watcher was installed or restarted."""

    for model in (Approval, HarnessInteraction, ChatQueue):
        for entity in store.iter_readable_entities(model):
            if not _attention_items(entity):
                continue
            with store.transaction() as transaction:
                record_attention_transition(transaction, None, entity)


def record_completed_goal_turn(transaction: StoreTransaction, turn: ChatTurn) -> None:
    """Queue a bounded semantic check of one saved agent reply."""

    if (
        turn.status != ChatTurnStatus.COMPLETE
        or not turn.goal_id
        or not turn.final_message_id
    ):
        return
    goal = _entity_in_transaction(transaction, ChatGoal, turn.goal_id)
    session = _entity_in_transaction(transaction, ChatSession, turn.session_id)
    if (
        goal is None
        or session is None
        or goal.engagement_id != turn.engagement_id
        or session.engagement_id != turn.engagement_id
        or goal.session_id != session.id
    ):
        return
    project_id = turn.engagement_id
    _append_project_event(
        transaction,
        engagement_id=project_id,
        event_type="agent.turn_completed",
        payload=ButlerEventPayload(
            project_id=project_id,
            goal_id=goal.id,
            session_id=session.id,
            source="core",
            actor_role="subagent" if goal.parent_goal_id else "main_agent",
            parent_goal_id=goal.parent_goal_id,
            summary="A goal agent completed a turn; its saved reply may request review.",
            metrics={"turn_revision": turn.revision},
            verification_state="agent_reported",
            evidence_refs=[
                ButlerEvidenceRef(kind="goal", id=goal.id, project_id=project_id),
                ButlerEvidenceRef(kind="chat_turn", id=turn.id, project_id=project_id),
            ],
        ),
        idempotency_key=f"goal-turn:{turn.id}:completed",
        now=utc_now(),
    )
