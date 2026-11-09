"""Runtime overlap uses the same atomic policy lock as compute and Core saves."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nebula.v3.database import Database, RuntimeInstanceRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetError,
    MemoryBudgetUpdate,
)
from nebula.v3.runtime_memory import RuntimeMemoryAdmission
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_deployment_lifecycle import manifest
from tests.v3.test_memory_budget import budgets as budgets
from tests.v3.test_memory_budget import capability, job
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio
_BYTES = 256 * MIB


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool(budgets):
    guards = [RuntimeMemoryAdmission(service) for service in budgets]
    coordinators = [
        DeploymentCoordinator(service.database, runtime_admission=guard.check)
        for service, guard in zip(budgets, guards)
    ]
    for version in ("v1", "v2"):
        await coordinators[0].stage(
            manifest(version, execution_reserve_bytes=_BYTES),
            Path("/isolated/runtime-memory") / version,
            "a" * 64,
        )
    return SimpleNamespace(budgets=budgets, guards=guards, coordinators=coordinators)


async def reserve(coordinator, version="v1"):
    return await coordinator.reserve_worker(
        version,
        role="api",
        endpoint="unix:///isolated/" + uuid4().hex,
        credential_reference=str(uuid4()),
    )


async def resize(service, value):
    saved = await service.status([capability()])
    changes = {
        "reserve_bytes": value,
        "aggregate_bytes": min(
            saved.policy.aggregate_bytes,
            saved.administrator_limits.capacity_bytes - value,
        ),
    }
    return await service.save(
        MemoryBudgetUpdate(
            expected_revision=saved.revision,
            policy=saved.policy.model_copy(update=changes),
        ),
        actor_id="operator",
        capabilities=[capability()],
    )


async def test_concurrent_runtime_admissions_cannot_spend_the_same_core_reserve(pool):
    first, second = pool.coordinators
    await resize(pool.budgets[0], 2 * GIB + _BYTES)
    outcomes = await asyncio.gather(
        reserve(first), reserve(second), return_exceptions=True
    )
    assert len([item for item in outcomes if isinstance(item, str)]) == 1
    assert len([item for item in outcomes if isinstance(item, MemoryBudgetError)]) == 1
    status = await pool.budgets[1].status([capability()])
    assert status.runtime_reserved_bytes == _BYTES
    assert (
        status.minimum_core_reserve_bytes
        == status.policy.reserve_bytes
        == 2 * GIB + _BYTES
    )


async def test_policy_cannot_remove_memory_reserved_by_a_resident_runtime(pool):
    await resize(pool.budgets[0], 2 * GIB + 2 * _BYTES)
    await reserve(pool.coordinators[0])
    before = await pool.budgets[1].status([capability()])
    with pytest.raises(MemoryBudgetError, match="Resident runtimes"):
        await resize(pool.budgets[1], 2 * GIB)
    after = await pool.budgets[0].status([capability()])
    assert after.revision == before.revision and after.policy == before.policy


async def test_policy_reduction_and_new_runtime_admission_serialize(pool):
    await resize(pool.budgets[0], 2 * GIB + 2 * _BYTES)
    await reserve(pool.coordinators[0])
    outcomes = await asyncio.gather(
        resize(pool.budgets[0], 2 * GIB + _BYTES),
        reserve(pool.coordinators[1], "v2"),
        return_exceptions=True,
    )
    assert len([item for item in outcomes if isinstance(item, MemoryBudgetError)]) == 1
    status = await pool.budgets[1].status([capability()])
    assert status.minimum_core_reserve_bytes <= status.policy.reserve_bytes


async def test_expired_runtime_lease_keeps_service_memory_until_verified_abort(pool):
    await resize(pool.budgets[0], 2 * GIB + _BYTES)
    identity = await reserve(pool.coordinators[0])
    async with pool.budgets[0].database.session() as session:
        row = await session.get(RuntimeInstanceRow, identity)
        row.lease_expires_at_ms = 0
    with pytest.raises(MemoryBudgetError):
        await reserve(pool.coordinators[1], "v2")
    # The reservation never spawned a PID, so its startup abort is definitive.
    await pool.coordinators[0].abort_start(identity)
    assert await reserve(pool.coordinators[1], "v2")
    assert (await pool.budgets[1].status()).runtime_reserved_bytes == _BYTES


async def test_changed_policy_after_preflight_is_rechecked_at_atomic_reservation(pool):
    await resize(pool.budgets[0], 2 * GIB + _BYTES)
    await pool.guards[0](manifest("v1", execution_reserve_bytes=_BYTES), "api")
    await resize(pool.budgets[1], 2 * GIB)
    with pytest.raises(MemoryBudgetError, match="Core reserve needs"):
        await reserve(pool.coordinators[0])
    assert (await pool.budgets[0].status()).runtime_reserved_bytes == 0


async def test_unaccounted_resident_runtime_holds_compute_without_changing_running_limits(
    pool,
):
    # Simulate a migrated or unknown startup that predates the new admission
    # adapter. Its resident memory must still be visible and stop new compute.
    legacy = DeploymentCoordinator(pool.budgets[0].database)
    await reserve(legacy)
    candidate = await job(pool.budgets[0])
    held = await pool.budgets[1].reserve(candidate.id, capability())
    assert held.state == "queued" and held.reserved_bytes == 0
    assert "Core reserve" in held.detail
    status = await pool.budgets[0].status([capability()])
    assert status.application_state == "draining" and status.available_bytes == 0


async def test_architecture_defaults_only_initialize_an_unsaved_policy(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    limits = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    first = MemoryBudgetService(
        database, administrator=lambda: limits, runtime_defaults_bytes=GIB
    )
    try:
        initial = await first.status()
        assert initial.policy.reserve_bytes == 3 * GIB
        assert initial.policy.aggregate_bytes == 13 * GIB
        await resize(first, 4 * GIB)
        later = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_defaults_bytes=2 * GIB
        )
        assert (await later.status()).policy.reserve_bytes == 4 * GIB
    finally:
        await database.dispose()


async def test_supervisor_binds_real_process_admission_to_saved_memory_policy(managed):
    limits = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
        maximum_swap_bytes=2 * GIB,
    )
    budgets = MemoryBudgetService(
        managed.database, administrator=lambda: limits, runtime_defaults_bytes=GIB
    )
    root = RuntimeSupervisor(
        managed.host,
        operator_token=_TOKEN,
        worker_admission=RuntimeMemoryAdmission(budgets),
    )
    await root.start()
    try:
        await managed.stage("v1", _API_WORKER)
        identity = await root.prepare_api("v1")
        assert (await budgets.status()).runtime_reserved_bytes == _BYTES
        with pytest.raises(MemoryBudgetError):
            await resize(budgets, 2 * GIB)
        await managed.host.retire(identity.runtime_id, timeout=5)
        assert (await resize(budgets, 2 * GIB)).runtime_reserved_bytes == 0
    finally:
        await root.close()
