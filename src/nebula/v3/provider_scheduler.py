"""Durable, bounded admission control for provider chat turns."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import ColumnElement, case, exists, func, or_, select, update

from .database import EntityRow, ProviderTurnQueueRow
from .domain import ChatTurn, ChatTurnStatus, utc_now
from .storage import ConflictError, NebulaStore
from .host_load import HostLoadGate
from .host_admission import save_host_wait
from .async_utils import finish_cleanup
from .diagnostics import create_diagnostic_task
from .execution_scope import current_execution_lease
from .runtime_capacity import CapacityReservation, RuntimeCapacity
from .runtime_coordination import LeaseLost

# A turn waiting in one of these states resumes through admission again, so
# its admission stays parked until it does.
_PARKED_TURN_STATUSES = (
    ChatTurnStatus.WAITING_APPROVAL.value,
    ChatTurnStatus.WAITING_CALLBACK.value,
    ChatTurnStatus.INTERRUPTED.value,
)
# A turn in one of these states has ended; nothing admits it again.
_ENDED_TURN_STATUSES = (
    ChatTurnStatus.COMPLETE.value,
    ChatTurnStatus.FAILED.value,
    ChatTurnStatus.CANCELLED.value,
)
# Admission states a later turn event still has to close.
_OPEN_ADMISSION_STATES = ("queued", "parked")


def _turn_exists() -> ColumnElement[bool]:
    return exists().where(
        EntityRow.id == ProviderTurnQueueRow.turn_id,
        EntityRow.kind == ChatTurn.entity_kind,
    )


def _turn_status() -> ColumnElement[str | None]:
    """The admission's turn status, read in SQL: no record is validated."""

    return (
        select(EntityRow.payload["status"].as_string())
        .where(
            EntityRow.id == ProviderTurnQueueRow.turn_id,
            EntityRow.kind == ChatTurn.entity_kind,
        )
        .scalar_subquery()
    )


def _released_state() -> ColumnElement[str]:
    """The state an admission takes once provider work for its turn stops.

    Parked while the turn can still resume through admission; otherwise
    over, as cancelled when the turn was stopped or no longer exists.
    """

    status = _turn_status()
    return case(
        (status.in_(_PARKED_TURN_STATUSES), "parked"),
        (
            or_(~_turn_exists(), status == ChatTurnStatus.CANCELLED.value),
            "cancelled",
        ),
        else_="complete",
    )


def _bounded_env(name: str, default: int, *, maximum: int = 32) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class ProviderSchedulerConfig:
    provider_limit: int = 60
    background_limit: int = 58
    per_turn_tool_limit: int = 4
    global_tool_limit: int = 8

    @classmethod
    def from_environment(cls) -> "ProviderSchedulerConfig":
        provider_limit = _bounded_env("NEBULA_PROVIDER_CONCURRENCY", 60, maximum=64)
        config = cls(
            provider_limit=provider_limit,
            background_limit=_bounded_env(
                "NEBULA_BACKGROUND_CONCURRENCY",
                min(58, max(1, provider_limit - 2)),
                maximum=64,
            ),
            per_turn_tool_limit=_bounded_env("NEBULA_TURN_TOOL_CONCURRENCY", 4),
            global_tool_limit=_bounded_env("NEBULA_GLOBAL_TOOL_CONCURRENCY", 8),
        )
        if config.background_limit > config.provider_limit:
            raise ValueError(
                "NEBULA_BACKGROUND_CONCURRENCY cannot exceed provider concurrency"
            )
        if config.per_turn_tool_limit > config.global_tool_limit:
            raise ValueError(
                "NEBULA_TURN_TOOL_CONCURRENCY cannot exceed global tool concurrency"
            )
        return config


class ProviderAdmission:
    def __init__(self, scheduler: "ProviderScheduler", lane: str) -> None:
        self.scheduler = scheduler
        self.lane = lane
        self.released = False
        # Fence cleanup to this admission, including commit whose result was
        # lost to cancellation. An older token cannot close a later lease.
        self.lease_owner = uuid4().hex
        self._release_task: asyncio.Task[None] | None = None
        self.host_slot: AbstractAsyncContextManager[CapacityReservation] | None = None

    async def release(self, turn_id: str) -> None:
        # The turn frees its slot as soon as provider work ends, before it
        # settles; the task that admitted it releases again on exit. A second
        # release must not complete a later admission of the same turn.
        if self._release_task is None:
            self.released = True
            self._release_task = create_diagnostic_task(
                self._release(turn_id),
                feature="chat",
                event_code="chat.provider_admission.release_failed",
                failure_message="A provider admission's durable lease could not be closed.",
                name="nebula-provider-admission-release",
            )

        async def join():
            await self._release_task

        await finish_cleanup(join())

    async def _release(self, turn_id: str) -> None:
        try:
            await self.scheduler._complete(turn_id, self.lease_owner)
        finally:
            try:
                if self.host_slot is not None:
                    await self.host_slot.__aexit__(None, None, None)
            finally:
                # A database failure must not permanently consume process capacity.
                if self.lane == "background":
                    self.scheduler._background.release()
                self.scheduler._provider.release()


class ProviderScheduler:
    """Provider capacity with two slots reserved for direct work by default."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        worker_id: str,
        config: ProviderSchedulerConfig | None = None,
        host_load: HostLoadGate | None = None,
        runtime_capacity: RuntimeCapacity | None = None,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.config = config or ProviderSchedulerConfig.from_environment()
        self._provider = asyncio.Semaphore(self.config.provider_limit)
        self._background = asyncio.Semaphore(self.config.background_limit)
        self._active: dict[str, str] = {}
        self._active_owners: dict[str, str] = {}
        # Positions are display state. Share one short-lived ordered snapshot
        # across waiters instead of querying/ranking the queue for every poll.
        # Admission, claims and leases always read durable state independently.
        self._positions: dict[str, int] = {}
        self._positions_until = 0.0
        self._positions_generation = 0
        self._positions_lock = asyncio.Lock()
        self.host_load = host_load
        self.runtime_capacity = runtime_capacity
        self._host_reasons: dict[str, str] = {}

    def queue_detail(self, turn_id: str) -> str:
        return self._host_reasons.get(turn_id, "Waiting for provider capacity")

    def _invalidate_positions(self) -> None:
        self._positions_until = 0.0
        self._positions_generation += 1

    async def enqueue(self, turn: ChatTurn) -> None:
        now = turn.queued_at or utc_now()
        async with self.store.database.session() as session:
            row = await session.get(ProviderTurnQueueRow, turn.id)
            if row is None:
                session.add(
                    ProviderTurnQueueRow(
                        turn_id=turn.id,
                        lane=turn.capacity_lane,
                        state="queued",
                        accepted_at=now,
                    )
                )
            elif row.state != "running":
                row.state = "queued"
                row.accepted_at = now
                row.admitted_at = None
                row.completed_at = None
                row.lease_owner = None
                row.lease_expires_at = None
        self._invalidate_positions()

    async def _wait_for_capacity(
        self, turn: ChatTurn, deferred
    ) -> ProviderAdmission | None:
        async def acquire() -> ProviderAdmission:
            performance = self.store.database.performance
            background = False
            provider = False
            admission = ProviderAdmission(self, turn.capacity_lane)
            try:
                if turn.capacity_lane == "background":
                    if self._background.locked():
                        await deferred("Waiting for provider background capacity")
                    with performance.span("provider.capacity.background"):
                        await self._background.acquire()
                    background = True
                if self._provider.locked():
                    await deferred("Waiting for provider capacity")
                with performance.span("provider.capacity.shared"):
                    await self._provider.acquire()
                provider = True
                if self.runtime_capacity is not None:
                    lease = current_execution_lease()
                    if lease is None or lease.project_id != turn.engagement_id:
                        raise LeaseLost(
                            "Provider capacity requires the exact project execution context"
                        )
                    demands = {"provider": 1}
                    if turn.capacity_lane == "background":
                        demands["background"] = 1
                    admission.host_slot = self.runtime_capacity.slot(
                        lease,
                        "provider:" + admission.lease_owner,
                        demands,
                        on_wait=deferred,
                    )
                    await admission.host_slot.__aenter__()
            except BaseException:
                if background:
                    self._background.release()
                if provider:
                    self._provider.release()
                raise
            return admission

        if self.host_load is None or turn.status != ChatTurnStatus.QUEUED:
            return await acquire()
        # Retain the semaphore waiter across healthy samples for FIFO ordering.
        # When pressure rises, abort it and release any partial acquisition.
        task = create_diagnostic_task(
            acquire(),
            feature="chat",
            event_code="chat.provider_capacity",
            failure_message="Provider capacity acquisition failed.",
            name=f"nebula-provider-capacity-{turn.id}",
        )

        async def discard() -> None:
            if not task.done():
                task.cancel()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            if isinstance(result, ProviderAdmission):
                await result.release(turn.id)

        try:
            while not task.done():
                await asyncio.wait({task}, timeout=self.host_load.monitor.interval)
                if not task.done() and (await self.host_load.snapshot()).busy:
                    await finish_cleanup(discard())
                    return None
            return await task
        except BaseException:
            await finish_cleanup(discard())
            raise

    async def admit(self, turn_id: str) -> ProviderAdmission:
        turn = await self.store.get(ChatTurn, turn_id)
        lane = turn.capacity_lane
        performance = self.store.database.performance
        admission: ProviderAdmission | None = None

        async def deferred(reason: str | None) -> None:
            if reason is None:
                self._host_reasons.pop(turn_id, None)
            else:
                self._host_reasons[turn_id] = reason
            await save_host_wait(self.store, ChatTurn, turn_id, reason)

        try:
            while True:
                if self.host_load is not None and turn.status == ChatTurnStatus.QUEUED:
                    with performance.span("provider.host_load_wait"):
                        await self.host_load.wait(deferred)
                latest = await self.store.get(ChatTurn, turn_id)
                if latest.status == ChatTurnStatus.CANCELLED:
                    raise asyncio.CancelledError
                admission = await self._wait_for_capacity(turn, deferred)
                if admission is None:
                    continue
                # Pressure can change while provider capacity is occupied. Release
                # both slots before returning to a pressure wait.
                if self.host_load is not None and turn.status == ChatTurnStatus.QUEUED:
                    if (await self.host_load.snapshot()).busy:
                        await admission.release(turn_id)
                        admission = None
                        continue
                break
            latest = await self.store.get(ChatTurn, turn_id)
            if latest.status == ChatTurnStatus.CANCELLED:
                raise asyncio.CancelledError
            admitted_at = utc_now()
            changes: dict[str, Any] = {
                "admitted_at": admitted_at,
                "admission_reason": None,
            }
            # Only a new turn starts routing here. A resumed turn keeps the
            # state it parked in (waiting for an approval or a callback, or
            # finalizing a retried answer): that state selects how it resumes.
            if latest.status == ChatTurnStatus.QUEUED:
                changes["status"] = ChatTurnStatus.ROUTING
            async with self.store.transaction() as transaction:
                row = await transaction.session.get(ProviderTurnQueueRow, turn_id)
                if row is None or row.state != "queued":
                    raise ConflictError("provider turn is no longer queued")
                updated = await transaction.update(
                    ChatTurn,
                    turn_id,
                    changes,
                    expected_revision=latest.revision,
                )
                row.state = "running"
                row.admitted_at = admitted_at
                row.lease_owner = admission.lease_owner
                row.lease_expires_at = admitted_at + timedelta(hours=24)
                (await transaction.session.flush())
            self._active[turn_id] = lane
            self._active_owners[turn_id] = admission.lease_owner
            self._invalidate_positions()
            del updated
            self._host_reasons.pop(turn_id, None)
            return admission
        except BaseException:
            self._host_reasons.pop(turn_id, None)
            # This also handles a commit followed by cancelled connection
            # cleanup, when no token has reached the parent yet.
            if admission is not None:
                await admission.release(turn_id)
            raise

    async def _complete(self, turn_id: str, lease_owner: str) -> None:
        if self._active_owners.get(turn_id) == lease_owner:
            self._active.pop(turn_id, None)
            self._active_owners.pop(turn_id, None)
        # Read in SQL, so a turn that is gone or unreadable still frees the
        # admission (and, in ProviderAdmission.release, its slots).
        async with self.store.database.session() as session:
            (
                await session.execute(
                    update(ProviderTurnQueueRow)
                    .where(
                        ProviderTurnQueueRow.turn_id == turn_id,
                        ProviderTurnQueueRow.state == "running",
                        ProviderTurnQueueRow.lease_owner == lease_owner,
                    )
                    .values(
                        state=_released_state(),
                        completed_at=utc_now(),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
        self._invalidate_positions()

    async def cancel(self, turn_id: str) -> None:
        async with self.store.database.session() as session:
            row = await session.get(ProviderTurnQueueRow, turn_id)
            if row is not None and row.state == "queued":
                row.state = "cancelled"
                row.completed_at = utc_now()
        self._invalidate_positions()

    def admitted(self, turn_id: str) -> bool:
        """Whether this process holds a provider slot for ``turn_id``."""

        return turn_id in self._active

    async def withdraw(self, turn_id: str) -> None:
        """Release a queued admission its runtime gave up on, as its turn stands.

        Parked when the turn can still resume, otherwise over; a turn still
        queued keeps its row, so a restart restores it.
        """

        async with self.store.database.session() as session:
            (
                await session.execute(
                    update(ProviderTurnQueueRow)
                    .where(
                        ProviderTurnQueueRow.turn_id == turn_id,
                        ProviderTurnQueueRow.state == "queued",
                        or_(
                            ~_turn_exists(),
                            _turn_status() != ChatTurnStatus.QUEUED.value,
                        ),
                    )
                    .values(
                        state=_released_state(),
                        completed_at=utc_now(),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
        self._invalidate_positions()

    async def settle(self, turn_id: str | None = None) -> list[str]:
        """Close queued or parked admissions whose turn ended or no longer exists.

        Admission release settles a running admission; a turn that ends while
        queued or parked (stopped, denied, or failed while it waited) ends
        without the scheduler. Pass ``turn_id`` to close one turn's admission;
        with none this closes every such admission, reading only open rows in
        SQL. Admissions of turns that can still resume stay parked, and an
        unreadable turn keeps its admission for a repaired record. Returns the
        turns whose admission it closed; a second pass closes none.
        """

        ended = [
            ProviderTurnQueueRow.state.in_(_OPEN_ADMISSION_STATES),
            or_(~_turn_exists(), _turn_status().in_(_ENDED_TURN_STATUSES)),
        ]
        if turn_id is not None:
            ended.append(ProviderTurnQueueRow.turn_id == turn_id)
        async with self.store.database.session() as session:
            settled = list(
                (
                    await session.scalars(
                        select(ProviderTurnQueueRow.turn_id).where(*ended)
                    )
                )
            )
            if settled:
                # The same conditions again, in the statement that writes: a
                # turn resumed since the read keeps its new admission.
                (
                    await session.execute(
                        update(ProviderTurnQueueRow)
                        .where(ProviderTurnQueueRow.turn_id.in_(settled), *ended)
                        .values(
                            state=_released_state(),
                            completed_at=func.coalesce(
                                ProviderTurnQueueRow.completed_at, utc_now()
                            ),
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                        .execution_options(synchronize_session=False)
                    )
                )
        self._invalidate_positions()
        return settled

    async def recover(self) -> list[str]:
        """Return queued turns and reset leases abandoned by a stopped Core."""

        async with self.store.database.session() as session:
            # ChatService startup owns the corresponding running-turn
            # interruption. The admission only drops its stale lease, and
            # stays parked if that left the turn resumable.
            (
                await session.execute(
                    update(ProviderTurnQueueRow)
                    .where(ProviderTurnQueueRow.state == "running")
                    .values(
                        state=_released_state(),
                        completed_at=utc_now(),
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            rows = list(
                (
                    await session.scalars(
                        select(ProviderTurnQueueRow)
                        .where(ProviderTurnQueueRow.state == "queued")
                        .order_by(
                            ProviderTurnQueueRow.accepted_at,
                            ProviderTurnQueueRow.turn_id,
                        )
                    )
                )
            )
            queued: list[str] = []
            for row in rows:
                expiry = row.lease_expires_at
                if expiry is not None and expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=utc_now().tzinfo)
                if expiry is not None and expiry <= utc_now():
                    row.lease_owner = None
                    row.lease_expires_at = None
                queued.append(row.turn_id)
        self._invalidate_positions()
        return queued

    async def _load_positions(self) -> dict[str, int]:
        async with self.store.database.session() as session:
            ids = await session.scalars(
                select(ProviderTurnQueueRow.turn_id)
                .where(ProviderTurnQueueRow.state == "queued")
                .order_by(
                    ProviderTurnQueueRow.accepted_at, ProviderTurnQueueRow.turn_id
                )
            )
            return {key: rank for rank, key in enumerate(ids, 1)}

    async def position(self, turn_id: str) -> int | None:
        # An enclosing transaction may see its own uncommitted queue writes;
        # those observations must never be published to other tasks.
        if self.store.database.task_session() is not None:
            return (await self._load_positions()).get(turn_id)
        loop = asyncio.get_running_loop()
        if loop.time() >= self._positions_until:
            async with self._positions_lock:
                if loop.time() >= self._positions_until:
                    generation = self._positions_generation
                    self._positions = await self._load_positions()
                    # Local mutations invalidate immediately; other processes'
                    # changes become visible within half the one-second poll.
                    self._positions_until = (
                        loop.time() + 0.5
                        if generation == self._positions_generation
                        else 0.0
                    )
        return self._positions.get(turn_id)

    async def metrics(self) -> dict[str, Any]:
        async with self.store.database.session() as session:
            queued_direct = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(ProviderTurnQueueRow)
                        .where(
                            ProviderTurnQueueRow.state == "queued",
                            ProviderTurnQueueRow.lane == "direct",
                        )
                    )
                )
                or 0
            )
            queued_background = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(ProviderTurnQueueRow)
                        .where(
                            ProviderTurnQueueRow.state == "queued",
                            ProviderTurnQueueRow.lane == "background",
                        )
                    )
                )
                or 0
            )
            oldest = await session.scalar(
                select(func.min(ProviderTurnQueueRow.accepted_at)).where(
                    ProviderTurnQueueRow.state == "queued"
                )
            )
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=utc_now().tzinfo)
        return {
            "provider_limit": self.config.provider_limit,
            "background_limit": self.config.background_limit,
            "active": len(self._active),
            "active_background": sum(
                lane == "background" for lane in self._active.values()
            ),
            "queued": queued_direct + queued_background,
            "queued_direct": queued_direct,
            "queued_background": queued_background,
            "oldest_wait_seconds": (
                max(0.0, (utc_now() - oldest).total_seconds())
                if oldest is not None
                else 0.0
            ),
        }
