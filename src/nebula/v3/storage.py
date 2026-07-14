"""Validated repositories and the durable append-only run-event ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence, TypeVar, cast
from uuid import uuid4

from sqlalchemy import and_, delete, exists, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import (
    Database,
    EntityRow,
    OperationEventRow,
    RunBudgetCounterRow,
    RunEventRow,
)
from .domain import (
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
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"entity already exists: {entity.id}") from exc
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
                revision=updated.revision,
                updated_at=updated.updated_at,
            )
        )
        if result.rowcount != 1:
            raise ConflictError(
                f"entity {entity_id} changed while the update was in progress"
            )
        return updated


class NebulaStore:
    """Persistence boundary for typed Nebula entities and run events."""

    def __init__(self, database: Database | str | Path) -> None:
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
            connection.rollback()
            raise ConflictError(
                f"entity or initial run event already exists: {entity.id}"
            ) from exc
        except Exception:
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
            maximum = (
                int(run["payload"].get("max_tool_calls", 0))
                if call.origin == ToolCallOrigin.CHAT
                else int(run["payload"].get("budget", {}).get("max_tool_calls", 0))
            )
            counter = (
                connection.execute(
                    select(RunBudgetCounterRow)
                    .where(RunBudgetCounterRow.run_id == call.run_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            current = int(counter["tool_calls"]) if counter else 0
            if current >= maximum:
                raise RunBudgetExceededError(
                    f"run {call.run_id} exhausted its tool-call budget ({maximum})"
                )
            if counter:
                connection.execute(
                    update(RunBudgetCounterRow)
                    .where(RunBudgetCounterRow.run_id == call.run_id)
                    .values(tool_calls=current + 1, updated_at=utc_now())
                )
            else:
                connection.execute(
                    insert(RunBudgetCounterRow).values(
                        run_id=call.run_id,
                        tool_calls=1,
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
                    created_at=call.created_at,
                    updated_at=call.updated_at,
                )
            )
            connection.commit()
            return call
        except Exception:
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
        offset: int = 0,
        limit: int = 100,
    ) -> list[EntityT]:
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        statement = select(EntityRow).where(EntityRow.kind == model.entity_kind)
        if engagement_id is not None:
            statement = statement.where(EntityRow.engagement_id == engagement_id)
        statement = (
            statement.order_by(EntityRow.created_at, EntityRow.id)
            .offset(offset)
            .limit(limit)
        )
        with self.database.session() as session:
            return [_row_to_entity(row, model) for row in session.scalars(statement)]

    def count(self, model: type[Entity], *, engagement_id: str | None = None) -> int:
        statement = select(func.count(EntityRow.id)).where(
            EntityRow.kind == model.entity_kind
        )
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
        except Exception:
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
        except Exception:
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

    def delete_chat_session(
        self, session_id: str, *, expected_revision: int | None = None
    ) -> None:
        """Atomically remove one conversation and its private derived records."""

        with self.database.session() as session:
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
            active_turn = and_(
                EntityRow.kind == "chat_turns",
                EntityRow.payload["session_id"].as_string() == session_id,
                EntityRow.payload["status"].as_string().in_(
                    ("routing", "waiting_approval", "finalizing")
                ),
            )
            if session.scalar(select(exists().where(active_turn))):
                raise ConflictError(
                    "conversation cannot be deleted while a response is active"
                )
            owned_records = or_(
                and_(
                    EntityRow.kind.in_(("chat_messages", "chat_turns")),
                    EntityRow.payload["session_id"].as_string() == session_id,
                ),
                and_(
                    EntityRow.kind == "context_snapshots",
                    EntityRow.payload["owner_type"].as_string() == "chat_session",
                    EntityRow.payload["owner_id"].as_string() == session_id,
                ),
                and_(
                    EntityRow.kind.in_(("tool_calls", "approvals")),
                    EntityRow.payload["chat_session_id"].as_string() == session_id,
                ),
            )
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

    def engagement_has_dependents(self, engagement_id: str) -> bool:
        """Return whether any persisted child is owned by this engagement."""

        predicate = and_(
            EntityRow.engagement_id == engagement_id,
            EntityRow.kind != "engagements",
        )
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
        statement = select(EntityRow.kind, func.count(EntityRow.id)).group_by(
            EntityRow.kind
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
        except Exception:
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
        except Exception:
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
    ) -> list[OperationEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        statement = (
            select(OperationEventRow)
            .where(
                OperationEventRow.operation_id == operation_id,
                OperationEventRow.sequence > after_sequence,
            )
            .order_by(OperationEventRow.sequence)
            .limit(limit)
        )
        with self.database.session() as session:
            return [
                self._row_to_operation_event(row)
                for row in session.scalars(statement).all()
            ]

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
