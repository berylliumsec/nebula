from __future__ import annotations

import os
import sqlite3
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from nebula.v3.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("NEBULA_V3_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )


def run_migrations_on(connection: Connection) -> None:
    """Apply the pending revisions on ``connection`` as one atomic unit.

    Python's sqlite driver never opens a transaction before DDL, so without
    an explicit one every ``CREATE TABLE``/``CREATE INDEX`` commits on its
    own while ``alembic_version`` only moves at the end of a script. A
    failure part-way through a revision would then leave those objects
    behind and every later start would fail with "already exists". Holding
    one ``BEGIN IMMEDIATE`` transaction around the whole run, the same way
    ``nebula.v3.database`` protects its bootstrap marker, makes an upgrade
    all-or-nothing. Alembic sees the open transaction and leaves commit and
    rollback to this function.
    """

    dbapi_connection = connection.connection.dbapi_connection
    if not isinstance(dbapi_connection, sqlite3.Connection):
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    previous_isolation_level = dbapi_connection.isolation_level
    # Autocommit mode stops the driver from issuing its own BEGIN/COMMIT
    # around DML; the explicit statements below own the transaction.
    dbapi_connection.isolation_level = None
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _configure(connection)
            context.run_migrations()
        except BaseException:
            # diagnostic-expected: the partial revision is rolled back and the failure re-raised unchanged.
            connection.rollback()
            raise
        connection.commit()
    finally:
        dbapi_connection.isolation_level = previous_isolation_level


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        run_migrations_on(supplied_connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        run_migrations_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
