"""Cleanup must drain persistence before releasing every database resource."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from nebula.v3.async_utils import async_done_callback, close_store_resources
from nebula.v3.cli import _async_command, _cli_stores
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.storage import NebulaStore
from tests.v3.test_async_core import (
    reject_synchronous_database_waits as reject_synchronous_database_waits,
)


def _resource(events, name, *, fail=False, wait=None):
    async def close():
        if wait is not None:
            await wait.wait()
        events.append(name)
        if fail:
            raise ValueError(name)

    return close


async def test_callback_failure_still_joins_other_callbacks_before_disposal():
    async def exercise():
        events = []
        release = asyncio.Event()
        started = asyncio.Event()

        async def failed(_future):
            started.set()
            await release.wait()
            raise ValueError("callback failure")

        async def slow(_future):
            await release.wait()
            await asyncio.sleep(0.01)
            events.append("callback joined")

        future = asyncio.get_running_loop().create_future()
        async_done_callback(failed)(future)
        async_done_callback(slow)(future)
        await started.wait()
        store = SimpleNamespace(
            database=SimpleNamespace(dispose=_resource(events, "database")),
            event_history=SimpleNamespace(close=_resource(events, "history")),
        )
        cleanup = asyncio.create_task(close_store_resources([store]))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(ValueError, match="callback failure"):
            await cleanup
        assert events == ["callback joined", "history", "database"]

    (await exercise())


async def test_history_failure_disposes_all_stores_and_shared_engine_once():
    async def exercise():
        events = []
        database = SimpleNamespace(dispose=_resource(events, "database"))
        stores = [
            SimpleNamespace(
                database=database,
                event_history=SimpleNamespace(close=_resource(events, "first")),
            ),
            SimpleNamespace(
                database=database,
                event_history=SimpleNamespace(
                    close=_resource(events, "second", fail=True)
                ),
            ),
        ]
        with pytest.raises(ValueError, match="second"):
            await close_store_resources(stores)
        assert events == ["second", "first", "database"]

    (await exercise())


async def test_repeated_cancellation_joins_history_before_engine_disposal():
    async def exercise():
        events = []
        release = asyncio.Event()
        store = SimpleNamespace(
            database=SimpleNamespace(dispose=_resource(events, "database")),
            event_history=SimpleNamespace(
                close=_resource(events, "history", wait=release)
            ),
        )
        cleanup = asyncio.create_task(close_store_resources([store]))
        for _ in range(3):
            await asyncio.sleep(0)
            cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert events == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert events == ["history", "database"]

    (await exercise())


async def test_purge_timeout_joins_transaction_rollback_before_disposal(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "purge.db")
        started = asyncio.Event()
        rolled_back = asyncio.Event()
        async with store.database.session() as session:
            await session.execute(
                text("CREATE TABLE cancellation_probe (value INTEGER)")
            )

        async def worker():
            try:
                async with store.database.session() as session:
                    await session.execute(
                        text("INSERT INTO cancellation_probe VALUES (1)")
                    )
                    started.set()
                    await asyncio.Event().wait()
            finally:
                rolled_back.set()

        store.event_history._worker = asyncio.create_task(worker())
        await started.wait()
        try:
            with pytest.raises(TimeoutError):
                await store.event_history.close(timeout=0.01)
            assert rolled_back.is_set()
            assert store.event_history._worker.done()
            await store.event_history.close()
            async with store.database.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM cancellation_probe")
                    )
                    == 0
                )
            assert store.database.pool_snapshot()["checked_out"] == 0
        finally:
            await store.database.dispose()

    (await exercise())


async def test_cli_callback_failure_resets_ownership_and_disposes_database(tmp_path):
    events = []

    @_async_command
    async def command():
        database = Database(tmp_path / "cli.db")
        await database.initialize()
        dispose = database.dispose

        async def observed_disposal():
            await dispose()
            events.append("disposed")

        database.dispose = observed_disposal
        store = SimpleNamespace(
            database=database,
            event_history=SimpleNamespace(close=_resource(events, "history")),
        )
        _cli_stores.get().append(store)

        async def failed(_future):
            raise ValueError("callback failure")

        async_done_callback(failed)(asyncio.get_running_loop().create_future())

    with pytest.raises(ValueError, match="callback failure"):
        await asyncio.to_thread(command)
    assert events == ["history", "disposed"]
    assert _cli_stores.get() is None


def test_auxiliary_pool_consumes_one_declared_other_connection():
    settings = PoolSettings(5, 12, 2, requested_size=20)
    auxiliary = settings.auxiliary()
    assert auxiliary.requested_size == 1
    assert auxiliary.other_connections == settings.other_connections
    assert auxiliary.peak_processes == settings.peak_processes
    with pytest.raises(ValueError, match="OTHER_CONNECTIONS"):
        PoolSettings(5, 0, 2).auxiliary()


pytestmark = pytest.mark.anyio


async def test_core_command_selects_server_loop_before_creating_async_resources(
    monkeypatch,
):
    import importlib.util
    import uvicorn

    stages = []
    configure = uvicorn.Config.setup_event_loop
    policy = asyncio.get_event_loop_policy()

    def observed(config):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        stages.append("configured")
        configure(config)

    monkeypatch.setattr(uvicorn.Config, "setup_event_loop", observed)

    @_async_command(server_loop=True)
    async def command():
        stages.append("running")
        return type(asyncio.get_running_loop()).__module__

    try:
        module = await asyncio.to_thread(command)
        assert stages == ["configured", "running"]
        if importlib.util.find_spec("uvloop") is not None:
            assert module == "uvloop"
    finally:
        asyncio.set_event_loop_policy(policy)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_registry_callback_returning_its_task_creates_no_new_work(outcome):
    loop = asyncio.get_running_loop()
    unhandled = []
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    registry = {}

    async def operation():
        if outcome == "failure":
            raise ValueError("original task failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError

    try:
        task = asyncio.create_task(operation())
        registry["work"] = task
        task.add_done_callback(async_done_callback(lambda _done: registry.pop("work")))
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if outcome == "failure":
            assert isinstance(result, ValueError)
        if outcome == "cancelled":
            assert isinstance(result, asyncio.CancelledError)
        await close_store_resources([])
        assert registry == {}
        assert unhandled == []
    finally:
        loop.set_exception_handler(handler)


@pytest.mark.parametrize("result_kind", ["task", "future"])
async def test_callback_awaitable_persistence_finishes_before_store_disposal(
    tmp_path, result_kind
):
    store = NebulaStore(tmp_path / "callback.db")
    async with store.database.session() as session:
        await session.execute(text("CREATE TABLE callback_probe (value INTEGER)"))
    release = asyncio.Event()
    started = asyncio.Event()
    events = []
    completed = asyncio.get_running_loop().create_future()

    async def persist():
        started.set()
        await release.wait()
        async with store.database.session() as session:
            await session.execute(text("INSERT INTO callback_probe VALUES (1)"))
        events.append("persisted")
        completed.set_result(None)

    persistence = asyncio.create_task(persist())
    source = asyncio.get_running_loop().create_future()
    async_done_callback(
        lambda _done: persistence if result_kind == "task" else completed
    )(source)
    await started.wait()
    original_dispose = store.database.dispose

    async def dispose():
        async with store.database.session() as session:
            assert (
                await session.scalar(text("SELECT count(*) FROM callback_probe")) == 1
            )
        events.append("disposed")
        await original_dispose()

    store.database.dispose = dispose
    cleanup = asyncio.create_task(close_store_resources([store]))
    await asyncio.sleep(0)
    assert not cleanup.done()
    release.set()
    await cleanup
    await persistence
    assert events == ["persisted", "disposed"]
    assert store.database.pool_snapshot()["checked_out"] == 0
