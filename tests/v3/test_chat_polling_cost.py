"""What the chat workspace's polled reads cost when nothing changed.

Open conversations poll session state, catch-up, pending-turn, activity, the
follow-up queue and published results every few seconds. These tests pin the
contract that an unchanged conversation is answered from small, indexed reads
(no whole turn payloads), and that a client holding the current ``ETag`` gets
``304 Not Modified`` without a body.
"""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import contextlib
from datetime import timedelta

import pytest
from fastapi import FastAPI
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3 import chat_catchup, chat_queue, chat_turn_headers
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


async def _conversation(tmp_path, *, turns: int = 3):
    store = NebulaStore(tmp_path / "polling.db")
    engagement = await store.create(Engagement(name="Polling cost"))
    profile = await store.create(
        ProviderProfile(name="Local provider", provider_type="vllm", is_local=True)
    )
    session = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Long conversation",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    saved = [
        (
            await store.create(
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


async def test_headers_reread_only_the_turn_that_changed(tmp_path, parsed_turns):
    store, _, _, session, saved = await _conversation(tmp_path)
    async with store.database.session() as database:
        first = await session_turn_headers(database, session.id)
    assert sorted(parsed_turns) == sorted(turn.id for turn in saved)
    assert [item.id for item in first] == [turn.id for turn in reversed(saved)]

    parsed_turns.clear()
    (
        await store.update(
            ChatTurn,
            saved[1].id,
            {"status": ChatTurnStatus.FAILED, "error": "Provider timed out"},
            expected_revision=saved[1].revision,
        )
    )
    async with store.database.session() as database:
        second = await session_turn_headers(database, session.id)
    assert parsed_turns == [saved[1].id]
    changed = next(item for item in second if item.id == saved[1].id)
    assert (changed.status, changed.error, changed.revision) == (
        "failed",
        "Provider timed out",
        saved[1].revision + 1,
    )


async def test_header_cache_never_crosses_databases(tmp_path):
    # Fixed ids and timestamps in two stores must not share a memoized header.
    moment = utc_now() - timedelta(hours=1)
    statuses = {}
    for name, status in (
        ("one", ChatTurnStatus.COMPLETE),
        ("two", ChatTurnStatus.FAILED),
    ):
        store = NebulaStore(tmp_path / f"{name}.db")
        (await store.create(Engagement(id="p", name=name)))
        (
            await store.create(
                ChatSession(
                    id="s",
                    engagement_id="p",
                    provider_profile_id="x",
                    model="m",
                    title=name,
                )
            )
        )
        (
            await store.create(
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
        )
        async with store.database.session() as database:
            statuses[name] = (await session_turn_headers(database, "s"))[0].status
    assert statuses == {"one": "complete", "two": "failed"}


async def test_unchanged_state_poll_parses_no_turn_payload(tmp_path, parsed_turns):
    store, engagement, profile, session, saved = await _conversation(tmp_path)
    first = await session_state(store, session)
    assert first["execution"] == "complete"

    parsed_turns.clear()
    assert (await session_state(store, session)) == first
    assert parsed_turns == []

    running = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    after = await session_state(store, session)
    assert parsed_turns == [running.id]
    assert (after["execution"], after["busy"], after["turn_id"]) == (
        "running",
        True,
        running.id,
    )
    assert after["revision"] > first["revision"]


async def test_state_route_answers_not_modified_until_the_projection_changes(tmp_path):
    store, engagement, profile, session, _ = await _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/state"

    first = await client.get(path, headers=_auth())
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-store"

    unchanged = await client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag

    (
        await store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
    )
    changed = await client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.json()["busy"] is True
    assert changed.headers["etag"] != etag


async def test_subagents_route_answers_not_modified_until_a_child_changes(tmp_path):
    from nebula.v3.domain import ChatSubagent, ChatSubagentStatus

    store, engagement, profile, parent, _ = await _conversation(tmp_path, turns=1)

    async def delegate(name: str, **fields) -> ChatSubagent:
        child = await store.create(
            ChatSession(
                engagement_id=engagement.id,
                title=name,
                provider_profile_id=profile.id,
                model="model-a",
                parent_session_id=parent.id,
            )
        )
        return await store.create(
            ChatSubagent(
                engagement_id=engagement.id,
                parent_session_id=parent.id,
                parent_turn_id="turn",
                child_session_id=child.id,
                name=name,
                task=f"{name}.",
                **fields,
            )
        )

    finished_at = utc_now()
    done = await delegate(
        "Map routes",
        status=ChatSubagentStatus.COMPLETED,
        started_at=finished_at - timedelta(seconds=40),
        finished_at=finished_at,
        result="Three routes.",
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{parent.id}/subagents"

    first = await client.get(path, headers=_auth())
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    etag = first.headers["etag"]
    assert [item["name"] for item in first.json()["subagents"]] == ["Map routes"]

    # The rail polls while a response runs: an unchanged list costs no body.
    unchanged = await client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag

    # A new child, and a finished child's changed record, each answer anew.
    (await delegate("Check headers"))
    started = await client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert started.status_code == 200
    assert [item["name"] for item in started.json()["subagents"]] == [
        "Map routes",
        "Check headers",
    ]
    assert started.headers["etag"] != etag
    etag = started.headers["etag"]

    (
        await store.update(
            ChatSubagent,
            done.id,
            {"result": "Four routes."},
            expected_revision=done.revision,
        )
    )
    revised = await client.get(path, headers={**_auth(), "If-None-Match": etag})
    assert revised.status_code == 200
    assert revised.json()["subagents"][0]["result"] == "Four routes."

    # Another conversation's validator never matches this one.
    other = await client.get(
        f"/api/v1/chat/sessions/{done.child_session_id}/subagents",
        headers={**_auth(), "If-None-Match": revised.headers["etag"]},
    )
    assert other.status_code == 200
    assert other.json()["subagents"] == []


async def test_catch_up_reads_headers_and_links_only_failures(
    tmp_path, parsed_turns, monkeypatch
):
    store, engagement, _, session, saved = await _conversation(tmp_path, turns=4)
    cursor = ChatReadCursor(
        id="cursor",
        engagement_id=engagement.id,
        session_id=session.id,
        device_id="device",
        through_at=utc_now() - timedelta(minutes=5),
    )
    (
        await store.update(
            ChatTurn,
            saved[2].id,
            {"status": ChatTurnStatus.FAILED, "error": "Provider timed out"},
            expected_revision=saved[2].revision,
        )
    )
    linked: list[str] = []
    real = chat_catchup.source_message

    async def counting(database, owner, turn):
        linked.append(turn.id)
        return await real(database, owner, turn)

    monkeypatch.setattr(chat_catchup, "source_message", counting)

    first = await catchup_projection(store, session, cursor)
    failures = [item for item in first["items"] if item["kind"] == "failure"]
    assert [item["turn_id"] for item in failures] == [saved[2].id]
    assert failures[0]["text"] == "Response failed: Provider timed out"
    # Only the reported failure is linked to its prompt; finished turns are not.
    assert linked == [saved[2].id]

    parsed_turns.clear()
    assert (await catchup_projection(store, session, cursor))["items"] == first["items"]
    assert parsed_turns == []


async def test_waiting_polls_get_a_compact_pending_turn(tmp_path, parsed_turns):
    store, engagement, profile, session, _ = await _conversation(tmp_path)
    waiting = await store.create(
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

    full = await client.get(path, headers=_auth())
    assert full.status_code == 200
    assert full.json()["content"].startswith("Partial answer")

    parsed_turns.clear()
    compact = await client.get(path, params={"view": "status"}, headers=_auth())
    assert compact.status_code == 200
    assert compact.json() == {
        "id": waiting.id,
        "session_id": session.id,
        "status": "waiting_callback",
        "revision": waiting.revision,
    }
    # Finding the blocking turn reads no finished turn's payload.
    assert parsed_turns == []

    (
        await store.update(
            ChatTurn,
            waiting.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=waiting.revision,
        )
    )
    assert (
        await client.get(path, params={"view": "status"}, headers=_auth())
    ).json() is None


async def test_activity_route_answers_not_modified_and_reads_no_payloads(
    tmp_path, parsed_turns
):
    store, engagement, profile, session, _ = await _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    params = {"engagement_id": engagement.id}

    first = await client.get(
        "/api/v1/chat/session-activity", params=params, headers=_auth()
    )
    assert first.status_code == 200
    assert first.json() == [
        {"session_id": session.id, "state": "idle", "turn_id": None}
    ]
    etag = first.headers["etag"]

    parsed_turns.clear()
    unchanged = await client.get(
        "/api/v1/chat/session-activity",
        params=params,
        headers={**_auth(), "If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert parsed_turns == []

    working = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    changed = await client.get(
        "/api/v1/chat/session-activity",
        params=params,
        headers={**_auth(), "If-None-Match": etag},
    )
    assert changed.status_code == 200
    assert changed.json() == [
        {"session_id": session.id, "state": "working", "turn_id": working.id}
    ]


async def test_conversation_results_are_filtered_in_sql_and_answer_not_modified(
    tmp_path, monkeypatch
):
    from nebula.v3 import structured_results

    store = NebulaStore(tmp_path / "results.db")
    (await store.create(Engagement(id="project", name="Project")))
    app = FastAPI()
    app.include_router(structured_results_router(store))
    client = TestClient(app)
    for index, session_id in enumerate(["chat-a", "chat-b", "chat-a", None]):
        body = {"title": f"Result {index}", "result": {"n": index}}
        if session_id:
            body["chat_session_id"] = session_id
        assert (
            await client.post("/projects/project/structured-results", json=body)
        ).status_code == 201

    def whole_project(*_args, **_kwargs):
        raise AssertionError("a conversation's list must not read every result")

    monkeypatch.setattr(structured_results, "_all_results", whole_project)
    path = "/projects/project/structured-results"
    listed = await client.get(path, params={"chat_session_id": "chat-a", "limit": 50})
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()] == ["Result 2", "Result 0"]

    unchanged = await client.get(
        path,
        params={"chat_session_id": "chat-a", "limit": 50},
        headers={"If-None-Match": listed.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""

    everything = await client.get(path, params={"limit": 2, "offset": 1})
    assert [item["title"] for item in everything.json()] == ["Result 2", "Result 1"]


async def _settle(store, service, request, count):
    """Enqueue ``count`` follow-ups and let Core settle each one as complete."""

    queue = await service.get("s")
    for index in range(count):
        queue = await enqueue(
            service, request, queue.revision if index else 0, f"key-{index}"
        )
        items = [dict(item) for item in queue.items]
        items[-1].update(status="sending", turn_id=f"turn-{index}")
        queue = await store.update(
            ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
        )
        (
            await store.create(
                ChatTurn(
                    id=f"turn-{index}",
                    engagement_id="p",
                    session_id="s",
                    provider_profile_id="provider-a",
                    model="model-a",
                    status=ChatTurnStatus.COMPLETE,
                )
            )
        )
        (await service.step(queue))
        queue = await service.get("s")
    return queue


async def test_settled_follow_ups_are_bounded_and_drop_request_bodies(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await _settle(store, service, request, RETAINED_SETTLED_ITEMS + 5)

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
    replay = await enqueue(service, request, queue.revision, "key-10")
    assert replay.revision == queue.revision
    with pytest.raises(Exception, match="already used for different content"):
        await enqueue(
            service,
            request.model_copy(update={"include_knowledge": True}),
            queue.revision,
            "key-10",
        )


async def test_worker_skips_a_dormant_queue_until_it_changes(tmp_path, monkeypatch):
    store, service, request = await setup_queue(tmp_path)
    queue = await _settle(store, service, request, 2)
    assert queue_is_dormant(queue)

    loaded: list[str] = []
    real = ChatQueue.model_validate

    def counting(value, *args, **kwargs):
        loaded.append(value.get("id") if isinstance(value, dict) else "?")
        return real(value, *args, **kwargs)

    monkeypatch.setattr(ChatQueue, "model_validate", counting)
    assert [item.id for item in (await service.queues(skip_dormant=True))] == [queue.id]
    service._remember_dormancy(queue)
    loaded.clear()
    assert (await service.queues(skip_dormant=True)) == []
    assert loaded == []
    # Recovery at startup still reads every queue.
    assert [item.id for item in (await service.queues())] == [queue.id]

    await enqueue(service, request, queue.revision, "after-dormancy")
    assert [item.id for item in (await service.queues(skip_dormant=True))] == [queue.id]


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def _value(value):
    return value


def _count_passes(service):
    passes = []
    real = service.queues

    async def counting(*, skip_dormant=False):
        passes.append(skip_dormant)
        return await real(skip_dormant=skip_dormant)

    service.queues = counting
    return passes


async def _stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _hold_dispatched_turns(service):
    """Leave each dispatched turn queued so the test settles it explicitly."""

    async def hold(prepared, **_):
        return prepared.turn.id

    service.chat.start_provider_turn = hold


async def _dispatched_turn(service, *, other_than=None):
    item = (await service.get("s")).items[0]
    if item["status"] == "sending" and item.get("turn_id") != other_than:
        return item["turn_id"]
    return None


async def _finish(store, turn_id):
    turn = await store.get(ChatTurn, turn_id)
    await store.update(
        ChatTurn,
        turn_id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )


async def test_units_of_work_signal_queue_writes_and_turn_activity(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    signals = []
    store.on_follow_up_change(
        lambda written, sessions: signals.append((written, sessions))
    )

    queue = await enqueue(service, request, paused=True)
    assert signals == [(True, frozenset())]
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    assert signals[1:] == [(False, frozenset({"s"}))] * 2
    # A turn write that keeps its status, or another record, signals nothing.
    signals.clear()
    current = await store.get(ChatTurn, turn.id)
    await store.update(
        ChatTurn, turn.id, {"error": "note"}, expected_revision=current.revision
    )
    project = await store.get(Engagement, "p")
    await store.update(
        Engagement, "p", {"description": "x"}, expected_revision=project.revision
    )
    assert signals == []
    # A unit that ends without committing still wakes the dispatcher, which
    # then reads an unchanged queue; a missed wake would cost a safety pass.
    with pytest.raises(RuntimeError):
        async with store.transaction() as transaction:
            await transaction.update(
                ChatQueue, queue.id, {"paused": False}, expected_revision=queue.revision
            )
            raise RuntimeError("abandon this unit of work")
    assert signals == [(True, frozenset())]


async def test_dispatcher_survives_a_failed_queue_read(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    _hold_dispatched_turns(service)
    queue = await enqueue(service, request, paused=True)
    real = service.queues
    failures = []

    async def flaky(*, skip_dormant=False):
        if len(failures) < 2:
            failures.append(1)
            raise RuntimeError("database unavailable")
        return await real(skip_dormant=skip_dormant)

    service.queues = flaky
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(len(failures) == 2))
        await service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await _until(lambda: _dispatched_turn(service))
    finally:
        await _stop(task)
        await service.chat.shutdown()


async def test_idle_dispatcher_sleeps_until_a_write_then_dispatches_at_once(
    tmp_path, monkeypatch
):
    # No poll interval elapses during this test: only wakes make progress.
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 60.0)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    _hold_dispatched_turns(service)
    queue = await enqueue(service, request, paused=True)
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(len(passes) == 1))
        await asyncio.sleep(0.3)
        assert len(passes) == 1, "an idle dispatcher must not poll"

        await service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await _until(lambda: _dispatched_turn(service))
        turn_id = await _dispatched_turn(service)

        # The response settling, not a poll, retires the follow-up.
        await _finish(store, turn_id)

        async def settled():
            return (await service.get("s")).items[0]["status"] == "complete"

        await _until(settled)
        assert (await store.count(ChatTurn)) == 1
        await asyncio.sleep(0.2)
        quiet = len(passes)
        await asyncio.sleep(0.3)
        assert len(passes) == quiet, "a settled queue must not keep it awake"
    finally:
        await _stop(task)
        await service.chat.shutdown()


async def test_follow_up_dispatches_when_the_blocking_turn_settles(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 60.0)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    # An operator's direct Send is still running when the follow-up is saved.
    direct = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    _hold_dispatched_turns(service)
    await enqueue(service, request)
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(len(passes) == 1))
        await asyncio.sleep(0.2)
        assert (await service.get("s")).items[0]["status"] == "queued"
        await _finish(store, direct.id)
        await _until(lambda: _dispatched_turn(service, other_than=direct.id))
    finally:
        await _stop(task)
        await service.chat.shutdown()


async def test_run_answers_a_follow_up_after_the_response_it_waited_for(
    tmp_path, monkeypatch
):
    # End to end through run(): the follow-up's own turn is answered by the
    # provider and settles the queue, with no poll interval elapsing.
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 60.0)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    direct = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )
    await enqueue(service, request)
    task = asyncio.create_task(service.run())
    try:
        await asyncio.sleep(0.2)
        assert (await service.get("s")).items[0]["status"] == "queued"
        await _finish(store, direct.id)

        async def answered():
            item = (await service.get("s")).items[0]
            return item["status"] == "complete" and item["turn_id"] != direct.id

        await _until(answered)
        follow_up = await store.get(
            ChatTurn, (await service.get("s")).items[0]["turn_id"]
        )
        assert follow_up.status == ChatTurnStatus.COMPLETE
    finally:
        await _stop(task)
        await service.chat.shutdown()


async def test_review_of_an_ended_turn_rests_until_that_turn_changes(
    tmp_path, monkeypatch
):
    # A fast active cadence would expose any polling of the parked review.
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.INTERRUPTED,
        )
    )
    items = queue.items
    items[0].update(status="needs_review", turn_id=turn.id, detail="Interrupted")
    await store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    reads = []
    real_turn = service.turn

    async def counting_turn(session_id, turn_id):
        reads.append(turn_id)
        return await real_turn(session_id, turn_id)

    service.turn = counting_turn
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(service._parked != {}))
        seen = len(reads)
        await asyncio.sleep(0.3)
        assert len(reads) == seen, "a review of an ended turn must not be polled"

        # Resuming the interrupted turn to completion settles the review.
        await store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=turn.revision,
        )

        async def reconciled():
            return (await service.get("s")).items[0]["status"] == "complete"

        await _until(reconciled)
    finally:
        await _stop(task)


async def test_activity_in_another_conversation_leaves_a_parked_queue_alone(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.FAILED,
        )
    )
    items = queue.items
    items[0].update(status="needs_review", turn_id=turn.id, detail="Failed")
    await store.update(
        ChatQueue,
        queue.id,
        {"items": items, "paused": True},
        expected_revision=queue.revision,
    )
    other = await store.create(
        ChatSession(
            engagement_id="p",
            provider_profile_id="provider-a",
            model="model-a",
            title="Busy conversation",
        )
    )
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(service._parked != {}))
        quiet = len(passes)
        for _ in range(10):
            busy = await store.create(
                ChatTurn(
                    engagement_id="p",
                    session_id=other.id,
                    provider_profile_id="provider-a",
                    model="model-a",
                    status=ChatTurnStatus.ROUTING,
                )
            )
            await store.update(
                ChatTurn,
                busy.id,
                {"status": ChatTurnStatus.COMPLETE},
                expected_revision=busy.revision,
            )
        await asyncio.sleep(0.2)
        assert len(passes) == quiet, "another conversation's turns woke the queue"
    finally:
        await _stop(task)


async def test_sent_follow_up_waiting_for_approval_rests_until_its_turn_moves(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.WAITING_APPROVAL,
        )
    )
    items = queue.items
    items[0].update(status="sending", turn_id=turn.id)
    await store.update(
        ChatQueue, queue.id, {"items": items}, expected_revision=queue.revision
    )
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(service._parked != {}))
        quiet = len(passes)
        await asyncio.sleep(0.3)
        assert len(passes) == quiet, "a turn waiting for approval must not be polled"
        await _finish(store, turn.id)

        async def settled():
            return (await service.get("s")).items[0]["status"] == "complete"

        await _until(settled)
    finally:
        await _stop(task)


async def test_follow_up_behind_a_response_awaiting_approval_rests_until_it_moves(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 60.0)
    store, service, request = await setup_queue(tmp_path)
    _hold_dispatched_turns(service)
    # The operator's direct Send waits for an approval when the follow-up is saved.
    direct = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            provider_profile_id="provider-a",
            model="model-a",
            status=ChatTurnStatus.WAITING_APPROVAL,
        )
    )
    await enqueue(service, request)
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    try:
        await _until(lambda: _value(service._parked != {}))
        quiet = len(passes)
        await asyncio.sleep(0.3)
        assert len(passes) == quiet, (
            "a response waiting for approval must not be polled"
        )
        await _finish(store, direct.id)
        await _until(lambda: _dispatched_turn(service, other_than=direct.id))
    finally:
        await _stop(task)
        await service.chat.shutdown()


async def test_safety_pass_finds_a_queue_written_by_another_store(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(chat_queue, "ACTIVE_POLL_SECONDS", 60.0)
    monkeypatch.setattr(chat_queue, "SAFETY_POLL_SECONDS", 0.3)
    store, service, request = await setup_queue(tmp_path)
    _hold_dispatched_turns(service)
    queue = await enqueue(service, request, paused=True)
    passes = _count_passes(service)
    task = asyncio.create_task(service.run())
    other = NebulaStore(tmp_path / "queue.db")
    try:
        await _until(lambda: _value(len(passes) == 1))
        # This write never signals the dispatcher's own store.
        await other.update(
            ChatQueue, queue.id, {"paused": False}, expected_revision=queue.revision
        )
        await _until(lambda: _dispatched_turn(service))
    finally:
        await _stop(task)
        await service.chat.shutdown()
        await other.database.dispose()


async def test_queue_route_answers_not_modified(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    app = FastAPI()
    app.include_router(queue_router(service))
    client = TestClient(app)

    first = await client.get("/chat/sessions/s/queue")
    assert first.status_code == 200 and first.json()["revision"] == 0
    etag = first.headers["etag"]
    assert (
        await client.get("/chat/sessions/s/queue", headers={"If-None-Match": etag})
    ).status_code == 304

    await enqueue(service, request, 0, "first", paused=True)
    changed = await client.get(
        "/chat/sessions/s/queue", headers={"If-None-Match": etag}
    )
    assert changed.status_code == 200
    assert [item["key"] for item in changed.json()["items"]] == ["first"]


async def test_live_queue_is_not_dormant(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    queue = await enqueue(service, request)
    assert not queue_is_dormant(queue)
    paused = await service.write(
        "s", QueueWrite(action="pause", expected_revision=queue.revision)
    )
    assert queue_is_dormant(paused)


async def test_cross_origin_clients_may_send_and_read_validators(tmp_path):
    # The desktop app talks to Core from another origin; the browser must let
    # it send If-None-Match and read the ETag it gets back.
    store, _, _, session, _ = await _conversation(tmp_path)
    client = TestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/state"
    preflight = await client.options(
        path,
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,if-none-match",
        },
    )
    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].lower()

    read = await client.get(path, headers={**_auth(), "Origin": "tauri://localhost"})
    assert read.status_code == 200
    exposed = read.headers["access-control-expose-headers"].lower()
    assert "etag" in exposed.split(", ")


pytestmark = pytest.mark.anyio
