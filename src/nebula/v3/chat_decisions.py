"""Explicit operator decisions with revision history and dispatch-time snapshots."""

from __future__ import annotations
import json
from typing import Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from .database import EntityRow
from .domain import ChatDecision, ChatMessage, ChatSession
from .storage import ConflictError


def decisions_for(store, session_id, project_id):
    with store.database.session() as database:
        rows = database.scalars(
            select(EntityRow)
            .where(
                EntityRow.kind == "chat_decisions",
                EntityRow.engagement_id == project_id,
                or_(
                    EntityRow.payload["session_id"].as_string() == session_id,
                    EntityRow.payload["scope"].as_string() == "project",
                ),
            )
            .order_by(EntityRow.created_at, EntityRow.id)
        )
        return [ChatDecision.model_validate(row.payload) for row in rows]


def decision_snapshot(store, session_id, project_id):
    entries = decisions_for(store, session_id, project_id) if project_id else []
    active = [
        {
            "id": item.id,
            "revision": item.revision,
            "kind": item.kind,
            "text": item.text,
            "scope": item.scope,
            "source_message_id": item.source_message_id,
            "source_session_id": item.source_session_id,
        }
        for item in entries
        if item.status == "active"
    ]
    if len(active) > 100 or sum(len(item["text"]) for item in active) > 40000:
        raise ConflictError(
            "Active operator context is too large. Supersede or remove decisions in Context before sending"
        )
    return active


def decision_instructions(snapshot):
    if not snapshot:
        return ""
    return (
        "\n\nOperator context — explicitly saved decisions and constraints:\nUnresolved assumptions remain questions, not established facts. If entries conflict, surface the conflict for the operator; do not silently pick a winner.\n"
        + json.dumps(snapshot, ensure_ascii=False)
    )


def fork_decisions(store, source, fork, boundary_sequence):
    for item in decisions_for(store, source.id, source.engagement_id):
        if (
            item.scope != "conversation"
            or item.status != "active"
            or item.effective_sequence > boundary_sequence
        ):
            continue
        store.create(
            ChatDecision(
                engagement_id=fork.engagement_id,
                session_id=fork.id,
                kind=item.kind,
                text=item.text,
                source_message_id=item.source_message_id,
                source_session_id=item.source_session_id,
                source_selection=item.source_selection,
                effective_sequence=item.effective_sequence,
                copied_from_id=item.id,
                copied_from_revision=item.revision,
            )
        )


class DecisionWrite(BaseModel):
    expected_revision: int = Field(ge=0)
    action: str = Field(pattern="^(save|supersede|remove|promote)$", default="save")
    kind: str = Field(pattern="^(decision|constraint|assumption)$", default="decision")
    text: str = Field(default="", max_length=4000)
    source_message_id: str | None = None
    source_selection: str | None = Field(default=None, max_length=200000)


def decisions_router(store):
    router = APIRouter(tags=["chat"])

    @router.get("/chat/sessions/{session_id}/decisions")
    def read(session_id: str):
        session = store.get(ChatSession, session_id)
        return decisions_for(store, session_id, session.engagement_id)

    @router.put("/chat/sessions/{session_id}/decisions/{decision_id}")
    def write(session_id: str, decision_id: str, body: DecisionWrite):
        session = store.get(ChatSession, session_id)
        if len(decision_id) > 200:
            raise HTTPException(422, "Decision identity is too long")
        if body.expected_revision == 0:
            if body.action != "save" or not body.text.strip():
                raise HTTPException(422, "Write the decision before saving")
            source = (
                store.get(ChatMessage, body.source_message_id)
                if body.source_message_id
                else None
            )
            if source and source.session_id != session_id:
                raise HTTPException(
                    404, "Source message does not belong to this conversation"
                )
            if body.source_selection and (
                not source or body.source_selection not in source.content
            ):
                raise HTTPException(
                    422, "Source selection must be exact text from the saved message"
                )
            with store.database.session() as database:
                latest = (
                    database.scalar(
                        select(EntityRow.payload["sequence"].as_integer())
                        .where(
                            EntityRow.kind == "chat_messages",
                            EntityRow.payload["session_id"].as_string() == session_id,
                        )
                        .order_by(EntityRow.payload["sequence"].as_integer().desc())
                        .limit(1)
                    )
                    or 0
                )
            return store.create(
                ChatDecision(
                    id=decision_id,
                    engagement_id=session.engagement_id,
                    session_id=session_id,
                    kind=body.kind,
                    text=body.text,
                    source_message_id=source.id if source else None,
                    source_session_id=session_id if source else None,
                    source_selection=body.source_selection,
                    effective_sequence=source.sequence if source else latest,
                )
            )
        current = store.get(ChatDecision, decision_id)
        if current.engagement_id != session.engagement_id or (
            current.scope == "conversation" and current.session_id != session_id
        ):
            raise HTTPException(404, "Decision does not belong to this project chat")
        if current.revision != body.expected_revision:
            raise ConflictError(
                "Decision changed on another device. Reload and reapply your edit"
            )
        if body.action == "promote":
            if current.scope != "conversation" or current.status != "active":
                raise ConflictError("Only active conversation entries can be promoted")
            promoted = ChatDecision(
                id="project-" + current.id,
                engagement_id=current.engagement_id,
                session_id=None,
                scope="project",
                kind=current.kind,
                text=current.text,
                source_message_id=current.source_message_id,
                source_session_id=current.source_session_id,
                source_selection=current.source_selection,
                effective_sequence=current.effective_sequence,
                copied_from_id=current.id,
                copied_from_revision=current.revision,
            )
            # Supersede the local copy atomically to prevent injecting it twice.
            with store.transaction() as tx:
                tx.update(
                    ChatDecision,
                    current.id,
                    {
                        "status": "superseded",
                        "history": [*current.history, revision_entry(current)],
                    },
                    expected_revision=current.revision,
                )
                tx.add(promoted)
            return promoted
        if current.status != "active":
            raise ConflictError(
                "This entry is no longer active. Create a new entry to restore it explicitly"
            )
        changes: dict[str, Any] = {
            "history": [*current.history, revision_entry(current)]
        }
        if body.action == "save":
            if not body.text.strip():
                raise HTTPException(422, "Decision text cannot be empty")
            changes.update(text=body.text, kind=body.kind)
        else:
            changes["status"] = "removed" if body.action == "remove" else "superseded"
        return store.update(
            ChatDecision, current.id, changes, expected_revision=current.revision
        )

    return router


def revision_entry(item):
    return {
        "revision": item.revision,
        "text": item.text,
        "kind": item.kind,
        "status": item.status,
        "updated_at": item.updated_at.isoformat(),
    }
