"""Project isolation and SQL fencing for a versioned execution process.

API and host databases never install these hooks. An execution database opens
only after the host migrates and registers its process incarnation. All of its
transactions take the same lock order, including the raw-connection ledger
writers. This deliberately serializes execution SQL transactions initially;
public read traffic uses independent API pools.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
import re
from typing import Any, Iterator

from sqlalchemy import and_, event, exists, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import with_loader_criteria
from sqlalchemy.sql import Delete, Insert, Select, Update
from sqlalchemy.sql.elements import BindParameter, TextClause
from sqlalchemy.sql import visitors
from sqlalchemy.sql.schema import Column, Table

from .database import (
    Base,
    EntityRow,
    SearchDocumentRow,
)
from .projection_write import SearchProjectionWrite
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease

_current_lease: ContextVar[ProjectLease | None] = ContextVar(
    "execution_lease", default=None
)


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
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('nebula:execution-sql:v1'))"
                )
            cursor.execute(
                "SELECT project_id, fencing_token, lease_expires_at_ms "
                "FROM project_ownership WHERE runtime_id = "
                + placeholder
                + " ORDER BY project_id"
                + (" FOR UPDATE" if dialect == "postgresql" else ""),
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
        finally:
            cursor.close()

    @staticmethod
    def _entity_predicate(projects):
        return or_(
            and_(EntityRow.kind != "engagements", EntityRow.engagement_id.is_(None)),
            EntityRow.engagement_id.in_(projects),
            and_(EntityRow.kind == "engagements", EntityRow.id.in_(projects)),
        )

    def _predicate(self, table, projects):
        columns = table.c
        if table.name == "entities":
            return self._entity_predicate(projects)
        if "engagement_id" in columns:
            return columns.engagement_id.in_(projects)
        if "project_id" in columns:
            return or_(columns.project_id.in_(projects), columns.project_id.is_(None))
        owner = next(
            (name for name in ("run_id", "turn_id", "session_id") if name in columns),
            None,
        )
        if owner is not None:
            return columns[owner].in_(
                select(EntityRow.id).where(EntityRow.engagement_id.in_(projects))
            )
        return None

    def _execute(self, connection, statement, multiparams, params, options):
        if connection.info.get("execution_checking"):
            return statement, multiparams, params
        # before_execute precedes SQLAlchemy's implicit begin on a fresh raw
        # connection. Load this transaction's ownership before compiling scope.
        if not connection.in_transaction():
            connection.begin()
        owned = connection.info.get("execution_owned", {})
        context = _current_lease.get()
        if context is not None and (
            context.runtime_id != self.runtime_id
            or owned.get(context.project_id) != context.fencing_token
        ):
            raise LeaseLost("execution task lost its project fencing token")
        projects = (context.project_id,) if context is not None else tuple(owned)
        if isinstance(statement, Select):
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
            for mapper in Base.registry.mappers:
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
            }:
                raise CoordinationConflict(
                    "execution workers cannot mutate host coordination state"
                )
            predicate = self._predicate(table, projects)
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
                    self._validate_insert(
                        connection,
                        statement.table,
                        {"id": projection.document_id, **dict(projection.fields)},
                        projects,
                    )
                elif (
                    getattr(statement, "_multi_values", None)
                    or getattr(statement, "select", None) is not None
                    or getattr(statement, "_post_values_clause", None) is not None
                ):
                    raise CoordinationConflict(
                        "execution insert shape requires an explicit project adapter"
                    )
                else:
                    values = {
                        getattr(key, "key", key): value.value
                        if isinstance(value, BindParameter)
                        else value
                        for key, value in (
                            getattr(statement, "_values", None) or {}
                        ).items()
                    }
                    batches = multiparams or [params or {}]
                    for batch in batches:
                        if not isinstance(batch, Mapping):
                            raise CoordinationConflict(
                                "execution insert values must be named"
                            )
                        self._validate_insert(
                            connection, table, {**values, **batch}, projects
                        )
            else:
                if isinstance(statement, Update):
                    if getattr(statement, "_ordered_values", None):
                        raise CoordinationConflict(
                            "ordered execution updates require an explicit project adapter"
                        )
                    values = {
                        getattr(key, "key", key): value.value
                        if isinstance(value, BindParameter)
                        else value
                        for key, value in (
                            getattr(statement, "_values", None) or {}
                        ).items()
                    }
                    for batch in multiparams or [params or {}]:
                        self._validate_update(
                            connection,
                            statement,
                            predicate,
                            {**values, **batch},
                            projects,
                        )
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

    def _validate_update(self, connection, statement, predicate, values, projects):
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
            return
        assigned = values.get(owner)
        payload = values.get("payload")
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
        connection.info["execution_checking"] = True
        try:
            current = list(
                connection.scalars(
                    select(table.c[owner]).where(*statement._where_criteria, predicate)
                )
            )
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
    ) -> None:
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
                return
        elif "engagement_id" in table.c:
            project = values.get("engagement_id")
        elif "project_id" in table.c:
            project = values.get("project_id")
            if project is None:
                return
        else:
            key = next(
                (
                    name
                    for name in ("run_id", "turn_id", "session_id")
                    if name in table.c
                ),
                None,
            )
            if key is None or not values.get(key):
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
