"""PostgreSQL lock contracts between execution workers of different projects.

Each runtime's transactions run one at a time and every runtime is fenced by
its ownership rows, while runtimes that own different projects run side by
side. Rows shared by all projects, and host Work batches, still serialize with
every execution transaction.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text, update

from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, Engagement, ProviderProfile, utc_now
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import LeaseLost, RuntimeCoordination
from nebula.v3.runtime_work_batches import RuntimeWorkBatches
from nebula.v3.storage import NebulaStore, NotFoundError
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_deployment_lifecycle import worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio

SETTLE = 10.0


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _pool():
    return PoolSettings(5, 12, 2, 5)


def _postgres_only(location):
    if not isinstance(location, str):
        pytest.skip("SQLite admits one writer with BEGIN IMMEDIATE; no advisory keys")


def _execution_store(location, runtime_id):
    return NebulaStore(
        Database(
            location,
            bootstrap=False,
            execution_runtime_id=runtime_id,
            pool_settings=_pool(),
        )
    )


async def _close(*stores):
    for store in stores:
        await store.event_history.close()
        await store.database.dispose()


@pytest.fixture
async def fleet(location):
    """Two projects, each owned by its own execution runtime, plus shared rows."""
    _postgres_only(location)
    control = NebulaStore(Database(location, pool_settings=_pool()))
    runtimes = {}
    try:
        for project in ("alpha", "beta"):
            await control.create(Engagement(id=project, name=project))
        for shared in ("shared-a", "shared-b"):
            await control.create(
                ProviderProfile(id=shared, name=shared, provider_type="openrouter")
            )
        coordinator = RuntimeCoordination(control.database)
        leases = {}
        for project in ("alpha", "beta"):
            runtime_id = "runtime-" + project
            await coordinator.register_runtime(runtime_id, "fixture")
            leases[project] = await coordinator.claim_project(project, runtime_id)
            runtimes[project] = _execution_store(location, runtime_id)
        yield control, coordinator, runtimes, leases
    finally:
        await _close(*runtimes.values(), control)


async def _pid(session):
    return await session.scalar(text("SELECT pg_backend_pid()"))


async def _blocked(control, holder, locktype):
    """Lock waits of ``locktype`` that backend ``holder`` is blocking."""
    async with control.database.session() as session:
        return await session.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE NOT granted "
                "AND locktype = :kind AND CAST(:holder AS integer) = "
                "ANY(pg_blocking_pids(pid))"
            ),
            {"kind": locktype, "holder": holder},
        )


async def _until_blocked(control, holder, locktype, task):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE
    while not await _blocked(control, holder, locktype):
        assert not task.done(), "the contending transaction did not wait"
        assert loop.time() < deadline, f"nothing waited on {locktype} behind {holder}"
        await asyncio.sleep(0.02)
    assert not task.done()


@asynccontextmanager
async def _held(store, lease, write):
    """Run ``write`` in an execution transaction, keep it open, yield its pid."""
    entered, release = asyncio.Event(), asyncio.Event()
    holder = []

    async def run():
        with execution_context(lease) if lease else _no_lease():
            async with store.transaction() as transaction:
                await write(transaction)
                holder.append(await _pid(transaction.session))
                entered.set()
                await release.wait()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), SETTLE)
        yield holder[0]
    finally:
        release.set()
        await asyncio.wait_for(task, SETTLE)


class _no_lease:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _rename(project, name):
    async def write(transaction):
        await transaction.update(Engagement, project, {"description": name})

    return write


async def test_projects_on_different_runtimes_do_not_wait_for_each_other(fleet):
    control, _, runtimes, leases = fleet
    async with _held(runtimes["alpha"], leases["alpha"], _rename("alpha", "open")):
        with execution_context(leases["beta"]):
            await asyncio.wait_for(
                runtimes["beta"].update(Engagement, "beta", {"description": "done"}),
                SETTLE,
            )
        assert (await control.get(Engagement, "beta")).description == "done"
        assert (await control.get(Engagement, "alpha")).description != "open"
    assert (await control.get(Engagement, "alpha")).description == "open"


async def test_one_runtime_still_runs_one_transaction_at_a_time(fleet, location):
    control, coordinator, runtimes, leases = fleet
    # Its runtime key orders a runtime's transactions, whether or not it owns
    # a project.
    await coordinator.register_runtime("runtime-idle", "fixture")
    idle = _execution_store(location, "runtime-idle")

    async def read(transaction):
        await transaction.session.execute(text("SELECT 1"))

    try:
        for store, lease in ((runtimes["alpha"], leases["alpha"]), (idle, None)):
            async with _held(store, lease, read) as holder:
                second = asyncio.create_task(store.get(ProviderProfile, "shared-a"))
                await _until_blocked(control, holder, "advisory", second)
            assert (await asyncio.wait_for(second, SETTLE)).id == "shared-a"
    finally:
        await _close(idle)


async def test_project_coordination_waits_for_its_open_transaction(fleet):
    control, coordinator, runtimes, leases = fleet
    async with _held(
        runtimes["alpha"], leases["alpha"], _rename("alpha", "open")
    ) as holder:

        async def fenced():
            async with coordinator.fenced(leases["alpha"]):
                pass

        claim = asyncio.create_task(fenced())
        # The ownership row the transaction locked orders the project's own
        # coordination, as it did under the single global key.
        await _until_blocked(control, holder, "transactionid", claim)
    await asyncio.wait_for(claim, SETTLE)


async def test_shared_rows_are_written_one_runtime_at_a_time(fleet):
    control, _, runtimes, leases = fleet

    async def alpha_write(transaction):
        await transaction.update(
            ProviderProfile, "shared-a", {"metadata": {"from": "alpha"}}
        )

    async with _held(runtimes["alpha"], leases["alpha"], alpha_write) as holder:
        with execution_context(leases["beta"]):
            beta = asyncio.create_task(
                runtimes["beta"].update(ProviderProfile, "shared-a", {"name": "beta"})
            )
        await _until_blocked(control, holder, "advisory", beta)
    # Beta read the row only after alpha committed, so neither change is lost
    # and beta's revision check passes.
    await asyncio.wait_for(beta, SETTLE)
    shared = await control.get(ProviderProfile, "shared-a")
    assert (shared.name, shared.metadata, shared.revision) == (
        "beta",
        {"from": "alpha"},
        3,
    )


async def test_opposite_order_shared_writes_do_not_deadlock(fleet):
    control, _, runtimes, leases = fleet
    alpha_first, beta_started, alpha_next = (asyncio.Event() for _ in range(3))

    holder = []

    async def alpha_writes():
        with execution_context(leases["alpha"]):
            async with runtimes["alpha"].transaction() as transaction:
                await transaction.update(
                    ProviderProfile, "shared-a", {"name": "alpha-a"}
                )
                holder.append(await _pid(transaction.session))
                alpha_first.set()
                await alpha_next.wait()
                await transaction.update(
                    ProviderProfile, "shared-b", {"metadata": {"alpha": True}}
                )

    async def beta_writes():
        with execution_context(leases["beta"]):
            async with runtimes["beta"].transaction() as transaction:
                beta_started.set()
                await transaction.update(
                    ProviderProfile, "shared-b", {"name": "beta-b"}
                )
                await transaction.update(
                    ProviderProfile, "shared-a", {"metadata": {"beta": True}}
                )

    alpha = asyncio.create_task(alpha_writes())
    await asyncio.wait_for(alpha_first.wait(), SETTLE)
    beta = asyncio.create_task(beta_writes())
    await asyncio.wait_for(beta_started.wait(), SETTLE)
    await _until_blocked(control, holder[0], "advisory", beta)
    # Alpha now writes the row beta wanted first; beta has not locked it.
    alpha_next.set()
    await asyncio.wait_for(asyncio.gather(alpha, beta), SETTLE)
    first = await control.get(ProviderProfile, "shared-a")
    second = await control.get(ProviderProfile, "shared-b")
    assert (first.name, first.metadata) == ("alpha-a", {"beta": True})
    assert (second.name, second.metadata) == ("beta-b", {"alpha": True})


async def test_read_then_write_of_a_shared_row_fires_once(fleet):
    control, _, runtimes, leases = fleet
    fired = []

    async def fire_once(project, hold=None):
        with execution_context(leases[project]):
            async with runtimes[project].transaction() as transaction:
                await transaction.lock_shared_rows()
                current = await transaction.session.get(EntityRow, "shared-b")
                if current.payload.get("metadata", {}).get("fired"):
                    return False
                await transaction.update(
                    ProviderProfile, "shared-b", {"metadata": {"fired": project}}
                )
                fired.append(project)
                if hold is not None:
                    hold[2].append(await _pid(transaction.session))
                    hold[0].set()
                    await hold[1].wait()
                return True

    hold = (asyncio.Event(), asyncio.Event(), [])
    alpha = asyncio.create_task(fire_once("alpha", hold))
    await asyncio.wait_for(hold[0].wait(), SETTLE)
    beta = asyncio.create_task(fire_once("beta"))
    await _until_blocked(control, hold[2][0], "advisory", beta)
    hold[1].set()
    assert await asyncio.wait_for(alpha, SETTLE) is True
    assert await asyncio.wait_for(beta, SETTLE) is False
    assert fired == ["alpha"]
    shared = await control.get(ProviderProfile, "shared-b")
    assert shared.metadata == {"fired": "alpha"}


async def test_cross_project_write_fails_without_waiting_for_its_owner(fleet):
    control, _, runtimes, leases = fleet
    async with _held(runtimes["beta"], leases["beta"], _rename("beta", "owner")):
        with execution_context(leases["alpha"]):
            with pytest.raises(NotFoundError):
                await asyncio.wait_for(
                    runtimes["alpha"].update(Engagement, "beta", {"description": "x"}),
                    SETTLE,
                )
            with pytest.raises(LeaseLost):
                await asyncio.wait_for(
                    runtimes["alpha"].create(
                        ChatTurn(
                            engagement_id="beta",
                            session_id="escape",
                            model="inert",
                            provider_profile_id="shared-a",
                        )
                    ),
                    SETTLE,
                )
    assert (await control.get(Engagement, "beta")).description == "owner"


async def test_work_batch_lock_excludes_every_execution_transaction(fleet):
    control, _, runtimes, leases = fleet
    # A batch waits for execution transactions already running...
    async with _held(
        runtimes["alpha"], leases["alpha"], _rename("alpha", "first")
    ) as holder:

        async def batch_lock():
            async with control.database.session() as session:
                await RuntimeWorkBatches._locks(None, session)

        batch = asyncio.create_task(batch_lock())
        await _until_blocked(control, holder, "advisory", batch)
    await asyncio.wait_for(batch, SETTLE)

    # ...and admits no new one, in any project, until it commits.
    entered, release = asyncio.Event(), asyncio.Event()
    batch_pid = []

    async def batch_hold():
        async with control.database.session() as session:
            await RuntimeWorkBatches._locks(None, session)
            batch_pid.append(await _pid(session))
            entered.set()
            await release.wait()

    holder = asyncio.create_task(batch_hold())
    try:
        await asyncio.wait_for(entered.wait(), SETTLE)
        with execution_context(leases["beta"]):
            blocked = asyncio.create_task(
                runtimes["beta"].update(Engagement, "beta", {"description": "after"})
            )
        await _until_blocked(control, batch_pid[0], "advisory", blocked)
    finally:
        release.set()
        await asyncio.wait_for(holder, SETTLE)
    await asyncio.wait_for(blocked, SETTLE)
    assert (await control.get(Engagement, "beta")).description == "after"


async def test_ownership_change_waits_for_the_owner_and_fences_it_after(fleet):
    control, coordinator, runtimes, leases = fleet
    async with _held(
        runtimes["alpha"], leases["alpha"], _rename("alpha", "kept")
    ) as holder:
        # Expiring and reclaiming the project serializes on the ownership row.
        async def reclaim():
            async with control.database.session() as session:
                await session.execute(
                    text(
                        "UPDATE project_ownership SET lease_expires_at_ms = 0 "
                        "WHERE project_id = 'alpha'"
                    )
                )
                await session.commit()

        change = asyncio.create_task(reclaim())
        await _until_blocked(control, holder, "transactionid", change)
    await asyncio.wait_for(change, SETTLE)
    assert (await control.get(Engagement, "alpha")).description == "kept"
    with execution_context(leases["alpha"]):
        with pytest.raises(LeaseLost):
            await runtimes["alpha"].update(Engagement, "alpha", {"description": "x"})
    # Another project's runtime is unaffected.
    with execution_context(leases["beta"]):
        await asyncio.wait_for(
            runtimes["beta"].update(Engagement, "beta", {"description": "fine"}),
            SETTLE,
        )


async def test_handoff_waits_for_an_in_flight_source_write(host, location):
    _postgres_only(location)
    coordinator, control = host
    source = await worker(host, "v1")
    target = await worker(host, "v2")
    lease = await coordinator.coordination.claim_project("one", source)
    await coordinator.request_handoff("one", target)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    source_store = _execution_store(location, source)
    target_store = _execution_store(location, target)
    try:
        async with _held(source_store, lease, _rename("one", "from source")) as holder:
            transfer = asyncio.create_task(coordinator.transfer("one"))
            await _until_blocked(control, holder, "transactionid", transfer)
            # The target's own transactions run meanwhile but see no project.
            assert (
                await asyncio.wait_for(target_store.list_entities(Engagement), SETTLE)
                == []
            )
        moved = await asyncio.wait_for(transfer, SETTLE)
        assert (moved.runtime_id, moved.fencing_token) == (
            target,
            lease.fencing_token + 1,
        )
        with execution_context(lease):
            with pytest.raises(LeaseLost):
                await source_store.update(Engagement, "one", {"description": "late"})
        with execution_context(moved):
            assert (
                await target_store.get(Engagement, "one")
            ).description == "from source"
    finally:
        await _close(source_store, target_store)


async def test_raw_writes_take_the_shared_row_key_only_for_shared_rows(fleet):
    control, _, runtimes, leases = fleet

    async def alpha_write(transaction):
        await transaction.update(ProviderProfile, "shared-a", {"name": "held"})

    async def raw_update(entity_id):
        with execution_context(leases["beta"]):
            async with runtimes["beta"].transaction() as transaction:
                await transaction.session.execute(
                    update(EntityRow)
                    .where(EntityRow.id == entity_id)
                    .values(updated_at=utc_now())
                )

    async with _held(runtimes["alpha"], leases["alpha"], alpha_write) as holder:
        # A project row never waits for another project's shared-row write.
        await asyncio.wait_for(raw_update("beta"), SETTLE)
        shared = asyncio.create_task(raw_update("shared-b"))
        await _until_blocked(control, holder, "advisory", shared)
    await asyncio.wait_for(shared, SETTLE)


async def test_a_shared_row_loaded_earlier_is_read_again_before_its_update(fleet):
    control, _, runtimes, leases = fleet
    loaded, changed = asyncio.Event(), asyncio.Event()

    async def beta_writes():
        with execution_context(leases["beta"]):
            async with runtimes["beta"].transaction() as transaction:
                early = await transaction.session.get(EntityRow, "shared-a")
                loaded.set()
                await changed.wait()
                await transaction.update(ProviderProfile, "shared-b", {"name": "b"})
                await transaction.update(ProviderProfile, "shared-a", {"name": "beta"})
                return early

    beta = asyncio.create_task(beta_writes())
    await asyncio.wait_for(loaded.wait(), SETTLE)
    try:
        # Bounded, so a regression to one global key fails instead of hanging.
        with execution_context(leases["alpha"]):
            await asyncio.wait_for(
                runtimes["alpha"].update(
                    ProviderProfile, "shared-a", {"metadata": {"from": "alpha"}}
                ),
                SETTLE,
            )
    finally:
        changed.set()
    await asyncio.wait_for(beta, SETTLE)
    shared = await control.get(ProviderProfile, "shared-a")
    assert (shared.name, shared.metadata) == ("beta", {"from": "alpha"})


async def test_shared_row_order_is_a_no_op_outside_postgres_workers(location, tmp_path):
    control = NebulaStore(Database(location, pool_settings=_pool()))
    runtime = None
    try:
        await control.create(Engagement(id="alpha", name="alpha"))
        coordinator = RuntimeCoordination(control.database)
        await coordinator.register_runtime("runtime-alpha", "fixture")
        lease = await coordinator.claim_project("alpha", "runtime-alpha")
        runtime = _execution_store(location, "runtime-alpha")
        async with control.transaction() as transaction:
            assert await transaction.lock_shared_rows() is False
        with execution_context(lease):
            async with runtime.transaction() as transaction:
                took = await transaction.lock_shared_rows()
                again = await transaction.lock_shared_rows()
        # SQLite's BEGIN IMMEDIATE already admits one execution writer.
        assert (took, again) == (isinstance(location, str), False)
    finally:
        await _close(*([runtime] if runtime else []), control)


# hashtext('operation:' || COLLIDING_OPERATION) equals the execution key itself.
COLLIDING_OPERATION = "6f1c2d3e-4a5b-4c6d-8e7f-00002f6543dd"


async def test_an_id_hashing_to_the_execution_key_never_upgrades_it(fleet):
    control, _, runtimes, leases = fleet
    async with control.database.session() as session:
        collision = await session.scalar(
            text(
                "SELECT hashtext('operation:' || :id) = "
                "hashtext('nebula:execution-sql:v1')"
            ),
            {"id": COLLIDING_OPERATION},
        )
    assert collision
    alpha = runtimes["alpha"]
    entered, go = asyncio.Event(), asyncio.Event()
    holder = []

    async def first():
        with execution_context(leases["alpha"]):
            async with alpha.transaction() as transaction:
                await transaction.update(Engagement, "alpha", {"description": "1"})
                holder.append(await _pid(transaction.session))
                entered.set()
                await go.wait()
                # Held shared since BEGIN; an exclusive request on the same
                # key would deadlock with the waiter below.
                await transaction.lock_operation_stream(COLLIDING_OPERATION)

    async def second():
        with execution_context(leases["alpha"]):
            await alpha.update(Engagement, "alpha", {"tags": ["two"]})

    one = asyncio.create_task(first())
    await asyncio.wait_for(entered.wait(), SETTLE)
    two = asyncio.create_task(second())
    await _until_blocked(control, holder[0], "advisory", two)
    go.set()
    await asyncio.wait_for(asyncio.gather(one, two), SETTLE)


@pytest.mark.parametrize("existing", [True, False])
async def test_two_anchor_projects_record_the_global_butler_check_once(
    location, tmp_path, monkeypatch, existing
):
    import nebula.v3.butler_recovery as butler_recovery
    from datetime import timedelta
    from nebula.v3.butler_authority import policy_id
    from nebula.v3.butler_oversight import project_state_id
    from nebula.v3.butler_recovery import ButlerRecoveryService, GLOBAL_STATE_ID
    from nebula.v3.domain import (
        ButlerAuthorityPolicy,
        ButlerGlobalState,
        ButlerProjectState,
        ChatSession,
    )

    _postgres_only(location)
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Check idle work.", encoding="utf-8")
    control = NebulaStore(Database(location, pool_settings=_pool()))
    runtimes = {}
    try:
        provider = await control.create(
            ProviderProfile(name="Fixture", provider_type="ollama", is_local=True)
        )
        worker_id = "worker-1"
        for project in ("alpha", "beta"):
            await control.create(Engagement(id=project, name=project))
            await control.create(
                ButlerAuthorityPolicy(
                    id=policy_id(project), engagement_id=project, authority="act"
                )
            )
            await control.create(
                ButlerProjectState(
                    id=project_state_id(project),
                    engagement_id=project,
                    lease_owner=worker_id,
                    lease_expires_at=utc_now() + timedelta(minutes=10),
                )
            )
            await control.create(
                ChatSession(
                    engagement_id=project,
                    title="Butler",
                    model="fixture-model",
                    provider_profile_id=provider.id,
                    metadata={"control_assistant": True},
                )
            )
        if existing:
            await control.create(
                ButlerGlobalState(
                    id=GLOBAL_STATE_ID,
                    last_periodic_at=utc_now() - timedelta(hours=1),
                    last_project_id="alpha",
                )
            )
        coordinator = RuntimeCoordination(control.database)
        leases = {}
        for project in ("alpha", "beta"):
            runtime_id = "runtime-" + project
            await coordinator.register_runtime(runtime_id, "fixture")
            leases[project] = await coordinator.claim_project(project, runtime_id)
            runtimes[project] = _execution_store(location, runtime_id)

        # Each worker sees only its own project, so both act as the anchor.
        # Hold the first recorder until the second has also read, if it can.
        arrived, both = [], asyncio.Event()
        original = butler_recovery._append_project_event

        async def gated(*args, **kwargs):
            arrived.append(1)
            if len(arrived) >= 2:
                both.set()
            try:
                await asyncio.wait_for(both.wait(), 2.0)
            except TimeoutError:
                pass
            return await original(*args, **kwargs)

        monkeypatch.setattr(butler_recovery, "_append_project_event", gated)

        async def fire(project):
            service = ButlerRecoveryService(
                runtimes[project], instructions_root=tmp_path
            )
            with execution_context(leases[project]):
                assert await service._global_anchor() == project
                return await service.fire_global_periodic_check(project, worker_id)

        results = await asyncio.wait_for(
            asyncio.gather(fire("alpha"), fire("beta")), 30
        )
        assert sorted(results) == [False, True]
        assert len(arrived) == 1
    finally:
        await _close(*runtimes.values(), control)
