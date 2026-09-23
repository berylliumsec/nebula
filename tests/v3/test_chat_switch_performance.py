from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import zlib

from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient

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


def _store(tmp_path) -> NebulaStore:
    store = NebulaStore(tmp_path / "chat-switch.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    for session_id in ("session-a", "session-b"):
        store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title=session_id,
                provider_profile_id="provider",
                model="model-a",
            )
        )
    return store


def test_chat_lookup_projection_tracks_writes_orders_history_and_uses_index(tmp_path):
    store = _store(tmp_path)
    now = utc_now()
    store.create_many(
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
    decision = store.create(
        ChatDecision(
            id="decision",
            engagement_id="project",
            session_id="session-a",
            text="Keep the exact session projection current.",
        )
    )
    approval = store.create(
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

    with store.database.session() as database:
        assert database.get(EntityRow, "message-a").chat_session_id == "session-a"
        assert database.get(EntityRow, approval.id).chat_session_id == "session-a"
    moved = store.update(
        ChatDecision,
        decision.id,
        {"session_id": "session-b"},
        expected_revision=decision.revision,
    )
    with store.database.session() as database:
        assert database.get(EntityRow, moved.id).chat_session_id == "session-b"

    service = ChatService(store)
    assert [message.id for message in service.session_messages("session-a")] == [
        "message-a",
        "message-b",
    ]
    assert [
        message.id
        for message in service.session_messages("session-a", include_replaced=True)
    ] == ["message-a", "message-b", "message-replaced"]

    with store.database.engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM entities "
            "WHERE kind = ? AND chat_session_id = ? ORDER BY created_at, id",
            ("chat_messages", "session-a"),
        ).all()
    assert any("ix_entities_kind_chat_session_created" in str(row) for row in plan), (
        plan
    )


def test_blocking_history_read_does_not_block_chat_state(tmp_path, monkeypatch):
    store = _store(tmp_path)
    entered = Event()
    release = Event()
    original = ChatService.session_messages

    def blocked(self, session_id, *, include_replaced=False):
        if session_id == "session-a":
            entered.set()
            assert release.wait(2), "history test gate was not released"
        return original(self, session_id, include_replaced=include_replaced)

    monkeypatch.setattr(ChatService, "session_messages", blocked)
    with TestClient(create_app(store, auth_token="test-token")) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(
                client.get,
                "/api/v1/chat/sessions/session-a/messages",
                headers=AUTH,
            )
            assert entered.wait(1)
            fast = executor.submit(
                client.get,
                "/api/v1/chat/sessions/session-a/state",
                headers=AUTH,
            )
            try:
                state = fast.result(timeout=0.75)
                assert state.status_code == 200, state.text
            finally:
                release.set()
            assert slow.result(timeout=2).status_code == 200


def test_large_chat_json_is_compressed_and_timed_but_small_and_sse_are_not(tmp_path):
    store = _store(tmp_path)
    store.create(
        ChatMessage(
            id="large-message",
            engagement_id="project",
            session_id="session-a",
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="repeatable transcript line " * 6_000,
        )
    )
    app = create_app(store, auth_token="test-token")

    async def test_stream():
        async def events():
            yield b"data: " + (b"x" * 4_000) + b"\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_api_route("/api/v1/chat/test-stream", test_stream, methods=["GET"])

    def encoded_response():
        return Response(
            content=zlib.compress(b"already encoded " * 1_000),
            media_type="application/octet-stream",
            headers={"Content-Encoding": "deflate"},
        )

    app.add_api_route("/api/v1/chat/test-encoded", encoded_response, methods=["GET"])
    with TestClient(app) as client:
        identity = client.get(
            "/api/v1/chat/sessions/session-a/messages",
            headers={**AUTH, "Accept-Encoding": "identity"},
        )
        compressed = client.get(
            "/api/v1/chat/sessions/session-a/messages",
            headers={**AUTH, "Accept-Encoding": "gzip"},
        )
        assert identity.status_code == compressed.status_code == 200
        assert compressed.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in compressed.headers["vary"]
        assert compressed.num_bytes_downloaded < len(identity.content) * 0.45
        assert compressed.headers["server-timing"].startswith("app;dur=")
        assert "session-a" not in compressed.headers["server-timing"]

        small = client.get(
            "/api/v1/chat/sessions/session-a/pending-turn",
            headers={**AUTH, "Accept-Encoding": "gzip"},
        )
        assert small.status_code == 200
        assert "content-encoding" not in small.headers
        assert small.headers["server-timing"].startswith("app;dur=")

        stream = client.get(
            "/api/v1/chat/test-stream", headers={"Accept-Encoding": "gzip"}
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in stream.headers
        assert "server-timing" not in stream.headers

        encoded = client.get(
            "/api/v1/chat/test-encoded", headers={"Accept-Encoding": "gzip"}
        )
        assert encoded.status_code == 200
        assert encoded.headers["content-encoding"] == "deflate"
        assert encoded.content == b"already encoded " * 1_000
        assert encoded.headers["server-timing"].startswith("app;dur=")
