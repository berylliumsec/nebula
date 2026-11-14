"""An open project view is explicit worker demand; reads alone never are."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from nebula.v3.database import (
    DeploymentHostRow,
    ProjectAdmissionHoldRow,
    ProjectOwnershipRow,
    ProjectViewLeaseRow,
)
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_policy import MemoryBudgetError
from nebula.v3.runtime_auto_admission import (
    MAX_IDLE_WORKER_GRACE_SECONDS,
    RuntimeAutoAdmission,
)
from nebula.v3.runtime_coordination import CoordinationConflict, RuntimeCoordination
from nebula.v3.runtime_ipc import ProjectRuntimeUnavailable
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_view_leases import (
    MAX_VIEW_LEASES_PER_ACTOR,
    VIEW_LEASE_RETAIN_MS,
    ProjectViewLeases,
    ViewLeaseRefused,
)
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_work_batches import TOKEN, batch as batch, runtime
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _database_clock(value):
    # Leases expire on database time; use it here instead of the batch
    # fixture's manual counter so admission and expiry agree.
    value.coordination._clock_ms = RuntimeCoordination._clock_ms.__get__(
        value.coordination
    )


async def _db_now(value) -> int:
    async with value.database.session() as session:
        return await value.coordination._clock_ms(session)


async def _expire(value, lease_id, *, ago_ms=1_000):
    now = await _db_now(value)
    async with value.database.session() as session:
        await session.execute(
            update(ProjectViewLeaseRow)
            .where(ProjectViewLeaseRow.lease_id == lease_id)
            .values(expires_at_ms=now - ago_ms)
        )


def _admission(value, *, prepared, retired, prepare_error=None, refusals=None):
    async def prepare(_version):
        if prepare_error is not None:
            raise prepare_error
        if refusals:
            raise refusals[0]
        runtime_id = f"viewed-worker-{len(prepared)}"
        prepared.append(runtime_id)
        await runtime(value, runtime_id)
        return SimpleNamespace(runtime_id=runtime_id)

    async def attach(project_id, runtime_id):
        return await value.coordination.claim_project(project_id, runtime_id)

    async def retire_idle_project(project_id):
        retired.append(project_id)
        async with value.database.session() as session:
            (await session.get(ProjectOwnershipRow, project_id)).runtime_id = None
        return True

    async def reconcile_idle_project_retirements():
        return []

    return RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=value.coordinator,
            store=value.store,
            prepare_execution=prepare,
            attach_execution=attach,
            host=SimpleNamespace(
                retire_idle_project=retire_idle_project,
                reconcile_idle_project_retirements=reconcile_idle_project_retirements,
            ),
        ),
        default_version="supported",
    )


async def _api_host(value):
    async with value.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "synthetic-api"


async def test_hold_expires_on_database_time_and_a_replayed_put_renews_or_recreates(
    batch,
):
    _database_clock(batch)
    leases = ProjectViewLeases(batch.database, batch.coordination)
    before = await _db_now(batch)
    first = await leases.hold("operator", "one", "view-lease-0001")
    assert first["project_id"] == "one" and first["ttl_seconds"] == 90
    assert first["renew_after_seconds"] == 30
    assert first["runtime"] == {"state": "starting"}
    async with batch.database.session() as session:
        row = await session.get(ProjectViewLeaseRow, ("operator", "view-lease-0001"))
        assert before + 89_000 <= row.expires_at_ms <= before + 91_000 + 5_000
        # Holding a view gives the project its fencing counter for admission.
        assert await session.get(ProjectOwnershipRow, "one") is not None
    # A replay of the same PUT is the renewal; after expiry it recreates.
    await _expire(batch, "view-lease-0001")
    again = await leases.hold("operator", "one", "view-lease-0001", 60)
    async with batch.database.session() as session:
        rows = list(await session.scalars(select(ProjectViewLeaseRow)))
    assert len(rows) == 1 and rows[0].expires_at_ms > await _db_now(batch) + 50_000
    assert again["ttl_seconds"] == 60
    # Release ends the lease now (the idle grace runs from here); a repeat
    # changes nothing.
    await leases.release("operator", "one", "view-lease-0001")
    async with batch.database.session() as session:
        ended = (await session.scalars(select(ProjectViewLeaseRow))).one()
    assert ended.expires_at_ms <= await _db_now(batch)
    await leases.release("operator", "one", "view-lease-0001")
    async with batch.database.session() as session:
        repeat = (await session.scalars(select(ProjectViewLeaseRow))).one()
    assert repeat.expires_at_ms == ended.expires_at_ms
    # A new view of the project supersedes the lapsed one, and leases no
    # idle grace can read any more are pruned.
    await leases.hold("device:phone", "two", "view-lease-0003")
    await _expire(batch, "view-lease-0003", ago_ms=VIEW_LEASE_RETAIN_MS + 1_000)
    await leases.hold("operator", "one", "view-lease-0002")
    async with batch.database.session() as session:
        kept = list(await session.scalars(select(ProjectViewLeaseRow)))
    assert [(row.actor_id, row.lease_id) for row in kept] == [
        ("operator", "view-lease-0002")
    ]
    assert VIEW_LEASE_RETAIN_MS >= MAX_IDLE_WORKER_GRACE_SECONDS * 1000


async def test_lease_refusals_are_bounded_and_explained(batch):
    _database_clock(batch)
    leases = ProjectViewLeases(batch.database, batch.coordination)
    cases = [
        (("operator", "missing", "view-lease-0001"), 404),
        (("operator", "one", "bad id"), 422),
        (("operator", "one", "view-lease-0001", 5), 422),
        (("operator", "one", "view-lease-0001", "90"), 422),
    ]
    for args, status in cases:
        with pytest.raises(ViewLeaseRefused) as refused:
            await leases.hold(*args)
        assert refused.value.status == status
    archived = await batch.store.create(
        Engagement(name="Archived view", status="archived")
    )
    with pytest.raises(ViewLeaseRefused) as refused:
        await leases.hold("operator", archived.id, "view-lease-0001")
    assert refused.value.status == 409
    await leases.hold("operator", "one", "view-lease-0001")
    # A lease names one project for its whole life.
    with pytest.raises(ViewLeaseRefused) as refused:
        await leases.hold("operator", "two", "view-lease-0001")
    assert refused.value.status == 409
    for index in range(2, MAX_VIEW_LEASES_PER_ACTOR + 1):
        await leases.hold("device:phone", "two", f"view-lease-{index:04d}")
    await leases.hold("device:phone", "two", "view-lease-0001")
    with pytest.raises(ViewLeaseRefused) as refused:
        await leases.hold("device:phone", "two", "view-lease-9999")
    assert refused.value.status == 429
    # Lapsed leases do not count; another actor has its own allowance.
    await _expire(batch, "view-lease-0002")
    await leases.hold("device:phone", "two", "view-lease-9999")
    await leases.hold("device:tablet", "two", "view-lease-9999")


async def test_a_live_view_admits_the_project_and_a_lapsed_one_lets_it_retire(batch):
    _database_clock(batch)
    await _api_host(batch)
    prepared, retired = [], []
    automatic = _admission(batch, prepared=prepared, retired=retired)
    routing = RuntimeRouting(
        batch.coordination, batch.host.credentials.directory, request_admission=True
    )
    read = {"path": "/api/v1/engagements/one/observations", "method": "GET"}
    # A read alone never admits a worker; the project is idle.
    with pytest.raises(ProjectRuntimeUnavailable) as idle:
        await routing.upstream_for(read)
    assert idle.value.reason == "idle"
    await automatic.reconcile()
    assert prepared == []

    leases = ProjectViewLeases(batch.database, batch.coordination)
    await leases.hold("operator", "one", "view-lease-0001")
    with pytest.raises(ProjectRuntimeUnavailable) as starting:
        await routing.upstream_for(read)
    assert starting.value.reason == "starting"
    await automatic.reconcile()
    assert len(prepared) == 1
    async with batch.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "one")).runtime_id == prepared[0]
    # Only the viewed project: another project stays without a worker.
    async with batch.database.session() as session:
        two = await session.get(ProjectOwnershipRow, "two")
        assert two is None or two.runtime_id is None

    # While the view stays open the worker is not idle.
    await automatic.reconcile()
    assert retired == []
    # Closing the view starts the host's idle grace (#1062); it retires after.
    automatic.idle_grace_ms = 60_000
    await leases.release("operator", "one", "view-lease-0001")
    await automatic.reconcile()
    assert retired == []
    await _expire(batch, "view-lease-0001", ago_ms=61_000)
    await automatic.reconcile()
    assert retired == ["one"]


async def test_a_closed_view_spends_no_grace_while_another_project_waits(batch):
    _database_clock(batch)
    await _api_host(batch)
    prepared, retired, refusals = [], [], []
    automatic = _admission(batch, prepared=prepared, retired=retired, refusals=refusals)
    automatic.idle_grace_ms = 60_000
    leases = ProjectViewLeases(batch.database, batch.coordination)
    await leases.hold("operator", "two", "view-lease-0002")
    await automatic.reconcile()
    assert len(prepared) == 1
    await leases.release("operator", "two", "view-lease-0002")
    await automatic.reconcile()
    assert retired == []
    # Another viewed project cannot get a worker: like #1062's command grace,
    # a closed view's grace gives its worker's memory back at once.
    refusals.append(MemoryBudgetError("Core reserve is below the minimum."))
    await leases.hold("operator", "one", "view-lease-0001")
    await automatic.reconcile()
    await automatic.reconcile()
    assert retired == ["two"]


async def test_a_view_lease_never_bypasses_the_memory_budget(batch):
    _database_clock(batch)
    await _api_host(batch)
    prepared, retired = [], []
    refusal = MemoryBudgetError(
        "Core reserve is below the resident-runtime minimum. Increase Core reserve."
    )
    automatic = _admission(
        batch, prepared=prepared, retired=retired, prepare_error=refusal
    )
    leases = ProjectViewLeases(batch.database, batch.coordination)
    await leases.hold("operator", "one", "view-lease-0001")
    await automatic.reconcile()
    assert prepared == []
    async with batch.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "one")).runtime_id is None
        hold = await session.get(ProjectAdmissionHoldRow, "one")
        assert hold.reason.startswith("Core reserve is below")
    # The view says why it waits, once, instead of failing every read.
    routing = RuntimeRouting(
        batch.coordination, batch.host.credentials.directory, request_admission=True
    )
    with pytest.raises(ProjectRuntimeUnavailable) as held:
        await routing.upstream_for(
            {"path": "/api/v1/engagements/one/observations", "method": "GET"}
        )
    assert held.value.reason == "held" and "Core reserve" in str(held.value)
    renewed = await leases.hold("operator", "one", "view-lease-0001")
    assert renewed["runtime"]["state"] == "held"


async def test_lease_routes_need_authentication_but_no_idempotency_key(batch):
    _database_clock(batch)
    client = await batch.client()
    path = "/api/v1/engagements/one/view-leases/view-lease-0001"
    assert (await client.put(path, json={})).status_code == 401
    held = await client.put(
        path, json={"ttl_seconds": 90}, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert held.status_code == 200, held.text
    assert held.json()["runtime"]["state"] in {"starting", "held", "ready"}
    replay = await client.put(path, headers={"Authorization": "Bearer " + TOKEN})
    assert replay.status_code == 200
    refused = await client.put(
        "/api/v1/engagements/missing/view-leases/view-lease-0001",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert refused.status_code == 404
    unknown = await client.put(
        path, json={"admit": True}, headers={"Authorization": "Bearer " + TOKEN}
    )
    assert unknown.status_code == 422
    released = await client.delete(path, headers={"Authorization": "Bearer " + TOKEN})
    assert released.status_code == 204
    async with batch.database.session() as session:
        ended = (await session.scalars(select(ProjectViewLeaseRow))).one()
    assert ended.expires_at_ms <= await _db_now(batch)


async def test_a_project_worker_cannot_write_view_leases_or_holds(execution):
    _control, runtime_store, _coordinator, lease = execution
    with execution_context(lease):
        for statement in (
            insert(ProjectViewLeaseRow).values(
                actor_id="operator",
                lease_id="view-lease-0001",
                project_id="mine",
                expires_at_ms=1,
                renewed_at_ms=1,
            ),
            insert(ProjectAdmissionHoldRow).values(
                project_id="mine", reason="forged", attempts=1, updated_at_ms=1
            ),
        ):
            with pytest.raises(CoordinationConflict, match="host coordination state"):
                async with runtime_store.database.session() as session:
                    await session.execute(statement)


def test_lease_tables_are_additive_and_downgrade_to_the_previous_schema(tmp_path):
    # An older release never reads these tables; a downgrade leaves its schema.
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from tests.v3.test_migrations import _run_migration

    engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}", future=True)
    try:
        _run_migration(engine, command.upgrade, "head")
        tables = set(inspect(engine).get_table_names())
        assert {"project_view_leases", "project_admission_holds"} <= tables
        _run_migration(engine, command.downgrade, "0048_chat_scoped_admission")
        after = set(inspect(engine).get_table_names())
        assert not {"project_view_leases", "project_admission_holds"} & after
        assert tables - after == {"project_view_leases", "project_admission_holds"}
        _run_migration(engine, command.upgrade, "head")
    finally:
        engine.dispose()
