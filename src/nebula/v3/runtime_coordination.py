"""SQL coordination primitives for the staged multi-process Core rollout.

No process-local lock is an authority. Writers hold the project's SQL row lock
and recheck the database clock before commit. Network calls must happen outside
``fenced``: a lost lease cannot undo an external effect. A claimed command left
by an expired owner becomes uncertain, never automatically queued again.

These primitives deliberately do not enable overlapping legacy Core processes.
The worker extraction must route *all* execution writes through this boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import BigInteger, cast, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import (
    Database,
    DeploymentHostRow,
    EntityRow,
    ProjectOwnershipRow,
    RunEventRow,
    RuntimeCommandRow,
    RuntimeInstanceRow,
    ProjectHandoffRow,
    RuntimeProcessRow,
)
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .domain import utc_now

HEARTBEAT_SECONDS = 5
LEASE_MS = 30_000
MAX_STREAM_BATCH = 256
STREAM_EPOCH = "durable-v1"
_STREAM_PREFIX = "chat_stream."
COMMAND_LANES = ("work", "control")
# A source owner keeps ticking its handoff only in these phases.
TICK_PHASES = frozenset({"requested", "finishing", "transferable"})
_DISPATCH_STATES = ("queued", "running", "uncertain")
_BATCH_BARRIER = "work.batch.barrier/v1"


class CoordinationConflict(RuntimeError):
    """A request conflicts with durable ownership or a previous receipt."""


class LeaseLost(CoordinationConflict):
    """The caller must stop admitting work and discard authoritative writes."""


class OperationRejected(CoordinationConflict):
    """A dispatcher proved that no effect was admitted before refusing."""


class ShutdownBlocked(OperationRejected):
    """Durable work refused shutdown before anything changed; the text names it."""


class StartAborted(OperationRejected):
    """A worker start failed and its abort was confirmed: nothing was started."""


class DeploymentChanged(OperationRejected):
    """The deployment revision moved after the caller read it; nothing changed.

    A caller whose request depends only on the current state, not on what it
    reviewed, may read the new revision and ask again.
    """


class ProjectRetiring(CoordinationConflict):
    """The project's worker is releasing it; the same request can be retried."""


def project_worker_commands(project_id, states: Sequence[str] = ("queued",)) -> Any:
    """SQL EXISTS for commands in ``states`` that only the project's worker runs.

    Project edits, deletion and every other project-routed write are delivered
    to the execution worker, so a queued one needs a worker even when the
    project has no open chat. Work-batch barriers are committed by the host.
    A running row without an owner belongs to an earlier worker; recovery or
    operator review resolves it, not a new worker.
    """
    return (
        select(RuntimeCommandRow.id)
        .where(
            RuntimeCommandRow.project_id == project_id,
            RuntimeCommandRow.project_id != GLOBAL_EXECUTION_PROJECT,
            RuntimeCommandRow.state.in_(tuple(states)),
            RuntimeCommandRow.name != _BATCH_BARRIER,
        )
        .exists()
    )


@dataclass(frozen=True)
class ProjectLease:
    project_id: str
    runtime_id: str
    fencing_token: int


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    project_id: str
    sequence: int
    accepted_at_ms: int


@dataclass(frozen=True)
class ClaimedCommand:
    receipt: CommandReceipt
    name: str
    payload: dict[str, Any]
    expected_revision: int | None


@dataclass(frozen=True)
class StreamFrame:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    epoch: str = STREAM_EPOCH


@dataclass(frozen=True)
class DispatchSnapshot:
    """A lock-free read of what a fenced claim for this owner would do now.

    It is a wake-up hint, never an authority: only ``claim_next`` changes state
    and it re-reads everything under the project's fence.
    """

    owned: bool
    lanes: frozenset[str]
    handoff_active: bool


def dispatch_holds(handoff, host, ownership) -> tuple[bool, bool]:
    """Return (held, retiring) for a project's command lanes.

    Shared by ``claim_next`` and ``dispatch_snapshot`` so an idle probe cannot
    disagree with the claim about which lanes may start a command.
    """
    retiring = ownership is not None and bool(ownership.retirement_requested)
    held = (
        (handoff is not None and handoff.admission_held and handoff.phase != "verified")
        or (host is not None and bool(host.shutdown_requested))
        or retiring
    )
    return held, retiring


def head_action(head, lease: ProjectLease) -> Literal["claim", "uncertain"] | None:
    """What a fenced claim does with a lane's head command; None is an empty poll."""
    if head is None or head.state == "uncertain":
        return None
    if head.state == "queued" and head.name == _BATCH_BARRIER:
        # The host commits this typed SQL barrier with its whole batch.
        # Keep it at the lane head so later work cannot overtake it.
        return None
    if head.state == "running":
        if (head.runtime_id, head.fencing_token) != (
            lease.runtime_id,
            lease.fencing_token,
        ):
            # The old worker could have made an external call. Fencing
            # cannot tell us its outcome; do not execute it a second time.
            return "uncertain"
        return None
    return "claim"


def source_handoff_active(handoff, lease: ProjectLease) -> bool:
    """True while this exact owner must keep ticking a handoff as its source."""
    return (
        handoff is not None
        and (handoff.source_runtime_id, handoff.source_fencing_token)
        == (lease.runtime_id, lease.fencing_token)
        and handoff.phase in TICK_PHASES
    )


def _identifier(value: str, name: str, maximum: int = 200) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _receipt(row: RuntimeCommandRow) -> CommandReceipt:
    return CommandReceipt(row.id, row.project_id, row.sequence, row.accepted_at_ms)


class RuntimeCoordination:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _clock_ms(self, session: AsyncSession) -> int:
        # PostgreSQL CURRENT_TIMESTAMP freezes at transaction start; using it
        # here would let a long transaction commit after its lease expires.
        if self.database.engine.dialect.name == "postgresql":
            value = func.extract("epoch", func.clock_timestamp()) * 1000
        else:
            value = (func.julianday("now") - 2440587.5) * 86_400_000
        now = await session.scalar(select(cast(value, BigInteger)))
        assert now is not None
        return int(now)

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[AsyncSession]:
        if self.database.task_session() is not None:
            raise RuntimeError("coordination writes require their own transaction")
        async with self.database.session() as session:
            if self.database.engine.dialect.name == "sqlite":
                # Acquire the writer before any read to avoid upgrading a stale
                # WAL snapshot. The bounded DB timeout is the only retry here.
                await session.execute(text("BEGIN IMMEDIATE"))
            yield session

    async def _insert_absent(self, session, row, values, keys) -> None:
        statement = (
            pg_insert
            if self.database.engine.dialect.name == "postgresql"
            else sqlite_insert
        )
        await session.execute(
            statement(row).values(**values).on_conflict_do_nothing(index_elements=keys)
        )

    async def register_runtime(
        self,
        runtime_id: str,
        version: str,
        *,
        role: Literal["execution", "api", "supervisor"] = "execution",
    ) -> None:
        _identifier(runtime_id, "runtime_id")
        _identifier(version, "version")
        if role not in {"execution", "api", "supervisor"}:
            raise ValueError("unknown runtime role")
        async with self._write() as session:
            now = await self._clock_ms(session)
            await self._insert_absent(
                session,
                RuntimeInstanceRow,
                {
                    "id": runtime_id,
                    "version": version,
                    "role": role,
                    "heartbeat_at_ms": now,
                    "lease_expires_at_ms": now + LEASE_MS,
                },
                ["id"],
            )
            row = await session.get(RuntimeInstanceRow, runtime_id)
            assert row is not None
            if (
                row.version != version
                or row.role != role
                or row.lease_expires_at_ms <= now
            ):
                raise CoordinationConflict(
                    "runtime incarnation cannot be replaced or revived"
                )

    async def heartbeat(self, runtime_id: str) -> None:
        async with self._write() as session:
            await session.execute(
                update(RuntimeInstanceRow)
                .where(
                    RuntimeInstanceRow.id == runtime_id,
                )
                .values(heartbeat_at_ms=RuntimeInstanceRow.heartbeat_at_ms)
            )
            await session.execute(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.runtime_id == runtime_id,
                )
                .values(command_sequence=ProjectOwnershipRow.command_sequence)
            )
            # A heartbeat may have waited behind an authoritative transaction.
            # Its old timestamp must not resurrect a lease that expired there.
            now = await self._clock_ms(session)
            renewed = await session.execute(
                update(RuntimeInstanceRow)
                .where(
                    RuntimeInstanceRow.id == runtime_id,
                    RuntimeInstanceRow.lease_expires_at_ms > now,
                )
                .values(heartbeat_at_ms=now, lease_expires_at_ms=now + LEASE_MS)
            )
            if renewed.rowcount != 1:
                raise LeaseLost("runtime lease expired; register a new incarnation")
            await session.execute(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.runtime_id == runtime_id,
                    ProjectOwnershipRow.lease_expires_at_ms > now,
                )
                .values(lease_expires_at_ms=now + LEASE_MS)
            )

    async def _ensure_project(self, session: AsyncSession, project_id: str) -> None:
        exists = await session.scalar(
            select(EntityRow.id).where(
                EntityRow.id == project_id,
                EntityRow.kind == "engagements",
            )
        )
        if exists is None:
            raise CoordinationConflict("project does not exist")
        # The counter remains after release so no incarnation can reuse a token.
        await self._insert_absent(
            session,
            ProjectOwnershipRow,
            {
                "project_id": project_id,
                "runtime_id": None,
                "fencing_token": 0,
                "lease_expires_at_ms": 0,
                "command_sequence": 0,
                "admission_requested": False,
                "retirement_requested": False,
            },
            ["project_id"],
        )

    async def _live_execution(
        self, session: AsyncSession, runtime_id: str, now: int
    ) -> int:
        # Admission and retirement share the process-row lock. A readiness
        # snapshot alone could admit an owner after shutdown saw an empty worker.
        process = await session.get(
            RuntimeProcessRow,
            runtime_id,
            with_for_update={"read": True},
            populate_existing=True,
        )
        if process is not None and process.state != "ready":
            raise LeaseLost("execution process is not admitting authoritative work")
        now = await self._clock_ms(session)
        found = await session.scalar(
            select(RuntimeInstanceRow.id).where(
                RuntimeInstanceRow.id == runtime_id,
                RuntimeInstanceRow.role == "execution",
                RuntimeInstanceRow.lease_expires_at_ms > now,
            )
        )
        if found is None:
            raise LeaseLost("a live execution runtime is required")
        return now

    async def claim_project(self, project_id: str, runtime_id: str) -> ProjectLease:
        _identifier(project_id, "project_id")
        async with self._write() as session:
            host = await session.get(
                DeploymentHostRow,
                "host",
                with_for_update={"read": True},
                populate_existing=True,
            )
            if host is not None and host.shutdown_requested:
                raise CoordinationConflict(
                    "the host is draining for coordinated shutdown"
                )
            await self._ensure_project(session, project_id)
            owner = await session.get(ProjectOwnershipRow, project_id)
            if owner is not None and owner.retirement_requested:
                raise CoordinationConflict(
                    "the project worker is closing; retry chat admission after retirement"
                )
            # UPDATE is a row lock on PostgreSQL and a writer lock on SQLite.
            # Read the clock after waiting for the lock, not before it.
            await session.execute(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.project_id == project_id,
                    ProjectOwnershipRow.retirement_requested.is_(False),
                )
                .values(command_sequence=ProjectOwnershipRow.command_sequence)
            )
            still_admitting = await session.scalar(
                select(ProjectOwnershipRow.project_id).where(
                    ProjectOwnershipRow.project_id == project_id,
                    ProjectOwnershipRow.retirement_requested.is_(False),
                )
            )
            if still_admitting is None:
                raise CoordinationConflict(
                    "the project worker is closing; retry chat admission after retirement"
                )
            now = await self._clock_ms(session)
            now = await self._live_execution(session, runtime_id, now)
            from .runtime_context_requirements import require_runtime_context

            await require_runtime_context(session, project_id, runtime_id)
            # Compatibility reads can outlast a lease. Validate the runtime and
            # start the ownership grant from the clock after those reads.
            now = await self._live_execution(session, runtime_id, now)
            token = await session.scalar(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.project_id == project_id,
                    or_(
                        ProjectOwnershipRow.runtime_id.is_(None),
                        ProjectOwnershipRow.lease_expires_at_ms <= now,
                    ),
                )
                .values(
                    runtime_id=runtime_id,
                    fencing_token=ProjectOwnershipRow.fencing_token + 1,
                    lease_expires_at_ms=now + LEASE_MS,
                )
                .returning(ProjectOwnershipRow.fencing_token)
            )
            if token is None:
                raise CoordinationConflict("project still has a live execution owner")
            return ProjectLease(project_id, runtime_id, token)

    async def _check(
        self, session: AsyncSession, lease: ProjectLease, *, lock: bool
    ) -> None:
        if lock:
            await session.execute(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.project_id == lease.project_id,
                )
                .values(command_sequence=ProjectOwnershipRow.command_sequence)
            )
        now = await self._clock_ms(session)
        now = await self._live_execution(session, lease.runtime_id, now)
        found = await session.scalar(
            select(ProjectOwnershipRow.project_id).where(
                ProjectOwnershipRow.project_id == lease.project_id,
                ProjectOwnershipRow.runtime_id == lease.runtime_id,
                ProjectOwnershipRow.fencing_token == lease.fencing_token,
                ProjectOwnershipRow.lease_expires_at_ms > now,
            )
        )
        if found is None:
            raise LeaseLost("project ownership changed or expired")

    @asynccontextmanager
    async def fenced(self, lease: ProjectLease) -> AsyncIterator[AsyncSession]:
        """Commit short SQL changes only while this exact owner is authoritative."""
        async with self._write() as session:
            await self._check(session, lease, lock=True)
            yield session
            await session.flush()
            await self._check(session, lease, lock=False)

    async def submit(
        self,
        *,
        project_id: str,
        actor_id: str,
        idempotency_key: str,
        name: str,
        payload: dict[str, Any],
        expected_revision: int | None = None,
        lane: Literal["work", "control"] = "work",
        chat_start: bool = False,
    ) -> CommandReceipt:
        for label, value in (
            ("project_id", project_id),
            ("actor_id", actor_id),
            ("idempotency_key", idempotency_key),
        ):
            _identifier(value, label)
        _identifier(name, "name", 100)
        if lane not in {"work", "control"}:
            raise ValueError("unknown command lane")
        if type(chat_start) is not bool:
            raise ValueError("chat_start must be a boolean")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("expected_revision must be positive")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        payload = json.loads(_json(payload))
        encoded = _json(
            {
                "project_id": project_id,
                # Both transports accept the same immutable HTTP intent. API
                # replacement must not change its idempotency identity.
                "name": "http.invoke/v1"
                if name in {"chat.accept/v1", "chat.resume/v1"}
                else name,
                "payload": payload,
                "expected_revision": expected_revision,
            }
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self._write() as session:
            existing = await session.scalar(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.actor_id == actor_id,
                    RuntimeCommandRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.payload_hash != digest or existing.lane != lane:
                    raise CoordinationConflict(
                        "idempotency key already accepted with a different payload"
                    )
                return _receipt(existing)
            await self._insert_absent(
                session,
                DeploymentHostRow,
                {
                    "key": "host",
                    "revision": 0,
                    "api_runtime_id": None,
                    "target_version": None,
                    "rollback_version": None,
                    "shutdown_requested": False,
                    "detail": "No runtime has been promoted.",
                },
                ["key"],
            )
            host = await session.get(
                DeploymentHostRow,
                "host",
                with_for_update={"read": True},
                populate_existing=True,
            )
            if host is not None and host.shutdown_requested:
                raise CoordinationConflict(
                    "the host is draining for coordinated shutdown"
                )
            await self._ensure_project(session, project_id)
            owner = await session.get(ProjectOwnershipRow, project_id)
            if owner is not None and owner.retirement_requested:
                raise ProjectRetiring(
                    "the project worker is closing; retry the request after retirement"
                )
            sequence = await session.scalar(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.project_id == project_id,
                    ProjectOwnershipRow.retirement_requested.is_(False),
                )
                .values(command_sequence=ProjectOwnershipRow.command_sequence + 1)
                .returning(ProjectOwnershipRow.command_sequence)
            )
            if sequence is None:
                raise ProjectRetiring(
                    "the project worker is closing; retry the request after retirement"
                )
            await self._insert_absent(
                session,
                RuntimeCommandRow,
                {
                    "id": str(uuid4()),
                    "project_id": project_id,
                    "actor_id": actor_id,
                    "idempotency_key": idempotency_key,
                    "sequence": sequence,
                    "name": name,
                    "lane": lane,
                    "payload": json.loads(_json(payload)),
                    "payload_hash": digest,
                    "expected_revision": expected_revision,
                    "accepted_at_ms": await self._clock_ms(session),
                    "state": "queued",
                    "chat_start": chat_start,
                    "runtime_id": None,
                    "fencing_token": None,
                    "result": None,
                },
                ["actor_id", "idempotency_key"],
            )
            row = await session.scalar(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.actor_id == actor_id,
                    RuntimeCommandRow.idempotency_key == idempotency_key,
                )
            )
            assert row is not None
            if row.payload_hash != digest or row.lane != lane:
                raise CoordinationConflict(
                    "idempotency key already accepted with a different payload"
                )
            return _receipt(row)

    async def mark_queued_chat_start(
        self,
        *,
        project_id: str,
        command_id: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Promote one authenticated legacy HTTP command after vault inspection."""
        async with self._write() as session:
            await self._ensure_project(session, project_id)
            held = await session.execute(
                update(ProjectOwnershipRow)
                .where(
                    ProjectOwnershipRow.project_id == project_id,
                    ProjectOwnershipRow.retirement_requested.is_(False),
                )
                .values(command_sequence=ProjectOwnershipRow.command_sequence)
            )
            if held.rowcount != 1:
                return False
            row = await session.get(
                RuntimeCommandRow,
                command_id,
                with_for_update=True,
                populate_existing=True,
            )
            if (
                row is None
                or row.project_id != project_id
                or row.actor_id != actor_id
                or row.name != "http.invoke/v1"
                or row.state != "queued"
                or row.chat_start
                or row.payload != payload
            ):
                return False
            row.chat_start = True
            return True

    async def retype_chat_start(
        self, lease: ProjectLease, command_id: str, *, name: str
    ) -> bool:
        """Carry a claimed HTTP chat start on the typed transport its owner serves.

        An API with no live project owner cannot tell which release will run a
        chat start, so it sends the generic ``http.invoke/v1`` command, whose
        streamed response would hold the project's work lane for the whole
        turn. The owner that claimed it knows its own features. Under its
        fence it may rename that running command to ``chat.accept/v1`` or
        ``chat.resume/v1``; the request identity ignores the transport name.
        """
        if name not in {"chat.accept/v1", "chat.resume/v1"}:
            raise ValueError("only a typed chat transport can replace a chat start")
        async with self.fenced(lease) as session:
            row = await session.get(
                RuntimeCommandRow,
                command_id,
                with_for_update=True,
                populate_existing=True,
            )
            if (
                row is None
                or row.project_id != lease.project_id
                or row.state != "running"
                or (row.runtime_id, row.fencing_token)
                != (lease.runtime_id, lease.fencing_token)
                or row.name != "http.invoke/v1"
                or not row.chat_start
            ):
                return False
            row.name = name
            return True

    async def claim_next(
        self, lease: ProjectLease, *, lane: Literal["work", "control"] | None = None
    ) -> ClaimedCommand | None:
        async with self.fenced(lease) as session:
            if lane not in {None, "work", "control"}:
                raise ValueError("unknown command lane")
            handoff = await session.get(ProjectHandoffRow, lease.project_id)
            host = await session.get(DeploymentHostRow, "host")
            ownership = await session.get(ProjectOwnershipRow, lease.project_id)
            held, retiring = dispatch_holds(handoff, host, ownership)
            # Retirement is a project-wide quiescence boundary. Unlike host
            # shutdown and handoff, it cannot leave the control lane running:
            # even an untouched queued control command could start after the
            # worker's quiescence checkpoint and before its owner is released.
            if retiring:
                return None
            # Control delivery has its own ordered lane. Stop, approvals and
            # input decisions cannot sit behind uncertain work or a handoff.
            selected_lane = "control" if held and lane is None else lane
            if held and selected_lane == "work":
                return None
            row = await session.scalar(
                select(RuntimeCommandRow)
                .where(
                    RuntimeCommandRow.project_id == lease.project_id,
                    RuntimeCommandRow.state.in_(_DISPATCH_STATES),
                    *(
                        [RuntimeCommandRow.lane == selected_lane]
                        if selected_lane is not None
                        else []
                    ),
                )
                .order_by(RuntimeCommandRow.sequence)
                .limit(1)
            )
            action = head_action(row, lease)
            if action is None:
                return None
            assert row is not None
            if action == "uncertain":
                row.state = "uncertain"
                return None
            row.state, row.runtime_id, row.fencing_token = (
                "running",
                lease.runtime_id,
                lease.fencing_token,
            )
            return ClaimedCommand(
                _receipt(row), row.name, row.payload, row.expected_revision
            )

    async def dispatch_snapshot(self, lease: ProjectLease) -> DispatchSnapshot:
        """Read, without locks or writes, which lanes a fenced claim could act on.

        An idle worker probes with this instead of opening a fenced write
        transaction per lane every poll. It uses the same hold and head rules
        as ``claim_next``; any lane it reports is still claimed under the fence.
        """
        async with self.database.session() as session:
            ownership = (
                await session.execute(
                    select(
                        ProjectOwnershipRow.runtime_id,
                        ProjectOwnershipRow.fencing_token,
                        ProjectOwnershipRow.lease_expires_at_ms,
                        ProjectOwnershipRow.retirement_requested,
                    ).where(ProjectOwnershipRow.project_id == lease.project_id)
                )
            ).first()
            now = await self._clock_ms(session)
            handoff = (
                await session.execute(
                    select(
                        ProjectHandoffRow.admission_held,
                        ProjectHandoffRow.phase,
                        ProjectHandoffRow.source_runtime_id,
                        ProjectHandoffRow.source_fencing_token,
                    ).where(ProjectHandoffRow.project_id == lease.project_id)
                )
            ).first()
            host = (
                await session.execute(
                    select(DeploymentHostRow.shutdown_requested).where(
                        DeploymentHostRow.key == "host"
                    )
                )
            ).first()
            owned = (
                ownership is not None
                and (ownership.runtime_id, ownership.fencing_token)
                == (lease.runtime_id, lease.fencing_token)
                and ownership.lease_expires_at_ms > now
            )
            held, retiring = dispatch_holds(handoff, host, ownership)
            lanes: set[str] = set()
            for lane in () if retiring else COMMAND_LANES:
                if held and lane == "work":
                    continue
                head = (
                    await session.execute(
                        select(
                            RuntimeCommandRow.state,
                            RuntimeCommandRow.name,
                            RuntimeCommandRow.runtime_id,
                            RuntimeCommandRow.fencing_token,
                        )
                        .where(
                            RuntimeCommandRow.project_id == lease.project_id,
                            RuntimeCommandRow.state.in_(_DISPATCH_STATES),
                            RuntimeCommandRow.lane == lane,
                        )
                        .order_by(RuntimeCommandRow.sequence)
                        .limit(1)
                    )
                ).first()
                if head_action(head, lease) is not None:
                    lanes.add(lane)
            return DispatchSnapshot(
                owned=owned,
                lanes=frozenset(lanes),
                handoff_active=source_handoff_active(handoff, lease),
            )

    async def finish_command(
        self,
        lease: ProjectLease,
        command_id: str,
        *,
        result: dict[str, Any],
        state: Literal["succeeded", "failed", "uncertain"] = "succeeded",
        apply: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        """Commit an outcome and its SQL effects in the same fenced transaction.

        ``apply`` may persist entities/turns using this database's active session.
        It must not perform external I/O. A failed callback or expired lease
        rolls back both the command outcome and those authoritative writes.
        """
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid command outcome")
        async with self.fenced(lease) as session:
            changed = await session.execute(
                update(RuntimeCommandRow)
                .where(
                    RuntimeCommandRow.id == command_id,
                    RuntimeCommandRow.project_id == lease.project_id,
                    RuntimeCommandRow.state == "running",
                    RuntimeCommandRow.runtime_id == lease.runtime_id,
                    RuntimeCommandRow.fencing_token == lease.fencing_token,
                )
                .values(state=state, result=json.loads(_json(result)))
            )
            if changed.rowcount != 1:
                raise CoordinationConflict("command is not running under this owner")
            if apply is not None:
                await apply(session)
            lane = await session.scalar(
                select(RuntimeCommandRow.lane).where(RuntimeCommandRow.id == command_id)
            )
            if lane == "control":
                handoff = await session.get(ProjectHandoffRow, lease.project_id)
                if handoff is not None and handoff.phase == "transferable":
                    handoff.phase, handoff.admission_held = "finishing", True
                    handoff.blockers = [
                        "A control changed project state; verify a new safe boundary"
                    ]
                    handoff.revision += 1

    async def append_stream_batch(
        self,
        lease: ProjectLease,
        turn_id: str,
        *,
        batch_key: str,
        events: Sequence[tuple[str, dict[str, Any]]],
        apply: Callable[[AsyncSession, list[StreamFrame]], Awaitable[None]]
        | None = None,
    ) -> list[StreamFrame]:
        """Persist one batch before publication, using existing event retention."""
        async with self.fenced(lease) as session:
            return await self._append_stream_batch(
                session, lease, turn_id, batch_key=batch_key, events=events, apply=apply
            )

    async def _append_stream_batch(
        self,
        session: AsyncSession,
        lease: ProjectLease,
        turn_id: str,
        *,
        batch_key: str,
        events: Sequence[tuple[str, dict[str, Any]]],
        apply: Callable[[AsyncSession, list[StreamFrame]], Awaitable[None]]
        | None = None,
    ) -> list[StreamFrame]:
        """Caller holds live fencing or the verified dead-owner recovery lock."""
        _identifier(batch_key, "batch_key")
        if not 1 <= len(events) <= MAX_STREAM_BATCH:
            raise ValueError(
                f"stream batch must contain 1 to {MAX_STREAM_BATCH} events"
            )
        for event_type, payload in events:
            _identifier(event_type, "event_type", 180)
            if not isinstance(payload, dict):
                raise ValueError("event payload must be an object")
        # Copy the caller's mutable values before waiting for locks.
        events = json.loads(_json(events))
        keys = [
            f"chat-stream/{hashlib.sha256(batch_key.encode()).hexdigest()}/{i}"
            for i in range(len(events))
        ]
        owner = await session.scalar(
            select(EntityRow.id).where(
                EntityRow.id == turn_id,
                EntityRow.engagement_id == lease.project_id,
                EntityRow.kind.in_(("chat_turns", "harness_turns")),
            )
        )
        if owner is None:
            raise CoordinationConflict("stream turn does not belong to this project")
        if self.database.engine.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:run_id))"),
                {"run_id": turn_id},
            )
        existing = list(
            await session.scalars(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == turn_id,
                    RunEventRow.idempotency_key.in_(keys),
                )
                .order_by(RunEventRow.sequence)
            )
        )
        stored = [
            {"data": payload, "batch_size": len(events), "epoch": STREAM_EPOCH}
            for _, payload in events
        ]
        if existing:
            if len(existing) != len(events) or any(
                row.event_type != _STREAM_PREFIX + kind or row.payload != data
                for row, (kind, _), data in zip(existing, events, stored)
            ):
                raise CoordinationConflict(
                    "stream batch key already contains different events"
                )
            return [self._frame(row) for row in existing]
        sequence = int(
            await session.scalar(
                select(func.max(RunEventRow.sequence)).where(
                    RunEventRow.run_id == turn_id
                )
            )
            or 0
        )
        rows = [
            RunEventRow(
                id=str(uuid4()),
                run_id=turn_id,
                sequence=sequence + index + 1,
                event_type=_STREAM_PREFIX + kind,
                payload=data,
                idempotency_key=key,
                actor_id=lease.runtime_id,
                occurred_at=utc_now(),
            )
            for index, ((kind, _), data, key) in enumerate(zip(events, stored, keys))
        ]
        await session.execute(
            insert(RunEventRow),
            [
                {
                    column.name: getattr(row, column.name)
                    for column in RunEventRow.__table__.columns
                }
                for row in rows
            ],
        )
        frames = [self._frame(row) for row in rows]
        if apply is not None:
            await apply(session, frames)
        return frames

    @staticmethod
    def _frame(row: RunEventRow) -> StreamFrame:
        return StreamFrame(
            row.sequence, row.event_type[len(_STREAM_PREFIX) :], row.payload["data"]
        )

    async def stream_head(self, turn_id: str) -> StreamFrame | None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == turn_id,
                    RunEventRow.event_type.startswith(_STREAM_PREFIX, autoescape=True),
                )
                .order_by(RunEventRow.sequence.desc())
                .limit(1)
            )
            return self._frame(row) if row is not None else None

    async def replay_stream(
        self, turn_id: str, *, after_sequence: int = 0, limit: int = MAX_STREAM_BATCH
    ) -> list[StreamFrame]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be nonnegative")
        if type(limit) is not int or not 1 <= limit <= MAX_STREAM_BATCH:
            raise ValueError(f"limit must be between 1 and {MAX_STREAM_BATCH}")
        async with self.database.session() as session:
            rows = await session.scalars(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == turn_id,
                    RunEventRow.sequence > after_sequence,
                    RunEventRow.event_type.startswith(_STREAM_PREFIX, autoescape=True),
                )
                .order_by(RunEventRow.sequence)
                .limit(limit)
            )
            return [self._frame(row) for row in rows]
