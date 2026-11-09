"""First-use project admission and cold API recovery under the stable host."""

from __future__ import annotations

import asyncio

from sqlalchemy import or_, select

from .database import (
    DeploymentHostRow,
    DeploymentReceiptRow,
    MemoryBudgetPolicyRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
)
from .diagnostics import record_caught_exception
from .domain import Engagement
from .setup import bootstrap_scratch_project


class RuntimeAutoAdmission:
    def __init__(self, supervisor, *, default_version: str):
        self.supervisor = supervisor
        self.default_version = default_version
        self.task = None
        self._pending: dict[str, str] = {}
        self._blocked: dict[str, tuple[int, int | None]] = {}
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
