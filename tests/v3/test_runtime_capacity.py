"""One SQL admission ledger bounds overlapping inert execution generations."""

import asyncio
from dataclasses import replace

import pytest

from nebula.v3.database import RuntimeCapacityReservationRow
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.execution_scope import execution_context
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.runtime_capacity import RuntimeCapacity
from nebula.v3.runtime_coordination import CoordinationConflict, LEASE_MS, LeaseLost
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def shared(cluster):
    coordinators, stores, clock = cluster
    capacities = [RuntimeCapacity(item) for item in coordinators]
    leases = [
        await coordinator.claim_project(project, runtime)
        for coordinator, project, runtime in zip(
            coordinators, ("project", "other"), ("old", "new")
        )
    ]
    await capacities[0].configure({"provider": 2, "background": 1, "tool": 1, "cpu": 2})
    return capacities, leases, stores, clock


async def test_concurrent_generations_cannot_spend_the_same_capacity(shared):
    capacities, leases, _, _ = shared
    outcomes = await asyncio.gather(
        *(
            capacity.acquire(lease, "work", {"provider": 2})
            for capacity, lease in zip(capacities, leases)
        )
    )
    assert sum(result is not None for result in outcomes) == 1
    assert (await capacities[0].status())["used"] == {"provider": 2}


async def test_background_work_leaves_shared_direct_capacity(shared):
    capacities, leases, _, _ = shared
    assert await capacities[0].acquire(
        leases[0], "background", {"provider": 1, "background": 1}
    )
    assert (
        await capacities[1].acquire(
            leases[1], "background", {"provider": 1, "background": 1}
        )
        is None
    )
    assert await capacities[1].acquire(leases[1], "direct", {"provider": 1})
    assert (await capacities[0].status())["used"] == {"provider": 2, "background": 1}


async def test_all_demands_reserve_atomically_without_partial_charges(shared):
    capacities, leases, _, _ = shared
    await capacities[0].acquire(leases[0], "tool", {"tool": 1})
    assert (
        await capacities[1].acquire(leases[1], "combined", {"tool": 1, "cpu": 2})
        is None
    )
    assert await capacities[1].acquire(leases[1], "cpu", {"cpu": 2})
    assert (await capacities[0].status())["used"] == {"tool": 1, "cpu": 2}


async def test_reservation_receipts_are_immutable_and_release_is_idempotent(shared):
    capacities, leases, _, _ = shared
    reservation = await capacities[0].acquire(leases[0], "work", {"provider": 1})
    assert (
        await capacities[1].acquire(leases[0], "work", {"provider": 1}) == reservation
    )
    with pytest.raises(CoordinationConflict, match="different demands"):
        await capacities[1].acquire(leases[0], "work", {"provider": 2})
    with pytest.raises(CoordinationConflict, match="immutable reservation"):
        await capacities[1].release(replace(reservation, lease=leases[1]))
    await capacities[0].release(reservation)
    await capacities[1].release(reservation)
    assert (await capacities[0].status())["used"] == {}
    with pytest.raises(CoordinationConflict, match="new attempt"):
        await capacities[1].acquire(leases[0], "work", {"provider": 1})


async def test_new_service_observes_existing_reservations_after_restart(shared):
    capacities, leases, _, _ = shared
    reservation = await capacities[0].acquire(leases[0], "tool", {"tool": 1})
    restarted = RuntimeCapacity(capacities[1].coordination)
    assert await restarted.acquire(leases[1], "other-tool", {"tool": 1}) is None
    await restarted.release(reservation)
    assert await capacities[0].acquire(leases[1], "other-tool", {"tool": 1})


async def test_expired_owner_keeps_capacity_until_its_effect_is_resolved(shared):
    capacities, leases, _, clock = shared
    first, second = capacities
    reservation = await first.acquire(leases[0], "unknown-provider", {"provider": 2})
    clock[0] += LEASE_MS + 1
    await second.coordination.register_runtime("replacement", "new-version")
    replacement = await second.coordination.claim_project("project", "replacement")
    with pytest.raises(LeaseLost):
        await first.acquire(leases[0], "stale", {"provider": 1})
    assert (
        await second.acquire(replacement, "replacement-work", {"provider": 1}) is None
    )
    # Only the original immutable receipt acknowledges this particular effect
    # ended. Lease expiry, handoff and a new process do not create that evidence.
    await first.release(reservation)
    current = await second.acquire(replacement, "replacement-work", {"provider": 1})
    with pytest.raises(CoordinationConflict):
        await first.release(replace(reservation, id=current.id))
    assert (await first.status())["used"] == {"provider": 1}


async def test_lower_capacity_drains_existing_work_and_requires_a_revision(shared):
    capacities, leases, _, _ = shared
    first, second = capacities
    reservation = await first.acquire(leases[0], "active", {"provider": 2})
    limits = (await first.status())["limits"] | {"provider": 1}
    changed = await first.configure(limits, expected_revision=1)
    assert changed["revision"] == 2
    with pytest.raises(CoordinationConflict, match="changed"):
        await second.configure(limits, expected_revision=1)
    with pytest.raises(CoordinationConflict, match="Resident host capacity"):
        await second.configure(limits | {"provider": 2})
    assert (await second.status())["draining"]
    assert await second.acquire(leases[1], "new", {"provider": 1}) is None
    await first.release(reservation)
    assert not (await second.status())["draining"]
    assert await second.acquire(leases[1], "new", {"provider": 1})


async def test_unsupported_or_unconfigured_capacity_fails_before_admission(shared):
    capacities, leases, _, _ = shared
    capacity = capacities[0]
    for demands in (
        {},
        {"other": 1},
        {"provider": True},
        {"provider": 0},
        {"provider": 4097},
    ):
        with pytest.raises(ValueError):
            await capacity.acquire(leases[0], "invalid", demands)
    with pytest.raises(CoordinationConflict, match="exceed"):
        await capacity.acquire(leases[0], "oversize", {"provider": 3})
    await capacity.configure({"provider": 1}, expected_revision=1)
    with pytest.raises(CoordinationConflict, match="configure every"):
        await capacity.acquire(leases[0], "missing", {"tool": 1})
    assert (await capacity.status())["held_reservations"] == 0


async def test_cancellation_joins_a_committed_acquisition_and_releases_it(
    shared, monkeypatch
):
    capacities, leases, _, _ = shared
    capacity = capacities[0]
    committed, proceed = asyncio.Event(), asyncio.Event()
    acquire = capacity.acquire

    async def held(*args):
        result = await acquire(*args)
        committed.set()
        await proceed.wait()
        return result

    monkeypatch.setattr(capacity, "acquire", held)

    async def caller():
        async with capacity.slot(leases[0], "lost-response", {"tool": 1}):
            pytest.fail("Cancelled admission must not start computation")

    task = asyncio.create_task(caller())
    await asyncio.wait_for(committed.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done() and (await capacity.status())["held_reservations"] == 1
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await capacity.status())["held_reservations"] == 0


async def test_active_slot_releases_after_known_local_cancellation(shared):
    capacities, leases, _, _ = shared
    entered = asyncio.Event()

    async def caller():
        async with capacities[0].slot(leases[0], "cancel", {"cpu": 2}):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(caller())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await capacities[1].acquire(leases[1], "after-cancel", {"cpu": 2})


async def turn_scheduler(store, capacity, lease):
    turn = await store.create(
        ChatTurn(
            engagement_id=lease.project_id,
            session_id="synthetic-session",
            provider_profile_id="inert-provider",
            model="inert-model",
            status=ChatTurnStatus.QUEUED,
            capacity_lane="background",
        )
    )
    scheduler = ProviderScheduler(
        store,
        worker_id=lease.runtime_id,
        runtime_capacity=capacity,
        config=ProviderSchedulerConfig(provider_limit=2, background_limit=1),
    )
    await scheduler.enqueue(turn)
    return turn, scheduler


async def test_real_provider_schedulers_share_capacity_across_generations(shared):
    capacities, leases, stores, _ = shared
    (turn, first), (other, second) = [
        await turn_scheduler(store, capacity, lease)
        for store, capacity, lease in zip(stores, capacities, leases)
    ]
    with execution_context(leases[0]):
        admitted = await first.admit(turn.id)
    with execution_context(leases[1]):
        waiting = asyncio.create_task(second.admit(other.id))
    try:
        # The shared wait is observable durable turn state; no timing assertion
        # on how long a synthetic SQL transaction happens to take.
        async with asyncio.timeout(3):
            while (
                await stores[1].get(ChatTurn, other.id)
            ).admission_reason != "Waiting for shared host capacity":
                await asyncio.sleep(0.01)
        assert not waiting.done()
        await admitted.release(turn.id)
        replacement = await asyncio.wait_for(waiting, 3)
        assert (await capacities[0].status())["used"] == {
            "provider": 1,
            "background": 1,
        }
        await replacement.release(other.id)
        assert (await capacities[1].status())["held_reservations"] == 0
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await admitted.release(turn.id)


async def test_shared_provider_capacity_rejects_unbound_project_tasks(shared):
    capacities, leases, stores, _ = shared
    turn, scheduler = await turn_scheduler(stores[0], capacities[0], leases[0])
    with pytest.raises(LeaseLost, match="exact project"):
        await scheduler.admit(turn.id)
    assert not scheduler._provider.locked() and not scheduler._background.locked()
    assert (await capacities[0].status())["held_reservations"] == 0


async def test_held_capacity_prevents_worker_retirement(shared):
    capacities, leases, _, _ = shared
    capacity = capacities[0]
    coordinator = DeploymentCoordinator(capacity.coordination.database)
    held = await capacity.acquire(leases[0], "external-effect", {"tool": 1})
    assert any(
        held.id in value
        for value in await coordinator.retirement_blockers(leases[0].runtime_id)
    )
    await capacity.release(held)
    assert not any(
        "Capacity reservation" in value
        for value in await coordinator.retirement_blockers(leases[0].runtime_id)
    )
    async with capacity.coordination.database.session() as session:
        row = await session.get(RuntimeCapacityReservationRow, held.id)
        assert row.state == "released" and row.release_reason == "effect-ended"
