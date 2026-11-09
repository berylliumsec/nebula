"""Project-bound access to the shared memory ledger from an execution role."""

from contextlib import asynccontextmanager

from sqlalchemy import select

from .database import MemoryJobRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .execution_scope import current_execution_lease
from .memory_budget import MemoryBudgetService, _HELD, _job
from .memory_policy import MemoryBudgetError
from .runtime_coordination import LeaseLost


class ExecutionMemoryBudget(MemoryBudgetService):
    def __init__(self, coordination, lease, **kwargs):
        super().__init__(coordination.database, **kwargs)
        self.coordination, self.lease = coordination, lease

    @asynccontextmanager
    async def _write(self):
        if current_execution_lease() != self.lease:
            raise LeaseLost("Memory changes require the bound project task")
        # The ownership check, policy/job mutation and final check share the
        # same transaction, including the PostgreSQL row lock. No check-then-
        # write gap allows a retired incarnation to start or free a job.
        async with self.coordination.fenced(self.lease) as session:
            yield session

    def _project(self, project_id):
        if (
            self.lease.project_id != GLOBAL_EXECUTION_PROJECT
            and project_id != self.lease.project_id
        ):
            raise LeaseLost("Memory job belongs to another project")

    async def _get(self, session, job_id, expected_revision=None):
        row = await super()._get(session, job_id, expected_revision)
        self._project(row.project_id)
        return row

    async def create_job(self, job_id, *, project_id, **kwargs):
        self._project(project_id)
        return await super().create_job(job_id, project_id=project_id, **kwargs)

    async def save(self, *args, **kwargs):
        if self.lease.project_id != GLOBAL_EXECUTION_PROJECT:
            raise LeaseLost("Host policy changes require the host control project")
        return await super().save(*args, **kwargs)

    async def reconciliation_jobs(self, *, after="", limit=64):
        if not 1 <= limit <= 256:
            raise MemoryBudgetError(
                "reconciliation page size must be between 1 and 256"
            )
        # A project worker must never reconcile another healthy worker's jobs.
        # Global orphan reconciliation belongs to the supervisor, independently
        # of the per-project execution lifecycle.
        async with self.database.session() as session:
            return [
                _job(row)
                for row in await session.scalars(
                    select(MemoryJobRow)
                    .where(
                        MemoryJobRow.project_id == self.lease.project_id,
                        MemoryJobRow.state.in_(_HELD),
                        MemoryJobRow.id > after,
                        MemoryJobRow.launch_owner.is_not(None),
                    )
                    .order_by(MemoryJobRow.id)
                    .limit(limit)
                )
            ]
