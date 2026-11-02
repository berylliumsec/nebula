"""Upgrade legacy envelopes atomically and keep all authoritative data intact."""

from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, event, inspect

from tests.v3.test_migrations import _run_migration

PREVIOUS = "0023_workspace_provenance_ledger"
CURRENT = "0024_entity_hot_lookup_indexes"
COLUMNS = {"artifact_tool_call_id", "tool_call_status", "tool_call_origin"}
INDEXES = {
    "ix_entities_kind_engagement_created_id",
    "ix_entities_artifact_tool_call_created",
    "ix_entities_tool_call_recovery_created",
}


def _legacy_database(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _run_migration(engine, command.upgrade, PREVIOUS)
    table = Table("entities", MetaData(), autoload_with=engine)
    envelopes = [
        ("artifact", "artifacts", {"metadata": {"tool_call_id": "call-1"}}),
        ("empty", "artifacts", {"metadata": {"tool_call_id": ""}}),
        ("missing", "artifacts", {}),
        ("null", "artifacts", {"metadata": None}),
        ("numeric", "artifacts", {"metadata": {"tool_call_id": 12}}),
        ("array", "artifacts", {"metadata": {"tool_call_id": ["call-1"]}}),
        ("call", "tool_calls", {"status": "approved", "origin": "chat"}),
        ("old-call", "tool_calls", {"status": "running"}),
        ("bad-call", "tool_calls", {"status": 12, "origin": None}),
        (
            "other",
            "assets",
            {
                "status": "approved",
                "origin": "chat",
                "metadata": {"tool_call_id": "call-1"},
            },
        ),
    ]
    stamp = datetime(2026, 11, 2, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "id": key,
                    "kind": kind,
                    "engagement_id": "project",
                    "revision": 7,
                    "payload": payload,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                for key, kind, payload in envelopes
            ],
        )
    return engine


def _envelopes(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT id,kind,engagement_id,revision,payload,created_at,updated_at FROM entities ORDER BY id"
        ).all()


def test_hot_lookup_upgrade_backfills_without_changing_legacy_envelopes(tmp_path):
    engine = _legacy_database(tmp_path)
    original = _envelopes(engine)
    try:
        for _ in range(2):
            _run_migration(engine, command.upgrade, CURRENT)
            assert _envelopes(engine) == original
            assert INDEXES <= {
                index["name"] for index in inspect(engine).get_indexes("entities")
            }
            with engine.connect() as connection:
                projected = {
                    row[0]: row[1:]
                    for row in connection.exec_driver_sql(
                        "SELECT id,artifact_tool_call_id,tool_call_status,tool_call_origin FROM entities"
                    )
                }
            assert projected["artifact"] == ("call-1", None, None)
            assert projected["empty"] == ("", None, None)
            assert projected["call"] == (None, "approved", "chat")
            assert projected["old-call"] == (None, "running", None)
            for key in ("missing", "null", "numeric", "array", "bad-call", "other"):
                assert projected[key] == (None, None, None)
            _run_migration(engine, command.upgrade, CURRENT)  # repeated startup
            _run_migration(engine, command.downgrade, PREVIOUS)
            assert not COLUMNS & {
                c["name"] for c in inspect(engine).get_columns("entities")
            }
            assert _envelopes(engine) == original
    finally:
        engine.dispose()


def test_failed_hot_lookup_migration_rolls_back_and_can_retry(tmp_path):
    engine = _legacy_database(tmp_path)
    original = _envelopes(engine)

    def fail_index(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE INDEX ix_entities_artifact_tool_call_created"):
            raise RuntimeError("synthetic index failure")

    try:
        event.listen(engine, "before_cursor_execute", fail_index)
        with pytest.raises(RuntimeError, match="synthetic index failure"):
            _run_migration(engine, command.upgrade, CURRENT)
        event.remove(engine, "before_cursor_execute", fail_index)
        assert _envelopes(engine) == original
        assert not COLUMNS & {
            c["name"] for c in inspect(engine).get_columns("entities")
        }
        assert not INDEXES & {
            i["name"] for i in inspect(engine).get_indexes("entities")
        }
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == PREVIOUS
            )
        _run_migration(engine, command.upgrade, CURRENT)
        assert _envelopes(engine) == original
    finally:
        engine.dispose()
