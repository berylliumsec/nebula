from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
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

from nebula.v3.database import Database
from nebula.v3.event_history import prune_orphaned_event_history


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


def _exercise_event_owner_retention_cycle(database_url: str) -> None:
    """Event history can be deleted once its owner is gone, never before."""

    engine = create_engine(database_url, future=True)
    try:
        _run_migration(engine, command.upgrade, "head")
        metadata = MetaData()
        entities = Table("entities", metadata, autoload_with=engine)
        operations = Table("operation_events", metadata, autoload_with=engine)
        runs = Table("run_events", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)

        def operation(
            event_id: str, operation_id: str, kind: str, sequence: int = 1
        ) -> dict:
            return {
                "id": event_id,
                "operation_id": operation_id,
                "operation_kind": kind,
                "engagement_id": "retention-project",
                "sequence": sequence,
                "event_type": "fixture",
                "payload": {},
                "actor_id": None,
                "occurred_at": now,
                "idempotency_key": None,
            }

        def run_event(event_id: str, run_id: str, sequence: int = 1) -> dict:
            return {
                "id": event_id,
                "run_id": run_id,
                "sequence": sequence,
                "event_type": "fixture",
                "payload": {},
                "actor_id": None,
                "occurred_at": now,
                "idempotency_key": None,
            }

        def delete_event(table: Table, event_id: str) -> int:
            with engine.begin() as connection:
                return connection.execute(
                    delete(table).where(table.c.id == event_id)
                ).rowcount

        with engine.begin() as connection:
            connection.execute(
                entities.insert(),
                [
                    _legacy_row(
                        "retention-project",
                        "engagements",
                        "retention-project",
                        {"name": "Project"},
                    ),
                    _legacy_row(
                        "retention-turn",
                        "harness_turns",
                        "retention-project",
                        {"prompt": "kept"},
                    ),
                ],
            )
            connection.execute(
                operations.insert(),
                [
                    operation("live", "retention-turn", "harness_turn"),
                    operation("orphan", "deleted-turn", "harness_turn"),
                    operation("terminal", "terminal-1", "container_terminal"),
                ],
            )
            connection.execute(
                runs.insert(),
                [
                    run_event("live-run", "retention-turn"),
                    run_event("orphan-run", "deleted-run"),
                ],
            )

        for table, event_id in (
            (operations, "live"),
            (operations, "terminal"),
            (runs, "live-run"),
        ):
            with pytest.raises(DBAPIError, match="while their record exists"):
                delete_event(table, event_id)
        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(
                    update(operations)
                    .where(operations.c.id == "orphan")
                    .values(event_type="rewritten")
                )
        assert delete_event(operations, "orphan") == 1
        assert delete_event(runs, "orphan-run") == 1

        # The downgrade restores the unconditional refusal.
        _run_migration(engine, command.downgrade, "0017_chat_throughput_foundation")
        with engine.begin() as connection:
            connection.execute(
                operations.insert(),
                [operation("orphan-2", "deleted-turn", "harness_turn")],
            )
            connection.execute(
                runs.insert(), [run_event("orphan-run-2", "deleted-run")]
            )
        with pytest.raises(DBAPIError, match="immutable"):
            delete_event(operations, "orphan-2")
        with pytest.raises(DBAPIError, match="append-only"):
            delete_event(runs, "orphan-run-2")

        _run_migration(engine, command.upgrade, "head")
        assert delete_event(operations, "orphan-2") == 1
        assert delete_event(runs, "orphan-run-2") == 1

        # The batched prune removes exactly that history on this database.
        with engine.begin() as connection:
            connection.execute(
                operations.insert(),
                [
                    operation("orphan-3", "deleted-turn", "harness_turn", 1),
                    operation("orphan-4", "deleted-turn", "harness_turn", 2),
                ],
            )
            connection.execute(
                runs.insert(), [run_event("orphan-run-3", "deleted-run")]
            )
        database = Database(database_url, bootstrap=False)
        try:
            report = prune_orphaned_event_history(
                database, batch_size=1, min_age=timedelta(0)
            )
        finally:
            database.dispose()
        assert (report.operation_events, report.run_events, report.batches) == (2, 1, 3)
        with engine.connect() as connection:
            assert sorted(connection.execute(select(operations.c.id)).scalars()) == [
                "live",
                "terminal",
            ]
            assert list(connection.execute(select(runs.c.id)).scalars()) == ["live-run"]
        # Once the project and its records are gone, so may all their history.
        with engine.begin() as connection:
            connection.execute(
                delete(entities).where(
                    entities.c.id.in_(["retention-project", "retention-turn"])
                )
            )
        assert delete_event(operations, "live") == 1
        assert delete_event(operations, "terminal") == 1
        assert delete_event(runs, "live-run") == 1
    finally:
        engine.dispose()


def test_sqlite_upgrade_downgrade_and_immutable_operation_events(tmp_path):
    _exercise_migration_cycle(f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}")


def test_sqlite_event_history_is_deletable_only_once_its_owner_is_gone(tmp_path):
    _exercise_event_owner_retention_cycle(
        f"sqlite+pysqlite:///{tmp_path / 'retention.db'}"
    )


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
    _exercise_event_owner_retention_cycle(database_url)
