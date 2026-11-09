"""Startup migrations require a new database or explicit offline adoption."""

import json
import sqlite3

import pytest

from nebula.v3.database import Database
from nebula.v3.runtime_launcher import runtime_root, write_private
from nebula.v3.runtime_service import (
    OFFLINE_ADOPTION,
    initialize_database,
    runtime_pool_settings,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_sqlite_startup_does_not_require_postgresql_fleet_settings(
    tmp_path, monkeypatch
):
    for key in ("ADMIN_RESERVE", "OTHER_CONNECTIONS", "PEAK_PROCESSES", "POOL_SIZE"):
        monkeypatch.delenv("NEBULA_V3_DB_" + key, raising=False)
    assert runtime_pool_settings(tmp_path / "fresh.sqlite3") is None
    assert runtime_pool_settings("sqlite+aiosqlite:///:memory:") is None
    with pytest.raises(ValueError, match="ADMIN_RESERVE"):
        runtime_pool_settings("postgresql://synthetic@localhost/test")


def test_postgresql_startup_propagates_declared_fleet_settings(monkeypatch):
    for key, value in {
        "ADMIN_RESERVE": "8",
        "OTHER_CONNECTIONS": "12",
        "PEAK_PROCESSES": "8",
        "POOL_SIZE": "3",
    }.items():
        monkeypatch.setenv("NEBULA_V3_DB_" + key, value)
    settings = runtime_pool_settings("postgresql+psycopg://synthetic@localhost/test")
    assert settings is not None
    assert (
        settings.admin_reserve,
        settings.other_connections,
        settings.peak_processes,
        settings.requested_size,
    ) == (8, 12, 8, 3)


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
