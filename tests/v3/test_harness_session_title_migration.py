"""Legacy harness-session links are indexed without changing saved envelopes."""

from datetime import datetime, timezone

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect

from tests.v3.test_migrations import _run_migration

PREVIOUS = "0027_chat_turn_status_lookup"
CURRENT = "0028_harness_session_title_lookup"
INDEX = "ix_entities_kind_harness_session_created"


def test_harness_session_title_migration_backfills_and_roundtrips(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _run_migration(engine, command.upgrade, PREVIOUS)
    table = Table("entities", MetaData(), autoload_with=engine)
    stamp = datetime(2026, 11, 5, tzinfo=timezone.utc)
    fixtures = [
        ("chat", "chat_sessions", {"harness_session_id": "session-1"}),
        ("run", "runs", {"harness_session_id": "session-1"}),
        ("turn", "harness_turns", {"harness_session_id": "session-1"}),
        ("number", "harness_turns", {"harness_session_id": 7}),
        ("missing", "harness_turns", {}),
        ("other", "assets", {"harness_session_id": "session-1"}),
    ]
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "id": key,
                    "kind": kind,
                    "engagement_id": "project",
                    "revision": 1,
                    "payload": payload,
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                for key, kind, payload in fixtures
            ],
        )
        before = connection.exec_driver_sql(
            "SELECT id,kind,payload FROM entities ORDER BY id"
        ).all()
    try:
        _run_migration(engine, command.upgrade, CURRENT)
        _run_migration(engine, command.upgrade, CURRENT)
        assert INDEX in {
            item["name"] for item in inspect(engine).get_indexes("entities")
        }
        with engine.connect() as connection:
            projected = dict(
                connection.exec_driver_sql(
                    "SELECT id,harness_session_id FROM entities"
                ).all()
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT id,kind,payload FROM entities ORDER BY id"
                ).all()
                == before
            )
        assert projected == {
            "chat": "session-1",
            "run": "session-1",
            "turn": "session-1",
            "number": None,
            "missing": None,
            "other": None,
        }
        _run_migration(engine, command.downgrade, PREVIOUS)
        assert "harness_session_id" not in {
            item["name"] for item in inspect(engine).get_columns("entities")
        }
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT id,kind,payload FROM entities ORDER BY id"
                ).all()
                == before
            )
    finally:
        engine.dispose()
