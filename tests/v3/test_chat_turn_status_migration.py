"""Backfill active-turn status fields without changing legacy JSON envelopes."""

from datetime import datetime, timezone

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from tests.v3.test_migrations import _run_migration

PREVIOUS = "0026_harness_tool_call_lookup"
CURRENT = "0027_chat_turn_status_lookup"


def test_chat_turn_status_migration_backfills_and_downgrades(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    _run_migration(engine, command.upgrade, PREVIOUS)
    entities = Table("entities", MetaData(), autoload_with=engine)
    stamp = datetime(2026, 11, 4, tzinfo=timezone.utc)
    cases = [
        ("routing", "chat_turns", "routing", {}, ("routing", False, False)),
        (
            "restart",
            "chat_turns",
            "interrupted",
            {"required": True, "cause": "core_restart"},
            ("interrupted", True, True),
        ),
        (
            "attempted",
            "chat_turns",
            "interrupted",
            {
                "required": True,
                "cause": "core_shutdown",
                "auto_resume_attempted_at": "now",
            },
            ("interrupted", True, False),
        ),
        (
            "legacy-error",
            "chat_turns",
            "interrupted",
            {"required": True},
            ("interrupted", True, True),
        ),
        (
            "retry",
            "chat_turns",
            "interrupted",
            {"automatic_retry_pending": True},
            ("interrupted", True, False),
        ),
        (
            "settled",
            "chat_turns",
            "interrupted",
            {},
            ("interrupted", False, False),
        ),
        ("other", "work_items", "routing", {}, (None, None, None)),
    ]
    with engine.begin() as connection:
        connection.execute(
            entities.insert(),
            [
                {
                    "id": key,
                    "kind": kind,
                    "engagement_id": "project",
                    "revision": 3,
                    "payload": {
                        "status": status,
                        "request_snapshot": {"recovery": recovery},
                        "error": "Core restarted while executing"
                        if key == "legacy-error"
                        else None,
                        "content": "historical payload" * 100,
                    },
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                for key, kind, status, recovery, _ in cases
            ],
        )
    with engine.connect() as connection:
        original = connection.execute(
            select(entities.c.id, entities.c.revision, entities.c.payload)
        ).all()

    _run_migration(engine, command.upgrade, CURRENT)
    _run_migration(engine, command.upgrade, CURRENT)
    assert {
        "ix_entities_chat_turn_status",
        "ix_entities_project_chat_turn_status",
    } <= {index["name"] for index in inspect(engine).get_indexes("entities")}
    projected = Table("entities", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(projected.c.id, projected.c.revision, projected.c.payload)
            ).all()
            == original
        )
        actual = {
            row[0]: row[1:]
            for row in connection.execute(
                select(
                    projected.c.id,
                    projected.c.chat_turn_status,
                    projected.c.chat_turn_recovery_visible,
                    projected.c.chat_turn_core_resume_pending,
                )
            )
        }
    assert actual == {key: expected for key, *_, expected in cases}

    _run_migration(engine, command.downgrade, PREVIOUS)
    downgraded = Table("entities", MetaData(), autoload_with=engine)
    assert "chat_turn_status" not in downgraded.c
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(downgraded.c.id, downgraded.c.revision, downgraded.c.payload)
            ).all()
            == original
        )
    engine.dispose()
