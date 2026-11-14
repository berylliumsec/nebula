"""Host-wide SQL memory policy, reservations and verified application records.

Only the host service exposes these operations to authenticated controllers.
Reservations span every execution generation. A missing worker is not proof
that its descendants died: capacity stays charged until the runner observes an
empty, correctly identified group. A durably never-launched reservation instead
requires exact cancellation authority or positive death of its recorded owner.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
import os
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import (
    Database,
    MemoryBudgetAuditRow,
    MemoryBudgetPolicyRow,
    MemoryJobRow,
)
from .domain import NebulaModel, utc_now
from .memory_host import cgroup_memory_capacity
from .memory_policy import (
    GIB,
    MIB,
    EffectiveMemoryPolicy,
    MemoryAdministratorLimits,
    MemoryBudgetConflict,
    MemoryBudgetError,
    MemoryBudgetPolicy,
    MemoryBudgetUpdate,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryLaunchOwner,
    MemoryLaunchGroups,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
    require_memory_enforcement,
    select_memory_policy,
    validate_policy,
    verified_memory_outcome,
)

POLICY_SCOPE = "host"
_HELD = ("reserved", "starting", "running", "updating", "uncertain")


class MemoryJob(NebulaModel):
    id: str
    project_id: str | None
    resource_kind: str
    resource_id: str
    revision: int
    state: str
    reserved_bytes: int
    snapshot: EffectiveMemoryPolicy
    evidence: MemoryEnforcementEvidence | None
    launch_owner: MemoryLaunchOwner | None = None
    launch_groups: MemoryLaunchGroups | None = None
    pending_limits: MemoryLimits | None
    application_state: str
    outcome: str | None
    detail: str | None
    created_at: datetime
    updated_at: datetime


class MemoryBudgetStatus(NebulaModel):
    revision: int
    policy: MemoryBudgetPolicy
    administrator_limits: MemoryAdministratorLimits
    capabilities: list[MemoryRunnerCapabilities]
    reserved_bytes: int
    available_bytes: int
    effective_aggregate_bytes: int
    held_jobs: int
    queued_jobs: int
    application_state: str
    detail: str
    runtime_reserved_bytes: int = 0
    minimum_core_reserve_bytes: int = 0


def administrator_memory_limits() -> MemoryAdministratorLimits:
    capacity = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    # A containerized Core must reserve against its own hard capacity, not the
    # physical host's larger /proc value. No available-memory sample is a limit.
    hard_limit = cgroup_memory_capacity()
    if hard_limit is not None:
        capacity = min(capacity, hard_limit)
    reserve = min(2 * GIB, max(256 * MIB, capacity // 4))
    reserve = int(os.environ.get("NEBULA_MEMORY_MIN_RESERVE_BYTES", reserve))
    aggregate = min(
        capacity - reserve,
        int(os.environ.get("NEBULA_MEMORY_MAX_AGGREGATE_BYTES", capacity - reserve)),
    )
    maximum_job = min(
        aggregate, int(os.environ.get("NEBULA_MEMORY_MAX_JOB_BYTES", aggregate))
    )
    maximum_swap = int(os.environ.get("NEBULA_MEMORY_MAX_SWAP_BYTES", 0))
    return MemoryAdministratorLimits(
        capacity_bytes=capacity,
        maximum_job_bytes=maximum_job,
        maximum_aggregate_bytes=aggregate,
        minimum_reserve_bytes=reserve,
        maximum_swap_bytes=maximum_swap,
    )


def _job(row: MemoryJobRow) -> MemoryJob:
    return MemoryJob.model_validate(
        {
            column.key: getattr(row, column.key)
            for column in MemoryJobRow.__table__.columns
        }
    )


def _charge(job, limits: MemoryLimits) -> int:
    return (
        limits.reservation_bytes
        + EffectiveMemoryPolicy.model_validate(job.snapshot).runner_overhead_bytes
    )


class MemoryBudgetService:
    def __init__(
        self,
        database: Database,
        *,
        administrator: Callable[
            [], MemoryAdministratorLimits
        ] = administrator_memory_limits,
        runtime_defaults_bytes: int = 0,
    ):
        if (
            type(runtime_defaults_bytes) is not int
            or not 0 <= runtime_defaults_bytes <= 1024**5
        ):
            raise MemoryBudgetError(
                "default runtime reserve must be a bounded byte count"
            )
        self.database = database
        self.administrator = administrator
        self.runtime_defaults_bytes = runtime_defaults_bytes

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[Any]:
        if self.database.task_session() is not None:
            raise RuntimeError("memory admission requires its own host transaction")
        async with self.database.session() as session:
            if self.database.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            yield session

    async def _policy(self, session) -> MemoryBudgetPolicyRow:
        existing = await session.scalar(
            select(MemoryBudgetPolicyRow)
            .where(MemoryBudgetPolicyRow.scope_key == POLICY_SCOPE)
            .with_for_update()
        )
        if existing is not None:
            return existing
        administrator = self.administrator()
        reserve = administrator.minimum_reserve_bytes + self.runtime_defaults_bytes
        aggregate = min(
            administrator.maximum_aggregate_bytes,
            administrator.capacity_bytes - reserve,
        )
        policy = MemoryBudgetPolicy(
            default_job=MemoryLimits(
                max_bytes=min(2 * GIB, administrator.maximum_job_bytes, aggregate)
            ),
            aggregate_bytes=aggregate,
            reserve_bytes=reserve,
        )
        validate_policy(policy, administrator)
        factory = (
            pg_insert
            if self.database.engine.dialect.name == "postgresql"
            else sqlite_insert
        )
        await session.execute(
            factory(MemoryBudgetPolicyRow)
            .values(
                scope_key=POLICY_SCOPE,
                revision=1,
                policy=policy.model_dump(mode="json"),
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["scope_key"])
        )
        # This single row serializes every policy change and aggregate claim.
        await session.execute(
            update(MemoryBudgetPolicyRow)
            .where(MemoryBudgetPolicyRow.scope_key == POLICY_SCOPE)
            .values(revision=MemoryBudgetPolicyRow.revision)
        )
        row = await session.get(MemoryBudgetPolicyRow, POLICY_SCOPE)
        assert row is not None
        return row

    @staticmethod
    def _audit(session, actor_id, scope, action, previous, proposed, result):
        if not actor_id or len(actor_id) > 200:
            raise MemoryBudgetError("a bounded authenticated actor is required")
        session.add(
            MemoryBudgetAuditRow(
                id=str(uuid4()),
                actor_id=actor_id,
                scope_key=scope,
                action=action,
                previous=previous,
                proposed=proposed,
                result=result,
                created_at=utc_now(),
            )
        )

    async def status(
        self, capabilities: Sequence[MemoryRunnerCapabilities] = ()
    ) -> MemoryBudgetStatus:
        async with self._write() as session:
            row = await self._policy(session)
            policy = MemoryBudgetPolicy.model_validate(row.policy)
            reserved = int(
                await session.scalar(
                    select(func.sum(MemoryJobRow.reserved_bytes)).where(
                        MemoryJobRow.state.in_(_HELD)
                    )
                )
                or 0
            )
            held = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryJobRow)
                    .where(MemoryJobRow.state.in_(_HELD))
                )
                or 0
            )
            queued = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MemoryJobRow)
                    .where(MemoryJobRow.state.in_(("queued", "blocked")))
                )
                or 0
            )
            administrator = self.administrator()
            from .runtime_memory import runtime_memory_footprint

            runtime_memory = await runtime_memory_footprint(
                session, services_floor=administrator.minimum_reserve_bytes
            )
            effective_aggregate = max(
                0,
                min(
                    policy.aggregate_bytes,
                    administrator.maximum_aggregate_bytes,
                    administrator.capacity_bytes - policy.reserve_bytes,
                ),
            )
            runner_capacities = [
                item.capacity_bytes - policy.reserve_bytes
                for item in capabilities
                if item.kernel_enforced and item.capacity_bytes is not None
            ]
            if runner_capacities:
                effective_aggregate = max(
                    0, min(effective_aggregate, *runner_capacities)
                )
            reserve_gap = (
                runtime_memory.minimum_core_reserve_bytes > policy.reserve_bytes
            )
            if reserve_gap:
                effective_aggregate = 0
            usable = any(
                item.kernel_enforced
                and item.protected_process_tree
                and item.explicit_swap
                for item in capabilities
            )
            state = (
                "draining"
                if reserve_gap or reserved > effective_aggregate
                else "ready"
                if usable
                else "unavailable"
            )
            return MemoryBudgetStatus(
                revision=row.revision,
                policy=policy,
                administrator_limits=administrator,
                capabilities=list(capabilities),
                reserved_bytes=reserved,
                available_bytes=max(0, effective_aggregate - reserved),
                effective_aggregate_bytes=effective_aggregate,
                held_jobs=held,
                queued_jobs=queued,
                runtime_reserved_bytes=runtime_memory.reserved_bytes,
                minimum_core_reserve_bytes=runtime_memory.minimum_core_reserve_bytes,
                application_state=state,
                detail="Core reserve is below the resident-runtime minimum. Increase Core reserve or retire inactive runtimes; new compute waits and running limits are unchanged."
                if reserve_gap
                else "New admissions wait for existing reservations to drain; running limits are unchanged."
                if state == "draining"
                else "Saved policy applies to newly submitted jobs; each launch still requires observed kernel enforcement."
                if usable
                else "Policy is saved. Configure a runner that verifies kernel memory and swap limits before launching compute jobs.",
            )

    async def save(
        self,
        request: MemoryBudgetUpdate,
        *,
        actor_id: str,
        permitted_profiles: Sequence[str] = (),
        capabilities: Sequence[MemoryRunnerCapabilities] = (),
    ) -> MemoryBudgetStatus:
        validate_policy(request.policy, self.administrator())
        if set(request.policy.profile_overrides) - set(permitted_profiles):
            raise MemoryBudgetError("choose an existing permitted execution profile")
        for capability in capabilities:
            limits = (
                (
                    request.policy.profile_overrides.get(capability.profile_id)
                    if capability.profile_id is not None
                    else None
                )
                or request.policy.environment_defaults.get(capability.environment)
                or request.policy.default_job
            )
            if limits.high_bytes is not None and not capability.pressure_threshold:
                raise MemoryBudgetError(
                    f"{capability.environment} cannot enforce the requested pressure threshold"
                )
        async with self._write() as session:
            row = await self._policy(session)
            if row.revision != request.expected_revision:
                raise MemoryBudgetConflict(
                    "memory budgets changed; reload before saving"
                )
            previous = row.policy
            from .runtime_memory import runtime_memory_footprint

            runtime_memory = await runtime_memory_footprint(
                session, services_floor=self.administrator().minimum_reserve_bytes
            )
            if request.policy.reserve_bytes < runtime_memory.minimum_core_reserve_bytes:
                raise MemoryBudgetError(
                    f"Resident runtimes and essential services need at least {runtime_memory.minimum_core_reserve_bytes} bytes of Core reserve; retire unused runtimes before reducing it"
                )
            row.policy = request.policy.model_dump(mode="json")
            row.revision += 1
            row.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                POLICY_SCOPE,
                "policy.save",
                previous,
                row.policy,
                {
                    "saved_revision": row.revision,
                    "application": "new_jobs",
                    "running_limits_changed": False,
                },
            )
        return await self.status(capabilities)

    async def create_job(
        self,
        job_id: str,
        *,
        project_id: str | None,
        resource_kind: str,
        resource_id: str,
        capabilities: MemoryRunnerCapabilities,
        requested: MemoryLimits | None = None,
        runner_overhead_bytes: int = 0,
    ) -> MemoryJob:
        if not job_id or len(job_id) > 200 or not resource_id or len(resource_id) > 200:
            raise MemoryBudgetError("bounded job and resource identities are required")
        async with self._write() as session:
            row = await self._policy(session)
            existing = await session.get(MemoryJobRow, job_id)
            if existing is not None:
                if (
                    existing.project_id,
                    existing.resource_kind,
                    existing.resource_id,
                ) != (project_id, resource_kind, resource_id):
                    raise MemoryBudgetConflict(
                        "job identity already belongs to another resource"
                    )
                frozen = EffectiveMemoryPolicy.model_validate(existing.snapshot)
                if frozen.runner_overhead_bytes != runner_overhead_bytes:
                    raise MemoryBudgetConflict(
                        "job identity already has different runner overhead"
                    )
                if (capabilities.environment, capabilities.profile_id) != (
                    frozen.environment,
                    frozen.profile_id,
                ) or (requested is not None and requested != frozen.limits):
                    raise MemoryBudgetConflict(
                        "job identity already has a different effective policy"
                    )
                return _job(existing)
            snapshot = select_memory_policy(
                MemoryBudgetPolicy.model_validate(row.policy),
                row.revision,
                self.administrator(),
                capabilities,
                requested=requested,
            )
            if (
                type(runner_overhead_bytes) is not int
                or not 0 <= runner_overhead_bytes <= GIB
            ):
                raise MemoryBudgetError("runner overhead must be a bounded byte amount")
            snapshot = snapshot.model_copy(
                update={"runner_overhead_bytes": runner_overhead_bytes}
            )
            state, application, detail = "queued", "not_started", None
            try:
                require_memory_enforcement(snapshot.limits, capabilities)
            except MemoryEnforcementUnavailable as error:
                # diagnostic-expected: unsupported work is durably held, never launched without limits.
                state, application, detail = "blocked", "unavailable", str(error)
            now = utc_now()
            job = MemoryJobRow(
                id=job_id,
                project_id=project_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                revision=1,
                state=state,
                reserved_bytes=0,
                snapshot=snapshot.model_dump(mode="json"),
                evidence=None,
                pending_limits=None,
                application_state=application,
                outcome=None,
                detail=detail,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()
            return _job(job)

    async def reserve(
        self,
        job_id: str,
        capabilities: MemoryRunnerCapabilities,
        *,
        owner: MemoryLaunchOwner | None = None,
    ) -> MemoryJob:
        async with self._write() as session:
            policy_row = await self._policy(session)
            job = await self._get(session, job_id)
            if job.state not in {"queued", "blocked"}:
                return _job(job)
            snapshot = EffectiveMemoryPolicy.model_validate(job.snapshot)
            if (capabilities.environment, capabilities.profile_id) != (
                snapshot.environment,
                snapshot.profile_id,
            ):
                raise MemoryBudgetConflict(
                    "runner identity differs from the job's frozen policy"
                )
            require_memory_enforcement(snapshot.limits, capabilities)
            policy = MemoryBudgetPolicy.model_validate(policy_row.policy)
            validate_policy(policy, self.administrator())
            used = int(
                await session.scalar(
                    select(func.sum(MemoryJobRow.reserved_bytes)).where(
                        MemoryJobRow.state.in_(_HELD)
                    )
                )
                or 0
            )
            administrator = self.administrator()
            from .runtime_memory import runtime_memory_footprint

            runtime_memory = await runtime_memory_footprint(
                session, services_floor=administrator.minimum_reserve_bytes
            )
            reserve_gap = (
                runtime_memory.minimum_core_reserve_bytes > policy.reserve_bytes
            )
            capacity = min(
                policy.aggregate_bytes,
                administrator.maximum_aggregate_bytes,
                administrator.capacity_bytes - policy.reserve_bytes,
            )
            if capabilities.capacity_bytes is not None:
                capacity = min(
                    capacity, capabilities.capacity_bytes - policy.reserve_bytes
                )
            if (
                reserve_gap
                or used + snapshot.reservation_bytes > capacity
                or snapshot.limits.max_bytes > administrator.maximum_job_bytes
                or snapshot.limits.swap_bytes > administrator.maximum_swap_bytes
            ):
                job.state, job.detail = (
                    "queued",
                    "Increase Core reserve or retire inactive runtimes before admitting more compute. Existing jobs keep their limits."
                    if reserve_gap
                    else "Waiting for aggregate memory capacity. Existing jobs keep their reserved limits.",
                )
            else:
                job.state, job.detail, job.reserved_bytes = (
                    "reserved",
                    None,
                    snapshot.reservation_bytes,
                )
                # A committed charge must identify the controller before its
                # next await. RESERVED is also the durable no-launch boundary.
                job.launch_owner = owner.model_dump(mode="json") if owner else None
            job.revision += 1
            job.updated_at = utc_now()
            await session.flush()
            return _job(job)

    @staticmethod
    async def _get(session, job_id, expected_revision=None):
        row = await session.get(MemoryJobRow, job_id)
        if row is None:
            raise MemoryBudgetError("memory job does not exist")
        if expected_revision is not None and row.revision != expected_revision:
            raise MemoryBudgetConflict(
                "memory job changed; reload before applying limits"
            )
        return row

    async def get_job(self, job_id: str) -> MemoryJob:
        async with self.database.session() as session:
            return _job(await self._get(session, job_id))

    async def list_jobs(self, *, limit: int = 100) -> list[MemoryJob]:
        if not 1 <= limit <= 256:
            raise MemoryBudgetError("job page size must be between 1 and 256")
        async with self.database.session() as session:
            return [
                _job(row)
                for row in await session.scalars(
                    select(MemoryJobRow)
                    .order_by(
                        case(
                            (MemoryJobRow.state.in_(_HELD), 0),
                            (MemoryJobRow.state.in_(("queued", "blocked")), 1),
                            else_=2,
                        ),
                        MemoryJobRow.created_at.desc(),
                        MemoryJobRow.id,
                    )
                    .limit(limit)
                )
            ]

    async def reconciliation_jobs(
        self, *, after: str = "", limit: int = 64
    ) -> list[MemoryJob]:
        if not 1 <= limit <= 256:
            raise MemoryBudgetError(
                "reconciliation page size must be between 1 and 256"
            )
        async with self.database.session() as session:
            return [
                _job(row)
                for row in await session.scalars(
                    select(MemoryJobRow)
                    .where(
                        MemoryJobRow.state.in_(_HELD),
                        MemoryJobRow.id > after,
                        MemoryJobRow.launch_owner.is_not(None),
                    )
                    .order_by(MemoryJobRow.id)
                    .limit(limit)
                )
            ]

    @staticmethod
    def _verify(job, evidence, limits):
        if (
            evidence.job_id != job.id
            or evidence.applied != limits
            or not evidence.protected_process_tree
        ):
            raise MemoryBudgetError(
                "runner evidence does not verify this job's effective limits and complete process tree"
            )

    async def claim_start(
        self, job_id: str, revision: int, *, owner: MemoryLaunchOwner | None = None
    ) -> MemoryJob:
        """Only one controller may cross the external launch boundary."""
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state != "reserved":
                raise MemoryBudgetConflict(
                    "job already has a launch owner or no reservation"
                )
            if job.launch_owner is not None:
                reserved_owner = MemoryLaunchOwner.model_validate(job.launch_owner)
                if (
                    owner is None
                    or owner.model_copy(update={"runner_identity": None})
                    != reserved_owner.model_copy(update={"runner_identity": None})
                    or (
                        reserved_owner.runner_identity is not None
                        and owner.runner_identity != reserved_owner.runner_identity
                    )
                ):
                    raise MemoryBudgetConflict(
                        "reservation belongs to another launch owner"
                    )
            job.state = "starting"
            job.launch_owner = owner.model_dump(mode="json") if owner else None
            job.revision += 1
            job.updated_at = utc_now()
            await session.flush()
            return _job(job)

    async def release_prelaunch(
        self,
        job_id: str,
        revision: int,
        *,
        owner: MemoryLaunchOwner,
        owner_dead: bool = False,
    ) -> MemoryJob:
        """Release a never-launched reservation without inventing group cleanup."""
        from .process_identity import launch_owner_ended, process_origin, same_process

        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if (
                job.state != "reserved"
                or job.launch_groups is not None
                or job.evidence is not None
                or job.launch_owner is None
                or MemoryLaunchOwner.model_validate(job.launch_owner) != owner
            ):
                raise MemoryBudgetConflict(
                    "reservation crossed launch or changed owner"
                )
            if owner_dead:
                unverified = not launch_owner_ended(
                    owner.process_origin, owner.process_id, owner.process_birth
                )
            else:
                unverified = (
                    owner.process_origin != process_origin()
                    or owner.process_id != os.getpid()
                    or not same_process(owner.process_id, owner.process_birth)
                )
            if unverified:
                raise MemoryBudgetConflict(
                    "prelaunch owner death or cancellation authority is unverified"
                )
            job.state, job.reserved_bytes, job.pending_limits = "finished", 0, None
            job.outcome = "worker_lost" if owner_dead else "cancelled"
            job.application_state = "not_started"
            job.detail = (
                "Launch owner died before crossing the launch boundary; no runner groups were created or cleaned. Explicit retry required."
                if owner_dead
                else "Controller cancelled before crossing the launch boundary; no runner groups were created or cleaned."
            )
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                "system",
                job.id,
                "job.prelaunch_released",
                None,
                None,
                {
                    "released": True,
                    "never_launched": True,
                    "owner_dead": owner_dead,
                    "groups_cleaned": False,
                    "owner": owner.model_dump(mode="json"),
                },
            )
            await session.flush()
            return _job(job)

    async def started(
        self, job_id: str, revision: int, evidence: MemoryEnforcementEvidence
    ) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state != "starting":
                raise MemoryBudgetConflict(
                    "job must reserve aggregate capacity before launch"
                )
            self._verify(
                job, evidence, EffectiveMemoryPolicy.model_validate(job.snapshot).limits
            )
            if job.launch_groups:
                groups = MemoryLaunchGroups.model_validate(job.launch_groups)
                if groups.group_id != evidence.group_id or set(
                    groups.auxiliary_group_ids
                ) != set(evidence.auxiliary_group_ids):
                    raise MemoryBudgetError(
                        "readiness differs from the immutable launch groups"
                    )
            if (
                EffectiveMemoryPolicy.model_validate(job.snapshot).runner_overhead_bytes
                and not evidence.auxiliary_group_ids
            ):
                raise MemoryBudgetError(
                    "helper identities must be durable before workload release"
                )
            if evidence.group_empty or evidence.termination_reason != "running":
                raise MemoryBudgetError(
                    "runner did not observe a live protected resource group"
                )
            job.state, job.application_state = "running", "verified"
            job.evidence, job.detail = evidence.model_dump(mode="json"), evidence.detail
            job.revision += 1
            job.updated_at = utc_now()
            await session.flush()
            return _job(job)

    async def groups_created(
        self, job_id: str, revision: int, groups: MemoryLaunchGroups
    ) -> MemoryJob:
        """Commit immutable cleanup identities before starting the guarded group."""
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state != "starting" or job.launch_groups:
                raise MemoryBudgetConflict(
                    "launch groups can be bound only once before start"
                )
            if (
                EffectiveMemoryPolicy.model_validate(job.snapshot).runner_overhead_bytes
                and not groups.auxiliary_group_ids
            ):
                raise MemoryBudgetError(
                    "charged helpers require durable cleanup identities"
                )
            job.launch_groups = groups.model_dump(mode="json")
            job.revision += 1
            job.updated_at = utc_now()
            await session.flush()
            return _job(job)

    async def finish(
        self,
        job_id: str,
        revision: int,
        evidence: MemoryEnforcementEvidence,
        *,
        actor_id: str = "system",
    ) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state == "finished":
                if job.evidence != evidence.model_dump(mode="json"):
                    raise MemoryBudgetConflict("finished memory evidence is immutable")
                return _job(job)
            if job.state not in _HELD:
                raise MemoryBudgetConflict(
                    "only a reserved job can release memory capacity"
                )
            active = (
                MemoryEnforcementEvidence.model_validate(job.evidence).applied
                if job.evidence
                else EffectiveMemoryPolicy.model_validate(job.snapshot).limits
            )
            target = (
                MemoryLimits.model_validate(job.pending_limits)
                if job.pending_limits
                else None
            )
            if evidence.applied not in (active, target):
                raise MemoryBudgetError(
                    "cleanup evidence does not match active or pending job limits"
                )
            self._verify(job, evidence, evidence.applied)
            if job.evidence:
                known = MemoryEnforcementEvidence.model_validate(job.evidence)
                if set(known.auxiliary_group_ids) != set(evidence.auxiliary_group_ids):
                    raise MemoryBudgetError(
                        "cleanup must verify the same owned auxiliary groups"
                    )
            if job.launch_groups:
                groups = MemoryLaunchGroups.model_validate(job.launch_groups)
                if groups.group_id != evidence.group_id or set(
                    groups.auxiliary_group_ids
                ) != set(evidence.auxiliary_group_ids):
                    raise MemoryBudgetError(
                        "cleanup differs from the immutable launch groups"
                    )
            if not evidence.group_empty:
                raise MemoryBudgetError(
                    "capacity remains reserved until the runner confirms every descendant is gone"
                )
            if EffectiveMemoryPolicy.model_validate(
                job.snapshot
            ).runner_overhead_bytes and (
                not evidence.auxiliary_groups_empty or not evidence.auxiliary_group_ids
            ):
                raise MemoryBudgetError(
                    "runner helper capacity remains charged until auxiliary groups are verified empty"
                )
            if (
                job.evidence
                and MemoryEnforcementEvidence.model_validate(job.evidence).group_id
                != evidence.group_id
            ):
                raise MemoryBudgetConflict(
                    "cleanup evidence belongs to another resource group"
                )
            previous = _job(job).model_dump(mode="json")
            job.state, job.reserved_bytes, job.pending_limits = "finished", 0, None
            job.evidence, job.outcome, job.detail = (
                evidence.model_dump(mode="json"),
                verified_memory_outcome(evidence),
                evidence.detail,
            )
            if evidence.application_uncertain:
                job.application_state = "uncertain"
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                job.id,
                "job.finished",
                previous,
                None,
                {"outcome": job.outcome, "released": True, "evidence": job.evidence},
            )
            await session.flush()
            return _job(job)

    async def observe(
        self, job_id: str, evidence: MemoryEnforcementEvidence
    ) -> MemoryJob:
        """Usage samples cannot alter limits, application revisions or outcomes."""
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id)
            if job.state not in {"running", "updating"} or not job.evidence:
                return _job(job)
            old = MemoryEnforcementEvidence.model_validate(job.evidence)
            if job.pending_limits and evidence.applied == MemoryLimits.model_validate(
                job.pending_limits
            ):
                # The explicit application action owns verification of a new
                # ceiling. A simultaneous usage poll cannot complete it.
                return _job(job)
            self._verify(job, evidence, old.applied)
            if evidence.group_id != old.group_id or evidence.group_empty:
                raise MemoryBudgetConflict(
                    "usage evidence belongs to another live group"
                )
            sample = evidence.model_copy(
                update={
                    "peak_bytes": max(old.peak_bytes or 0, evidence.peak_bytes or 0)
                    or None,
                    "auxiliary_group_ids": old.auxiliary_group_ids,
                }
            )
            if evidence.auxiliary_group_ids and set(
                evidence.auxiliary_group_ids
            ) != set(old.auxiliary_group_ids):
                raise MemoryBudgetError(
                    "usage cannot replace auxiliary group ownership"
                )
            job.evidence = sample.model_dump(mode="json")
            await session.flush()
            return _job(job)

    async def worker_lost(self, job_id: str, revision: int) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state in _HELD:
                job.state, job.detail = (
                    "uncertain",
                    "Worker lost. Capacity remains charged until its protected group is reconciled and empty.",
                )
                job.revision += 1
                job.updated_at = utc_now()
            await session.flush()
            return _job(job)

    async def cancel_not_started(
        self, job_id: str, revision: int, *, actor_id: str
    ) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state not in {"queued", "blocked"} or job.reserved_bytes:
                raise MemoryBudgetConflict(
                    "a reserved or launched job requires verified runner cleanup"
                )
            previous = _job(job).model_dump(mode="json")
            job.state, job.outcome, job.detail = (
                "finished",
                "cancelled",
                "Cancelled before memory admission.",
            )
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                job.id,
                "job.cancelled_queued",
                previous,
                None,
                {"released": True, "never_reserved": True},
            )
            await session.flush()
            return _job(job)

    async def unstarted_cleanup(
        self,
        job_id: str,
        revision: int,
        *,
        group_id: str,
        detail: str,
        verified_empty: bool,
    ) -> MemoryJob:
        """Called only by an authenticated runner after an unreleased launch was removed."""
        if not verified_empty or not group_id:
            raise MemoryBudgetError(
                "unstarted cleanup requires verified absence of the identified group"
            )
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state not in {"reserved", "starting", "uncertain"} or job.evidence:
                raise MemoryBudgetConflict(
                    "observed or released work requires its normal cleanup evidence"
                )
            job.state, job.outcome, job.reserved_bytes = "finished", "launch_failed", 0
            job.application_state, job.detail = "failed", detail[:4000]
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                "system",
                job.id,
                "job.unstarted_cleanup",
                None,
                None,
                {
                    "group_id": group_id,
                    "verified_empty": True,
                    "application": "failed",
                    "detail": detail[:4000],
                },
            )
            await session.flush()
            return _job(job)

    async def failed_running_update(
        self,
        job_id: str,
        revision: int,
        evidence: MemoryEnforcementEvidence,
        *,
        detail: str,
        actor_id: str = "system",
    ) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state != "updating" or not job.evidence:
                raise MemoryBudgetConflict("job has no pending memory application")
            old = MemoryEnforcementEvidence.model_validate(job.evidence)
            self._verify(job, evidence, old.applied)
            if evidence.group_id != old.group_id or evidence.group_empty:
                raise MemoryBudgetError(
                    "failed application must verify the original live group and limits"
                )
            proposed = job.pending_limits
            evidence = self._keep_auxiliary(old, evidence)
            job.evidence = evidence.model_dump(mode="json")
            job.reserved_bytes, job.pending_limits = _charge(job, old.applied), None
            job.state, job.application_state, job.detail = (
                "running",
                "failed",
                detail[:4000],
            )
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                job.id,
                "job.apply_failed",
                old.model_dump(mode="json"),
                proposed,
                {
                    "application": "failed",
                    "observed": job.evidence,
                    "detail": detail[:4000],
                },
            )
            await session.flush()
            return _job(job)

    async def request_running_update(
        self,
        job_id: str,
        request: MemoryRunningUpdate,
        capability: MemoryRunnerCapabilities,
        *,
        actor_id: str,
    ) -> MemoryJob:
        require_memory_enforcement(request.limits, capability)
        if not capability.live_update:
            raise MemoryEnforcementUnavailable(
                "This runner cannot safely change running limits; change the budget and explicitly retry the job"
            )
        async with self._write() as session:
            row = await self._policy(session)
            job = await self._get(session, job_id, request.expected_revision)
            if job.state != "running" or not job.evidence:
                raise MemoryBudgetConflict(
                    "only an observed running job can receive a live limit change"
                )
            snapshot = EffectiveMemoryPolicy.model_validate(job.snapshot)
            if (snapshot.environment, snapshot.profile_id) != (
                capability.environment,
                capability.profile_id,
            ):
                raise MemoryBudgetConflict(
                    "runner identity differs from the running job"
                )
            administrator = self.administrator()
            if (
                request.limits.max_bytes > administrator.maximum_job_bytes
                or request.limits.swap_bytes > administrator.maximum_swap_bytes
            ):
                raise MemoryBudgetError("running limits exceed administrator ceilings")
            old = MemoryEnforcementEvidence.model_validate(job.evidence).applied
            lowering = (
                request.limits.max_bytes < old.max_bytes
                or request.limits.swap_bytes < old.swap_bytes
                or (
                    request.limits.high_bytes is not None
                    and (
                        old.high_bytes is None
                        or request.limits.high_bytes < old.high_bytes
                    )
                )
            )
            if lowering and not request.acknowledge_possible_termination:
                raise MemoryBudgetError(
                    "lowering limits may terminate this job; explicitly acknowledge that impact"
                )
            # A runner can apply RAM and swap sequentially. Charge the largest
            # intermediate combination until kernel readback verifies both.
            intermediate = max(old.max_bytes, request.limits.max_bytes) + max(
                old.swap_bytes, request.limits.swap_bytes
            )
            intermediate += snapshot.runner_overhead_bytes
            extra = max(0, intermediate - job.reserved_bytes)
            used = int(
                await session.scalar(
                    select(func.sum(MemoryJobRow.reserved_bytes)).where(
                        MemoryJobRow.state.in_(_HELD)
                    )
                )
                or 0
            )
            policy = MemoryBudgetPolicy.model_validate(row.policy)
            capacity = min(
                policy.aggregate_bytes,
                administrator.maximum_aggregate_bytes,
                administrator.capacity_bytes - policy.reserve_bytes,
            )
            if capability.capacity_bytes is not None:
                capacity = min(
                    capacity, capability.capacity_bytes - policy.reserve_bytes
                )
            if used + extra > capacity:
                raise MemoryBudgetError(
                    "aggregate capacity is unavailable for the increased running limit"
                )
            job.reserved_bytes += extra
            job.pending_limits, job.application_state, job.state = (
                request.limits.model_dump(mode="json"),
                "pending",
                "updating",
            )
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                job.id,
                "job.apply_requested",
                old.model_dump(mode="json"),
                job.pending_limits,
                {"application": "pending"},
            )
            await session.flush()
            return _job(job)

    async def applied_running_update(
        self,
        job_id: str,
        revision: int,
        evidence: MemoryEnforcementEvidence,
        *,
        actor_id: str = "system",
    ) -> MemoryJob:
        async with self._write() as session:
            await self._policy(session)
            job = await self._get(session, job_id, revision)
            if job.state != "updating" or not job.pending_limits or not job.evidence:
                raise MemoryBudgetConflict("job has no pending memory application")
            target = MemoryLimits.model_validate(job.pending_limits)
            self._verify(job, evidence, target)
            if (
                MemoryEnforcementEvidence.model_validate(job.evidence).group_id
                != evidence.group_id
                or evidence.group_empty
            ):
                raise MemoryBudgetError(
                    "live update must verify the same live resource group"
                )
            previous = job.evidence
            evidence = self._keep_auxiliary(
                MemoryEnforcementEvidence.model_validate(previous), evidence
            )
            job.reserved_bytes, job.evidence = (
                _charge(job, target),
                evidence.model_dump(mode="json"),
            )
            job.pending_limits, job.application_state, job.state = (
                None,
                "verified",
                "running",
            )
            job.revision += 1
            job.updated_at = utc_now()
            self._audit(
                session,
                actor_id,
                job.id,
                "job.applied",
                previous,
                job.evidence,
                {"application": "verified"},
            )
            await session.flush()
            return _job(job)

    @staticmethod
    def _keep_auxiliary(
        old: MemoryEnforcementEvidence, new: MemoryEnforcementEvidence
    ) -> MemoryEnforcementEvidence:
        if new.auxiliary_group_ids and set(new.auxiliary_group_ids) != set(
            old.auxiliary_group_ids
        ):
            raise MemoryBudgetError(
                "live application cannot replace auxiliary group ownership"
            )
        return new.model_copy(
            update={
                "auxiliary_group_ids": old.auxiliary_group_ids,
                "peak_bytes": max(old.peak_bytes or 0, new.peak_bytes or 0) or None,
            }
        )
