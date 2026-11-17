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
from .chat_goals import active_time_changes
from .deployments import DeploymentCoordinator
from .domain import (
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
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


def orphan_receipt_key(expected_owner: ProjectLease) -> str:
    """The durable receipt that records one expired owner's reconciliation."""
    identity = hashlib.sha256(
        (
            expected_owner.project_id
            + "\0"
            + expected_owner.runtime_id
            + "\0"
            + str(expected_owner.fencing_token)
        ).encode()
    ).hexdigest()
    return "orphan-recovery:" + identity


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
    receipt_key = orphan_receipt_key(expected_owner)
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
                # An earlier reconciliation left the dead owner's capacity
                # held; settling it again is idempotent and replays nothing.
                await _settle_dead_owner_capacity(session, store, expected_owner, now)
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
                    waiting_subagent = pending.get(
                        "status"
                    ) == "waiting_callback" and isinstance(
                        pending.get("subagent_wait"), dict
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
            (
                released_ended_claims,
                review_turns,
            ) = await _release_ended_provider_claims(
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
                        and turn.id not in released_ended_claims
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
            released_capacity += await _settle_dead_owner_capacity(
                session, store, expected_owner, now
            )
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
                "released_failed_provider_claim_ids": released_ended_claims,
                "goal_review_turn_ids": review_turns,
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


async def _settle_dead_owner_capacity(
    session, store: NebulaStore, owner: ProjectLease, now: int
) -> list[str]:
    """Release host capacity that only a provably dead owner still holds.

    A worker reserves capacity before it admits a provider turn and releases
    it after the admission ends, each in its own commit, and holds a ``tool:``
    slot around every tool call. Death in any of those gaps leaves a held
    reservation that no living process will release, and recovery would wait
    on it forever. The expected owner was proven dead and expired by the
    caller; an earlier owner of this project counts only when its own process
    is gone too. Capacity is accounting: releasing it replays nothing. A
    reservation whose effect has durable completion evidence (the process
    that made a provider call is gone; a tool call reached a final state) is
    released as ended, and any other as of unknown outcome, while the effect's
    own record (a tool call, an interrupted turn) stays as it is for review.
    """
    await session.flush()
    rows = list(
        await session.scalars(
            select(RuntimeCapacityReservationRow)
            .where(
                RuntimeCapacityReservationRow.project_id == owner.project_id,
                RuntimeCapacityReservationRow.state == "held",
                or_(
                    and_(
                        RuntimeCapacityReservationRow.runtime_id == owner.runtime_id,
                        RuntimeCapacityReservationRow.fencing_token
                        == owner.fencing_token,
                    ),
                    RuntimeCapacityReservationRow.fencing_token < owner.fencing_token,
                ),
            )
            .order_by(RuntimeCapacityReservationRow.id)
            .limit(MAX_ORPHAN_ROWS + 1)
        )
    )
    if len(rows) > MAX_ORPHAN_ROWS:
        raise CoordinationConflict(
            "Too many held capacity reservations; retain them for operator review"
        )
    gone: dict[str, bool] = {owner.runtime_id: True}
    released = []
    for row in rows:
        if row.runtime_id not in gone:
            process = await session.get(RuntimeProcessRow, row.runtime_id)
            gone[row.runtime_id] = (
                process is None
                or not process.pid
                or not same_process(process.pid, process.process_birth)
            )
        if not gone[row.runtime_id]:
            # A live earlier owner still releases its own reservation.
            continue
        ended = row.request_key.startswith("provider:")
        if row.request_key.startswith("tool:"):
            try:
                call = await store.get(ToolCall, row.request_key[len("tool:") :])
            except NotFoundError:
                # diagnostic-expected: no durable call record means no
                # completion evidence; the slot is released as unknown.
                call = None
            ended = call is not None and call.status in {
                ToolCallStatus.COMPLETE,
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }
        row.state = "released"
        row.release_reason = (
            "dead-owner-effect-ended" if ended else "dead-owner-outcome-unknown"
        )
        row.released_at_ms = now
        released.append(row.id)
    return released


# A turn in one of these states produces nothing more. Its worker saves it
# before releasing its claim in a separate commit, so a death in between
# leaves an ended turn (and its goal) claimed by nobody alive.
ENDED_TURN_STATUSES = (
    ChatTurnStatus.COMPLETE,
    ChatTurnStatus.FAILED,
    ChatTurnStatus.CANCELLED,
    ChatTurnStatus.INTERRUPTED,
)


async def _release_ended_provider_claims(
    session,
    store: NebulaStore,
    transaction: StoreTransaction,
    project_id: str,
) -> tuple[list[str], list[str]]:
    """Release the dead owner's claims on provider turns that already ended.

    The turn is not replayed or rewritten, and a goal whose last turn settled
    every effect keeps running. When a durable child, approval, tool or hook
    record does not prove its effect settled, that record stays as it is and
    a running goal is paused for operator review instead of continuing; the
    project itself is not held.

    Returns the released turn IDs and those whose goal was held for review.
    """
    rows = list(
        await session.scalars(
            select(EntityRow)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.payload["backend"].as_string() == ChatBackend.PROVIDER.value,
                EntityRow.payload["status"]
                .as_string()
                .in_([status.value for status in ENDED_TURN_STATUSES]),
                EntityRow.payload["execution_claim_id"].as_string().is_not(None),
            )
            .order_by(EntityRow.id)
            .limit(MAX_ORPHAN_ROWS + 1)
        )
    )
    if len(rows) > MAX_ORPHAN_ROWS:
        raise CoordinationConflict(
            "Too many ended provider claims; retain work for operator review"
        )

    released: list[str] = []
    held: list[str] = []
    now = utc_now()
    for row in rows:
        turn = ChatTurn.model_validate(row.payload)
        try:
            await _require_settled_failed_turn_effects(store, turn)
            unconfirmed = None
        except CoordinationConflict as error:
            # diagnostic-expected: the unconfirmed effect stays recorded and
            # its goal is held below; only the dead owner's claim is released.
            unconfirmed = str(error)
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
        released.append(turn.id)
        goal_row = await session.get(EntityRow, turn.goal_id) if turn.goal_id else None
        if goal_row is None or goal_row.kind != ChatGoal.entity_kind:
            continue
        goal = ChatGoal.model_validate(goal_row.payload)
        if goal.engagement_id != project_id:
            continue
        changes: dict = {}
        if goal.execution_claim_id == turn.execution_claim_id:
            changes.update(
                {
                    "execution_owner_id": None,
                    "execution_claim_id": None,
                    "execution_claimed_at": None,
                    **active_time_changes(goal, working=False, now=now),
                }
            )
        if unconfirmed is not None and goal.status == ChatGoalStatus.RUNNING:
            changes.update(
                {
                    "status": ChatGoalStatus.PAUSED,
                    "paused_at": now,
                    "active_since": None,
                    "elapsed_seconds": goal.active_elapsed_seconds(now),
                    "blocked_reason": (
                        "Project runtime stopped after a response ended while "
                        f"an effect was unconfirmed ({unconfirmed.rstrip('.')}). "
                        "Review it before resuming; no effect has been retried."
                    ),
                }
            )
            held.append(turn.id)
        if changes:
            await transaction.update(
                ChatGoal, goal.id, changes, expected_revision=goal.revision
            )
    # A goal claim no turn holds any more belongs only to the dead owner (a
    # claim is taken on a turn and its goal together); nothing can release
    # it after this, and recovery would wait on it forever.
    await session.flush()
    for row in await session.scalars(
        select(EntityRow)
        .where(
            EntityRow.engagement_id == project_id,
            EntityRow.kind == ChatGoal.entity_kind,
            EntityRow.payload["execution_claim_id"].as_string().is_not(None),
            or_(
                EntityRow.payload["execution_owner_id"].as_string().is_(None),
                EntityRow.payload["execution_owner_id"].as_string()
                != HARNESS_CLAIM_OWNER,
            ),
        )
        .order_by(EntityRow.id)
        .limit(MAX_ORPHAN_ROWS)
    ):
        goal = ChatGoal.model_validate(row.payload)
        if await session.scalar(
            select(EntityRow.id)
            .where(
                EntityRow.engagement_id == project_id,
                EntityRow.kind == ChatTurn.entity_kind,
                EntityRow.payload["execution_claim_id"].as_string()
                == goal.execution_claim_id,
            )
            .limit(1)
        ):
            continue
        await transaction.update(
            ChatGoal,
            goal.id,
            {
                "execution_owner_id": None,
                "execution_claim_id": None,
                "execution_claimed_at": None,
                **active_time_changes(goal, working=False, now=now),
            },
            expected_revision=goal.revision,
        )
        released.append(goal.id)
    return released, held


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
