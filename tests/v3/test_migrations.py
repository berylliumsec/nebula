from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, delete, inspect, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


def _run_migration(
    engine: Engine,
    operation: Callable[[Config, str], None],
    revision: str,
) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def _exercise_migration_cycle(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        if "alembic_version" in inspect(engine).get_table_names():
            _run_migration(engine, command.downgrade, "base")

        _run_migration(engine, command.upgrade, "head")
        assert "operation_events" in inspect(engine).get_table_names()

        metadata = MetaData()
        events = Table("operation_events", metadata, autoload_with=engine)
        event_id = "migration-operation-event"
        with engine.begin() as connection:
            connection.execute(
                events.insert().values(
                    id=event_id,
                    operation_id="execution-1",
                    operation_kind="operator_execution",
                    engagement_id="engagement-1",
                    sequence=1,
                    event_type="execution.queued",
                    payload={},
                    actor_id=None,
                    occurred_at=datetime.now(timezone.utc),
                    idempotency_key="queued",
                )
            )

        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(
                    update(events)
                    .where(events.c.id == event_id)
                    .values(event_type="rewritten")
                )
        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(delete(events).where(events.c.id == event_id))

        _run_migration(engine, command.downgrade, "0002_event_immutability")
        assert "operation_events" not in inspect(engine).get_table_names()
        if engine.dialect.name == "postgresql":
            with engine.connect() as connection:
                remaining = connection.exec_driver_sql(
                    "SELECT count(*) FROM pg_proc "
                    "WHERE proname='nebula_reject_operation_event_mutation'"
                ).scalar_one()
            assert remaining == 0

        _run_migration(engine, command.upgrade, "head")
        assert "operation_events" in inspect(engine).get_table_names()
        _run_migration(engine, command.downgrade, "base")
        assert "operation_events" not in inspect(engine).get_table_names()
        assert "run_events" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_sqlite_upgrade_downgrade_and_immutable_operation_events(tmp_path):
    _exercise_migration_cycle(f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}")


@pytest.mark.skipif(
    not os.getenv("NEBULA_TEST_POSTGRES_URL"),
    reason="NEBULA_TEST_POSTGRES_URL is required for PostgreSQL migration coverage",
)
def test_postgresql_upgrade_downgrade_and_immutable_operation_events():
    _exercise_migration_cycle(os.environ["NEBULA_TEST_POSTGRES_URL"])
