"""Execution-side safe-boundary inventory and durable-state restoration proof."""

from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import or_, select

from .database import (
    EntityRow,
    MemoryJobRow,
    ProjectHandoffRow,
    ProviderTurnQueueRow,
    RuntimeResourceRow,
)
from .deployments import checkpoint_digest
from .deployment_protocol import ResourceSnapshot
from .memory_budget import _HELD
from .runtime_coordination import CoordinationConflict

CHECKPOINT_FORMAT = "nebula.project-checkpoint/v1"
MAX_CHECKPOINT_ROWS = 10_000
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
}
_LIVE_SQL = {
    "automation_sessions": {"starting", "ready", "running", "active"},
    "command_executions": {"running", "interrupted"},
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
    """Bounded revision proof; payloads and credentials never enter a receipt."""
    rows = list(
        await session.execute(
            select(EntityRow.id, EntityRow.kind, EntityRow.revision)
            .where(
                or_(
                    EntityRow.engagement_id == project_id,
                    (EntityRow.kind == "engagements") & (EntityRow.id == project_id),
                )
            )
            .order_by(EntityRow.id)
            .limit(MAX_CHECKPOINT_ROWS + 1)
        )
    )
    queues = list(
        await session.execute(
            select(
                ProviderTurnQueueRow.turn_id,
                ProviderTurnQueueRow.state,
                ProviderTurnQueueRow.lease_owner,
            )
            .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
            .where(EntityRow.engagement_id == project_id)
            .order_by(ProviderTurnQueueRow.turn_id)
            .limit(MAX_CHECKPOINT_ROWS + 1)
        )
    )
    if len(rows) > MAX_CHECKPOINT_ROWS or len(queues) > MAX_CHECKPOINT_ROWS:
        raise CoordinationConflict(
            "This project exceeds the bounded handoff checkpoint; retain its current runtime until a paged adapter is available"
        )
    raw = json.dumps(
        [list(map(list, rows)), list(map(list, queues))], separators=(",", ":")
    ).encode()
    return {
        "format": CHECKPOINT_FORMAT,
        "project_id": project_id,
        "entity_count": len(rows),
        "queue_count": len(queues),
        "sql_sha256": hashlib.sha256(raw).hexdigest(),
    }


async def restored_checkpoint(coordinator, lease):
    async with coordinator.coordination.fenced(lease) as session:
        handoff = await session.get(ProjectHandoffRow, lease.project_id)
        if handoff is None or handoff.phase != "transferred":
            return None
        if (
            handoff.target_runtime_id != lease.runtime_id
            or handoff.checkpoint.get("format") != CHECKPOINT_FORMAT
        ):
            raise CoordinationConflict(
                "This runtime cannot restore the project's checkpoint"
            )
        if (
            await project_sql_checkpoint(session, lease.project_id)
            != handoff.checkpoint
        ):
            raise CoordinationConflict(
                "Project state changed before restoration; keep queued work held for reconciliation"
            )
        return checkpoint_digest(handoff.checkpoint)


class ProjectHandoffAgent:
    def __init__(self, context, lease, app, *, restored_digest=None):
        self.context, self.lease, self.app = context, lease, app
        self.coordinator = context.coordinator
        self.chat = app.state.control_assistant_work.chat
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

    async def tick(self):
        try:
            return await self._tick()
        except CoordinationConflict as error:
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
            if (
                handoff is None
                or (handoff.source_runtime_id, handoff.source_fencing_token)
                != (self.lease.runtime_id, self.lease.fencing_token)
                or handoff.phase not in {"requested", "finishing", "transferable"}
            ):
                return
            held = handoff.admission_held
        if held:
            await self.chat.park_queued_for_handoff()
        else:
            await self.chat.restore_locally_parked_turns()
        found = self.handles()
        async with self.coordinator.coordination.fenced(self.lease) as session:
            rows = await session.execute(
                select(
                    EntityRow.id,
                    EntityRow.kind,
                    EntityRow.payload["status"].as_string(),
                )
                .where(
                    EntityRow.engagement_id == self.lease.project_id,
                    EntityRow.kind.in_(tuple(_LIVE_SQL)),
                )
                .limit(MAX_CHECKPOINT_ROWS + 1)
            )
            for identity, kind, state in rows:
                if state in _LIVE_SQL[kind]:
                    found["sql/" + identity] = (
                        f"{kind} {identity} remains {state}; its restoration is unavailable"
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
            checkpoint = await project_sql_checkpoint(session, self.lease.project_id)
        # Optional naming/compaction must finish its authoritative writes too.
        busy = any(not task.done() for task in self.chat._naming_tasks)
        busy = busy or any(
            not item.task.done() for item in self.chat._precompactions.values()
        )
        return await self.coordinator.checkpoint(
            self.lease,
            checkpoint=checkpoint,
            blockers=["Conversation maintenance is finishing"] if busy else [],
        )
