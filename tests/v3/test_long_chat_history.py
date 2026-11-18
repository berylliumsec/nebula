"""A conversation of hundreds of thousands of messages must not stall its worker.

Its history is read a bounded page per transaction, CPU-heavy pure steps over
it run off the event loop, appends use one aggregate, and a full transcript
streams a slice at a time. The results stay exactly what they were.
"""

import asyncio
import json
import threading
from datetime import timedelta

import pytest

from nebula.v3 import async_utils, chat as chat_module, execution_kernel, storage
from nebula.v3.api import create_app
from nebula.v3.context import ContextCompactor, source_digest
from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ContextOwnerType,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import AsyncTestClient
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def history(location):
    store = NebulaStore(Database(location, pool_settings=PoolSettings(5, 12, 2, 5)))
    try:
        await store.create(Engagement(id="project", name="Project"))
        await store.create(
            ProviderProfile(id="provider", name="Inert", provider_type="openrouter")
        )
        session = await store.create(
            ChatSession(
                id="long-chat",
                engagement_id="project",
                title="Long chat",
                model="inert",
                provider_profile_id="provider",
            )
        )
        # Ties on creation time must still page by id, as the one query did.
        base = utc_now() - timedelta(hours=1)
        async with store.transaction() as transaction:
            for index in range(1, 238):
                await transaction.add(
                    ChatMessage(
                        engagement_id="project",
                        session_id=session.id,
                        sequence=index,
                        role=ChatRole.USER if index % 2 else ChatRole.ASSISTANT,
                        content=f"message {index}",
                        created_at=base + timedelta(seconds=index // 3),
                    )
                )
        yield store, session
    finally:
        await store.event_history.close()
        await store.database.dispose()


def _ordered(rows, newest_first=False):
    return sorted(rows, key=lambda row: (row[0], row[1]), reverse=newest_first)


async def _expected(store, session_id, newest_first=False):
    async with store.database.session() as connection:
        from sqlalchemy import select

        rows = (
            await connection.execute(
                select(EntityRow.created_at, EntityRow.id).where(
                    EntityRow.kind == "chat_messages",
                    EntityRow.chat_session_id == session_id,
                )
            )
        ).all()
    return [entity_id for _, entity_id in _ordered(rows, newest_first)]


async def test_long_history_reads_a_bounded_page_per_transaction(history, monkeypatch):
    store, session = history
    monkeypatch.setattr(storage, "HISTORY_PARTITION", 20)
    opened = []
    original = store.database.session

    def counted(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.database, "session", counted)
    everything = await store.list_session_entities(ChatMessage, session.id)
    assert [item.id for item in everything] == await _expected(store, session.id)
    assert len(opened) >= 237 // 20
    newest = await store.list_session_entities(
        ChatMessage, session.id, newest_first=True, limit=55
    )
    assert [item.id for item in newest] == (
        await _expected(store, session.id, newest_first=True)
    )[:55]
    before = newest[-1]
    older = await store.list_session_entities(
        ChatMessage, session.id, newest_first=True, before=before
    )
    assert [item.id for item in older] == (
        await _expected(store, session.id, newest_first=True)
    )[55:]


async def test_next_sequence_is_one_aggregate_over_every_stored_message(history):
    store, session = history
    service = chat_module.ChatService(store)
    assert await store.max_chat_message_sequence(session.id) == 237
    assert await store.max_chat_message_sequence("no-such-chat") is None
    assert await service._next_sequence(session) == 238
    recorded = session.model_copy(
        update={"metadata": {**session.metadata, "last_sequence": 400}}
    )
    assert await service._next_sequence(recorded) == 401


async def test_large_pure_work_runs_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(async_utils, "OFF_LOOP_ITEMS", 3)
    loop_thread = threading.get_ident()
    assert await async_utils.off_loop(3, threading.get_ident) == loop_thread
    assert await async_utils.off_loop(4, threading.get_ident) != loop_thread
    # The merge a turn makes gives the same result on or off the loop.
    stored = [
        ChatMessage(
            engagement_id="project",
            session_id="chat",
            sequence=index,
            role=ChatRole.USER if index % 2 else ChatRole.ASSISTANT,
            content=f"message {index}",
        )
        for index in range(1, 8)
    ]
    incoming = [chat_module.ChatRequestMessage(role=ChatRole.USER, content="next")]
    merged = await chat_module.ChatService._merge_history_off_loop(stored, incoming)
    assert merged == chat_module.ChatService._merge_history(stored, incoming)


async def test_compaction_sources_validate_against_the_sessions_own_messages(
    history,
):
    store, session = history
    messages = await store.list_session_entities(ChatMessage, session.id)
    sources = chat_module._chat_context_sources(messages[:40])
    compactor = ContextCompactor(store)
    await compactor._validate_canonical_sources(
        ContextOwnerType.CHAT_SESSION, session.id, sources
    )
    assert chat_module._covered_digest(messages[:40]) == source_digest(sources)
    moved = chat_module._chat_context_sources(
        [messages[0].model_copy(update={"sequence": 999})]
    )
    with pytest.raises(ValueError, match="does not exist in this session"):
        await compactor._validate_canonical_sources(
            ContextOwnerType.CHAT_SESSION, session.id, moved
        )


async def test_full_transcript_streams_the_same_json(history, monkeypatch):
    store, session = history
    client = AsyncTestClient(create_app(store, auth_token="test-token"))
    path = f"/api/v1/chat/sessions/{session.id}/messages"
    headers = {"Authorization": "Bearer test-token"}
    whole = await client.get(path, params={"include_replaced": "true"}, headers=headers)
    monkeypatch.setattr(execution_kernel, "OFF_LOOP_ITEMS", 10)
    streamed = await client.get(
        path, params={"include_replaced": "true"}, headers=headers
    )
    assert whole.status_code == streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("application/json")
    assert json.loads(streamed.content) == json.loads(whole.content)
    assert len(json.loads(streamed.content)) == 237


async def test_long_history_read_leaves_the_loop_responsive(history, monkeypatch):
    store, session = history
    monkeypatch.setattr(storage, "HISTORY_PARTITION", 10)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    try:
        await store.list_session_entities(ChatMessage, session.id)
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
    # The read yielded between its pages, so other tasks ran meanwhile.
    assert ticks >= 237 // 10
