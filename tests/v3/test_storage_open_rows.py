"""Storage work is found by status through an index, within the worker's project."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, event, insert, inspect, select

from nebula.v3 import storage_integrations
from nebula.v3.database import Database, EntityRow
from nebula.v3.domain import StorageDispatch, StorageSource, utc_now
from nebula.v3.execution_scope import execution_context
from nebula.v3.query_predicates import open_storage_row
from nebula.v3.storage_integrations import StorageIntegrationService
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_a_project_worker_pages_through_only_its_own_pending_dispatches(
    execution, monkeypatch
):
    control, runtime, _, lease = execution
    for project in ("mine", "other"):
        await control.create(
            StorageSource(
                id=f"source-{project}",
                name=project,
                engagement_id=project,
                profile_id="provider",
                model="inert",
                token_sha256="0" * 64,
            )
        )
    # Settled history from both projects' sources, with a few pending rows
    # spread through it. Dispatch rows carry no project of their own.
    started = utc_now() - timedelta(days=1)
    rows, pending = [], {"mine": [], "other": []}
    for index in range(3000):
        project = "mine" if index % 2 else "other"
        open_row = index % 500 in (0, 1)
        dispatch = StorageDispatch(
            id=f"dispatch-{index:05d}",
            incident_id=f"incident-{index}",
            source_id=f"source-{project}",
            event_id=f"event-{index}",
            prompt="A storage alert",
            status="pending" if open_row else "queued",
            created_at=started + timedelta(seconds=index),
            updated_at=started + timedelta(seconds=index),
        )
        if open_row:
            pending[project].append(dispatch.id)
        rows.append(
            dict(
                id=dispatch.id,
                kind=dispatch.entity_kind,
                engagement_id=None,
                revision=1,
                payload=dispatch.model_dump(mode="json"),
                created_at=dispatch.created_at,
                updated_at=dispatch.updated_at,
            )
        )
    async with control.database.session() as session:
        for offset in range(0, len(rows), 500):
            await session.execute(insert(EntityRow), rows[offset : offset + 500])

    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "storage_dispatches" in statement:
            statements.append((statement, parameters))

    # Small pages: the scan must move past rows that stay pending.
    monkeypatch.setattr(storage_integrations, "DISPATCH_PAGE", 2)
    service = StorageIntegrationService(runtime, None, None, None)
    with execution_context(lease):
        served = [item.id async for item in service._pending_dispatches()]
        # Each scan starts again at the oldest pending row.
        engine = runtime.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            again = [item.id async for item in service._pending_dispatches()]
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert served == again == pending["mine"]
    assert len(served) == 6 and not set(served) & set(pending["other"])
    # Three full pages and the short one that ends the scan.
    assert len(statements) == 4

    sqlite = control.database.engine.dialect.name == "sqlite"
    async with control.database.connection() as connection:
        for statement, parameters in statements:
            plan = str(
                (
                    await connection.exec_driver_sql(
                        ("EXPLAIN QUERY PLAN " if sqlite else "EXPLAIN ") + statement,
                        parameters,
                    )
                ).all()
            )
            assert "ix_entities_storage_dispatches_open" in plan, plan


OPEN_ROW_INDEXES = {
    "ix_entities_storage_dispatches_open",
    "ix_entities_storage_incidents_open",
    "ix_entities_storage_actions_open",
}


def _migrate(engine, revision, *, down=False):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "src/nebula/v3/migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def test_open_row_indexes_cover_saved_rows_and_roll_back(location):
    engine = create_engine(
        Database._normalize_sync_url(location), pool_size=1, max_overflow=0
    )
    try:
        _migrate(engine, "0048_chat_scoped_admission")
        entities = Table("entities", MetaData(), autoload_with=engine)
        stamp = datetime(2026, 11, 9, tzinfo=timezone.utc)
        with engine.begin() as connection:
            for status in ("pending", "queued"):
                saved = StorageDispatch(
                    id=f"saved-{status}",
                    incident_id="incident",
                    source_id="source",
                    event_id=status,
                    prompt="A storage alert written by the previous release",
                    status=status,
                    created_at=stamp,
                    updated_at=stamp,
                )
                connection.execute(
                    entities.insert().values(
                        id=saved.id,
                        kind=saved.entity_kind,
                        engagement_id=None,
                        revision=1,
                        payload=saved.model_dump(mode="json"),
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
        _migrate(engine, "0049_storage_open_rows")
        indexes = {index["name"] for index in inspect(engine).get_indexes("entities")}
        assert OPEN_ROW_INDEXES <= indexes
        pending = select(EntityRow.id).where(
            open_storage_row(EntityRow, "storage_dispatches", engine.dialect.name)
        )
        with engine.connect() as connection:
            # Rows saved before the index exist in it.
            assert connection.scalars(pending).all() == ["saved-pending"]
        # Code rollback needs nothing; a schema downgrade drops only the indexes.
        _migrate(engine, "0048_chat_scoped_admission", down=True)
        indexes = {index["name"] for index in inspect(engine).get_indexes("entities")}
        assert not OPEN_ROW_INDEXES & indexes
        with engine.connect() as connection:
            assert connection.scalars(pending).all() == ["saved-pending"]
            assert len(connection.execute(select(entities)).all()) == 2
    finally:
        engine.dispose()
