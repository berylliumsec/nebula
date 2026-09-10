"""Real Core with saved synthetic Grok activity and a paused queue; executes no tools."""

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

with tempfile.TemporaryDirectory(prefix="nebula-workspace-recovery-") as directory:
    root = Path(directory)
    store = NebulaStore(root / "core.db")
    project = store.create(Engagement(id="recovery-project", name="Workspace recovery"))
    profile = store.create(
        HarnessProfile(
            id="recovery-profile",
            name="Saved Grok",
            kind="grok_acp",
            executable="/nonexistent/fixture-no-execution",
        )
    )
    session = store.create(
        HarnessSession(
            id="recovery-harness",
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="fixture",
            status="idle",
        )
    )
    chat = store.create(
        ChatSession(
            id="recovery-chat",
            engagement_id=project.id,
            title="Saved workspace activity",
            backend="harness",
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            model="fixture",
        )
    )
    turn = store.create(
        HarnessTurn(
            id="recovery-turn",
            engagement_id=project.id,
            harness_session_id=session.id,
            origin="chat",
            chat_session_id=chat.id,
            chat_turn_id="recovery-chat-turn",
            status="complete",
            prompt="Synthetic saved fixture",
            response="Saved response after partial workspace searches.",
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
    for index, (status, payload) in enumerate(
        [
            (
                "running",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "saved-search",
                    "title": "use_tool",
                    "rawInput": {"tool_name": "nebula__workspace_search_aabbccddeeff"},
                },
            ),
            (
                "failed",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "saved-search",
                    "status": "failed",
                    "rawOutput": {
                        "message": "Mcp error: -32603: search deadline exceeded"
                    },
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": "Tool `nebula__workspace_search_aabbccddeeff` failed via `use_tool`",
                            },
                        }
                    ],
                },
            ),
        ]
    ):
        kind = "tool_started" if index == 0 else "tool_completed"
        store.append_operation_event(
            turn.id,
            "harness_turn",
            project.id,
            "harness." + kind,
            {
                "type": kind,
                "vendor": "grok_acp",
                "harness_turn_id": turn.id,
                "item_id": "saved-search",
                "item_kind": "tool",
                "item_status": status,
                "title": "tool",
                "tool_name": "tool",
                "server_id": "grok",
                "payload": payload,
                "artifact_ids": [],
            },
        )
    store.create(
        ChatQueue(
            id="chat-queue-" + chat.id,
            engagement_id=project.id,
            session_id=chat.id,
            paused=True,
            items=[
                {
                    "id": "queued-one",
                    "key": "queued-one",
                    "status": "queued",
                    "request": {
                        "messages": [
                            {"role": "user", "content": "Long queued follow-up " * 80}
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
        app, host="0.0.0.0", port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19430"))
    )
