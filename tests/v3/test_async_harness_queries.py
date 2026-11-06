"""Prove title lookups stop at indexed rows as matching history grows."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

from sqlalchemy import event, insert

from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    HarnessTurn,
    HarnessTurnOrigin,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_async_postgresql import postgresql_url as postgresql_url


def _row(entity):
    return dict(
        id=entity.id,
        kind=entity.entity_kind,
        engagement_id=entity.engagement_id,
        revision=entity.revision,
        payload=entity.model_dump(mode="json"),
        harness_session_id=entity.harness_session_id,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


async def _measure(store, count, *, start=0):
    created = utc_now() - timedelta(days=2)
    rows = []
    # Half the growing history matches the listed session. The rest must not
    # affect the lookup either. Temporary chats deliberately sort first.
    for i in range(start, count):
        key = "listed" if i % 2 else "unrelated"
        rows.append(
            _row(
                HarnessTurn(
                    id=f"turn-{i}",
                    engagement_id="project",
                    harness_session_id=key,
                    origin=HarnessTurnOrigin.ANALYSIS,
                    prompt=f"turn {i}",
                    created_at=created + timedelta(seconds=i),
                )
            )
        )
        rows.append(
            _row(
                ChatSession(
                    id=f"temporary-{i}",
                    engagement_id="project",
                    backend=ChatBackend.HARNESS,
                    harness_profile_id="profile",
                    harness_session_id=key,
                    model="fixture",
                    title="Excluded",
                    metadata={"temporary_assistant": True},
                    created_at=created + timedelta(days=1, seconds=i),
                )
            )
        )
    async with store.database.session() as session:
        for offset in range(0, len(rows), 500):
            await session.execute(insert(EntityRow), rows[offset : offset + 500])
        await session.execute(__import__("sqlalchemy").text("ANALYZE entities"))
    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "WITH requested_harness_sessions AS" in statement:
            statements.append((statement, parameters))

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with store.database.session() as session:
            connection = await session.connection()
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            sqlite = connection.dialect.name == "sqlite"
            raw = await connection.get_raw_connection() if sqlite else None
            if raw:
                await raw.driver_connection.set_progress_handler(tick, 1)
            try:
                titles = await store.harness_session_title_sources(
                    ["listed"], engagement_id="project"
                )
            finally:
                if raw:
                    await raw.driver_connection.set_progress_handler(None, 0)
        assert titles[0] == {"listed": "Saved title"}
        assert titles[2]["listed"].id == "turn-1"
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", capture
        )
    assert len(statements) == 3
    plans = []
    async with store.database.connection() as connection:
        for statement, parameters in statements:
            prefix = (
                "EXPLAIN QUERY PLAN "
                if sqlite
                else "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            )
            plan = (
                await connection.exec_driver_sql(prefix + statement, parameters)
            ).all()
            plans.append([list(row) for row in plan])
    return steps, plans


async def _exercise(database):
    store = NebulaStore(database)
    try:
        await store.create(
            ChatSession(
                id="saved",
                engagement_id="project",
                backend=ChatBackend.HARNESS,
                harness_profile_id="profile",
                harness_session_id="listed",
                model="fixture",
                title="Saved title",
            )
        )
        small, small_plans = await _measure(store, 20)
        large, large_plans = await _measure(store, 10000, start=20)
        description = str(large_plans)
        assert "ix_entities_harness_chat_title_project" in description, description
        assert any(
            index in description
            for index in (
                "ix_entities_harness_title_project",
                "ix_entities_kind_harness_session_created",
            )
        )
        if database.engine.dialect.name == "sqlite":
            assert large <= small * 1.5 + 100, (small, large)
            assert "USE TEMP B-TREE" not in description
        else:
            # On the large fixture every correlated seek emits at most one row.
            # Buffer reads stay bounded even when many rows share a session.
            root_plans = [plan[0][0][0]["Plan"] for plan in large_plans]
            assert (
                sum(
                    plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)
                    for plan in root_plans
                )
                < 100
            ), root_plans
        print(
            {
                "dialect": database.engine.dialect.name,
                "small_vm_steps": small,
                "large_vm_steps": large,
                "matching_turns": 5000,
                "temporary_chats": 10000,
            }
        )
    finally:
        await store.event_history.close()
        await database.dispose()


async def test_sqlite_harness_lookup_work_does_not_scan_matching_history(tmp_path):
    (await _exercise(Database(tmp_path / "seeks.db")))


async def test_postgresql_harness_lookup_work_does_not_scan_matching_history(
    postgresql_url,
):
    (await _exercise(Database(postgresql_url, pool_settings=PoolSettings(5, 12, 2))))


pytestmark = pytest.mark.anyio
