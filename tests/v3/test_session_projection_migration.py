"""The additive display cache can roll back without touching operator data."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select

from nebula.v3.database import Database
from nebula.v3.domain import ChatSession, ChatTurn
from nebula.v3.storage import NebulaStore
from tests.v3.test_approval_continuation import fixture
from tests.v3.test_migrations import (
    LAST_REVERSIBLE,
    ROLLBACK_REFUSED,
    _run_migration,
)


def _copy_rows(source, target, tables) -> None:
    """Copy SQLite rows verbatim into the same columns of an older schema."""

    with target.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ? AS source", (str(source),))
        for table in tables:
            columns = ", ".join(
                item["name"] for item in inspect(connection).get_columns(table)
            )
            connection.exec_driver_sql(
                f"INSERT INTO main.{table} ({columns}) "
                f"SELECT {columns} FROM source.{table}"
            )
        connection.commit()
        connection.exec_driver_sql("DETACH DATABASE source")


async def test_projection_migration_roundtrip_preserves_authoritative_records(
    tmp_path,
):
    from nebula.v3.database import SessionProjectionRow
    from nebula.v3.session_state import session_state

    _, store, runtime, _, _, owner = await fixture(tmp_path)
    chat = await store.get(ChatSession, owner.session_id)
    original = await store.get(ChatTurn, owner.id)
    (await session_state(store, chat, runtime))
    (await store.database.engine.dispose())
    current = create_engine(Database._normalize_sync_url(tmp_path / "nebula.db"))
    # Revisions from 0031 refuse to downgrade (c1d88d9, #960) and keep the
    # cache in place.
    try:
        with pytest.raises(RuntimeError, match=ROLLBACK_REFUSED):
            _run_migration(current, command.downgrade, "0014_application_graph")
        assert "session_projections" in inspect(current).get_table_names()
    finally:
        current.dispose()
    async with store.database.session() as database:
        assert (await database.scalar(select(SessionProjectionRow))) is not None

    # The cache still rolls back on a database written before that boundary:
    # the same records and cached projection, with no runtime revision applied.
    location = tmp_path / "before-runtime-coordination.db"
    engine = create_engine(Database._normalize_sync_url(location))
    try:
        _run_migration(engine, command.upgrade, LAST_REVERSIBLE)
        _copy_rows(tmp_path / "nebula.db", engine, ("entities", "session_projections"))
        with engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(
                    Table("session_projections", MetaData(), autoload_with=engine)
                )
            )
        _run_migration(engine, command.downgrade, "0014_application_graph")
        assert "session_projections" not in inspect(engine).get_table_names()
        entities = Table("entities", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            payload = connection.scalar(
                select(entities.c.payload).where(entities.c.id == owner.id)
            )
        assert ChatTurn.model_validate(payload) == original
        _run_migration(engine, command.upgrade, "head")
    finally:
        engine.dispose()
    store = NebulaStore(location)
    async with store.database.session() as database:
        assert (await database.scalar(select(SessionProjectionRow))) is None
    assert (await session_state(store, chat, runtime))["pending"]
    assert (await store.get(ChatTurn, owner.id)) == original


pytestmark = pytest.mark.anyio
