"""Task ownership, bounded acquisition, and cancellation of async Core storage."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings, connection_budget


def test_connection_budget_charges_reserves_consumers_and_rollout_overlap():
    budget = connection_budget(
        PoolSettings(5, 12, 2), max_connections=100, postgres_reserved=3
    )
    assert budget.ceiling == budget.pool_size == 40
    assert budget.pool_size * budget.peak_processes + 3 + 5 + 12 <= 100


@pytest.mark.parametrize(
    "role,database,expected", [(30, -1, 6), (-1, 40, 11), (30, 40, 6)]
)
def test_connection_budget_honors_role_and_database_limits(role, database, expected):
    assert (
        connection_budget(
            PoolSettings(5, 12, 2),
            max_connections=100,
            postgres_reserved=3,
            role_limit=role,
            database_limit=database,
        ).ceiling
        == expected
    )


@pytest.mark.parametrize("size", [0, -1, 41])
def test_connection_budget_refuses_unbounded_or_oversized_pool(size):
    with pytest.raises(ValueError):
        connection_budget(
            PoolSettings(5, 12, 2, size), max_connections=100, postgres_reserved=3
        )


def test_connection_budget_refuses_no_remaining_capacity():
    with pytest.raises(ValueError):
        connection_budget(
            PoolSettings(5, 12, 2), max_connections=20, postgres_reserved=3
        )


def test_postgres_requires_explicit_deployment_budgets():
    with pytest.raises(ValueError, match="ADMIN_RESERVE"):
        PoolSettings.from_environment({})
    settings = PoolSettings.from_environment(
        {
            "NEBULA_V3_DB_ADMIN_RESERVE": "5",
            "NEBULA_V3_DB_OTHER_CONNECTIONS": "12",
            "NEBULA_V3_DB_PEAK_PROCESSES": "2",
        }
    )
    assert settings == PoolSettings(5, 12, 2)


@pytest.mark.parametrize("timeout", ["0", "-1", "inf", "nan"])
def test_pool_timeout_must_be_finite(timeout):
    with pytest.raises(ValueError):
        PoolSettings.from_environment({"NEBULA_V3_DB_POOL_TIMEOUT": timeout})


def test_async_bootstrap_health_reopen_and_task_owned_sessions(tmp_path):
    async def exercise():
        db = Database(tmp_path / "async.db")
        try:
            await db.initialize()
            seen = []

            async def read():
                async with db.session() as session:
                    assert isinstance(session, AsyncSession)
                    seen.append(session)
                    return await session.scalar(text("SELECT 1"))

            assert await asyncio.gather(*(read() for _ in range(12))) == [1] * 12
            assert len({id(x) for x in seen}) == 12
            assert (await db.health())["journal_mode"] == "wal"
            assert db.pool_snapshot()["checked_out"] == 0
            assert db.pool_snapshot()["peak_checked_out"] <= 5
        finally:
            await db.dispose()
        reopened = Database(tmp_path / "async.db")
        try:
            assert (await reopened.health())["schema_version"] == 5
        finally:
            await reopened.dispose()

    asyncio.run(exercise())


def test_cancellation_rolls_back_and_releases_connection(tmp_path):
    async def exercise():
        db = Database(tmp_path / "cancel.db")
        entered = asyncio.Event()
        try:

            async def write():
                async with db.session() as session:
                    await session.execute(
                        text(
                            "INSERT INTO bootstrap_state (key,status,created_at) VALUES ('cancelled','pending',CURRENT_TIMESTAMP)"
                        )
                    )
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(write())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with db.session() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM bootstrap_state WHERE key='cancelled'"
                        )
                    )
                    == 0
                )
            assert db.pool_snapshot()["checked_out"] == 0
        finally:
            await db.dispose()

    asyncio.run(exercise())


def test_full_pool_waits_asynchronously_and_timeout_does_not_leak(tmp_path):
    async def exercise():
        db = Database(
            tmp_path / "pool.db", pool_settings=PoolSettings(1, 0, 1, timeout=0.05)
        )
        release = asyncio.Event()
        ready = asyncio.Queue()

        async def hold():
            async with db.session():
                ready.put_nowait(True)
                await release.wait()

        tasks = []
        try:
            await db.initialize()
            tasks = [asyncio.create_task(hold()) for _ in range(5)]
            for _ in tasks:
                await asyncio.wait_for(ready.get(), 5)
            ticks = 0

            async def heartbeat():
                nonlocal ticks
                for _ in range(8):
                    await asyncio.sleep(0.01)
                    ticks += 1

            async def blocked():
                with pytest.raises(PoolTimeoutError):
                    async with db.session():
                        pytest.fail("a sixth connection exceeded the bound")

            await asyncio.gather(heartbeat(), blocked())
            assert ticks == 8
            assert db.pool_snapshot()["timeouts"] == 1
            assert db.pool_snapshot()["peak_checked_out"] == 5
        finally:
            release.set()
            await asyncio.gather(*tasks)
            assert db.pool_snapshot()["checked_out"] == 0
            assert db.pool_snapshot()["waiters"] == 0
            await db.dispose()

    asyncio.run(exercise())


def test_one_connection_bulk_read_admission_is_shared_and_nestable():
    from nebula.v3.domain import Engagement
    from nebula.v3.read_workers import read_in_worker
    from nebula.v3.storage import NebulaStore

    async def exercise():
        database = Database(":memory:")
        store = NebulaStore(database)
        await database.initialize()
        try:
            async with database.bulk():
                async with database.bulk():
                    await store.create(Engagement(name="One connection"))
            async with store.transaction(immediate=True):
                assert len(await store.list_entities(Engagement)) == 1

            class Reader:
                def __init__(self):
                    self.store = store
                    self.active = 0
                    self.peak = 0

                async def read(self):
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    try:
                        await asyncio.sleep(0.01)
                        return await self.store.list_entities(Engagement)
                    finally:
                        self.active -= 1

            reader = Reader()
            pages = await asyncio.gather(
                *(read_in_worker(reader.read) for _ in range(6))
            )
            assert all(len(page) == 1 for page in pages)
            assert reader.peak == 1
            assert database.pool_snapshot()["checked_out"] == 0
        finally:
            await store.event_history.close()
            await database.dispose()

    asyncio.run(exercise())
