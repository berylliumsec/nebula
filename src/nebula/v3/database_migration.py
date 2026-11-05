"""Offline, verified migration of a Core SQLite database to PostgreSQL.

Only the relational Core database is copied. The data directory, artifacts,
credentials, auxiliary SQLite stores, and service configuration stay in place.
The target schema and all rows commit together, after content verification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, Boolean, DateTime, MetaData, String, Table, create_engine
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.pool import NullPool

from .database import CURRENT_SCHEMA_VERSION


class MigrationError(RuntimeError):
    """A migration was refused or rolled back without changing the source."""


@dataclass(frozen=True)
class TableMigrationReport:
    table: str
    rows: int
    sha256: str


@dataclass(frozen=True)
class MigrationReport:
    source: str
    source_bytes: int
    schema_revision: str
    tables: list[TableMigrationReport]
    elapsed_seconds: float
    verified: bool = True
    schema: str = "nebula.sqlite-to-postgresql/v1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _JsonValue:
    value: Any


Progress = Callable[[dict[str, Any]], None]


def _migration_config() -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    return config


def _current_heads() -> list[str]:
    return sorted(ScriptDirectory.from_config(_migration_config()).get_heads())


def _source_path(source: Path) -> Path:
    try:
        path = source.expanduser().resolve(strict=True)
    except OSError:
        # diagnostic-expected: absent sources are reported without creating a file.
        raise MigrationError("The source SQLite database does not exist.") from None
    if not path.is_file():
        raise MigrationError("The source SQLite database must be a regular file.")
    return path


def _file_identity(path: Path) -> tuple[tuple[int, int, int, int] | None, ...]:
    identities: list[tuple[int, int, int, int] | None] = []
    for candidate in (path, Path(f"{path}-wal")):
        try:
            value = candidate.stat()
        except FileNotFoundError:
            # diagnostic-expected: a checkpointed database may have no WAL file.
            identities.append(None)
        else:
            # Opening an already checkpointed WAL database in mode=ro can
            # create empty WAL/SHM coordination files. They contain no commits.
            if candidate != path and value.st_size == 0:
                identities.append(None)
                continue
            identities.append(
                (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            )
    return tuple(identities)


@contextmanager
def _source_connection(path: Path) -> Iterator[Connection]:
    # A custom creator avoids URI interpretation of literal ?/# in filenames.
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    engine = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, timeout=1),
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.commit()
            yield connection
    finally:
        engine.dispose()


def _source_schema(connection: Connection) -> tuple[MetaData, str]:
    metadata = MetaData()
    metadata.reflect(bind=connection)
    if "alembic_version" not in metadata.tables:
        raise MigrationError(
            "The source has no Core schema revision. Upgrade it with the current "
            "Core before migrating."
        )
    revisions = sorted(
        str(row[0])
        for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
    )
    if revisions != _current_heads():
        raise MigrationError(
            "The source schema does not match this Core build. Upgrade the SQLite "
            "database with this Core build before migrating."
        )
    if "schema_versions" not in metadata.tables:
        raise MigrationError("The source is missing the Core schema version ledger.")
    version = connection.exec_driver_sql(
        "SELECT MAX(version) FROM schema_versions"
    ).scalar()
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError("The source Core schema version is unsupported.")
    return metadata, ",".join(revisions)


def _quoted_sqlite(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def inspect_sqlite_source(source: Path) -> dict[str, Any]:
    """Read a consistent source inventory without creating or upgrading a DB."""

    path = _source_path(source)
    try:
        with _source_connection(path) as connection:
            connection.exec_driver_sql("BEGIN")
            metadata, revision = _source_schema(connection)
            tables = [
                {
                    "table": table.name,
                    "rows": connection.exec_driver_sql(
                        f"SELECT COUNT(*) FROM {_quoted_sqlite(table.name)}"
                    ).scalar_one(),
                }
                for table in metadata.sorted_tables
            ]
            return {
                "schema": "nebula.sqlite-migration-inspection/v1",
                "source": str(path),
                "source_bytes": path.stat().st_size,
                "schema_revision": revision,
                "tables": tables,
                "requires_stopped_core": True,
            }
    except MigrationError:
        raise
    except Exception:
        # diagnostic-expected: this offline boundary sanitizes driver errors because SQL data can contain secrets.
        # Driver errors may include SQL data or connection credentials.
        raise MigrationError(
            "The source SQLite database could not be inspected."
        ) from None


def _target_engine(target_url: str) -> Engine:
    try:
        url = make_url(target_url)
    except Exception:
        # diagnostic-expected: malformed URLs may contain passwords and must never be logged verbatim.
        raise MigrationError("Supply a valid PostgreSQL target URL.") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise MigrationError("The target must use postgresql+psycopg://.")
    try:
        return create_engine(
            url.set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            hide_parameters=True,
        )
    except Exception:
        # diagnostic-expected: driver initialization errors are sanitized at the offline CLI boundary.
        raise MigrationError(
            "The PostgreSQL driver could not be initialized."
        ) from None


def _validate_schema(source: MetaData, target: MetaData) -> None:
    if set(source.tables) != set(target.tables):
        raise MigrationError(
            "The source contains missing or additional tables for this Core build. "
            "No tables can be omitted from migration."
        )
    for name, table in source.tables.items():
        other = target.tables[name]
        if set(table.columns.keys()) != set(other.columns.keys()):
            raise MigrationError(
                f"The source columns do not match the target for {name}."
            )
        if [column.name for column in table.primary_key] != [
            column.name for column in other.primary_key
        ]:
            raise MigrationError(f"The source primary key does not match for {name}.")
        if not table.primary_key.columns:
            raise MigrationError(f"The table {name} has no stable migration order.")


def _require_empty_target(target: Connection) -> None:
    # Match Database._bootstrap_serialization so an accidentally started Core
    # waits for the atomic migration rather than racing its uncommitted DDL.
    target.exec_driver_sql(
        "SELECT pg_advisory_xact_lock("
        "hashtext('nebula:bootstrap'), hashtext(current_schema()))"
    )
    has_objects = target.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = current_schema()::regnamespace) "
        "OR EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = current_schema()::regnamespace) "
        "OR EXISTS (SELECT 1 FROM pg_type WHERE typnamespace = current_schema()::regnamespace)"
    ).scalar_one()
    if has_objects:
        raise MigrationError(
            "The PostgreSQL target schema must be empty; existing data is never overwritten."
        )


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(
            _json_has_nul(key) or _json_has_nul(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_json_has_nul(item) for item in value)
    return False


def _decode_value(column: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, JSON):
        parsed = json.loads(value)
        # PostgreSQL json accepts escaped NUL at input, but JSON extraction
        # fails later. Refuse an unusable migrated Core before committing it.
        if "\\u0000" in value and _json_has_nul(parsed):
            raise MigrationError(
                f"JSON NUL characters in {column.table.name}.{column.name} are "
                "unsupported by PostgreSQL queries. Resolve them before migrating."
            )
        return _JsonValue(parsed)
    if isinstance(column.type, DateTime):
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    if isinstance(column.type, Boolean):
        if value not in (0, 1, False, True):
            raise MigrationError(
                f"Invalid boolean in {column.table.name}.{column.name}."
            )
        return bool(value)
    return value


def _canonical_row(values: Sequence[Any]) -> bytes:
    def canonical(value: Any) -> Any:
        if isinstance(value, _JsonValue):
            return {"json": value.value}
        if isinstance(value, datetime):
            return {
                "datetime": value.astimezone(timezone.utc).isoformat(
                    timespec="microseconds"
                )
            }
        if isinstance(value, date):
            return {"date": value.isoformat()}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        return value

    return (_json_text([canonical(value) for value in values]) + "\n").encode("utf-8")


def _notify(progress: Progress | None, phase: str, table: str, rows: int) -> None:
    if progress is not None:
        progress({"phase": phase, "table": table, "rows": rows})


def _driver_connection(target: Connection) -> Any:
    raw = target.connection.driver_connection
    if raw is None:
        raise MigrationError("The PostgreSQL driver connection is unavailable.")
    return raw


def _copy_table(
    source: Connection,
    target: Connection,
    source_table: Table,
    target_table: Table,
    batch_size: int,
    progress: Progress | None,
) -> TableMigrationReport:
    from psycopg import sql
    from psycopg.types.json import Json

    columns = [source_table.c[name] for name in target_table.columns.keys()]
    names = [column.name for column in columns]
    source_sql = (
        "SELECT "
        + ",".join(_quoted_sqlite(name) for name in names)
        + f" FROM {_quoted_sqlite(source_table.name)} ORDER BY "
        + ",".join(
            _quoted_sqlite(column.name)
            + (" COLLATE BINARY" if isinstance(column.type, String) else "")
            for column in source_table.primary_key
        )
    )
    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(target_table.name), sql.SQL(",").join(map(sql.Identifier, names))
    )
    raw_target = _driver_connection(target)
    digest = hashlib.sha256()
    count = 0
    _notify(progress, "copy", source_table.name, count)
    with source.exec_driver_sql(source_sql) as rows, raw_target.cursor() as cursor:
        with cursor.copy(copy_sql) as writer:
            for row in rows:
                values = [
                    _decode_value(column, value) for column, value in zip(columns, row)
                ]
                digest.update(_canonical_row(values))
                writer.write_row(
                    [
                        Json(value.value, dumps=_json_text)
                        if isinstance(value, _JsonValue)
                        else value
                        for value in values
                    ]
                )
                count += 1
                if count % batch_size == 0:
                    _notify(progress, "copy", source_table.name, count)
    _notify(progress, "copied", source_table.name, count)
    return TableMigrationReport(source_table.name, count, digest.hexdigest())


def _verify_table(
    target: Connection,
    table: Table,
    expected: TableMigrationReport,
    batch_size: int,
    progress: Progress | None,
) -> None:
    from psycopg import sql

    columns = list(table.columns)
    selected = [
        sql.SQL("{}::text").format(sql.Identifier(column.name))
        if isinstance(column.type, JSON)
        else sql.Identifier(column.name)
        for column in columns
    ]
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(",").join(selected),
        sql.Identifier(table.name),
        sql.SQL(",").join(
            sql.SQL('{} COLLATE "C"').format(sql.Identifier(column.name))
            if isinstance(column.type, String)
            else sql.Identifier(column.name)
            for column in table.primary_key
        ),
    )
    digest = hashlib.sha256()
    count = 0
    raw_target = _driver_connection(target)
    has_text_key = any(isinstance(column.type, String) for column in table.primary_key)
    order_index = "__nebula_migration_verify_order"
    if has_text_key:
        # A locale-sensitive PK cannot serve byte ordering. Build a small
        # key/TID index instead of externally sorting whole JSON envelopes.
        _notify(progress, "index", table.name, expected.rows)
        with raw_target.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE INDEX {} ON {} ({})").format(
                    sql.Identifier(order_index),
                    sql.Identifier(table.name),
                    sql.SQL(",").join(
                        sql.SQL('{} COLLATE "C"').format(sql.Identifier(column.name))
                        if isinstance(column.type, String)
                        else sql.Identifier(column.name)
                        for column in table.primary_key
                    ),
                )
            )
    with raw_target.cursor(name="nebula_migration_verify") as cursor:
        cursor.itersize = batch_size
        cursor.execute(query)
        for row in cursor:
            values = [
                _decode_value(column, value) for column, value in zip(columns, row)
            ]
            digest.update(_canonical_row(values))
            count += 1
            if count % batch_size == 0:
                _notify(progress, "verify", table.name, count)
    if count != expected.rows or digest.hexdigest() != expected.sha256:
        raise MigrationError(
            f"Content verification failed for {table.name}; target rolled back."
        )
    if has_text_key:
        with raw_target.cursor() as cursor:
            cursor.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(order_index)))
    _notify(progress, "verified", table.name, count)


def _reset_sequences(target: Connection, metadata: MetaData) -> None:
    from psycopg import sql

    raw_target = _driver_connection(target)
    with raw_target.cursor() as cursor:
        for table in metadata.sorted_tables:
            for column in table.columns:
                cursor.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)", (table.name, column.name)
                )
                sequence = cursor.fetchone()[0]
                if sequence is not None:
                    cursor.execute(
                        sql.SQL("SELECT MAX({}) FROM {}").format(
                            sql.Identifier(column.name), sql.Identifier(table.name)
                        )
                    )
                    maximum = cursor.fetchone()[0]
                    cursor.execute(
                        "SELECT setval(%s::regclass, %s, %s)",
                        (
                            sequence,
                            maximum if maximum is not None else 1,
                            maximum is not None,
                        ),
                    )


def migrate_sqlite_to_postgresql(
    source: Path,
    target_url: str,
    *,
    batch_size: int = 1000,
    confirm_core_stopped: bool = False,
    progress: Progress | None = None,
) -> MigrationReport:
    """Copy a stopped current SQLite Core into an empty PostgreSQL schema.

    Confirmation is required because a consistent SQLite read snapshot alone
    cannot prevent new writes after it was taken. Stop every Core process and
    other writer first, and keep them stopped until the receipt is verified and
    the service is configured for PostgreSQL. Failed migration rolls back the
    entire target schema, so the same empty target is safe to retry.
    """

    if not confirm_core_stopped:
        raise MigrationError(
            "Stop Core and every SQLite writer, then confirm Core is stopped."
        )
    if not 1 <= batch_size <= 10_000:
        raise MigrationError("The batch size must be between 1 and 10000.")
    path = _source_path(source)
    identity = _file_identity(path)
    if identity[0] is None:
        raise MigrationError("The source SQLite database is no longer available.")
    source_bytes = identity[0][2]
    engine = _target_engine(target_url)
    started = time.monotonic()
    phase = "opening the source and target"
    committed = False
    try:
        with _source_connection(path) as source_connection:
            before_version = source_connection.exec_driver_sql(
                "PRAGMA data_version"
            ).scalar_one()
            source_connection.commit()
            source_connection.exec_driver_sql("BEGIN")
            metadata, revision = _source_schema(source_connection)
            with engine.connect() as target, target.begin():
                _require_empty_target(target)
                phase = "creating the PostgreSQL schema"
                config = _migration_config()
                config.attributes["connection"] = target
                command.upgrade(config, "head")
                target_metadata = MetaData()
                target_metadata.reflect(bind=target)
                _validate_schema(metadata, target_metadata)
                # Alembic seeds these two ledgers on a new schema. Copy their
                # original rows too, preserving the complete source history.
                target.exec_driver_sql("DELETE FROM schema_versions")
                target.exec_driver_sql("DELETE FROM alembic_version")
                reports = []
                for table in target_metadata.sorted_tables:
                    phase = f"copying {table.name}"
                    report = _copy_table(
                        source_connection,
                        target,
                        metadata.tables[table.name],
                        table,
                        batch_size,
                        progress,
                    )
                    phase = f"verifying {table.name}"
                    _verify_table(target, table, report, batch_size, progress)
                    reports.append(report)
                phase = "final verification"
                _reset_sequences(target, target_metadata)
                source_connection.rollback()
                after_version = source_connection.exec_driver_sql(
                    "PRAGMA data_version"
                ).scalar_one()
                if before_version != after_version or identity != _file_identity(path):
                    raise MigrationError(
                        "The source changed during migration; stop every writer and retry with an empty target."
                    )
                _notify(progress, "committing", "", sum(item.rows for item in reports))
                phase = "committing"
            committed = True
            return MigrationReport(
                source=str(path),
                source_bytes=source_bytes,
                schema_revision=revision,
                tables=reports,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
    except MigrationError:
        raise
    except Exception as error:
        # diagnostic-expected: the offline CLI reports only the stage and exception type, suppressing secret-bearing driver errors.
        if committed:
            raise MigrationError(
                "The PostgreSQL migration committed, but source connection cleanup "
                "failed. Keep Core stopped and inspect the target before continuing."
            ) from None
        if phase == "committing":
            raise MigrationError(
                "The PostgreSQL commit outcome is uncertain. Keep Core stopped and "
                "inspect the target before retrying; it may already contain the migration."
            ) from None
        raise MigrationError(
            f"Migration failed while {phase} ({type(error).__name__}); "
            "the PostgreSQL transaction was rolled back. The migrator did not modify the SQLite source."
        ) from None
    finally:
        engine.dispose()
