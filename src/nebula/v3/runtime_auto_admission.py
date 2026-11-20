"""Project admission for chats and queued project work, and cold API recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import and_, case, delete, func, or_, select

from .database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    EntityRow,
    MemoryBudgetPolicyRow,
    ProjectAdmissionHoldRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCapacityReservationRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
    RuntimeReleaseRow,
    RuntimeResourceRow,
)
from .deployment_protocol import (
    BUTLER_HOME_FEATURE,
    BUTLER_HOME_PROJECT,
    GLOBAL_EXECUTION_PROJECT,
    IDLE_RETIREMENT_FEATURE,
    ReleaseManifest,
    WorkerIdentity,
)
from .diagnostics import record_caught_exception, record_diagnostic
from .domain import Engagement
from .memory_policy import MemoryBudgetError
from .runtime_coordination import (
    CoordinationConflict,
    pending_storage_dispatches,
    project_worker_commands,
)
from .runtime_ipc import RuntimeIpcError
from .runtime_operations import _DURABLE_OUTCOME_ACTIONS
from .runtime_view_leases import live_view_lease
from .setup import bootstrap_scratch_project

_RECOVERY_ACTOR = "runtime-auto-recovery"
# A failed project admission is retried after these delays, and at once when
# the host or memory policy changes. After _ADMISSION_PERSISTENT failures in a
# row the diagnostic says the failure is not transient.
_ADMISSION_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_ADMISSION_PERSISTENT = 5
_ATTACHABLE = ("reserved", "starting", "ready")
# Expired-owner recovery never stops for a project. Each recovery within
# _RECOVERY_WINDOW_MS of the previous ones waits longer before the next:
# _RECOVERY_BACKOFF_MS doubling per recent recovery, capped at
# _RECOVERY_BACKOFF_MAX_MS, so a worker that keeps failing is retried every
# few minutes instead of crash-looping, and recovers once the failure stops.
_RECOVERY_WINDOW_MS = 30 * 60_000
_RECOVERY_BACKOFF_MS = 15_000
_RECOVERY_BACKOFF_MAX_MS = 5 * 60_000
_RECOVERY_RETRY_MS = 30_000
# Fresh targets one expired owner's recovery may prepare after earlier ones
# ended before they took the project.
_PREPARE_ATTEMPTS = 32


def _attempt_stage(action: str, attempt: int) -> str:
    # The first attempt keeps the original operation identity.
    return action if attempt == 1 else f"{action}#{attempt}"


# A project worker stays this long after the project's last routed request
# before it retires for lack of open chats or work, so a rename, archive or
# second action on a quiet project does not pay another cold start. Bounded;
# NEBULA_IDLE_WORKER_GRACE_SECONDS overrides it and 0 restores immediate
# retirement. Grace ends early while another project waits for a worker.
IDLE_WORKER_GRACE_SECONDS = 300
MAX_IDLE_WORKER_GRACE_SECONDS = 3600


def idle_worker_grace_ms() -> int:
    raw = os.environ.get("NEBULA_IDLE_WORKER_GRACE_SECONDS")
    try:
        seconds = float(raw) if raw is not None else IDLE_WORKER_GRACE_SECONDS
    except ValueError:
        # diagnostic-expected: an unreadable override keeps the default grace.
        seconds = IDLE_WORKER_GRACE_SECONDS
    if seconds != seconds:  # NaN
        seconds = IDLE_WORKER_GRACE_SECONDS
    return int(min(MAX_IDLE_WORKER_GRACE_SECONDS, max(0.0, seconds)) * 1000)


# A project whose wanted worker the memory reserve refuses first gets the
# memory of a resident worker that runs nothing (open chats are saved state
# and need no worker). When every resident worker is busy, the project's
# requests wait at most this long before they fail with the projects holding
# the memory; the project's held state says so meanwhile. Bounded;
# NEBULA_WORKER_MEMORY_WAIT_SECONDS overrides it.
WORKER_MEMORY_WAIT_SECONDS = 180
MIN_WORKER_MEMORY_WAIT_SECONDS = 10
MAX_WORKER_MEMORY_WAIT_SECONDS = 3600
# A resident worker that refused to give up its memory (it was quiescing work
# SQL could not see) is not asked again for this long.
_CAPACITY_RETIREMENT_RETRY_SECONDS = 30.0


def worker_memory_wait_seconds() -> float:
    raw = os.environ.get("NEBULA_WORKER_MEMORY_WAIT_SECONDS")
    try:
        seconds = float(raw) if raw is not None else WORKER_MEMORY_WAIT_SECONDS
    except ValueError:
        # diagnostic-expected: an unreadable override keeps the default wait.
        seconds = WORKER_MEMORY_WAIT_SECONDS
    if seconds != seconds:  # NaN
        seconds = WORKER_MEMORY_WAIT_SECONDS
    return min(
        MAX_WORKER_MEMORY_WAIT_SECONDS, max(MIN_WORKER_MEMORY_WAIT_SECONDS, seconds)
    )


def memory_refusal(error: BaseException | None) -> MemoryBudgetError | None:
    """The memory-reserve refusal behind an admission failure, if any.

    The supervisor reports a refused start as OperationRejected raised from
    the reserve's MemoryBudgetError.
    """
    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, MemoryBudgetError):
            return error
        error = error.__cause__ or error.__context__
        seen += 1
    return None


# A refused automatic recovery step proved no effect; it is asked again after
# this long, so a transient refusal never blocks recovery for good.
REFUSED_RECOVERY_RETRY_MS = 30_000
REFUSED_RECOVERY_MAX_RETRY_MS = 600_000


@dataclass(frozen=True)
class _AdmissionRetry:
    signature: tuple[int, int | None]
    failures: int
    retry_at: float
    # When this project first failed to get a worker, on the loop clock.
    waiting_since: float = 0.0
    # Whether the latest failure was the memory reserve's refusal.
    memory: bool = False
    # Whether a resident worker already gave its memory to this project since
    # that failure; another is released only once this one is refused again.
    released: bool = False


class RuntimeAutoAdmission:
    def __init__(self, supervisor, *, default_version: str):
        self.supervisor = supervisor
        self.default_version = default_version
        self.task = None
        self._pending: dict[str, str] = {}
        # Cold API recovery only; projects use _admission_retry.
        self._blocked: dict[str, tuple[int, int | None]] = {}
        self._admission_retry: dict[str, _AdmissionRetry] = {}
        self._retirement_cursor: str | None = None
        self.idle_grace_ms = idle_worker_grace_ms()
        self._legacy_chat_cursor: tuple[str, int, str] | None = None
        self._legacy_chat_scan_complete = False
        self._legacy_chat_vault = None
        self._legacy_chat_retry: dict[str, tuple[str, str, dict, int]] = {}
        self._legacy_chat_next_scan_at = 0.0
        self._recovery_blocked: dict[str, tuple[str, int, int]] = {}
        # The recent-recovery count last reported per backing-off project.
        self._recovery_backoff_noted: dict[str, int] = {}
        self._recovery_cursor: str | None = None
        # Whether each staged release can quiesce under a project retirement
        # hold. A staged release's manifest never changes.
        self._idle_retirement: dict[str, bool] = {}
        # Whether each staged release can host the Butler home project, and
        # the last release noted as unable to, so the hold is said once.
        self._butler_home: dict[str, bool] = {}
        self._butler_home_noted: str | None = None
        # The idle worker last noted as skipped, and why, so each skip is
        # recorded once instead of on every half-second pass.
        self._idle_skips: dict[str, str] = {}
        self.memory_wait_seconds = worker_memory_wait_seconds()
        # Resident projects whose worker refused a capacity retirement, and
        # when they may be asked again (loop clock).
        self._capacity_refused: dict[str, float] = {}
        self._serial = asyncio.Lock()

    def _note_api_hold(self, reason: str | None) -> None:
        """Let health tell a refused API admission from a start in progress."""
        bootstrap = getattr(self.supervisor, "bootstrap", None)
        if bootstrap is not None:
            bootstrap.api_admission_hold = reason

    async def start(self):
        if self.task is not None:
            return
        await bootstrap_scratch_project(self.supervisor.store)
        # diagnostic-expected: the owned loop records operation failures and is cancelled and joined on close.
        self.task = asyncio.create_task(self._run(), name="nebula-first-admission")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self):
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "runtime.first_admission.waiting",
                    "New project work remains queued while admission is unavailable.",
                    error,
                    stage="admission",
                )
            await asyncio.sleep(0.5)

    async def reconcile(self):
        async with self._serial:
            supervisor = self.supervisor
            await self._recover_legacy_chat_starts()
            async with supervisor.coordinator.database.session() as session:
                host = await session.get(DeploymentHostRow, "host")
                if host is None or host.shutdown_requested:
                    return
                policy = await session.get(MemoryBudgetPolicyRow, "host")
                now_ms = await supervisor.coordinator.coordination._clock_ms(session)
                signature = (host.revision, policy.revision if policy else None)
                version = host.target_version or self.default_version
                promotion = await session.scalar(
                    select(DeploymentReceiptRow)
                    .where(
                        DeploymentReceiptRow.action == "api.promoted",
                    )
                    .order_by(
                        DeploymentReceiptRow.result["revision"]
                        .as_integer()
                        .desc()
                        .nullslast(),
                        DeploymentReceiptRow.occurred_at_ms.desc(),
                        DeploymentReceiptRow.id.desc(),
                    )
                    .limit(1)
                )
                api_version = (
                    promotion.result.get("version", self.default_version)
                    if promotion
                    else self.default_version
                )
                open_chat = (
                    select(EntityRow.id)
                    .where(
                        EntityRow.kind == "chat_sessions",
                        EntityRow.engagement_id == ProjectOwnershipRow.project_id,
                        EntityRow.payload["metadata"]["archived_at"]
                        .as_string()
                        .is_(None),
                    )
                    .exists()
                )
                untracked_open_projects = list(
                    await session.scalars(
                        select(EntityRow.engagement_id)
                        .where(
                            EntityRow.kind == "chat_sessions",
                            EntityRow.engagement_id.is_not(None),
                            EntityRow.payload["metadata"]["archived_at"]
                            .as_string()
                            .is_(None),
                            ~select(ProjectOwnershipRow.project_id)
                            .where(
                                ProjectOwnershipRow.project_id
                                == EntityRow.engagement_id
                            )
                            .exists(),
                        )
                        .distinct()
                        .limit(16)
                    )
                )
                # Some Core-owned paths create a chat session without first
                # passing through RuntimeRouting. Give only that session's
                # exact project a fencing counter so the normal open-chat
                # demand query below can admit it.
                for project_id in untracked_open_projects:
                    await supervisor.coordinator.coordination._ensure_project(
                        session, project_id
                    )
                pending_chat_start = (
                    select(RuntimeCommandRow.id)
                    .where(
                        RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                        RuntimeCommandRow.chat_start.is_(True),
                        RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                    )
                    .exists()
                )
                legacy_chat_start = (
                    select(RuntimeCommandRow.id)
                    .where(
                        RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                        RuntimeCommandRow.name.in_(
                            ("chat.accept/v1", "chat.resume/v1")
                        ),
                        RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                    )
                    .exists()
                )
                # A rename, archive, delete or any other project-routed write
                # waits for the project's worker even without an open chat.
                queued_work = project_worker_commands(ProjectOwnershipRow.project_id)
                # Retiring a worker while its project command runs would only
                # record a blocker every pass; wait for the command to finish.
                active_work = project_worker_commands(
                    ProjectOwnershipRow.project_id, ("queued", "running")
                )
                # A recorded storage alert opens its incident chat from the
                # project's worker; keep or start one until it has.
                storage_alert = pending_storage_dispatches(
                    ProjectOwnershipRow.project_id, session.bind.dialect.name
                )
                now = asyncio.get_running_loop().time()
                # Projects waiting out a retry delay must not fill the batch
                # and starve projects that could be admitted now.
                waiting = [
                    project_id
                    for project_id, retry in self._admission_retry.items()
                    if retry.signature == signature and now < retry.retry_at
                ]
                # An open project view (a live view lease) is demand like an
                # open chat. Once it lapses the worker keeps its idle grace,
                # when the host has one, before it retires.
                viewed = live_view_lease(ProjectOwnershipRow.project_id, now_ms)
                # Demand beyond an open chat: something the operator asked
                # for or is looking at. Only it takes memory from resident
                # projects; an open chat alone waits for a free slot.
                active_demand = or_(
                    pending_chat_start,
                    legacy_chat_start,
                    queued_work,
                    viewed,
                    storage_alert,
                )
                demand_query = (
                    select(ProjectOwnershipRow, queued_work, active_demand)
                    # A verified quiescence clears the owner while retaining
                    # its fencing history. Reopening may claim the next fence;
                    # expired or uncertain owners still have a runtime ID.
                    .where(
                        ProjectOwnershipRow.runtime_id.is_(None),
                        # A held project refuses the grant; preparing a
                        # worker for it would only leave one unassigned.
                        ProjectOwnershipRow.retirement_requested.is_(False),
                        or_(
                            open_chat,
                            pending_chat_start,
                            legacy_chat_start,
                            queued_work,
                            viewed,
                            storage_alert,
                        ),
                    )
                )
                # A project that still wants a worker its admission could not
                # start means capacity is short: idle workers spend no grace
                # while one waits, so they give their memory reserve back.
                short_of_capacity = False
                if self.idle_grace_ms and self._admission_retry:
                    short_of_capacity = (
                        await session.scalar(
                            demand_query.with_only_columns(
                                ProjectOwnershipRow.project_id
                            )
                            .where(
                                ProjectOwnershipRow.project_id.in_(
                                    list(self._admission_retry)
                                )
                            )
                            .limit(1)
                        )
                    ) is not None
                # Projects whose active demand the memory reserve refused.
                capacity_waiters: list[str] = []
                memory_waiters = [
                    project_id
                    for project_id, retry in self._admission_retry.items()
                    if retry.memory
                ]
                if memory_waiters:
                    capacity_waiters = list(
                        await session.scalars(
                            select(ProjectOwnershipRow.project_id)
                            .where(
                                ProjectOwnershipRow.project_id.in_(memory_waiters),
                                ProjectOwnershipRow.runtime_id.is_(None),
                                ProjectOwnershipRow.retirement_requested.is_(False),
                                active_demand,
                            )
                            .order_by(ProjectOwnershipRow.project_id)
                            .limit(8)
                        )
                    )
                if waiting:
                    demand_query = demand_query.where(
                        ProjectOwnershipRow.project_id.not_in(waiting)
                    )
                demands = list(
                    await session.execute(
                        demand_query.order_by(
                            case((active_demand, 0), else_=1),
                            ProjectOwnershipRow.project_id,
                        ).limit(16)
                    )
                )
                idle = [
                    ProjectOwnershipRow.runtime_id.is_not(None),
                    ProjectOwnershipRow.lease_expires_at_ms > now_ms,
                    ~open_chat,
                    ~pending_chat_start,
                    ~legacy_chat_start,
                    ~active_work,
                    # An open project view keeps its worker like an open chat.
                    ~viewed,
                    ~storage_alert,
                ]
                if self.idle_grace_ms and not short_of_capacity:
                    idle.append(
                        ~select(RuntimeCommandRow.id)
                        .where(
                            RuntimeCommandRow.project_id
                            == ProjectOwnershipRow.project_id,
                            RuntimeCommandRow.accepted_at_ms
                            > now_ms - self.idle_grace_ms,
                        )
                        .exists()
                    )
                    # A view that closed within the grace keeps it too: the
                    # grace runs from the lease's lapse, not from a second timer.
                    idle.append(
                        ~live_view_lease(
                            ProjectOwnershipRow.project_id,
                            now_ms,
                            within_ms=self.idle_grace_ms,
                        )
                    )
                idle_query = select(
                    ProjectOwnershipRow.project_id,
                    ProjectOwnershipRow.runtime_id,
                ).where(*idle)
                if self._retirement_cursor is not None:
                    idle_query = idle_query.where(
                        ProjectOwnershipRow.project_id > self._retirement_cursor
                    )
                idle_owners = list(
                    await session.execute(
                        idle_query.order_by(ProjectOwnershipRow.project_id).limit(8)
                    )
                )
                if not idle_owners and self._retirement_cursor is not None:
                    self._retirement_cursor = None
                    idle_owners = list(
                        await session.execute(
                            select(
                                ProjectOwnershipRow.project_id,
                                ProjectOwnershipRow.runtime_id,
                            )
                            .where(*idle)
                            .order_by(ProjectOwnershipRow.project_id)
                            .limit(8)
                        )
                    )
                self._retirement_cursor = idle_owners[-1][0] if idle_owners else None
                # During a rolling release, workers of the previous release
                # stay resident until their handoff. One that cannot quiesce
                # outside a host shutdown would refuse every retirement, and
                # each refused hold briefly fails the project's reads and chat
                # admission. The deployer's handoff moves it instead.
                idle_owners = [
                    (project_id, runtime_id)
                    for project_id, runtime_id in idle_owners
                    if await self._retires_idle(session, project_id, runtime_id)
                ]
                # Resident workers whose project runs, views and awaits
                # nothing (open chats aside), least recently used first. Each
                # one retired makes room for one waiting project.
                # A waiter that was already given memory tries it before it
                # takes more.
                wanting = [
                    project_id
                    for project_id in capacity_waiters
                    if not self._admission_retry[project_id].released
                ]
                capacity_owners: list[tuple[str, str]] = []
                needed = len(wanting) - len(idle_owners)
                if needed > 0:
                    loop_now = asyncio.get_running_loop().time()
                    refused = [
                        project_id
                        for project_id, until in self._capacity_refused.items()
                        if until > loop_now
                    ]
                    last_used = (
                        select(func.max(RuntimeCommandRow.accepted_at_ms))
                        .where(
                            RuntimeCommandRow.project_id
                            == ProjectOwnershipRow.project_id
                        )
                        .scalar_subquery()
                    )
                    capacity_query = select(
                        ProjectOwnershipRow.project_id,
                        ProjectOwnershipRow.runtime_id,
                    ).where(
                        ProjectOwnershipRow.runtime_id.is_not(None),
                        ProjectOwnershipRow.lease_expires_at_ms > now_ms,
                        ProjectOwnershipRow.retirement_requested.is_(False),
                        ProjectOwnershipRow.project_id != GLOBAL_EXECUTION_PROJECT,
                        # Butler's own worker is not traded for memory: it is
                        # what the operator uses to see the other projects.
                        ProjectOwnershipRow.project_id != BUTLER_HOME_PROJECT,
                        ProjectOwnershipRow.project_id.not_in(
                            [*capacity_waiters, *(p for p, _r in idle_owners)]
                        ),
                        ~pending_chat_start,
                        ~legacy_chat_start,
                        ~active_work,
                        ~viewed,
                        ~storage_alert,
                    )
                    if refused:
                        capacity_query = capacity_query.where(
                            ProjectOwnershipRow.project_id.not_in(refused)
                        )
                    capacity_owners = [
                        (project_id, runtime_id)
                        for project_id, runtime_id in await session.execute(
                            capacity_query.order_by(
                                func.coalesce(last_used, 0),
                                ProjectOwnershipRow.project_id,
                            ).limit(needed)
                        )
                        if await self._retires_idle(session, project_id, runtime_id)
                    ]
                api_id = host.api_runtime_id
            if api_id is None and self._blocked.get("api") != signature:
                try:
                    worker = await supervisor.prepare_api(api_version)
                    current = await supervisor.coordinator.status()
                    await supervisor.promote_api(
                        worker.runtime_id, expected_revision=current.revision
                    )
                    self._blocked.pop("api", None)
                    self._note_api_hold(None)
                except Exception as error:
                    self._blocked["api"] = signature
                    self._note_api_hold(str(error))
                    record_caught_exception(
                        "runtime",
                        "runtime.api_admission.waiting",
                        "Host recovery remains available while API admission is held.",
                        error,
                        stage="admission",
                    )
                return
            await self._recover_expired()
            runtime_host = getattr(supervisor, "host", None)
            if runtime_host is not None:
                reconcile_retirements = getattr(
                    runtime_host, "reconcile_idle_project_retirements", None
                )
                if reconcile_retirements is not None:
                    await reconcile_retirements()
                retire_idle = getattr(runtime_host, "retire_idle_project", None)
                if retire_idle is not None:
                    for project_id, _runtime_id in idle_owners:
                        try:
                            await retire_idle(project_id)
                        except Exception as error:
                            record_caught_exception(
                                "runtime",
                                "runtime.project_retirement.waiting",
                                "An execution worker remains assigned until the project has no chat or work blockers.",
                                error,
                                stage="admission",
                                metadata={
                                    "entity_id": project_id,
                                    "kind": "project",
                                },
                            )
                    released = 0
                    for project_id, _runtime_id in capacity_owners:
                        if await self._release_for_capacity(retire_idle, project_id):
                            released += 1
                    # A released worker's memory goes to the waiting projects
                    # first: they are asked again at once, and projects with
                    # only open chats wait while any of them still waits.
                    for project_id in wanting[:released]:
                        retry = self._admission_retry.get(project_id)
                        if retry is not None:
                            self._admission_retry[project_id] = _AdmissionRetry(
                                retry.signature,
                                retry.failures,
                                0.0,
                                retry.waiting_since,
                                retry.memory,
                                True,
                            )
                    if needed > 0 and not capacity_owners:
                        # Nothing can be freed: requests that waited out the
                        # bound fail, naming the projects holding the memory.
                        await self._fail_memory_waits(wanting)
            await self._forget_settled_pending()
            for demand, has_project_work, is_active in demands:
                if capacity_waiters and not is_active:
                    continue
                retry = self._admission_retry.get(demand.project_id)
                if (
                    retry is not None
                    and retry.signature == signature
                    and asyncio.get_running_loop().time() < retry.retry_at
                ):
                    continue
                try:
                    project = await supervisor.store.get(Engagement, demand.project_id)
                    if project.status == "archived" and not has_project_work:
                        # An archived project's chats keep no worker, but an
                        # explicit request for it (unarchive or delete) does.
                        continue
                    runtime_id = self._pending.get(project.id)
                    if runtime_id is not None:
                        async with supervisor.coordinator.database.session() as session:
                            pending = await session.get(RuntimeProcessRow, runtime_id)
                        if pending is None or pending.state not in (
                            "reserved",
                            "starting",
                            "ready",
                        ):
                            # A quiesced, aborted or retired process never
                            # takes this project; prepare a fresh one.
                            self._pending.pop(project.id, None)
                            runtime_id = None
                    if runtime_id is None:
                        # Unknown unassigned processes may be manually prepared
                        # targets or a lost spawn response. Never steal/recreate
                        # them; require explicit reconciliation instead.
                        async with supervisor.coordinator.database.session() as session:
                            reserved = list(
                                await session.scalars(
                                    select(RuntimeProcessRow.runtime_id).where(
                                        RuntimeProcessRow.role == "execution",
                                        RuntimeProcessRow.state.in_(
                                            ("reserved", "starting", "ready")
                                        ),
                                        ~RuntimeProcessRow.runtime_id.in_(
                                            select(
                                                ProjectOwnershipRow.runtime_id
                                            ).where(
                                                ProjectOwnershipRow.runtime_id.is_not(
                                                    None
                                                )
                                            )
                                        ),
                                    )
                                )
                            )
                        if reserved:
                            continue
                        if (
                            project.id == BUTLER_HOME_PROJECT
                            and not await self._hosts_butler_home(version)
                        ):
                            # An older release would run Butler's home under
                            # project-local Butler code; it waits for a release
                            # that declares butler.home/v1.
                            continue
                        worker = await supervisor.prepare_execution(version)
                        runtime_id = worker.runtime_id
                        self._pending[project.id] = runtime_id
                    await supervisor.attach_execution(project.id, runtime_id)
                    self._pending.pop(project.id, None)
                    self._admission_retry.pop(project.id, None)
                    await self._clear_hold(project.id)
                except Exception as error:
                    failures = (retry.failures if retry is not None else 0) + 1
                    delay = _ADMISSION_BACKOFF_SECONDS[
                        min(failures, len(_ADMISSION_BACKOFF_SECONDS)) - 1
                    ]
                    refusal = memory_refusal(error)
                    memory = refusal is not None
                    # Say why first, so the retry delay runs from after the
                    # write and is not spent on it.
                    await self._record_hold(
                        demand.project_id,
                        error,
                        failures,
                        reason=await self._memory_wait_reason(refusal, bool(is_active))
                        if refusal is not None
                        else None,
                    )
                    now = asyncio.get_running_loop().time()
                    self._admission_retry[demand.project_id] = _AdmissionRetry(
                        signature,
                        failures,
                        # A project the reserve refuses is asked again soon:
                        # memory frees when another project's worker retires.
                        now + (min(delay, 5.0) if memory and is_active else delay),
                        retry.waiting_since if retry is not None else now,
                        memory,
                    )
                    record_caught_exception(
                        "runtime",
                        "runtime.project_admission.waiting",
                        "This project's saved work stays queued; Core retries starting its worker automatically."
                        if failures < _ADMISSION_PERSISTENT
                        else "This project's worker keeps failing to start, so its saved work stays queued. Check the memory budget and runtime status; Core keeps retrying every minute.",
                        error,
                        stage="admission",
                        metadata={
                            "entity_id": demand.project_id,
                            "kind": "project",
                            "attempt": failures,
                        },
                    )

    async def _record_hold(
        self,
        project_id: str,
        error: Exception | None,
        attempts: int,
        *,
        reason: str | None = None,
    ):
        """Say why a wanted worker is not starting, so a view waits calmly.

        Only an operator-facing gate (the memory budget) or an explicit
        ``reason`` is quoted; any other refusal gets a generic reason. Best
        effort: admission and recovery keep retrying.
        """
        if reason is None:
            refusal = memory_refusal(error)
            reason = (
                str(refusal)[:1000]
                if refusal is not None and str(refusal)
                else "This project's worker could not start yet; Core keeps retrying automatically."
            )
        try:
            async with self.supervisor.coordinator.database.session() as session:
                now = await self.supervisor.coordinator.coordination._clock_ms(session)
                row = await session.get(ProjectAdmissionHoldRow, project_id)
                if row is None:
                    session.add(
                        ProjectAdmissionHoldRow(
                            project_id=project_id,
                            reason=reason,
                            attempts=attempts,
                            updated_at_ms=now,
                        )
                    )
                else:
                    row.reason, row.attempts, row.updated_at_ms = reason, attempts, now
        except Exception as failure:
            record_caught_exception(
                "runtime",
                "runtime.project_admission.hold_unrecorded",
                "A held project start could not record its reason; views show a generic wait.",
                failure,
                stage="admission",
            )

    async def _recovery_backoff(self, owner, attempts: int, wait_ms: int) -> None:
        """Say once per new failure that recovery is waiting, and until when."""
        if self._recovery_backoff_noted.get(owner.project_id) == attempts:
            return
        self._recovery_backoff_noted[owner.project_id] = attempts
        seconds = max(1, round(wait_ms / 1000))
        reason = (
            f"This project's worker stopped {attempts} "
            f"time{'s' if attempts != 1 else ''} in the last "
            f"{_RECOVERY_WINDOW_MS // 60_000} minutes. Core recovers it "
            f"automatically again in about {seconds} s and keeps retrying; "
            "check the project's runtime failures if this repeats."
        )
        await self._record_hold(owner.project_id, None, attempts, reason=reason)
        record_diagnostic(
            "warning",
            "runtime",
            "runtime.project_recovery.backoff",
            reason,
            outcome="waiting",
            stage="recovery",
            retryable=True,
            metadata={
                "entity_id": owner.project_id,
                "kind": "project",
                "attempt": attempts,
            },
        )

    async def _memory_holders(self) -> list[str]:
        """Labels of the projects whose resident workers hold Core memory."""
        coordinator = self.supervisor.coordinator
        async with coordinator.database.session() as session:
            projects = list(
                await session.scalars(
                    select(ProjectOwnershipRow.project_id)
                    .where(
                        ProjectOwnershipRow.runtime_id.is_not(None),
                        ProjectOwnershipRow.project_id != GLOBAL_EXECUTION_PROJECT,
                    )
                    .order_by(ProjectOwnershipRow.project_id)
                    .limit(9)
                )
            )
            labels = [
                await coordinator._project_label_in(session, project_id)
                for project_id in projects[:8]
            ]
        if len(projects) > 8:
            labels.append("and more")
        return labels

    async def _memory_wait_reason(
        self, refusal: MemoryBudgetError, active: bool
    ) -> str:
        try:
            holders = await self._memory_holders()
        except Exception as error:
            record_caught_exception(
                "runtime",
                "runtime.project_admission.holders_unread",
                "A project waiting for memory says so without naming the projects holding it.",
                error,
                stage="admission",
            )
            holders = []
        named = " Resident workers: " + ", ".join(holders) + "." if holders else ""
        when = (
            "Core gives it the memory of a resident worker as soon as one is idle."
            if active
            else "Its chats are saved; it starts when memory frees or when you "
            "open or use the project."
        )
        # The gate's own words first: they say whether the reserve is full or
        # below what resident runtimes already need.
        return (str(refusal).strip() + " Waiting for memory: " + when + named)[:1000]

    async def _release_for_capacity(self, retire_idle, project_id: str) -> bool:
        """Retire one idle resident worker so a waiting project can start."""
        if retire_idle is None:
            return False
        refused_until = (
            asyncio.get_running_loop().time() + _CAPACITY_RETIREMENT_RETRY_SECONDS
        )
        try:
            released = bool(await retire_idle(project_id, for_capacity=True))
        except Exception as error:
            self._capacity_refused[project_id] = refused_until
            record_caught_exception(
                "runtime",
                "runtime.project_capacity.retained",
                "An idle project's worker kept its memory; a project waiting for memory keeps waiting.",
                error,
                stage="admission",
                metadata={"entity_id": project_id, "kind": "project"},
            )
            return False
        if not released:
            self._capacity_refused[project_id] = refused_until
            return False
        self._capacity_refused.pop(project_id, None)
        record_diagnostic(
            "info",
            "runtime",
            "runtime.project_capacity.released",
            "An idle project's worker retired to give its memory to a project waiting for one; its chats stay saved.",
            outcome="success",
            stage="admission",
            metadata={"entity_id": project_id, "kind": "project"},
        )
        return True

    async def _fail_memory_waits(self, waiters: list[str]) -> None:
        """End requests that waited too long for memory no worker can free."""
        if not waiters:
            return
        loop_now = asyncio.get_running_loop().time()
        expired = [
            project_id
            for project_id in waiters
            if (retry := self._admission_retry.get(project_id)) is not None
            and loop_now - retry.waiting_since >= self.memory_wait_seconds
        ]
        if not expired:
            return
        coordination = self.supervisor.coordinator.coordination
        holders = await self._memory_holders()
        seconds = round(self.memory_wait_seconds)
        detail = (
            f"This request waited {seconds} s for memory to start its "
            "project's worker and did not run. The Core reserve is full and "
            "every resident worker is busy"
            + (": " + ", ".join(holders) if holders else "")
            + ". Send it again when one of them finishes, or increase Core "
            "reserve in Memory budgets."
        )[:1000]
        result = {
            "http_status": 503,
            "detail": detail,
            "waiting_for_memory": True,
            "not_started": True,
        }
        for project_id in expired:
            try:
                async with coordination.database.session() as session:
                    now_ms = await coordination._clock_ms(session)
                failed = await coordination.fail_unstarted_requests(
                    project_id,
                    accepted_before_ms=now_ms - seconds * 1000,
                    result=result,
                )
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "runtime.project_admission.wait_unsettled",
                    "Requests waiting for memory stay queued; Core tries to settle them again.",
                    error,
                    stage="admission",
                    metadata={"entity_id": project_id, "kind": "project"},
                )
                continue
            if failed:
                record_diagnostic(
                    "warning",
                    "runtime",
                    "runtime.project_admission.memory_timeout",
                    detail,
                    outcome="failure",
                    stage="admission",
                    retryable=True,
                    metadata={
                        "entity_id": project_id,
                        "kind": "project",
                        "requests": failed,
                    },
                )

    async def _clear_hold(self, project_id: str) -> None:
        try:
            async with self.supervisor.coordinator.database.session() as session:
                await session.execute(
                    delete(ProjectAdmissionHoldRow).where(
                        ProjectAdmissionHoldRow.project_id == project_id
                    )
                )
        except Exception as failure:
            record_caught_exception(
                "runtime",
                "runtime.project_admission.hold_unclear",
                "A started project's held reason could not be cleared.",
                failure,
                stage="admission",
            )

    async def _hosts_butler_home(self, version: str) -> bool:
        """Whether ``version`` declares ``butler.home/v1``; says once when not."""

        supported = self._butler_home.get(version)
        if supported is None:
            async with self.supervisor.coordinator.database.session() as session:
                release = await session.get(RuntimeReleaseRow, version)
            supported = release is not None and (
                BUTLER_HOME_FEATURE
                in ReleaseManifest.model_validate(release.manifest).features
            )
            if release is not None:
                # A missing release may still be staged; ask again next pass.
                self._butler_home[version] = supported
        if not supported and self._butler_home_noted != version:
            self._butler_home_noted = version
            record_diagnostic(
                "info",
                "runtime",
                "runtime.butler_home.release_lacks_feature",
                "Butler's own worker waits: the execution release cannot host it.",
                project_id=BUTLER_HOME_PROJECT,
                stage="admission",
                outcome="skipped",
                operator_detail=(
                    "Butler keeps working inside each project until a release "
                    "with butler.home/v1 is the execution target."
                ),
                metadata={"version": version, "feature": BUTLER_HOME_FEATURE},
                force=True,
            )
        return supported

    async def _retires_idle(self, session, project_id: str, runtime_id: str) -> bool:
        process = await session.get(RuntimeProcessRow, runtime_id)
        if process is None:
            self._note_idle_skip(project_id, runtime_id, None, "process_missing")
            return False
        supported = self._idle_retirement.get(process.version)
        if supported is None:
            release = await session.get(RuntimeReleaseRow, process.version)
            if release is None:
                self._note_idle_skip(
                    project_id, runtime_id, process.version, "release_missing"
                )
                return False
            supported = (
                IDLE_RETIREMENT_FEATURE
                in ReleaseManifest.model_validate(release.manifest).features
            )
            self._idle_retirement[process.version] = supported
        if not supported:
            self._note_idle_skip(
                project_id, runtime_id, process.version, "release_lacks_feature"
            )
        return supported

    def _note_idle_skip(
        self, project_id: str, runtime_id: str, version: str | None, kind: str
    ) -> None:
        """Say once why an idle worker stays resident, so it is not a silent leak.

        Its project keeps working on that worker; the deployer's handoff moves
        it to a release that can quiesce. Nothing is held for the operator.
        """
        key = runtime_id + "\0" + project_id
        if self._idle_skips.get(key) == kind:
            return
        if len(self._idle_skips) >= 256:
            self._idle_skips.clear()
        self._idle_skips[key] = kind
        record_diagnostic(
            "info",
            "runtime",
            "runtime.idle_retirement.skipped",
            {
                "release_lacks_feature": (
                    "An idle project's worker stays resident: its release "
                    "cannot quiesce for idle retirement."
                ),
                "release_missing": (
                    "An idle project's worker stays resident: its release "
                    "record is missing."
                ),
                "process_missing": (
                    "An idle project's worker stays resident: its process "
                    "record is missing."
                ),
            }[kind],
            project_id=project_id,
            stage="idle-retirement",
            outcome="skipped",
            operator_detail=(
                "The deployer's rolling handoff moves this project to the "
                "current release, whose workers retire when idle."
                if kind == "release_lacks_feature"
                else "Idle retirement retries on its next pass."
            ),
            metadata={"kind": kind, "version": version},
            # Diagnostics default to errors only; this is an expected state
            # an operator still needs to see when a worker looks leaked.
            force=True,
        )

    async def _forget_settled_pending(self) -> None:
        """Drop a prepared worker that can no longer start its project.

        An attach can time out after the worker already took the project, and
        that worker can later retire. Reusing its runtime ID would retry a dead
        process forever. Only SQL state settles this, never an IPC error: a
        live worker that is still unassigned stays reserved for its project.
        """
        if not self._pending:
            return
        async with self.supervisor.coordinator.database.session() as session:
            for project_id, runtime_id in list(self._pending.items()):
                process = await session.get(RuntimeProcessRow, runtime_id)
                owners = set(
                    await session.scalars(
                        select(ProjectOwnershipRow.project_id).where(
                            ProjectOwnershipRow.runtime_id == runtime_id
                        )
                    )
                )
                if project_id in owners:
                    # The timed-out attach succeeded; the project is owned.
                    self._pending.pop(project_id, None)
                    self._admission_retry.pop(project_id, None)
                elif process is None or process.state not in _ATTACHABLE or owners:
                    # Retired, failed or serving another project: prepare anew.
                    self._pending.pop(project_id, None)

    @staticmethod
    def _recovery_operation_id(owner: dict, stage: str) -> str:
        digest = hashlib.sha256(
            json.dumps([owner, stage], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return "auto-recovery:" + digest

    async def _unassigned_execution_with_effects(self, project_id: str) -> str | None:
        """An unowned execution process that may hold this project's effects.

        Recovery attaches only the worker its own ledgered preparation
        returned, and that deterministic operation is never dispatched twice,
        so an idle unowned worker (a prepared deployment target, a source not
        yet retired) neither competes with nor duplicates it. Recovery of a
        project waits only while an unowned process holds what its retirement
        would also wait for, for that project: an open resource, an unfinished
        command or held capacity. Another project's leftovers stay with that
        project's own reconciliation.
        """
        unowned = ~RuntimeProcessRow.runtime_id.in_(
            select(ProjectOwnershipRow.runtime_id).where(
                ProjectOwnershipRow.runtime_id.is_not(None)
            )
        )
        holds = or_(
            RuntimeProcessRow.runtime_id.in_(
                select(RuntimeResourceRow.runtime_id).where(
                    RuntimeResourceRow.project_id == project_id,
                    RuntimeResourceRow.state != "closed",
                )
            ),
            RuntimeProcessRow.runtime_id.in_(
                select(RuntimeCommandRow.runtime_id).where(
                    RuntimeCommandRow.project_id == project_id,
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
            ),
            RuntimeProcessRow.runtime_id.in_(
                select(RuntimeCapacityReservationRow.runtime_id).where(
                    RuntimeCapacityReservationRow.project_id == project_id,
                    RuntimeCapacityReservationRow.state == "held",
                )
            ),
        )
        async with self.supervisor.coordinator.database.session() as session:
            return await session.scalar(
                select(RuntimeProcessRow.runtime_id)
                .where(
                    RuntimeProcessRow.role == "execution",
                    RuntimeProcessRow.state.in_(("reserved", "starting", "ready")),
                    unowned,
                    holds,
                )
                .limit(1)
            )

    async def _recovery_target(
        self, owner: dict, version: str, gone: dict
    ) -> tuple[str, int]:
        """The prepared target recovery claims, and its preparation attempt.

        Each preparation is a ledgered operation dispatched at most once. A
        prepared target that ended before it took the project (its process
        is gone or no longer attachable) can never be claimed, so the next
        attempt, under its own operation identity, prepares a fresh one; a
        target that is still usable is always kept.
        """
        for attempt in range(1, _PREPARE_ATTEMPTS + 1):
            prepared = await self._recovery_operation(
                owner,
                "prepare_execution",
                {"version": version},
                stage=_attempt_stage("prepare_execution", attempt),
            )
            runtime_id = prepared.get("runtime_id")
            if not isinstance(runtime_id, str) or not runtime_id:
                raise CoordinationConflict(
                    "The saved recovery preparation has no confirmed runtime identity"
                )
            if runtime_id not in gone:
                async with self.supervisor.coordinator.database.session() as session:
                    process = await session.get(RuntimeProcessRow, runtime_id)
                if process is not None and process.state in _ATTACHABLE:
                    return runtime_id, attempt
            record_diagnostic(
                "warning",
                "runtime",
                "runtime.project_recovery.target_lost",
                "A prepared recovery target ended before it took the project; Core prepares a fresh one.",
                outcome="recovering",
                stage="recovery",
                retryable=True,
                metadata={
                    "entity_id": owner["project_id"],
                    "kind": "project",
                    "attempt": attempt,
                },
            )
        raise CoordinationConflict(
            "Every prepared recovery target for this owner ended before it took the project"
        )

    async def _confirm_interrupted_start(self, operation_id: str) -> None:
        """Record the readiness a dead host no longer could.

        The host that spawned this preparation's worker died after the
        bound-child checkpoint and before it recorded the worker ready, so the
        worker runs unowned in ``starting`` and the preparation stays
        uncertain. When that exact incarnation (runtime, PID, birth and
        endpoint from its checkpoint) answers its authenticated identity
        probe, readiness is recorded exactly as the spawn would have, and the
        ledger then settles the preparation from that durable record. A
        worker that does not answer is left for process reconciliation.
        """
        supervisor = self.supervisor
        async with supervisor.coordinator.database.session() as session:
            row = await session.get(DeploymentOperationRow, operation_id)
            origin = ((row.result or {}).get("recovery") or {}) if row else {}
            runtime_id = origin.get("runtime_id")
            process = (
                await session.get(RuntimeProcessRow, runtime_id)
                if isinstance(runtime_id, str)
                else None
            )
        if (
            process is None
            or process.state != "starting"
            or not process.pid
            or not process.process_birth
            or process.endpoint != origin.get("endpoint")
        ):
            return
        identity = WorkerIdentity(
            runtime_id=process.runtime_id,
            version=process.version,
            role=process.role,
            pid=process.pid,
            process_birth=process.process_birth,
            endpoint=process.endpoint,
        )
        client = await supervisor.host.rpc(process.runtime_id)
        try:
            observed = WorkerIdentity.model_validate(
                await client.call("worker.identity", {}, timeout=2)
            )
        except (OSError, httpx.HTTPError, RuntimeIpcError):
            # diagnostic-expected: an unanswered probe leaves the uncertain preparation for the next pass; recovery reports its wait.
            return
        finally:
            await client.close()
        if observed == identity:
            await supervisor.coordinator.worker_ready(identity)

    async def _recovery_operation(
        self,
        owner: dict,
        action: str,
        parameters: dict,
        *,
        stage: str | None = None,
    ):
        management = self.supervisor.management
        operation_id = self._recovery_operation_id(owner, stage or action)
        ledger = management.ledger()
        async with self.supervisor.coordinator.database.session() as session:
            prior = await session.get(DeploymentOperationRow, operation_id)
        retry_refused = False
        if prior is not None and prior.state == "failed":
            # A refused attempt proved it had no effect, so the same
            # deterministic operation is asked again, but not on every tick.
            async with self.supervisor.coordinator.database.session() as session:
                now_ms = await self.supervisor.coordinator.coordination._clock_ms(
                    session
                )
                retries = await session.scalar(
                    select(func.count())
                    .select_from(DeploymentReceiptRow)
                    .where(
                        DeploymentReceiptRow.action == "operation.retried",
                        DeploymentReceiptRow.result["operation_id"].as_string()
                        == operation_id,
                    )
                )
            # A spawn that keeps timing out under load backs off to 10 minutes.
            delay = min(
                REFUSED_RECOVERY_MAX_RETRY_MS,
                REFUSED_RECOVERY_RETRY_MS * 2 ** min(int(retries or 0), 10),
            )
            if now_ms - prior.updated_at_ms < delay:
                detail = (prior.result or {}).get("detail", "refused")
                raise CoordinationConflict(
                    f"Automatic recovery operation {operation_id} was refused "
                    f"({detail}); it is asked again shortly"
                )
            retry_refused = True
        if prior is not None and not retry_refused:
            # Saved parameters and results are authoritative, including an
            # unknown spawn outcome after a host restart.
            receipt = await ledger.status(operation_id, actor_id=_RECOVERY_ACTOR)
            if receipt["state"] == "uncertain" and (
                action in _DURABLE_OUTCOME_ACTIONS or action == "prepare_execution"
            ):
                # Settle it from its durable record: a committed effect is
                # succeeded; a proven absent one is failed and is asked again
                # after the refused-operation backoff. Never re-dispatched.
                # A preparation whose host died settles from its worker spawn
                # checkpoint and process row (a ready, verified worker or a
                # retired reservation); until then it is asked again.
                if action == "prepare_execution":
                    await self._confirm_interrupted_start(operation_id)
                receipt = await ledger.reconcile(operation_id, actor_id=_RECOVERY_ACTOR)
                if receipt["state"] == "failed":
                    raise CoordinationConflict(
                        f"Automatic recovery operation {operation_id} had no "
                        "committed effect; it is asked again shortly"
                    )
        else:
            holder = (
                await self._unassigned_execution_with_effects(owner["project_id"])
                if action == "prepare_execution"
                else None
            )
            if holder is not None:
                raise CoordinationConflict(
                    f"Unassigned execution process {holder} still holds project "
                    "resources, commands or capacity and needs explicit "
                    "reconciliation before automatic recovery"
                )

            async def dispatch(name, values):
                return await management.dispatch(
                    name, values, operation_id, _RECOVERY_ACTOR
                )

            receipt = await ledger.execute(
                operation_id=operation_id,
                actor_id=_RECOVERY_ACTOR,
                action=action,
                parameters=parameters,
                dispatch=dispatch,
                retry_rejected=True,
            )
        if receipt["state"] != "succeeded":
            raise CoordinationConflict(
                f"Automatic recovery operation {operation_id} is {receipt['state']}; "
                "read its saved deployment result before reconciliation"
            )
        return receipt["result"]

    async def _legacy_chat_start_for(self, intent, project_id: str) -> bool:
        """Accept a legacy chat start only when it names this exact project."""
        from .runtime_delivery import RuntimeDelivery

        if not RuntimeDelivery.is_chat_start_intent(intent):
            return False
        if intent.path == "/api/v1/chat/goal-conversations":
            return json.loads(intent.body()).get("engagement_id") == project_id
        if intent.method == "PATCH":
            # An unarchive names only its chat. Promote it for that chat's
            # project, never for whichever project the command row claims.
            chat_id = intent.path.rsplit("/", 1)[1]
            async with self.supervisor.coordinator.database.session() as session:
                owner = await session.scalar(
                    select(EntityRow.engagement_id).where(
                        EntityRow.id == chat_id,
                        EntityRow.kind == "chat_sessions",
                    )
                )
            return owner == project_id
        return False

    async def _recover_legacy_chat_starts(self):
        """Promote queued legacy HTTP requests after authenticating their intent."""
        host = getattr(self.supervisor, "host", None)
        data_root = getattr(host, "data_root", None)
        if data_root is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._legacy_chat_scan_complete and not self._legacy_chat_retry:
            if now < self._legacy_chat_next_scan_at:
                return
            # New API workers persist chat_start on acceptance. This bounded
            # rescan also catches a legacy ingress row committed after startup.
            self._legacy_chat_cursor = None
            self._legacy_chat_scan_complete = False
        from .runtime_chat_acceptance import HTTP_INVOKE_COMMAND
        from .runtime_delivery import HttpIntent, RuntimeDelivery
        from .runtime_vault import RuntimeVault

        vault_root = Path(data_root) / "runtime" / "payloads"
        if not (vault_root / "key").is_file():
            return
        if self._legacy_chat_vault is None:
            self._legacy_chat_vault = RuntimeVault(vault_root)
        vault = self._legacy_chat_vault
        candidate_by_id: dict[str, tuple[str, str, dict, int]] = {}
        scan_exhausted = False
        async with self.supervisor.coordinator.database.session() as session:
            if self._legacy_chat_retry:
                retry_rows = await session.scalars(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.id.in_(list(self._legacy_chat_retry))
                    )
                )
                current_retries = {row.id: row for row in retry_rows}
                for command_id, prior in list(self._legacy_chat_retry.items()):
                    row = current_retries.get(command_id)
                    if (
                        row is None
                        or row.name != HTTP_INVOKE_COMMAND
                        or row.state != "queued"
                        or row.chat_start
                        or (row.project_id, row.actor_id, row.payload)
                        != (prior[0], prior[1], prior[2])
                    ):
                        self._legacy_chat_retry.pop(command_id, None)
                        continue
                    candidate_by_id[command_id] = (
                        row.project_id,
                        row.actor_id,
                        row.payload,
                        row.sequence,
                    )

            query = select(
                RuntimeCommandRow.id,
                RuntimeCommandRow.project_id,
                RuntimeCommandRow.actor_id,
                RuntimeCommandRow.payload,
                RuntimeCommandRow.sequence,
            ).where(
                RuntimeCommandRow.name == HTTP_INVOKE_COMMAND,
                RuntimeCommandRow.state == "queued",
                RuntimeCommandRow.chat_start.is_(False),
            )
            if self._legacy_chat_cursor is not None:
                project_id, sequence, command_id = self._legacy_chat_cursor
                query = query.where(
                    or_(
                        RuntimeCommandRow.project_id > project_id,
                        and_(
                            RuntimeCommandRow.project_id == project_id,
                            RuntimeCommandRow.sequence > sequence,
                        ),
                        and_(
                            RuntimeCommandRow.project_id == project_id,
                            RuntimeCommandRow.sequence == sequence,
                            RuntimeCommandRow.id > command_id,
                        ),
                    )
                )
            candidates = list(
                await session.execute(
                    query.order_by(
                        RuntimeCommandRow.project_id,
                        RuntimeCommandRow.sequence,
                        RuntimeCommandRow.id,
                    ).limit(16)
                )
            )
            if candidates:
                self._legacy_chat_cursor = (
                    candidates[-1].project_id,
                    candidates[-1].sequence,
                    candidates[-1].id,
                )
            else:
                scan_exhausted = True
            for row in candidates:
                candidate_by_id[row.id] = (
                    row.project_id,
                    row.actor_id,
                    row.payload,
                    row.sequence,
                )

        for command_id, (project_id, actor_id, payload, _sequence) in list(
            candidate_by_id.items()
        ):
            try:
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"format", "request_reference"}
                    or payload.get("format") != 1
                    or not isinstance(payload.get("request_reference"), str)
                ):
                    continue
                raw = await asyncio.to_thread(
                    vault.get,
                    payload["request_reference"],
                    purpose=RuntimeDelivery._intent_purpose(actor_id),
                )
                intent = HttpIntent.model_validate_json(raw)
                if not await self._legacy_chat_start_for(intent, project_id):
                    self._legacy_chat_retry.pop(command_id, None)
                    continue
                marked = await self.supervisor.coordinator.coordination.mark_queued_chat_start(
                    project_id=project_id,
                    command_id=command_id,
                    actor_id=actor_id,
                    payload=payload,
                )
                if marked:
                    self._legacy_chat_retry.pop(command_id, None)
                else:
                    self._legacy_chat_retry[command_id] = (
                        project_id,
                        actor_id,
                        payload,
                        _sequence,
                    )
            except Exception as error:
                self._legacy_chat_retry[command_id] = (
                    project_id,
                    actor_id,
                    payload,
                    _sequence,
                )
                record_caught_exception(
                    "runtime",
                    "runtime.legacy_chat_start.unavailable",
                    "A legacy queued command remains held because its chat intent "
                    "could not be verified.",
                    error,
                    stage="admission",
                    metadata={"entity_id": command_id, "kind": "runtime-command"},
                )
        if scan_exhausted and not self._legacy_chat_retry:
            self._legacy_chat_scan_complete = True
            self._legacy_chat_next_scan_at = loop.time() + 30.0

    async def _recover_expired(self):
        supervisor = self.supervisor
        async with supervisor.coordinator.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            if host is None or host.shutdown_requested:
                return
            target_version = host.target_version
            now = await supervisor.coordinator.coordination._clock_ms(session)
            open_chat = (
                select(EntityRow.id)
                .where(
                    EntityRow.kind == "chat_sessions",
                    EntityRow.engagement_id == ProjectOwnershipRow.project_id,
                    EntityRow.payload["metadata"]["archived_at"].as_string().is_(None),
                )
                .exists()
            )
            pending_chat_start = (
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                    RuntimeCommandRow.chat_start.is_(True),
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
                .exists()
            )
            legacy_chat_start = (
                select(RuntimeCommandRow.id)
                .where(
                    RuntimeCommandRow.project_id == ProjectOwnershipRow.project_id,
                    RuntimeCommandRow.name.in_(("chat.accept/v1", "chat.resume/v1")),
                    RuntimeCommandRow.state.in_(("queued", "running", "uncertain")),
                )
                .exists()
            )
            # Queued project work admits a worker without any chat, so the
            # same work needs that worker recovered when it dies. A command
            # the dead owner was running becomes uncertain during recovery.
            project_work = project_worker_commands(
                ProjectOwnershipRow.project_id, ("queued", "running")
            )
            query = select(ProjectOwnershipRow, project_work).where(
                ProjectOwnershipRow.runtime_id.is_not(None),
                ProjectOwnershipRow.lease_expires_at_ms <= now,
                or_(
                    open_chat,
                    pending_chat_start,
                    legacy_chat_start,
                    project_work,
                    # A storage alert recorded before the owner died opens its
                    # incident chat only from a recovered worker.
                    pending_storage_dispatches(
                        ProjectOwnershipRow.project_id, session.bind.dialect.name
                    ),
                ),
            )
            if self._recovery_cursor is not None:
                query = query.where(
                    ProjectOwnershipRow.project_id > self._recovery_cursor
                )
            owners = list(
                await session.execute(
                    query.order_by(ProjectOwnershipRow.project_id).limit(16)
                )
            )
        # This cursor only schedules bounded scans; operation receipts remain
        # the authority across controller or host restarts.
        self._recovery_cursor = owners[-1][0].project_id if owners else None
        if not owners:
            return
        # Older embedders without a process host cannot prove OS absence.
        if (
            getattr(supervisor, "host", None) is None
            or getattr(supervisor, "management", None) is None
        ):
            return
        discovered = await supervisor.host.rediscover()
        observed = {
            worker.runtime_id: worker
            for worker in discovered
            if worker.process_state == "gone" and worker.identity is not None
        }
        # A frozen or hung owner keeps a live process but can never renew its
        # expired lease. Once it is provably unresponsive the host kills it,
        # and the normal dead-owner recovery below runs in this same pass.
        retire_unresponsive = getattr(supervisor.host, "retire_unresponsive", None)
        expired = {owner.runtime_id for owner, _work in owners}
        for worker in discovered:
            if (
                retire_unresponsive is None
                or worker.runtime_id not in expired
                or worker.runtime_id in observed
                or worker.process_state != "alive"
                or getattr(worker, "reachable", True)
                or worker.identity is None
                or worker.identity.role != "execution"
            ):
                continue
            try:
                if await retire_unresponsive(worker.identity):
                    observed[worker.runtime_id] = worker
            except Exception as error:
                record_caught_exception(
                    "runtime",
                    "runtime.project_recovery.unresponsive_worker",
                    "An unresponsive project worker could not be retired; its "
                    "project stays held until the process ends.",
                    error,
                    stage="recovery",
                    metadata={"entity_id": worker.runtime_id, "kind": "runtime"},
                )
        for owner, has_project_work in owners:
            worker = observed.get(owner.runtime_id)
            if worker is None:
                continue
            blocked = self._recovery_blocked.get(owner.project_id)
            if blocked and blocked[:2] == (owner.runtime_id, owner.fencing_token):
                if blocked[2] > now:
                    continue
            try:
                project = await supervisor.store.get(Engagement, owner.project_id)
                if project.status == "archived" and not has_project_work:
                    # As in admission: an archived project's chats keep no
                    # worker, but its delete or unarchive request does.
                    continue
                expected_owner = {
                    "project_id": owner.project_id,
                    "runtime_id": owner.runtime_id,
                    "fencing_token": owner.fencing_token,
                }
                async with supervisor.coordinator.database.session() as session:
                    existing = await session.scalar(
                        select(RuntimeRecoveryRow.operation_id)
                        .where(
                            RuntimeRecoveryRow.project_id == owner.project_id,
                            RuntimeRecoveryRow.source_runtime_id == owner.runtime_id,
                            RuntimeRecoveryRow.source_fencing_token
                            == owner.fencing_token,
                            RuntimeRecoveryRow.phase.in_(
                                ("held", "prepared", "verified")
                            ),
                        )
                        .limit(1)
                    )
                    attempts, latest = (
                        await session.execute(
                            select(
                                func.count(), func.max(RuntimeRecoveryRow.created_at_ms)
                            ).where(
                                RuntimeRecoveryRow.project_id == owner.project_id,
                                RuntimeRecoveryRow.created_at_ms
                                >= now - _RECOVERY_WINDOW_MS,
                            )
                        )
                    ).one()
                if existing is not None:
                    continue
                if attempts:
                    due = latest + min(
                        _RECOVERY_BACKOFF_MAX_MS,
                        _RECOVERY_BACKOFF_MS * 2 ** min(attempts - 1, 10),
                    )
                    if now < due:
                        await self._recovery_backoff(owner, attempts, due - now)
                        self._recovery_blocked[owner.project_id] = (
                            owner.runtime_id,
                            owner.fencing_token,
                            due,
                        )
                        continue
                await self._recovery_operation(
                    expected_owner,
                    "reconcile_project",
                    {"expected_owner": expected_owner},
                )
                target_runtime_id, attempt = await self._recovery_target(
                    expected_owner,
                    target_version or worker.identity.version,
                    observed,
                )
                async with supervisor.coordinator.database.session() as session:
                    handoff = await session.get(ProjectHandoffRow, owner.project_id)
                    revision = handoff.revision if handoff else None
                await self._recovery_operation(
                    expected_owner,
                    "recover_project",
                    {
                        "project_id": owner.project_id,
                        "target_runtime_id": target_runtime_id,
                        "expected_owner": expected_owner,
                        "expected_handoff_revision": revision,
                    },
                    stage=_attempt_stage("recover_project", attempt),
                )
                self._recovery_blocked.pop(owner.project_id, None)
                self._recovery_backoff_noted.pop(owner.project_id, None)
                await self._clear_hold(owner.project_id)
            except Exception as error:
                self._recovery_blocked[owner.project_id] = (
                    owner.runtime_id,
                    owner.fencing_token,
                    now + _RECOVERY_RETRY_MS,
                )
                record_caught_exception(
                    "runtime",
                    "runtime.project_recovery.waiting",
                    "This project remains held until its expired runtime can be safely recovered.",
                    error,
                    stage="recovery",
                )
