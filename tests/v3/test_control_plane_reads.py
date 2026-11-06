"""Control-plane reads remain complete and responsive as stored history grows."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import ContextVar
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session, sessionmaker

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService
from nebula.v3.control_assistant_work import dispatch_work_tool
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    ChatGoal,
    ChatMessage,
    ChatSession,
    Engagement,
    HarnessTurn,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    ScopePolicy,
    utc_now,
)
from nebula.v3.read_workers import read_in_worker
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.temporary_chat import temporary_chat_router
from tests.v3.test_butler_capabilities import _scheduled_capabilities
from tests.v3.test_harnesses import _runtime


async def test_interactive_and_scheduled_read_entry_points_use_worker_owned_sessions(
    tmp_path, monkeypatch
):
    store, project, chat, _, _, _, scheduled = await _scheduled_capabilities(tmp_path)
    interactive = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id, work=scheduled._work
    )
    (
        await store.create(
            ChatGoal(
                engagement_id=project.id,
                session_id=chat.id,
                objective="Synthetic inspection",
                completion_criteria=["Observe stored state"],
            )
        )
    )
    loop_thread = threading.get_ident()
    observed = []

    class OwnedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.get_ident()

        def scalars(self, *args, **kwargs):
            assert threading.get_ident() == self.owner
            return super().scalars(*args, **kwargs)

        def commit(self):
            assert threading.get_ident() == self.owner
            return super().commit()

        def close(self):
            assert threading.get_ident() == self.owner
            return super().close()

    store.database._session_factory = sessionmaker(
        bind=store.database.engine, class_=OwnedSession, expire_on_commit=False
    )
    for method in ("find_entities", "list_entities", "iter_readable_entities"):
        original = getattr(store, method)

        def checked(*args, selected=original, **kwargs):
            assert threading.get_ident() != loop_thread, selected.__name__
            observed.append(selected.__name__)
            return selected(*args, **kwargs)

        monkeypatch.setattr(store, method, checked)

    async def exercise():
        for operation in (
            "nebula.settings.models",
            "nebula.projects.list",
            "nebula.providers.list",
            "nebula.harnesses.list",
            "nebula.mcp.list",
            "nebula.devices.list",
            "nebula.evidence.list",
            "nebula.library.list",
            "nebula.notes.list",
            "nebula.assets.list",
            "nebula.reports.list",
            "nebula.storage.sources",
            "nebula.sources.list",
            "nebula.data.results",
            "nebula.work.catalog",
            "nebula.work.overview",
            "nebula.work.chats",
            "nebula.work.goals",
        ):
            result = await interactive.invoke(
                {"operation": operation, "arguments": {}}, change=False
            )
            assert result["butler_operation"] == operation
        for operation, arguments in (
            ("nebula.global.projects", {}),
            ("nebula.global.project", {"project_id": project.id}),
            ("nebula.global.goal", {"project_id": project.id, "session_id": chat.id}),
            ("nebula.global.assistant_defaults", {"project_id": project.id}),
            ("nebula.global.work_catalog", {"project_id": project.id}),
            (
                "nebula.global.chat_settings_many",
                {"project_id": project.id, "session_ids": [chat.id]},
            ),
            (
                "nebula.global.work_read",
                {
                    "project_id": project.id,
                    "operation": "nebula.work.chat_settings",
                    "arguments": {"session_id": chat.id},
                },
            ),
            ("nebula.inspect.turns", {"session_id": chat.id}),
        ):
            target = (
                interactive if operation.startswith("nebula.inspect") else scheduled
            )
            result = await target.invoke(
                {"operation": operation, "arguments": arguments}, change=False
            )
            assert result["butler_operation"] == operation

    (await exercise())
    assert observed


async def test_mixed_settings_read_burst_retains_capacity_after_cancellation(tmp_path):
    store, project, chat, _, _, _, scheduled = await _scheduled_capabilities(tmp_path)
    app = scheduled._work.app
    release = threading.Event()
    guard = threading.Lock()
    entered = 0
    loop_thread = threading.get_ident()

    def hold_catalog(conn, cursor, statement, parameters, context, executemany):
        nonlocal entered
        if (
            not statement.startswith("SELECT")
            or McpServerProfile.entity_kind not in parameters
        ):
            return
        assert threading.get_ident() != loop_thread
        with guard:
            entered += 1
        assert release.wait(5), "catalog reads blocked Core's request loop"

    event.listen(store.database.engine, "before_cursor_execute", hold_catalog)

    async def read(index):
        if index % 3 == 0:
            return await dispatch_work_tool(
                scheduled._work,
                project.id,
                "nebula.work.chat_settings",
                {"session_id": chat.id},
            )
        operation = (
            "nebula.global.chat_settings"
            if index % 3 == 1
            else "nebula.global.work_read"
        )
        arguments = (
            {"project_id": project.id, "session_id": chat.id}
            if index % 3 == 1
            else {
                "project_id": project.id,
                "operation": "nebula.work.chat_settings",
                "arguments": {"session_id": chat.id},
            }
        )
        return await scheduled.invoke(
            {"operation": operation, "arguments": arguments}, change=False
        )

    async def exercise():
        tasks = [asyncio.create_task(read(index)) for index in range(24)]
        try:
            for _ in range(200):
                if entered == 4:
                    break
                if any(task.done() for task in tasks):
                    await next(task for task in tasks if task.done())
                await asyncio.sleep(0.005)
            assert entered == 4
            for task in tasks[:4]:
                task.cancel()
            await asyncio.sleep(0.05)
            assert entered == 4, "cancellation released a still-running reader's slot"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://core.test"
            ) as client:
                response = await asyncio.wait_for(
                    client.get(
                        "/api/v1/auth/devices",
                        headers={"Authorization": "Bearer test-token"},
                    ),
                    timeout=0.5,
                )
                assert response.status_code == 200
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(item, asyncio.CancelledError) for item in results[:4])
            assert all(item["session_id"] == chat.id for item in results[4:])
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Drain all four physical workers, including cancelled callers.
            await asyncio.gather(*(read_in_worker(lambda: None) for _ in range(4)))

    try:
        (await exercise())
    finally:
        event.remove(store.database.engine, "before_cursor_execute", hold_catalog)


async def test_worker_retains_context_and_recovers_from_a_failed_read():
    identity = ContextVar("test_read_identity", default="missing")

    def failed():
        assert identity.get() == "synthetic-project"
        raise ValueError("synthetic storage failure")

    async def exercise():
        identity.set("synthetic-project")
        with pytest.raises(ValueError, match="synthetic storage failure"):
            await read_in_worker(failed)
        assert await read_in_worker(identity.get) == "synthetic-project"

    (await exercise())


async def test_racing_harness_activity_reads_cannot_publish_an_older_goal(
    tmp_path, monkeypatch
):
    from nebula.v3.harnesses import HarnessEvent

    store, project, profile, _, _, runtime = await _runtime(tmp_path)
    session = await runtime.create_session(
        engagement_id=project.id, profile_id=profile.id, model=None
    )
    turn = await store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin="analysis",
            prompt="Synthetic read",
            status="running",
        )
    )

    async def record(objective):
        (
            await runtime._persist_activity(
                turn,
                session,
                HarnessEvent(
                    type="item_upsert",
                    item_kind="goal",
                    item_id="goal",
                    goal={"objective": objective, "status": "running"},
                ),
            )
        )

    (await record("Initial"))
    (await runtime._turn_activity_state(turn))
    (await record("Older replay"))
    queried = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    original = store.replay_operation_events

    async def racing(*args, **kwargs):
        rows = await original(*args, **kwargs)
        if threading.get_ident() != loop_thread:
            queried.set()
            assert release.wait(3)
        return rows

    monkeypatch.setattr(store, "replay_operation_events", racing)
    with ThreadPoolExecutor(max_workers=1) as worker:
        older = worker.submit(runtime._turn_activity_state, turn)
        try:
            assert queried.wait(2)
            (await record("Newest replay"))
            newest = await runtime._turn_activity_state(turn)
            release.set()
            late = older.result(timeout=2)
            assert newest.goal.objective == late.goal.objective == "Newest replay"
            cached = runtime._turn_activity_states[turn.id]
            assert cached.goal.objective == "Newest replay"
            assert cached.cursor >= newest.cursor
        finally:
            release.set()


async def test_search_policy_count_keeps_legacy_defaults_without_hydration(tmp_path):
    store = NebulaStore(tmp_path / "policies.db")
    for index, changes in enumerate(
        (
            {"web_search": True},
            {"web_search": True, "local_only": True},
            {"web_search": False},
        )
    ):
        (
            await store.create(
                ScopePolicy(id=f"scope-{index}", engagement_id=f"p-{index}", **changes)
            )
        )
    legacy = (await store.get(ScopePolicy, "scope-0")).model_dump(mode="json")
    del legacy["local_only"]
    async with store.database.session() as session:
        (
            await session.execute(
                update(EntityRow)
                .where(EntityRow.id == "scope-0")
                .values(payload=legacy)
            )
        )
    assert (await store.web_search_project_count()) == 1


async def test_chat_preview_reads_recent_active_messages_in_sequence_order(
    tmp_path, monkeypatch
):
    store, project, chat, _, _, _, scheduled = await _scheduled_capabilities(tmp_path)
    now = utc_now()
    messages = [
        ChatMessage(
            id=f"message-{index:04}",
            engagement_id=project.id,
            session_id=chat.id,
            sequence=index + 1,
            role="system" if index == 998 else "assistant",
            content=f"Message {index}",
            created_at=now - timedelta(seconds=index),
            metadata={"retracted_at": "synthetic"} if index >= 1000 else {},
        )
        for index in range(1100)
    ]
    async with store.transaction() as transaction:
        (await transaction.add_chat_messages(messages))
    expected = (await ChatService(store).session_messages(chat.id))[-8:]
    hydrated = Counter()
    from nebula.v3 import storage

    original = storage._row_to_entity

    def counted(row, model):
        hydrated[model.entity_kind] += 1
        return original(row, model)

    monkeypatch.setattr(storage, "_row_to_entity", counted)
    result = scheduled._work.chat_detail(project.id, chat.id)
    assert result["recent_messages"] == [
        {"id": item.id, "role": item.role.value, "content": item.content[:2000]}
        for item in expected
        if item.role.value in {"user", "assistant"}
    ]
    assert hydrated[ChatMessage.entity_kind] == 108
    assert (await store.get(ChatSession, chat.id)).revision == chat.revision


async def test_child_lookup_filters_before_hydration_and_has_no_page_horizon(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "children.db")
    (
        await store.create(
            ChatSession(
                id="parent-chat",
                engagement_id="p",
                title="Parent",
                model="m",
                provider_profile_id="synthetic",
            )
        )
    )
    parent = await store.create(
        ChatGoal(
            engagement_id="p",
            session_id="parent-chat",
            objective="Parent",
            completion_criteria=["Observe"],
        )
    )
    (
        await store.create_many(
            [
                ChatGoal(
                    engagement_id="other",
                    session_id=f"other-{index}",
                    objective="Unrelated",
                    completion_criteria=["Observe"],
                )
                for index in range(1001)
            ]
        )
    )
    children = await store.create_many(
        [
            ChatGoal(
                engagement_id="p",
                session_id=f"child-{index}",
                parent_goal_id=parent.id,
                objective="Child",
                completion_criteria=["Observe"],
            )
            for index in range(2)
        ]
    )
    (
        await store.create(
            ChatGoal(
                engagement_id="other",
                session_id="wrong-project",
                parent_goal_id=parent.id,
                objective="Foreign",
                completion_criteria=["Observe"],
            )
        )
    )
    from nebula.v3 import storage

    hydrated = []
    original = storage._row_to_entity

    def counted(row, model):
        hydrated.append(row.id)
        return original(row, model)

    monkeypatch.setattr(storage, "_row_to_entity", counted)
    assert [
        row.id for row in (await ChatGoalService(store).list_children("parent-chat"))
    ] == [row.id for row in children]
    assert len(hydrated) <= 4
    assert (await ChatGoalService(store).list_children("missing-goal-chat")) == []


async def test_scope_catalog_reaches_late_profiles_and_keeps_notebook_scope(tmp_path):
    store = NebulaStore(tmp_path / "catalog.db")
    project = await store.create(Engagement(name="Synthetic"))
    (
        await store.create_many(
            [
                McpServerProfile(
                    name=f"Disabled-{index}",
                    transport="stdio",
                    command="/bin/true",
                    enabled=False,
                )
                for index in range(1001)
            ]
        )
    )
    capabilities = McpCapabilitySnapshot(
        checked_at=utc_now(),
        tools=[
            McpToolSnapshot(name="read", description="Read fixture", read_only=True)
        ],
    )
    visible = await store.create(
        McpServerProfile(
            name="Late",
            transport="stdio",
            command="/bin/true",
            enabled=True,
            trusted_stdio=True,
            capabilities=capabilities,
        )
    )
    foreign = await store.create(
        McpServerProfile(
            name="Foreign-notebook",
            transport="stdio",
            command="/bin/true",
            enabled=True,
            trusted_stdio=True,
            capabilities=capabilities,
            metadata={"built_in": "notebook_v1", "engagement_id": "other"},
        )
    )
    app = create_app(
        store, auth_token="synthetic-token", execution_data_root=tmp_path / "runtime"
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core.test"
        ) as client:
            path = f"/api/v1/engagements/{project.id}/scope/tool-candidates"
            assert (await client.get(path)).status_code == 401
            response = await client.get(
                path, headers={"Authorization": "Bearer synthetic-token"}
            )
            assert response.status_code == 200
            servers = {row["server_id"] for row in response.json()}
            assert visible.id in servers
            assert foreign.id not in servers

    (await exercise())


@pytest.mark.parametrize("resource", ["scope-candidates", "search-status"])
async def test_slow_catalog_and_policy_status_leave_core_responsive(tmp_path, resource):
    store = NebulaStore(tmp_path / "slow-api.db")
    project = await store.create(Engagement(name="Synthetic"))
    app = create_app(
        store, auth_token="synthetic-token", execution_data_root=tmp_path / "runtime"
    )
    path = (
        f"/api/v1/engagements/{project.id}/scope/tool-candidates"
        if resource == "scope-candidates"
        else "/api/v1/integrations/web-search"
    )
    kind = (
        McpServerProfile.entity_kind
        if resource == "scope-candidates"
        else ScopePolicy.entity_kind
    )
    loop_thread = threading.get_ident()
    entered = 0
    release = threading.Event()
    guard = threading.Lock()

    def held(conn, cursor, statement, parameters, context, executemany):
        nonlocal entered
        if not statement.startswith("SELECT") or kind not in parameters:
            return
        assert threading.get_ident() != loop_thread
        with guard:
            entered += 1
        assert release.wait(5), "status read blocked Core's request loop"

    event.listen(store.database.engine, "before_cursor_execute", held)

    async def exercise():
        headers = {"Authorization": "Bearer synthetic-token"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core.test"
        ) as client:
            tasks = [
                asyncio.create_task(client.get(path, headers=headers))
                for _ in range(20)
            ]
            try:
                for _ in range(200):
                    if entered == 4:
                        break
                    if any(task.done() for task in tasks):
                        await next(task for task in tasks if task.done())
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.05)
                assert entered == 4
                response = await asyncio.wait_for(
                    client.get("/api/v1/auth/devices", headers=headers), timeout=0.5
                )
                assert response.status_code == 200
                assert (await client.get(path)).status_code == 401
                release.set()
                assert all(
                    response.status_code == 200
                    for response in await asyncio.gather(*tasks)
                )
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    try:
        (await exercise())
    finally:
        event.remove(store.database.engine, "before_cursor_execute", held)


async def test_cleanup_yields_and_only_discards_expired_detached_popups(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "cleanup.db")
    old = utc_now() - timedelta(days=2)
    for name, metadata in (
        ("ordinary", {}),
        ("side", {"side_chat": True, "temporary_assistant": True}),
        ("expired", {"temporary_assistant": True}),
        ("renewed", {"temporary_assistant": True}),
    ):
        (
            await store.create(
                ChatSession(
                    id=name,
                    engagement_id="p",
                    title=name,
                    model="m",
                    provider_profile_id="synthetic",
                    created_at=old,
                    updated_at=old,
                    metadata=metadata,
                )
            )
        )
    runtime = Mock(cancel_turn=AsyncMock(), close_session=AsyncMock())
    router = temporary_chat_router(store, lambda: ChatService(store), runtime)
    original = store.expired_temporary_chat_ids
    loop_thread = threading.get_ident()
    release = threading.Event()
    entered = threading.Event()

    async def held(before):
        assert threading.get_ident() != loop_thread
        ids = await original(before)
        entered.set()
        assert release.wait(3)
        return ids

    monkeypatch.setattr(store, "expired_temporary_chat_ids", held)

    async def exercise():
        try:
            async with router.lifespan_context(None):
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert entered.is_set()
                (
                    await store.update(
                        ChatSession,
                        "renewed",
                        {"metadata": {"temporary_assistant": True}},
                    )
                )
                release.set()
                for _ in range(200):
                    with suppress(NotFoundError):
                        (await store.get(ChatSession, "expired"))
                        await asyncio.sleep(0.005)
                        continue
                    break
                else:
                    pytest.fail("expired popup was not collected")
        finally:
            release.set()

    (await exercise())
    assert all(
        (await store.get(ChatSession, name)) for name in ("ordinary", "side", "renewed")
    )
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, "expired"))


pytestmark = pytest.mark.anyio
