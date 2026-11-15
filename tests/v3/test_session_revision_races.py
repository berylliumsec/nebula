"""#1100: Core's bookkeeping on a conversation lands inside a writer's window.

Each test runs the real concurrent writer (first-turn naming, an operator
enqueue, a peer message, a saved compaction) between the target's read and
its revision-guarded write.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import ChatSession, ChatTurn, Engagement
from nebula.v3.storage import ConflictError, NebulaStore
from tests.v3.revision_window import before_transaction, before_update
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_queue import enqueue, setup_queue

pytestmark = pytest.mark.anyio


async def _first_exchange(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "rewind.db")
    engagement = await store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = await store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store)
    deferred = []
    real_name = service._name_initial_session

    async def hold_naming(prepared, response):
        deferred.append((prepared, response))

    service._name_initial_session = hold_naming
    completion = await service.complete(
        await service.prepare(
            ChatCompletionRequest(
                engagement_id=engagement.id,
                provider_id=profile.id,
                include_knowledge=False,
                messages=[
                    {
                        "role": "user",
                        "content": "What does this certificate error mean?",
                    }
                ],
            )
        )
    )
    if service._naming_tasks:
        await asyncio.gather(*tuple(service._naming_tasks))
    assert deferred, "naming was not scheduled"
    return store, service, completion.session_id, real_name, deferred[0]


async def test_edit_in_place_survives_first_turn_naming(tmp_path, monkeypatch):
    store, service, session_id, real_name, args = await _first_exchange(
        tmp_path, monkeypatch
    )
    messages = await service.session_messages(session_id)
    before = await store.get(ChatSession, session_id)

    window = before_transaction(store, "_rewind_session_once", lambda: real_name(*args))
    # The UI's edit-in-place sends no expected_revision.
    session, retained, retracted = await service.rewind_session(
        session_id, before_message_id=messages[0].id
    )

    assert window.fired
    assert retained == []
    assert [item.id for item in retracted] == [item.id for item in messages]
    assert await service.session_messages(session_id) == []
    after = await store.get(ChatSession, session_id)
    # First-turn naming is kept beside the retraction.
    assert after.title != before.title
    assert session.revision == after.revision


async def test_edit_in_place_with_operator_revision_stays_strict(tmp_path, monkeypatch):
    store, service, session_id, real_name, args = await _first_exchange(
        tmp_path, monkeypatch
    )
    messages = await service.session_messages(session_id)
    before = await store.get(ChatSession, session_id)
    before_transaction(store, "_rewind_session_once", lambda: real_name(*args))
    with pytest.raises(ConflictError):
        await service.rewind_session(
            session_id,
            before_message_id=messages[0].id,
            expected_revision=before.revision,
        )
    assert len(await service.session_messages(session_id)) == len(messages)


async def _drive(service, rounds=60):
    for _ in range(rounds):
        await service.step(await service.get("s"))
        await asyncio.sleep(0.01)


async def test_operator_enqueue_during_dispatch_keeps_first_follow_up(tmp_path):
    store, service, request = await setup_queue(tmp_path)
    await enqueue(service, request)

    async def operator_enqueue():
        fresh = await service.get("s")
        await enqueue(service, request, fresh.revision, "second")

    window = before_transaction(store, "_write_turn_inputs", operator_enqueue)
    try:
        await _drive(service)
        final = await service.get("s")
        assert window.fired
        assert [item["status"] for item in final.items] == ["complete", "complete"]
        assert final.paused is False
        assert await store.count(ChatTurn) == 2
    finally:
        await service.chat.shutdown()


async def test_popup_keepalive_keeps_a_concurrent_turns_metadata(tmp_path):
    from unittest.mock import AsyncMock, Mock

    from fastapi import FastAPI

    from nebula.v3.temporary_chat import temporary_chat_router
    from tests.v3.async_test_support import AsyncTestClient

    store = NebulaStore(tmp_path / "keepalive.db")
    engagement = await store.create(Engagement(name="Popup"))
    profile = await store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    first = await service.prepare_async(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "First"}],
            include_knowledge=False,
        )
    )
    main_id = (await service.complete(first)).session_id
    runtime = Mock()
    runtime.cancel_turn = AsyncMock()
    runtime.close_session = AsyncMock()
    app = FastAPI()
    app.include_router(temporary_chat_router(store, lambda: service, runtime))
    client = AsyncTestClient(app)
    created = await client.post(
        "/chat/temporary-sessions",
        json={"engagement_id": engagement.id, "session_id": main_id},
    )
    assert created.status_code == 201, created.text
    popup_id = created.json()["id"]

    async def popup_turn_persists():
        # Stands in for the popup turn's saved bookkeeping.
        latest = await store.get(ChatSession, popup_id)
        await store.update(
            ChatSession,
            popup_id,
            {
                "metadata": {
                    **latest.metadata,
                    "last_sequence": 41,
                    "harness_context_handoff_pending": False,
                }
            },
            expected_revision=latest.revision,
        )

    window = before_update(
        store, "keepalive", popup_turn_persists, model=ChatSession, direct=False
    )
    response = await client.post(f"/chat/temporary-sessions/{popup_id}/keepalive")

    assert response.status_code == 204, response.text
    assert window.fired
    saved = await store.get(ChatSession, popup_id)
    assert saved.metadata["last_sequence"] == 41
    assert saved.metadata["harness_context_handoff_pending"] is False


async def test_subagent_report_posts_when_naming_lands_in_its_window(
    tmp_path, monkeypatch
):
    import inspect

    import nebula.v3.chat_subagents as chat_subagents_module
    from nebula.v3.domain import ChatMessage, ChatSubagent, ChatSubagentStatus
    from tests.v3.test_chat_subagents import (
        RoutedProvider,
        _call,
        _drain,
        _finish,
        _request,
        _response,
        _setup,
    )

    caught: list[str] = []
    original_record = chat_subagents_module.record_caught_exception

    def capture(feature, code, message, exc, **kwargs):
        caught.append(code)
        return original_record(feature, code, message, exc, **kwargs)

    monkeypatch.setattr(chat_subagents_module, "record_caught_exception", capture)
    naming_gate = asyncio.Event()

    class GatedNaming(RoutedProvider):
        async def complete(self, request):
            if request.metadata.get("operation") == "conversation_naming":
                await naming_gate.wait()
                return _response(text="Auth review")
            return await super().complete(request)

    provider = GatedNaming(
        parent=[
            _call(
                "p1",
                "start_subagent",
                task="Review the auth module.",
                name="Auth",
                context=None,
            ),
            _finish("p2"),
            _response(text="Started a review."),
            _finish("p3"),
            _response(text="Second answer."),
        ],
        child=[_response(text="Cookies lack SameSite.")],
    )
    store, project, _, chat = await _setup(tmp_path, provider)
    provider.child_gate = asyncio.Event()
    prepared = await chat.prepare_async(
        _request(project, content="Review auth.", allow_subagents=True)
    )
    await _drain(chat, await chat.start_provider_turn(prepared))
    (record,) = await store.list_entities(ChatSubagent)
    assert chat._naming_tasks, "first-turn naming should be in flight"

    # Land the real first-turn naming between the report's read of the
    # parent conversation and its write.
    original_messages = chat.session_messages
    fired = []

    async def paused(session_id, **kwargs):
        result = await original_messages(session_id, **kwargs)
        caller = inspect.currentframe().f_back
        if not fired and caller and caller.f_code.co_name == "_append_to_parent":
            fired.append(session_id)
            naming_gate.set()
            for task in list(chat._naming_tasks):
                await task
        return result

    chat.session_messages = paused
    provider.child_gate.set()
    try:
        for _ in range(500):
            finished = await store.get(ChatSubagent, record.id)
            if (
                finished.status == ChatSubagentStatus.COMPLETED
                and finished.result_message_id
            ):
                break
            await asyncio.sleep(0.01)
        assert fired
        assert finished.result_message_id
        assert "chat.subagent.result_post_failed" not in caught
        report = await store.get(ChatMessage, finished.result_message_id)
        session = await store.get(ChatSession, prepared.session.id)
        assert report.session_id == session.id
        assert session.title == "Auth review"
        assert session.metadata["last_sequence"] == report.sequence
    finally:
        await chat.shutdown()
