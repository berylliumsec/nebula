"""Execution-side safe-boundary inventory and durable-state restoration proof."""

from __future__ import annotations

import asyncio
import hashlib
from itertools import groupby
import json

from sqlalchemy import Integer, String, literal, or_, select, union_all

from .database import (
    EntityRow,
    MemoryJobRow,
    ProjectHandoffRow,
    ProviderToolManifestRow,
    ProviderTurnQueueRow,
    RuntimeResourceRow,
    RuntimeRecoveryRow,
)
from .deployments import checkpoint_digest
from .deployment_protocol import ResourceSnapshot
from .memory_budget import _HELD
from .runtime_coordination import CoordinationConflict, source_handoff_active

CHECKPOINT_FORMAT = "nebula.project-checkpoint/v1"
CHECKPOINT_BATCH_ROWS = 10_000
_PREFIX = "handoff-inventory:"

# These handles have no implemented restoration adapter. Their presence is a
# concrete blocker, including idle vendor connections and background descendants.
_HANDLES = {
    "automation_runtime": ("_sessions", "_processes"),
    "notebooks": ("workers", "_drains"),
    "container_terminal_service": ("_sessions", "_finishing_sessions"),
    "debug_service": ("_sessions", "_attach_tasks"),
    "harness_runtime_service": (
        "_connections",
        "_active",
        "_mission_tasks",
        "_scheduled_mission_tasks",
        "_chat_turn_tasks",
        "_gateway_work",
    ),
    "mission_service": ("_tasks", "_scheduled_tasks"),
    "execution_service": ("_tasks",),
    "report_render_service": ("_tasks",),
    "execution_ai_service": ("_tasks",),
    # A storage alert being dispatched: its source callback and incident chat.
    "storage_integrations": ("_dispatching",),
}
_LIVE_SQL = {
    "automation_sessions": {"starting", "ready", "running", "active"},
    # An interrupted command has no process in this runtime: Core already
    # gave up on it, and a late results callback reaches whichever runtime
    # owns the project. It must not pin the project to this runtime forever.
    "command_executions": {"running"},
    "harness_sessions": {"starting", "idle", "running", "waiting_approval"},
    "harness_turns": {"running"},
    "native_hook_executions": {"running"},
    "tool_calls": {"running"},
    "browser_sessions": {"active", "paused"},
    "browser_automation_leases": {"active", "paused"},
    "notebook_runs": {"running"},
    "operator_executions": {"running", "starting"},
    "report_renders": {"running"},
    "runs": {"planning", "running", "waiting_approval", "cancelling"},
    "generated_drafts": {"generating"},
}


async def project_sql_checkpoint(session, project_id):
    """Stream one statement snapshot; retain the exact v1 reference-only digest."""
    entities = select(
        literal(0).label("inventory"),
        EntityRow.id.label("identity"),
        EntityRow.kind.label("value"),
        EntityRow.revision.label("revision"),
        literal(None, type_=String).label("lease_owner"),
    ).where(
        or_(
            EntityRow.engagement_id == project_id,
            (EntityRow.kind == "engagements") & (EntityRow.id == project_id),
        )
    )
    queues = (
        select(
            literal(1).label("inventory"),
            ProviderTurnQueueRow.turn_id.label("identity"),
            ProviderTurnQueueRow.state.label("value"),
            literal(None, type_=Integer).label("revision"),
            ProviderTurnQueueRow.lease_owner.label("lease_owner"),
        )
        .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
        .where(EntityRow.engagement_id == project_id)
    )
    manifests = select(
        literal(2).label("inventory"),
        ProviderToolManifestRow.session_id.label("identity"),
        ProviderToolManifestRow.digest.label("value"),
        ProviderToolManifestRow.revision.label("revision"),
        literal(None, type_=String).label("lease_owner"),
    ).where(ProviderToolManifestRow.project_id == project_id)
    statement = union_all(entities, queues, manifests).order_by(
        "inventory", "identity", "revision"
    )
    counts = [0, 0, 0]
    inventory = 0
    digest = hashlib.sha256()
    digest.update(b"[[")
    result = await session.stream(
        statement, execution_options={"yield_per": CHECKPOINT_BATCH_ROWS}
    )
    try:
        async for batch in result.partitions(CHECKPOINT_BATCH_ROWS):
            for kind, group in groupby(batch, key=lambda row: row.inventory):
                while inventory < kind:
                    digest.update(b"],[")
                    inventory += 1
                if kind == 0:
                    values = [[row.identity, row.value, row.revision] for row in group]
                elif kind == 1:
                    values = [
                        [row.identity, row.value, row.lease_owner] for row in group
                    ]
                else:
                    values = [[row.identity, row.revision, row.value] for row in group]
                if counts[kind]:
                    digest.update(b",")
                digest.update(json.dumps(values, separators=(",", ":"))[1:-1].encode())
                counts[kind] += len(values)
    finally:
        await result.close()
    while inventory < 1:
        digest.update(b"],[")
        inventory += 1
    digest.update(b"]]")
    return {
        "format": CHECKPOINT_FORMAT,
        "project_id": project_id,
        "entity_count": counts[0],
        "queue_count": counts[1],
        "sql_sha256": digest.hexdigest(),
    }


async def snapshot_project_checkpoint(coordinator, project_id, *, lease=None):
    """Scan without owning the heartbeat row; publication checks the generation.

    Triggers advance the generation in the same commit as every checkpoint
    input change, including writes from older workers. A change while scanning
    invalidates the result rather than letting a mixed snapshot be restored.
    The caller locks ownership, then validates this generation before using it.
    """
    from .runtime_checkpoint_generation import read_checkpoint_generation

    async with coordinator.database.session() as session:
        if lease is not None:
            await coordinator.coordination._check(session, lease, lock=False)
        generation = await read_checkpoint_generation(session, project_id)
        checkpoint = await project_sql_checkpoint(session, project_id)
        if await read_checkpoint_generation(session, project_id) != generation:
            raise CoordinationConflict(
                "Project changed during checkpoint scan; retry recovery"
            )
        if lease is not None:
            await coordinator.coordination._check(session, lease, lock=False)
    return checkpoint, generation


async def restored_checkpoint(coordinator, lease):
    from .runtime_checkpoint_generation import assert_checkpoint_generation

    # Recovery preparation does its own unlocked scan. Ordinary handoff must
    # compare the accepted checkpoint as well, without pinning heartbeat SQL.
    async with coordinator.database.session() as session:
        await coordinator.coordination._check(session, lease, lock=False)
        pending = await session.scalar(
            select(RuntimeRecoveryRow.operation_id)
            .where(
                RuntimeRecoveryRow.project_id == lease.project_id,
                RuntimeRecoveryRow.target_runtime_id == lease.runtime_id,
                RuntimeRecoveryRow.target_fencing_token == lease.fencing_token,
                RuntimeRecoveryRow.phase.in_(("held", "prepared")),
            )
            .limit(1)
        )
    if pending is not None:
        return await coordinator.prepare_recovery(lease, operation_id=pending)
    async with coordinator.database.session() as session:
        handoff = await session.get(ProjectHandoffRow, lease.project_id)
        if handoff is None or handoff.phase != "transferred":
            return None
    checkpoint, generation = await snapshot_project_checkpoint(
        coordinator, lease.project_id, lease=lease
    )
    async with coordinator.coordination.fenced(lease) as session:
        await assert_checkpoint_generation(session, lease.project_id, generation)
        recovery = await session.scalar(
            select(RuntimeRecoveryRow)
            .where(
                RuntimeRecoveryRow.project_id == lease.project_id,
                RuntimeRecoveryRow.target_runtime_id == lease.runtime_id,
                RuntimeRecoveryRow.target_fencing_token == lease.fencing_token,
                RuntimeRecoveryRow.phase.in_(("held", "prepared")),
            )
            .limit(1)
        )
        recovery_id = recovery.operation_id if recovery else None
        handoff = await session.get(ProjectHandoffRow, lease.project_id)
        if recovery_id is not None:
            pass
        elif handoff is None or handoff.phase != "transferred":
            return None
        elif (
            handoff.target_runtime_id != lease.runtime_id
            or handoff.checkpoint.get("format") != CHECKPOINT_FORMAT
        ):
            raise CoordinationConflict(
                "This runtime cannot restore the project's checkpoint"
            )
        elif checkpoint != handoff.checkpoint:
            raise CoordinationConflict(
                "Project state changed before restoration; keep queued work held for reconciliation"
            )
        if recovery_id is None:
            return checkpoint_digest(handoff.checkpoint)
    return await coordinator.prepare_recovery(lease, operation_id=recovery_id)


async def activation_allowed(coordinator, lease):
    """A prepared application starts producers only after durable release."""
    async with coordinator.coordination.fenced(lease) as session:
        recovery = await session.scalar(
            select(RuntimeRecoveryRow)
            .where(
                RuntimeRecoveryRow.project_id == lease.project_id,
                RuntimeRecoveryRow.target_runtime_id == lease.runtime_id,
                RuntimeRecoveryRow.target_fencing_token == lease.fencing_token,
            )
            .order_by(RuntimeRecoveryRow.created_at_ms.desc())
            .limit(1)
        )
        if recovery is not None and recovery.phase != "verified":
            raise CoordinationConflict("Project recovery has not been verified")
        handoff = await session.get(ProjectHandoffRow, lease.project_id)
        if (
            handoff is None
            or handoff.phase != "verified"
            or handoff.admission_held
            or handoff.target_runtime_id != lease.runtime_id
        ):
            raise CoordinationConflict(
                "Project restoration has not been verified; queued work remains held"
            )


class ProjectHandoffAgent:
    def __init__(self, context, lease, app, *, restored_digest=None):
        self.context, self.lease, self.app = context, lease, app
        self.coordinator = context.coordinator
        self.chat = app.state.control_assistant_work.chat
        self.queue = getattr(app.state.control_assistant_work, "queue", None)
        self.restored_digest = restored_digest

    def handles(self):
        found = {}
        for name, attributes in _HANDLES.items():
            participant = getattr(self.app.state, name, None)
            if participant is None:
                raise CoordinationConflict(
                    "The handoff participant is unavailable: " + name
                )
            for attribute in attributes:
                values = getattr(participant, attribute, None)
                if not isinstance(values, dict):
                    raise CoordinationConflict(
                        "The handoff participant contract changed: "
                        + name
                        + "."
                        + attribute
                    )
                active = sum(
                    not value.done()
                    if isinstance(value, (asyncio.Task, asyncio.Future))
                    else bool(value)
                    for value in values.values()
                )
                if active:
                    found[name + "." + attribute] = (
                        f"{active} resident handle(s) in {name} require their current runtime"
                    )
        return found

    def dispatch_blockers(self) -> list[str]:
        """Name dispatches whose only receipt is still in this process.

        A follow-up claimed by this worker's dispatcher but not yet linked to a
        durable turn looks settled in SQL; a later owner would review it as a
        crash. A Butler action between its dispatching commit and its saved
        outcome could be recorded only as uncertain. Handoff and quiescence
        both refuse while either is in flight.
        """
        blockers = []
        if self.queue is not None and getattr(self.queue, "_inflight", None):
            blockers.append("A queued follow-up is being dispatched")
        butler = getattr(self.app.state, "butler_authority", None)
        if butler is not None and getattr(butler, "_dispatching", None):
            blockers.append("A Butler action is being dispatched")
        return blockers

    async def tick(self):
        try:
            return await self._tick()
        except CoordinationConflict as error:
            # diagnostic-expected: unsupported handoff adapters retain ownership and persist the actual blockers.
            # Unsupported inventory/checkpoint adapters retain a usable old
            # project and publish the actual blocker, without a kill deadline.
            await self.coordinator.resource(
                self.lease,
                ResourceSnapshot(
                    id=_PREFIX
                    + hashlib.sha256(
                        (self.lease.project_id + "\0contract").encode()
                    ).hexdigest(),
                    project_id=self.lease.project_id,
                    runtime_id=self.lease.runtime_id,
                    fencing_token=self.lease.fencing_token,
                    kind="resident_handle",
                    state="uncertain",
                    restoration="pinned",
                    detail=str(error)[:4000],
                ),
            )
            return await self.coordinator.checkpoint(
                self.lease, checkpoint={}, blockers=[str(error)[:4000]]
            )

    async def _tick(self):
        async with self.coordinator.coordination.fenced(self.lease) as session:
            handoff = await session.get(ProjectHandoffRow, self.lease.project_id)
            # The idle probe uses the same predicate to decide when to tick.
            if not source_handoff_active(handoff, self.lease):
                return
            held = handoff.admission_held
        if held:
            prepare_callbacks = getattr(self.chat, "prepare_handoff_callbacks", None)
            if prepare_callbacks is not None:
                await prepare_callbacks()
            await self.chat.park_queued_for_handoff()
        else:
            await self.chat.restore_locally_parked_turns()
        found = self.handles()
        callback_blockers = getattr(self.chat, "handoff_callback_blockers", None)
        if callback_blockers is not None:
            found.update(await callback_blockers())
        async with self.coordinator.database.session() as session:
            await self.coordinator.coordination._check(session, self.lease, lock=False)
            from .runtime_checkpoint_generation import read_checkpoint_generation

            inventory_generation = await read_checkpoint_generation(
                session, self.lease.project_id
            )
            rows = await session.execute(
                select(
                    EntityRow.id,
                    EntityRow.kind,
                    EntityRow.payload["status"].as_string(),
                    EntityRow.payload["execution_claim_id"].as_string(),
                )
                .where(
                    EntityRow.engagement_id == self.lease.project_id,
                    or_(
                        (
                            EntityRow.kind.in_(("chat_turns", "chat_goals"))
                            & EntityRow.payload["execution_claim_id"]
                            .as_string()
                            .is_not(None)
                        ),
                        *(
                            (EntityRow.kind == kind)
                            & EntityRow.payload["status"].as_string().in_(states)
                            for kind, states in _LIVE_SQL.items()
                        ),
                    ),
                )
                .limit(101)
            )
            claims = rows.all()
            if len(claims) > 100:
                raise CoordinationConflict(
                    "More than 100 active project records need reconciliation; retain their runtime for review"
                )
            is_active = getattr(
                self.chat, "has_active_provider_turn", lambda _identity: False
            )
            active_claims = {
                claim
                for identity, kind, _state, claim in claims
                if kind == "chat_turns" and claim and is_active(identity)
            }
            for identity, kind, state, execution_claim in claims:
                if execution_claim and execution_claim not in active_claims:
                    found["claim/" + identity] = (
                        f"{kind} {identity} still owns an execution claim; finish its admitted work before transfer"
                    )
                if state in _LIVE_SQL.get(kind, ()):
                    found["sql/" + identity] = (
                        f"{kind} {identity} remains {state}; its restoration is unavailable"
                    )
            deliveries = await session.scalars(
                select(EntityRow.id)
                .where(
                    EntityRow.engagement_id == self.lease.project_id,
                    EntityRow.kind == "approvals",
                    EntityRow.payload["continuation"]["adapter_status"]
                    .as_string()
                    .in_(("pending", "unknown")),
                )
                .limit(101)
            )
            for identity in deliveries:
                found["approval/" + identity] = (
                    f"Approval {identity} has unconfirmed adapter delivery; retain its owner without replay"
                )
            jobs = list(
                await session.scalars(
                    select(MemoryJobRow.id)
                    .where(
                        MemoryJobRow.project_id == self.lease.project_id,
                        MemoryJobRow.state.in_(_HELD),
                    )
                    .limit(257)
                )
            )
            for identity in jobs:
                found["memory/" + identity] = (
                    f"Compute job {identity} retains charged resources on this runtime"
                )
            if len(found) > 100:
                raise CoordinationConflict(
                    "More than 100 resident resources require this project; retain its runtime and review the resource inventory"
                )
        checkpoint, generation = await snapshot_project_checkpoint(
            self.coordinator, self.lease.project_id, lease=self.lease
        )
        if generation != inventory_generation:
            raise CoordinationConflict(
                "Project changed during handoff inventory; retry"
            )
        from .runtime_checkpoint_generation import assert_checkpoint_generation

        async with self.coordinator.coordination.fenced(self.lease) as session:
            await assert_checkpoint_generation(
                session, self.lease.project_id, generation
            )
            current_jobs = set(
                await session.scalars(
                    select(MemoryJobRow.id)
                    .where(
                        MemoryJobRow.project_id == self.lease.project_id,
                        MemoryJobRow.state.in_(_HELD),
                    )
                    .limit(257)
                )
            )
            if current_jobs != set(jobs):
                raise CoordinationConflict(
                    "Compute jobs changed during handoff inventory; retry"
                )
            now = await self.coordinator.coordination._clock_ms(session)
            current = {}
            for key, detail in found.items():
                identity = (
                    _PREFIX
                    + hashlib.sha256(
                        (self.lease.project_id + "\0" + key).encode()
                    ).hexdigest()
                )
                current[identity] = detail
            for row in await session.scalars(
                select(RuntimeResourceRow).where(
                    RuntimeResourceRow.project_id == self.lease.project_id,
                    RuntimeResourceRow.id.startswith(_PREFIX, autoescape=True),
                )
            ):
                if (row.runtime_id, row.fencing_token) != (
                    self.lease.runtime_id,
                    self.lease.fencing_token,
                ):
                    continue
                row.state, row.updated_at_ms = (
                    ("active" if row.id in current else "closed"),
                    now,
                )
                if row.id in current:
                    row.detail = current.pop(row.id)
            for identity, detail in current.items():
                session.add(
                    RuntimeResourceRow(
                        id=identity,
                        project_id=self.lease.project_id,
                        runtime_id=self.lease.runtime_id,
                        fencing_token=self.lease.fencing_token,
                        kind="resident_handle",
                        state="active",
                        restoration="pinned",
                        detail=detail,
                        checkpoint={},
                        updated_at_ms=now,
                    )
                )
        # Optional naming/compaction must finish its authoritative writes too.
        busy = any(not task.done() for task in self.chat._naming_tasks)
        busy = busy or any(
            not item.task.done() for item in self.chat._precompactions.values()
        )
        blockers = ["Conversation maintenance is finishing"] if busy else []
        # Read after the checkpoint: a claim or Butler dispatch committed
        # before that read is still in flight here, or its turn or outcome has
        # since changed the checkpointed revisions and transfer refuses the
        # stale proof. One starting later is refused by that check or fenced
        # before its action leaves planned.
        blockers.extend(self.dispatch_blockers())
        return await self.coordinator.checkpoint(
            self.lease, checkpoint=checkpoint, blockers=blockers
        )
