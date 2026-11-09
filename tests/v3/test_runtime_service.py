"""Startup migrations require a new database or explicit offline adoption."""

import json
import sqlite3

import pytest

from nebula.v3.database import Database
from nebula.v3.runtime_launcher import runtime_root, write_private
from nebula.v3.runtime_service import OFFLINE_ADOPTION, initialize_database

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_fresh_start_bootstraps_once_and_reopen_does_not_migrate(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "nebula.db", bootstrap=False)
    try:
        await initialize_database(database, tmp_path)

        async def forbidden():
            pytest.fail("Routine reopen applied migrations")

        monkeypatch.setattr(database, "bootstrap", forbidden)
        await initialize_database(database, tmp_path)
    finally:
        await database.dispose()


async def test_nonempty_legacy_database_requires_explicit_offline_adoption(tmp_path):
    path = tmp_path / "nebula.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_saved_work (value TEXT)")
        connection.execute("INSERT INTO legacy_saved_work VALUES ('saved')")
    database = Database(path, bootstrap=False)
    try:
        with pytest.raises(RuntimeError, match="verified migration"):
            await initialize_database(database, tmp_path)
    finally:
        await database.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM legacy_saved_work").fetchall() == [
            ("saved",)
        ]
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            == 1
        )


async def test_offline_adoption_preserves_legacy_data_and_consumes_marker(tmp_path):
    path = tmp_path / "nebula.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_saved_work (value TEXT)")
        connection.execute("INSERT INTO legacy_saved_work VALUES ('saved')")
    marker = runtime_root(tmp_path) / OFFLINE_ADOPTION
    write_private(
        marker,
        json.dumps(
            {"state": "offline-legacy-adoption", "data_root": str(tmp_path)}
        ).encode(),
    )
    database = Database(path, bootstrap=False)
    try:
        await initialize_database(database, tmp_path)
        assert not marker.exists()
    finally:
        await database.dispose()
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT value FROM legacy_saved_work").fetchone()[0]
            == "saved"
        )
