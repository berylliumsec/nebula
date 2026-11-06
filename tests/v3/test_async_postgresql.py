"""Native async storage behavior against isolated PostgreSQL schemas."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

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
from tests.v3 import test_recovery_candidate_scan as recovery_cases


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
async def test_postgresql_storage_contracts(
    postgresql_url, tmp_path, monkeypatch, case
):
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
    await case(tmp_path)


@pytest.mark.parametrize(
    "case",
    ["load", "legacy", "unreadable", "complete", "routing", "settled", "deleted"],
)
async def test_postgresql_recovery_candidate_scan(
    postgresql_url, tmp_path, monkeypatch, case
):
    @asynccontextmanager
    async def open_store(_):
        store = NebulaStore(
            Database(postgresql_url, pool_settings=PoolSettings(5, 12, 2))
        )
        try:
            yield store
        finally:
            await store.event_history.close()
            await store.database.dispose()

    monkeypatch.setattr(recovery_cases, "open_store", open_store)
    cases = {
        "load": recovery_cases.test_recovery_scan_loads_only_pending_interrupted_payloads,
        "legacy": recovery_cases.test_recovery_scan_keeps_rows_with_missing_projections,
        "unreadable": recovery_cases.test_recovery_scan_skips_unreadable_candidates,
    }
    if case in cases:
        await cases[case](tmp_path, monkeypatch)
    else:
        await recovery_cases.test_recovery_scan_rechecks_after_id_selection(
            tmp_path, monkeypatch, case
        )


async def test_postgresql_budget_discovery_and_slow_query_leave_loop_responsive(
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

    (await exercise())


pytestmark = pytest.mark.anyio


async def test_postgresql_budget_follows_login_limit_after_startup_role_switch():
    """SET ROLE cannot widen the authenticated role's connection allocation."""
    import psycopg
    from psycopg import sql

    configured = os.getenv("NEBULA_TEST_POSTGRES_ADMIN_URL")
    if not configured:
        pytest.skip(
            "Owned PostgreSQL role-budget fixture requires its explicit admin URL"
        )
    admin_url = make_url(Database._normalize_sync_url(configured)).set(
        drivername="postgresql"
    )
    suffix = uuid4().hex[:12]
    login = "nebula_budget_login_" + suffix
    effective = "nebula_budget_effective_" + suffix
    password = "synthetic-owned-role-password"
    async with await psycopg.AsyncConnection.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        created = []
        databases = []
        try:
            await admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT 3").format(
                    sql.Identifier(login), sql.Literal(password)
                )
            )
            created.append(login)
            await admin.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(effective))
            )
            created.append(effective)
            await admin.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(effective), sql.Identifier(login)
                )
            )
            runtime_url = admin_url.set(
                drivername="postgresql+psycopg",
                username=login,
                password=password,
                query={"options": "-crole=" + effective},
            ).render_as_string(hide_password=False)
            database = Database(
                runtime_url, bootstrap=False, pool_settings=PoolSettings(1, 0, 1)
            )
            databases.append(database)
            await database.initialize()
            async with database.engine.connect() as connection:
                identities = (
                    await connection.execute(text("SELECT session_user, current_user"))
                ).one()
                assert identities == (login, effective)
            assert database.budget.role_limit == 3
            assert database.budget.ceiling == database.pool_size == 2
            assert database.pool_snapshot()["max_overflow"] == 0
            oversized = Database(
                runtime_url,
                bootstrap=False,
                pool_settings=PoolSettings(1, 0, 1, requested_size=3),
            )
            databases.append(oversized)
            with pytest.raises(ValueError, match="safe ceiling 2"):
                await oversized.initialize()
        finally:
            for database in databases:
                await database.dispose()
            for role in created:
                await admin.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                )
