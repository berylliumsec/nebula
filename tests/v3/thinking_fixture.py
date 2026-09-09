"""Disposable real Core with synthetic saved thinking; no tools/providers execute."""

import os
import tempfile
from pathlib import Path

import uvicorn
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.storage import NebulaStore
from nebula.v3.domain import (
    Engagement,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    ChatSession,
    ChatMessage,
    ChatQueue,
)

with tempfile.TemporaryDirectory(prefix="nebula-thinking-") as directory:
    root = Path(directory)
    store = NebulaStore(root / "core.db")
    project = store.create(
        Engagement(id="thinking-project", name="Thinking visibility")
    )
    for vendor in ["grok_acp", "codex_app_server"]:
        profile = store.create(
            HarnessProfile(
                id=vendor, name=vendor, kind=vendor, executable="/nonexistent/fixture"
            )
        )
        session = store.create(
            HarnessSession(
                id=vendor + "-session",
                engagement_id=project.id,
                harness_profile_id=profile.id,
                model="fixture",
                status="idle",
            )
        )
        chat = store.create(
            ChatSession(
                id=vendor + "-chat",
                engagement_id=project.id,
                title=vendor + " thinking",
                backend="harness",
                harness_profile_id=profile.id,
                harness_session_id=session.id,
                model="fixture",
            )
        )
        turn = store.create(
            HarnessTurn(
                id=vendor + "-turn",
                engagement_id=project.id,
                harness_session_id=session.id,
                origin="chat",
                chat_session_id=chat.id,
                chat_turn_id=vendor + "-chat-turn",
                status="complete",
                prompt="Synthetic fixture",
                response="Saved final response for " + vendor,
            )
        )
        store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=chat.id,
                sequence=1,
                role="assistant",
                content=turn.response,
                metadata={"harness_turn_id": turn.id},
            )
        )

        def append(kind, item, **fields):
            store.append_operation_event(
                turn.id,
                "harness_turn",
                project.id,
                "harness." + kind,
                {
                    "type": kind,
                    "vendor": vendor,
                    "harness_turn_id": turn.id,
                    "item_id": item,
                    "item_kind": "reasoning",
                    "item_status": "streaming",
                    "title": "Reasoning",
                    "artifact_ids": [],
                    "payload": {},
                    **fields,
                },
            )

        first = "reasoning" if vendor == "grok_acp" else "reasoning-1"
        append(
            "output_delta", first, stream="reasoning_summary", delta="First thinking "
        )
        append(
            "output_delta",
            first,
            stream="reasoning_summary",
            delta="episode from " + vendor,
        )
        if vendor == "grok_acp":
            append(
                "tool_started",
                "saved-tool",
                item_kind="tool",
                title="Inspect fixture",
                tool_name="inspect_fixture",
            )
            append(
                "tool_completed",
                "saved-tool",
                item_kind="tool",
                item_status="completed",
                title="Inspect fixture",
                tool_name="inspect_fixture",
            )
        else:
            append(
                "item_upsert",
                first,
                item_status="completed",
                payload={
                    "reasoning_summary_text": "First thinking episode from " + vendor,
                    "reasoning_summary_state": "available",
                },
            )
        second = "reasoning" if vendor == "grok_acp" else "reasoning-2"
        append(
            "output_delta",
            second,
            stream="reasoning_summary",
            delta="Second thinking episode from " + vendor,
        )
        if vendor == "grok_acp":
            append(
                "output_delta",
                "commentary-1",
                stream="commentary",
                title="Commentary",
                delta="A public progress update.",
            )
            append(
                "output_delta",
                "thinking-3",
                stream="reasoning_summary",
                delta="Long thinking " + "x" * 39986,
            )
            append(
                "output_delta",
                "thinking-3",
                stream="reasoning_summary",
                delta="y" * 40000,
            )
            append("item_upsert", "thinking-3", item_status="completed")
        else:
            append(
                "item_upsert",
                second,
                item_status="completed",
                payload={
                    "reasoning_summary_text": "Second thinking episode from " + vendor,
                    "reasoning_summary_state": "available",
                },
            )

    # Legacy cancellation has events and a user message, but no final assistant row.
    stopped = store.create(
        HarnessTurn(
            id="stopped-turn",
            engagement_id=project.id,
            harness_session_id="grok_acp-session",
            origin="chat",
            chat_session_id="grok_acp-chat",
            chat_turn_id="stopped-chat-turn",
            status="cancelled",
            prompt="Stop this work",
            error="Stopped by operator",
        )
    )
    store.create(
        ChatMessage(
            id="stopped-user",
            engagement_id=project.id,
            session_id="grok_acp-chat",
            sequence=2,
            role="user",
            content="Stop this work",
            metadata={"harness_turn_id": stopped.id},
        )
    )
    store.append_operation_event(
        stopped.id,
        "harness_turn",
        project.id,
        "harness.output_delta",
        {
            "type": "output_delta",
            "vendor": "grok_acp",
            "harness_turn_id": stopped.id,
            "item_id": "commentary",
            "item_kind": "reasoning",
            "item_status": "completed",
            "stream": "commentary",
            "delta": "Recorded work before the operator stopped.",
            "artifact_ids": [],
            "payload": {},
        },
    )
    store.append_operation_event(
        stopped.id,
        "harness_turn",
        project.id,
        "harness.message_delta",
        {
            "type": "message_delta",
            "vendor": "grok_acp",
            "harness_turn_id": stopped.id,
            "delta": "A partial answer retained after stopping.",
            "artifact_ids": [],
            "payload": {},
        },
    )
    store.create(
        ChatMessage(
            id="next-user",
            engagement_id=project.id,
            session_id="grok_acp-chat",
            sequence=3,
            role="user",
            content="My next message after stopping",
        )
    )

    queue_chat = store.create(
        ChatSession(
            id="queue-chat",
            engagement_id=project.id,
            title="Queue reachability",
            backend="harness",
            harness_profile_id="grok_acp",
            harness_session_id="grok_acp-session",
            model="fixture",
        )
    )
    store.create(
        ChatQueue(
            id="chat-queue-queue-chat",
            engagement_id=project.id,
            session_id=queue_chat.id,
            paused=True,
            items=[
                {
                    "id": "review-one",
                    "key": "review-one",
                    "status": "needs_review",
                    "detail": "Review this saved follow-up before retrying.",
                    "request": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Long follow-up content. " * 150,
                            }
                        ]
                    },
                }
            ],
        )
    )

    app = create_app(
        store,
        artifact_store=ArtifactStore(root / "artifacts"),
        auth_token="model-test-token",
        allow_insecure_device_pairing=True,
        static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
    )
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19443"))
    )
