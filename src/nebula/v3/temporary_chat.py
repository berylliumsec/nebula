"""Leased conversation branches used only by the Ask Nebula popup."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from .domain import ChatBackend, ChatSession, Engagement, HarnessSession, utc_now
from .storage import NotFoundError

logger = logging.getLogger(__name__)


class TemporaryChatRequest(BaseModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    backend: ChatBackend = ChatBackend.PROVIDER
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=500)


def temporary_chat_router(store, chat_service, harness_runtime):
    async def discard(session_id):
        try:
            chat = store.get(ChatSession, session_id)
        except NotFoundError:
            return
        if not chat.metadata.get("temporary_assistant"):
            raise HTTPException(
                409, "Only temporary assistant conversations can be discarded here"
            )
        turn = chat_service().pending_turn(chat.id)
        if turn:
            if turn.harness_turn_id:
                await harness_runtime.cancel_turn(
                    turn.harness_turn_id, reason="Popup discarded"
                )
            else:
                await chat_service().stop_provider_turn(turn.id)
        if chat.harness_session_id:
            await harness_runtime.close_session(chat.harness_session_id)
        store.delete_chat_session(chat.id)
        # This session belongs exclusively to the popup, never the source chat.
        if chat.harness_session_id:
            with suppress(NotFoundError):
                store.delete(HarnessSession, chat.harness_session_id)

    async def collect():
        while True:
            offset = 0
            expired = []
            while True:
                rows = store.list_entities(
                    ChatSession, include_temporary=True, offset=offset, limit=1000
                )
                expired.extend(
                    row.id
                    for row in rows
                    if row.metadata.get("temporary_assistant")
                    and row.updated_at + timedelta(days=1) < utc_now()
                )
                if len(rows) < 1000:
                    break
                offset += len(rows)
            for session_id in expired:
                try:
                    await discard(session_id)
                except Exception:
                    logger.exception("Temporary assistant cleanup failed; will retry")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(collect())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    router = APIRouter(tags=["chat"], lifespan=lifespan)

    @router.post(
        "/chat/temporary-sessions", response_model=ChatSession, status_code=201
    )
    async def create(request: TemporaryChatRequest):
        store.get(Engagement, request.engagement_id)
        source = (
            store.get(ChatSession, request.session_id) if request.session_id else None
        )
        if source and source.engagement_id != request.engagement_id:
            raise HTTPException(409, "Conversation is not in this project")
        backend = source.backend if source else request.backend
        profile_id = source.harness_profile_id if source else request.harness_profile_id
        model = source.model if source else request.model
        if not source and backend == ChatBackend.PROVIDER and not request.provider_id:
            raise HTTPException(
                422, "Choose an assistant provider before asking a question"
            )
        harness = None
        if backend == ChatBackend.HARNESS:
            # A fresh vendor session receives the snapshot through the existing
            # history-handoff path. It never resumes the parent's live runtime.
            harness = harness_runtime.create_session(
                engagement_id=request.engagement_id,
                profile_id=profile_id or "",
                model=model,
                mcp_server_ids=[],
            )
        messages = chat_service().session_messages(source.id) if source else []
        chat = ChatSession(
            id=str(uuid4()),
            engagement_id=request.engagement_id,
            title="Ask Nebula",
            backend=backend,
            model=model or (harness.model if harness else ""),
            provider_profile_id=source.provider_profile_id
            if source
            else request.provider_id,
            harness_profile_id=profile_id,
            harness_session_id=harness.id if harness else None,
            parent_session_id=source.id if source else None,
            forked_from_message_id=messages[-1].id if messages else None,
            metadata={
                "temporary_assistant": True,
                "harness_context_handoff_pending": bool(harness and messages),
            },
        )
        # The hidden marker and snapshot are committed together: no visible chat
        # is created and then hidden, even while another client refreshes.
        with store.transaction() as transaction:
            transaction.add(chat)
            for message in messages:
                transaction.add(
                    message.model_copy(
                        update={
                            "id": str(uuid4()),
                            "session_id": chat.id,
                            "source_message_id": message.id,
                        }
                    )
                )
        if source and messages:
            from .chat_decisions import fork_decisions

            fork_decisions(store, source, chat, messages[-1].sequence)
        return chat

    @router.post("/chat/temporary-sessions/{session_id}/keepalive", status_code=204)
    async def keepalive(session_id: str):
        chat = store.get(ChatSession, session_id)
        if not chat.metadata.get("temporary_assistant"):
            raise HTTPException(
                409, "Only temporary assistant conversations can be renewed here"
            )
        store.update(ChatSession, chat.id, {"metadata": chat.metadata})
        return Response(status_code=204)

    @router.delete("/chat/temporary-sessions/{session_id}", status_code=204)
    async def delete(session_id: str):
        await discard(session_id)
        return Response(status_code=204)

    return router
