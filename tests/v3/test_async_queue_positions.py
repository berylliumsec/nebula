"""Queue display polling shares reads without caching admission authority."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event

from nebula.v3.database import ProviderTurnQueueRow
from nebula.v3.domain import utc_now
from nebula.v3.provider_scheduler import ProviderScheduler
from nebula.v3.storage import NebulaStore


async def _queue(store, ids):
    now = utc_now()
    async with store.database.session() as session:
        for i, key in enumerate(ids):
            session.add(
                ProviderTurnQueueRow(
                    turn_id=key,
                    state="queued",
                    lane="direct",
                    accepted_at=now + timedelta(seconds=i),
                )
            )


async def test_waiters_share_one_position_read_and_cancel_invalidates(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "queue.db")
        scheduler = ProviderScheduler(store, worker_id="synthetic")
        ids = [f"turn-{i}" for i in range(30)]
        await _queue(store, ids)
        reads = []

        def capture(_connection, _cursor, sql, *_args):
            if sql.lstrip().startswith("SELECT") and "provider_turn_queue" in sql:
                reads.append(sql)

        event.listen(
            store.database.engine.sync_engine, "before_cursor_execute", capture
        )
        try:
            assert await asyncio.gather(
                *(scheduler.position(key) for key in ids)
            ) == list(range(1, 31))
            assert len(reads) == 1
            assert await scheduler.position(ids[-1]) == 30
            assert len(reads) == 1
            await scheduler.cancel(ids[0])
            assert await scheduler.position(ids[0]) is None
            assert await scheduler.position(ids[-1]) == 29
            assert sum("ORDER BY" in sql for sql in reads) == 2
        finally:
            await store.database.dispose()

    (await exercise())


async def test_other_process_updates_expire_without_affecting_admission(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "queue.db")
        first = ProviderScheduler(store, worker_id="first")
        second = ProviderScheduler(store, worker_id="second")
        await _queue(store, ["a", "b"])
        try:
            assert await first.position("b") == 2
            await second.cancel("a")
            await asyncio.sleep(0.55)
            assert await first.position("a") is None
            assert await first.position("b") == 1
            assert not first.admitted("b")
        finally:
            await store.database.dispose()

    (await exercise())


async def test_uncommitted_position_is_never_shared_between_tasks(tmp_path):
    async def exercise():
        store = NebulaStore(tmp_path / "queue.db")
        scheduler = ProviderScheduler(store, worker_id="synthetic")
        await _queue(store, ["a"])
        try:
            assert await scheduler.position("a") == 1
            with pytest.raises(ValueError, match="rollback"):
                async with store.database.session() as session:
                    session.add(
                        ProviderTurnQueueRow(
                            turn_id="uncommitted",
                            state="queued",
                            lane="direct",
                            accepted_at=utc_now() + timedelta(seconds=1),
                        )
                    )
                    await session.flush()
                    assert await scheduler.position("uncommitted") == 2
                    other = asyncio.create_task(scheduler.position("uncommitted"))
                    assert await other is None
                    raise ValueError("rollback")
            assert await scheduler.position("uncommitted") is None
        finally:
            await store.database.dispose()

    (await exercise())


pytestmark = pytest.mark.anyio
