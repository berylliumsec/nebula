import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app, _with_heartbeats
from nebula.v3.chat import ChatService, _ActiveProviderTurn
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
)
from nebula.v3.storage import NebulaStore


def saved_turn(store, backend):
    store.create(Engagement(id="project", name="Project"))
    chat = store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Saved",
            backend=backend,
            model="fixture",
            provider_profile_id="provider" if backend == "provider" else None,
            harness_profile_id="harness" if backend == "harness" else None,
            harness_session_id="session" if backend == "harness" else None,
        )
    )
    harness_id = None
    if backend == "harness":
        store.create(
            HarnessProfile(
                id="harness",
                name="Harness",
                kind="grok_acp",
                executable="/nonexistent/fixture",
            )
        )
        store.create(
            HarnessSession(
                id="session",
                engagement_id="project",
                harness_profile_id="harness",
                model="fixture",
            )
        )
        harness_id = "harness-turn"
        store.create(
            HarnessTurn(
                id=harness_id,
                engagement_id="project",
                harness_session_id="session",
                origin="chat",
                chat_session_id=chat.id,
                chat_turn_id="turn",
                prompt="one request",
                status="complete",
            )
        )
        for text in ("first", "tail"):
            store.append_operation_event(
                harness_id,
                "harness_turn",
                "project",
                "harness.message_delta",
                {
                    "type": "message_delta",
                    "delta": text,
                    "harness_turn_id": harness_id,
                    "payload": {},
                },
            )
    message = store.create(
        ChatMessage(
            id="answer",
            engagement_id="project",
            session_id=chat.id,
            sequence=1,
            role="assistant",
            content="Saved final answer",
        )
    )
    store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id=chat.id,
            backend=backend,
            provider_profile_id="provider" if backend == "provider" else None,
            harness_turn_id=harness_id,
            model="fixture",
            status="complete",
            final_message_id=message.id,
        )
    )


@pytest.mark.parametrize("backend", ["harness", "provider"])
def test_chat_follow_is_read_only_and_returns_saved_completion(tmp_path, backend):
    store = NebulaStore(tmp_path / "core.db")
    saved_turn(store, backend)
    app = create_app(store, auth_token="test-token")
    with TestClient(app) as client:
        before = store.get(ChatTurn, "turn").revision
        response = client.get(
            "/api/v1/chat/turns/turn/events?after=1",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        frames = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert frames[-1]["type"] == "done"
        assert frames[-1]["message"]["content"] == "Saved final answer"
        if backend == "harness":
            assert [f["delta"] for f in frames if f["type"] == "message_delta"] == [
                "tail"
            ]
        assert store.get(ChatTurn, "turn").revision == before
        assert client.get("/api/v1/chat/turns/turn/events").status_code == 401
        assert (
            client.get(
                "/api/v1/chat/turns/turn/events?after=-1",
                headers={"Authorization": "Bearer test-token"},
            ).status_code
            == 422
        )


def test_chat_follow_never_restarts_an_uncertain_provider_turn(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    saved_turn(store, "provider")
    turn = store.get(ChatTurn, "turn")
    store.update(
        ChatTurn,
        turn.id,
        {"status": "interrupted", "final_message_id": None},
        expected_revision=turn.revision,
    )
    app = create_app(store, auth_token="test-token")
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/chat/turns/turn/events",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 409
        assert store.get(ChatTurn, turn.id).status.value == "interrupted"


def test_provider_follow_cursor_is_stable_across_viewers(tmp_path):
    async def scenario():
        service = ChatService(NebulaStore(tmp_path / "core.db"))
        runtime = _ActiveProviderTurn(
            events=[
                ("delta", {"type": "delta", "delta": "first"}),
                ("delta", {"type": "delta", "delta": "second"}),
            ]
        )
        service._active_provider_turns["turn"] = runtime
        first = service.follow_provider_turn("turn")
        assert (await anext(first))[1]["sequence"] == 1
        await first.aclose()
        second = service.follow_provider_turn("turn", after_sequence=1)
        assert (await anext(second))[1] == {
            "type": "delta",
            "delta": "second",
            "sequence": 2,
        }
        await second.aclose()
        assert len(runtime.events) == 2

    asyncio.run(scenario())


def test_heartbeats_do_not_cancel_quiet_work_and_cleanup_pending_reads():
    async def scenario():
        release = asyncio.Event()
        closed = asyncio.Event()

        async def source():
            try:
                await release.wait()
                yield "result"
            finally:
                closed.set()

        follower = _with_heartbeats(source(), "heartbeat", interval=0.001)
        assert await anext(follower) == "heartbeat"
        assert not closed.is_set()
        release.set()
        assert await anext(follower) == "result"
        await follower.aclose()
        assert closed.is_set()

    asyncio.run(scenario())
