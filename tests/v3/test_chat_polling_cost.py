"""What the chat workspace's polled reads cost when nothing changed.

Open conversations poll session state, catch-up, pending-turn, activity, the
follow-up queue and published results every few seconds. These tests pin the
contract that an unchanged conversation is answered from small, indexed reads
(no whole turn payloads), and that a client holding the current ``ETag`` gets
``304 Not Modified`` without a body.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nebula.v3 import chat_catchup, chat_turn_headers
from nebula.v3.api import create_app
from nebula.v3.chat_catchup import catchup_projection
from nebula.v3.chat_queue import (
    RETAINED_SETTLED_ITEMS,
    QueueWrite,
    queue_is_dormant,
    queue_router,
)
from nebula.v3.chat_turn_headers import session_turn_headers
from nebula.v3.domain import (
    ChatQueue,
    ChatReadCursor,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.session_state import session_state
from nebula.v3.storage import NebulaStore
from nebula.v3.structured_results import structured_results_router
from tests.v3.test_chat_queue import enqueue, setup_queue


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _conversation(tmp_path, *, turns: int = 3):
    store = NebulaStore(tmp_path / "polling.db")
    engagement = store.create(Engagement(name="Polling cost"))
    profile = store.create(
        ProviderProfile(name="Local provider", provider_type="vllm", is_local=True)
    )
    session = store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Long conversation",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    saved = [
        store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
                # Real finished turns carry large tool histories.
                tool_history=[{"name": "run_command", "output": "x" * 50_000}],
            )
        )
        for _ in range(turns)
    ]
    return store, engagement, profile, session, saved


@pytest.fixture
def parsed_turns(monkeypatch):
    """Ids of turn payloads read to build headers, in order."""

    parsed: list[str] = []
    real = chat_turn_headers._header

    def counting(payload, revision):
        parsed.append(payload["id"])
        return real(payload, revision)

    monkeypatch.setattr(chat_turn_headers, "_header", counting)
    return parsed


def test_headers_reread_only_the_turn_that_changed(tmp_path, parsed_turns):
    store, _, _, session, saved = _conversation(tmp_path)
    with store.database.session() as database:
        first = session_turn_headers(database, session.id)
    assert sorted(parsed_turns) == sorted(turn.id for turn in saved)
    assert [item.id for item in first] == [turn.id for turn in reversed(saved)]

    parsed_turns.clear()
    store.update(
        ChatTurn,
        saved[1].id,
        {"status": ChatTurnStatus.FAILED, "error": "Provider timed out"},
        expected_revision=saved[1].revision,
    )
    with store.database.session() as database:
        second = session_turn_headers(database, session.id)
    assert parsed_turns == [saved[1].id]
    changed = next(item for item in second if item.id == saved[1].id)
    assert (changed.status, changed.error, changed.revision) == (
        "failed",
        "Provider timed out",
        saved[1].revision + 1,
    )


def test_header_cache_never_crosses_databases(tmp_path):
    # Fixed ids and timestamps in two stores must not share a memoized header.
    moment = utc_now() - timedelta(hours=1)
    statuses = {}
    for name, status in (
        ("one", ChatTurnStatus.COMPLETE),
        ("two", ChatTurnStatus.FAILED),
    ):
        store = NebulaStore(tmp_path / f"{name}.db")
        store.create(Engagement(id="p", name=name))
        store.create(
            ChatSession(
                id="s",
                engagement_id="p",
                provider_profile_id="x",
                model="m",
                title=name,
            )
        )
        store.create(
            ChatTurn(
                id="t",
                engagement_id="p",
                session_id="s",
                provider_profile_id="x",
                model="m",
                status=status,
                created_at=moment,
                updated_at=moment,
            )
        )
        with store.database.session() as database:
            statuses[name] = session_turn_headers(database, "s")[0].status
    assert statuses == {"one": "complete", "two": "failed"}


def test_unchanged_state_poll_parses_no_turn_payload(tmp_path, parsed_turns):
    store, engagement, profile, session, saved = _conversation(tmp_path)
    first = session_state(store, session)
    assert first["execution"] == "complete"

    parsed_turns.clear()
    assert session_state(store, session) == first
    assert parsed_turns == []

    running = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    after = session_state(store, session)
    assert parsed_turns == [running.id]
    assert (after["execution"], after["busy"], after["turn_id"]) == (
        "running",
        True,
        running.id,
    )
    assert after["revision"] > first["revision"]


def test_state_route_answers_not_modified_until_the_projection_changes(tmp_path):
    store, engagement, profile, session, _ = _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/state"

    first = client.get(path, headers=_auth())
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-store"

    unchanged = client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag

    store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    changed = client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.json()["busy"] is True
    assert changed.headers["etag"] != etag


def test_catch_up_reads_headers_and_links_only_failures(
    tmp_path, parsed_turns, monkeypatch
):
    store, engagement, _, session, saved = _conversation(tmp_path, turns=4)
    cursor = ChatReadCursor(
        id="cursor",
        engagement_id=engagement.id,
        session_id=session.id,
        device_id="device",
        through_at=utc_now() - timedelta(minutes=5),
    )
    store.update(
        ChatTurn,
        saved[2].id,
        {"status": ChatTurnStatus.FAILED, "error": "Provider timed out"},
        expected_revision=saved[2].revision,
    )
    linked: list[str] = []
    real = chat_catchup.source_message

    def counting(database, owner, turn):
        linked.append(turn.id)
        return real(database, owner, turn)

    monkeypatch.setattr(chat_catchup, "source_message", counting)

    first = catchup_projection(store, session, cursor)
    failures = [item for item in first["items"] if item["kind"] == "failure"]
    assert [item["turn_id"] for item in failures] == [saved[2].id]
    assert failures[0]["text"] == "Response failed: Provider timed out"
    # Only the reported failure is linked to its prompt; finished turns are not.
    assert linked == [saved[2].id]

    parsed_turns.clear()
    assert catchup_projection(store, session, cursor)["items"] == first["items"]
    assert parsed_turns == []


def test_waiting_polls_get_a_compact_pending_turn(tmp_path, parsed_turns):
    store, engagement, profile, session, _ = _conversation(tmp_path)
    waiting = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.WAITING_CALLBACK,
            content="Partial answer " * 2_000,
            reasoning="Saved thinking " * 2_000,
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/pending-turn"

    full = client.get(path, headers=_auth())
    assert full.status_code == 200
    assert full.json()["content"].startswith("Partial answer")

    parsed_turns.clear()
    compact = client.get(path, params={"view": "status"}, headers=_auth())
    assert compact.status_code == 200
    assert compact.json() == {
        "id": waiting.id,
        "session_id": session.id,
        "status": "waiting_callback",
        "revision": waiting.revision,
    }
    # Finding the blocking turn reads no finished turn's payload.
    assert parsed_turns == []

    store.update(
        ChatTurn,
        waiting.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=waiting.revision,
    )
    assert client.get(path, params={"view": "status"}, headers=_auth()).json() is None


def test_activity_route_answers_not_modified_and_reads_no_payloads(
    tmp_path, parsed_turns
):
    store, engagement, profile, session, _ = _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    params = {"engagement_id": engagement.id}

    first = client.get("/api/v1/chat/session-activity", params=params, headers=_auth())
    assert first.status_code == 200
    assert first.json() == [
        {"session_id": session.id, "state": "idle", "turn_id": None}
    ]
    etag = first.headers["etag"]

    parsed_turns.clear()
    unchanged = client.get(
        "/api/v1/chat/session-activity",
        params=params,
        headers={**_auth(), "If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert parsed_turns == []

    working = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    changed = client.get(
        "/api/v1/chat/session-activity",
        params=params,
        headers={**_auth(), "If-None-Match": etag},
    )
    assert changed.status_code == 200
    assert changed.json() == [
        {"session_id": session.id, "state": "working", "turn_id": working.id}
    ]


def test_conversation_results_are_filtered_in_sql_and_answer_not_modified(
    tmp_path, monkeypatch
):
    from nebula.v3 import structured_results

    store = NebulaStore(tmp_path / "results.db")
    store.create(Engagement(id="project", name="Project"))
    app = FastAPI()
    app.include_router(structured_results_router(store))
    client = TestClient(app)
    for index, session_id in enumerate(["chat-a", "chat-b", "chat-a", None]):
        body = {"title": f"Result {index}", "result": {"n": index}}
        if session_id:
            body["chat_session_id"] = session_id
        assert (
            client.post("/projects/project/structured-results", json=body).status_code
            == 201
        )

    def whole_project(*_args, **_kwargs):
        raise AssertionError("a conversation's list must not read every result")

    monkeypatch.setattr(structured_results, "_all_results", whole_project)
    path = "/projects/project/structured-results"
    listed = client.get(path, params={"chat_session_id": "chat-a", "limit": 50})
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()] == ["Result 2", "Result 0"]

    unchanged = client.get(
        path,
        params={"chat_session_id": "chat-a", "limit": 50},
        headers={"If-None-Match": listed.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""

    everything = client.get(path, params={"limit": 2, "offset": 1})
    assert [item["title"] for item in everything.json()] == ["Result 2", "Result 1"]


def _settle(store, service, request, count):
    """Enqueue ``count`` follow-ups and let Core settle each one as complete."""

    queue = service.get("s")
    for index in range(count):
        queue = enqueue(
            service, request, queue.revision if index else 0, f"key-{index}"
        )
        items = [dict(item) for item in queue.items]
        items[-1].update(status="sending", turn_id=f"turn-{index}")
        queue = store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        store.create(
            ChatTurn(
                id=f"turn-{index}",
                engagement_id="p",
                session_id="s",
                provider_profile_id="provider-a",
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
            )
        )
        asyncio.run(service.step(queue))
        queue = service.get("s")
    return queue


def test_settled_follow_ups_are_bounded_and_drop_request_bodies(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = _settle(store, service, request, RETAINED_SETTLED_ITEMS + 5)

    assert [item["status"] for item in queue.items] == ["complete"] * (
        RETAINED_SETTLED_ITEMS
    )
    # The oldest settled follow-ups are gone; the retained ones keep their turn.
    assert queue.items[0]["key"] == "key-5"
    assert queue.items[-1]["turn_id"] == f"turn-{RETAINED_SETTLED_ITEMS + 4}"
    assert all(
        "request" not in item and "original_request" not in item for item in queue.items
    )
    assert all(item["request_digest"] for item in queue.items)

    # A replayed enqueue of a retained key is still recognized, not re-added.
    replay = enqueue(service, request, queue.revision, "key-10")
    assert replay.revision == queue.revision
    with pytest.raises(Exception, match="already used for different content"):
        enqueue(
            service,
            request.model_copy(update={"include_knowledge": True}),
            queue.revision,
            "key-10",
        )


def test_worker_skips_a_dormant_queue_until_it_changes(tmp_path, monkeypatch):
    store, service, request = setup_queue(tmp_path)
    queue = _settle(store, service, request, 2)
    assert queue_is_dormant(queue)

    loaded: list[str] = []
    real = ChatQueue.model_validate

    def counting(value, *args, **kwargs):
        loaded.append(value.get("id") if isinstance(value, dict) else "?")
        return real(value, *args, **kwargs)

    monkeypatch.setattr(ChatQueue, "model_validate", counting)
    assert [item.id for item in service.queues(skip_dormant=True)] == [queue.id]
    service._remember_dormancy(queue)
    loaded.clear()
    assert service.queues(skip_dormant=True) == []
    assert loaded == []
    # Recovery at startup still reads every queue.
    assert [item.id for item in service.queues()] == [queue.id]

    enqueue(service, request, queue.revision, "after-dormancy")
    assert [item.id for item in service.queues(skip_dormant=True)] == [queue.id]


def test_queue_route_answers_not_modified(tmp_path):
    store, service, request = setup_queue(tmp_path)
    app = FastAPI()
    app.include_router(queue_router(service))
    client = TestClient(app)

    first = client.get("/chat/sessions/s/queue")
    assert first.status_code == 200 and first.json()["revision"] == 0
    etag = first.headers["etag"]
    assert (
        client.get(
            "/chat/sessions/s/queue", headers={"If-None-Match": etag}
        ).status_code
        == 304
    )

    enqueue(service, request, 0, "first", paused=True)
    changed = client.get("/chat/sessions/s/queue", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert [item["key"] for item in changed.json()["items"]] == ["first"]


def test_live_queue_is_not_dormant(tmp_path):
    store, service, request = setup_queue(tmp_path)
    queue = enqueue(service, request)
    assert not queue_is_dormant(queue)
    paused = service.write(
        "s", QueueWrite(action="pause", expected_revision=queue.revision)
    )
    assert queue_is_dormant(paused)


def test_cross_origin_clients_may_send_and_read_validators(tmp_path):
    # The desktop app talks to Core from another origin; the browser must let
    # it send If-None-Match and read the ETag it gets back.
    store, _, _, session, _ = _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/state"
    preflight = client.options(
        path,
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,if-none-match",
        },
    )
    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].lower()

    read = client.get(path, headers={**_auth(), "Origin": "tauri://localhost"})
    assert read.status_code == 200
    exposed = read.headers["access-control-expose-headers"].lower()
    assert "etag" in exposed.split(", ")
