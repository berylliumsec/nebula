"""Host reconciliation of positively dead project owners, without effect replay."""

from __future__ import annotations

import hashlib

from sqlalchemy import select, update

from .database import (
    DeploymentReceiptRow,
    EntityRow,
    ProjectOwnershipRow,
    ProviderTurnQueueRow,
    RuntimeCapacityReservationRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeResourceRow,
)
from .deployments import DeploymentCoordinator
from .domain import (
    ChatBackend,
    ChatTurn,
    ChatTurnStatus,
    NativeHookExecution,
    ToolCall,
    utc_now,
)
from .process_identity import process_origin, same_process
from .runtime_chat_stream import RuntimeChatJournal
from .runtime_capacity import RuntimeCapacity
from .runtime_coordination import CoordinationConflict, ProjectLease
from .storage import NebulaStore, StoreTransaction

MAX_ORPHAN_ROWS = 100


async def reconcile_dead_project(
    coordinator: DeploymentCoordinator,
    expected_owner: ProjectLease,
    *,
    launch_origin: str | None,
) -> dict:
    """Reconcile only an expired owner whose original OS incarnation is absent."""
    from .chat import ChatService
    from .provider_scheduler import _released_state

    if launch_origin is None or launch_origin != process_origin():
        raise CoordinationConflict(
            "The original process namespace cannot be verified; retain this project's work"
        )
    coordination = coordinator.coordination
    identity = hashlib.sha256(
        (
            expected_owner.project_id
            + "\0"
            + expected_owner.runtime_id
            + "\0"
            + str(expected_owner.fencing_token)
        ).encode()
    ).hexdigest()
    receipt_key = "orphan-recovery:" + identity
    store = NebulaStore(coordinator.database)
    chat = ChatService(store)
    try:
        async with RuntimeCapacity(coordination)._write() as session:
            host = await coordinator._host(session)
            if host.shutdown_requested:
                raise CoordinationConflict(
                    "Core is holding recovery for coordinated shutdown"
                )
            owner = await session.get(
                ProjectOwnershipRow, expected_owner.project_id, with_for_update=True
            )
            if owner is None or (owner.runtime_id, owner.fencing_token) != (
                expected_owner.runtime_id,
                expected_owner.fencing_token,
            ):
                raise CoordinationConflict(
                    "Project ownership changed; read fresh recovery state"
                )
            process = await session.get(RuntimeProcessRow, expected_owner.runtime_id)
            instance = await session.get(RuntimeInstanceRow, expected_owner.runtime_id)
            now = await coordination._clock_ms(session)
            if (
                process is None
                or process.role != "execution"
                or not process.pid
                or instance is None
                or instance.role != "execution"
                or instance.lease_expires_at_ms > now
                or owner.lease_expires_at_ms > now
            ):
                raise CoordinationConflict(
                    "The original execution owner is not expired"
                )
            if same_process(process.pid, process.process_birth):
                raise CoordinationConflict(
                    "The original execution process is still alive"
                )
            previous = await session.get(DeploymentReceiptRow, receipt_key)
            if previous is not None:
                return previous.result
            rows = list(
                await session.scalars(
                    select(EntityRow)
                    .where(
                        EntityRow.engagement_id == expected_owner.project_id,
                        EntityRow.kind == ChatTurn.entity_kind,
                        EntityRow.payload["backend"].as_string()
                        == ChatBackend.PROVIDER.value,
                        EntityRow.payload["status"]
                        .as_string()
                        .in_(
                            (
                                ChatTurnStatus.ROUTING.value,
                                ChatTurnStatus.FINALIZING.value,
                            )
                        ),
                    )
                    .order_by(EntityRow.id)
                    .limit(MAX_ORPHAN_ROWS + 1)
                )
            )
            resources = list(
                await session.scalars(
                    select(RuntimeResourceRow)
                    .where(
                        RuntimeResourceRow.project_id == expected_owner.project_id,
                        RuntimeResourceRow.runtime_id == expected_owner.runtime_id,
                        RuntimeResourceRow.fencing_token
                        == expected_owner.fencing_token,
                        RuntimeResourceRow.state != "closed",
                        RuntimeResourceRow.kind == "provider_turn",
                    )
                    .limit(MAX_ORPHAN_ROWS + 1)
                )
            )
            if max(len(rows), len(resources)) > MAX_ORPHAN_ROWS:
                raise CoordinationConflict(
                    "Too many orphaned responses; retain work for operator review"
                )
            transaction = StoreTransaction(session)
            interrupted = []
            for row in rows:
                turn = ChatTurn.model_validate(row.payload)
                calls = await store.find_entities(
                    ToolCall,
                    {"chat_turn_id": turn.id},
                    engagement_id=expected_owner.project_id,
                )
                hooks = await store.find_entities(
                    NativeHookExecution,
                    {"chat_turn_id": turn.id},
                    engagement_id=expected_owner.project_id,
                )
                await chat._interrupt_orphaned_turn(
                    turn,
                    calls,
                    hooks,
                    cause="Project runtime stopped",
                    transaction=transaction,
                )
                interrupted.append(turn.id)
            closed = []
            for resource in resources:
                turn_id = resource.checkpoint.get("turn_id")
                segment = resource.checkpoint.get("segment")
                if (
                    not isinstance(turn_id, str)
                    or not isinstance(segment, str)
                    or resource.id != RuntimeChatJournal.resource_id(turn_id, segment)
                ):
                    raise CoordinationConflict(
                        "An orphaned stream has an invalid durable identity"
                    )
                turn = await store.get(ChatTurn, turn_id)
                if (
                    turn.engagement_id != expected_owner.project_id
                    or turn.backend != ChatBackend.PROVIDER
                    or turn.execution_claim_id is not None
                    or turn.status
                    in {ChatTurnStatus.ROUTING, ChatTurnStatus.FINALIZING}
                ):
                    raise CoordinationConflict(
                        "The orphaned stream still requires execution reconciliation"
                    )
                await coordination._append_stream_batch(
                    session,
                    expected_owner,
                    turn_id,
                    batch_key="stream-end/" + segment,
                    events=RuntimeChatJournal.end_events(turn_id, segment, failed=True),
                )
                resource.state = "closed"
                resource.detail = "The original runtime exited; response interruption and stream end are recorded."
                resource.updated_at_ms = now
                closed.append(resource.id)
            admissions = list(
                await session.scalars(
                    select(ProviderTurnQueueRow)
                    .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
                    .where(
                        EntityRow.engagement_id == expected_owner.project_id,
                        EntityRow.kind == ChatTurn.entity_kind,
                        EntityRow.payload["backend"].as_string()
                        == ChatBackend.PROVIDER.value,
                        EntityRow.payload["execution_claim_id"].as_string().is_(None),
                        EntityRow.payload["status"]
                        .as_string()
                        .not_in(
                            (
                                ChatTurnStatus.ROUTING.value,
                                ChatTurnStatus.FINALIZING.value,
                            )
                        ),
                        ProviderTurnQueueRow.state == "running",
                    )
                    .limit(MAX_ORPHAN_ROWS + 1)
                )
            )
            if len(admissions) > MAX_ORPHAN_ROWS:
                raise CoordinationConflict(
                    "Too many abandoned admissions; retain work for review"
                )
            settled = [admission.turn_id for admission in admissions]
            provider_keys = {
                "provider:" + admission.lease_owner
                for admission in admissions
                if admission.lease_owner
            }
            released_capacity = []
            if provider_keys:
                for reservation in await session.scalars(
                    select(RuntimeCapacityReservationRow).where(
                        RuntimeCapacityReservationRow.project_id
                        == expected_owner.project_id,
                        RuntimeCapacityReservationRow.runtime_id
                        == expected_owner.runtime_id,
                        RuntimeCapacityReservationRow.fencing_token
                        == expected_owner.fencing_token,
                        RuntimeCapacityReservationRow.request_key.in_(provider_keys),
                        RuntimeCapacityReservationRow.state == "held",
                    )
                ):
                    if reservation.demands.get("provider") == 1 and set(
                        reservation.demands
                    ) <= {"provider", "background"}:
                        reservation.state = "released"
                        reservation.release_reason = "dead-provider-owner"
                        reservation.released_at_ms = now
                        released_capacity.append(reservation.id)
            if settled:
                await session.execute(
                    update(ProviderTurnQueueRow)
                    .where(ProviderTurnQueueRow.turn_id.in_(settled))
                    .values(
                        state=_released_state(),
                        completed_at=utc_now(),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
            result = {
                "project_id": expected_owner.project_id,
                "runtime_id": expected_owner.runtime_id,
                "fencing_token": expected_owner.fencing_token,
                "interrupted_turn_ids": interrupted,
                "closed_resource_ids": closed,
                "settled_admission_ids": settled,
                "released_capacity_ids": released_capacity,
                "effects_replayed": False,
            }
            session.add(
                DeploymentReceiptRow(
                    id=receipt_key,
                    action="project.orphans_reconciled",
                    result=result,
                    occurred_at_ms=now,
                )
            )
            return result
    finally:
        await store.event_history.close()
