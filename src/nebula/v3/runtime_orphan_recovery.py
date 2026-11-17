"""Host reconciliation of positively dead project owners, without effect replay."""

from __future__ import annotations

import hashlib

from sqlalchemy import and_, or_, select, update

from .database import (
    DeploymentReceiptRow,
    EntityRow,
    ProjectOwnershipRow,
    ProviderTurnQueueRow,
    RuntimeCapacityReservationRow,
    RuntimeCommandRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeResourceRow,
)
from .deployments import DeploymentCoordinator
from .domain import (
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatGoal,
    ChatTurn,
    ChatTurnStatus,
    ChatSubagent,
    ChatSubagentStatus,
    NativeHookExecution,
    RiskClass,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from .process_identity import process_origin, same_process
from .runtime_chat_stream import RuntimeChatJournal
from .runtime_capacity import RuntimeCapacity
from .runtime_coordination import (
    STORAGE_REPORT_COMMAND,
    CoordinationConflict,
    ProjectLease,
)
from .storage import NebulaStore, NotFoundError, StoreTransaction

MAX_ORPHAN_ROWS = 100
MAX_HARNESS_CLAIMS = 1000
# Claims the harness runtime stamps on a goal turn and its goal. Its own
# restart settlement (HarnessRuntimeService.startup) interrupts the turn and
# pauses the goal, but only once the project activates; a claim left here
# would hold that activation forever.
HARNESS_CLAIM_OWNER = "harness-runtime"
# A request the dead owner was running is failed, not retried: the operator
# accepts that its effect may have partly happened.
INTERRUPTED_COMMAND_RESULT = {
    "http_status": 503,
    "detail": "The project worker stopped while this request ran. It was not retried and may have partly applied.",
    "interrupted_by_runtime_stop": True,
}


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
                        or_(
                            EntityRow.payload["status"]
                            .as_string()
                            .in_(
                                (
                                    ChatTurnStatus.ROUTING.value,
                                    ChatTurnStatus.FINALIZING.value,
                                )
                            ),
                            and_(
                                EntityRow.payload["status"].as_string()
                                == ChatTurnStatus.WAITING_CALLBACK.value,
                                EntityRow.payload["execution_claim_id"]
                                .as_string()
                                .is_not(None),
                            ),
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
                if turn.status == ChatTurnStatus.WAITING_CALLBACK:
                    history = await chat._turn_history(turn)
                    pending = history[-1] if history else {}
                    waiting_subagent = (
                        pending.get("status") == "waiting_callback"
                        and isinstance(pending.get("subagent_wait"), dict)
                    )
                    unknown_tool = pending.get("status") == "waiting_callback" and any(
                        call.id == pending.get("tool_call_id")
                        and chat._tool_effect_unknown(call)
                        for call in calls
                    )
                    if not (waiting_subagent or unknown_tool):
                        raise CoordinationConflict(
                            "An orphaned command callback still requires effect reconciliation"
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
            released_failed_claims = await _release_failed_provider_claims(
                session, store, transaction, expected_owner.project_id
            )
            released_claims = await _release_harness_claims(
                session, transaction, expected_owner.project_id
            )
            settled_commands = await _settle_interrupted_commands(
                session, expected_owner
            )
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
                    or (
                        turn.execution_claim_id is not None
                        and turn.id not in released_failed_claims
                    )
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
                "released_claim_ids": released_claims,
                "released_failed_provider_claim_ids": released_failed_claims,
                "settled_command_ids": settled_commands,
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


async def _release_failed_provider_claims(
    session,
    store: NebulaStore,
    transaction: StoreTransaction,
    project_id: str,
) -> list[str]:
    """Release only exact paired claims on failed provider turns with settled effects.

    Failed turns are not replayed or rewritten. Their claims can be cleared only
    when the paired running goal has the same claim and every durable child,
    approval, tool, and hook record proves there is no unfinished effect.
    """
    rows = list(
        await session.scalars(
            select(EntityRow)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.payload["backend"].as_string() == ChatBackend.PROVIDER.value,
                EntityRow.payload["status"].as_string() == ChatTurnStatus.FAILED.value,
                EntityRow.payload["execution_claim_id"].as_string().is_not(None),
            )
            .order_by(EntityRow.id)
            .limit(MAX_ORPHAN_ROWS + 1)
        )
    )
    if len(rows) > MAX_ORPHAN_ROWS:
        raise CoordinationConflict(
            "Too many failed provider claims; retain work for operator review"
        )

    released: list[str] = []
    for row in rows:
        turn = ChatTurn.model_validate(row.payload)
        if (
            turn.goal_id is None
            or not turn.execution_owner_id
            or not turn.execution_claim_id
            or turn.execution_claimed_at is None
        ):
            raise CoordinationConflict(
                "A failed provider turn has an incomplete execution claim"
            )
        goal_row = await session.get(EntityRow, turn.goal_id)
        if goal_row is None or goal_row.kind != ChatGoal.entity_kind:
            raise CoordinationConflict(
                "A failed provider turn does not have its exact running goal claim"
            )
        goal = ChatGoal.model_validate(goal_row.payload)
        if (
            goal.engagement_id != project_id
            or goal.session_id != turn.session_id
            or goal.status.value != "running"
            or goal.execution_claim_id != turn.execution_claim_id
            or goal.execution_owner_id != turn.execution_owner_id
            or goal.execution_claimed_at is None
        ):
            raise CoordinationConflict(
                "A failed provider turn does not have its exact running goal claim"
            )
        await _require_settled_failed_turn_effects(store, turn)
        await transaction.update(
            ChatTurn,
            turn.id,
            {
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
            },
            expected_revision=turn.revision,
        )
        await transaction.update(
            ChatGoal,
            goal.id,
            {
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
            },
            expected_revision=goal.revision,
        )
        released.append(turn.id)
    return released


async def _require_settled_failed_turn_effects(
    store: NebulaStore, turn: ChatTurn
) -> None:
    calls = await store.find_entities(
        ToolCall,
        {"chat_turn_id": turn.id},
        engagement_id=turn.engagement_id,
    )
    safe_call_statuses = {
        ToolCallStatus.COMPLETE,
        ToolCallStatus.DENIED,
        ToolCallStatus.CANCELLED,
    }
    approvals_by_id = {
        approval.id: approval
        for approval in await store.find_entities(
            Approval,
            {"chat_turn_id": turn.id},
            engagement_id=turn.engagement_id,
        )
    }
    calls_by_id = {call.id: call for call in calls}
    for call in calls:
        if call.approval_id:
            try:
                approval = await store.get(Approval, call.approval_id)
            except NotFoundError:
                raise CoordinationConflict(
                    "A failed provider tool call references a missing approval"
                ) from None
            if (
                approval.engagement_id != turn.engagement_id
                or approval.chat_turn_id != turn.id
                or approval.tool_call_id != call.id
            ):
                raise CoordinationConflict(
                    "A failed provider tool call references an approval for another project, turn, or call"
                )
            approvals_by_id[approval.id] = approval
        if call.status in safe_call_statuses:
            continue
        # A failed read has no external or persistent effect. Other failed
        # calls can have partially applied effects and require operator review.
        if (
            call.status == ToolCallStatus.FAILED
            and call.risk_class == RiskClass.LOCAL_READ
        ):
            continue
        raise CoordinationConflict(
            "A failed provider turn still has an unconfirmed tool effect"
        )

    hooks = await store.find_entities(
        NativeHookExecution,
        {"chat_turn_id": turn.id},
        engagement_id=turn.engagement_id,
    )
    if any(hook.status == "running" or hook.side_effects != "none" for hook in hooks):
        raise CoordinationConflict(
            "A failed provider turn still has an unconfirmed native-hook effect"
        )

    children = await store.find_entities(
        ChatSubagent,
        {"parent_turn_id": turn.id},
        engagement_id=turn.engagement_id,
    )
    if any(
        child.status == ChatSubagentStatus.RUNNING
        or child.pending_goal_charge_turn_id == turn.id
        for child in children
    ):
        raise CoordinationConflict(
            "A failed provider turn still has unfinished child work"
        )

    for call in calls:
        for approval in await store.find_entities(
            Approval,
            {"tool_call_id": call.id},
            engagement_id=turn.engagement_id,
        ):
            approvals_by_id[approval.id] = approval
    if turn.approval_id:
        try:
            approval = await store.get(Approval, turn.approval_id)
        except NotFoundError:
            raise CoordinationConflict(
                "A failed provider turn references a missing approval"
            ) from None
        else:
            if (
                approval.engagement_id != turn.engagement_id
                or approval.chat_turn_id != turn.id
            ):
                raise CoordinationConflict(
                    "A failed provider turn references an approval for another project or turn"
                )
            approvals_by_id[approval.id] = approval
    approvals = approvals_by_id.values()
    for approval in approvals:
        if (
            approval.engagement_id != turn.engagement_id
            or approval.chat_turn_id != turn.id
        ):
            raise CoordinationConflict(
                "A failed provider turn references an approval for another project or turn"
            )
        related_call = None
        if approval.tool_call_id:
            related_call = calls_by_id.get(approval.tool_call_id)
            if related_call is None:
                try:
                    related_call = await store.get(ToolCall, approval.tool_call_id)
                except NotFoundError:
                    raise CoordinationConflict(
                        "A failed provider approval references a missing tool call"
                    ) from None
            if (
                related_call.engagement_id != turn.engagement_id
                or related_call.chat_turn_id != turn.id
                or related_call.approval_id != approval.id
            ):
                raise CoordinationConflict(
                    "A failed provider approval references another project, turn, or tool call"
                )
        if approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED} and (
            related_call is None
            or (
                related_call.status not in safe_call_statuses
                and not (
                    related_call.status == ToolCallStatus.FAILED
                    and related_call.risk_class == RiskClass.LOCAL_READ
                )
            )
        ):
            raise CoordinationConflict(
                "A failed provider approval has no exact settled tool call"
            )
        continuation = approval.continuation
        if approval.status == ApprovalStatus.PENDING or (
            continuation is not None
            and (
                continuation.status in {"pending", "failed"}
                or continuation.adapter_status in {"pending", "failed", "unknown"}
            )
        ):
            raise CoordinationConflict(
                "A failed provider turn still has an unconfirmed approval effect"
            )


async def _release_harness_claims(session, transaction, project_id: str) -> list[str]:
    """Clear the dead owner's harness goal claims; their effects are not replayed.

    Recovery holds while any conversation work keeps a claim, and only the
    harness runtime's own restart settlement clears these, after activation.
    Releasing them here (whatever the turn's status: interrupted and cancelled
    goal turns can keep theirs) lets recovery verify; the new worker's startup
    then interrupts the unfinished turns and pauses their goals as after any
    Core restart.
    """
    await session.flush()
    rows = list(
        await session.scalars(
            select(EntityRow)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind.in_((ChatTurn.entity_kind, ChatGoal.entity_kind)),
                EntityRow.payload["execution_owner_id"].as_string()
                == HARNESS_CLAIM_OWNER,
                EntityRow.payload["execution_claim_id"].as_string().is_not(None),
            )
            .order_by(EntityRow.kind, EntityRow.id)
            .limit(MAX_HARNESS_CLAIMS + 1)
        )
    )
    if len(rows) > MAX_HARNESS_CLAIMS:
        raise CoordinationConflict(
            "Too many claimed harness responses; retain work for operator review"
        )
    released = []
    for row in rows:
        model = ChatTurn if row.kind == ChatTurn.entity_kind else ChatGoal
        await transaction.update(
            model,
            row.id,
            {
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
            },
            expected_revision=row.revision,
        )
        released.append(row.id)
    return released


async def _settle_interrupted_commands(session, owner: ProjectLease) -> list[str]:
    """Fail requests the dead owner left running, and earlier uncertain ones.

    No worker runs them again, so each answers its waiting client as failed
    instead of holding recovery for an outcome nobody can supply. Storage
    reports keep their own requeue in ``begin_recovery``.
    """
    rows = list(
        await session.scalars(
            select(RuntimeCommandRow)
            .where(
                RuntimeCommandRow.project_id == owner.project_id,
                RuntimeCommandRow.name != STORAGE_REPORT_COMMAND,
                (
                    (RuntimeCommandRow.state == "uncertain")
                    | (
                        (RuntimeCommandRow.state == "running")
                        & (RuntimeCommandRow.runtime_id == owner.runtime_id)
                        & (RuntimeCommandRow.fencing_token == owner.fencing_token)
                    )
                ),
            )
            .order_by(RuntimeCommandRow.sequence)
        )
    )
    for row in rows:
        row.result = {**INTERRUPTED_COMMAND_RESULT, "previous_state": row.state}
        row.state = "failed"
    return [row.id for row in rows]
