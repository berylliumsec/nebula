"""SQLAlchemy 2 database bootstrap for the Nebula 3 headless core."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import fcntl
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    insert,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import utc_now

CURRENT_SCHEMA_VERSION = 5
SCRATCH_PROJECT_BOOTSTRAP_KEY = "scratch_project_v1"

_BOOTSTRAP_THREAD_LOCKS: dict[str, threading.Lock] = {}
_BOOTSTRAP_THREAD_LOCKS_GUARD = threading.Lock()


def _bootstrap_thread_lock(key: str) -> threading.Lock:
    """Return the process-local half of the database bootstrap lock.

    ``flock`` provides the cross-process guarantee for local SQLite files. An
    explicit thread lock is still required because file-lock semantics within
    one process vary across supported Unix platforms.
    """

    with _BOOTSTRAP_THREAD_LOCKS_GUARD:
        lock = _BOOTSTRAP_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BOOTSTRAP_THREAD_LOCKS[key] = lock
        return lock


class Base(DeclarativeBase):
    pass


class SchemaVersionRow(Base):
    __tablename__ = "schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class BootstrapStateRow(Base):
    """Durable one-shot application bootstrap decisions.

    A fresh database is marked eligible before any user-facing service runs. An
    existing database is marked complete instead, so upgrading an empty or
    imported database can never unexpectedly add a project.
    """

    __tablename__ = "bootstrap_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    engagement_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EntityRow(Base):
    """Versioned JSON documents validated by the Pydantic domain boundary."""

    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_kind_engagement", "kind", "engagement_id"),
        Index("ix_entities_engagement_updated", "engagement_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    engagement_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_run_events_idempotency"),
        Index("ix_run_events_replay", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)


class OperationEventRow(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "sequence", name="uq_operation_events_sequence"
        ),
        UniqueConstraint(
            "operation_id",
            "idempotency_key",
            name="uq_operation_events_idempotency",
        ),
        Index("ix_operation_events_replay", "operation_id", "sequence"),
        Index("ix_operation_events_engagement_time", "engagement_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    engagement_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)


class RunBudgetCounterRow(Base):
    __tablename__ = "run_budget_counters"

    run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


@event.listens_for(RunEventRow, "before_update")
def _prevent_event_update(*_: object) -> None:
    raise RuntimeError("run events are append-only")


@event.listens_for(RunEventRow, "before_delete")
def _prevent_event_delete(*_: object) -> None:
    raise RuntimeError("run events are append-only")


@event.listens_for(OperationEventRow, "before_update")
def _prevent_operation_event_update(*_: object) -> None:
    raise RuntimeError("operation events are append-only")


@event.listens_for(OperationEventRow, "before_delete")
def _prevent_operation_event_delete(*_: object) -> None:
    raise RuntimeError("operation events are append-only")


class SchemaVersionError(RuntimeError):
    """Raised when a database was created by an incompatible Nebula version."""


class Database:
    """Own the SQLAlchemy engine, sessions, WAL setup, and schema version."""

    def __init__(
        self,
        location: str | Path,
        *,
        echo: bool = False,
        bootstrap: bool = True,
    ) -> None:
        self.url = self._normalize_url(location)
        connect_args: dict[str, Any] = {}
        if self.url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}

        self.engine = create_engine(
            self.url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )
        if bootstrap:
            try:
                self.bootstrap()
            except Exception as caught_error:
                record_caught_exception(
                    "storage",
                    "storage.database.caught_failure_001",
                    "A handled storage operation raised an exception.",
                    caught_error,
                    stage="database",
                )
                self.engine.dispose()
                raise

    @staticmethod
    def _normalize_url(location: str | Path) -> str:
        value = str(location)
        if "://" in value or value == "sqlite://":
            return value
        if value == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        path = Path(value).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        return f"sqlite+pysqlite:///{path}"

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            database_rows = cursor.execute("PRAGMA database_list").fetchall()
            for _, name, location in database_rows:
                if name != "main" or not location:
                    continue
                database_path = Path(location)
                for path in (
                    database_path,
                    Path(f"{database_path}-wal"),
                    Path(f"{database_path}-shm"),
                ):
                    if path.exists():
                        path.chmod(0o600)
        finally:
            cursor.close()

    def bootstrap(self) -> int:
        """Create the current schema or validate an existing schema version.

        Version rows are append-only migration markers.  Future migrations should
        apply one version at a time and insert a row only after each transaction.
        """

        with self._bootstrap_serialization():
            # Capture and persist this fact before Alembic creates bookkeeping
            # or application tables. It is deliberately stricter than "contains
            # no engagements": an existing/upgraded/imported database is never
            # eligible for an implicit Scratch Project, even when its entities
            # have later been removed. Persisting before migration also lets a
            # later constructor recover safely if migration is interrupted.
            database_was_truly_empty = not inspect(self.engine).get_table_names()
            self._prepare_bootstrap_marker(database_was_truly_empty)
            self._run_alembic_migrations()
            if self.engine.dialect.name == "sqlite":
                database_path = self.engine.url.database
                if database_path and database_path != ":memory:":
                    Path(database_path).chmod(0o600)
            with self.engine.begin() as connection:
                current = connection.scalar(select(func.max(SchemaVersionRow.version)))
                if current is None:
                    connection.execute(
                        insert(SchemaVersionRow).values(
                            version=CURRENT_SCHEMA_VERSION, applied_at=utc_now()
                        )
                    )
                    current = CURRENT_SCHEMA_VERSION
                elif current > CURRENT_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"database schema {current} is newer than supported schema "
                        f"{CURRENT_SCHEMA_VERSION}"
                    )
                elif current < CURRENT_SCHEMA_VERSION:
                    self._migrate(connection, current, CURRENT_SCHEMA_VERSION)
                marker = connection.scalar(
                    select(BootstrapStateRow).where(
                        BootstrapStateRow.key == SCRATCH_PROJECT_BOOTSTRAP_KEY
                    )
                )
                if marker is None:
                    # Defensive recovery for databases whose bootstrap table was
                    # modified independently of Nebula's packaged migrations.
                    now = utc_now()
                    connection.execute(
                        insert(BootstrapStateRow).values(
                            key=SCRATCH_PROJECT_BOOTSTRAP_KEY,
                            status=(
                                "eligible" if database_was_truly_empty else "complete"
                            ),
                            engagement_id=None,
                            created_at=now,
                            completed_at=(None if database_was_truly_empty else now),
                        )
                    )
                return CURRENT_SCHEMA_VERSION

    @contextmanager
    def _bootstrap_serialization(self) -> Iterator[None]:
        """Serialize schema/bootstrap decisions for one local database.

        The adjacent lock file is intentionally durable: the kernel owns the
        actual lock, so a crash releases it without stale-lock cleanup. Keeping
        the inode in place also avoids unlink/recreate races between processes.
        """

        database_path = self._sqlite_database_path()
        key = str(database_path) if database_path is not None else self.url
        with _bootstrap_thread_lock(key):
            if database_path is None:
                yield
                return

            lock_path = Path(f"{database_path}.bootstrap.lock")
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _sqlite_database_path(self) -> Path | None:
        if self.engine.dialect.name != "sqlite":
            return None
        database = self.engine.url.database
        if not database or database == ":memory:":
            return None
        return Path(database).expanduser().resolve()

    def _prepare_bootstrap_marker(self, database_was_truly_empty: bool) -> None:
        """Persist first-run eligibility before migrations can be interrupted."""

        connection = self.engine.connect()
        try:
            if self.engine.dialect.name == "sqlite":
                # Python's sqlite driver does not reliably begin a transaction
                # for DDL. An explicit write transaction makes table creation
                # and the eligibility row atomic across crashes.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            BootstrapStateRow.metadata.tables[BootstrapStateRow.__tablename__].create(
                connection, checkfirst=True
            )
            marker = connection.scalar(
                select(BootstrapStateRow).where(
                    BootstrapStateRow.key == SCRATCH_PROJECT_BOOTSTRAP_KEY
                )
            )
            if marker is not None:
                connection.commit()
                return
            now = utc_now()
            connection.execute(
                insert(BootstrapStateRow).values(
                    key=SCRATCH_PROJECT_BOOTSTRAP_KEY,
                    status=("eligible" if database_was_truly_empty else "complete"),
                    engagement_id=None,
                    created_at=now,
                    completed_at=(None if database_was_truly_empty else now),
                )
            )
            connection.commit()
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.database.caught_failure_002",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="database",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def _run_alembic_migrations(self) -> None:
        """Apply packaged schema DDL after the atomic bootstrap-marker preflight."""

        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).with_name("migrations"))
        )
        config.attributes["connection"] = self.engine.connect()
        try:
            command.upgrade(config, "head")
        finally:
            config.attributes["connection"].close()

    @staticmethod
    def _migrate(connection: Any, current: int, target: int) -> None:
        if current >= target:
            return
        if current < 1 or target > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"no migration path from schema {current} to {target}"
            )
        # Alembic has already applied the authoritative DDL. Keep the compact
        # application schema ledger in step with every packaged revision.
        for version in range(current + 1, target + 1):
            connection.execute(
                insert(SchemaVersionRow).values(version=version, applied_at=utc_now())
            )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.database.caught_failure_003",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="database",
            )
            session.rollback()
            raise
        finally:
            session.close()

    def current_schema_version(self) -> int:
        with self._session_factory() as session:
            value = session.scalar(select(func.max(SchemaVersionRow.version)))
            if value is None:
                raise SchemaVersionError("database has not been bootstrapped")
            return int(value)

    def health(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
            result: dict[str, Any] = {
                "database": "ok",
                "dialect": self.engine.dialect.name,
                "schema_version": self.current_schema_version(),
            }
            if self.engine.dialect.name == "sqlite":
                result["journal_mode"] = connection.exec_driver_sql(
                    "PRAGMA journal_mode"
                ).scalar_one()
            return result

    def dispose(self) -> None:
        self.engine.dispose()


def create_database(location: str | Path, *, echo: bool = False) -> Database:
    """Convenience factory used by the CLI, API, and tests."""

    return Database(location, echo=echo)
