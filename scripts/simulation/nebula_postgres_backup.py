"""Private PostgreSQL snapshots for the simulation Core deployer."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit


MANIFEST = "database-backup.json"
DUMP = "postgresql.dump"
BACKUP_FILES = (MANIFEST, DUMP)
_OPTIONS = {
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
    "options": "PGOPTIONS",
}


def configured_postgres(environment_file: Path) -> dict[str, str] | None:
    """Resolve libpq settings without putting credentials in command arguments."""

    entries = [
        line.partition("=")[2].strip().strip("\"'")
        for line in environment_file.read_text().splitlines()
        if line.startswith("NEBULA_V3_DATABASE_URL=")
    ]
    url = entries[-1] if entries else ""
    if not url or url.startswith("sqlite"):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.split("+", 1)[0] not in {"postgresql", "postgres"}:
            raise ValueError("unsupported database backend")
        settings = {
            "PGDATABASE": unquote(parsed.path.lstrip("/")),
            "PGUSER": unquote(parsed.username or getpass.getuser()),
            "PGHOST": parsed.hostname or "/var/run/postgresql",
            "PGPORT": str(parsed.port or 5432),
            "PGCONNECT_TIMEOUT": "10",
        }
        if parsed.password is not None:
            settings["PGPASSWORD"] = unquote(parsed.password)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key not in _OPTIONS:
                raise ValueError("unsupported libpq option")
            settings[_OPTIONS[key]] = value
        if not settings["PGDATABASE"]:
            raise ValueError("missing database name")
    except ValueError as exc:
        raise RuntimeError(
            "Core PostgreSQL URL cannot be used for a deployment backup"
        ) from exc
    return settings


def _identity(settings: dict[str, str]) -> str:
    # Password rotation must not prevent restoration to the same database.
    identity = {
        key: settings[key] for key in ("PGDATABASE", "PGUSER", "PGHOST", "PGPORT")
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(arguments: list[str], settings: dict[str, str]) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("PG")
    }
    result = subprocess.run(
        arguments,
        env={**environment, **settings},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=14_400,
        check=False,
    )
    if result.returncode:
        # libpq errors may contain connection details; the receipt stays credential-free.
        raise RuntimeError(
            f"{arguments[0]} failed (exit {result.returncode}); inspect PostgreSQL service access"
        )
    return result.stdout


def snapshot_postgres(directory: Path, settings: dict[str, str]) -> None:
    dump = directory / DUMP
    descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    _run(["pg_dump", "--format=custom", f"--file={dump}"], settings)
    _run(["pg_restore", "--list", str(dump)], settings)
    manifest = {
        "schema": "nebula.database-backup/v1",
        "backend": "postgresql",
        "target_sha256": _identity(settings),
        "dump": DUMP,
        "dump_sha256": _sha256(dump),
        "dump_bytes": dump.stat().st_size,
    }
    path = directory / MANIFEST
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)


def contains_database_backup(directory: Path) -> bool:
    if (directory / MANIFEST).exists():
        try:
            if (directory / MANIFEST).is_symlink():
                return False
            record = json.loads((directory / MANIFEST).read_text())
            if not isinstance(record, dict):
                return False
            dump = directory / DUMP
            return bool(
                record.get("schema") == "nebula.database-backup/v1"
                and record.get("backend") == "postgresql"
                and record.get("dump") == DUMP
                and len(record.get("dump_sha256", "")) == 64
                and not dump.is_symlink()
                and dump.is_file()
                and dump.stat().st_size == record.get("dump_bytes")
                and dump.stat().st_size > 0
            )
        except (OSError, ValueError, TypeError):
            return False
    database = directory / "nebula.db"
    return database.is_file() and not database.is_symlink()


def validate_database_backup(directory: Path, environment_file: Path) -> None:
    settings = configured_postgres(environment_file)
    if not contains_database_backup(directory):
        raise RuntimeError("rollback database backup is missing or incomplete")
    if (directory / MANIFEST).exists():
        record = json.loads((directory / MANIFEST).read_text())
        if settings is None or record.get("target_sha256") != _identity(settings):
            raise RuntimeError(
                "rollback PostgreSQL target differs from the snapshot; restore its database configuration first"
            )
        if _sha256(directory / DUMP) != record["dump_sha256"]:
            raise RuntimeError("rollback PostgreSQL dump failed its checksum")
    elif settings is not None:
        raise RuntimeError(
            "SQLite rollback requires restoring the SQLite database configuration first"
        )


def restore_database_backup(directory: Path, environment_file: Path) -> None:
    validate_database_backup(directory, environment_file)
    settings = configured_postgres(environment_file)
    if settings is not None:
        listing = _run(["pg_restore", "--list", str(directory / DUMP)], settings) or b""
        includes_public = any(
            len(parts := line.split()) >= 6
            and parts[3:6] == [b"SCHEMA", b"-", b"public"]
            for line in listing.splitlines()
        )
        # Decode the complete archive before touching the server. A failed decoder
        # must never leave psql at EOF with a partially restored transaction.
        with tempfile.NamedTemporaryFile(
            prefix=".restore-", suffix=".sql", dir=directory
        ) as sql_file:
            _run(
                [
                    "pg_restore",
                    "--no-owner",
                    f"--file={sql_file.name}",
                    str(directory / DUMP),
                ],
                settings,
            )
            reset = """DO $nebula_restore$
DECLARE item record;
BEGIN
  FOR item IN SELECT nspname FROM pg_catalog.pg_namespace
    WHERE nspname NOT IN ('information_schema', 'pg_catalog')
      AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
  LOOP
    EXECUTE pg_catalog.format('DROP SCHEMA %I CASCADE', item.nspname);
  END LOOP;
END $nebula_restore$;"""
            if not includes_public:
                reset += " CREATE SCHEMA public AUTHORIZATION CURRENT_USER;"
            # Reset and restore all application schemas in one transaction,
            # including objects introduced only by the failed candidate.
            _run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--set=ON_ERROR_STOP=1",
                    "--single-transaction",
                    f"--dbname={settings['PGDATABASE']}",
                    "--command",
                    reset,
                    "--file",
                    sql_file.name,
                ],
                settings,
            )
