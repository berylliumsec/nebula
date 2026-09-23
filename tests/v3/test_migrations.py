from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    delete,
    event,
    inspect,
    select,
    update,
)
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
    reversible_head = "0013_application_model_outbox"
    try:
        if "alembic_version" in inspect(engine).get_table_names():
            _run_migration(engine, command.downgrade, "base")

        # The graph replacement intentionally cannot reconstruct experimental
        # records. Exercise the reversible chain independently of that boundary.
        _run_migration(engine, command.upgrade, reversible_head)
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

        _run_migration(engine, command.upgrade, reversible_head)
        assert "operation_events" in inspect(engine).get_table_names()
        _run_migration(engine, command.downgrade, "base")
        assert "operation_events" not in inspect(engine).get_table_names()
        assert "run_events" not in inspect(engine).get_table_names()

        _run_migration(engine, command.upgrade, "head")
        assert {"operation_events", "application_graphs", "session_projections"} <= set(
            inspect(engine).get_table_names()
        )
        with pytest.raises(RuntimeError, match="irreversible; restore a backup"):
            _run_migration(engine, command.downgrade, reversible_head)
        assert "operation_events" in inspect(engine).get_table_names()
        assert "application_graphs" in inspect(engine).get_table_names()
        _run_migration(engine, command.upgrade, "head")
    finally:
        engine.dispose()


def _exercise_chat_session_lookup_cycle(database_url: str) -> None:
    """Exercise the conversation projection at its reversible head boundary."""

    engine = create_engine(database_url, future=True)
    row_ids = ["postgres-chat-message", "postgres-chat-approval"]
    try:
        _run_migration(engine, command.downgrade, "0015_session_projection")
        entities = Table("entities", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                entities.insert(),
                [
                    _legacy_row(
                        row_ids[0],
                        "chat_messages",
                        "project-1",
                        {"session_id": "session-a", "content": "kept"},
                    ),
                    _legacy_row(
                        row_ids[1],
                        "approvals",
                        "project-1",
                        {"chat_session_id": "session-b", "status": "pending"},
                    ),
                ],
            )

        _run_migration(engine, command.upgrade, "head")
        projected = Table("entities", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            values = dict(
                connection.execute(
                    select(projected.c.id, projected.c.chat_session_id).where(
                        projected.c.id.in_(row_ids)
                    )
                ).all()
            )
        assert values == {
            "postgres-chat-message": "session-a",
            "postgres-chat-approval": "session-b",
        }

        _run_migration(engine, command.downgrade, "0015_session_projection")
        downgraded = Table("entities", MetaData(), autoload_with=engine)
        assert "chat_session_id" not in downgraded.c
        with engine.begin() as connection:
            payloads = dict(
                connection.execute(
                    select(downgraded.c.id, downgraded.c.payload).where(
                        downgraded.c.id.in_(row_ids)
                    )
                ).all()
            )
            connection.execute(delete(downgraded).where(downgraded.c.id.in_(row_ids)))
        assert payloads[row_ids[0]]["content"] == "kept"
        assert payloads[row_ids[1]]["chat_session_id"] == "session-b"
        _run_migration(engine, command.upgrade, "head")
    finally:
        engine.dispose()


def test_sqlite_upgrade_downgrade_and_immutable_operation_events(tmp_path):
    _exercise_migration_cycle(f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}")


def _legacy_row(entity_id: str, kind: str, project_id: str, payload: dict) -> dict:
    now = datetime.now(timezone.utc)
    envelope = {
        "id": entity_id,
        "revision": 1,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        **payload,
    }
    return {
        "id": entity_id,
        "kind": kind,
        "engagement_id": project_id,
        "revision": 1,
        "payload": envelope,
        "created_at": now,
        "updated_at": now,
    }


def test_relation_migration_backfills_legacy_arrays_and_skips_dangling_ids(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'relations.db'}")
    _run_migration(engine, command.upgrade, "0010_browser_automation_indexes")
    metadata = MetaData()
    entities = Table("entities", metadata, autoload_with=engine)
    row = _legacy_row

    with engine.begin() as connection:
        connection.execute(
            entities.insert(),
            [
                row("asset-1", "assets", "project-1", {"name": "Gateway"}),
                row(
                    "finding-1",
                    "findings",
                    "project-1",
                    {"title": "Issue", "asset_ids": ["asset-1"]},
                ),
            ],
        )
    _run_migration(engine, command.upgrade, "head")
    relations = Table("resource_relations", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        edge = connection.execute(select(relations)).mappings().one()
    assert (edge["source_kind"], edge["predicate"], edge["target_kind"]) == (
        "finding",
        "affects",
        "asset",
    )
    engine.dispose()

    dangling = create_engine(f"sqlite+pysqlite:///{tmp_path / 'dangling.db'}")
    _run_migration(dangling, command.upgrade, "0010_browser_automation_indexes")
    entities = Table("entities", MetaData(), autoload_with=dangling)
    with dangling.begin() as connection:
        connection.execute(
            entities.insert(),
            [
                row("asset-2", "assets", "project-1", {"name": "Gateway"}),
                row(
                    "finding-2",
                    "findings",
                    "project-1",
                    {"title": "Issue", "asset_ids": ["missing", "asset-2"]},
                ),
            ],
        )
    # A Finding may still name an Asset that was deleted before the upgrade.
    # The backfill skips that reference instead of wedging every later start.
    _run_migration(dangling, command.upgrade, "head")
    relations = Table("resource_relations", MetaData(), autoload_with=dangling)
    with dangling.connect() as connection:
        targets = connection.execute(select(relations.c.target_id)).scalars().all()
    assert targets == ["asset-2"]
    dangling.dispose()


def test_sqlite_migration_failure_leaves_no_partial_schema_and_can_retry(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'atomic.db'}")
    _run_migration(engine, command.upgrade, "0010_browser_automation_indexes")
    entities = Table("entities", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            entities.insert(),
            [
                _legacy_row("asset-1", "assets", "project-1", {"name": "Gateway"}),
                _legacy_row(
                    "finding-1",
                    "findings",
                    "project-1",
                    {"title": "Issue", "asset_ids": ["asset-1"]},
                ),
            ],
        )

    def fail_backfill(_connection, _cursor, statement, *_rest):
        if statement.lstrip().upper().startswith("INSERT INTO RESOURCE_RELATIONS"):
            raise RuntimeError("simulated mid-migration failure")

    # Fail only after 0011 has already issued its CREATE TABLE/INDEX statements.
    event.listen(engine, "before_cursor_execute", fail_backfill)
    with pytest.raises(RuntimeError, match="simulated mid-migration failure"):
        _run_migration(engine, command.upgrade, "head")
    event.remove(engine, "before_cursor_execute", fail_backfill)

    assert "resource_relations" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert version == "0010_browser_automation_indexes"

    # Once the cause is gone the same upgrade succeeds on the untouched schema.
    _run_migration(engine, command.upgrade, "head")
    relations = Table("resource_relations", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        edge = connection.execute(select(relations)).mappings().one()
    assert (edge["source_id"], edge["target_id"]) == ("finding-1", "asset-1")
    engine.dispose()


@pytest.mark.skipif(
    not os.getenv("NEBULA_TEST_POSTGRES_URL"),
    reason="NEBULA_TEST_POSTGRES_URL is required for PostgreSQL migration coverage",
)
def test_postgresql_upgrade_downgrade_and_immutable_operation_events():
    database_url = os.environ["NEBULA_TEST_POSTGRES_URL"]
    _exercise_migration_cycle(database_url)
    _exercise_chat_session_lookup_cycle(database_url)
