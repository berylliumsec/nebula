"""Validated repositories and the durable append-only run-event ledger."""

from __future__ import annotations

from .diagnostics import record_caught_exception

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence, TypeVar, cast
from uuid import uuid4

if TYPE_CHECKING:
    from .application_model.service import ApplicationModelService

from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import (
    Database,
    EntityRow,
    OperationEventRow,
    ProviderTurnQueueRow,
    RunBudgetCounterRow,
    RunEventRow,
    SearchDocumentRow,
)
from .domain import (
    Artifact,
    ChatTurn,
    ENTITY_MODEL_BY_KIND,
    Entity,
    OperationEvent,
    ToolCall,
    ToolCallOrigin,
    RunEvent,
    entity_engagement_id,
    utc_now,
)

EntityT = TypeVar("EntityT", bound=Entity)


def _recovery_pending(payload: dict[str, Any]) -> bool:
    """An interrupted turn still blocks its conversation until recovery ends.

    Mirrors ``ChatService._turn_is_pending`` for a stored ``chat_turns`` row.
    """

    snapshot = payload.get("request_snapshot")
    recovery = snapshot.get("recovery") if isinstance(snapshot, dict) else None
    return isinstance(recovery, dict) and bool(
        recovery.get("required") or recovery.get("automatic_retry_pending")
    )


def not_temporary_chat_session() -> ColumnElement[bool]:
    """SQL predicate that excludes "Ask Nebula" popup sessions from chat rows.

    ``list_entities``, ``count``, ``overview`` and the federated search
    projection all share this definition so a temporary conversation is either
    hidden everywhere or nowhere.
    """

    return func.coalesce(
        EntityRow.payload["metadata"]["temporary_assistant"].as_boolean(),
        False,
    ).is_(False)


def _check_model_update(model):
    if model.entity_kind.startswith("application_model_"):
        raise ValueError("Experimental application-model records are retired")


class StorageError(RuntimeError):
    pass


class NotFoundError(StorageError):
    pass


class ConflictError(StorageError):
    pass


class CorruptRecordError(StorageError):
    pass


class RunBudgetExceededError(StorageError):
    pass


def _dump_entity(entity: Entity) -> dict[str, Any]:
    return entity.model_dump(mode="json")


_AUTOMATION_ENTITY_KINDS = frozenset(
    {
        "browser_automation_leases",
        "browser_commands",
        "browser_proxy_rules",
    }
)

_CHAT_SESSION_ID_ENTITY_KINDS = frozenset(
    {
        "chat_bookmarks",
        "chat_decisions",
        "chat_goals",
        "chat_messages",
        "chat_queues",
        "chat_read_cursors",
        "chat_schedules",
        "chat_turns",
    }
)

_CHAT_SESSION_REFERENCE_ENTITY_KINDS = frozenset(
    {
        "approvals",
        "harness_interactions",
        "harness_turns",
        "native_checkpoints",
        "native_hook_executions",
        "tool_calls",
        "workspace_provenance_observations",
    }
)


def _automation_lookup_fields(entity: Entity) -> dict[str, Any]:
    """Return indexed projections for the run-scoped browser entity family."""

    if entity.entity_kind not in _AUTOMATION_ENTITY_KINDS:
        return {}
    status = getattr(entity, "status", None)
    return {
        "automation_run_id": getattr(entity, "run_id", None),
        "automation_session_id": getattr(entity, "session_id", None),
        "automation_status": getattr(status, "value", status),
        "automation_expires_at": getattr(entity, "expires_at", None),
    }


def _chat_lookup_fields(entity: Entity) -> dict[str, Any]:
    """Return the indexed exact-conversation projection for known chat records."""

    if entity.entity_kind in _CHAT_SESSION_ID_ENTITY_KINDS:
        return {"chat_session_id": getattr(entity, "session_id", None)}
    if entity.entity_kind in _CHAT_SESSION_REFERENCE_ENTITY_KINDS:
        return {"chat_session_id": getattr(entity, "chat_session_id", None)}
    return {}


def _entity_lookup_fields(entity: Entity) -> dict[str, Any]:
    """Return every denormalized lookup field maintained for an entity row."""

    return {**_automation_lookup_fields(entity), **_chat_lookup_fields(entity)}


PayloadFilterValue = str | int | Sequence[str] | None


def _entity_order(newest_first: bool) -> tuple[Any, Any]:
    """Return the stable ``(created_at, id)`` ordering for entity pages."""

    if newest_first:
        return (EntityRow.created_at.desc(), EntityRow.id.desc())
    return (EntityRow.created_at, EntityRow.id)


def _row_to_entity(row: EntityRow, expected: type[EntityT] | None = None) -> EntityT:
    model = ENTITY_MODEL_BY_KIND.get(row.kind)
    if model is None:
        raise CorruptRecordError(f"unknown stored entity kind: {row.kind}")
    if expected is not None and model is not expected:
        raise CorruptRecordError(
            f"record {row.id} is {model.__name__}, expected {expected.__name__}"
        )
    try:
        return cast(EntityT, model.model_validate(row.payload))
    except Exception as exc:
        record_caught_exception(
            "storage",
            "storage.storage.caught_failure_001",
            "A handled storage operation raised an exception.",
            exc,
            stage="storage",
        )
        raise CorruptRecordError(f"record {row.id} failed validation") from exc


class StoreTransaction:
    """A unit-of-work used when an operation must commit all entities together."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, entity: Entity) -> Entity:
        row = EntityRow(
            id=entity.id,
            kind=entity.entity_kind,
            engagement_id=entity_engagement_id(entity),
            revision=entity.revision,
            payload=_dump_entity(entity),
            **_entity_lookup_fields(entity),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_002",
                "A handled storage operation raised an exception.",
                exc,
                stage="storage",
            )
            raise ConflictError(f"entity already exists: {entity.id}") from exc
        from .search import upsert_search_document

        upsert_search_document(self.session, row)
        self.session.flush()
        return entity

    def add_all(self, entities: Sequence[Entity]) -> Sequence[Entity]:
        for entity in entities:
            self.add(entity)
        return entities

    def update(
        self,
        model: type[EntityT],
        entity_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> EntityT:
        """Apply an optimistic entity update inside this unit of work."""

        _check_model_update(model)
        protected = {"id", "created_at", "updated_at", "revision"}.intersection(changes)
        if protected:
            raise ValueError(f"cannot patch protected fields: {sorted(protected)}")
        row = self.session.get(EntityRow, entity_id)
        if row is None or row.kind != model.entity_kind:
            raise NotFoundError(f"{model.entity_kind} entity not found: {entity_id}")
        if expected_revision is not None and row.revision != expected_revision:
            raise ConflictError(
                f"revision conflict: expected {expected_revision}, found {row.revision}"
            )
        current = _row_to_entity(row, model)
        payload = current.model_dump(mode="python")
        payload.update(changes)
        payload["id"] = current.id
        payload["created_at"] = current.created_at
        payload["updated_at"] = utc_now()
        payload["revision"] = current.revision + 1
        updated = model.model_validate(payload)
        result = self.session.execute(
            update(EntityRow)
            .where(
                EntityRow.id == entity_id,
                EntityRow.kind == model.entity_kind,
                EntityRow.revision == current.revision,
            )
            .values(
                payload=_dump_entity(updated),
                engagement_id=entity_engagement_id(updated),
                **_entity_lookup_fields(updated),
                revision=updated.revision,
                updated_at=updated.updated_at,
            )
        )
        if result.rowcount != 1:
            raise ConflictError(
                f"entity {entity_id} changed while the update was in progress"
            )
        self.session.flush()
        refreshed = self.session.get(EntityRow, entity_id)
        if refreshed is not None:
            from .search import upsert_search_document

            upsert_search_document(self.session, refreshed)
        return updated

    def delete(
        self,
        model: type[Entity],
        entity_id: str,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Delete one revision-guarded entity inside the current unit of work."""

        row = self.session.get(EntityRow, entity_id)
        if row is None or row.kind != model.entity_kind:
            raise NotFoundError(f"{model.entity_kind} entity not found: {entity_id}")
        if expected_revision is not None and row.revision != expected_revision:
            raise ConflictError(
                f"revision conflict: expected {expected_revision}, found {row.revision}"
            )
        self.session.delete(row)
        document = self.session.get(SearchDocumentRow, entity_id)
        if document is not None:
            self.session.delete(document)
        for child in self.session.scalars(
            select(SearchDocumentRow).where(
                SearchDocumentRow.id.like(f"{entity_id}::tab::%")
            )
        ):
            self.session.delete(child)
        self.session.flush()

    def append_operation_event(
        self,
        operation_id: str,
        operation_kind: str,
        engagement_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> OperationEvent:
        """Append a receipt in the same transaction as its authoritative state."""

        if not all((operation_id, operation_kind, engagement_id, event_type)):
            raise ValueError("operation event identifiers and type are required")
        existing = None
        if idempotency_key:
            existing = self.session.scalar(
                select(OperationEventRow).where(
                    OperationEventRow.operation_id == operation_id,
                    OperationEventRow.idempotency_key == idempotency_key,
                )
            )
        if existing is not None:
            existing_time = existing.occurred_at
            if existing_time.tzinfo is None:
                existing_time = existing_time.replace(tzinfo=timezone.utc)
            event = OperationEvent(
                id=existing.id,
                operation_id=existing.operation_id,
                operation_kind=existing.operation_kind,
                engagement_id=existing.engagement_id,
                sequence=existing.sequence,
                event_type=existing.event_type,
                payload=existing.payload,
                actor_id=existing.actor_id,
                occurred_at=existing_time,
                idempotency_key=existing.idempotency_key,
            )
            if (
                event.operation_kind != operation_kind
                or event.engagement_id != engagement_id
                or event.event_type != event_type
                or event.payload != (payload or {})
                or event.actor_id != actor_id
            ):
                raise ConflictError(
                    "idempotency key was reused for a different operation event"
                )
            return event
        last_sequence = self.session.scalar(
            select(func.max(OperationEventRow.sequence)).where(
                OperationEventRow.operation_id == operation_id
            )
        )
        event = OperationEvent(
            operation_id=operation_id,
            operation_kind=operation_kind,
            engagement_id=engagement_id,
            sequence=int(last_sequence or 0) + 1,
            event_type=event_type,
            payload=payload or {},
            actor_id=actor_id,
            occurred_at=occurred_at or utc_now(),
            idempotency_key=idempotency_key,
        )
        self.session.add(OperationEventRow(**event.model_dump(mode="python")))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("operation event already exists") from exc
        return event


class NebulaStore:
    """Persistence boundary for typed Nebula entities and run events."""

    def __init__(self, database: Database | str | Path) -> None:
        self.application_model_service: ApplicationModelService | None = None
        self.database = (
            database if isinstance(database, Database) else Database(database)
        )

    def _begin_run_write(self, connection: Any, run_id: str) -> None:
        """Serialize sequence assignment for one run on supported databases."""

        dialect = self.database.engine.dialect.name
        if dialect == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            return
        connection.begin()
        if dialect == "postgresql":
            # Run events do not require a matching AgentRun row at the storage
            # boundary, so a row lock is not always available. A transaction-
            # scoped advisory lock keeps MAX(sequence) + 1 safe per run without
            # blocking unrelated run ledgers.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:run_id))"),
                {"run_id": run_id},
            )

    @contextmanager
    def transaction(self) -> Iterator[StoreTransaction]:
        with self.database.session() as session:
            yield StoreTransaction(session)

    def create(self, entity: EntityT) -> EntityT:
        with self.transaction() as transaction:
            transaction.add(entity)
        return entity

    def list_session_entities(
        self,
        model: type[EntityT],
        session_id: str,
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[EntityT]:
        """Return one conversation's records of ``model``, oldest first, filtered in SQL.

        ``list_entities`` pages a whole kind oldest first, so a lookup that reads
        its first 1,000-row page and filters by ``session_id`` in Python stops
        seeing newer conversations once Core holds that many records of the
        kind. Per-conversation lookups must filter in the database instead.
        """

        statement = select(EntityRow).where(
            EntityRow.kind == model.entity_kind,
            EntityRow.chat_session_id == session_id,
        )
        if statuses is not None:
            statement = statement.where(
                EntityRow.payload["status"].as_string().in_(list(statuses))
            )
        statement = statement.order_by(EntityRow.created_at, EntityRow.id)
        with self.database.session() as session:
            return [
                model.model_validate(row.payload) for row in session.scalars(statement)
            ]

    def list_tool_call_artifacts(
        self, engagement_id: str, tool_call_id: str
    ) -> list[Artifact]:
        """Return the artifacts one tool call recorded, oldest first."""

        statement = (
            select(EntityRow)
            .where(
                EntityRow.kind == Artifact.entity_kind,
                EntityRow.engagement_id == engagement_id,
                EntityRow.payload["metadata"]["tool_call_id"].as_string()
                == tool_call_id,
            )
            .order_by(EntityRow.created_at, EntityRow.id)
        )
        with self.database.session() as session:
            return [
                Artifact.model_validate(row.payload)
                for row in session.scalars(statement)
            ]

    def has_entity_with_metadata(
        self, model: type[Entity], key: str, value: str
    ) -> bool:
        """Report whether any ``model`` row carries ``metadata[key] == value``.

        Checked in SQL so the answer does not depend on how many rows of the
        kind exist; a first-page scan stops seeing newer rows past 1,000.
        """

        statement = select(
            exists().where(
                EntityRow.kind == model.entity_kind,
                EntityRow.payload["metadata"][key].as_string() == value,
            )
        )
        with self.database.session() as session:
            return bool(session.scalar(statement))

    def find_entity(
        self, model: type[EntityT], field: str, value: str
    ) -> EntityT | None:
        """Return the oldest ``model`` row whose top-level ``field`` equals ``value``.

        Filtered in SQL so the lookup does not depend on how many rows of the
        kind exist; a first-page scan stops seeing newer rows past 1,000.
        """

        statement = (
            select(EntityRow)
            .where(
                EntityRow.kind == model.entity_kind,
                EntityRow.payload[field].as_string() == value,
            )
            .order_by(EntityRow.created_at, EntityRow.id)
            .limit(1)
        )
        with self.database.session() as session:
            row = session.scalar(statement)
            return model.model_validate(row.payload) if row is not None else None

    def create_many(self, entities: list[Entity]) -> list[Entity]:
        with self.transaction() as transaction:
            transaction.add_all(entities)
        return entities

    def create_with_event(
        self,
        entity: EntityT,
        *,
        run_id: str,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[EntityT, RunEvent]:
        """Atomically create an entity and append its initial audit event."""

        if not run_id or not event_type:
            raise ValueError("run_id and event_type are required")
        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, run_id)
            connection.execute(
                insert(EntityRow).values(
                    id=entity.id,
                    kind=entity.entity_kind,
                    engagement_id=entity_engagement_id(entity),
                    revision=entity.revision,
                    payload=_dump_entity(entity),
                    **_entity_lookup_fields(entity),
                    created_at=entity.created_at,
                    updated_at=entity.updated_at,
                )
            )
            event = self._next_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=event_payload,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            connection.execute(
                insert(RunEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return entity, event
        except IntegrityError as exc:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_003",
                "A handled storage operation raised an exception.",
                exc,
                stage="storage",
            )
            connection.rollback()
            raise ConflictError(
                f"entity or initial run event already exists: {entity.id}"
            ) from exc
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_004",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_with_operation_event(
        self,
        entity: EntityT,
        *,
        operation_id: str,
        operation_kind: str,
        engagement_id: str,
        event_type: str,
        event_payload: dict[str, Any],
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[EntityT, OperationEvent]:
        """Atomically create an entity and its first durable workflow event."""

        if not all((operation_id, operation_kind, engagement_id, event_type)):
            raise ValueError("operation event identifiers and type are required")
        if entity_engagement_id(entity) != engagement_id:
            raise ValueError("operation event engagement must own the entity")
        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, f"operation:{operation_id}")
            connection.execute(
                insert(EntityRow).values(
                    id=entity.id,
                    kind=entity.entity_kind,
                    engagement_id=engagement_id,
                    revision=entity.revision,
                    payload=_dump_entity(entity),
                    **_entity_lookup_fields(entity),
                    created_at=entity.created_at,
                    updated_at=entity.updated_at,
                )
            )
            event = self._next_operation_event(
                connection,
                operation_id=operation_id,
                operation_kind=operation_kind,
                engagement_id=engagement_id,
                event_type=event_type,
                payload=event_payload,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                occurred_at=occurred_at,
            )
            connection.execute(
                insert(OperationEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return entity, event
        except IntegrityError as exc:
            record_caught_exception(
                "storage",
                "storage.storage.browser_operation_conflict",
                "A browser workflow create conflicted with durable state.",
                exc,
                stage="storage",
            )
            connection.rollback()
            raise ConflictError(
                f"entity or operation event already exists: {entity.id}"
            ) from exc
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.browser_operation_failed",
                "A browser workflow create failed.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve_tool_call(self, call: ToolCall) -> ToolCall:
        """Atomically reserve one durable run tool-call slot and create the call."""

        connection = self.database.engine.connect()
        try:
            if self.database.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.begin()
            existing = (
                connection.execute(select(EntityRow).where(EntityRow.id == call.id))
                .mappings()
                .first()
            )
            if existing is not None:
                connection.commit()
                row = EntityRow(
                    id=existing["id"],
                    kind=existing["kind"],
                    engagement_id=existing["engagement_id"],
                    revision=existing["revision"],
                    payload=existing["payload"],
                    created_at=existing["created_at"],
                    updated_at=existing["updated_at"],
                )
                return _row_to_entity(row, ToolCall)

            owner_kind = (
                ChatTurn.entity_kind if call.origin == ToolCallOrigin.CHAT else "runs"
            )
            run = (
                connection.execute(
                    select(EntityRow)
                    .where(
                        EntityRow.id == call.run_id,
                        EntityRow.kind == owner_kind,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if run is None:
                raise NotFoundError(
                    f"tool-call owner is required before execution: {call.run_id}"
                )
            # PostgreSQL readers do not block on the run row until the
            # ``FOR UPDATE`` statement above. A concurrent reservation may
            # therefore have committed this deterministic call ID while this
            # transaction waited. Recheck before consuming budget.
            existing = (
                connection.execute(select(EntityRow).where(EntityRow.id == call.id))
                .mappings()
                .first()
            )
            if existing is not None:
                connection.commit()
                row = EntityRow(
                    id=existing["id"],
                    kind=existing["kind"],
                    engagement_id=existing["engagement_id"],
                    revision=existing["revision"],
                    payload=existing["payload"],
                    created_at=existing["created_at"],
                    updated_at=existing["updated_at"],
                )
                return _row_to_entity(row, ToolCall)
            artifact_query = call.metadata.get("budget_class") == "artifact_query"
            budget_field = (
                "max_artifact_queries" if artifact_query else "max_tool_calls"
            )
            maximum_value = (
                run["payload"].get(budget_field)
                if call.origin == ToolCallOrigin.CHAT
                else run["payload"].get("budget", {}).get(budget_field)
            )
            maximum = int(maximum_value) if maximum_value is not None else None
            counter = (
                connection.execute(
                    select(RunBudgetCounterRow)
                    .where(RunBudgetCounterRow.run_id == call.run_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            counter_field = "artifact_queries" if artifact_query else "tool_calls"
            current = int(counter[counter_field]) if counter else 0
            if maximum is not None and current >= maximum:
                raise RunBudgetExceededError(
                    f"run {call.run_id} exhausted its "
                    f"{'artifact-query' if artifact_query else 'tool-call'} budget ({maximum})"
                )
            if counter:
                connection.execute(
                    update(RunBudgetCounterRow)
                    .where(RunBudgetCounterRow.run_id == call.run_id)
                    .values(**{counter_field: current + 1, "updated_at": utc_now()})
                )
            else:
                connection.execute(
                    insert(RunBudgetCounterRow).values(
                        run_id=call.run_id,
                        tool_calls=0 if artifact_query else 1,
                        artifact_queries=1 if artifact_query else 0,
                        input_tokens=0,
                        output_tokens=0,
                        cost_microusd=0,
                        updated_at=utc_now(),
                    )
                )
            connection.execute(
                insert(EntityRow).values(
                    id=call.id,
                    kind=call.entity_kind,
                    engagement_id=call.engagement_id,
                    revision=call.revision,
                    payload=_dump_entity(call),
                    **_entity_lookup_fields(call),
                    created_at=call.created_at,
                    updated_at=call.updated_at,
                )
            )
            connection.commit()
            return call
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_005",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, model: type[EntityT], entity_id: str) -> EntityT:
        with self.database.session() as session:
            row = session.get(EntityRow, entity_id)
            if row is None or row.kind != model.entity_kind:
                raise NotFoundError(
                    f"{model.entity_kind} entity not found: {entity_id}"
                )
            return _row_to_entity(row, model)

    def get_by_kind(self, kind: str, entity_id: str) -> Entity:
        model = ENTITY_MODEL_BY_KIND.get(kind)
        if model is None:
            raise NotFoundError(f"unknown entity kind: {kind}")
        return self.get(model, entity_id)

    def list_entities(
        self,
        model: type[EntityT],
        *,
        engagement_id: str | None = None,
        automation_run_id: str | None = None,
        automation_session_id: str | None = None,
        automation_status: str | None = None,
        automation_expires_before: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
        include_temporary: bool = False,
        newest_first: bool = False,
    ) -> list[EntityT]:
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        statement = select(EntityRow).where(EntityRow.kind == model.entity_kind)
        if model.entity_kind == "chat_sessions" and not include_temporary:
            statement = statement.where(not_temporary_chat_session())
        if engagement_id is not None:
            statement = statement.where(EntityRow.engagement_id == engagement_id)
        if automation_run_id is not None:
            statement = statement.where(
                EntityRow.automation_run_id == automation_run_id
            )
        if automation_session_id is not None:
            statement = statement.where(
                EntityRow.automation_session_id == automation_session_id
            )
        if automation_status is not None:
            statement = statement.where(
                EntityRow.automation_status == automation_status
            )
        if automation_expires_before is not None:
            statement = statement.where(
                EntityRow.automation_expires_at <= automation_expires_before
            )
        statement = (
            statement.order_by(*_entity_order(newest_first)).offset(offset).limit(limit)
        )
        with self.database.session() as session:
            return [_row_to_entity(row, model) for row in session.scalars(statement)]

    def find_entities(
        self,
        model: type[EntityT],
        filters: Mapping[str, PayloadFilterValue],
        *,
        engagement_id: str | None = None,
        automation_run_id: str | None = None,
        automation_session_id: str | None = None,
        automation_status: str | Sequence[str] | None = None,
        offset: int = 0,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[EntityT]:
        """Return the ``model`` rows whose payload matches ``filters``, filtered in SQL.

        ``list_entities`` pages a whole kind oldest first, so a lookup that
        reads its first 1,000-row page and filters in Python stops seeing newer
        rows once Core holds that many records of the kind. Each filter names a
        top-level payload field, or ``"metadata.<key>"`` for one metadata
        entry; a string must equal the field, an integer must equal a field
        the schema types as an integer, a sequence lists the accepted strings
        and ``None`` requires the field to be null or absent. The keyword
        arguments match the indexed projections the table maintains for the
        browser automation kinds; prefer them to the equivalent payload
        field. Rows come back oldest first unless ``newest_first`` is
        set, and ``limit=None`` returns every match.
        """

        if offset < 0:
            raise ValueError("offset cannot be negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        statement = select(EntityRow).where(EntityRow.kind == model.entity_kind)
        if engagement_id is not None:
            statement = statement.where(EntityRow.engagement_id == engagement_id)
        if automation_run_id is not None:
            statement = statement.where(
                EntityRow.automation_run_id == automation_run_id
            )
        if automation_session_id is not None:
            statement = statement.where(
                EntityRow.automation_session_id == automation_session_id
            )
        if isinstance(automation_status, str):
            statement = statement.where(
                EntityRow.automation_status == automation_status
            )
        elif automation_status is not None:
            statuses = list(automation_status)
            if not statuses:
                return []
            statement = statement.where(EntityRow.automation_status.in_(statuses))
        for field, value in filters.items():
            column: Any = EntityRow.payload
            for part in field.split("."):
                column = column[part]
            if isinstance(value, bool):
                raise TypeError(f"boolean payload filters are not supported: {field}")
            column = column.as_string()
            if isinstance(value, int):
                # Compare the text form. Casting the field to an integer would
                # make PostgreSQL fail the query on any non-numeric value.
                statement = statement.where(column.cast(String) == str(value))
                continue
            if value is None:
                statement = statement.where(column.is_(None))
            elif isinstance(value, str):
                statement = statement.where(column == value)
            else:
                accepted = list(value)
                if not accepted:
                    return []
                statement = statement.where(column.in_(accepted))
        statement = statement.order_by(*_entity_order(newest_first)).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self.database.session() as session:
            return [_row_to_entity(row, model) for row in session.scalars(statement)]

    def list_latest_entities(
        self,
        model: type[EntityT],
        filters: Mapping[str, PayloadFilterValue] | None = None,
        *,
        engagement_id: str | None = None,
        automation_run_id: str | None = None,
        limit: int = 1000,
    ) -> list[EntityT]:
        """Return the newest ``limit`` rows matching ``filters``, oldest first.

        A bounded view of a kind that keeps growing, such as captured traffic
        or pending intercepts, has to show the latest records. Reading the
        oldest page instead hides every new row once the kind passes the
        bound. The window is taken from the newest end and returned in the
        chronological order the views already render.
        """

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = self.find_entities(
            model,
            filters or {},
            engagement_id=engagement_id,
            automation_run_id=automation_run_id,
            newest_first=True,
            limit=limit,
        )
        rows.reverse()
        return rows

    def iter_readable_entities(
        self, model: type[EntityT], *, page_size: int = 1000
    ) -> Iterator[EntityT]:
        """Yield every ``model`` record oldest first, skipping rows that no longer validate.

        ``list_entities`` raises ``CorruptRecordError`` for a whole page as soon
        as one row fails validation, so a reference scan over every kind turned
        into a failure of the operation that ran it once any record anywhere was
        unreadable. Scans only need the readable rows; each skipped row is
        recorded so the corruption stays visible.
        """

        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        offset = 0
        while True:
            statement = (
                select(EntityRow)
                .where(EntityRow.kind == model.entity_kind)
                .order_by(EntityRow.created_at, EntityRow.id)
                .offset(offset)
                .limit(page_size)
            )
            readable: list[EntityT] = []
            scanned = 0
            with self.database.session() as session:
                for row in session.scalars(statement):
                    scanned += 1
                    try:
                        readable.append(_row_to_entity(row, model))
                    except CorruptRecordError as exc:
                        record_caught_exception(
                            "storage",
                            "storage.scan.skipped_unreadable_record",
                            "A reference scan skipped a stored record that failed validation.",
                            exc,
                            stage="storage",
                            metadata={"kind": row.kind, "entity_id": row.id},
                        )
            yield from readable
            if scanned < page_size:
                return
            offset += scanned

    def count(
        self,
        model: type[Entity],
        *,
        engagement_id: str | None = None,
        include_temporary: bool = False,
    ) -> int:
        statement = select(func.count(EntityRow.id)).where(
            EntityRow.kind == model.entity_kind
        )
        if model.entity_kind == "chat_sessions" and not include_temporary:
            statement = statement.where(not_temporary_chat_session())
        if engagement_id is not None:
            statement = statement.where(EntityRow.engagement_id == engagement_id)
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def update(
        self,
        model: type[EntityT],
        entity_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> EntityT:
        with self.transaction() as transaction:
            return transaction.update(
                model,
                entity_id,
                changes,
                expected_revision=expected_revision,
            )

    def update_with_event(
        self,
        model: type[EntityT],
        entity_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        run_id: str,
        event_type: str,
        event_payload: dict[str, Any],
        actor_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[EntityT, RunEvent]:
        """Atomically persist an entity transition and its audit event."""

        _check_model_update(model)
        protected = {"id", "created_at", "updated_at", "revision"}.intersection(changes)
        if protected:
            raise ValueError(f"cannot patch protected fields: {sorted(protected)}")
        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, run_id)
            existing_event = self._event_for_idempotency_key(
                connection, run_id, idempotency_key
            )
            if existing_event is not None:
                self._validate_idempotent_event(
                    existing_event,
                    event_type=event_type,
                    payload=event_payload,
                    actor_id=actor_id,
                )
                referenced_ids = {
                    value
                    for key, value in existing_event.payload.items()
                    if key.endswith("_id") and isinstance(value, str)
                }
                if entity_id != run_id and entity_id not in referenced_ids:
                    raise ConflictError(
                        "idempotency key belongs to a different entity transition"
                    )
                current_row = (
                    connection.execute(
                        select(EntityRow).where(
                            EntityRow.id == entity_id,
                            EntityRow.kind == model.entity_kind,
                        )
                    )
                    .mappings()
                    .first()
                )
                if current_row is None:
                    raise ConflictError(
                        "idempotent event exists but its transitioned entity is missing"
                    )
                current_entity = self._mapping_to_entity(current_row, model)
                connection.commit()
                return current_entity, existing_event
            row = (
                connection.execute(
                    select(EntityRow).where(
                        EntityRow.id == entity_id, EntityRow.kind == model.entity_kind
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError(
                    f"{model.entity_kind} entity not found: {entity_id}"
                )
            if int(row["revision"]) != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, found {row['revision']}"
                )
            payload = dict(row["payload"])
            payload.update(changes)
            payload.update(
                {
                    "id": entity_id,
                    "updated_at": utc_now(),
                    "revision": expected_revision + 1,
                }
            )
            updated_entity = model.model_validate(payload)
            result = connection.execute(
                update(EntityRow)
                .where(
                    EntityRow.id == entity_id,
                    EntityRow.kind == model.entity_kind,
                    EntityRow.revision == expected_revision,
                )
                .values(
                    payload=_dump_entity(updated_entity),
                    engagement_id=entity_engagement_id(updated_entity),
                    **_entity_lookup_fields(updated_entity),
                    revision=updated_entity.revision,
                    updated_at=updated_entity.updated_at,
                )
            )
            if result.rowcount != 1:
                raise ConflictError("entity transition lost an optimistic lock race")
            event = self._next_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=event_payload,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            connection.execute(
                insert(RunEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return updated_entity, event
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_006",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_with_operation_event(
        self,
        model: type[EntityT],
        entity_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        operation_id: str,
        operation_kind: str,
        engagement_id: str,
        event_type: str,
        event_payload: dict[str, Any],
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[EntityT, OperationEvent]:
        """Atomically persist an entity transition and its operation event."""

        _check_model_update(model)
        if not all((operation_id, operation_kind, engagement_id, event_type)):
            raise ValueError("operation event identifiers and type are required")
        protected = {"id", "created_at", "updated_at", "revision"}.intersection(changes)
        if protected:
            raise ValueError(f"cannot patch protected fields: {sorted(protected)}")

        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, f"operation:{operation_id}")
            row = (
                connection.execute(
                    select(EntityRow).where(
                        EntityRow.id == entity_id,
                        EntityRow.kind == model.entity_kind,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError(
                    f"{model.entity_kind} entity not found: {entity_id}"
                )
            if int(row["revision"]) != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, "
                    f"found {row['revision']}"
                )

            payload = dict(row["payload"])
            payload.update(changes)
            payload.update(
                {
                    "id": entity_id,
                    "updated_at": utc_now(),
                    "revision": expected_revision + 1,
                }
            )
            updated_entity = model.model_validate(payload)
            result = connection.execute(
                update(EntityRow)
                .where(
                    EntityRow.id == entity_id,
                    EntityRow.kind == model.entity_kind,
                    EntityRow.revision == expected_revision,
                )
                .values(
                    payload=_dump_entity(updated_entity),
                    engagement_id=entity_engagement_id(updated_entity),
                    **_entity_lookup_fields(updated_entity),
                    revision=updated_entity.revision,
                    updated_at=updated_entity.updated_at,
                )
            )
            if result.rowcount != 1:
                raise ConflictError("entity transition lost an optimistic lock race")

            event = self._next_operation_event(
                connection,
                operation_id=operation_id,
                operation_kind=operation_kind,
                engagement_id=engagement_id,
                event_type=event_type,
                payload=event_payload,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                occurred_at=occurred_at,
            )
            connection.execute(
                insert(OperationEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return updated_entity, event
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_007",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace(
        self,
        model: type[EntityT],
        entity_id: str,
        replacement: EntityT,
        *,
        expected_revision: int | None = None,
    ) -> EntityT:
        if replacement.id != entity_id:
            raise ValueError("replacement id must match the resource id")
        existing = self.get(model, entity_id)
        changes = replacement.model_dump(
            mode="python", exclude={"id", "created_at", "updated_at", "revision"}
        )
        return self.update(
            model,
            entity_id,
            changes,
            expected_revision=expected_revision or existing.revision,
        )

    def delete(
        self,
        model: type[Entity],
        entity_id: str,
        *,
        expected_revision: int | None = None,
    ) -> None:
        with self.database.session() as session:
            predicates = [
                EntityRow.id == entity_id,
                EntityRow.kind == model.entity_kind,
            ]
            if expected_revision is not None:
                predicates.append(EntityRow.revision == expected_revision)
            result = session.execute(delete(EntityRow).where(*predicates))
            if result.rowcount != 1:
                if expected_revision is not None:
                    current_revision = session.scalar(
                        select(EntityRow.revision).where(
                            EntityRow.id == entity_id,
                            EntityRow.kind == model.entity_kind,
                        )
                    )
                    if current_revision is not None:
                        raise ConflictError(
                            "revision conflict: expected "
                            f"{expected_revision}, found {current_revision}"
                        )
                raise NotFoundError(
                    f"{model.entity_kind} entity not found: {entity_id}"
                )

    def validate_chat_session_delete(
        self, session_id: str, *, expected_revision: int | None = None
    ) -> None:
        """Raise what ``delete_chat_session`` would raise, without deleting.

        A caller with side effects of its own (closing the vendor session a
        harness chat runs on) refuses here first, so a refused delete leaves
        the surviving conversation untouched.
        """

        with self.database.session() as session:
            self._chat_session_delete_scope(
                session, session_id, expected_revision=expected_revision
            )

    def _chat_session_delete_scope(
        self, session: Session, session_id: str, *, expected_revision: int | None
    ) -> tuple[EntityRow, list[str]]:
        """The conversation row and the chat ids its delete removes.

        Raises ``NotFoundError`` or ``ConflictError`` when the delete must be
        refused: a stale revision, a running subagent, or an active turn.
        """

        row = session.scalar(
            select(EntityRow).where(
                EntityRow.id == session_id,
                EntityRow.kind == "chat_sessions",
            )
        )
        if row is None:
            raise NotFoundError(f"chat_sessions entity not found: {session_id}")
        if expected_revision is not None and row.revision != expected_revision:
            raise ConflictError(
                f"revision conflict: expected {expected_revision}, found {row.revision}"
            )
        # Subagent conversations belong to their parent and go with it.
        subagent_rows = session.scalars(
            select(EntityRow).where(
                EntityRow.kind == "chat_subagents",
                EntityRow.payload["parent_session_id"].as_string() == session_id,
            )
        ).all()
        if any(item.payload.get("status") == "running" for item in subagent_rows):
            raise ConflictError(
                "conversation cannot be deleted while a subagent is running"
            )
        session_ids = [
            session_id,
            *(
                str(item.payload["child_session_id"])
                for item in subagent_rows
                if item.payload.get("child_session_id")
            ),
        ]
        # What the conversation reports as its pending turn, which also stops
        # a rename or archive: a queued turn is still admitted and run, and an
        # interrupted one awaiting recovery is resumed by Core.
        active_turn = and_(
            EntityRow.kind == "chat_turns",
            EntityRow.chat_session_id.in_(session_ids),
            EntityRow.payload["status"]
            .as_string()
            .in_(
                (
                    "queued",
                    "routing",
                    "waiting_approval",
                    "waiting_callback",
                    "finalizing",
                )
            ),
        )
        interrupted = session.scalars(
            select(EntityRow.payload).where(
                EntityRow.kind == "chat_turns",
                EntityRow.chat_session_id.in_(session_ids),
                EntityRow.payload["status"].as_string() == "interrupted",
            )
        )
        if session.scalar(select(exists().where(active_turn))) or any(
            _recovery_pending(payload) for payload in interrupted
        ):
            raise ConflictError(
                "conversation cannot be deleted while a response is active"
            )
        active_harness_turn = and_(
            EntityRow.kind == "harness_turns",
            EntityRow.chat_session_id.in_(session_ids),
            EntityRow.payload["status"]
            .as_string()
            .in_(("queued", "running", "waiting_approval")),
        )
        if session.scalar(select(exists().where(active_harness_turn))):
            raise ConflictError(
                "conversation cannot be deleted while a harness turn is active"
            )
        return row, session_ids

    def delete_chat_session(
        self, session_id: str, *, expected_revision: int | None = None
    ) -> None:
        """Atomically remove one conversation and its private derived records."""

        with self.database.session() as session:
            row, session_ids = self._chat_session_delete_scope(
                session, session_id, expected_revision=expected_revision
            )
            # Vendor (harness) sessions belong to the conversations that opened
            # them; nothing else releases the row once the chat is gone, and a
            # leftover row keeps the project from being deleted. A mission that
            # continued from the chat still runs on the same vendor session, so
            # a session any run references is left in place.
            harness_session_ids = {
                str(item.payload["harness_session_id"])
                for item in session.scalars(
                    select(EntityRow).where(
                        EntityRow.kind == "chat_sessions",
                        EntityRow.id.in_(session_ids),
                    )
                )
                if item.payload.get("harness_session_id")
            }
            if harness_session_ids:
                shared_with_runs = session.scalars(
                    select(EntityRow).where(
                        EntityRow.kind == "runs",
                        EntityRow.payload["harness_session_id"]
                        .as_string()
                        .in_(sorted(harness_session_ids)),
                    )
                )
                harness_session_ids.difference_update(
                    str(item.payload["harness_session_id"]) for item in shared_with_runs
                )
            owned_predicates = [
                and_(
                    EntityRow.kind.in_(
                        (
                            "chat_messages",
                            "chat_turns",
                            "chat_goals",
                            "chat_bookmarks",
                            "chat_queues",
                            "chat_decisions",
                            "chat_read_cursors",
                            "chat_schedules",
                        )
                    ),
                    EntityRow.chat_session_id.in_(session_ids),
                ),
                and_(
                    EntityRow.kind == "context_snapshots",
                    EntityRow.payload["owner_type"].as_string() == "chat_session",
                    EntityRow.payload["owner_id"].as_string().in_(session_ids),
                ),
                and_(
                    EntityRow.kind.in_(("tool_calls", "approvals")),
                    EntityRow.chat_session_id.in_(session_ids),
                ),
                and_(
                    EntityRow.kind.in_(("harness_turns", "harness_interactions")),
                    EntityRow.chat_session_id.in_(session_ids),
                ),
                # Workspace checkpoints and native hook runs are captured for a
                # conversation but carry the project's id; left behind they
                # keep an otherwise empty project from being deleted.
                and_(
                    EntityRow.kind.in_(
                        ("native_checkpoints", "native_hook_executions")
                    ),
                    EntityRow.chat_session_id.in_(session_ids),
                ),
                and_(
                    EntityRow.kind.in_(("chat_subagents", "chat_subagent_messages")),
                    EntityRow.payload["parent_session_id"].as_string() == session_id,
                ),
                # Incoming peer-message records are private to the recipient
                # transcript. Outgoing records survive sender deletion so the
                # recipient keeps durable attribution to the deleted session ID.
                and_(
                    EntityRow.kind == "chat_agent_messages",
                    EntityRow.payload["recipient_session_id"]
                    .as_string()
                    .in_(session_ids),
                ),
                and_(
                    EntityRow.kind == "chat_sessions",
                    EntityRow.id.in_(session_ids[1:]),
                ),
            ]
            if harness_session_ids:
                owned_predicates.append(
                    and_(
                        EntityRow.kind == "harness_sessions",
                        EntityRow.id.in_(sorted(harness_session_ids)),
                    )
                )
            owned_records = or_(*owned_predicates)
            # Chat-origin tool calls key their budget counter by the turn id.
            chat_turn_ids = select(EntityRow.id).where(
                EntityRow.kind == "chat_turns",
                EntityRow.chat_session_id.in_(session_ids),
            )
            session.execute(
                delete(RunBudgetCounterRow).where(
                    RunBudgetCounterRow.run_id.in_(chat_turn_ids)
                )
            )
            # Admission rows are keyed by turn id; one left behind names a turn
            # that no longer exists.
            session.execute(
                delete(ProviderTurnQueueRow).where(
                    ProviderTurnQueueRow.turn_id.in_(chat_turn_ids)
                )
            )
            # Operation events are an immutable audit ledger. As with deleted
            # missions, retain those records while removing the mutable chat,
            # harness-turn, and interaction entities that expose them in the UI.
            session.execute(delete(EntityRow).where(owned_records))
            result = session.execute(
                delete(EntityRow).where(
                    EntityRow.id == session_id,
                    EntityRow.kind == "chat_sessions",
                    EntityRow.revision == row.revision,
                )
            )
            if result.rowcount != 1:
                raise ConflictError("conversation changed while it was being deleted")

    def delete_run(self, run_id: str, *, expected_revision: int | None = None) -> None:
        """Atomically remove a terminal mission and its mutable execution records.

        Immutable event ledgers remain as audit records, but become inaccessible
        through mission APIs once the owning run is removed.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(EntityRow).where(
                    EntityRow.id == run_id,
                    EntityRow.kind == "runs",
                )
            )
            if row is None:
                raise NotFoundError(f"runs entity not found: {run_id}")
            if expected_revision is not None and row.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, found {row.revision}"
                )

            status = row.payload.get("status")
            if status not in {"complete", "failed", "cancelled", "interrupted"}:
                raise ConflictError(
                    "mission must be completed, failed, cancelled, or interrupted before deletion"
                )

            session.execute(
                delete(RunBudgetCounterRow).where(RunBudgetCounterRow.run_id == run_id)
            )
            session.execute(
                delete(EntityRow).where(
                    or_(
                        and_(
                            EntityRow.kind.in_(
                                (
                                    "tasks",
                                    "agent_attempts",
                                    "tool_calls",
                                    "approvals",
                                    "harness_turns",
                                    "harness_interactions",
                                    # Browser autonomy is leased to one run; a
                                    # surviving lease refuses the next one on
                                    # that browser session until it expires.
                                    "browser_automation_leases",
                                    "browser_commands",
                                    "browser_proxy_rules",
                                )
                            ),
                            EntityRow.payload["run_id"].as_string() == run_id,
                        ),
                        and_(
                            EntityRow.kind == "context_snapshots",
                            EntityRow.payload["owner_type"].as_string() == "agent_run",
                            EntityRow.payload["owner_id"].as_string() == run_id,
                        ),
                    )
                )
            )
            result = session.execute(
                delete(EntityRow).where(
                    EntityRow.id == run_id,
                    EntityRow.kind == "runs",
                    EntityRow.revision == row.revision,
                )
            )
            if result.rowcount != 1:
                raise ConflictError("mission changed while it was being deleted")

    def delete_archived_engagement(
        self, engagement_id: str, *, expected_revision: int
    ) -> None:
        """Remove an idle archive atomically, without touching filesystem or audit ledgers."""
        from .application_model.persistence import graphs, edits
        from .database import ResourceRelationRow
        from .terminal_history import TerminalCommandRow, TerminalCommandPreferenceRow

        with self.database.session() as session:
            # Acquire the writer lock before inspecting children; a concurrent
            # restore or new turn cannot interleave with the checked deletion.
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(EntityRow, engagement_id)
            if row is None or row.kind != "engagements":
                raise NotFoundError(f"engagements entity not found: {engagement_id}")
            if row.revision != expected_revision:
                raise ConflictError(
                    "Project changed. Refresh Archived projects and try again."
                )
            if row.payload.get("status") != "archived":
                raise ConflictError(
                    "Archive the project before permanently deleting it."
                )
            # Session envelopes can remain starting before their first turn or
            # after restart. Only turn records establish unfinished harness work.
            terminal_states = {
                "runs": {"complete", "failed", "cancelled", "interrupted"},
                "chat_turns": {"complete", "failed", "cancelled", "interrupted"},
                "harness_turns": {"complete", "failed", "cancelled", "interrupted"},
                "operator_executions": {
                    "completed",
                    "denied",
                    "timed_out",
                    "cancelled",
                    "failed",
                    "interrupted",
                },
                "command_executions": {
                    "completed",
                    "timed_out",
                    "cancelled",
                    "failed",
                    "interrupted",
                },
                "browser_commands": {"complete", "failed", "cancelled", "expired"},
                "automation_sessions": {"closed", "failed", "interrupted"},
            }
            for kind, allowed in terminal_states.items():
                busy = session.scalar(
                    select(EntityRow.id)
                    .where(
                        EntityRow.engagement_id == engagement_id,
                        EntityRow.kind == kind,
                        EntityRow.payload["status"].as_string().not_in(allowed),
                    )
                    .limit(1)
                )
                if busy:
                    raise ConflictError(
                        f"Project still has unfinished {kind.replace('_', ' ')}. "
                        "Stop or finish its work and close command sessions before deleting; "
                        "restore the project to access those controls."
                    )
            queues = session.scalars(
                select(EntityRow).where(
                    EntityRow.engagement_id == engagement_id,
                    EntityRow.kind == "chat_queues",
                )
            )
            if any(
                item.get("status") not in {"complete", "cancelled", "failed"}
                for queue in queues
                for item in queue.payload.get("items", [])
            ):
                raise ConflictError(
                    "Project has queued follow-ups. Restore it and clear the queue before deleting."
                )
            # Counters are keyed by the mission id or, for chat-origin tool
            # calls, by the chat turn id.
            counter_owner_ids = select(EntityRow.id).where(
                EntityRow.engagement_id == engagement_id,
                EntityRow.kind.in_(("runs", "chat_turns")),
            )
            session.execute(
                delete(RunBudgetCounterRow).where(
                    RunBudgetCounterRow.run_id.in_(counter_owner_ids)
                )
            )
            session.execute(
                delete(ProviderTurnQueueRow).where(
                    ProviderTurnQueueRow.turn_id.in_(
                        select(EntityRow.id).where(
                            EntityRow.engagement_id == engagement_id,
                            EntityRow.kind == "chat_turns",
                        )
                    )
                )
            )
            for table, column in (
                (graphs, graphs.c.project_id),
                (edits, edits.c.project_id),
                (ResourceRelationRow, ResourceRelationRow.project_id),
                (SearchDocumentRow, SearchDocumentRow.project_id),
                (TerminalCommandRow, TerminalCommandRow.engagement_id),
                (
                    TerminalCommandPreferenceRow,
                    TerminalCommandPreferenceRow.engagement_id,
                ),
            ):
                session.execute(delete(table).where(column == engagement_id))
            session.execute(
                delete(EntityRow).where(EntityRow.engagement_id == engagement_id)
            )

    def engagement_has_dependents(
        self,
        engagement_id: str,
        *,
        exclude_entity_ids: Sequence[str] = (),
    ) -> bool:
        """Return whether any persisted child is owned by this engagement."""

        predicate = and_(
            EntityRow.engagement_id == engagement_id,
            EntityRow.kind != "engagements",
        )
        if exclude_entity_ids:
            predicate = and_(predicate, EntityRow.id.not_in(exclude_entity_ids))
        with self.database.session() as session:
            return bool(session.scalar(select(exists().where(predicate))))

    def provider_has_history_references(self, provider_id: str) -> bool:
        """Return whether durable chat or run history references a provider."""

        predicate = or_(
            and_(
                EntityRow.kind == "runs",
                EntityRow.payload["supervisor_provider_id"].as_string() == provider_id,
            ),
            and_(
                EntityRow.kind.in_(
                    ("agent_attempts", "chat_sessions", "chat_messages")
                ),
                EntityRow.payload["provider_profile_id"].as_string() == provider_id,
            ),
        )
        with self.database.session() as session:
            return bool(session.scalar(select(exists().where(predicate))))

    def overview(self, engagement_id: str | None = None) -> dict[str, Any]:
        # Temporary "Ask Nebula" sessions are hidden from the conversation list,
        # so the conversation count must leave them out as well.
        statement = (
            select(EntityRow.kind, func.count(EntityRow.id))
            .where(or_(EntityRow.kind != "chat_sessions", not_temporary_chat_session()))
            .group_by(EntityRow.kind)
        )
        if engagement_id is not None:
            statement = statement.where(EntityRow.engagement_id == engagement_id)
        with self.database.session() as session:
            counts = {kind: int(count) for kind, count in session.execute(statement)}
        return {
            "engagement_id": engagement_id,
            "counts": {kind: counts.get(kind, 0) for kind in ENTITY_MODEL_BY_KIND},
            "schema_version": self.database.current_schema_version(),
        }

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RunEvent:
        """Atomically assign the next sequence and append an immutable event.

        SQLite uses ``BEGIN IMMEDIATE`` so concurrent writers cannot calculate the
        same sequence.  An idempotency key returns the original event, allowing a
        recovered worker to safely retry a persisted transition.
        """

        if not run_id or not event_type:
            raise ValueError("run_id and event_type are required")
        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, run_id)

            existing_event = self._event_for_idempotency_key(
                connection, run_id, idempotency_key
            )
            if existing_event is not None:
                self._validate_idempotent_event(
                    existing_event,
                    event_type=event_type,
                    payload=payload,
                    actor_id=actor_id,
                )
                connection.commit()
                return existing_event

            event = self._next_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=payload or {},
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                occurred_at=occurred_at or utc_now(),
            )
            connection.execute(
                insert(RunEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return event
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_008",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def replay_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[RunEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        statement = (
            select(RunEventRow)
            .where(
                RunEventRow.run_id == run_id,
                RunEventRow.sequence > after_sequence,
            )
            .order_by(RunEventRow.sequence)
            .limit(limit)
        )
        with self.database.session() as session:
            rows = session.scalars(statement).all()
            return [self._row_to_event(row) for row in rows]

    def append_operation_event(
        self,
        operation_id: str,
        operation_kind: str,
        engagement_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> OperationEvent:
        """Append one replayable, immutable operator-workflow event."""

        if not all((operation_id, operation_kind, engagement_id, event_type)):
            raise ValueError("operation event identifiers and type are required")
        connection = self.database.engine.connect()
        try:
            self._begin_run_write(connection, f"operation:{operation_id}")
            if idempotency_key:
                existing = (
                    connection.execute(
                        select(OperationEventRow).where(
                            OperationEventRow.operation_id == operation_id,
                            OperationEventRow.idempotency_key == idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    event = self._mapping_to_operation_event(existing)
                    if (
                        event.operation_kind != operation_kind
                        or event.engagement_id != engagement_id
                        or event.event_type != event_type
                        or event.payload != (payload or {})
                        or event.actor_id != actor_id
                    ):
                        raise ConflictError(
                            "idempotency key was reused for a different operation event"
                        )
                    connection.commit()
                    return event
            last_sequence = connection.scalar(
                select(func.max(OperationEventRow.sequence)).where(
                    OperationEventRow.operation_id == operation_id
                )
            )
            event = OperationEvent(
                operation_id=operation_id,
                operation_kind=operation_kind,
                engagement_id=engagement_id,
                sequence=int(last_sequence or 0) + 1,
                event_type=event_type,
                payload=payload or {},
                actor_id=actor_id,
                occurred_at=occurred_at or utc_now(),
                idempotency_key=idempotency_key,
            )
            connection.execute(
                insert(OperationEventRow).values(**event.model_dump(mode="python"))
            )
            connection.commit()
            return event
        except Exception as caught_error:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_009",
                "A handled storage operation raised an exception.",
                caught_error,
                stage="storage",
            )
            connection.rollback()
            raise
        finally:
            connection.close()

    def replay_operation_events(
        self,
        operation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
        through_sequence: int | None = None,
        exclude_event_types: Sequence[str] = (),
        payload_values: Mapping[str, Sequence[str]] | None = None,
    ) -> list[OperationEvent]:
        """Return one operation's events after ``after_sequence``, in order.

        ``through_sequence`` caps the range, ``exclude_event_types`` skips
        whole event types, and ``payload_values`` keeps only events whose
        top-level payload field is one of the listed values. All filtering
        happens in SQL on the ``(operation_id, sequence)`` index.
        """

        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        statement = select(OperationEventRow).where(
            OperationEventRow.operation_id == operation_id,
            OperationEventRow.sequence > after_sequence,
        )
        if through_sequence is not None:
            statement = statement.where(OperationEventRow.sequence <= through_sequence)
        if exclude_event_types:
            statement = statement.where(
                OperationEventRow.event_type.not_in(list(exclude_event_types))
            )
        for field_name, accepted in (payload_values or {}).items():
            statement = statement.where(
                OperationEventRow.payload[field_name].as_string().in_(list(accepted))
            )
        statement = statement.order_by(OperationEventRow.sequence).limit(limit)
        with self.database.session() as session:
            return [
                self._row_to_operation_event(row)
                for row in session.scalars(statement).all()
            ]

    def last_operation_event_sequence(self, operation_id: str) -> int:
        """Return the newest sequence recorded for an operation, or 0."""

        statement = select(func.max(OperationEventRow.sequence)).where(
            OperationEventRow.operation_id == operation_id
        )
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def list_operation_events(
        self, engagement_id: str, *, offset: int = 0, limit: int = 1000
    ) -> list[OperationEvent]:
        if offset < 0 or not 1 <= limit <= 10_000:
            raise ValueError("invalid operation event page")
        statement = (
            select(OperationEventRow)
            .where(OperationEventRow.engagement_id == engagement_id)
            .order_by(OperationEventRow.occurred_at, OperationEventRow.id)
            .offset(offset)
            .limit(limit)
        )
        with self.database.session() as session:
            return [
                self._row_to_operation_event(row)
                for row in session.scalars(statement).all()
            ]

    @staticmethod
    def _row_to_operation_event(row: OperationEventRow) -> OperationEvent:
        occurred_at = row.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return OperationEvent(
            id=row.id,
            operation_id=row.operation_id,
            operation_kind=row.operation_kind,
            engagement_id=row.engagement_id,
            sequence=row.sequence,
            event_type=row.event_type,
            payload=row.payload,
            actor_id=row.actor_id,
            occurred_at=occurred_at,
            idempotency_key=row.idempotency_key,
        )

    @staticmethod
    def _mapping_to_operation_event(row: Any) -> OperationEvent:
        occurred_at = row["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return OperationEvent(
            id=row["id"],
            operation_id=row["operation_id"],
            operation_kind=row["operation_kind"],
            engagement_id=row["engagement_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            payload=row["payload"],
            actor_id=row["actor_id"],
            occurred_at=occurred_at,
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _row_to_event(row: RunEventRow) -> RunEvent:
        occurred_at = row.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return RunEvent(
            id=row.id,
            run_id=row.run_id,
            sequence=row.sequence,
            event_type=row.event_type,
            payload=row.payload,
            actor_id=row.actor_id,
            occurred_at=occurred_at,
            idempotency_key=row.idempotency_key,
        )

    @staticmethod
    def _mapping_to_event(row: Any) -> RunEvent:
        occurred_at = row["occurred_at"]
        if isinstance(occurred_at, str):
            occurred_at = datetime.fromisoformat(occurred_at)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return RunEvent(
            id=row["id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            payload=row["payload"],
            actor_id=row["actor_id"],
            occurred_at=occurred_at,
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _mapping_to_entity(row: Any, model: type[EntityT]) -> EntityT:
        if row["kind"] != model.entity_kind:
            raise CorruptRecordError(
                f"record {row['id']} is {row['kind']}, expected {model.entity_kind}"
            )
        try:
            return model.model_validate(row["payload"])
        except Exception as exc:
            record_caught_exception(
                "storage",
                "storage.storage.caught_failure_010",
                "A handled storage operation raised an exception.",
                exc,
                stage="storage",
            )
            raise CorruptRecordError(f"record {row['id']} failed validation") from exc

    def _event_for_idempotency_key(
        self, connection: Any, run_id: str, idempotency_key: str | None
    ) -> RunEvent | None:
        if not idempotency_key:
            return None
        existing = (
            connection.execute(
                select(RunEventRow).where(
                    RunEventRow.run_id == run_id,
                    RunEventRow.idempotency_key == idempotency_key,
                )
            )
            .mappings()
            .first()
        )
        return self._mapping_to_event(existing) if existing is not None else None

    @staticmethod
    def _validate_idempotent_event(
        event: RunEvent,
        *,
        event_type: str,
        payload: dict[str, Any] | None,
        actor_id: str | None,
    ) -> None:
        if (
            event.event_type != event_type
            or event.payload != (payload or {})
            or event.actor_id != actor_id
        ):
            raise ConflictError("idempotency key was reused for a different run event")

    @staticmethod
    def _next_event(
        connection: Any,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None,
        actor_id: str | None,
        idempotency_key: str | None,
        occurred_at: datetime | None = None,
    ) -> RunEvent:
        last_sequence = connection.scalar(
            select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == run_id)
        )
        return RunEvent(
            id=str(uuid4()),
            run_id=run_id,
            sequence=int(last_sequence or 0) + 1,
            event_type=event_type,
            payload=payload or {},
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at or utc_now(),
        )

    @staticmethod
    def _next_operation_event(
        connection: Any,
        *,
        operation_id: str,
        operation_kind: str,
        engagement_id: str,
        event_type: str,
        payload: dict[str, Any] | None,
        actor_id: str | None,
        idempotency_key: str | None,
        occurred_at: datetime | None = None,
    ) -> OperationEvent:
        last_sequence = connection.scalar(
            select(func.max(OperationEventRow.sequence)).where(
                OperationEventRow.operation_id == operation_id
            )
        )
        return OperationEvent(
            operation_id=operation_id,
            operation_kind=operation_kind,
            engagement_id=engagement_id,
            sequence=int(last_sequence or 0) + 1,
            event_type=event_type,
            payload=payload or {},
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at or utc_now(),
        )
