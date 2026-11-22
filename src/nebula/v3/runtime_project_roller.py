"""In-Core project roller: move resident projects onto the execution target.

Once ``target_execution`` names a version that differs from a project's
resident worker, the roller moves that project with the same verified handoff
the external deployer uses: prepare an unassigned target, request the
handoff, let the supervisor's handoff loop transfer and verify it, then retire
the ownerless source. It runs next to auto-admission in the supervisor and is
a reconciler: every pass reads ownership, handoff rows and processes from SQL
and acts only on what that read shows, so a Core restart mid-roll resumes
where SQL says the roll stands.

* **K at a time.** At most ``NEBULA_ROLL_PROJECTS_AT_ONCE`` (default 2)
  handoffs it requested are unfinished at once. ``0`` turns the roller off.
* **Pinned projects are never held.** A project whose open resources cannot
  move (a live harness session, a terminal, a running mission, ...) is skipped
  from SQL before any handoff is requested. A handoff whose source reports
  such a pin is withdrawn, and its prepared target is retargeted to the next
  project. A held handoff that has not transferred within
  ``NEBULA_ROLL_HELD_LIMIT_SECONDS`` is withdrawn too, so a conversation never
  waits on the roll. Each such project is retried later with backoff
  (``NEBULA_ROLL_PINNED_RETRY_SECONDS``, doubling to 15 minutes), without
  rerunning the external deployer. Nothing is ever evicted or interrupted.
* **Sources retire after verified handoff.** A source runtime is retired only
  once a handoff to one of the roller's own targets is verified, the target
  owns the project, and the source owns nothing.
* **It defers to an operator's deployer.** While an operator or device
  deployment operation is running, or ran within
  ``NEBULA_ROLL_OPERATOR_QUIET_SECONDS`` (default 600), the roller acts on
  nothing, so it never races the external deployer's journal.

Every effect is an ordinary deployment operation in the receipt ledger under
the ``core:roller`` actor, and the roller writes ``roll.*`` receipts that name
its targets, so a restarted Core recognises the targets it prepared before.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from sqlalchemy import or_, select

from .database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
    RuntimeResourceRow,
)
from .deployment_protocol import (
    BUTLER_HOME_FEATURE,
    BUTLER_HOME_PROJECT,
    HandoffPhase,
    ReleaseManifest,
    RollMovingProject,
    RollPinnedProject,
    RollStatus,
)
from .diagnostics import record_caught_exception, record_diagnostic

ROLLER_ACTOR = "core:roller"
HANDOFF_FEATURE = "project.handoff/v1"
TARGET_PREPARED = "roll.target_prepared"
HANDOFF_REQUESTED = "roll.handoff_requested"
HANDOFF_WITHDRAWN = "roll.handoff_withdrawn"
SOURCE_RETIRED = "roll.source_retired"
TARGET_RETIRED = "roll.target_retired"

DEFAULT_PROJECTS_AT_ONCE = 2
MAX_PROJECTS_AT_ONCE = 16
DEFAULT_PINNED_RETRY_SECONDS = 60
MAX_PINNED_RETRY_SECONDS = 15 * 60
DEFAULT_HELD_LIMIT_SECONDS = 300
DEFAULT_OPERATOR_QUIET_SECONDS = 600
PASS_SECONDS = 2.0
# A failed step (prepare, retire, withdraw) is asked again after this delay,
# doubling to STEP_RETRY_MAX_SECONDS, never on every pass.
STEP_RETRY_SECONDS = 5.0
STEP_RETRY_MAX_SECONDS = 300.0
# An operator operation still marked running after this long is treated as
# left behind by a stopped host, not as a deployer at work.
STALE_RUNNING_MS = 3600 * 1000
_BLOCKER_CHARS = 300
_PassState = Literal["rolling", "waiting", "blocked", "complete"]

_UNFINISHED = (
    HandoffPhase.REQUESTED,
    HandoffPhase.FINISHING,
    HandoffPhase.TRANSFERABLE,
    HandoffPhase.TRANSFERRED,
)
_WITHDRAWABLE = (HandoffPhase.REQUESTED, HandoffPhase.FINISHING)
# Bounded lists keep deployment status small on a host with many projects.
_STATUS_ITEMS = 20
_TARGET_SCAN = 500


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        # diagnostic-expected: a malformed roll setting falls back to its documented default.
        return default
    return max(minimum, min(maximum, value))


def roll_projects_at_once() -> int:
    return _bounded_env(
        "NEBULA_ROLL_PROJECTS_AT_ONCE",
        DEFAULT_PROJECTS_AT_ONCE,
        0,
        MAX_PROJECTS_AT_ONCE,
    )


def roll_pinned_retry_seconds() -> int:
    return _bounded_env(
        "NEBULA_ROLL_PINNED_RETRY_SECONDS",
        DEFAULT_PINNED_RETRY_SECONDS,
        5,
        MAX_PINNED_RETRY_SECONDS,
    )


def roll_held_limit_seconds() -> int:
    return _bounded_env(
        "NEBULA_ROLL_HELD_LIMIT_SECONDS", DEFAULT_HELD_LIMIT_SECONDS, 30, 3600
    )


def roll_operator_quiet_seconds() -> int:
    return _bounded_env(
        "NEBULA_ROLL_OPERATOR_QUIET_SECONDS",
        DEFAULT_OPERATOR_QUIET_SECONDS,
        0,
        24 * 3600,
    )


class RollStepFailed(Exception):
    """A roll step was refused or did not confirm its effect."""


class LedgerRollActions:
    """Run each roll step as an ordinary deployment operation.

    The operation goes through the same receipt ledger and dispatch as an
    authenticated ``POST /api/v1/deployment/operations``, under the
    ``core:roller`` actor, so its receipt, its crash semantics and the
    supervisor's checks are the deployer's.
    """

    def __init__(self, supervisor):
        self.supervisor = supervisor

    async def _run(self, action: str, parameters: dict) -> dict:
        management = self.supervisor.management
        operation_id = "roll-" + action.replace("_", "-") + "-" + uuid4().hex

        async def dispatch(name, values):
            return await management.dispatch(name, values, operation_id, ROLLER_ACTOR)

        receipt = await management.ledger().execute(
            operation_id=operation_id,
            actor_id=ROLLER_ACTOR,
            action=action,
            parameters=parameters,
            dispatch=dispatch,
        )
        if receipt.get("state") != "succeeded":
            result = receipt.get("result") or {}
            raise RollStepFailed(
                f"{action} {receipt.get('state')}: "
                + str(result.get("reason") or result.get("detail") or "no detail")[
                    :1000
                ]
            )
        return receipt.get("result") or {}

    async def prepare(self, version: str) -> str:
        result = await self._run("prepare_execution", {"version": version})
        runtime_id = result.get("runtime_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise RollStepFailed("prepare_execution omitted its runtime ID")
        return runtime_id

    async def handoff(self, project_id: str, target_runtime_id: str) -> None:
        await self._run(
            "handoff",
            {"project_id": project_id, "target_runtime_id": target_runtime_id},
        )

    async def retire(self, runtime_id: str) -> None:
        await self._run("retire", {"runtime_id": runtime_id})

    async def withdraw(
        self, project_id: str, target_runtime_id: str, revision: int
    ) -> bool:
        # A compare-and-cancel in one transaction; it never starts a handoff.
        return await self.supervisor.coordinator.withdraw_handoff(
            project_id, target_runtime_id=target_runtime_id, revision=revision
        )


@dataclass
class _Backoff:
    attempts: int
    retry_at: float
    blockers: list[str]
    runtime_version: str | None


@dataclass
class _Snapshot:
    now_ms: int
    target: str | None
    shutdown: bool
    operator_busy: str | None
    # project -> (runtime_id, retirement_requested, lease live)
    owners: dict[str, tuple[str | None, bool, bool]]
    # runtime -> (version, role, state)
    processes: dict[str, tuple[str, str, str]]
    handoffs: dict[str, ProjectHandoffRow]
    manifests: dict[str, ReleaseManifest]
    own_targets: set[str]
    owner_runtimes: set[str] = field(default_factory=set)
    unfinished_targets: set[str] = field(default_factory=set)


class RuntimeProjectRoller:
    def __init__(
        self,
        coordinator,
        actions,
        *,
        projects_at_once: int | None = None,
        pinned_retry_seconds: int | None = None,
        held_limit_seconds: int | None = None,
        operator_quiet_seconds: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.coordinator = coordinator
        self.actions = actions
        self.projects_at_once = (
            roll_projects_at_once() if projects_at_once is None else projects_at_once
        )
        self.pinned_retry_seconds = (
            roll_pinned_retry_seconds()
            if pinned_retry_seconds is None
            else pinned_retry_seconds
        )
        self.held_limit_seconds = (
            roll_held_limit_seconds()
            if held_limit_seconds is None
            else held_limit_seconds
        )
        self.operator_quiet_seconds = (
            roll_operator_quiet_seconds()
            if operator_quiet_seconds is None
            else operator_quiet_seconds
        )
        self.clock = clock
        self.task: asyncio.Task | None = None
        self._serial = asyncio.Lock()
        self._backoff: dict[str, _Backoff] = {}
        # When this roller first saw each of its handoffs held, keyed by
        # (project, target); a restart starts the held limit again.
        self._held_since: dict[tuple[str, str], float] = {}
        self._retired_sources = 0
        self._noted: dict[str, str] = {}
        self._completed_target: str | None = None
        self._manifests: dict[str, ReleaseManifest] = {}
        # Failed steps by key: (failures, retry at on self.clock).
        self._step_retry: dict[str, tuple[int, float]] = {}
        self._status = RollStatus(
            state="disabled" if self.projects_at_once == 0 else "idle",
            detail="The roller has not run a pass yet.",
            projects_at_once=self.projects_at_once,
        )

    @classmethod
    def for_supervisor(cls, supervisor, **options) -> "RuntimeProjectRoller":
        roller = cls(supervisor.coordinator, LedgerRollActions(supervisor), **options)
        supervisor.coordinator.roller = roller
        return roller

    def status(self) -> RollStatus:
        return self._status

    async def start(self) -> None:
        if self.task is not None or self.projects_at_once == 0:
            return
        # diagnostic-expected: the owned loop records pass failures and is cancelled and joined on close.
        self.task = asyncio.create_task(self._run(), name="nebula-project-roller")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if getattr(self.coordinator, "roller", None) is self:
            self.coordinator.roller = None

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
                self._note_failure("pass", "The project roller pass failed", error)
            await asyncio.sleep(PASS_SECONDS)

    # -- one pass ---------------------------------------------------------

    async def reconcile(self) -> RollStatus:
        async with self._serial:
            if self.projects_at_once == 0:
                self._status = RollStatus(
                    state="disabled",
                    detail="NEBULA_ROLL_PROJECTS_AT_ONCE is 0; the external deployer moves projects.",
                    projects_at_once=0,
                )
                return self._status
            snapshot = await self._read()
            if snapshot.shutdown or snapshot.target is None:
                self._status = RollStatus(
                    state="idle",
                    detail="Core is shutting down."
                    if snapshot.shutdown
                    else "No execution target is set.",
                    target_version=snapshot.target,
                    projects_at_once=self.projects_at_once,
                    updated_at_ms=snapshot.now_ms,
                )
                return self._status
            if snapshot.operator_busy is not None:
                # Start nothing new, but never leave the roller's own
                # handoffs held or its targets and verified sources behind.
                blockers: list[str] = []
                await self._retire_verified_sources(snapshot, blockers)
                moving = await self._settle_own_handoffs(snapshot, blockers)
                await self._retire_unused_targets(snapshot, blockers)
                self._status = RollStatus(
                    state="deferred",
                    detail=snapshot.operator_busy,
                    target_version=snapshot.target,
                    projects_at_once=self.projects_at_once,
                    remaining=len(self._stale_projects(snapshot)),
                    moving=moving[:_STATUS_ITEMS],
                    pinned=self._pinned_status(),
                    blockers=list(dict.fromkeys(blockers))[:10],
                    retired_sources=self._retired_sources,
                    updated_at_ms=snapshot.now_ms,
                )
                return self._status
            return await self._roll(snapshot)

    async def _read(self) -> _Snapshot:
        async with self.coordinator.database.session() as session:
            now_ms = await self.coordinator.coordination._clock_ms(session)
            host = await session.get(DeploymentHostRow, "host")
            owners = {
                row.project_id: (
                    row.runtime_id,
                    bool(row.retirement_requested),
                    row.lease_expires_at_ms > now_ms,
                )
                for row in await session.scalars(select(ProjectOwnershipRow))
            }
            processes = {
                row.runtime_id: (row.version, row.role, row.state)
                for row in await session.scalars(
                    select(RuntimeProcessRow).where(
                        RuntimeProcessRow.state != "retired"
                    )
                )
            }
            target = host.target_version if host is not None else None
            owner_runtimes = {
                runtime_id for runtime_id, _r, _l in owners.values() if runtime_id
            }
            # Steady state: every execution worker is owned and runs the
            # target. No project can be stale, no handoff target or verified
            # source of the roller's can be live, so the pass reads no more.
            if target is None or all(
                role != "execution"
                or (version == target and runtime_id in owner_runtimes)
                for runtime_id, (version, role, _state) in processes.items()
            ):
                return _Snapshot(
                    now_ms=now_ms,
                    target=target,
                    shutdown=bool(host is not None and host.shutdown_requested),
                    operator_busy=None,
                    owners=owners,
                    processes=processes,
                    handoffs={},
                    manifests=self._manifests,
                    own_targets=set(),
                    owner_runtimes=owner_runtimes,
                )
            handoffs = {
                row.project_id: row
                for row in await session.scalars(select(ProjectHandoffRow))
            }
            for release in await session.scalars(
                select(RuntimeReleaseRow).where(
                    RuntimeReleaseRow.version.not_in(list(self._manifests))
                )
            ):
                try:
                    # A staged release's manifest never changes.
                    self._manifests[release.version] = ReleaseManifest.model_validate(
                        release.manifest
                    )
                except ValueError:
                    # diagnostic-expected: a release whose manifest this version cannot read takes no roll; its projects report the missing handoff support.
                    continue
            own_targets: set[str] = set()
            for receipt in await session.scalars(
                select(DeploymentReceiptRow)
                .where(DeploymentReceiptRow.action == TARGET_PREPARED)
                .order_by(DeploymentReceiptRow.occurred_at_ms.desc())
                .limit(_TARGET_SCAN)
            ):
                runtime_id = (receipt.result or {}).get("runtime_id")
                if isinstance(runtime_id, str):
                    own_targets.add(runtime_id)
            # A preparation whose host stopped before the roller saved its
            # receipt is still named by its operation's spawn checkpoint.
            for operation in await session.scalars(
                select(DeploymentOperationRow)
                .where(
                    DeploymentOperationRow.actor_id == ROLLER_ACTOR,
                    DeploymentOperationRow.action == "prepare_execution",
                )
                .order_by(DeploymentOperationRow.updated_at_ms.desc())
                .limit(_TARGET_SCAN)
            ):
                result = operation.result or {}
                for runtime_id in (
                    result.get("runtime_id"),
                    (result.get("recovery") or {}).get("runtime_id"),
                ):
                    if isinstance(runtime_id, str):
                        own_targets.add(runtime_id)
            operator_busy = None
            if self.operator_quiet_seconds > 0 and host is not None:
                recent = await session.scalar(
                    select(DeploymentOperationRow)
                    .where(
                        or_(
                            DeploymentOperationRow.actor_id == "operator",
                            DeploymentOperationRow.actor_id.startswith("device:"),
                        ),
                        DeploymentOperationRow.updated_at_ms
                        > now_ms
                        - max(
                            self.operator_quiet_seconds * 1000,
                            STALE_RUNNING_MS,
                        ),
                        or_(
                            DeploymentOperationRow.state == "running",
                            DeploymentOperationRow.updated_at_ms
                            > now_ms - self.operator_quiet_seconds * 1000,
                        ),
                    )
                    .order_by(DeploymentOperationRow.updated_at_ms.desc())
                    .limit(1)
                )
                if recent is not None:
                    operator_busy = (
                        f"An operator deployment operation ({recent.action}) "
                        f"ran within the last {self.operator_quiet_seconds} s; "
                        "the roller waits so it never races the deployer."
                    )
        snapshot = _Snapshot(
            now_ms=now_ms,
            target=target,
            shutdown=bool(host is not None and host.shutdown_requested),
            operator_busy=operator_busy,
            owners=owners,
            processes=processes,
            handoffs=handoffs,
            manifests=self._manifests,
            own_targets=own_targets,
            owner_runtimes=owner_runtimes,
        )
        snapshot.unfinished_targets = {
            row.target_runtime_id
            for row in handoffs.values()
            if row.phase in _UNFINISHED
        }
        return snapshot

    def _stale_projects(self, snapshot: _Snapshot) -> list[str]:
        """Projects whose live worker runs a version other than the target."""
        stale = []
        for project_id, (runtime_id, _retiring, _live) in sorted(
            snapshot.owners.items()
        ):
            process = snapshot.processes.get(runtime_id) if runtime_id else None
            if (
                process is not None
                and process[1] == "execution"
                and process[0] != snapshot.target
            ):
                stale.append(project_id)
        return stale

    async def _roll(self, snapshot: _Snapshot) -> RollStatus:
        target = snapshot.target
        assert target is not None
        blockers: list[str] = []
        manifest = snapshot.manifests.get(target)
        await self._retire_verified_sources(snapshot, blockers)
        moving = await self._settle_own_handoffs(snapshot, blockers)
        stale = self._stale_projects(snapshot)
        if manifest is None or HANDOFF_FEATURE not in manifest.features:
            blockers.insert(
                0,
                f"Execution target {target} does not declare {HANDOFF_FEATURE}; "
                "its projects move only through idle retirement or recovery.",
            )
            await self._retire_unused_targets(snapshot, blockers)
            return self._publish(snapshot, "blocked", stale, moving, blockers)
        unused = self._unused_targets(snapshot)
        free = max(0, self.projects_at_once - len(moving))
        now = self.clock()
        candidates = []
        for project_id in stale:
            runtime_id, retiring, live = snapshot.owners[project_id]
            handoff = snapshot.handoffs.get(project_id)
            if handoff is not None and handoff.phase in _UNFINISHED:
                continue
            if retiring or not live:
                # Idle retirement or expired-owner recovery owns this project;
                # both put it on the target without a handoff.
                continue
            backoff = self._backoff.get(project_id)
            if backoff is not None and backoff.retry_at > now:
                continue
            candidates.append(project_id)
        # Projects never tried go before retries of pinned ones.
        candidates.sort(key=lambda item: (item in self._backoff, item))
        capacity_blocked = False
        for project_id in candidates:
            if free <= 0 or capacity_blocked:
                break
            runtime_id, _retiring, _live = snapshot.owners[project_id]
            assert runtime_id is not None
            source_version = snapshot.processes[runtime_id][0]
            source_manifest = snapshot.manifests.get(source_version)
            if source_manifest is None or HANDOFF_FEATURE not in (
                source_manifest.features
            ):
                self._defer(
                    project_id,
                    source_version,
                    [
                        f"Its worker's release {source_version} cannot take a verified handoff; "
                        "the project moves when it goes idle or is recovered."
                    ],
                )
                continue
            if (
                project_id == BUTLER_HOME_PROJECT
                and BUTLER_HOME_FEATURE not in manifest.features
            ):
                self._defer(
                    project_id,
                    source_version,
                    [f"Execution target {target} cannot host Butler's home."],
                )
                continue
            pins = await self._known_pins(project_id, manifest)
            if pins:
                # Never hold a project SQL already shows cannot move.
                self._defer(project_id, source_version, pins)
                continue
            target_id = unused.pop(0) if unused else None
            if target_id is None:
                if not self._step_ready("prepare"):
                    blockers.append(
                        self._noted_detail("prepare")
                        or f"Preparing a {target} execution worker is retried shortly."
                    )
                    capacity_blocked = True
                    break
                try:
                    target_id = await self.actions.prepare(target)
                except Exception as error:
                    # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
                    self._step_failed("prepare")
                    detail = self._note_failure(
                        "prepare",
                        "The roller could not prepare an execution target",
                        error,
                    )
                    blockers.append(
                        f"Preparing a {target} execution worker was refused: {detail}"
                    )
                    capacity_blocked = True
                    break
                self._step_ok("prepare")
                await self._record(
                    TARGET_PREPARED, {"runtime_id": target_id, "version": target}
                )
                snapshot.own_targets.add(target_id)
            try:
                await self.actions.handoff(project_id, target_id)
            except Exception as error:
                # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
                detail = self._note_failure(
                    "handoff:" + project_id,
                    "The roller's handoff request was refused",
                    error,
                )
                self._defer(project_id, source_version, [detail])
                # The target never took the project; give it to the next one.
                unused.insert(0, target_id)
                continue
            await self._record(
                HANDOFF_REQUESTED,
                {
                    "project_id": project_id,
                    "source_runtime_id": runtime_id,
                    "target_runtime_id": target_id,
                    "target_version": target,
                },
            )
            snapshot.unfinished_targets.add(target_id)
            self._held_since[(project_id, target_id)] = self.clock()
            moving.append(
                RollMovingProject(
                    project_id=project_id,
                    phase=HandoffPhase.REQUESTED,
                    target_runtime_id=target_id,
                    admission_held=True,
                )
            )
            free -= 1
        # Targets no project took this pass are not kept as spares.
        await self._retire_unused_targets(snapshot, blockers)
        state: _PassState
        if moving:
            state = "rolling"
        elif capacity_blocked:
            state = "blocked"
        elif any(item not in self._backoff for item in stale):
            # Waiting for a slot, another handoff, idle retirement or recovery.
            state = "rolling"
        elif stale:
            state = "waiting"
        else:
            state = "complete"
        return self._publish(snapshot, state, stale, moving, blockers)

    # -- steps ------------------------------------------------------------

    async def _known_pins(
        self, project_id: str, manifest: ReleaseManifest
    ) -> list[str]:
        """Open resources that SQL already shows the target cannot restore.

        This mirrors the pin rule of the source's handoff checkpoint, so a
        project with a live harness session, terminal or running mission is
        never held just to learn it cannot move.
        """
        supported = set(manifest.restorable_resources)
        pins: list[str] = []
        async with self.coordinator.database.session() as session:
            for resource in await session.scalars(
                select(RuntimeResourceRow)
                .where(
                    RuntimeResourceRow.project_id == project_id,
                    RuntimeResourceRow.state != "closed",
                )
                .limit(200)
            ):
                finishing_provider = (
                    resource.kind == "provider_turn"
                    and resource.state == "active"
                    and resource.restoration == "durable"
                )
                if (
                    resource.restoration == "pinned"
                    or resource.state == "uncertain"
                    or (resource.kind not in supported and not finishing_provider)
                ):
                    pins.append(
                        resource.detail
                        or f"{resource.kind} {resource.id} cannot be restored by the target"
                    )
        return list(dict.fromkeys(pins))[:5]

    async def _settle_own_handoffs(
        self, snapshot: _Snapshot, blockers: list[str]
    ) -> list[RollMovingProject]:
        """Count this roller's unfinished handoffs; withdraw pinned or stuck ones."""
        moving: list[RollMovingProject] = []
        now = self.clock()
        live_keys = set()
        for project_id, row in sorted(snapshot.handoffs.items()):
            if row.phase not in _UNFINISHED or row.target_runtime_id not in (
                snapshot.own_targets
            ):
                continue
            key = (project_id, row.target_runtime_id)
            live_keys.add(key)
            pinned = row.phase == HandoffPhase.FINISHING and not row.admission_held
            held_since = self._held_since.setdefault(key, now)
            stuck = (
                row.phase in _WITHDRAWABLE
                and row.admission_held
                and now - held_since >= self.held_limit_seconds
            )
            if pinned or stuck:
                reasons = list(row.blockers or [])[:5]
                if stuck:
                    reasons.insert(
                        0,
                        f"The handoff held new turns for {self.held_limit_seconds} s "
                        "without transferring; it was withdrawn so the project keeps working.",
                    )
                if await self._withdraw(snapshot, project_id, row, reasons):
                    continue
            moving.append(
                RollMovingProject(
                    project_id=project_id,
                    phase=HandoffPhase(row.phase),
                    target_runtime_id=row.target_runtime_id,
                    admission_held=bool(row.admission_held),
                )
            )
            if row.blockers and row.admission_held:
                blockers.extend(
                    f"{project_id}: {item}" for item in list(row.blockers)[:2]
                )
        for key in list(self._held_since):
            if key not in live_keys:
                self._held_since.pop(key, None)
        return moving

    async def _withdraw(
        self, snapshot: _Snapshot, project_id: str, row, reasons: list[str]
    ) -> bool:
        source_id = row.source_runtime_id
        owner = snapshot.owners.get(project_id)
        key = "withdraw:" + project_id
        if (
            owner is None
            or owner[0] != source_id
            or not owner[2]
            or not self._step_ready(key)
        ):
            # An expired source belongs to expired-owner recovery.
            return False
        try:
            withdrawn = await self.actions.withdraw(
                project_id, row.target_runtime_id, row.revision
            )
        except Exception as error:
            # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
            self._step_failed(key)
            self._note_failure(
                key, "The roller could not withdraw a pinned handoff", error
            )
            return False
        self._step_ok(key)
        if not withdrawn:
            # The handoff moved on (for example it transferred) since this
            # pass read it; the next pass reads it again.
            return False
        source = snapshot.processes.get(source_id)
        self._defer(project_id, source[0] if source else None, reasons)
        self._held_since.pop((project_id, row.target_runtime_id), None)
        # The target took nothing; it is now free for the next project.
        snapshot.unfinished_targets.discard(row.target_runtime_id)
        await self._record(
            HANDOFF_WITHDRAWN,
            {
                "project_id": project_id,
                "source_runtime_id": source_id,
                "target_runtime_id": row.target_runtime_id,
                "blockers": reasons[:5],
            },
        )
        record_diagnostic(
            "info",
            "runtime",
            "runtime.roll.handoff_withdrawn",
            "A project stays on its current version until its open work can move.",
            project_id=project_id,
            stage="deployment",
            outcome="deferred",
            retryable=True,
            metadata={"entity_id": project_id, "kind": "project"},
        )
        return True

    def _unused_targets(self, snapshot: _Snapshot) -> list[str]:
        """This roller's ready targets that no project or handoff holds."""
        unused = []
        for runtime_id in sorted(snapshot.own_targets):
            process = snapshot.processes.get(runtime_id)
            if (
                process is not None
                and process[1] == "execution"
                and process[2] == "ready"
                and process[0] == snapshot.target
                and runtime_id not in snapshot.owner_runtimes
                and runtime_id not in snapshot.unfinished_targets
            ):
                unused.append(runtime_id)
        return unused

    async def _retire_unused_targets(
        self, snapshot: _Snapshot, blockers: list[str]
    ) -> None:
        """Retire prepared targets no project took.

        An unassigned worker holds memory and makes auto-admission wait for
        explicit reconciliation, so the roller never keeps a spare one.
        """
        # A worker that was the source of a handoff retires only through
        # the verified-handoff path, never as a spare target.
        sources = {row.source_runtime_id for row in snapshot.handoffs.values()}
        for runtime_id in sorted(snapshot.own_targets):
            process = snapshot.processes.get(runtime_id)
            key = "retire:" + runtime_id
            if (
                process is None
                or process[1] != "execution"
                or process[2] != "ready"
                or runtime_id in snapshot.owner_runtimes
                or runtime_id in snapshot.unfinished_targets
                or runtime_id in sources
                or not self._step_ready(key)
            ):
                continue
            try:
                await self.actions.retire(runtime_id)
            except Exception as error:
                # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
                self._step_failed(key)
                detail = self._note_failure(
                    "retire-target:" + runtime_id,
                    "The roller could not retire an unused execution target",
                    error,
                )
                blockers.append(
                    f"Unused target {runtime_id} is not retired yet: {detail}"
                )
                continue
            self._step_ok(key)
            snapshot.processes.pop(runtime_id, None)
            await self._record(TARGET_RETIRED, {"runtime_id": runtime_id})

    async def _retire_verified_sources(
        self, snapshot: _Snapshot, blockers: list[str]
    ) -> None:
        sources = set()
        for project_id, row in snapshot.handoffs.items():
            owner = snapshot.owners.get(project_id)
            if (
                row.phase == HandoffPhase.VERIFIED
                and row.target_runtime_id in snapshot.own_targets
                and owner is not None
                and owner[0] == row.target_runtime_id
                and row.source_runtime_id != row.target_runtime_id
            ):
                sources.add(row.source_runtime_id)
        for source_id in sorted(sources):
            process = snapshot.processes.get(source_id)
            if (
                process is None
                or process[1] != "execution"
                or source_id in snapshot.owner_runtimes
                or source_id in snapshot.unfinished_targets
                or not self._step_ready("retire:" + source_id)
            ):
                continue
            try:
                await self.actions.retire(source_id)
            except Exception as error:
                # diagnostic-expected: _note_failure records it with record_caught_exception once per distinct failure; the step is retried later.
                self._step_failed("retire:" + source_id)
                detail = self._note_failure(
                    "retire-source:" + source_id,
                    "The roller could not retire a handed-off source yet",
                    error,
                )
                blockers.append(f"Source {source_id} is not retired yet: {detail}")
                continue
            self._step_ok("retire:" + source_id)
            snapshot.processes.pop(source_id, None)
            self._retired_sources += 1
            await self._record(
                SOURCE_RETIRED, {"runtime_id": source_id, "version": process[0]}
            )

    # -- bookkeeping ------------------------------------------------------

    def _defer(
        self, project_id: str, runtime_version: str | None, blockers: list[str]
    ) -> None:
        previous = self._backoff.get(project_id)
        attempts = (previous.attempts if previous is not None else 0) + 1
        delay = min(
            MAX_PINNED_RETRY_SECONDS,
            self.pinned_retry_seconds * 2 ** min(attempts - 1, 10),
        )
        self._backoff[project_id] = _Backoff(
            attempts=attempts,
            retry_at=self.clock() + delay,
            blockers=[item[:_BLOCKER_CHARS] for item in blockers[:5]],
            runtime_version=runtime_version,
        )

    def _step_ready(self, key: str) -> bool:
        retry = self._step_retry.get(key)
        return retry is None or retry[1] <= self.clock()

    def _step_failed(self, key: str) -> None:
        failures = self._step_retry.get(key, (0, 0.0))[0] + 1
        delay = min(
            STEP_RETRY_MAX_SECONDS,
            STEP_RETRY_SECONDS * 2 ** min(failures - 1, 10),
        )
        self._step_retry[key] = (failures, self.clock() + delay)

    def _step_ok(self, key: str) -> None:
        self._step_retry.pop(key, None)
        self._noted.pop(key, None)

    def _noted_detail(self, key: str) -> str | None:
        noted = self._noted.get(key)
        return noted.split(":", 1)[1] if noted else None

    async def _record(self, action: str, result: dict) -> None:
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._receipt(session, action, result)

    def _note_failure(self, key: str, message: str, error: BaseException) -> str:
        detail = (str(error) or type(error).__name__)[:1000]
        signature = type(error).__name__ + ":" + detail
        if self._noted.get(key) != signature:
            self._noted[key] = signature
            record_caught_exception(
                "runtime",
                "runtime.roll.step_waiting",
                message + "; it is retried.",
                error,
                stage="deployment",
            )
        return detail

    def _pinned_status(self) -> list[RollPinnedProject]:
        now = self.clock()
        return [
            RollPinnedProject(
                project_id=project_id,
                runtime_version=backoff.runtime_version,
                blockers=backoff.blockers,
                retry_in_seconds=max(0, int(backoff.retry_at - now + 0.999)),
                attempts=backoff.attempts,
            )
            for project_id, backoff in sorted(self._backoff.items())
        ][:_STATUS_ITEMS]

    def _publish(
        self,
        snapshot: _Snapshot,
        state: _PassState,
        stale: list[str],
        moving: list[RollMovingProject],
        blockers: list[str],
    ) -> RollStatus:
        stale_set = set(stale)
        for project_id in list(self._backoff):
            if project_id not in stale_set:
                # Moved by the roll, idle retirement or recovery.
                self._backoff.pop(project_id, None)
        pinned = self._pinned_status()
        details = {
            "rolling": f"Moving projects onto {snapshot.target}, "
            f"{self.projects_at_once} at a time.",
            "waiting": "Only pinned projects remain on older versions; each is retried "
            "when its open work may have ended. Their conversations keep running.",
            "blocked": "The roll cannot move projects right now.",
            "complete": f"Every resident project runs {snapshot.target}.",
        }
        if state == "complete" and self._completed_target != snapshot.target:
            self._completed_target = snapshot.target
            record_diagnostic(
                "info",
                "runtime",
                "runtime.roll.complete",
                f"Every resident project now runs {snapshot.target}.",
                stage="deployment",
                outcome="success",
                metadata={"version": snapshot.target},
            )
        elif state != "complete":
            self._completed_target = None
        self._status = RollStatus(
            state=state,
            detail=blockers[0] if state == "blocked" and blockers else details[state],
            target_version=snapshot.target,
            projects_at_once=self.projects_at_once,
            remaining=len(stale),
            moving=moving[:_STATUS_ITEMS],
            pinned=pinned,
            blockers=list(dict.fromkeys(blockers))[:10],
            retired_sources=self._retired_sources,
            updated_at_ms=snapshot.now_ms,
        )
        return self._status
