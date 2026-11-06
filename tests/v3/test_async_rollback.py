"""The additive async indexes can roll back without discarding saved work."""

import asyncio
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore
from tests.v3.test_async_postgresql import postgresql_url as postgresql_url


def _rehearse(location, settings=None):
    async def seed():
        store = NebulaStore(Database(location, pool_settings=settings))
        try:
            project = await store.create(Engagement(name="Retained across rollback"))
            await store.append_event(
                "rollback-fixture",
                "saved",
                {"project": project.id},
                idempotency_key="once",
            )
            return project.model_dump(mode="json")
        finally:
            await store.event_history.close()
            await store.database.dispose()

    before = asyncio.run(seed())
    engine = create_engine(
        Database._normalize_sync_url(location),
        pool_size=1,
        max_overflow=0,
        pool_timeout=10,
    )
    try:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).parents[2] / "src/nebula/v3/migrations"),
        )
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0028_harness_session_title_lookup")
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0028_harness_session_title_lookup"
            )
            assert "runtime_switch_status" not in {
                column["name"] for column in inspect(connection).get_columns("entities")
            }
            indexes = {
                index["name"] for index in inspect(connection).get_indexes("entities")
            }
            assert "ix_entities_kind_harness_session_created" in indexes
            assert "ix_entities_harness_chat_title_project" not in indexes
            payload = connection.scalar(
                text("SELECT payload FROM entities WHERE id=:id"), {"id": before["id"]}
            )
            assert (
                json.loads(payload) if isinstance(payload, str) else payload
            ) == before
            assert connection.scalar(text("SELECT count(*) FROM run_events")) == 1
    finally:
        engine.dispose()

    async def reopen():
        store = NebulaStore(Database(location, pool_settings=settings))
        try:
            assert (await store.get(Engagement, before["id"])).model_dump(
                mode="json"
            ) == before
            assert len(await store.replay_events("rollback-fixture")) == 1
        finally:
            await store.event_history.close()
            await store.database.dispose()

    asyncio.run(reopen())


def test_sqlite_additive_schema_rollback_preserves_rows(tmp_path):
    _rehearse(tmp_path / "rollback.db")


def test_postgresql_additive_schema_rollback_preserves_rows(postgresql_url):
    _rehearse(postgresql_url, PoolSettings(5, 12, 2))
