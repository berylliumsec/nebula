"""Atomic host-wide admission, separate from enforceable compute memory limits.

Only the supervisor sets the policy. Workers share its SQL ledger through a
coordination pool; a project-scoped application pool cannot write these tables.
An expired ownership lease never proves that a vendor call or tool has ended.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select

from .async_utils import finish_cleanup
from .database import RuntimeCapacityPolicyRow, RuntimeCapacityReservationRow
from .runtime_coordination import (
    CoordinationConflict,
    ProjectLease,
    RuntimeCoordination,
)

CAPACITY_DIMENSIONS = frozenset({"provider", "background", "tool", "cpu"})


def _units(values: Mapping[str, int]) -> dict[str, int]:
    if (
        not isinstance(values, Mapping)
        or not values
        or not set(values) <= CAPACITY_DIMENSIONS
        or any(
            type(value) is not int or not 1 <= value <= 4096
            for value in values.values()
        )
    ):
        raise ValueError(
            "Capacity requires explicit supported dimensions of 1 through 4096 units"
        )
    return dict(sorted(values.items()))


@dataclass(frozen=True)
class CapacityReservation:
    id: str
    lease: ProjectLease
    request_key: str
    demands: dict[str, int]
    state: str


def _reservation(row):
    return CapacityReservation(
        row.id,
        ProjectLease(row.project_id, row.runtime_id, row.fencing_token),
        row.request_key,
        dict(row.demands),
        row.state,
    )


class RuntimeCapacity:
    def __init__(self, coordination: RuntimeCoordination):
        self.coordination = coordination

    @asynccontextmanager
    async def _write(self):
        # All dimensions and every runtime generation use one lock order. The
        # policy row is created under this same lock, including concurrent starts.
        async with self.coordination._write() as session:
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy import text

                await session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtext(current_database()), "
                        "hashtext(current_schema() || ':nebula:runtime-capacity:v1'))"
                    )
                )
            yield session

    async def configure(self, limits, *, expected_revision: int | None = None):
        limits = _units(limits)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("Capacity policy revisions must be nonnegative integers")
        async with self._write() as session:
            row = await session.get(
                RuntimeCapacityPolicyRow, "host", with_for_update=True
            )
            if row is None:
                if expected_revision not in {None, 0}:
                    raise CoordinationConflict("Host capacity policy changed")
                row = RuntimeCapacityPolicyRow(key="host", revision=1, limits=limits)
                session.add(row)
            elif expected_revision is None:
                if row.limits != limits:
                    raise CoordinationConflict(
                        "Resident host capacity differs; refresh its revision before changing it"
                    )
            elif (
                type(expected_revision) is not int or row.revision != expected_revision
            ):
                raise CoordinationConflict("Host capacity policy changed")
            else:
                row.limits = limits
                row.revision += 1
            return {"revision": row.revision, "limits": dict(row.limits)}

    async def acquire(
        self, lease: ProjectLease, request_key: str, demands
    ) -> CapacityReservation | None:
        demands = _units(demands)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 200:
            raise ValueError(
                "Capacity reservation requires a bounded immutable request key"
            )
        async with self._write() as session:
            await self.coordination._check(session, lease, lock=True)
            existing = await session.scalar(
                select(RuntimeCapacityReservationRow).where(
                    RuntimeCapacityReservationRow.runtime_id == lease.runtime_id,
                    RuntimeCapacityReservationRow.project_id == lease.project_id,
                    RuntimeCapacityReservationRow.fencing_token == lease.fencing_token,
                    RuntimeCapacityReservationRow.request_key == request_key,
                )
            )
            if existing is not None:
                if existing.demands != demands:
                    raise CoordinationConflict(
                        "Capacity key already contains different demands"
                    )
                if existing.state != "held":
                    raise CoordinationConflict(
                        "Capacity key was released; a new attempt needs a new key"
                    )
                await self.coordination._check(session, lease, lock=False)
                return _reservation(existing)
            policy = await session.get(RuntimeCapacityPolicyRow, "host")
            if policy is None or not set(demands) <= set(policy.limits):
                raise CoordinationConflict(
                    "Supervisor must configure every requested host capacity before execution"
                )
            if any(value > policy.limits[name] for name, value in demands.items()):
                raise CoordinationConflict(
                    "Workload demands exceed the configured host capacity"
                )
            used = {name: 0 for name in policy.limits}
            for held in await session.scalars(
                select(RuntimeCapacityReservationRow).where(
                    RuntimeCapacityReservationRow.state == "held"
                )
            ):
                for name, value in held.demands.items():
                    used[name] = used.get(name, 0) + value
            if any(
                used[name] + value > policy.limits[name]
                for name, value in demands.items()
            ):
                return None
            row = RuntimeCapacityReservationRow(
                id=str(uuid4()),
                runtime_id=lease.runtime_id,
                project_id=lease.project_id,
                fencing_token=lease.fencing_token,
                request_key=request_key,
                demands=demands,
                state="held",
                created_at_ms=await self.coordination._clock_ms(session),
            )
            session.add(row)
            await session.flush()
            await self.coordination._check(session, lease, lock=False)
            return _reservation(row)

    async def release(self, reservation: CapacityReservation):
        # A stale owner may acknowledge its own effect has ended. It cannot
        # release a new incarnation's slot, even when it reused a logical turn ID.
        async with self._write() as session:
            row = await session.get(
                RuntimeCapacityReservationRow, reservation.id, with_for_update=True
            )
            if row is None or (
                row.runtime_id != reservation.lease.runtime_id
                or row.project_id != reservation.lease.project_id
                or row.fencing_token != reservation.lease.fencing_token
                or row.request_key != reservation.request_key
                or row.demands != reservation.demands
            ):
                raise CoordinationConflict(
                    "Capacity release does not identify its immutable reservation"
                )
            if row.state == "held":
                row.state, row.release_reason = "released", "effect-ended"
                row.released_at_ms = await self.coordination._clock_ms(session)

    async def status(self):
        async with self.coordination.database.session() as session:
            policy = await session.get(RuntimeCapacityPolicyRow, "host")
            rows = list(
                await session.scalars(
                    select(RuntimeCapacityReservationRow).where(
                        RuntimeCapacityReservationRow.state == "held"
                    )
                )
            )
            used: dict[str, int] = {}
            for row in rows:
                for name, value in row.demands.items():
                    used[name] = used.get(name, 0) + value
            limits = dict(policy.limits) if policy else {}
            return {
                "revision": policy.revision if policy else 0,
                "limits": limits,
                "used": used,
                "held_reservations": len(rows),
                "draining": any(
                    value > limits.get(name, 0) for name, value in used.items()
                ),
            }

    @asynccontextmanager
    async def slot(
        self,
        lease: ProjectLease,
        request_key: str,
        demands,
        *,
        interval=0.25,
        on_wait=None,
    ):
        if interval < 0.01 or interval > 5:
            raise ValueError("Capacity polling must remain bounded")
        acquired = None
        try:
            # The acquisition task is joined even if its caller disappears after
            # SQL commits, so a lost result cannot leak a known-idle reservation.
            while acquired is None:
                # diagnostic-expected: this acquisition is shielded and drained below, releasing any late reservation.
                task = asyncio.create_task(self.acquire(lease, request_key, demands))
                try:
                    acquired = await asyncio.shield(task)
                except BaseException:

                    async def drain():
                        result = await task
                        if result is not None:
                            await self.release(result)

                    await finish_cleanup(drain())
                    raise
                if acquired is None:
                    if on_wait is not None:
                        await on_wait("Waiting for shared host capacity")
                        on_wait = None
                    await asyncio.sleep(interval)
            yield acquired
        finally:
            if acquired is not None:
                await finish_cleanup(self.release(acquired))
