"""Whether a project's saved work can execute now, read from coordination SQL.

Reads that SQL answers without the project's worker (a goal, a saved request's
status) otherwise present stored state as if work were progressing. This is
the same evidence request routing uses: the ownership lease, the owner's
process and instance, retirement, admission holds, held transfers, and an
automatic recovery that could not confirm its result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import (
    DeploymentOperationRow,
    ProjectAdmissionHoldRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
)

ExecutionWaitReason = Literal["starting", "held", "closing"]


class ExecutionWaiting(BaseModel):
    """Saved work in this project is not executing until a worker serves it."""

    reason: ExecutionWaitReason
    detail: str = Field(max_length=1_000)


_STARTING = (
    "Waiting for the project's execution worker. Saved work continues when it is ready."
)
_RECOVERING = "The project's execution worker stopped responding. Saved work continues after automatic recovery."
_CLOSING = "The project's execution worker is closing. Saved work continues when the project is reopened."


async def _clock_ms(session: AsyncSession) -> int:
    # Leases are written with the database clock; compare on the same clock.
    if session.bind.dialect.name == "postgresql":
        value = func.extract("epoch", func.clock_timestamp()) * 1000
    else:
        value = (func.julianday("now") - 2440587.5) * 86_400_000
    now = await session.scalar(select(cast(value, BigInteger)))
    assert now is not None
    return int(now)


def _held(reason: str | None) -> ExecutionWaiting:
    detail = (reason or "").strip() or "Project recovery is held."
    return ExecutionWaiting(
        reason="held", detail=("Project execution is held: " + detail)[:1_000]
    )


async def _uncertain_recovery(
    session: AsyncSession, owner: ProjectOwnershipRow
) -> str | None:
    """The reason an automatic recovery of this exact owner stopped unconfirmed."""

    rows = await session.scalars(
        select(DeploymentOperationRow).where(
            DeploymentOperationRow.action == "reconcile_project",
            DeploymentOperationRow.state == "uncertain",
        )
    )
    for row in rows:
        expected = (row.parameters or {}).get("expected_owner") or {}
        if (
            expected.get("project_id") == owner.project_id
            and expected.get("runtime_id") == owner.runtime_id
            and expected.get("fencing_token") == owner.fencing_token
        ):
            result = row.result or {}
            return str(result.get("reason") or "automatic recovery is unconfirmed")
    return None


async def project_execution_waiting(
    session: AsyncSession, project_id: str
) -> ExecutionWaiting | None:
    """``None`` when a live, ready worker serves the project; otherwise why not."""

    owner = await session.get(ProjectOwnershipRow, project_id)
    if owner is None or owner.runtime_id is None:
        hold = await session.get(ProjectAdmissionHoldRow, project_id)
        if hold is not None:
            return _held(hold.reason)
        return ExecutionWaiting(reason="starting", detail=_STARTING)
    if owner.retirement_requested:
        return ExecutionWaiting(reason="closing", detail=_CLOSING)
    now = await _clock_ms(session)
    if owner.lease_expires_at_ms <= now:
        uncertain = await _uncertain_recovery(session, owner)
        if uncertain is not None:
            return _held(uncertain)
        return ExecutionWaiting(reason="starting", detail=_RECOVERING)
    process = await session.get(RuntimeProcessRow, owner.runtime_id)
    instance = await session.get(RuntimeInstanceRow, owner.runtime_id)
    if (
        process is None
        or instance is None
        or process.state != "ready"
        or instance.lease_expires_at_ms <= now
    ):
        return ExecutionWaiting(reason="starting", detail=_STARTING)
    handoff = await session.get(ProjectHandoffRow, project_id)
    if (
        handoff is not None
        and handoff.admission_held
        and handoff.phase == "transferred"
    ):
        blockers = [str(item) for item in handoff.blockers or () if item]
        return _held(blockers[0] if blockers else None)
    return None
