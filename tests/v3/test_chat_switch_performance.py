from __future__ import annotations
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta
import time
from types import SimpleNamespace
import zlib

from fastapi.responses import Response, StreamingResponse
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatService
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    Approval,
    ChatDecision,
    ChatMessage,
    ChatRole,
    ChatSession,
    Engagement,
    ProviderProfile,
    RiskClass,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.storage import NebulaStore


AUTH = {"Authorization": "Bearer test-token"}


async def _store(tmp_path) -> NebulaStore:
    store = NebulaStore(tmp_path / "chat-switch.db")
    (await store.create(Engagement(id="project", name="Project")))
    (
        await store.create(
            ProviderProfile(
                id="provider",
                name="Provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                metadata={"default_model": "model-a"},
            )
        )
    )
    for session_id in ("session-a", "session-b"):
        (
            await store.create(
                ChatSession(
                    id=session_id,
                    engagement_id="project",
                    title=session_id,
                    provider_profile_id="provider",
                    model="model-a",
                )
            )
        )
    return store


async def test_chat_lookup_projection_tracks_writes_orders_history_and_uses_index(
    tmp_path,
):
    store = await _store(tmp_path)
    now = utc_now()
    (
        await store.create_many(
            [
                ChatMessage(
                    id="message-b",
                    engagement_id="project",
                    session_id="session-a",
                    sequence=2,
                    role=ChatRole.ASSISTANT,
                    content="second",
                    created_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                ),
                ChatMessage(
                    id="message-a",
                    engagement_id="project",
                    session_id="session-a",
                    sequence=1,
                    role=ChatRole.USER,
                    content="first",
                    created_at=now,
                    updated_at=now,
                ),
                ChatMessage(
                    id="message-replaced",
                    engagement_id="project",
                    session_id="session-a",
                    sequence=3,
                    role=ChatRole.ASSISTANT,
                    content="old",
                    metadata={"retracted_at": now.isoformat()},
                    created_at=now + timedelta(seconds=2),
                    updated_at=now + timedelta(seconds=2),
                ),
                ChatMessage(
                    id="other-message",
                    engagement_id="project",
                    session_id="session-b",
                    sequence=1,
                    role=ChatRole.USER,
                    content="other",
                ),
            ]
        )
    )
    decision = await store.create(
        ChatDecision(
            id="decision",
            engagement_id="project",
            session_id="session-a",
            text="Keep the exact session projection current.",
        )
    )
    approval = await store.create(
        Approval(
            id="approval",
            engagement_id="project",
            run_id="turn",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="session-a",
            risk_class=RiskClass.LOCAL_READ,
            exact_request={},
            policy_rationale="Focused test",
            requested_by="operator",
        )
    )

    async with store.database.session() as database:
        assert (
            await database.get(EntityRow, "message-a")
        ).chat_session_id == "session-a"
        assert (
            await database.get(EntityRow, approval.id)
        ).chat_session_id == "session-a"
    moved = await store.update(
        ChatDecision,
        decision.id,
        {"session_id": "session-b"},
        expected_revision=decision.revision,
    )
    async with store.database.session() as database:
        assert (await database.get(EntityRow, moved.id)).chat_session_id == "session-b"

    service = ChatService(store)
    assert [
        message.id for message in (await service.session_messages("session-a"))
    ] == [
        "message-a",
        "message-b",
    ]
    assert [
        message.id
        for message in (
            await service.session_messages("session-a", include_replaced=True)
        )
    ] == ["message-a", "message-b", "message-replaced"]

    async with store.database.engine.connect() as connection:
        plan = (
            await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM entities "
                "WHERE kind = ? AND chat_session_id = ? ORDER BY created_at, id",
                ("chat_messages", "session-a"),
            )
        ).all()
    assert any("ix_entities_kind_chat_session_created" in str(row) for row in plan), (
        plan
    )


async def test_blocking_history_read_does_not_block_chat_state(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = ChatService.session_messages

    async def blocked(self, session_id, *, include_replaced=False):
        if session_id == "session-a":
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        return await original(self, session_id, include_replaced=include_replaced)

    monkeypatch.setattr(ChatService, "session_messages", blocked)
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        slow = asyncio.create_task(
            client.get("/api/v1/chat/sessions/session-a/messages", headers=AUTH)
        )
        await asyncio.wait_for(entered.wait(), 1)
        try:
            state = await asyncio.wait_for(
                client.get("/api/v1/chat/sessions/session-a/state", headers=AUTH), 0.75
            )
            assert state.status_code == 200, state.text
        finally:
            release.set()
        assert (await asyncio.wait_for(slow, 2)).status_code == 200


async def test_optional_naming_database_read_does_not_block_event_loop(
    tmp_path, monkeypatch
):
    store = await _store(tmp_path)
    (
        await store.create(
            ChatMessage(
                id="naming-prompt",
                engagement_id="project",
                session_id="session-a",
                sequence=1,
                role=ChatRole.USER,
                content="Investigate the conversation switch latency.",
            )
        )
    )
    service = ChatService(store)
    original = service.session_messages

    async def slow_messages(session_id):
        await asyncio.sleep(0.4)
        return await original(session_id)

    monkeypatch.setattr(service, "session_messages", slow_messages)

    class NamingProvider:
        async def complete(self, request):
            assert request.metadata["operation"] == "conversation_naming"
            return SimpleNamespace(text="Responsive conversation title")

    async def scenario():
        prepared = SimpleNamespace(
            session=(await store.get(ChatSession, "session-a")),
            pending_session=None,
            engagement_id="project",
            resolved_model="model-a",
            provider=NamingProvider(),
        )
        task = asyncio.create_task(service._name_initial_session(prepared, "Answer"))
        started = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - started < 0.25
        await task
        named = await store.get(ChatSession, "session-a")
        assert named.title == "Responsive conversation title"
        assert named.metadata["initial_title_state"] == "generated"

    (await scenario())


async def test_large_chat_json_is_compressed_and_timed_but_small_and_sse_are_not(
    tmp_path,
):
    store = await _store(tmp_path)
    (
        await store.create(
            ChatMessage(
                id="large-message",
                engagement_id="project",
                session_id="session-a",
                sequence=1,
                role=ChatRole.ASSISTANT,
                content="repeatable transcript line " * 6_000,
            )
        )
    )
    app = create_app(store, auth_token="test-token")

    async def test_stream():
        async def events():
            yield b"data: " + (b"x" * 4_000) + b"\n\n"

        return StreamingResponse((await events()), media_type="text/event-stream")

    app.add_api_route("/api/v1/chat/test-stream", test_stream, methods=["GET"])

    def encoded_response():
        return Response(
            content=zlib.compress(b"already encoded " * 1_000),
            media_type="application/octet-stream",
            headers={"Content-Encoding": "deflate"},
        )

    app.add_api_route("/api/v1/chat/test-encoded", encoded_response, methods=["GET"])
    async with TestClient(app) as client:
        identity = await client.get(
            "/api/v1/chat/sessions/session-a/messages",
            headers={**AUTH, "Accept-Encoding": "identity"},
        )
        compressed = await client.get(
            "/api/v1/chat/sessions/session-a/messages",
            headers={**AUTH, "Accept-Encoding": "gzip"},
        )
        assert identity.status_code == compressed.status_code == 200
        assert compressed.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in compressed.headers["vary"]
        assert compressed.num_bytes_downloaded < len(identity.content) * 0.45
        assert compressed.headers["server-timing"].startswith("app;dur=")
        assert "session-a" not in compressed.headers["server-timing"]

        small = await client.get(
            "/api/v1/chat/sessions/session-a/pending-turn",
            headers={**AUTH, "Accept-Encoding": "gzip"},
        )
        assert small.status_code == 200
        assert "content-encoding" not in small.headers
        assert small.headers["server-timing"].startswith("app;dur=")

        stream = await client.get(
            "/api/v1/chat/test-stream", headers={"Accept-Encoding": "gzip"}
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in stream.headers
        assert "server-timing" not in stream.headers

        encoded = await client.get(
            "/api/v1/chat/test-encoded", headers={"Accept-Encoding": "gzip"}
        )
        assert encoded.status_code == 200
        assert encoded.headers["content-encoding"] == "deflate"
        assert encoded.content == b"already encoded " * 1_000
        assert encoded.headers["server-timing"].startswith("app;dur=")


pytestmark = pytest.mark.anyio
