from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    MetaData,
    String,
    create_engine,
    inspect,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from nebula.v3.database import Database
from nebula.v3.database_migration import (
    MigrationError,
    inspect_sqlite_source,
    migrate_sqlite_to_postgresql,
)
from nebula.v3 import database_migration as migration


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "core.db"
    database = Database(path)
    metadata = MetaData()
    metadata.reflect(bind=database.engine)
    now = datetime(2026, 11, 5, 11, 30, 12, 345678, tzinfo=timezone.utc)
    with database.engine.begin() as connection:
        entities = metadata.tables["entities"]
        for entity_id in ("project", "Zebra", "alpha", "éclair"):
            connection.execute(
                entities.insert().values(
                    id=entity_id,
                    kind="engagements",
                    engagement_id=None,
                    revision=7,
                    payload={"name": entity_id, "nested": [None, True, 3.125, "☃\n\t"]},
                    created_at=now,
                    updated_at=now,
                )
            )
        for table in metadata.sorted_tables:
            if table.name in {
                "entities",
                "schema_versions",
                "bootstrap_state",
                "alembic_version",
            }:
                continue
            values = {}
            for column in table.columns:
                if column.foreign_keys:
                    values[column.name] = "project"
                elif column.nullable:
                    values[column.name] = None
                elif isinstance(column.type, JSON):
                    values[column.name] = {
                        "unicode": "café ☃",
                        "values": [None, False, 4, 2.5],
                    }
                elif isinstance(column.type, DateTime):
                    values[column.name] = now
                elif isinstance(column.type, Boolean):
                    values[column.name] = True
                elif isinstance(column.type, Integer):
                    values[column.name] = 1
                elif isinstance(column.type, String):
                    values[column.name] = "value"[: column.type.length or 5]
                else:
                    raise AssertionError(
                        f"Add synthetic seed for {table.name}.{column.name}"
                    )
            connection.execute(table.insert().values(**values))
        # JSON null must remain distinct from SQL NULL. A timestamp with an
        # explicit offset must preserve its instant across backend adapters.
        connection.exec_driver_sql(
            "INSERT INTO entities (id, kind, revision, payload, created_at, updated_at) "
            "VALUES ('json-null', 'engagements', 1, 'null', "
            "'2026-11-05 17:00:00.000001+05:30', '2026-11-05 11:30:00.000001+00:00')"
        )
    database.engine.dispose()
    # Database's existing URL normalizer treats ? as a URL query. Rename only
    # after seeding so this fixture exercises the migrator's literal-path URI.
    migrated_path = tmp_path / "core ? unicode-é.db"
    path.rename(migrated_path)
    return migrated_path


@pytest.fixture
def target_url():
    base = os.environ.get("NEBULA_TEST_POSTGRES_URL")
    if not base:
        pytest.skip(
            "NEBULA_TEST_POSTGRES_URL is required for PostgreSQL migration coverage"
        )
    url = make_url(base).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    schema = f"nebula_migration_{uuid4().hex}"
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    target = url.set(query={**url.query, "options": f"-csearch_path={schema}"})
    try:
        yield target.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


def test_inspection_preserves_source_and_lists_every_schema_table(source: Path):
    identity = migration._file_identity(source)
    result = inspect_sqlite_source(source)
    names = {item["table"] for item in result["tables"]}
    assert {
        "application_graphs",
        "application_graph_edits",
        "terminal_command_records",
        "entities",
    } <= names
    assert (
        next(item["rows"] for item in result["tables"] if item["table"] == "entities")
        == 5
    )
    assert result["requires_stopped_core"] is True
    assert migration._file_identity(source) == identity


def test_missing_source_is_not_created(tmp_path: Path):
    source = tmp_path / "missing.db"
    with pytest.raises(MigrationError, match="does not exist"):
        inspect_sqlite_source(source)
    assert not source.exists()


def test_old_source_schema_is_refused_without_upgrade(source: Path):
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'old-revision'")
    with pytest.raises(MigrationError, match="does not match"):
        inspect_sqlite_source(source)
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "old-revision"
        )


def test_migration_requires_explicit_stopped_core_confirmation(source: Path):
    with pytest.raises(MigrationError, match="Stop Core"):
        migrate_sqlite_to_postgresql(source, "postgresql+psycopg:///unused")


@pytest.mark.parametrize("batch_size", [0, 10_001])
def test_migration_refuses_unbounded_or_invalid_batches(source: Path, batch_size: int):
    with pytest.raises(MigrationError, match="batch size"):
        migrate_sqlite_to_postgresql(
            source,
            "postgresql+psycopg:///unused",
            batch_size=batch_size,
            confirm_core_stopped=True,
        )


def test_migration_never_accepts_a_sqlite_target(source: Path, tmp_path: Path):
    target = tmp_path / "target.db"
    with pytest.raises(MigrationError, match="target must use"):
        migrate_sqlite_to_postgresql(
            source, f"sqlite:///{target}", confirm_core_stopped=True
        )
    assert not target.exists()


def test_full_schema_copy_verifies_content_and_keeps_event_guards(
    source: Path, target_url: str
):
    original = migration._file_identity(source)
    progress = []
    report = migrate_sqlite_to_postgresql(
        source,
        target_url,
        batch_size=2,
        confirm_core_stopped=True,
        progress=progress.append,
    )
    assert report.verified
    assert migration._file_identity(source) == original
    assert all(len(item.sha256) == 64 for item in report.tables)
    assert {item.table for item in report.tables} == {
        item["table"] for item in inspect_sqlite_source(source)["tables"]
    }
    assert any(item["phase"] == "verify" and item["rows"] == 2 for item in progress)
    assert "target_url" not in report.as_dict()
    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT payload::text FROM entities WHERE id = 'json-null'"
                ).scalar_one()
                == "null"
            )
            assert connection.exec_driver_sql(
                "SELECT created_at FROM entities WHERE id = 'json-null'"
            ).scalar_one() == datetime(2026, 11, 5, 11, 30, 0, 1, tzinfo=timezone.utc)
            assert (
                connection.exec_driver_sql(
                    "SELECT revision FROM entities WHERE id = 'project'"
                ).scalar_one()
                == 7
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM application_graph_edits"
                ).scalar_one()
                == 1
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM terminal_command_records"
                ).scalar_one()
                == 1
            )
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE run_events SET event_type = 'tampered'"
                )
        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE operation_events SET event_type = 'tampered'"
                )
    finally:
        engine.dispose()


def test_existing_target_is_never_overwritten(source: Path, target_url: str):
    engine = create_engine(target_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE existing (value text)")
        connection.exec_driver_sql("INSERT INTO existing VALUES ('keep')")
    with pytest.raises(MigrationError, match="must be empty"):
        migrate_sqlite_to_postgresql(source, target_url, confirm_core_stopped=True)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT value FROM existing").scalar_one()
            == "keep"
        )
    engine.dispose()


def test_interrupted_copy_rolls_back_whole_schema_and_can_retry(
    source: Path, target_url: str
):
    def interrupt(event):
        if event["phase"] == "copied" and event["table"] == "entities":
            raise RuntimeError("synthetic failure")

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_sqlite_to_postgresql(
            source, target_url, confirm_core_stopped=True, progress=interrupt
        )
    engine = create_engine(target_url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()
    assert migrate_sqlite_to_postgresql(
        source, target_url, confirm_core_stopped=True
    ).verified


def test_source_writer_drift_refuses_commit(source: Path, target_url: str):
    written = False

    def write_source(event):
        nonlocal written
        if event["phase"] == "copied" and event["table"] == "entities" and not written:
            written = True
            with sqlite3.connect(source, timeout=1) as writer:
                writer.execute(
                    "UPDATE entities SET payload = ? WHERE id = 'alpha'",
                    (json.dumps({"changed": True}),),
                )

    with pytest.raises(MigrationError, match="source changed"):
        migrate_sqlite_to_postgresql(
            source, target_url, confirm_core_stopped=True, progress=write_source
        )
    engine = create_engine(target_url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_additional_source_table_refuses_silent_data_loss(
    source: Path, target_url: str
):
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE extra_user_data (id TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute("INSERT INTO extra_user_data VALUES ('extra', 'keep')")
    with pytest.raises(MigrationError, match="additional tables"):
        migrate_sqlite_to_postgresql(source, target_url, confirm_core_stopped=True)
    engine = create_engine(target_url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_content_mismatch_refuses_commit(source: Path, target_url: str, monkeypatch):
    original_copy = migration._copy_table

    def corrupted_digest(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        return (
            replace(result, sha256="0" * 64) if result.table == "entities" else result
        )

    monkeypatch.setattr(migration, "_copy_table", corrupted_digest)
    with pytest.raises(MigrationError, match="Content verification failed"):
        migrate_sqlite_to_postgresql(source, target_url, confirm_core_stopped=True)
    engine = create_engine(target_url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


@pytest.mark.parametrize("payload", [{"nul": "\x00"}, {"nonfinite": float("nan")}])
def test_unsupported_json_refuses_commit(source: Path, target_url: str, payload):
    encoded = json.dumps(payload)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE entities SET payload = ? WHERE id = 'alpha'", (encoded,)
        )
    with pytest.raises(MigrationError):
        migrate_sqlite_to_postgresql(source, target_url, confirm_core_stopped=True)
    engine = create_engine(target_url)
    assert inspect(engine).get_table_names() == []
    engine.dispose()
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute(
                "SELECT payload FROM entities WHERE id = 'alpha'"
            ).fetchone()[0]
            == encoded
        )
