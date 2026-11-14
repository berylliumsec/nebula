"""Project-view leases: an open project view is explicit worker demand.

Reads never admit a project worker, so a project with no open chat and no
queued work would never load its Notes, Findings or Evidence. A client that
opens a project view holds a lease instead: a keyed request it renews while
the view stays open. The host's admission loop counts a live lease as demand,
exactly like an open chat, and retires the worker (after its idle grace) once
the lease lapses. Memory-budget and host-pressure gates still decide whether
and when the worker starts.

Expiry is computed from database time inside the writing statement, so clock
skew between the client, the API worker and the host cannot end a lease early
or keep it alive. PUT by a client-chosen lease ID is idempotent: replaying it
renews (or, after expiry, recreates) the same lease, so it needs no
Idempotency-Key. Releasing a lease ends it now instead of deleting it, so the
host's idle grace runs from the moment the view closed; lapsed rows are pruned
once no grace can still read them. Only the API worker's SQL-global path
writes these rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import BigInteger, and_, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import (
    Database,
    EntityRow,
    ProjectAdmissionHoldRow,
    ProjectOwnershipRow,
    ProjectViewLeaseRow,
    RuntimeProcessRow,
)
from .runtime_coordination import RuntimeCoordination

VIEW_LEASE_TTL_SECONDS = 90
VIEW_LEASE_RENEW_SECONDS = 30
MIN_VIEW_LEASE_TTL_SECONDS = 30
MAX_VIEW_LEASE_TTL_SECONDS = 300
# Open project views one actor (the operator or one paired device) may hold.
MAX_VIEW_LEASES_PER_ACTOR = 16
# A lapsed lease is kept as long as the longest idle grace the host allows
# (runtime_auto_admission.MAX_IDLE_WORKER_GRACE_SECONDS), then pruned.
VIEW_LEASE_RETAIN_MS = 3600 * 1000
_LEASE_ID = re.compile(r"[A-Za-z0-9_-]{8,100}")


class ViewLeaseRefused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def database_clock_ms(dialect: str):
    """Database time in epoch milliseconds, evaluated inside the statement."""
    if dialect == "postgresql":
        value = func.extract("epoch", func.clock_timestamp()) * 1000
    else:
        value = (func.julianday("now") - 2440587.5) * 86_400_000
    return cast(value, BigInteger)


def _iso(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ProjectViewLeases:
    def __init__(self, database: Database, coordination: RuntimeCoordination):
        self.database = database
        self.coordination = coordination

    async def hold(
        self,
        actor_id: str,
        project_id: str,
        lease_id: str,
        ttl_seconds: Any = VIEW_LEASE_TTL_SECONDS,
    ) -> dict[str, Any]:
        if not _LEASE_ID.fullmatch(lease_id or ""):
            raise ViewLeaseRefused(
                422, "A view lease ID is 8-100 letters, digits, '-' or '_'"
            )
        if (
            type(ttl_seconds) is not int
            or not MIN_VIEW_LEASE_TTL_SECONDS
            <= ttl_seconds
            <= MAX_VIEW_LEASE_TTL_SECONDS
        ):
            raise ViewLeaseRefused(
                422,
                f"ttl_seconds must be a whole number from {MIN_VIEW_LEASE_TTL_SECONDS} to {MAX_VIEW_LEASE_TTL_SECONDS}",
            )
        dialect = self.database.engine.dialect.name
        clock = database_clock_ms(dialect)
        async with self.coordination._write() as session:
            project = await session.scalar(
                select(EntityRow).where(
                    EntityRow.id == project_id, EntityRow.kind == "engagements"
                )
            )
            # The actor can read every project it can see; a missing or
            # deleted project is not a lease target.
            if project is None:
                raise ViewLeaseRefused(404, "The project does not exist")
            if (project.payload or {}).get("status") == "archived":
                raise ViewLeaseRefused(
                    409,
                    "An archived project keeps no worker; restore it to work in it",
                )
            others = await session.scalar(
                select(func.count())
                .select_from(ProjectViewLeaseRow)
                .where(
                    ProjectViewLeaseRow.actor_id == actor_id,
                    ProjectViewLeaseRow.lease_id != lease_id,
                    ProjectViewLeaseRow.expires_at_ms > clock,
                )
            )
            if (others or 0) >= MAX_VIEW_LEASES_PER_ACTOR:
                raise ViewLeaseRefused(
                    429,
                    "Too many project views are open from this device; close one and retry",
                )
            # Prune leases no idle grace can read any more, and this actor's
            # lapsed leases for the project: the new lease supersedes them.
            await session.execute(
                delete(ProjectViewLeaseRow).where(
                    or_(
                        ProjectViewLeaseRow.expires_at_ms
                        < clock - VIEW_LEASE_RETAIN_MS,
                        and_(
                            ProjectViewLeaseRow.actor_id == actor_id,
                            ProjectViewLeaseRow.project_id == project_id,
                            ProjectViewLeaseRow.lease_id != lease_id,
                            ProjectViewLeaseRow.expires_at_ms <= clock,
                        ),
                    )
                )
            )
            await self.coordination._ensure_project(session, project_id)
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(ProjectViewLeaseRow).values(
                actor_id=actor_id,
                lease_id=lease_id,
                project_id=project_id,
                expires_at_ms=clock + ttl_seconds * 1000,
                renewed_at_ms=clock,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    ProjectViewLeaseRow.actor_id,
                    ProjectViewLeaseRow.lease_id,
                ],
                set_={
                    "expires_at_ms": clock + ttl_seconds * 1000,
                    "renewed_at_ms": clock,
                },
                # A lease names one project for its whole life.
                where=ProjectViewLeaseRow.project_id == project_id,
            ).returning(ProjectViewLeaseRow.expires_at_ms)
            expires = await session.scalar(statement)
            if expires is None:
                raise ViewLeaseRefused(
                    409, "This view lease belongs to another project"
                )
            runtime = await self._runtime(session, project_id)
        return {
            "lease_id": lease_id,
            "project_id": project_id,
            "expires_at": _iso(int(expires)),
            "ttl_seconds": ttl_seconds,
            "renew_after_seconds": VIEW_LEASE_RENEW_SECONDS,
            "runtime": runtime,
        }

    async def release(self, actor_id: str, project_id: str, lease_id: str) -> None:
        """End a lease now. The row stays lapsed so the idle grace runs from here."""
        clock = database_clock_ms(self.database.engine.dialect.name)
        async with self.coordination._write() as session:
            await session.execute(
                update(ProjectViewLeaseRow)
                .where(
                    ProjectViewLeaseRow.actor_id == actor_id,
                    ProjectViewLeaseRow.lease_id == lease_id,
                    ProjectViewLeaseRow.project_id == project_id,
                    ProjectViewLeaseRow.expires_at_ms > clock,
                )
                .values(expires_at_ms=clock)
            )

    async def _runtime(self, session, project_id: str) -> dict[str, Any]:
        owner = await session.get(ProjectOwnershipRow, project_id)
        now = await self.coordination._clock_ms(session)
        if owner is not None and owner.runtime_id and owner.lease_expires_at_ms > now:
            process = await session.get(RuntimeProcessRow, owner.runtime_id)
            if process is not None and process.state == "ready":
                return {"state": "ready"}
        hold = await session.get(ProjectAdmissionHoldRow, project_id)
        if hold is not None:
            return {"state": "held", "reason": hold.reason}
        return {"state": "starting"}


def live_view_lease(project_id, now_ms, *, within_ms: int = 0):
    """SQL EXISTS for a project's view lease that is live (or lapsed within_ms)."""
    return (
        select(ProjectViewLeaseRow.lease_id)
        .where(
            and_(
                ProjectViewLeaseRow.project_id == project_id,
                ProjectViewLeaseRow.expires_at_ms > now_ms - within_ms,
            )
        )
        .exists()
    )
