"""Startup migrations require a new database or explicit offline adoption."""

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest

from nebula.v3.cli import probe_listen_address
from nebula.v3.database import Database
from nebula.v3.runtime_launcher import runtime_root, write_private
from nebula.v3.runtime_service import (
    OFFLINE_ADOPTION,
    inherited_listener,
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


_GENERATION = """
import os, socket, sys
from nebula.v3.runtime_service import inherited_listener
try:
    listener = inherited_listener("127.0.0.1", int(sys.argv[1]))
except ValueError as error:
    print("refused:", error, flush=True)
    raise SystemExit(3)
assert listener is not None and not listener.get_inheritable()
assert "LISTEN_FDS" not in os.environ and "LISTEN_PID" not in os.environ
connection, _ = listener.accept()
connection.sendall(sys.argv[2].encode())
connection.close()
"""


def _generation(holder, port, name):
    """Start one supervisor-like generation the way a socket unit does."""
    descriptor = holder.fileno()
    source = str(Path(__file__).resolve().parents[2] / "src")
    return subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            f'exec 3<&{descriptor} {descriptor}<&-; LISTEN_PID=$$ LISTEN_FDS=1 exec "$0" -c "$1" "$2" "$3"',
            sys.executable,
            _GENERATION,
            str(port),
            name,
        ],
        pass_fds=(descriptor,),
        env={**os.environ, "PYTHONPATH": source},
        stdout=subprocess.PIPE,
        text=True,
    )


def _held_listener():
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(16)
    return holder, holder.getsockname()[1]


def _read(port):
    with socket.create_connection(("127.0.0.1", port), timeout=30) as client:
        return client.recv(16).decode()


def test_inherited_listener_queues_connections_between_supervisor_generations():
    holder, port = _held_listener()
    try:
        first = _generation(holder, port, "first")
        assert _read(port) == "first"
        assert first.wait(30) == 0
        # No generation is accepting now. The connection is still accepted by
        # the kernel and waits instead of being refused.
        waiting = socket.create_connection(("127.0.0.1", port), timeout=30)
        second = _generation(holder, port, "second")
        assert waiting.recv(16).decode() == "second"
        waiting.close()
        assert second.wait(30) == 0
    finally:
        holder.close()


def test_inherited_listener_must_match_the_requested_address():
    holder, port = _held_listener()
    try:
        wrong = _generation(holder, port + 1 if port < 65535 else port - 1, "wrong")
        output, _ = wrong.communicate(timeout=30)
        assert wrong.returncode == 3 and "does not match" in output
    finally:
        holder.close()


def test_listener_is_not_adopted_by_another_process(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert inherited_listener("127.0.0.1", 8000) is None
    # Never passed on to workers or tools started by this process.
    assert "LISTEN_FDS" not in os.environ and "LISTEN_PID" not in os.environ


def test_explicit_descriptor_requires_the_exact_requested_address():
    # A launcher between the socket unit and Core hides LISTEN_PID; the
    # operator passes --listen-fd instead, and exposure is checked from the
    # bound address itself.
    holder, port = _held_listener()
    wildcard = socket.socket()
    wildcard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    wildcard.bind(("0.0.0.0", 0))
    wildcard.listen(1)
    try:
        adopted = inherited_listener(
            "127.0.0.1", port, descriptor=os.dup(holder.fileno())
        )
        assert adopted.getsockname() == ("127.0.0.1", port)
        assert not adopted.get_inheritable()
        adopted.close()
        with pytest.raises(ValueError, match="IP address"):
            inherited_listener("localhost", port, descriptor=os.dup(holder.fileno()))
        with pytest.raises(ValueError, match="does not match"):
            inherited_listener(
                "127.0.0.1",
                wildcard.getsockname()[1],
                descriptor=os.dup(wildcard.fileno()),
            )
    finally:
        holder.close()
        wildcard.close()


def test_restart_probe_ignores_closed_connections_but_refuses_a_live_listener():
    server, port = _held_listener()
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = server.accept()
    # The server side closes first, leaving its end in TIME_WAIT on the port.
    accepted.close()
    client.recv(1)
    client.close()
    with pytest.raises(OSError):
        probe_listen_address("127.0.0.1", port)
    server.close()
    probe_listen_address("127.0.0.1", port)
