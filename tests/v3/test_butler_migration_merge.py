"""The reconciled PR must upgrade either previously published migration leaf."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, inspect

from nebula.v3.database import Database
from tests.v3.test_migrations import _run_migration


@pytest.mark.parametrize(
    "previous", [None, "0024_entity_hot_lookup_indexes", "0024_project_create_requests"]
)
async def test_reconciled_butler_migrations_preserve_either_existing_leaf(
    tmp_path, previous
):
    path = tmp_path / "core.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    stamp = datetime(2026, 11, 2, tzinfo=timezone.utc)
    head = ScriptDirectory(
        str(Path(__file__).parents[2] / "src/nebula/v3/migrations")
    ).get_current_head()
    try:
        if previous:
            _run_migration(engine, command.upgrade, previous)
            entities = Table("entities", MetaData(), autoload_with=engine)
            with engine.begin() as connection:
                connection.execute(
                    entities.insert().values(
                        id="retained-note",
                        kind="observations",
                        engagement_id="synthetic-project",
                        revision=7,
                        payload={"body": "Retained operator content"},
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                if previous == "0024_project_create_requests":
                    receipts = Table(
                        "project_create_requests", MetaData(), autoload_with=engine
                    )
                    connection.execute(
                        receipts.insert().values(
                            actor_id="operator",
                            request_id="retry-key",
                            payload_hash="a" * 64,
                            engagement_id="synthetic-project",
                            created_at=stamp,
                        )
                    )
        with engine.connect() as connection:
            original = (
                connection.exec_driver_sql(
                    "SELECT id,kind,engagement_id,revision,payload,created_at,updated_at FROM entities ORDER BY id"
                ).all()
                if previous
                else []
            )
        for _ in range(2):
            _run_migration(engine, command.upgrade, "head")
            assert "project_create_requests" in inspect(engine).get_table_names()
            assert {
                "artifact_tool_call_id",
                "tool_call_status",
                "tool_call_origin",
            } <= {c["name"] for c in inspect(engine).get_columns("entities")}
            assert "ix_entities_artifact_tool_call_created" in {
                i["name"] for i in inspect(engine).get_indexes("entities")
            }
            with engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalars().all() == [head]
                if previous:
                    assert (
                        connection.exec_driver_sql(
                            "SELECT id,kind,engagement_id,revision,payload,created_at,updated_at FROM entities ORDER BY id"
                        ).all()
                        == original
                    )
                if previous == "0024_project_create_requests":
                    assert (
                        connection.exec_driver_sql(
                            "SELECT engagement_id FROM project_create_requests WHERE request_id='retry-key'"
                        ).scalar_one()
                        == "synthetic-project"
                    )
        database = Database(path)
        (await database.engine.dispose())
    finally:
        engine.dispose()


pytestmark = pytest.mark.anyio
