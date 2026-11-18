"""A project waiting for memory never waits forever, nor holds a Core stop.

The default Core reserve is sized from host memory; a full reserve gives an
idle resident's memory to a waiting project; when every resident is busy the
waiting requests fail after a bound, naming the projects holding the memory;
and a request no worker has started never blocks a host shutdown.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3 import runtime_auto_admission
from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    MemoryBudgetAuditRow,
    MemoryBudgetPolicyRow,
    ProjectAdmissionHoldRow,
    ProjectOwnershipRow,
    ProjectViewLeaseRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import IDLE_RETIREMENT_FEATURE
from nebula.v3.domain import ChatSession, Engagement
from nebula.v3.memory_budget import MemoryBudgetService, default_runtime_slots
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetError,
    MemoryBudgetUpdate,
)
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission, memory_refusal
from nebula.v3.runtime_coordination import OperationRejected, ProjectLease
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_memory_budget import capability
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
_WORKER = 256 * MIB


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _limits(capacity: int) -> MemoryAdministratorLimits:
    floor = min(2 * GIB, max(256 * MIB, capacity // 4))
    return MemoryAdministratorLimits(
        capacity_bytes=capacity,
        maximum_job_bytes=capacity - floor,
        maximum_aggregate_bytes=capacity - floor,
        minimum_reserve_bytes=floor,
        maximum_swap_bytes=0,
    )


def test_default_runtime_slots_follow_host_memory_within_bounds():
    # A quarter of memory in 256 MiB slots, at least the old API + 2 projects.
    assert default_runtime_slots(1 * GIB, _WORKER) == 3
    assert default_runtime_slots(4 * GIB, _WORKER) == 4
    assert default_runtime_slots(8 * GIB, _WORKER) == 8
    assert default_runtime_slots(16 * GIB, _WORKER) == 16
    assert default_runtime_slots(64 * GIB, _WORKER) == 16
    # A PostgreSQL budget's declared processes bound it too.
    assert default_runtime_slots(64 * GIB, _WORKER, process_ceiling=5) == 5
    assert default_runtime_slots(64 * GIB, _WORKER, process_ceiling=1) == 3


async def test_fresh_default_reserve_is_sized_from_host_memory(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    limits = _limits(16 * GIB)
    try:
        service = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_worker_bytes=_WORKER
        )
        status = await service.status()
        assert status.revision == 1
        # 2 GiB services floor + 16 slots of 256 MiB, not 2 GiB + 3 slots.
        assert status.policy.reserve_bytes == 6 * GIB
        assert status.policy.aggregate_bytes == 10 * GIB
    finally:
        await database.dispose()


async def test_only_a_never_saved_default_is_resized(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    limits = _limits(16 * GIB)
    try:
        # An existing Core wrote the earlier fixed default (API + 2 projects).
        earlier = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_defaults_bytes=3 * _WORKER
        )
        assert (await earlier.status()).policy.reserve_bytes == 2 * GIB + 3 * _WORKER
        sized = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_worker_bytes=_WORKER
        )
        assert await sized.size_unsaved_default() == 6 * GIB
        status = await sized.status()
        assert (status.revision, status.policy.reserve_bytes) == (2, 6 * GIB)
        assert status.policy.aggregate_bytes == 10 * GIB
        async with database.session() as session:
            actions = list(await session.scalars(select(MemoryBudgetAuditRow.action)))
        assert actions == ["policy.default_sized"]
        # Sizing again changes nothing.
        assert await sized.size_unsaved_default() is None

        # An operator save is kept exactly, even below the host default.
        await sized.save(
            MemoryBudgetUpdate(
                expected_revision=status.revision,
                policy=status.policy.model_copy(
                    update={"reserve_bytes": 3 * GIB, "aggregate_bytes": 12 * GIB}
                ),
            ),
            actor_id="operator",
            capabilities=[capability()],
        )
        larger = MemoryBudgetService(
            database,
            administrator=lambda: _limits(32 * GIB),
            runtime_worker_bytes=_WORKER,
        )
        assert await larger.size_unsaved_default() is None
        assert (await larger.status()).policy.reserve_bytes == 3 * GIB
    finally:
        await database.dispose()


async def test_saved_policy_at_revision_two_is_never_resized(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    limits = _limits(16 * GIB)
    try:
        earlier = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_defaults_bytes=3 * _WORKER
        )
        status = await earlier.status()
        await earlier.save(
            MemoryBudgetUpdate(expected_revision=1, policy=status.policy),
            actor_id="operator",
            capabilities=[capability()],
        )
        sized = MemoryBudgetService(
            database, administrator=lambda: limits, runtime_worker_bytes=_WORKER
        )
        assert await sized.size_unsaved_default() is None
        async with database.session() as session:
            row = await session.get(MemoryBudgetPolicyRow, "host")
            assert row.policy["reserve_bytes"] == 2 * GIB + 3 * _WORKER
    finally:
        await database.dispose()


def test_memory_refusal_is_found_behind_the_supervisor_rejection():
    try:
        try:
            raise MemoryBudgetError("Core reserve needs at least 1 bytes")
        except MemoryBudgetError as error:
            raise OperationRejected(str(error)) from error
    except OperationRejected as wrapped:
        assert isinstance(memory_refusal(wrapped), MemoryBudgetError)
    assert memory_refusal(OperationRejected("other refusal")) is None


async def _queued(coordinator, project_id, key, *, name="project.inspect"):
    return await coordinator.coordination.submit(
        project_id=project_id,
        actor_id="operator",
        idempotency_key=key,
        name=name,
        payload={},
    )


async def test_unstarted_request_of_a_held_workerless_project_never_holds_shutdown(
    host,
):
    coordinator, _store = host
    await _queued(coordinator, "two", "waiting-for-memory")
    # Until Core holds the project's worker, the request may still start.
    async with coordinator.database.session() as session:
        assert len(await coordinator._shutdown_blockers(session)) == 1
    async with coordinator.database.session() as session:
        session.add(
            ProjectAdmissionHoldRow(
                project_id="two",
                reason="Core reserve needs at least 3 GiB",
                attempts=1,
                updated_at_ms=0,
            )
        )
    async with coordinator.database.session() as session:
        assert await coordinator._shutdown_blockers(session) == []
    # A served project's queued work still holds it.
    runtime_id = await worker(host, "v1")
    await coordinator.coordination.claim_project("one", runtime_id)
    await _queued(coordinator, "one", "served-work")
    async with coordinator.database.session() as session:
        blockers = await coordinator._shutdown_blockers(session)
    assert len(blockers) == 1 and "remains active" in blockers[0]


async def test_failing_unstarted_requests_settles_only_old_workerless_ones(host):
    coordinator, _store = host
    coordination = coordinator.coordination
    result = {"http_status": 503, "detail": "waited for memory"}
    # A project with a worker is never settled from outside it.
    runtime_id = await worker(host, "v1")
    await coordination.claim_project("one", runtime_id)
    served = await _queued(coordinator, "one", "served")
    assert (
        await coordination.fail_unstarted_requests(
            "one", accepted_before_ms=10**15, result=result
        )
        == 0
    )
    old = await _queued(coordinator, "two", "old")
    report = await coordination.submit(
        project_id="two",
        actor_id="operator",
        idempotency_key="report",
        name="storage.report/v1",
        payload={},
        lane="report",
    )
    await asyncio.sleep(0.01)
    young = await _queued(coordinator, "two", "young")
    cutoff = min(old.accepted_at_ms, young.accepted_at_ms - 1)
    assert (
        await coordination.fail_unstarted_requests(
            "two", accepted_before_ms=max(cutoff, report.accepted_at_ms), result=result
        )
        == 1
    )
    async with coordinator.database.session() as session:
        states = {
            row.id: (row.state, row.result)
            for row in await session.scalars(select(RuntimeCommandRow))
        }
    assert states[old.command_id] == ("failed", result)
    assert states[young.command_id][0] == "queued"
    assert states[report.command_id][0] == "queued"
    assert states[served.command_id][0] == "queued"


async def test_capacity_retirement_releases_a_project_with_only_open_chats(host):
    coordinator, store = host
    runtime_id = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", runtime_id)
    await store.create(
        ChatSession(
            engagement_id="one",
            title="Open chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )
    # Idle retirement keeps a project with open chats.
    assert await coordinator.request_project_retirement(lease) is False
    assert await coordinator.request_project_retirement(lease, for_capacity=True)
    checkpoint = await coordinator.quiesce_checkpoint(lease, local_blockers=[])
    await coordinator.release_quiesced_project(lease, checkpoint=checkpoint)
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
    assert owner.runtime_id is None


# --- the admission loop ---------------------------------------------------


async def _ready_worker(coordinator, runtime_id):
    await coordinator.coordination.register_runtime(runtime_id, "capacity")
    async with coordinator.database.session() as session:
        session.add(
            RuntimeProcessRow(
                runtime_id=runtime_id,
                version="capacity",
                role="execution",
                pid=0,
                process_birth="synthetic-no-process",
                endpoint="unix:///synthetic/" + runtime_id,
                credential_reference="synthetic-" + runtime_id,
                state="ready",
                updated_at_ms=0,
            )
        )


@pytest.fixture
async def admission(host, monkeypatch):
    """Two execution slots; retiring a worker frees its slot."""
    coordinator, store = host
    await store.create(Engagement(id="three", name="Synthetic three"))
    await coordinator.stage(
        manifest("capacity", features=["project.quiesce/v1", IDLE_RETIREMENT_FEATURE]),
        Path("/isolated/capacity"),
        "c" * 64,
    )
    await coordinator.coordination.register_runtime("api", "capacity", role="api")
    await coordinator.target_execution("capacity")
    async with coordinator.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "api"
    value = SimpleNamespace(
        coordinator=coordinator, store=store, prepared=[], released=[], busy=set()
    )

    async def prepare(_version):
        async with coordinator.database.session() as session:
            live = list(
                await session.scalars(
                    select(RuntimeProcessRow.runtime_id).where(
                        RuntimeProcessRow.role == "execution",
                        RuntimeProcessRow.state != "retired",
                    )
                )
            )
        if len(live) >= 2:
            try:
                raise MemoryBudgetError("Core reserve needs at least 3 GiB")
            except MemoryBudgetError as error:
                raise OperationRejected(str(error)) from error
        runtime_id = f"capacity-worker-{len(value.prepared)}"
        value.prepared.append(runtime_id)
        await _ready_worker(coordinator, runtime_id)
        return SimpleNamespace(runtime_id=runtime_id)

    async def attach(project_id, runtime_id):
        return await coordinator.coordination.claim_project(project_id, runtime_id)

    async def retire_idle_project(project_id, *, for_capacity=False):
        assert for_capacity
        async with coordinator.database.session() as session:
            owner = await session.get(ProjectOwnershipRow, project_id)
            lease = ProjectLease(project_id, owner.runtime_id, owner.fencing_token)
        if not await coordinator.request_project_retirement(lease, for_capacity=True):
            return False
        checkpoint = await coordinator.quiesce_checkpoint(lease, local_blockers=[])
        await coordinator.release_quiesced_project(lease, checkpoint=checkpoint)
        async with coordinator.database.session() as session:
            (await session.get(RuntimeProcessRow, lease.runtime_id)).state = "retired"
        value.released.append(project_id)
        return True

    monkeypatch.setattr(
        runtime_auto_admission, "record_caught_exception", lambda *a, **k: None
    )
    monkeypatch.setattr(
        runtime_auto_admission, "_ADMISSION_BACKOFF_SECONDS", (0.05,) * 6
    )
    value.automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=coordinator,
            store=store,
            prepare_execution=prepare,
            attach_execution=attach,
            host=SimpleNamespace(retire_idle_project=retire_idle_project),
        ),
        default_version="capacity",
    )
    for project_id in ("one", "two", "three"):
        await store.create(
            ChatSession(
                engagement_id=project_id,
                title="Open chat",
                model="synthetic-model",
                provider_profile_id="synthetic-provider",
            )
        )
    return value


async def _owner(value, project_id):
    async with value.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, project_id)
        return owner.runtime_id if owner is not None else None


async def _settle(value, rounds=8):
    for _ in range(rounds):
        await value.automatic.reconcile()
        await asyncio.sleep(0.06)


async def _reach(value):
    """Projects one and two take both slots; three waits with only a chat."""
    async with value.coordinator.database.session() as session:
        for project_id in ("one", "two"):
            session.add(
                ProjectViewLeaseRow(
                    actor_id="operator",
                    lease_id="view-" + project_id,
                    project_id=project_id,
                    expires_at_ms=10**15,
                    renewed_at_ms=0,
                )
            )
    await _settle(value, 2)
    assert await _owner(value, "one") and await _owner(value, "two")
    async with value.coordinator.database.session() as session:
        for row in await session.scalars(select(ProjectViewLeaseRow)):
            await session.delete(row)
    await _settle(value, 3)
    # An open chat alone never takes another project's memory.
    assert await _owner(value, "three") is None and value.released == []


async def test_waiting_project_takes_an_idle_residents_memory(admission):
    value = admission
    await _reach(value)
    await _queued(value.coordinator, "three", "three-work")
    await _settle(value)
    assert await _owner(value, "three") is not None
    # Exactly one resident gave up its worker, the least recently used one.
    assert value.released == ["one"]
    assert await _owner(value, "two") is not None
    async with value.coordinator.database.session() as session:
        assert await session.get(ProjectAdmissionHoldRow, "three") is None


async def test_busy_residents_keep_memory_and_the_wait_ends_with_their_names(
    admission,
):
    value = admission
    await _reach(value)
    # Both residents run work: neither may be released.
    for project_id in ("one", "two"):
        await _queued(value.coordinator, project_id, "busy-" + project_id)
    value.automatic.memory_wait_seconds = 0.5
    await _queued(value.coordinator, "three", "three-work")
    await _settle(value, 3)
    async with value.coordinator.database.session() as session:
        hold = await session.get(ProjectAdmissionHoldRow, "three")
    assert hold is not None and hold.reason.startswith("Core reserve needs")
    assert "Waiting for memory" in hold.reason
    assert "Synthetic one" in hold.reason and "Synthetic two" in hold.reason
    await asyncio.sleep(0.6)
    await _settle(value, 3)
    assert value.released == []
    assert await _owner(value, "one") and await _owner(value, "two")
    assert await _owner(value, "three") is None
    async with value.coordinator.database.session() as session:
        command = await session.scalar(
            select(RuntimeCommandRow).where(RuntimeCommandRow.project_id == "three")
        )
    assert command.state == "failed"
    assert command.result["http_status"] == 503
    assert command.result["waiting_for_memory"] is True
    assert "Synthetic one" in command.result["detail"]
    assert "Synthetic two" in command.result["detail"]
