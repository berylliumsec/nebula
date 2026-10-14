"""The upgrade changes future project defaults, never saved chat choices."""

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_legacy_project_defaults_upgrade_without_rewriting_chat_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0019_existing_project_execution_mode")
    entities = Table("entities", MetaData(), autoload_with=engine)
    now = datetime.now(timezone.utc)
    rows = []
    for id_, kind, payload in (
        (
            "project",
            "engagements",
            {"name": "Legacy", "assistant_defaults": {"allow_agent_messaging": False}},
        ),
        ("off", "chat_sessions", {"metadata": {"allow_agent_messaging": False}}),
        ("on", "chat_sessions", {"metadata": {"allow_agent_messaging": True}}),
    ):
        rows.append(
            {
                "id": id_,
                "kind": kind,
                "engagement_id": "project",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "payload": {
                    "id": id_,
                    "revision": 1,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    **payload,
                },
            }
        )
    with engine.begin() as connection:
        connection.execute(entities.insert(), rows)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with engine.connect() as connection:
        saved = {
            row.id: row
            for row in connection.execute(
                select(entities.c.id, entities.c.revision, entities.c.payload).where(
                    entities.c.id.in_(["project", "off", "on"])
                )
            )
        }
    assert (
        saved["project"].payload["assistant_defaults"]["allow_agent_messaging"] is True
    )
    assert saved["project"].revision == saved["project"].payload["revision"] == 2
    assert saved["off"].payload["metadata"]["allow_agent_messaging"] is False
    assert saved["on"].payload["metadata"]["allow_agent_messaging"] is True
    assert saved["off"].revision == saved["on"].revision == 1
    engine.dispose()
