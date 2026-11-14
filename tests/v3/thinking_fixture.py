"""Disposable real Core with synthetic saved thinking; no tools/providers execute."""

import asyncio
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
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-thinking-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        project = await store.create(
            Engagement(id="thinking-project", name="Thinking visibility")
        )
        for vendor in ["grok_acp", "codex_app_server"]:
            profile = await store.create(
                HarnessProfile(
                    id=vendor,
                    name=vendor,
                    kind=vendor,
                    executable="/nonexistent/fixture",
                )
            )
            session = await store.create(
                HarnessSession(
                    id=vendor + "-session",
                    engagement_id=project.id,
                    harness_profile_id=profile.id,
                    model="fixture",
                    status="idle",
                )
            )
            chat = await store.create(
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
            turn = await store.create(
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
            await store.create(
                ChatMessage(
                    engagement_id=project.id,
                    session_id=chat.id,
                    sequence=1,
                    role="assistant",
                    content=turn.response,
                    metadata={"harness_turn_id": turn.id},
                )
            )

            async def append(kind, item, **fields):
                await store.append_operation_event(
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
            await append(
                "output_delta",
                first,
                stream="reasoning_summary",
                delta="First thinking ",
            )
            await append(
                "output_delta",
                first,
                stream="reasoning_summary",
                delta="episode from " + vendor,
            )
            if vendor == "grok_acp":
                await append(
                    "tool_started",
                    "saved-tool",
                    item_kind="tool",
                    title="Inspect fixture",
                    tool_name="inspect_fixture",
                )
                await append(
                    "tool_completed",
                    "saved-tool",
                    item_kind="tool",
                    item_status="completed",
                    title="Inspect fixture",
                    tool_name="inspect_fixture",
                )
            else:
                await append(
                    "item_upsert",
                    first,
                    item_status="completed",
                    payload={
                        "reasoning_summary_text": "First thinking episode from "
                        + vendor,
                        "reasoning_summary_state": "available",
                    },
                )
            second = "reasoning" if vendor == "grok_acp" else "reasoning-2"
            await append(
                "output_delta",
                second,
                stream="reasoning_summary",
                delta="Second thinking episode from " + vendor,
            )
            if vendor == "grok_acp":
                await append(
                    "output_delta",
                    "commentary-1",
                    stream="commentary",
                    title="Commentary",
                    delta="A public progress update.",
                )
                await append(
                    "output_delta",
                    "thinking-3",
                    stream="reasoning_summary",
                    delta="Long thinking " + "x" * 39986,
                )
                await append(
                    "output_delta",
                    "thinking-3",
                    stream="reasoning_summary",
                    delta="y" * 40000,
                )
                await append("item_upsert", "thinking-3", item_status="completed")
            else:
                await append(
                    "item_upsert",
                    second,
                    item_status="completed",
                    payload={
                        "reasoning_summary_text": "Second thinking episode from "
                        + vendor,
                        "reasoning_summary_state": "available",
                    },
                )

            # New long text must survive both diagnostic and UI limits, and differing
            # final summaries must leave the earlier public stream inspectable.
            await append(
                "output_delta",
                "long-summary",
                stream="reasoning_summary",
                delta="Long public text. " * 5000 + "PRESERVED TAIL",
            )
            await append(
                "item_upsert",
                "long-summary",
                item_status="completed",
                payload={
                    "reasoning_summary_text": "Completed summary for " + vendor,
                    "reasoning_summary_source": "completed_item",
                    "reasoning_summary_state": "available",
                },
            )
            await append(
                "item_upsert",
                "no-summary",
                item_status="completed",
                payload={"reasoning_summary_state": "not_provided"},
            )
            await append(
                "item_upsert",
                "old-truncated",
                item_status="completed",
                payload={
                    "reasoning_summary_text": "Historical saved text…[truncated]",
                    "reasoning_summary_state": "available",
                },
            )

        # Codex can finish every reasoning episode without exposing a public
        # summary. Keep this separate from the mixed-summary fixture above.
        no_summary_chat = await store.create(
            ChatSession(
                id="codex-no-summary-chat",
                engagement_id=project.id,
                title="Codex no-summary thinking",
                backend="harness",
                harness_profile_id="codex_app_server",
                harness_session_id="codex_app_server-session",
                model="fixture",
            )
        )
        no_summary_turn = await store.create(
            HarnessTurn(
                id="codex-no-summary-turn",
                engagement_id=project.id,
                harness_session_id="codex_app_server-session",
                origin="chat",
                chat_session_id=no_summary_chat.id,
                chat_turn_id="codex-no-summary-chat-turn",
                status="complete",
                prompt="Synthetic fixture",
                response="Saved answer with no public thinking summary.",
            )
        )
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=no_summary_chat.id,
                sequence=1,
                role="assistant",
                content=no_summary_turn.response,
                metadata={"harness_turn_id": no_summary_turn.id},
            )
        )
        for status, state in [("running", "pending"), ("completed", "not_provided")]:
            await store.append_operation_event(
                no_summary_turn.id,
                "harness_turn",
                project.id,
                "harness.item_upsert",
                {
                    "type": "item_upsert",
                    "vendor": "codex_app_server",
                    "harness_turn_id": no_summary_turn.id,
                    "item_id": "reasoning-without-summary",
                    "item_kind": "reasoning",
                    "item_status": status,
                    "title": "Reasoning",
                    "artifact_ids": [],
                    "payload": {"reasoning_summary_state": state},
                },
            )
        await store.append_operation_event(
            no_summary_turn.id,
            "harness_turn",
            project.id,
            "harness.output_delta",
            {
                "type": "output_delta",
                "vendor": "codex_app_server",
                "harness_turn_id": no_summary_turn.id,
                "item_id": "public-commentary",
                "item_kind": "reasoning",
                "item_status": "completed",
                "title": "Commentary",
                "stream": "commentary",
                "delta": "Checked the public result while Codex kept reasoning private.",
                "artifact_ids": [],
                "payload": {},
            },
        )

        # The agent's Work check-in can live in a child project while its chat
        # remains in the parent, as in real project dashboard usage.
        work_project = await store.create(
            Engagement(
                id="thinking-child-project",
                name="Child work project",
                parent_engagement_id=project.id,
            )
        )
        work = WorkService(store)
        work_item = await work.create(
            work_project.id,
            WorkCreate(
                title="Project progress task",
                status="in_progress",
                source_kind="import",
            ),
            actor_id="fixture",
        )
        await work.check_in(
            work_project.id,
            work_item.id,
            WorkCheckIn(
                summary="Stage: review. The current task is complete and the next step is ready.",
                next_step="Review the next task and confirm the result.",
                status="in_progress",
            ),
            actor_kind="agent",
            actor_id=no_summary_chat.id,
            source_session_id=no_summary_chat.id,
            allow_parent_source_session=True,
        )

        # Legacy cancellation has events and a user message, but no final assistant row.
        stopped = await store.create(
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
        await store.create(
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
        await store.append_operation_event(
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
        await store.append_operation_event(
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
        await store.create(
            ChatMessage(
                id="next-user",
                engagement_id=project.id,
                session_id="grok_acp-chat",
                sequence=3,
                role="user",
                content="My next message after stopping",
            )
        )

        queue_chat = await store.create(
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
        await store.create(
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
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19443")),
            )
        ).serve()


if __name__ == "__main__":
    asyncio.run(main())
