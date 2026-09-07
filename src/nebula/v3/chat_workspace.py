"""Conversation navigation projections over canonical messages, with durable bookmarks."""
from hashlib import sha256
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, exists
from sqlalchemy.orm import aliased
from .database import EntityRow
from .domain import ChatBookmark, ChatMessage, ChatSession, Engagement
from .storage import NebulaStore, NotFoundError, ConflictError

class BookmarkWrite(BaseModel):
    active: bool
    expected_revision: int = Field(ge=0)


def bookmark_id(session_id: str, message_id: str) -> str:
    return "bookmark-" + sha256(f"{session_id}:{message_id}".encode()).hexdigest()


def workspace_router(store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["chat"])

    @router.get("/chat/projects/{project_id}/search")
    def search(project_id: str, q: str = Query(default="", max_length=512), session_id: str | None = None, bookmarked: bool = False, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        store.get(Engagement, project_id)
        statement = select(EntityRow).where(EntityRow.kind == "chat_messages", EntityRow.engagement_id == project_id)
        if session_id:
            session = store.get(ChatSession, session_id)
            if session.engagement_id != project_id:
                raise NotFoundError("Conversation is not in this project")
            statement = statement.where(EntityRow.payload["session_id"].as_string() == session_id)
        if q.strip():
            statement = statement.where(EntityRow.payload["content"].as_string().icontains(q.strip(), autoescape=True))
        if bookmarked:
            mark = aliased(EntityRow)
            statement = statement.where(exists(select(mark.id).where(mark.kind == "chat_bookmarks", mark.engagement_id == project_id, mark.payload["message_id"].as_string() == EntityRow.id, mark.payload["active"].as_boolean().is_(True))))
        statement = statement.order_by(EntityRow.created_at, EntityRow.id).offset(offset).limit(limit + 1)
        with store.database.session() as database:
            rows = [ChatMessage.model_validate(row.payload) for row in database.scalars(statement)]
        items = []
        for message in rows[:limit]:
            text = message.content
            start = max(0, text.casefold().find(q.strip().casefold()) - 80) if q.strip() else 0
            session = store.get(ChatSession, message.session_id)
            items.append({"message_id": message.id, "session_id": session.id, "title": session.title, "role": message.role, "excerpt": text[start:start+400], "sequence": message.sequence, "created_at": message.created_at})
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/chat/sessions/{session_id}/bookmarks")
    def bookmarks(session_id: str):
        session = store.get(ChatSession, session_id)
        with store.database.session() as database:
            rows = database.scalars(select(EntityRow).where(EntityRow.kind == "chat_bookmarks", EntityRow.engagement_id == session.engagement_id, EntityRow.payload["session_id"].as_string() == session_id))
            return [ChatBookmark.model_validate(row.payload) for row in rows]

    @router.put("/chat/sessions/{session_id}/bookmarks/{message_id}")
    def set_bookmark(session_id: str, message_id: str, request: BookmarkWrite):
        session = store.get(ChatSession, session_id)
        message = store.get(ChatMessage, message_id)
        if message.session_id != session.id or message.engagement_id != session.engagement_id:
            raise NotFoundError("Message is not in this conversation")
        identity = bookmark_id(session_id, message_id)
        if request.expected_revision == 0:
            return store.create(ChatBookmark(id=identity, engagement_id=session.engagement_id, session_id=session.id, message_id=message.id, active=request.active))
        return store.update(ChatBookmark, identity, {"active": request.active}, expected_revision=request.expected_revision)

    return router
