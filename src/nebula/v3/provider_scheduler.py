"""Durable, bounded admission control for provider chat turns."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, case, exists, func, or_, select, update

from .database import EntityRow, ProviderTurnQueueRow
from .domain import ChatTurn, ChatTurnStatus, utc_now
from .storage import ConflictError, NebulaStore

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
    provider_limit: int = 6
    background_limit: int = 4
    per_turn_tool_limit: int = 4
    global_tool_limit: int = 8

    @classmethod
    def from_environment(cls) -> "ProviderSchedulerConfig":
        config = cls(
            provider_limit=_bounded_env("NEBULA_PROVIDER_CONCURRENCY", 6),
            background_limit=_bounded_env("NEBULA_BACKGROUND_CONCURRENCY", 4),
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

    async def release(self, turn_id: str) -> None:
        # The turn frees its slot as soon as provider work ends, before it
        # settles; the task that admitted it releases again on exit. A second
        # release must not complete a later admission of the same turn.
        if self.released:
            return
        self.released = True
        self.scheduler._complete(turn_id)
        if self.lane == "background":
            self.scheduler._background.release()
        self.scheduler._provider.release()


class ProviderScheduler:
    """Six provider slots with four shared by background work."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        worker_id: str,
        config: ProviderSchedulerConfig | None = None,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.config = config or ProviderSchedulerConfig.from_environment()
        self._provider = asyncio.Semaphore(self.config.provider_limit)
        self._background = asyncio.Semaphore(self.config.background_limit)
        self._active: dict[str, str] = {}

    def enqueue(self, turn: ChatTurn) -> None:
        now = turn.queued_at or utc_now()
        with self.store.database.session() as session:
            row = session.get(ProviderTurnQueueRow, turn.id)
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

    async def admit(self, turn_id: str) -> ProviderAdmission:
        turn = self.store.get(ChatTurn, turn_id)
        lane = turn.capacity_lane
        if lane == "background":
            await self._background.acquire()
        try:
            await self._provider.acquire()
        except BaseException:
            if lane == "background":
                self._background.release()
            raise
        try:
            latest = self.store.get(ChatTurn, turn_id)
            if latest.status == ChatTurnStatus.CANCELLED:
                raise asyncio.CancelledError
            admitted_at = utc_now()
            changes: dict[str, Any] = {"admitted_at": admitted_at}
            # Only a new turn starts routing here. A resumed turn keeps the
            # state it parked in (waiting for an approval or a callback, or
            # finalizing a retried answer): that state selects how it resumes.
            if latest.status == ChatTurnStatus.QUEUED:
                changes["status"] = ChatTurnStatus.ROUTING
            with self.store.transaction() as transaction:
                row = transaction.session.get(ProviderTurnQueueRow, turn_id)
                if row is None or row.state != "queued":
                    raise ConflictError("provider turn is no longer queued")
                updated = transaction.update(
                    ChatTurn,
                    turn_id,
                    changes,
                    expected_revision=latest.revision,
                )
                row.state = "running"
                row.admitted_at = admitted_at
                row.lease_owner = self.worker_id
                row.lease_expires_at = admitted_at + timedelta(hours=24)
                transaction.session.flush()
            self._active[turn_id] = lane
            del updated
            return ProviderAdmission(self, lane)
        except BaseException:
            self._provider.release()
            if lane == "background":
                self._background.release()
            raise

    def _complete(self, turn_id: str) -> None:
        self._active.pop(turn_id, None)
        # Read in SQL, so a turn that is gone or unreadable still frees the
        # admission (and, in ProviderAdmission.release, its slots).
        with self.store.database.session() as session:
            session.execute(
                update(ProviderTurnQueueRow)
                .where(
                    ProviderTurnQueueRow.turn_id == turn_id,
                    ProviderTurnQueueRow.state == "running",
                )
                .values(
                    state=_released_state(),
                    completed_at=utc_now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )

    def cancel(self, turn_id: str) -> None:
        with self.store.database.session() as session:
            row = session.get(ProviderTurnQueueRow, turn_id)
            if row is not None and row.state == "queued":
                row.state = "cancelled"
                row.completed_at = utc_now()

    def settle(self, turn_id: str | None = None) -> list[str]:
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
        with self.store.database.session() as session:
            settled = list(
                session.scalars(select(ProviderTurnQueueRow.turn_id).where(*ended))
            )
            if settled:
                # The same conditions again, in the statement that writes: a
                # turn resumed since the read keeps its new admission.
                session.execute(
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
        return settled

    def recover(self) -> list[str]:
        """Return queued turns and reset leases abandoned by a stopped Core."""

        with self.store.database.session() as session:
            # ChatService startup owns the corresponding running-turn
            # interruption. The admission only drops its stale lease, and
            # stays parked if that left the turn resumable.
            session.execute(
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
            rows = list(
                session.scalars(
                    select(ProviderTurnQueueRow)
                    .where(ProviderTurnQueueRow.state == "queued")
                    .order_by(
                        ProviderTurnQueueRow.accepted_at,
                        ProviderTurnQueueRow.turn_id,
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
            return queued

    def position(self, turn_id: str) -> int | None:
        with self.store.database.session() as session:
            row = session.get(ProviderTurnQueueRow, turn_id)
            if row is None or row.state != "queued":
                return None
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderTurnQueueRow)
                    .where(
                        ProviderTurnQueueRow.state == "queued",
                        (
                            (ProviderTurnQueueRow.accepted_at < row.accepted_at)
                            | (
                                (ProviderTurnQueueRow.accepted_at == row.accepted_at)
                                & (ProviderTurnQueueRow.turn_id <= row.turn_id)
                            )
                        ),
                    )
                )
                or 0
            )

    def metrics(self) -> dict[str, Any]:
        with self.store.database.session() as session:
            queued_direct = int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderTurnQueueRow)
                    .where(
                        ProviderTurnQueueRow.state == "queued",
                        ProviderTurnQueueRow.lane == "direct",
                    )
                )
                or 0
            )
            queued_background = int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderTurnQueueRow)
                    .where(
                        ProviderTurnQueueRow.state == "queued",
                        ProviderTurnQueueRow.lane == "background",
                    )
                )
                or 0
            )
            oldest = session.scalar(
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
