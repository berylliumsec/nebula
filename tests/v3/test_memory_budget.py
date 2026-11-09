"""SQL admission, frozen policy and truthful worker application contracts."""

import asyncio

import pytest
from sqlalchemy import select

from nebula.v3.database import Database, MemoryBudgetAuditRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MemoryAdministratorLimits,
    MemoryBudgetConflict,
    MemoryBudgetError,
    MemoryBudgetUpdate,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
    resolve_memory_policy,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def capability(**changes):
    return MemoryRunnerCapabilities(
        environment="container",
        profile_id="fixture",
        kernel_enforced=True,
        protected_process_tree=True,
        explicit_swap=True,
        pressure_threshold=True,
        live_update=True,
        capacity_bytes=16 * GIB,
        detail="Isolated enforcement fixture",
        **changes,
    )


@pytest.fixture
async def budgets(location):
    databases = [
        Database(location, pool_settings=PoolSettings(5, 12, 2, 5)) for _ in range(2)
    ]
    administrator = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    services = [
        MemoryBudgetService(database, administrator=lambda: administrator)
        for database in databases
    ]
    try:
        start = await services[0].status([capability()])
        await services[0].save(
            MemoryBudgetUpdate(
                expected_revision=start.revision,
                policy=start.policy.model_copy(update={"aggregate_bytes": 4 * GIB}),
            ),
            actor_id="operator",
            capabilities=[capability()],
        )
        yield services
    finally:
        for database in databases:
            await database.dispose()


async def job(service, identity="job", *, caps=None, requested=None):
    return await service.create_job(
        identity,
        project_id="synthetic",
        resource_kind="fixture",
        resource_id=identity,
        capabilities=caps or capability(),
        requested=requested,
    )


def evidence(job, *, empty=False, reason="running", limits=None, **changes):
    return MemoryEnforcementEvidence(
        job_id=job.id,
        group_id=f"fixture-{job.id}",
        applied=limits or job.snapshot.limits,
        protected_process_tree=True,
        group_empty=empty,
        termination_reason=reason,
        detail="Synthetic runner evidence",
        **changes,
    )


async def running(service, identity="job"):
    await job(service, identity)
    reserved = await service.reserve(identity, capability())
    claimed = await service.claim_start(identity, reserved.revision)
    return await service.started(identity, claimed.revision, evidence(claimed))


async def test_policy_and_job_snapshots_survive_another_service(budgets):
    first, second = budgets
    original = await job(first)
    status = await second.status([capability()])
    await second.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(
                update={"default_job": MemoryLimits(max_bytes=GIB)}
            ),
        ),
        actor_id="butler",
        capabilities=[capability()],
    )
    assert (await first.get_job(original.id)).snapshot == original.snapshot
    assert (await job(second, "new")).snapshot.limits.max_bytes == GIB
    assert (await job(first)).snapshot == original.snapshot


async def test_concurrent_reservations_never_exceed_the_host_aggregate(budgets):
    first, second = budgets
    for index in range(6):
        await job(first, f"job-{index}")
    reservations = await asyncio.gather(
        *[
            (first, second)[index % 2].reserve(f"job-{index}", capability())
            for index in range(6)
        ]
    )
    assert sum(item.reserved_bytes for item in reservations) == 4 * GIB
    assert sum(item.state == "reserved" for item in reservations) == 2
    assert (await first.status([capability()])).reserved_bytes == 4 * GIB


async def test_swap_allowance_is_included_in_aggregate_reservations(budgets):
    first, second = budgets
    status = await first.status()
    await first.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(
                update={"default_job": MemoryLimits(max_bytes=2 * GIB, swap_bytes=GIB)}
            ),
        ),
        actor_id="operator",
        capabilities=[capability()],
    )
    await job(first, "one")
    await job(first, "two")
    results = await asyncio.gather(
        first.reserve("one", capability()), second.reserve("two", capability())
    )
    assert sum(item.reserved_bytes for item in results) == 3 * GIB
    assert sorted(item.state for item in results) == ["queued", "reserved"]


async def test_helper_capacity_is_reserved_and_cannot_be_released_without_cleanup(
    budgets,
):
    first, _ = budgets
    for identity in ("one", "two"):
        await first.create_job(
            identity,
            project_id="synthetic",
            resource_kind="fixture",
            resource_id=identity,
            capabilities=capability(),
            runner_overhead_bytes=64 * 1024**2,
        )
    reserved = await first.reserve("one", capability())
    assert reserved.reserved_bytes == 2 * GIB + 64 * 1024**2
    assert (await first.reserve("two", capability())).state == "queued"
    claimed = await first.claim_start("one", reserved.revision)
    live = await first.started(
        "one", claimed.revision, evidence(claimed, auxiliary_group_ids=["helper"])
    )
    proof = evidence(
        live, empty=True, reason="completed", auxiliary_group_ids=["helper"]
    )
    with pytest.raises(MemoryBudgetError, match="helper"):
        await first.finish("one", live.revision, proof)
    assert (await first.status()).reserved_bytes == reserved.reserved_bytes
    result = await first.finish(
        "one",
        live.revision,
        proof.model_copy(
            update={"auxiliary_group_ids": ["helper"], "auxiliary_groups_empty": True}
        ),
    )
    assert result.reserved_bytes == 0


async def test_stale_policy_update_conflicts_and_audit_preserves_the_actor(budgets):
    first, second = budgets
    status = await first.status([capability()])
    update = MemoryBudgetUpdate(expected_revision=status.revision, policy=status.policy)
    results = await asyncio.gather(
        first.save(update, actor_id="operator"),
        second.save(update, actor_id="butler"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, MemoryBudgetConflict) for item in results) == 1
    async with first.database.session() as session:
        audits = list(
            await session.scalars(
                select(MemoryBudgetAuditRow).order_by(MemoryBudgetAuditRow.created_at)
            )
        )
        assert audits[-1].actor_id in {"operator", "butler"}
        assert audits[-1].previous is not None and audits[-1].proposed is not None
        assert audits[-1].result["running_limits_changed"] is False


async def test_unsupported_enforcement_is_held_without_reserving_capacity(budgets):
    first, _ = budgets
    unsupported = capability().model_copy(
        update={
            "protected_process_tree": False,
            "detail": "Configure a protected runner",
        }
    )
    blocked = await job(first, caps=unsupported)
    assert blocked.state == "blocked"
    assert blocked.application_state == "unavailable"
    assert blocked.reserved_bytes == 0
    with pytest.raises(MemoryEnforcementUnavailable):
        await first.reserve(blocked.id, unsupported)


async def test_only_one_controller_can_claim_external_launch(budgets):
    first, second = budgets
    await job(first)
    reserved = await first.reserve("job", capability())
    results = await asyncio.gather(
        first.claim_start("job", reserved.revision),
        second.claim_start("job", reserved.revision),
        return_exceptions=True,
    )
    assert sum(isinstance(item, MemoryBudgetConflict) for item in results) == 1
    assert (await first.status()).reserved_bytes == 2 * GIB


async def test_runner_application_requires_matching_observed_limits(budgets):
    first, _ = budgets
    await job(first)
    reserved = await first.reserve("job", capability())
    claimed = await first.claim_start("job", reserved.revision)
    with pytest.raises(MemoryBudgetError, match="does not verify"):
        await first.started(
            "job",
            claimed.revision,
            evidence(claimed, limits=MemoryLimits(max_bytes=GIB)),
        )
    assert (await first.get_job("job")).application_state == "not_started"
    verified = await first.started("job", claimed.revision, evidence(claimed))
    assert verified.application_state == "verified"


async def test_cleanup_proof_is_required_before_capacity_is_released(budgets):
    first, second = budgets
    active = await running(first)
    with pytest.raises(MemoryBudgetError, match="every descendant"):
        await second.finish(
            active.id, active.revision, evidence(active, reason="completed")
        )
    assert (await first.status()).reserved_bytes == 2 * GIB
    finished = await second.finish(
        active.id, active.revision, evidence(active, empty=True, reason="completed")
    )
    assert finished.state == "finished" and finished.reserved_bytes == 0
    assert (await first.status()).reserved_bytes == 0


async def test_worker_loss_stays_charged_until_reconciled(budgets):
    first, second = budgets
    active = await running(first)
    uncertain = await second.worker_lost(active.id, active.revision)
    assert uncertain.state == "uncertain" and uncertain.reserved_bytes == 2 * GIB
    await first.finish(
        uncertain.id,
        uncertain.revision,
        evidence(uncertain, empty=True, reason="worker_lost"),
    )
    assert (await second.status()).reserved_bytes == 0


@pytest.mark.parametrize("positive", [False, True])
async def test_memory_failure_requires_positive_kernel_evidence(budgets, positive):
    first, _ = budgets
    active = await running(first)
    outcome = await first.finish(
        active.id,
        active.revision,
        evidence(
            active,
            empty=True,
            reason="memory_limit_exceeded",
            oom_kill_count=1 if positive else 0,
            oom_victim_confirmed=positive,
            peak_bytes=2 * GIB,
        ),
    )
    assert outcome.outcome == ("memory_limit_exceeded" if positive else "failed")
    assert outcome.evidence.peak_bytes == 2 * GIB


async def test_lower_aggregate_drains_without_changing_running_limits(budgets):
    first, _ = budgets
    for identity in ("one", "two"):
        await running(first, identity)
    status = await first.status([capability()])
    lowered = await first.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(update={"aggregate_bytes": 2 * GIB}),
        ),
        actor_id="butler",
        capabilities=[capability()],
    )
    assert lowered.application_state == "draining"
    assert lowered.reserved_bytes == 4 * GIB and lowered.available_bytes == 0
    assert (await first.get_job("one")).evidence.applied.max_bytes == 2 * GIB


async def test_live_lowering_requires_separate_acknowledgement_and_readback(budgets):
    first, _ = budgets
    active = await running(first)
    request = MemoryRunningUpdate(
        expected_revision=active.revision, limits=MemoryLimits(max_bytes=GIB)
    )
    with pytest.raises(MemoryBudgetError, match="explicitly acknowledge"):
        await first.request_running_update(
            active.id, request, capability(), actor_id="operator"
        )
    pending = await first.request_running_update(
        active.id,
        request.model_copy(update={"acknowledge_possible_termination": True}),
        capability(),
        actor_id="operator",
    )
    assert pending.application_state == "pending" and pending.reserved_bytes == 2 * GIB
    applied = await first.applied_running_update(
        pending.id,
        pending.revision,
        evidence(pending, limits=request.limits),
        actor_id="runner",
    )
    assert applied.application_state == "verified" and applied.reserved_bytes == GIB


async def test_saved_configuration_does_not_claim_worker_application(budgets):
    first, _ = budgets
    status = await first.status()
    assert status.application_state == "unavailable"
    assert "Policy is saved" in status.detail


async def test_older_running_job_stays_visible_ahead_of_finished_history_and_queue(
    budgets,
):
    service, _ = budgets
    active = await running(service, "older-active")
    recent = await job(service, "newer-finished")
    await service.cancel_not_started(recent.id, recent.revision, actor_id="operator")
    await job(service, "newest-queued")
    page = await service.list_jobs(limit=1)
    assert len(page) == 1 and page[0].id == active.id and page[0].state == "running"


async def test_policy_precedence_and_profile_override_permissions(budgets):
    first, _ = budgets
    status = await first.status([capability()])
    policy = status.policy.model_copy(
        update={
            "environment_defaults": {"container": MemoryLimits(max_bytes=3 * GIB)},
            "profile_overrides": {"fixture": MemoryLimits(max_bytes=4 * GIB)},
        }
    )
    with pytest.raises(MemoryBudgetError, match="existing permitted"):
        await first.save(
            MemoryBudgetUpdate(expected_revision=status.revision, policy=policy),
            actor_id="operator",
        )
    saved = await first.save(
        MemoryBudgetUpdate(expected_revision=status.revision, policy=policy),
        actor_id="operator",
        permitted_profiles=["fixture"],
    )
    result = resolve_memory_policy(
        saved.policy,
        saved.revision,
        saved.administrator_limits,
        capability(),
        requested=MemoryLimits(max_bytes=GIB),
    )
    assert result.source == "job" and result.limits.max_bytes == GIB


@pytest.mark.parametrize("change", ["aggregate", "reserve", "job", "swap"])
async def test_administrator_ceilings_are_enforced(budgets, change):
    first, _ = budgets
    status = await first.status()
    policy = status.policy.model_copy(
        update={
            "aggregate_bytes": 16 * GIB
            if change == "aggregate"
            else status.policy.aggregate_bytes,
            "reserve_bytes": GIB
            if change == "reserve"
            else status.policy.reserve_bytes,
            "default_job": MemoryLimits(
                max_bytes=9 * GIB if change == "job" else 2 * GIB,
                swap_bytes=3 * GIB if change == "swap" else 0,
            ),
        }
    )
    with pytest.raises(MemoryBudgetError):
        await first.save(
            MemoryBudgetUpdate(expected_revision=status.revision, policy=policy),
            actor_id="operator",
        )
