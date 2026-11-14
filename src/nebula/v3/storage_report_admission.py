"""Admission, cancellation and retention for storage senders' durable reports.

A storage source token can save reports faster than its project's worker runs
them. These bounds keep that backlog small and short-lived: reports run on
their own lane, a source and a project may each hold only a few unexecuted
reports, a source that stops being valid takes its queued reports with it, and
finished reports are pruned. The release checks run inside the transaction that
saves a report, under the project's ownership row lock, so no claim, handoff or
target change can slip between the check and the save.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select

from .database import (
    DeploymentHostRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
)
from .deployment_protocol import HandoffPhase, ReleaseManifest
from .runtime_coordination import (
    CoordinationConflict,
    ProjectLease,
    RuntimeCoordination,
    STORAGE_REPORT_COMMAND,
    STORAGE_SOURCE_ACTOR_PREFIX,
)

# A release whose execution workers can run STORAGE_REPORT_COMMAND.
STORAGE_REPORT_FEATURE = STORAGE_REPORT_COMMAND
# Unexecuted (queued or running) reports one source may hold in its project,
# and all sources of one project together. A storage device's burst of volume
# transitions fits; a flood is refused with Retry-After instead of queued.
STORAGE_REPORT_SOURCE_LIMIT = 32
STORAGE_REPORT_PROJECT_LIMIT = 128
STORAGE_REPORT_RETRY_AFTER_SECONDS = 15
# Finished reports are kept briefly so a sender's resend reads its saved answer,
# then pruned with their vault payloads. A resend after pruning runs again and
# Core dedupes it by event ID.
STORAGE_REPORT_RETENTION_MS = 10 * 60_000
STORAGE_REPORT_RETAINED = 200
STORAGE_REPORT_MINIMUM_AGE_MS = 60_000
STORAGE_REPORT_PRUNE_BATCH = 100
_UNEXECUTED = ("queued", "running", "uncertain")


class StorageReportBacklog(CoordinationConflict):
    """The source or its project already holds its bound of unexecuted reports."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retry_after = STORAGE_REPORT_RETRY_AFTER_SECONDS


class StorageReportUnsupported(CoordinationConflict):
    """The worker that would run the next report cannot run storage reports."""


def source_actor_prefix(source_id: str) -> str:
    return f"{STORAGE_SOURCE_ACTOR_PREFIX}{source_id}:"


def source_id_of_actor(actor_id: str) -> str:
    """The source ID inside a well-formed storage principal."""
    return actor_id.removeprefix(STORAGE_SOURCE_ACTOR_PREFIX).rpartition(":")[0]


async def storage_report_backlog(
    session, project_id: str, source_id: str
) -> tuple[int, int]:
    """(source, project) counts of unexecuted storage reports in a project."""
    pending = (
        select(func.count())
        .select_from(RuntimeCommandRow)
        .where(
            RuntimeCommandRow.project_id == project_id,
            RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
            RuntimeCommandRow.state.in_(_UNEXECUTED),
        )
    )
    source = await session.scalar(
        pending.where(
            RuntimeCommandRow.actor_id.startswith(
                source_actor_prefix(source_id), autoescape=True
            )
        )
    )
    project = await session.scalar(pending)
    return int(source or 0), int(project or 0)


def refuse_backlog(source_count: int, project_count: int) -> None:
    if source_count >= STORAGE_REPORT_SOURCE_LIMIT:
        raise StorageReportBacklog(
            "This storage source already has its limit of reports waiting to run"
        )
    if project_count >= STORAGE_REPORT_PROJECT_LIMIT:
        raise StorageReportBacklog(
            "This project already has its limit of storage reports waiting to run"
        )


async def _release_supports(session, version: str | None) -> bool:
    if version is None:
        return False
    release = await session.get(RuntimeReleaseRow, version)
    return (
        release is not None
        and STORAGE_REPORT_FEATURE
        in ReleaseManifest.model_validate(release.manifest).features
    )


async def storage_reports_supported(
    session, project_id: str, *, default: bool, now_ms: int
) -> bool:
    """Whether every worker that may run this project's next report can run it.

    That is the live owner (an expired one runs nothing more), the release a
    new owner starts from, and the target of a handoff still in flight.
    ``default`` stands for a host with no promoted target, whose owners start
    from the API's own build.
    """
    owner = await session.get(ProjectOwnershipRow, project_id)
    if owner is not None and owner.runtime_id and owner.lease_expires_at_ms > now_ms:
        process = await session.get(RuntimeProcessRow, owner.runtime_id)
        if not await _release_supports(
            session, process.version if process is not None else None
        ):
            return False
    host = await session.get(DeploymentHostRow, "host")
    target = host.target_version if host is not None else None
    if target is None:
        if not default:
            return False
    elif not await _release_supports(session, target):
        return False
    handoff = await session.get(ProjectHandoffRow, project_id)
    if handoff is not None and handoff.phase != HandoffPhase.VERIFIED:
        if not await _release_supports(session, handoff.target_version):
            return False
    return True


def storage_report_admission(
    coordination: RuntimeCoordination,
    *,
    project_id: str,
    actor_id: str,
    release_default: bool | None,
    discard: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """The submit hook that bounds and fences one new storage report.

    It runs after ``submit`` locked the project's ownership row. A refusal
    removes the report's vault payload unless another saved report of the same
    principal shares it; a concurrent save of that report waits for this lock.
    """
    source_id = source_id_of_actor(actor_id)

    async def admit(session) -> None:
        try:
            if release_default is not None and not await storage_reports_supported(
                session,
                project_id,
                default=release_default,
                now_ms=await coordination._clock_ms(session),
            ):
                raise StorageReportUnsupported(
                    "The worker that runs this project's next report cannot run "
                    "storage reports yet"
                )
            refuse_backlog(
                *await storage_report_backlog(session, project_id, source_id)
            )
        except CoordinationConflict:
            await discard(session)
            raise

    return admit


class StorageReportControl:
    """An execution owner's control over its project's queued storage reports."""

    def __init__(self, coordination: RuntimeCoordination, lease: ProjectLease):
        self.coordination, self.lease = coordination, lease

    async def cancel(self, source) -> int:
        """Settle the source's queued reports whose principal is no longer valid."""
        from .storage_sender import storage_source_actor

        keep = (
            storage_source_actor(source)
            if source.enabled and source.engagement_id == self.lease.project_id
            else None
        )
        return await self.coordination.cancel_storage_reports(
            self.lease, source_id=source.id, keep_actor=keep
        )


async def discard_unreferenced(session, vault, actor_id: str, reference: str, purpose):
    """Remove a request payload unless a saved report of this principal uses it.

    Reports of one principal with the same body share one content-addressed
    payload. The caller holds the project's ownership row lock, so a save of the
    same report in this project either committed before this check or re-checks
    its payload after the lock is released.
    """
    referenced = await session.scalar(
        select(RuntimeCommandRow.id)
        .where(
            RuntimeCommandRow.actor_id == actor_id,
            RuntimeCommandRow.payload["request_reference"].as_string() == reference,
        )
        .limit(1)
    )
    if referenced is None:
        await asyncio.to_thread(vault.discard, reference, purpose=purpose)
