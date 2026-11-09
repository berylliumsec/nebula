"""The host memory ledger remains atomic across project execution versions."""

import asyncio

import pytest
from sqlalchemy import update

from nebula.v3.database import ProjectOwnershipRow
from nebula.v3.execution_memory import ExecutionMemoryBudget
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import GIB, MemoryAdministratorLimits, MemoryBudgetUpdate
from nebula.v3.runtime_coordination import LeaseLost
from tests.v3.test_execution_worker import runtime as runtime
from tests.v3.test_memory_budget import capability
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def memory(runtime):
    administrator = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    host = MemoryBudgetService(
        runtime.coordination.database, administrator=lambda: administrator
    )
    status = await host.status([capability()])
    await host.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(update={"aggregate_bytes": 2 * GIB}),
        ),
        actor_id="operator",
        capabilities=[capability()],
    )
    service = ExecutionMemoryBudget(
        runtime.coordination.coordination,
        runtime.lease,
        administrator=lambda: administrator,
    )
    yield service, host


async def create(service, identity="job", project="project"):
    return await service.create_job(
        identity,
        project_id=project,
        resource_kind="fixture",
        resource_id=identity,
        capabilities=capability(),
    )


async def test_memory_mutations_require_the_original_project_task(runtime, memory):
    service, host = memory
    with pytest.raises(LeaseLost, match="bound project task"):
        await create(service)
    with execution_context(runtime.lease):
        created = await create(service)
        reserved = await service.reserve(created.id, capability())
    assert reserved.reserved_bytes == 2 * GIB
    assert (await host.status()).reserved_bytes == reserved.reserved_bytes


async def test_other_project_jobs_and_policy_writes_are_rejected(runtime, memory):
    service, host = memory
    foreign = await create(host, "foreign", project="other")
    status = await host.status()
    with execution_context(runtime.lease):
        with pytest.raises(LeaseLost, match="another project"):
            await create(service, "escape", project="other")
        with pytest.raises(LeaseLost, match="another project"):
            await service.reserve(foreign.id, capability())
        with pytest.raises(LeaseLost, match="host control project"):
            await service.save(
                MemoryBudgetUpdate(
                    expected_revision=status.revision, policy=status.policy
                ),
                actor_id="operator",
            )
    assert (await host.status()).revision == status.revision


async def test_expired_project_cannot_start_or_release_a_memory_reservation(
    runtime, memory
):
    service, host = memory
    with execution_context(runtime.lease):
        created = await create(service)
        reserved = await service.reserve(created.id, capability())
    async with runtime.coordination.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "project")
            .values(lease_expires_at_ms=0)
        )
    await runtime.coordination.coordination.register_runtime(
        "replacement", "new-version"
    )
    await runtime.coordination.coordination.claim_project("project", "replacement")
    with execution_context(runtime.lease):
        with pytest.raises(LeaseLost):
            await service.claim_start(created.id, reserved.revision)
        with pytest.raises(LeaseLost):
            await service.unstarted_cleanup(
                created.id,
                reserved.revision,
                group_id="synthetic-absent-group",
                detail="Synthetic verified cleanup",
                verified_empty=True,
            )
    assert (await host.status()).reserved_bytes == 2 * GIB
    assert (await host.get_job(created.id)).state == "reserved"


async def test_two_versions_share_one_atomic_aggregate_reservation(runtime, memory):
    service, host = memory
    await runtime.coordination.coordination.register_runtime("new", "second-version")
    other = await runtime.coordination.coordination.claim_project("other", "new")
    second = ExecutionMemoryBudget(
        runtime.coordination.coordination, other, administrator=service.administrator
    )

    async def reserve(bound, lease, identity):
        with execution_context(lease):
            created = await create(bound, identity, project=lease.project_id)
            return await bound.reserve(created.id, capability())

    results = await asyncio.gather(
        reserve(service, runtime.lease, "first"), reserve(second, other, "second")
    )
    assert sorted(result.state for result in results) == ["queued", "reserved"]
    assert (await host.status()).reserved_bytes == 2 * GIB
