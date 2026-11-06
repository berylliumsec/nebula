"""Native async storage behavior against isolated PostgreSQL schemas."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.storage import NebulaStore
from tests.v3 import test_async_storage as storage_cases


@pytest.fixture
def postgresql_url():
    configured = os.getenv("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip(
            "NEBULA_TEST_POSTGRES_URL is required for native PostgreSQL coverage"
        )
    url = make_url(Database._normalize_sync_url(configured))
    schema = "nebula_async_" + uuid4().hex
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=10)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine.dispose()
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.mark.parametrize(
    "case",
    [
        storage_cases.test_async_crud_revision_checks_and_delete,
        storage_cases.test_async_transaction_rolls_back_every_entity,
        storage_cases.test_async_ledger_initializes_first_and_deduplicates,
        storage_cases.test_async_ledger_serializes_concurrent_tasks,
        storage_cases.test_async_harness_titles_seek_requested_sessions,
    ],
    ids=["crud", "rollback", "ledger-retry", "concurrent-ledger", "harness-titles"],
)
def test_postgresql_storage_contracts(postgresql_url, tmp_path, monkeypatch, case):
    @asynccontextmanager
    async def store_factory(_):
        store = NebulaStore(
            Database(postgresql_url, pool_settings=PoolSettings(5, 12, 2))
        )
        try:
            yield store
        finally:
            await store.event_history.close()
            await store.database.dispose()

    monkeypatch.setattr(storage_cases, "open_store", store_factory)
    case(tmp_path)


def test_postgresql_budget_discovery_and_slow_query_leave_loop_responsive(
    postgresql_url,
):
    async def exercise():
        database = Database(postgresql_url, pool_settings=PoolSettings(5, 12, 2))
        try:
            await database.initialize()
            budget = database.pool_snapshot()["budget"]
            assert (
                budget["ceiling"]
                == (budget["max_connections"] - budget["postgres_reserved"] - 5 - 12)
                // 2
            )
            assert database.engine.pool._max_overflow == 0
            ticks = 0

            async def heartbeat():
                nonlocal ticks
                for _ in range(8):
                    await asyncio.sleep(0.01)
                    ticks += 1

            async def slow_query():
                async with database.session() as session:
                    await session.execute(text("SELECT pg_sleep(0.12)"))
                    assert ticks >= 5

            await asyncio.gather(heartbeat(), slow_query())
            assert database.pool_snapshot()["checked_out"] == 0
        finally:
            await database.dispose()

    asyncio.run(exercise())
