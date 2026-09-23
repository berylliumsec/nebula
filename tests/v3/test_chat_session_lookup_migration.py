from __future__ import annotations

from datetime import datetime, timezone

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from tests.v3.test_migrations import _run_migration


def test_chat_session_lookup_backfills_and_downgrades_without_touching_payloads(
    tmp_path,
):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'chat-session-lookup.db'}", future=True
    )
    _run_migration(engine, command.upgrade, "0015_session_projection")
    metadata = MetaData()
    entities = Table("entities", metadata, autoload_with=engine)
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": "message-1",
            "kind": "chat_messages",
            "engagement_id": "project",
            "revision": 1,
            "payload": {"session_id": "session-a", "content": "kept"},
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "approval-1",
            "kind": "approvals",
            "engagement_id": "project",
            "revision": 1,
            "payload": {"chat_session_id": "session-b", "status": "pending"},
            "created_at": now,
            "updated_at": now,
        },
        {
            "id": "peer-message-1",
            "kind": "chat_agent_messages",
            "engagement_id": "project",
            "revision": 1,
            "payload": {
                "sender_session_id": "session-a",
                "recipient_session_id": "session-b",
            },
            "created_at": now,
            "updated_at": now,
        },
    ]
    with engine.begin() as connection:
        connection.execute(entities.insert(), rows)

    _run_migration(engine, command.upgrade, "0016_chat_session_lookup")
    inspector = inspect(engine)
    assert "chat_session_id" in {
        column["name"] for column in inspector.get_columns("entities")
    }
    assert "ix_entities_kind_chat_session_created" in {
        index["name"] for index in inspector.get_indexes("entities")
    }
    projected = Table("entities", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        values = dict(
            connection.execute(
                select(projected.c.id, projected.c.chat_session_id).where(
                    projected.c.id.in_([row["id"] for row in rows])
                )
            ).all()
        )
    assert values == {
        "message-1": "session-a",
        "approval-1": "session-b",
        "peer-message-1": None,
    }

    _run_migration(engine, command.downgrade, "0015_session_projection")
    downgraded = Table("entities", MetaData(), autoload_with=engine)
    assert "chat_session_id" not in downgraded.c
    with engine.connect() as connection:
        payloads = dict(
            connection.execute(select(downgraded.c.id, downgraded.c.payload)).all()
        )
    assert payloads["message-1"]["content"] == "kept"
    assert payloads["approval-1"]["chat_session_id"] == "session-b"
