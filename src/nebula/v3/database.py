"""SQLAlchemy 2 database bootstrap for the Nebula 3 headless core."""

from __future__ import annotations

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
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import utc_now

CURRENT_SCHEMA_VERSION = 2


class Base(DeclarativeBase):
    pass


class SchemaVersionRow(Base):
    __tablename__ = "schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
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


class RunBudgetCounterRow(Base):
    __tablename__ = "run_budget_counters"

    run_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
            self.bootstrap()

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
        finally:
            cursor.close()

    def bootstrap(self) -> int:
        """Create the current schema or validate an existing schema version.

        Version rows are append-only migration markers.  Future migrations should
        apply one version at a time and insert a row only after each transaction.
        """

        self._run_alembic_migrations()
        if self.engine.dialect.name == "sqlite":
            database_path = self.engine.url.database
            if database_path and database_path != ":memory:":
                Path(database_path).chmod(0o600)
        with self.engine.begin() as connection:
            current = connection.scalar(select(func.max(SchemaVersionRow.version)))
            if current is None:
                connection.execute(
                    SchemaVersionRow.__table__.insert().values(
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
            return CURRENT_SCHEMA_VERSION

    def _run_alembic_migrations(self) -> None:
        """Make packaged Alembic revisions the sole schema-DDL authority."""

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
        if current == 1 and target == 2:
            # ``Base.metadata.create_all`` has already installed the additive
            # budget counter table. Record the migration only after that DDL.
            connection.execute(
                SchemaVersionRow.__table__.insert().values(
                    version=2, applied_at=utc_now()
                )
            )
            return
        raise SchemaVersionError(f"no migration path from schema {current} to {target}")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
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
