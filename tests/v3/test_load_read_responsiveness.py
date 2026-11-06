"""Search and Work bursts leave Core responsive to unrelated operator reads."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import threading
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import delete, event, func, select

from nebula.v3.action_registry import ActionRegistry
from nebula.v3.api import create_app
from nebula.v3.database import SearchDocumentRow
from nebula.v3.domain import Engagement, Observation, SearchScope, WorkItem, WorkUpdate
from nebula.v3.read_workers import read_in_worker
from nebula.v3.search import FederatedSearch
from nebula.v3.storage import NebulaStore


AUTH = {"Authorization": "Bearer synthetic-load-token"}
READS = [
    ("/search", {"query": "Synthetic", "active_project": "project"}, "note"),
    ("/work/items", {}, "item"),
    ("/work/updates", {}, "update"),
    ("/work/updates", {"source_session_id": "chat"}, "update"),
    ("/engagements/project/work", {}, "item"),
    ("/engagements/project/work/item/updates", {}, "update"),
]


async def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "load.db")
    (
        await store.create_many(
            [
                Engagement(id="project", name="Project"),
                Observation(
                    id="note",
                    engagement_id="project",
                    observation_type="note",
                    title="Synthetic note λ",
                    body="Saved content remains authoritative.",
                ),
                WorkItem(id="item", engagement_id="project", title="Synthetic work λ"),
                WorkUpdate(
                    id="update",
                    engagement_id="project",
                    item_id="item",
                    summary="Saved progress λ",
                    status="backlog",
                    actor_kind="operator",
                    actor_id="fixture",
                    source_session_id="chat",
                ),
            ]
        )
    )
    return store, create_app(
        store, auth_token="synthetic-load-token", execution_data_root=tmp_path
    )


def _expensive_query(statement, parameters):
    return statement.startswith("SELECT") and (
        "search_documents" in statement
        or WorkItem.entity_kind in parameters
        or WorkUpdate.entity_kind in parameters
    )


@pytest.mark.parametrize(("path", "params", "identity"), READS)
async def test_slow_search_or_work_read_leaves_other_requests_responsive(
    tmp_path, path, params, identity
):
    store, app = await _fixture(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    read_threads = []

    def hold_read(conn, cursor, statement, parameters, context, executemany):
        if not _expensive_query(statement, parameters):
            return
        read_threads.append(threading.get_ident())
        entered.set()
        if threading.get_ident() == loop_thread:
            return
        assert release.wait(3), "slow history read was never released"

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            slow = asyncio.create_task(client.get(path, params=params))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    if slow.done():
                        await slow
                        pytest.fail("history read never reached the diagnostic gate")
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                assert loop_thread not in read_threads, (
                    "history read occupied Core's loop"
                )
                response = await asyncio.wait_for(
                    client.get("/engagements/project/assistant-defaults"), timeout=0.5
                )
                assert response.status_code == 200, response.text
                assert not slow.done(), "history gate did not hold the expensive read"
                release.set()
                result = await asyncio.wait_for(slow, timeout=3)
                assert result.status_code == 200, result.text
                assert result.headers["content-type"] == "application/json"
                body = result.json()
                if path == "/search":
                    assert body["items"][0]["ref"]["id"] == identity
                    assert body["items"][0]["label"] == "Synthetic note λ"
                else:
                    assert [row["id"] for row in body] == [identity]
            finally:
                release.set()
                if not slow.done():
                    slow.cancel()
                with suppress(asyncio.CancelledError):
                    await slow

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", hold_read)
    try:
        (await exercise())
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", hold_read
        )
        (await store.database.engine.dispose())


async def test_search_page_does_not_add_a_query_per_result(tmp_path):
    store, app = await _fixture(tmp_path)
    (
        await store.create_many(
            [
                Observation(
                    id=f"note-{index}",
                    engagement_id="project",
                    observation_type="note",
                    title=f"Synthetic note {index}",
                    body="Saved long note. " * 200,
                )
                for index in range(32)
            ]
        )
    )
    queries = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            queries.append(statement)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            params = {"query": "Synthetic", "active_project": "project", "limit": 1}
            first = await client.get("/search", params=params)
            assert first.status_code == 200, first.text
            first_count = len(queries)
            queries.clear()
            page = await client.get("/search", params={**params, "limit": 30})
            assert page.status_code == 200, page.text
            assert len(page.json()["items"]) == 30
            assert page.json()["next_cursor"]
            assert page.json()["items"][0] == first.json()["items"][0]
            assert len(queries) == first_count, (
                "each result added another database read"
            )

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", record)
    try:
        (await exercise())
    finally:
        event.remove(store.database.engine.sync_engine, "before_cursor_execute", record)
        (await store.database.engine.dispose())


async def test_parallel_search_instances_repair_a_missing_projection_once(tmp_path):
    store, app = await _fixture(tmp_path)
    async with store.database.session() as session:
        (await session.execute(delete(SearchDocumentRow)))
    entered = threading.Event()
    release = threading.Event()

    # Hold the first repair at SQL execution, before any document can commit.
    def hold_insert(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("INSERT INTO search_documents"):
            return
        entered.set()
        assert release.wait(5), "projection repair was not released"

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            params = {"query": "Synthetic", "active_project": "project"}
            tasks = [asyncio.create_task(client.get("/search", params=params))]
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    if tasks[0].done():
                        await tasks[0]
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                tasks.extend(
                    asyncio.create_task(client.get("/search", params=params))
                    for _ in range(11)
                )
                # Butler constructs separate search services over Core's same store.
                for _ in range(2):
                    search = FederatedSearch(store, ActionRegistry(store))
                    tasks.append(
                        asyncio.create_task(
                            search.search(
                                query="Synthetic",
                                active_project="project",
                                scope=SearchScope.ACTIVE,
                                kinds=[],
                                cursor=None,
                                limit=30,
                            )
                        )
                    )
                await asyncio.sleep(0.05)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
                for response in results[:12]:
                    assert response.status_code == 200, response.text
                    assert response.json()["items"][0]["ref"]["id"] == "note"
                assert all(result.items[0].ref.id == "note" for result in results[12:])
                repairs = sum(
                    response.json()["partial_index"] for response in results[:12]
                )
                repairs += sum(result.partial_index for result in results[12:])
                assert repairs == 1, "competing searches repeated the same repair"
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    event.listen(
        store.database.engine.sync_engine, "before_cursor_execute", hold_insert
    )
    try:
        (await exercise())
        async with store.database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(SearchDocumentRow)
                )
            ) == 2
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", hold_insert
        )
        (await store.database.engine.dispose())


@pytest.mark.parametrize("mutation", ["edit", "delete"])
async def test_projection_repair_preserves_concurrent_saved_changes(
    tmp_path, monkeypatch, mutation
):
    from nebula.v3 import search as search_module

    store, app = await _fixture(tmp_path)
    async with store.database.session() as session:
        # Pause before the first repair write; an earlier SQLite write would
        # correctly serialize the competing edit behind the repair transaction.
        (
            await session.execute(
                delete(SearchDocumentRow).where(SearchDocumentRow.id == "note")
            )
        )
    entered = threading.Event()
    release = threading.Event()
    original = search_module.upsert_search_document

    def hold_old_note(session, row):
        if row.id == "note" and row.revision == 1:
            entered.set()
            assert release.wait(5), "saved-note race was not released"
        return original(session, row)

    monkeypatch.setattr(search_module, "upsert_search_document", hold_old_note)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            request = asyncio.create_task(
                client.get(
                    "/search",
                    params={"query": "Synthetic", "active_project": "project"},
                )
            )
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    if request.done():
                        await request
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                if mutation == "edit":
                    saved = await store.update(
                        Observation,
                        "note",
                        {"title": "Synthetic newer note"},
                        expected_revision=1,
                    )
                else:
                    (await store.delete(Observation, "note", expected_revision=1))
                release.set()
                response = await asyncio.wait_for(request, timeout=5)
                assert response.status_code == 200, response.text
                if mutation == "delete":
                    assert response.json()["items"] == []
                    async with store.database.session() as session:
                        assert (await session.get(SearchDocumentRow, "note")) is None
                    return
                note = response.json()["items"][0]
                assert note["label"] == saved.title
                assert note["ref"]["revision"] == saved.revision
                assert all(
                    action["available"]
                    for action in note["actions"]
                    if action["authority"] != "device"
                )
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)

    try:
        (await exercise())
    finally:
        (await store.database.engine.dispose())


async def test_mixed_read_burst_retains_capacity_when_callers_cancel(tmp_path):
    store, app = await _fixture(tmp_path)
    release = threading.Event()
    guard = threading.Lock()
    entered = 0
    loop_thread = threading.get_ident()

    def hold_read(conn, cursor, statement, parameters, context, executemany):
        nonlocal entered
        if not _expensive_query(statement, parameters):
            return
        assert threading.get_ident() != loop_thread
        with guard:
            entered += 1
        assert release.wait(5), "read burst occupied Core's request loop"

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://core.test/api/v1",
            headers=AUTH,
        ) as client:
            tasks = [
                asyncio.create_task(client.get(path, params=params))
                for path, params, _ in READS * 4
            ]
            try:
                for _ in range(200):
                    if entered == 4:
                        break
                    if any(task.done() for task in tasks):
                        await next(task for task in tasks if task.done())
                    await asyncio.sleep(0.005)
                assert entered == 4, "expensive reads did not share bounded admission"
                for task in tasks[:4]:
                    task.cancel()
                await asyncio.sleep(0.05)
                assert entered == 4, "cancelled callers released physical read capacity"
                response = await asyncio.wait_for(
                    client.get("/engagements/project/assistant-defaults"), timeout=0.5
                )
                assert response.status_code == 200, response.text
                assert store.database.engine.pool.checkedout() <= 4
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                assert all(
                    isinstance(value, asyncio.CancelledError) for value in results[:4]
                )
                assert all(value.status_code == 200 for value in results[4:])
                assert (await client.get("/work/items")).status_code == 200
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(*(read_in_worker(lambda: None) for _ in range(4)))

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", hold_read)
    try:
        (await exercise())
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", hold_read
        )
        (await store.database.engine.dispose())


pytestmark = pytest.mark.anyio
