"""The additive display cache can roll back without touching operator data."""

from alembic import command
from sqlalchemy import MetaData, Table, inspect, select

from nebula.v3.domain import ChatSession, ChatTurn
from tests.v3.test_approval_continuation import fixture
from tests.v3.test_migrations import _run_migration


def test_projection_migration_roundtrip_preserves_authoritative_records(tmp_path):
    from nebula.v3.database import SessionProjectionRow
    from nebula.v3.session_state import session_state

    _, store, runtime, _, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    original = store.get(ChatTurn, owner.id)
    session_state(store, chat, runtime)
    engine = store.database.engine
    _run_migration(engine, command.downgrade, "0014_application_graph")
    assert "session_projections" not in inspect(engine).get_table_names()
    entities = Table("entities", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        payload = connection.scalar(
            select(entities.c.payload).where(entities.c.id == owner.id)
        )
    assert ChatTurn.model_validate(payload) == original
    _run_migration(engine, command.upgrade, "head")
    with store.database.session() as database:
        assert database.scalar(select(SessionProjectionRow)) is None
    assert session_state(store, chat, runtime)["pending"]
    assert store.get(ChatTurn, owner.id) == original
