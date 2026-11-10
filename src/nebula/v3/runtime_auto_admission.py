"""First-use project admission and cold API recovery under the stable host."""

from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import func, or_, select

from .database import (
    DeploymentHostRow,
    DeploymentOperationRow,
    DeploymentReceiptRow,
    MemoryBudgetPolicyRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
)
from .diagnostics import record_caught_exception
from .domain import Engagement
from .runtime_coordination import CoordinationConflict
from .setup import bootstrap_scratch_project

_RECOVERY_ACTOR = "runtime-auto-recovery"
_RECOVERY_WINDOW_MS = 10 * 60_000
_RECOVERY_LIMIT = 3
_RECOVERY_RETRY_MS = 30_000


class RuntimeAutoAdmission:
    def __init__(self, supervisor, *, default_version: str):
        self.supervisor = supervisor
        self.default_version = default_version
        self.task = None
        self._pending: dict[str, str] = {}
        self._blocked: dict[str, tuple[int, int | None]] = {}
        self._recovery_blocked: dict[str, tuple[str, int, int]] = {}
        self._recovery_cursor: str | None = None
        self._serial = asyncio.Lock()

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
            async with supervisor.coordinator.database.session() as session:
                host = await session.get(DeploymentHostRow, "host")
                if host is None or host.shutdown_requested:
                    return
                policy = await session.get(MemoryBudgetPolicyRow, "host")
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
                demands = list(
                    await session.scalars(
                        select(ProjectOwnershipRow)
                        # A verified quiescence clears the owner while retaining
                        # its fencing history. Reopening may claim the next fence;
                        # expired or uncertain owners still have a runtime ID.
                        .where(
                            ProjectOwnershipRow.runtime_id.is_(None),
                            or_(
                                ProjectOwnershipRow.admission_requested.is_(True),
                                select(RuntimeCommandRow.id)
                                .where(
                                    RuntimeCommandRow.project_id
                                    == ProjectOwnershipRow.project_id,
                                    RuntimeCommandRow.name != "work.batch.barrier/v1",
                                    RuntimeCommandRow.state.in_(
                                        ("queued", "running", "uncertain")
                                    ),
                                )
                                .exists(),
                            ),
                        )
                        .limit(16)
                    )
                )
                api_id = host.api_runtime_id
            if api_id is None and self._blocked.get("api") != signature:
                try:
                    worker = await supervisor.prepare_api(api_version)
                    current = await supervisor.coordinator.status()
                    await supervisor.promote_api(
                        worker.runtime_id, expected_revision=current.revision
                    )
                    self._blocked.pop("api", None)
                except Exception as error:
                    self._blocked["api"] = signature
                    record_caught_exception(
                        "runtime",
                        "runtime.api_admission.waiting",
                        "Host recovery remains available while API admission is held.",
                        error,
                        stage="admission",
                    )
                return
            await self._recover_expired()
            for demand in demands:
                if self._blocked.get(demand.project_id) == signature:
                    continue
                try:
                    project = await supervisor.store.get(Engagement, demand.project_id)
                    if project.status == "archived":
                        continue
                    runtime_id = self._pending.get(project.id)
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
                        worker = await supervisor.prepare_execution(version)
                        runtime_id = worker.runtime_id
                        self._pending[project.id] = runtime_id
                    await supervisor.attach_execution(project.id, runtime_id)
                    self._pending.pop(project.id, None)
                    self._blocked.pop(project.id, None)
                except Exception as error:
                    self._blocked[demand.project_id] = signature
                    record_caught_exception(
                        "runtime",
                        "runtime.project_admission.waiting",
                        "This project's first work remains queued until admission succeeds.",
                        error,
                        stage="admission",
                    )

    @staticmethod
    def _recovery_operation_id(owner: dict, stage: str) -> str:
        digest = hashlib.sha256(
            json.dumps([owner, stage], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return "auto-recovery:" + digest

    async def _has_unassigned_execution(self) -> bool:
        async with self.supervisor.coordinator.database.session() as session:
            return (
                await session.scalar(
                    select(RuntimeProcessRow.runtime_id)
                    .where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state.in_(("reserved", "starting", "ready")),
                        ~RuntimeProcessRow.runtime_id.in_(
                            select(ProjectOwnershipRow.runtime_id).where(
                                ProjectOwnershipRow.runtime_id.is_not(None)
                            )
                        ),
                    )
                    .limit(1)
                )
                is not None
            )

    async def _recovery_operation(self, owner: dict, action: str, parameters: dict):
        management = self.supervisor.management
        operation_id = self._recovery_operation_id(owner, action)
        ledger = management.ledger()
        async with self.supervisor.coordinator.database.session() as session:
            prior = await session.get(DeploymentOperationRow, operation_id)
        if prior is not None:
            # Saved parameters and results are authoritative, including an
            # unknown spawn outcome after a host restart.
            receipt = await ledger.status(operation_id, actor_id=_RECOVERY_ACTOR)
        else:
            if action == "prepare_execution" and await self._has_unassigned_execution():
                raise CoordinationConflict(
                    "An unassigned execution process needs explicit reconciliation before automatic recovery"
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
            )
        if receipt["state"] != "succeeded":
            raise CoordinationConflict(
                f"Automatic recovery operation {operation_id} is {receipt['state']}; "
                "read its saved deployment result before reconciliation"
            )
        return receipt["result"]

    async def _recover_expired(self):
        supervisor = self.supervisor
        async with supervisor.coordinator.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            if host is None or host.shutdown_requested:
                return
            target_version = host.target_version
            now = await supervisor.coordinator.coordination._clock_ms(session)
            query = select(ProjectOwnershipRow).where(
                ProjectOwnershipRow.runtime_id.is_not(None),
                ProjectOwnershipRow.lease_expires_at_ms <= now,
            )
            if self._recovery_cursor is not None:
                query = query.where(
                    ProjectOwnershipRow.project_id > self._recovery_cursor
                )
            owners = list(
                await session.scalars(
                    query.order_by(ProjectOwnershipRow.project_id).limit(16)
                )
            )
        # This cursor only schedules bounded scans; operation receipts remain
        # the authority across controller or host restarts.
        self._recovery_cursor = owners[-1].project_id if owners else None
        if not owners:
            return
        # Older embedders without a process host cannot prove OS absence.
        if (
            getattr(supervisor, "host", None) is None
            or getattr(supervisor, "management", None) is None
        ):
            return
        observed = {
            worker.runtime_id: worker
            for worker in await supervisor.host.rediscover()
            if worker.process_state == "gone" and worker.identity is not None
        }
        for owner in owners:
            worker = observed.get(owner.runtime_id)
            if worker is None:
                continue
            blocked = self._recovery_blocked.get(owner.project_id)
            if blocked and blocked[:2] == (owner.runtime_id, owner.fencing_token):
                if blocked[2] > now:
                    continue
            try:
                project = await supervisor.store.get(Engagement, owner.project_id)
                if project.status == "archived":
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
                    attempts = await session.scalar(
                        select(func.count())
                        .select_from(RuntimeRecoveryRow)
                        .where(
                            RuntimeRecoveryRow.project_id == owner.project_id,
                            RuntimeRecoveryRow.created_at_ms
                            >= now - _RECOVERY_WINDOW_MS,
                        )
                    )
                if existing is not None:
                    continue
                if attempts >= _RECOVERY_LIMIT:
                    raise CoordinationConflict(
                        "Automatic recovery is held after three recoveries in ten minutes; inspect this project's runtime failures"
                    )
                await self._recovery_operation(
                    expected_owner,
                    "reconcile_project",
                    {"expected_owner": expected_owner},
                )
                prepared = await self._recovery_operation(
                    expected_owner,
                    "prepare_execution",
                    {"version": target_version or worker.identity.version},
                )
                target_runtime_id = prepared.get("runtime_id")
                if not isinstance(target_runtime_id, str) or not target_runtime_id:
                    raise CoordinationConflict(
                        "The saved recovery preparation has no confirmed runtime identity"
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
                )
                self._recovery_blocked.pop(owner.project_id, None)
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
