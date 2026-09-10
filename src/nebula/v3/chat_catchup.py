"""Deterministic catch-up and provenance projections, without model requests."""

from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING
from hashlib import sha256
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from .database import EntityRow
from .domain import (
    ChatReadCursor,
    ChatMessage,
    ChatSession,
    ChatTurn,
    utc_now,
)
from .chat_naming import substantive_prompt
from .storage import ConflictError, NotFoundError
from .tool_results import sanitize_model_history_result


if TYPE_CHECKING:
    from .harnesses import HarnessRuntimeService


def cursor_id(session_id, device_id):
    return "chat-read-" + sha256(f"{session_id}:{device_id}".encode()).hexdigest()


def source_message(database, session, turn):
    if turn.final_message_id:
        row = database.get(EntityRow, turn.final_message_id)
        if row and row.payload.get("session_id") == session.id:
            return ChatMessage.model_validate(row.payload)
    row = database.scalar(
        select(EntityRow)
        .where(
            EntityRow.kind == "chat_messages",
            EntityRow.payload["session_id"].as_string() == session.id,
            EntityRow.payload["role"].as_string() == "user",
            EntityRow.created_at >= turn.created_at,
        )
        .order_by(EntityRow.created_at)
        .limit(1)
    )
    return ChatMessage.model_validate(row.payload) if row else None


def catchup_projection(store, session, cursor):
    through = utc_now()
    items = []
    truncated = False
    with store.database.session() as database:
        turns = [
            ChatTurn.model_validate(row.payload)
            for row in database.scalars(
                select(EntityRow)
                .where(
                    EntityRow.kind == "chat_turns",
                    EntityRow.payload["session_id"].as_string() == session.id,
                )
                .order_by(EntityRow.updated_at.desc())
                .limit(101)
            )
        ]
        for turn in turns[:100]:
            message = source_message(database, session, turn)
            entry = {
                "id": turn.id,
                "turn_id": turn.id,
                "message_id": message.id if message else None,
                "at": turn.updated_at.isoformat(),
            }
            if (
                cursor
                and turn.updated_at > cursor.through_at
                and turn.status.value in {"failed", "interrupted", "cancelled"}
            ):
                items.append(
                    {
                        **entry,
                        "kind": "failure",
                        "text": f"Response {turn.status.value}: {turn.error or 'Inspect the recorded response'}",
                    }
                )
        if cursor:
            candidates = [
                ChatMessage.model_validate(row.payload)
                for row in database.scalars(
                    select(EntityRow)
                    .where(
                        EntityRow.kind == "chat_messages",
                        EntityRow.payload["session_id"].as_string() == session.id,
                        EntityRow.payload["role"].as_string() == "assistant",
                        EntityRow.created_at > cursor.through_at,
                        EntityRow.created_at <= through,
                    )
                    .order_by(EntityRow.created_at.desc())
                    .limit(101)
                )
            ]
            truncated = len(candidates) > 100 or len(turns) > 100
            for message in candidates[:100]:
                user_row = database.scalar(
                    select(EntityRow)
                    .where(
                        EntityRow.kind == "chat_messages",
                        EntityRow.payload["session_id"].as_string() == session.id,
                        EntityRow.payload["role"].as_string() == "user",
                        EntityRow.payload["sequence"].as_integer() < message.sequence,
                    )
                    .order_by(EntityRow.payload["sequence"].as_integer().desc())
                    .limit(1)
                )
                user_prompt = user_row.payload.get("content", "") if user_row else ""
                outputs = bool(
                    message.citations
                    or any(
                        block.type in {"artifact", "image", "code"}
                        for block in message.content_blocks
                    )
                    or "```" in message.content
                    or message.metadata.get("tool_results")
                )
                if not outputs and not substantive_prompt([user_prompt]):
                    continue
                items.append(
                    {
                        "id": message.id,
                        "message_id": message.id,
                        "turn_id": message.metadata.get("chat_turn_id"),
                        "at": message.created_at.isoformat(),
                        "kind": "results" if outputs else "completed",
                        "text": message.content[:240],
                    }
                )
    items.sort(key=lambda item: item["at"], reverse=True)
    from .session_state import session_state

    # The read cursor owns what is unseen, not whether an action is pending.
    pending = [
        {**item, "kind": "pending", "message_id": None}
        for item in session_state(store, session)["pending"]
    ]
    unseen_pending = [
        entry
        for entry in pending
        if cursor and datetime.fromisoformat(entry["at"]) > cursor.through_at
    ]
    return {
        "revision": cursor.revision if cursor else 0,
        "initialized": cursor is not None,
        "through_at": through.isoformat(),
        "items": [*unseen_pending, *items][:50],
        "pending": pending,
        "truncated": truncated or len(items) + len(unseen_pending) > 50,
    }


class CursorWrite(BaseModel):
    expected_revision: int = Field(ge=0)
    through_at: datetime
    device_id: str = Field(min_length=1, max_length=200)


def catchup_router(store, harness: HarnessRuntimeService | None):
    router = APIRouter(tags=["chat"])

    def device(request, supplied):
        return getattr(request.state, "auth_device_id", None) or supplied

    @router.get("/chat/sessions/{session_id}/catch-up")
    def read(
        session_id: str,
        request: Request,
        device_id: str = Query(min_length=1, max_length=200),
    ):
        session = store.get(ChatSession, session_id)
        try:
            cursor = store.get(
                ChatReadCursor, cursor_id(session_id, device(request, device_id))
            )
        except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
            cursor = None
        return catchup_projection(store, session, cursor)

    @router.put("/chat/sessions/{session_id}/read-cursor")
    def advance(session_id: str, request: Request, body: CursorWrite):
        session = store.get(ChatSession, session_id)
        owner = device(request, body.device_id)
        identity = cursor_id(session_id, owner)
        if body.through_at.tzinfo is None or body.through_at > utc_now():
            raise HTTPException(422, "Read cursor must use a recorded server timestamp")
        if body.expected_revision == 0:
            return store.create(
                ChatReadCursor(
                    id=identity,
                    engagement_id=session.engagement_id,
                    session_id=session_id,
                    device_id=owner,
                    through_at=body.through_at,
                )
            )
        current = store.get(ChatReadCursor, identity)
        if body.through_at < current.through_at:
            raise ConflictError(
                "This device has already read newer activity. Reload its cursor"
            )
        return store.update(
            ChatReadCursor,
            identity,
            {"through_at": body.through_at},
            expected_revision=body.expected_revision,
        )

    @router.get("/chat/sessions/{session_id}/turns/{turn_id}/summary")
    def turn_summary(session_id: str, turn_id: str):
        session = store.get(ChatSession, session_id)
        turn = store.get(ChatTurn, turn_id)
        if turn.session_id != session_id:
            raise HTTPException(404, "Response does not belong to this conversation")
        with store.database.session() as database:
            message = source_message(database, session, turn)
        return {
            "id": turn.id,
            "status": turn.status,
            "error": turn.error,
            "model": turn.model,
            "created_at": turn.created_at,
            "updated_at": turn.updated_at,
            "message_id": message.id if message else None,
        }

    @router.get("/chat/sessions/{session_id}/messages/{message_id}/evidence")
    def evidence(session_id: str, message_id: str):
        session = store.get(ChatSession, session_id)
        message = store.get(ChatMessage, message_id)
        if (
            message.session_id != session_id
            or message.engagement_id != session.engagement_id
            or message.role.value != "assistant"
        ):
            raise HTTPException(
                404, "Assistant answer does not belong to this conversation"
            )
        observations, artifacts, limitations = [], [], []
        for _ in range(32):
            if not message.source_message_id:
                break
            try:
                original = store.get(ChatMessage, message.source_message_id)
            except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
                limitations.append(
                    "The original branch source is no longer retained; only copied references remain"
                )
                break
            if original.engagement_id != session.engagement_id:
                raise HTTPException(
                    404, "Branch provenance does not match this project"
                )
            message = original
        turn_id = message.metadata.get("chat_turn_id")
        if turn_id:
            try:
                turn = store.get(ChatTurn, turn_id)
            except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
                turn = None
                limitations.append(
                    "The original response receipt is no longer retained"
                )
            if turn and turn.session_id != message.session_id:
                raise HTTPException(
                    404, "Recorded turn does not match this conversation"
                )
            for entry in turn.tool_history if turn else []:
                value = entry.get("provider_result")
                if not isinstance(value, (str, dict)):
                    continue
                receipt = sanitize_model_history_result(
                    value,
                    tool_call_id=str(
                        entry.get("tool_call_id") or entry.get("model_call_id") or ""
                    ),
                    tool_name=str(entry.get("name") or ""),
                    trusted_result=entry.get("trusted_result") is True,
                )
                observations.append(
                    {
                        "id": entry.get("tool_call_id") or entry.get("model_call_id"),
                        "kind": "observed",
                        "label": entry.get("name"),
                        "status": entry.get("status"),
                        "receipt": receipt,
                        "artifact_id": entry.get("result_artifact_id"),
                        "evidence_ids": entry.get("evidence_ids", []),
                    }
                )
        harness_turn_id = message.metadata.get("harness_turn_id")
        if harness_turn_id:
            from .domain import HarnessTurn

            try:
                turn = store.get(HarnessTurn, harness_turn_id)
            except NotFoundError:  # diagnostic-expected: optional or removed historical record remains unavailable
                turn = None
                limitations.append(
                    "The original harness event ledger is no longer retained"
                )
            if turn and turn.chat_session_id != message.session_id:
                raise HTTPException(
                    404, "Recorded harness turn does not match this conversation"
                )
            events = (
                harness.activity_events(harness_turn_id, limit=10000).events
                if turn and harness is not None
                else []
            )
            for event in events:
                if (
                    event.type in {"tool_completed", "checkpoint", "error"}
                    or event.item_kind in {"file_change", "image"}
                    and event.type in {"item_completed", "item_upsert"}
                ):
                    observations.append(
                        {
                            "id": event.id,
                            "kind": "reported"
                            if event.type == "checkpoint"
                            else "observed",
                            "label": event.title or event.type.replace("_", " "),
                            "status": event.item_status,
                            "summary": event.summary or event.message,
                            "artifact_ids": event.artifact_ids,
                        }
                    )
            if len(events) >= 10000:
                limitations.append(
                    "Only the first 10,000 recorded events were inspected; open the work ledger for the remainder"
                )
        for block in message.content_blocks:
            if block.artifact_id:
                artifacts.append(
                    {
                        "id": block.artifact_id,
                        "kind": block.type,
                        "label": block.alt or "Retained artifact reference",
                    }
                )
        if not message.citations and not observations and not artifacts:
            limitations.append(
                "No supporting citations, receipts or retained artifact references are recorded for this answer"
            )
        limitations.append(
            "An assistant interpretation is not an independently verified observation. Source presence does not establish correctness"
        )
        return {
            "message_id": message_id,
            "source_message_id": message.id,
            "source_session_id": message.session_id,
            "retrieved": [item.model_dump(mode="json") for item in message.citations],
            "observations": observations,
            "artifacts": artifacts,
            "limitations": limitations,
        }

    return router
