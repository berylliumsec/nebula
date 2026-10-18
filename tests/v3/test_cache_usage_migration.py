"""Historical cache detail comes from durable turns, never guessed totals."""

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def _upgrade(engine, revision: str) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _usage(
    input_tokens: int, cached_input_tokens: int = 0, cost_usd: float | None = None
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "total_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_creation_input_tokens": 0,
        "cost_usd": cost_usd,
    }


def test_cached_usage_migration_reconciles_goals_and_settled_child_rounds(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'cache-usage.db'}", future=True)
    _upgrade(engine, "0021_chat_session_recent_index")
    entities = Table("entities", MetaData(), autoload_with=engine)
    now = datetime.now(timezone.utc)

    def row(entity_id: str, kind: str, **fields) -> dict:
        return {
            "id": entity_id,
            "kind": kind,
            "engagement_id": "project",
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "payload": {
                "id": entity_id,
                "revision": 1,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "engagement_id": "project",
                **fields,
            },
        }

    with engine.begin() as connection:
        connection.execute(
            entities.insert(),
            [
                row(
                    "parent-turn",
                    "chat_turns",
                    session_id="parent",
                    goal_id="goal",
                    usage=_usage(100, 80, 0.05),
                ),
                row(
                    "child-turn",
                    "chat_turns",
                    session_id="child",
                    goal_id=None,
                    usage=_usage(200, 120, 0.10),
                ),
                row(
                    "charge",
                    "chat_goal_usage_charges",
                    goal_id="goal",
                    child_turn_id="child-turn",
                    usage=_usage(200, 120, 0.10),
                ),
                row(
                    "goal",
                    "chat_goals",
                    session_id="parent",
                    usage=_usage(300),
                ),
                row(
                    "subagent",
                    "chat_subagents",
                    child_session_id="child",
                    child_turn_id="child-turn",
                    usage=_usage(200),
                ),
                row(
                    "previous-round",
                    "chat_turns",
                    session_id="active-child",
                    usage=_usage(10, 8, 0.005),
                ),
                row(
                    "current-round",
                    "chat_turns",
                    session_id="active-child",
                    usage=_usage(20, 15, 0.006),
                ),
                row(
                    "active-subagent",
                    "chat_subagents",
                    child_session_id="active-child",
                    child_turn_id="current-round",
                    usage=_usage(10),
                ),
                row(
                    "unrelated-turn",
                    "chat_turns",
                    session_id="unreconciled-child",
                    usage=_usage(50, 30, 0.02),
                ),
                row(
                    "unreconciled-subagent",
                    "chat_subagents",
                    child_session_id="unreconciled-child",
                    child_turn_id="unrelated-turn",
                    usage=_usage(60),
                ),
            ],
        )

    _upgrade(engine, "head")
    with engine.connect() as connection:
        rows = {
            item["id"]: item
            for item in connection.execute(
                select(entities.c.id, entities.c.revision, entities.c.payload).where(
                    entities.c.id.in_(
                        [
                            "goal",
                            "subagent",
                            "active-subagent",
                            "unreconciled-subagent",
                        ]
                    )
                )
            ).mappings()
        }

    goal = rows["goal"]
    assert goal["revision"] == 2
    assert goal["payload"]["usage"] == _usage(300, 200, 0.15)
    child = rows["subagent"]
    assert child["revision"] == 2
    assert child["payload"]["usage"] == _usage(200, 120, 0.10)
    active = rows["active-subagent"]
    assert active["revision"] == 2
    assert active["payload"]["usage"] == _usage(10, 8, 0.005)
    unmatched = rows["unreconciled-subagent"]
    assert unmatched["revision"] == 1
    assert unmatched["payload"]["usage"] == _usage(60)
    engine.dispose()
