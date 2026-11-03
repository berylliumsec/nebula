"""The tool-turn projection backfills atomically without changing saved envelopes."""

from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, event, inspect

from tests.v3.test_migrations import _run_migration

PREVIOUS = "0025_merge_project_and_lookups"
CURRENT = "0026_harness_tool_call_lookup"
COLUMN = "tool_call_harness_turn_id"
INDEX = "ix_entities_tool_call_turn_status_created"


def _legacy(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _run_migration(engine, command.upgrade, PREVIOUS)
    table = Table("entities", MetaData(), autoload_with=engine)
    stamp = datetime(2026, 11, 3, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                dict(
                    id=key,
                    kind=kind,
                    engagement_id="project",
                    revision=7,
                    payload=payload,
                    created_at=stamp,
                    updated_at=stamp,
                )
                for key, kind, payload in [
                    ("call", "tool_calls", {"metadata": {"harness_turn_id": "turn"}}),
                    ("empty", "tool_calls", {"metadata": {"harness_turn_id": ""}}),
                    ("missing", "tool_calls", {}),
                    ("null", "tool_calls", {"metadata": None}),
                    ("number", "tool_calls", {"metadata": {"harness_turn_id": 12}}),
                    (
                        "array",
                        "tool_calls",
                        {"metadata": {"harness_turn_id": ["turn"]}},
                    ),
                    ("boolean", "tool_calls", {"metadata": {"harness_turn_id": True}}),
                    ("other", "artifacts", {"metadata": {"harness_turn_id": "turn"}}),
                ]
            ],
        )
    return engine


def _envelopes(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT id,kind,engagement_id,revision,payload,created_at,updated_at FROM entities ORDER BY id"
        ).all()


def test_tool_turn_migration_roundtrip_keeps_authoritative_records(tmp_path):
    engine = _legacy(tmp_path)
    original = _envelopes(engine)
    try:
        for _ in range(2):
            _run_migration(engine, command.upgrade, CURRENT)
            _run_migration(engine, command.upgrade, CURRENT)
            assert _envelopes(engine) == original
            assert INDEX in {i["name"] for i in inspect(engine).get_indexes("entities")}
            with engine.connect() as connection:
                projected = dict(
                    connection.exec_driver_sql(
                        f"SELECT id,{COLUMN} FROM entities"
                    ).all()
                )
            assert projected.pop("call") == "turn"
            assert projected.pop("empty") == ""
            assert set(projected.values()) == {None}
            _run_migration(engine, command.downgrade, PREVIOUS)
            assert COLUMN not in {
                c["name"] for c in inspect(engine).get_columns("entities")
            }
            assert _envelopes(engine) == original
    finally:
        engine.dispose()


def test_failed_tool_turn_index_creation_rolls_back_and_retries(tmp_path):
    engine = _legacy(tmp_path)
    original = _envelopes(engine)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"CREATE INDEX {INDEX}"):
            raise RuntimeError("synthetic index failure")

    try:
        event.listen(engine, "before_cursor_execute", fail)
        with pytest.raises(RuntimeError, match="synthetic index failure"):
            _run_migration(engine, command.upgrade, CURRENT)
        event.remove(engine, "before_cursor_execute", fail)
        assert _envelopes(engine) == original
        assert COLUMN not in {
            c["name"] for c in inspect(engine).get_columns("entities")
        }
        assert INDEX not in {i["name"] for i in inspect(engine).get_indexes("entities")}
        _run_migration(engine, command.upgrade, CURRENT)
        assert _envelopes(engine) == original
    finally:
        engine.dispose()
