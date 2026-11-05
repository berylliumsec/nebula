"""Server database backups must follow the authoritative Core backend."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
import nebula_postgres_backup as backup


def environment(
    tmp_path, url="postgresql+psycopg://simulation:private-pass@localhost/backup_test"
):
    path = tmp_path / "service.env"
    path.write_text("NEBULA_V3_DATABASE_URL=" + url + "\n")
    path.chmod(0o600)
    return path


def fake_dump(arguments, settings):
    if arguments[0] == "pg_dump":
        Path(
            next(arg[7:] for arg in arguments if arg.startswith("--file="))
        ).write_bytes(b"PGDMP-synthetic")


def test_private_credentials_are_environment_only_and_unix_socket_is_supported(
    tmp_path,
):
    path = environment(
        tmp_path,
        "postgresql+psycopg://simulation:p%40ss@/backup_test?host=%2Fvar%2Frun%2Fpostgresql",
    )
    path.write_text("NEBULA_V3_DATABASE_URL=sqlite:///obsolete.db\n" + path.read_text())
    settings = backup.configured_postgres(path)
    assert settings["PGPASSWORD"] == "p@ss"
    assert settings["PGHOST"] == "/var/run/postgresql"
    assert settings["PGDATABASE"] == "backup_test"


def test_snapshot_and_restore_keep_private_manifest_and_atomic_restore(
    tmp_path, monkeypatch
):
    path = environment(tmp_path)
    directory = tmp_path / "snapshot"
    directory.mkdir()
    calls = []

    def run(arguments, settings):
        calls.append(arguments)
        fake_dump(arguments, settings)

    monkeypatch.setattr(backup, "_run", run)
    backup.snapshot_postgres(directory, backup.configured_postgres(path))
    backup.restore_database_backup(directory, path)
    assert backup.contains_database_backup(directory)
    assert (directory / backup.DUMP).stat().st_mode & 0o777 == 0o600
    assert (directory / backup.MANIFEST).stat().st_mode & 0o777 == 0o600
    assert "private-pass" not in (directory / backup.MANIFEST).read_text()
    assert all("private-pass" not in str(call) for call in calls)
    assert "--single-transaction" in calls[-1]
    assert "--set=ON_ERROR_STOP=1" in calls[-1]


def test_corrupt_dump_is_refused_before_restore(tmp_path, monkeypatch):
    path = environment(tmp_path)
    monkeypatch.setattr(backup, "_run", fake_dump)
    backup.snapshot_postgres(tmp_path, backup.configured_postgres(path))
    dump = tmp_path / backup.DUMP
    dump.write_bytes(b"PGDMP-corrupted")
    with pytest.raises(RuntimeError, match="missing or incomplete|checksum"):
        backup.restore_database_backup(tmp_path, path)


def test_backend_or_database_mismatch_never_restores_to_another_target(
    tmp_path, monkeypatch
):
    path = environment(tmp_path)
    monkeypatch.setattr(backup, "_run", fake_dump)
    backup.snapshot_postgres(tmp_path, backup.configured_postgres(path))
    path.write_text("NEBULA_V3_DATABASE_URL=postgresql+psycopg:///other_database\n")
    with pytest.raises(RuntimeError, match="target differs"):
        backup.restore_database_backup(tmp_path, path)
    (tmp_path / backup.MANIFEST).unlink()
    (tmp_path / "nebula.db").write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="SQLite rollback requires"):
        backup.restore_database_backup(tmp_path, path)


@pytest.mark.skipif(
    not os.getenv("NEBULA_TEST_POSTGRES_BACKUP_URL"),
    reason="isolated PostgreSQL backup database required",
)
def test_actual_pg_dump_restore_roundtrip(tmp_path):
    from sqlalchemy import create_engine, text

    url = os.environ["NEBULA_TEST_POSTGRES_BACKUP_URL"]
    path = environment(tmp_path, url)
    settings = backup.configured_postgres(path)
    assert settings["PGDATABASE"].endswith("backup_test")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE backup_probe (id integer PRIMARY KEY, value text NOT NULL)"
                )
            )
            connection.execute(text("INSERT INTO backup_probe VALUES (1, 'durable')"))
        backup.snapshot_postgres(tmp_path, settings)
        with engine.begin() as connection:
            connection.execute(text("UPDATE backup_probe SET value='changed'"))
            connection.execute(
                text(
                    "CREATE TABLE candidate_only (id integer REFERENCES backup_probe(id))"
                )
            )
            connection.execute(
                text(
                    "CREATE FUNCTION candidate_only_function() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
                )
            )
        backup.restore_database_backup(tmp_path, path)
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT id,value FROM backup_probe")
            ).all() == [(1, "durable")]
            assert (
                connection.execute(
                    text("SELECT to_regclass('candidate_only')")
                ).scalar()
                is None
            )
            assert (
                connection.execute(
                    text("SELECT to_regprocedure('candidate_only_function()')")
                ).scalar()
                is None
            )
            connection.execute(text("DROP TABLE backup_probe"))
    finally:
        engine.dispose()


def test_deployer_snapshots_postgres_and_auxiliary_sqlite_without_stale_main(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "postgres_test_deployer", SCRIPT_DIR / "nebula_deployer.py"
    )
    deployer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployer)
    data = tmp_path / "data"
    data.mkdir()
    (data / "nebula.db").write_bytes(b"obsolete SQLite main")
    for name in deployer.SQLITE_DATABASES[1:]:
        file = data / name
        file.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(file) as connection:
            connection.execute("CREATE TABLE marker (id integer)")
    path = environment(tmp_path)
    monkeypatch.setattr(deployer, "DATA", data)
    monkeypatch.setattr(deployer, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(deployer, "TOKEN_FILE", path)
    monkeypatch.setattr(backup, "_run", fake_dump)
    result = deployer.snapshot("pre-postgresql-test")
    assert not (result / "nebula.db").exists()
    assert backup.contains_database_backup(result)
    assert all((result / name).is_file() for name in deployer.SQLITE_DATABASES[1:])
    assert json.loads((result / backup.MANIFEST).read_text())["backend"] == "postgresql"


def test_rollback_preserves_failed_postgres_and_recovers_files_for_retry(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "rollback_test_deployer", SCRIPT_DIR / "nebula_deployer.py"
    )
    deployer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployer)
    backups = tmp_path / "backups"
    keep = backups / "pre-test"
    keep.mkdir(parents=True)
    (keep / "saved-file").write_text("accepted state")
    path = environment(tmp_path)
    monkeypatch.setattr(backup, "_run", fake_dump)
    backup.snapshot_postgres(keep, backup.configured_postgres(path))
    data = tmp_path / "data"
    data.mkdir()
    (data / "saved-file").write_text("failed state")
    releases = tmp_path / "releases"
    (releases / "old").mkdir(parents=True)
    record = {"active": "new", "previous": "old", "rollback_backup": str(keep)}
    events = []
    for key, value in {
        "DATA": data,
        "BACKUPS": backups,
        "TOKEN_FILE": path,
        "RELEASES": releases,
        "STATE": tmp_path / "state.json",
    }.items():
        monkeypatch.setattr(deployer, key, value)
    monkeypatch.setattr(deployer, "state", lambda: record.copy())
    monkeypatch.setattr(
        deployer, "edge", lambda action: events.append("edge:" + action)
    )
    monkeypatch.setattr(
        deployer, "service", lambda action: events.append("service:" + action)
    )
    monkeypatch.setattr(deployer, "require_stopped", lambda: None)
    monkeypatch.setattr(deployer, "switch_to", lambda release: None)
    monkeypatch.setattr(deployer, "wait_ready", lambda commit: events.append("healthy"))

    def preserve(directory, settings):
        assert (directory / "saved-file").read_text() == "failed state"
        backup.snapshot_postgres(directory, settings)
        events.append("failed database preserved")

    def restore(directory, environment_file):
        assert "failed database preserved" in events
        backup.restore_database_backup(directory, environment_file)
        events.append("database restored")

    monkeypatch.setattr(deployer, "snapshot_postgres", preserve)
    monkeypatch.setattr(
        deployer,
        "restore_database_backup",
        lambda *args: (_ for _ in ()).throw(RuntimeError("restore unavailable")),
    )
    with pytest.raises(RuntimeError, match="failed state is preserved"):
        deployer.rollback()
    saved = json.loads(deployer.STATE.read_text())
    assert saved["rollback_attempt"]["status"] == "incomplete"
    assert backup.contains_database_backup(Path(saved["post_state"]))
    assert (data / "saved-file").read_text() == "failed state"
    monkeypatch.setattr(deployer, "state", lambda: saved.copy())
    monkeypatch.setattr(deployer, "restore_database_backup", restore)
    result = deployer.rollback()
    assert (
        events.index("failed database preserved")
        < events.index("database restored")
        < events.index("healthy")
    )
    assert (data / "saved-file").read_text() == "accepted state"
    assert all(not (data / name).exists() for name in backup.BACKUP_FILES)
    assert backup.contains_database_backup(Path(result["preserved_post_state"]))
