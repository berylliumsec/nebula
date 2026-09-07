"""Bounded chat projections; never synthesize artifacts from model claims."""
import re
from hashlib import sha256
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from .database import EntityRow
from .domain import Artifact, ChatMessage, ChatSession, ChatTurn, KnowledgeSource, ToolCall
from .storage import NotFoundError


def results_router(store, artifacts):
    router = APIRouter(tags=["chat"])

    @router.get("/chat/projects/{project_id}/sources/{source_id}/preview")
    def source_preview(project_id: str, source_id: str):
        source = store.get(KnowledgeSource, source_id)
        if source.engagement_id != project_id or not source.artifact_id:
            raise NotFoundError("Retained source is unavailable in this project")
        artifact = store.get(Artifact, source.artifact_id)
        if artifact.engagement_id != project_id or artifacts is None:
            raise NotFoundError("Source artifact is unavailable")
        if artifact.size > 25_000_000:
            raise HTTPException(413, "Select a smaller source or a bounded excerpt")
        from .knowledge import extract_document
        document = extract_document(artifacts.read(artifact), filename=artifact.filename or source.name, media_type=artifact.media_type)
        text = "\n\n".join(section.text for section in document.sections)
        selected = text[:20_000]
        return {"text": selected, "truncated": len(text) > len(selected), "sha256": sha256(selected.encode()).hexdigest(), "label": source.name}

    @router.get("/chat/sessions/{session_id}/results")
    def results(session_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=40, ge=1, le=100)):
        session = store.get(ChatSession, session_id)
        with store.database.session() as database:
            messages = [ChatMessage.model_validate(row.payload) for row in database.scalars(select(EntityRow).where(EntityRow.kind == "chat_messages", EntityRow.engagement_id == session.engagement_id, EntityRow.payload["session_id"].as_string() == session_id).order_by(EntityRow.payload["sequence"].as_integer()).offset(offset).limit(limit + 1))]
            calls = [ToolCall.model_validate(row.payload) for row in database.scalars(select(EntityRow).where(EntityRow.kind == "tool_calls", EntityRow.engagement_id == session.engagement_id, EntityRow.payload["chat_session_id"].as_string() == session_id))]
        items = []
        for message in messages[:limit]:
            if message.role.value != "assistant":
                continue
            for index, block in enumerate(message.content_blocks):
                if block.type in ("artifact", "image", "code"):
                    if block.artifact_id or block.type == "code":
                        items.append({"id": f"{message.id}-block-{index}", "message_id": message.id, "kind": block.type, "label": block.alt or block.language or "Retained output", "artifact_id": block.artifact_id, "text": block.text})
            for index, match in enumerate(re.finditer(r"```([^\n]*)\n([\s\S]*?)```", message.content)):
                items.append({"id": f"{message.id}-code-{index}", "message_id": message.id, "kind": "code", "label": match[1] or "Code excerpt", "text": match[2], "artifact_id": None})
            turn_id = message.metadata.get("harness_turn_id")
            if turn_id and artifacts is not None:
                with store.database.session() as database:
                    diffs = [Artifact.model_validate(row.payload) for row in database.scalars(select(EntityRow).where(EntityRow.kind == "artifacts", EntityRow.engagement_id == session.engagement_id, EntityRow.payload["source"].as_string() == "harness-file-diff", EntityRow.payload["metadata"]["harness_turn_id"].as_string() == turn_id))]
                for diff in diffs:
                    with artifacts.path_for(diff).open("rb") as stream:
                        preview = stream.read(8192).decode("utf-8", errors="replace")
                    items.append({"id": diff.id, "message_id": message.id, "kind": "file_change", "label": "Recorded file changes (bounded preview)", "text": preview, "artifact_id": diff.id})
            for citation in message.citations:
                items.append({"id": f"{message.id}-{citation.chunk_id}", "message_id": message.id, "kind": "citation", "label": citation.name, "text": citation.excerpt, "source_id": citation.source_id})
            for call in calls:
                if not call.chat_turn_id:
                    continue
                try:
                    turn = store.get(ChatTurn, call.chat_turn_id)
                except NotFoundError:
                    continue
                if turn.final_message_id == message.id:
                    items.append({"id": call.id, "message_id": message.id, "kind": "tool", "label": call.tool_name, "tool_call_id": call.id, "artifact_id": call.result_artifact_id, "status": call.status, "text": call.error})
        return {"items": items, "next_offset": offset + limit if len(messages) > limit else None}

    @router.get("/chat/sessions/{session_id}/context-sources")
    def context_sources(session_id: str, offset: int = Query(default=0, ge=0)):
        session = store.get(ChatSession, session_id)
        with store.database.session() as database:
            rows = database.scalars(select(EntityRow).where(EntityRow.kind == "chat_messages", EntityRow.engagement_id == session.engagement_id, EntityRow.payload["session_id"].as_string() == session_id).order_by(EntityRow.payload["sequence"].as_integer().desc()).offset(offset).limit(41))
            messages = [ChatMessage.model_validate(row.payload) for row in rows]
        from .chat import _CHAT_INSTRUCTIONS
        return {"items": [{"message_id": m.id, "sequence": m.sequence, "attachments": m.metadata.get("context_attachments", [])} for m in messages[:40] if m.metadata.get("context_attachments")], "next_offset": offset + 40 if len(messages) > 40 else None, "core_instructions": _CHAT_INSTRUCTIONS if session.backend.value == "provider" else None, "instruction_note": "Core assistant policy; retrieval and operator context are additional inputs." if session.backend.value == "provider" else "Harness internal instructions are not fully exposed to Nebula."}
    return router
