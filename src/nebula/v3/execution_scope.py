"""Project isolation and SQL fencing for a versioned execution process.

API and host databases never install these hooks. An execution database opens
only after the host migrates and registers its process incarnation. All of its
transactions take the same lock order, including the raw-connection ledger
writers.

On PostgreSQL every transaction first holds ``EXECUTION_SQL_KEY`` shared, then
its own runtime's key exclusively, then share-locks the runtime's ownership
rows (``FOR KEY SHARE``). A runtime's transactions therefore still run one at a
time, while different projects' runtimes run side by side: they write disjoint
project rows. Claims, transfers, retirement and fenced coordination writes lock
an ownership row ``FOR UPDATE``, which waits for these transactions, so they
stay ordered against each runtime's work and a stale owner never commits after
a new owner's grant. Lease renewal and command submission only update non-key
columns (``FOR NO KEY UPDATE``), which a key-share lock admits: a worker's own
long transaction can no longer starve its heartbeat or hold API acceptance.

Because a runtime's transactions run one at a time, one runaway transaction
would still freeze the whole project. A generous serialization guard,
``EXECUTION_TRANSACTION_SECONDS`` (120 s; the lease no longer depends on it),
abandons one that runs away: on PostgreSQL ``statement_timeout`` and
``idle_in_transaction_session_timeout``, and on every dialect no statement
starts once the transaction is older than that. Large legitimate work commits
in bounded pages instead (for example the search projection repair), so it
stays far below the guard at millions of rows.

A host Work batch takes ``EXECUTION_SQL_KEY`` exclusively and so
still excludes every execution transaction; an older release that takes it
exclusively still runs alone. Rows shared by all projects (global entities and
their search documents) are the exception: the first statement that may write
one takes the shared-row writer key until commit, so execution transactions
write those rows one at a time and cannot deadlock on them with each other.
Host and API connections never take these keys.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
import os
import re
import time
from typing import Any, Iterator

from sqlalchemy import and_, event, exists, false, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import CompileError, DBAPIError, InvalidRequestError, StatementError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import with_loader_criteria
from sqlalchemy.sql import Delete, Insert, Select, Update
from sqlalchemy.sql.elements import BindParameter, ClauseElement, TextClause
from sqlalchemy.sql import visitors
from sqlalchemy.sql.schema import Column, Table

from .advisory_keys import EXECUTION_SQL_KEY
from .database import (
    Base,
    EntityRow,
    SearchDocumentRow,
)
from .deployment_protocol import BUTLER_HOME_PROJECT
from .projection_write import SearchProjectionWrite
from .runtime_coordination import (
    CoordinationConflict,
    LeaseLost,
    ProjectLease,
)

_current_lease: ContextVar[ProjectLease | None] = ContextVar(
    "execution_lease", default=None
)
_observer_reads: ContextVar[bool] = ContextVar(
    "execution_observer_reads", default=False
)


@contextmanager
def observer_reads() -> Iterator[None]:
    """Let the Butler home worker's reads see every project's committed rows.

    Only Butler read tools enter this, and only on the worker that owns
    ``BUTLER_HOME_PROJECT``; ``ExecutionSQLScope._execute`` refuses it on any
    other worker. Inside it, plain SELECTs carry no project criteria and take no
    lock in the projects they read. Row-locking SELECTs and every INSERT,
    UPDATE and DELETE keep the owned-project predicate, so Butler can neither
    lock nor change another project's rows: actions on another project go to
    its owner as commands. Background controllers never run inside it, because
    they assume they see only their own project.
    """

    token = _observer_reads.set(True)
    try:
        yield
    finally:
        _observer_reads.reset(token)


class CommandWrites:
    """The re-runnable command whose route this task (and its children) runs.

    While ``active``, every committed project write in the task's context also
    marks the command's ``runtime_command_writes`` row in the same
    transaction. When the route returns, the runner deactivates it, so a task
    that outlives a finished request never marks it.

    When the route raises instead, the tracker stays active and becomes
    ``failed``: a task the route started may still be writing, and each of
    its commits must then find the record still held by this run, or roll
    back. Once the runner starts settling the request (``settling``), no
    commit of this run can succeed at all, so a request settled as having
    saved nothing never gains a late write, and a re-run cannot apply it twice.
    """

    __slots__ = ("command_id", "project_id", "active", "failed", "settling")

    def __init__(self, command_id: str, project_id: str):
        self.command_id, self.project_id = command_id, project_id
        self.active, self.failed, self.settling = True, False, False

    def release(self) -> None:
        """Let this run's remaining tasks commit untracked, as after success."""
        self.active = self.failed = self.settling = False


class CommandSettled(CoordinationConflict):
    """A late write of a failed request whose outcome was already settled."""


_command_writes: ContextVar[CommandWrites | None] = ContextVar(
    "execution_command_writes", default=None
)


@contextmanager
def tracking_command_writes(
    command_id: str, project_id: str
) -> Iterator[CommandWrites]:
    """Mark this command written by each project write committed in this context."""
    tracker = CommandWrites(command_id, project_id)
    token = _command_writes.set(tracker)
    try:
        yield tracker
    except BaseException:
        # Tasks the route started may outlive it: keep recording their
        # writes, under the stricter check, until the runner settles it.
        tracker.failed = True
        raise
    else:
        tracker.active = False
    finally:
        _command_writes.reset(token)


# Shared before exclusive, in one statement whose order PostgreSQL guarantees.
_BEGIN_LOCKS = (
    "WITH execution AS MATERIALIZED ("
    f"SELECT pg_advisory_xact_lock_shared({EXECUTION_SQL_KEY})) "
    "SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
    "hashtext('nebula:execution-runtime:v1:' || CAST(%s AS text))) FROM execution"
)
GLOBAL_WRITES_KEY = (
    "hashtext(current_schema()), hashtext('nebula:execution-global-writes:v1')"
)
_GLOBAL_TABLES = {"entities", "search_documents"}


# Measured at ~2.7 M entities, a 300k-message chat and a drifted projection,
# the longest legitimate worker transaction was ~16 s; this is a runaway guard.
DEFAULT_EXECUTION_TRANSACTION_SECONDS = 120.0


def _transaction_seconds() -> float:
    default = DEFAULT_EXECUTION_TRANSACTION_SECONDS
    value = os.getenv("NEBULA_V3_EXECUTION_TRANSACTION_SECONDS")
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        # diagnostic-expected: a malformed override keeps the safe default.
        return default
    return seconds if seconds > 0 else 0.0


# Zero disables the bound (tests only).
EXECUTION_TRANSACTION_SECONDS = _transaction_seconds()


class ExecutionTransactionTooLong(CoordinationConflict):
    """A project-scoped transaction outlived its bound and was abandoned.

    Nothing it wrote is committed. Long work must commit in bounded batches,
    because every other transaction of the project waits behind it.
    """


@contextmanager
def execution_context(lease: ProjectLease) -> Iterator[None]:
    """Child tasks inherit this exact incarnation/token, never a later claim."""
    token = _current_lease.set(lease)
    try:
        yield
    finally:
        _current_lease.reset(token)


def current_execution_lease() -> ProjectLease | None:
    return _current_lease.get()


def lock_global_writes(connection) -> None:
    """Hold the shared-row writer key on this execution transaction until commit.

    Writes take it on their own. A transaction that reads a shared row and
    then decides what to write takes it before that read, so another
    project's worker cannot change the row in between.
    """
    if connection.info.get("execution_global_writes", True):
        return
    cursor = connection.connection.cursor()
    try:
        cursor.execute(f"SELECT pg_advisory_xact_lock({GLOBAL_WRITES_KEY})")
    finally:
        cursor.close()
    connection.info["execution_global_writes"] = True


class ExecutionSQLScope:
    def __init__(self, runtime_id: str):
        self.runtime_id = runtime_id

    def install(self, engine) -> None:
        event.listen(engine, "begin", self._begin)
        event.listen(engine, "before_execute", self._execute, retval=True)
        event.listen(engine, "before_cursor_execute", self._driver_execute)
        event.listen(engine, "commit", self._commit)
        event.listen(engine, "rollback", self._rollback)

    @staticmethod
    def _clock_sql(dialect: str) -> str:
        if dialect == "postgresql":
            return "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000 AS BIGINT)"
        return "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"

    def _begin(self, connection) -> None:
        dialect = connection.dialect.name
        placeholder = "%s" if dialect == "postgresql" else "?"
        cursor = connection.connection.cursor()
        try:
            if dialect == "sqlite":
                cursor.execute("BEGIN IMMEDIATE")
            else:
                cursor.execute(_BEGIN_LOCKS, (self.runtime_id,))
                bound = EXECUTION_TRANSACTION_SECONDS
                if bound > 0:
                    milliseconds = str(int(bound * 1000))
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true), "
                        "set_config('idle_in_transaction_session_timeout', %s, true)",
                        (milliseconds, milliseconds),
                    )
            # Key share: claims and transfers (FOR UPDATE) wait for this
            # transaction; heartbeat renewal and command submission do not.
            cursor.execute(
                "SELECT project_id, fencing_token, lease_expires_at_ms "
                "FROM project_ownership WHERE runtime_id = "
                + placeholder
                + " ORDER BY project_id"
                + (" FOR KEY SHARE" if dialect == "postgresql" else ""),
                (self.runtime_id,),
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT " + self._clock_sql(dialect))
            now = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT lease_expires_at_ms FROM runtime_instances WHERE id = "
                + placeholder,
                (self.runtime_id,),
            )
            runtime = cursor.fetchone()
            if runtime is None or runtime[0] <= now:
                raise LeaseLost("execution process lease expired")
            owned = {
                project: int(fence) for project, fence, expiry in rows if expiry > now
            }
            context = _current_lease.get()
            if context is not None:
                if (
                    context.runtime_id != self.runtime_id
                    or owned.get(context.project_id) != context.fencing_token
                ):
                    raise LeaseLost("execution task lost its project fencing token")
                owned = {context.project_id: context.fencing_token}
            connection.info["execution_owned"] = owned
            connection.info["execution_dirty"] = False
            connection.info["execution_began"] = time.monotonic()
            # SQLite's BEGIN IMMEDIATE already admits one writer at a time.
            connection.info["execution_global_writes"] = dialect != "postgresql"
        finally:
            cursor.close()

    @staticmethod
    def _global_rows(table):
        if table.name == "entities":
            return and_(table.c.kind != "engagements", table.c.engagement_id.is_(None))
        return table.c.project_id.is_(None)

    def _may_write_global_rows(self, connection, statement, options, batches):
        marked = options.get("nebula_global_entity")
        if marked is not None:
            return bool(marked)
        criteria = tuple(getattr(statement, "_where_criteria", ()))
        if not criteria or not all(isinstance(batch, Mapping) for batch in batches):
            return True
        table = statement.table
        connection.info["execution_checking"] = True
        try:
            return any(
                connection.scalar(
                    select(literal(1))
                    .select_from(table)
                    .where(*criteria, self._global_rows(table))
                    .limit(1),
                    dict(batch),
                )
                is not None
                for batch in batches
            )
        except (CompileError, StatementError) as error:
            if isinstance(error, DBAPIError):
                raise
            # diagnostic-expected: a statement whose criteria cannot be checked
            # alone is treated as a shared-row write and takes the writer key.
            return True
        finally:
            connection.info["execution_checking"] = False

    @staticmethod
    def _one_of(column, projects, expanding=True):
        if expanding:
            return column.in_(projects)
        # An executemany UPDATE or DELETE (an ORM flush of several rows) cannot
        # carry an expanding IN parameter, so each project is bound on its own.
        if not projects:
            return false()
        return column.in_([literal(project, column.type) for project in projects])

    @classmethod
    def _entity_predicate(cls, projects, expanding=True):
        return or_(
            and_(EntityRow.kind != "engagements", EntityRow.engagement_id.is_(None)),
            cls._one_of(EntityRow.engagement_id, projects, expanding),
            and_(
                EntityRow.kind == "engagements",
                cls._one_of(EntityRow.id, projects, expanding),
            ),
        )

    def _predicate(self, table, projects, expanding=True):
        columns = table.c
        if table.name == "entities":
            return self._entity_predicate(projects, expanding)
        if "engagement_id" in columns:
            return self._one_of(columns.engagement_id, projects, expanding)
        if "project_id" in columns:
            return or_(
                self._one_of(columns.project_id, projects, expanding),
                columns.project_id.is_(None),
            )
        owner = next(
            (name for name in ("run_id", "turn_id", "session_id") if name in columns),
            None,
        )
        if owner is not None:
            return columns[owner].in_(
                select(EntityRow.id).where(
                    self._one_of(EntityRow.engagement_id, projects, expanding)
                )
            )
        return None

    def _execute(self, connection, statement, multiparams, params, options):
        if connection.info.get("execution_checking"):
            return statement, multiparams, params
        # before_execute precedes SQLAlchemy's implicit begin on a fresh raw
        # connection. Load this transaction's ownership before compiling scope.
        if not connection.in_transaction():
            connection.begin()
        began = connection.info.get("execution_began")
        bound = EXECUTION_TRANSACTION_SECONDS
        if began is not None and bound > 0 and time.monotonic() - began > bound:
            raise ExecutionTransactionTooLong(
                f"A project transaction stayed open longer than {bound:g} s and was "
                "abandoned; every other request in the project waits behind one. "
                "Long work must commit in smaller batches."
            )
        owned = connection.info.get("execution_owned", {})
        context = _current_lease.get()
        if context is not None and (
            context.runtime_id != self.runtime_id
            or owned.get(context.project_id) != context.fencing_token
        ):
            raise LeaseLost("execution task lost its project fencing token")
        projects = (context.project_id,) if context is not None else tuple(owned)
        observing = _observer_reads.get()
        if observing and projects != (BUTLER_HOME_PROJECT,):
            raise CoordinationConflict(
                "observer reads belong only to the Butler home worker"
            )
        if isinstance(statement, Select):
            # A row-locking read is a write for fencing: it keeps the owned
            # predicate even while observing, so Butler cannot block another
            # project's worker on its rows.
            observing = observing and statement._for_update_arg is None
            mapped_tables = set()
            roots = [
                *statement._raw_columns,
                *statement._from_obj,
                *(item[0] for item in statement._setup_joins),
            ]
            pending = list(roots)
            while pending:
                part = pending.pop()
                if isinstance(part, Select):
                    continue
                annotations = getattr(part, "_annotations", {})
                mapper = annotations.get("parentmapper") or annotations.get(
                    "parententity"
                )
                table = getattr(mapper, "local_table", None)
                if table is None:
                    table = getattr(
                        getattr(mapper, "mapper", None), "local_table", None
                    )
                if table is not None:
                    mapped_tables.add(table)
                if hasattr(part, "get_children"):
                    pending.extend(part.get_children())
            for node in visitors.iterate(statement):
                if isinstance(node, (Column, Table)):
                    table = node.table if isinstance(node, Column) else node
                    known = (
                        Base.metadata.tables.get(getattr(table, "key", None))
                        if table is not None
                        else None
                    )
                    # Derived columns can belong to a Subquery or Alias. Their
                    # underlying tables/selects are visited below and retain
                    # the same mapped-read checks and loader criteria.
                    annotations = getattr(node, "_annotations", {})
                    if (
                        known is not None
                        and known not in mapped_tables
                        and self._predicate(known, projects) is not None
                        and not (
                            "parententity" in annotations
                            or "parentmapper" in annotations
                            or "entity_namespace" in annotations
                        )
                    ):
                        raise CoordinationConflict(
                            "execution project reads require mapped columns"
                        )
                if not isinstance(node, Select):
                    continue
                if node._propagate_attrs.get("compile_state_plugin") == "orm":
                    continue
                if any(
                    from_clause.is_derived_from(table)
                    for from_clause in node.get_final_froms()
                    for table in Base.metadata.tables.values()
                    if self._predicate(table, projects) is not None
                ):
                    raise CoordinationConflict(
                        "execution reads require mapped project columns"
                    )
            # Observer reads keep the mapped-column checks above, but carry no
            # project criteria: they see projects created mid-turn and avoid an
            # IN list of every project on every mapper.
            for mapper in () if observing else Base.registry.mappers:
                predicate = self._predicate(mapper.local_table, projects)
                if predicate is not None:
                    statement = statement.options(
                        with_loader_criteria(
                            mapper.class_, predicate, include_aliases=True
                        )
                    )
        elif isinstance(statement, (Insert, Update, Delete)):
            table = statement.table
            if table.name == "runtime_chat_bindings" and not isinstance(
                statement, Insert
            ):
                raise CoordinationConflict(
                    "Accepted chat bindings are immutable; retention belongs to the host"
                )
            if table.name in {
                "schema_versions",
                "bootstrap_state",
                "project_create_requests",
                "runtime_instances",
                "project_ownership",
                "runtime_commands",
                "runtime_command_writes",
                "runtime_delivery",
                "device_pairing_offers",
                "runtime_capacity_policy",
                "runtime_capacity_reservations",
                "butler_memory_items",
                "butler_memory_cursors",
                "runtime_index_state",
                "runtime_releases",
                "runtime_processes",
                "deployment_host",
                "project_handoffs",
                "runtime_resources",
                "deployment_receipts",
                "memory_jobs",
                "memory_budget_policies",
                "memory_budget_audit",
                "project_view_leases",
                "project_admission_holds",
            }:
                raise CoordinationConflict(
                    "execution workers cannot mutate host coordination state"
                )
            predicate = self._predicate(table, projects, expanding=not multiparams)
            if predicate is None:
                raise CoordinationConflict(
                    f"execution table has no project authority: {table.name}"
                )
            if isinstance(statement, Insert):
                projection = options.get("nebula_projection_write")
                if projection is not None:
                    if (
                        multiparams
                        or params
                        or not isinstance(projection, SearchProjectionWrite)
                    ):
                        raise CoordinationConflict(
                            "projection writes require immutable literal input"
                        )
                    statement = self._projection_statement(
                        projection, projects, connection.dialect.name
                    )
                    if (
                        self._validate_insert(
                            connection,
                            statement.table,
                            {"id": projection.document_id, **dict(projection.fields)},
                            projects,
                        )
                        is None
                    ):
                        lock_global_writes(connection)
                elif (
                    getattr(statement, "_multi_values", None)
                    or getattr(statement, "select", None) is not None
                    or getattr(statement, "_post_values_clause", None) is not None
                ):
                    raise CoordinationConflict(
                        "execution insert shape requires an explicit project adapter"
                    )
                else:
                    for batch in self._parameter_sets(multiparams, params):
                        if (
                            self._validate_insert(
                                connection,
                                table,
                                self._assigned_values(statement, table, batch),
                                projects,
                            )
                            is None
                            and table.name in _GLOBAL_TABLES
                        ):
                            lock_global_writes(connection)
            else:
                if isinstance(statement, Update):
                    if getattr(statement, "_ordered_values", None):
                        raise CoordinationConflict(
                            "ordered execution updates require an explicit project adapter"
                        )
                    for batch in self._parameter_sets(multiparams, params):
                        self._validate_update(
                            connection,
                            statement,
                            predicate,
                            self._assigned_values(statement, table, batch),
                            batch,
                            projects,
                        )
                if (
                    table.name in _GLOBAL_TABLES
                    and not connection.info.get("execution_global_writes")
                    and self._may_write_global_rows(
                        connection, statement, options, multiparams or [params or {}]
                    )
                ):
                    lock_global_writes(connection)
                statement = statement.where(predicate)
            connection.info["execution_dirty"] = True
        elif isinstance(statement, TextClause):
            first = statement.text.strip().split(None, 1)[0].upper()
            if first not in {"SELECT", "PRAGMA"}:
                raise CoordinationConflict(
                    "execution SQL must use structured project-scoped statements"
                )
            identifiers = set(
                re.findall(r"[A-Za-z_][A-Za-z_0-9]*", statement.text.lower())
            )
            if any(
                table.name in identifiers
                for table in Base.metadata.tables.values()
                if self._predicate(table, projects) is not None
            ):
                raise CoordinationConflict(
                    "textual project reads bypass execution scope"
                )
        else:
            raise CoordinationConflict(
                "execution SQL statement requires an explicit scope adapter"
            )
        return statement, multiparams, params

    def _projection_statement(self, descriptor, projects, dialect):
        from datetime import datetime

        values = dict(descriptor.fields)
        allowed = set(SearchDocumentRow.__table__.c.keys()) - {"id"}
        if set(values) != allowed or any(
            type(value) not in {str, int, datetime, type(None)}
            for value in values.values()
        ):
            raise CoordinationConflict(
                "projection fields must be complete literal values"
            )
        if values["revision"] != descriptor.source_revision or not (
            descriptor.document_id == descriptor.source_id
            or descriptor.document_id.startswith(descriptor.source_id + "::tab::")
        ):
            raise CoordinationConflict("projection identity must match its source")
        project = values["project_id"]
        source_project = or_(
            and_(EntityRow.kind == "engagements", EntityRow.id == project),
            and_(
                EntityRow.kind != "engagements",
                EntityRow.engagement_id.is_(None)
                if project is None
                else EntityRow.engagement_id == project,
            ),
        )
        current = exists(
            select(EntityRow.id).where(
                EntityRow.id == descriptor.source_id,
                EntityRow.revision == descriptor.source_revision,
                source_project,
                self._entity_predicate(projects),
            )
        )
        fields = {"id": descriptor.document_id, **values}
        factory = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = factory(SearchDocumentRow).from_select(
            list(fields),
            select(
                *[
                    literal(value, type_=SearchDocumentRow.__table__.c[key].type)
                    for key, value in fields.items()
                ]
            ).where(current),
        )
        same_project = (
            SearchDocumentRow.project_id.is_(None)
            if project is None
            else SearchDocumentRow.project_id == project
        )
        return statement.on_conflict_do_update(
            index_elements=[SearchDocumentRow.id],
            set_={key: getattr(statement.excluded, key) for key in values},
            where=and_(
                SearchDocumentRow.revision <= statement.excluded.revision, same_project
            ),
        )

    @staticmethod
    def _driver_execute(
        connection, cursor, statement, parameters, context, executemany
    ):
        if context.compiled is None:
            raise CoordinationConflict(
                "raw driver SQL is unavailable in execution workers"
            )

    @staticmethod
    def _parameter_sets(multiparams, params) -> list[Mapping[str, Any]]:
        batches = multiparams or [params or {}]
        for batch in batches:
            if not isinstance(batch, Mapping):
                raise CoordinationConflict("execution statement values must be named")
        return batches

    @staticmethod
    def _assigned_values(statement, table, batch) -> dict[str, Any]:
        """Return the column values one parameter set really writes.

        SQLAlchemy binds a VALUES/SET entry from the execution parameters when
        the parameters carry its bind name (or, for a literal, its column
        key), and adds any other parameter named after a column. A bind whose
        value is missing cannot be validated, so it fails closed.
        """
        values: dict[str, Any] = {}
        for key, value in (getattr(statement, "_values", None) or {}).items():
            name = getattr(key, "key", key)
            if isinstance(value, BindParameter):
                if value.key in batch:
                    value = batch[value.key]
                elif value.unique and name in batch:
                    value = batch[name]
                elif value.required:
                    raise CoordinationConflict(
                        "execution statement values must be bound"
                    )
                else:
                    value = value.effective_value
            values[name] = value
        for name, value in batch.items():
            if name in table.c and name not in values:
                values[name] = value
        return values

    def _validate_update(
        self, connection, statement, predicate, values, parameters, projects
    ):
        table = statement.table
        owner = next(
            (
                name
                for name in (
                    "engagement_id",
                    "project_id",
                    "run_id",
                    "turn_id",
                    "session_id",
                )
                if name in table.c
            ),
            None,
        )
        if owner is None:
            raise CoordinationConflict(
                f"execution table has no project authority: {table.name}"
            )
        assigned = values.get(owner)
        payload = values.get("payload")
        if table.name == "entities" and isinstance(payload, ClauseElement):
            raise CoordinationConflict(
                "execution entity payloads require literal documents"
            )
        if owner not in values and not (
            table.name == "entities"
            and isinstance(payload, dict)
            and "engagement_id" in payload
        ):
            return
        if assigned is not None and not isinstance(assigned, str):
            raise CoordinationConflict(
                "execution owner changes require literal identifiers"
            )
        # Evaluate exactly the rows this UPDATE will touch: its WHERE criteria
        # can bind values (an ORM flush binds the primary key) that travel in
        # this parameter set, not in the statement.
        check = select(table.c[owner]).where(*statement._where_criteria, predicate)
        try:
            check.compile(dialect=connection.dialect).construct_params(parameters)
        except InvalidRequestError as exc:
            raise CoordinationConflict(
                "execution update rows cannot be determined"
            ) from exc
        connection.info["execution_checking"] = True
        try:
            current = list(connection.scalars(check, parameters))
        finally:
            connection.info["execution_checking"] = False
        for project in current:
            if owner in values and assigned != project:
                raise LeaseLost(
                    "execution updates cannot move records between projects"
                )
            if (
                table.name == "entities"
                and isinstance(payload, dict)
                and payload.get("engagement_id", project) != project
            ):
                raise LeaseLost("execution payload cannot change project authority")

    def _validate_insert(
        self, connection, table, values: dict[str, Any], projects
    ) -> str | None:
        """Return the inserted row's project, or None for a row shared by all."""
        if table.name == "entities":
            if not values.get("kind") or not values.get("id"):
                raise CoordinationConflict(
                    "execution entity insert must identify its kind and owner"
                )
            project = (
                values.get("id")
                if values.get("kind") == "engagements"
                else values.get("engagement_id")
            )
            payload = values.get("payload")
            if (
                isinstance(payload, dict)
                and payload.get("engagement_id", project) != project
            ):
                raise LeaseLost("execution payload cannot change project authority")
            # Global configuration retains optimistic revisions. Shared host
            # services, not each execution generation, own their lifecycle.
            if project is None:
                return None
        elif "engagement_id" in table.c:
            project = values.get("engagement_id")
        elif "project_id" in table.c:
            project = values.get("project_id")
            if project is None:
                return None
        else:
            key = next(
                (
                    name
                    for name in ("run_id", "turn_id", "session_id")
                    if name in table.c
                ),
                None,
            )
            if key is None or not isinstance(values.get(key), str) or not values[key]:
                raise CoordinationConflict("execution insert lacks its owning record")
            connection.info["execution_checking"] = True
            try:
                project = connection.scalar(
                    select(EntityRow.engagement_id).where(EntityRow.id == values[key])
                )
            finally:
                connection.info["execution_checking"] = False
        if project not in projects:
            raise LeaseLost("execution insert belongs to an unowned project")
        return project

    def _commit(self, connection) -> None:
        if not connection.info.get("execution_dirty"):
            return
        cursor = connection.connection.cursor()
        placeholder = "%s" if connection.dialect.name == "postgresql" else "?"
        try:
            cursor.execute("SELECT " + self._clock_sql(connection.dialect.name))
            now = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT lease_expires_at_ms FROM runtime_instances WHERE id = "
                + placeholder,
                (self.runtime_id,),
            )
            runtime = cursor.fetchone()
            if runtime is None or runtime[0] <= now:
                raise LeaseLost("execution process lease expired before commit")
            for project, fence in connection.info["execution_owned"].items():
                cursor.execute(
                    "SELECT runtime_id, fencing_token, lease_expires_at_ms FROM project_ownership WHERE project_id = "
                    + placeholder,
                    (project,),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or row[0] != self.runtime_id
                    or row[1] != fence
                    or row[2] <= now
                ):
                    raise LeaseLost("execution project lease expired before commit")
            tracker = _command_writes.get()
            context = _current_lease.get()
            if (
                tracker is not None
                and tracker.active
                and context is not None
                and context.project_id == tracker.project_id
            ):
                if tracker.settling:
                    raise CommandSettled(
                        "this request's outcome was settled; a late write of it is discarded"
                    )
                # Committed atomically with these writes: once set, the
                # command is never run again after its worker dies.
                cursor.execute(
                    "UPDATE runtime_command_writes SET wrote = "
                    + placeholder
                    + " WHERE command_id = "
                    + placeholder
                    + " AND project_id = "
                    + placeholder
                    + " AND runtime_id = "
                    + placeholder
                    + " AND fencing_token = "
                    + placeholder
                    + " AND wrote = "
                    + placeholder,
                    (
                        True,
                        tracker.command_id,
                        tracker.project_id,
                        self.runtime_id,
                        context.fencing_token,
                        False,
                    ),
                )
                if tracker.failed:
                    # The route already failed. Its record must still be this
                    # run's: one the runner settled (and deleted) or that a
                    # new run replaced no longer admits this write.
                    cursor.execute(
                        "SELECT wrote FROM runtime_command_writes WHERE command_id = "
                        + placeholder
                        + " AND project_id = "
                        + placeholder
                        + " AND runtime_id = "
                        + placeholder
                        + " AND fencing_token = "
                        + placeholder,
                        (
                            tracker.command_id,
                            tracker.project_id,
                            self.runtime_id,
                            context.fencing_token,
                        ),
                    )
                    record = cursor.fetchone()
                    if record is None or not record[0] or tracker.settling:
                        raise CommandSettled(
                            "this request's outcome was settled; a late write of it is discarded"
                        )
        finally:
            cursor.close()

    @staticmethod
    def _rollback(connection) -> None:
        # A statement interrupted by cancellation, a timeout or a lost backend
        # leaves the connection invalidated. Its driver connection is discarded,
        # the pool clears .info when it reconnects and _begin rebuilds ownership
        # for the next transaction. Reading .info now would raise
        # PendingRollbackError in place of the original exception.
        if connection.invalidated:
            return
        connection.info.pop("execution_owned", None)
        connection.info.pop("execution_dirty", None)
        connection.info.pop("execution_began", None)
