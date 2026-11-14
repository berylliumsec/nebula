"""Workers ride out a short SQL outage only while their leases are provable."""

import asyncio
import time

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from nebula.v3 import runtime_coordination
from nebula.v3.database import (
    Database,
    ProjectOwnershipRow,
    RuntimeInstanceRow,
    transient_database_error,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement
from nebula.v3.runtime_coordination import (
    LeaseLost,
    LeaseRenewal,
    RuntimeCoordination,
)
from nebula.v3.runtime_worker import keep_lease
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 3, 5)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def short_leases(monkeypatch):
    """6 s SQL leases with a 3 s local bound, renewed every 0.1 s."""
    monkeypatch.setattr(runtime_coordination, "LEASE_MS", 6_000)
    monkeypatch.setattr(runtime_coordination, "LEASE_SAFETY_MS", 3_000)
    return 0.1


def refused():
    return OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))


class SqlOutage:
    """Refuse every new connection and drop pooled ones, like a stopped server."""

    def __init__(self, database: Database) -> None:
        self.database, self.down, self.refusals = database, False, 0
        event.listen(database.engine.sync_engine, "do_connect", self._connect)

    def _connect(self, dialect, _record, _args, _params):
        if self.down:
            self.refusals += 1
            raise dialect.loaded_dbapi.OperationalError("synthetic connection refused")

    async def start(self) -> None:
        self.down = True
        await self.database.engine.dispose()

    def stop(self) -> None:
        self.down = False

    def close(self) -> None:
        event.remove(self.database.engine.sync_engine, "do_connect", self._connect)


def test_only_connection_level_failures_are_transient():
    assert transient_database_error(refused())
    assert transient_database_error(ConnectionResetError())
    assert transient_database_error(TimeoutError())
    assert not transient_database_error(
        IntegrityError("INSERT", {}, ValueError("duplicate"))
    )
    assert not transient_database_error(LeaseLost("expired"))
    assert not transient_database_error(ValueError("bad"))


def test_renewal_bound_starts_unproven_and_counts_from_the_acknowledged_start(
    short_leases,
):
    now = [100.0]
    renewal = LeaseRenewal(clock=lambda: now[0])
    assert renewal.remaining() == 0 and not renewal.spent
    renewal.renewed(100.0)
    now[0] = 102.0
    assert renewal.remaining() == pytest.approx(1.0)
    # A late acknowledgement of an older attempt never extends the bound.
    renewal.renewed(99.0)
    assert renewal.remaining() == pytest.approx(1.0)
    now[0] = 103.0
    assert renewal.remaining() == 0 and renewal.spent


async def test_transient_refusals_inside_the_bound_keep_renewing(short_leases):
    interval = short_leases
    renewal, calls, failing = LeaseRenewal(), [], [False]

    async def renew():
        calls.append(time.monotonic())
        if failing[0]:
            raise refused()

    keeper = asyncio.create_task(keep_lease(renew, renewal, interval=interval))
    try:
        await asyncio.sleep(0.35)
        failing[0] = True
        await asyncio.sleep(0.8)
        assert not keeper.done()
        failing[0] = False
        resumed = time.monotonic()
        await asyncio.sleep(0.4)
        assert not keeper.done()
        assert renewal.renewed_at is not None and renewal.renewed_at >= resumed
    finally:
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)


async def test_an_outage_longer_than_the_bound_stops_before_the_lease_expires(
    short_leases,
):
    renewal, renewed = LeaseRenewal(), []

    async def renew():
        if renewed:
            raise refused()
        renewed.append(time.monotonic())

    with pytest.raises(LeaseLost, match="could no longer prove its lease") as raised:
        async with asyncio.timeout(5):
            await keep_lease(renew, renewal, interval=short_leases)
    elapsed = time.monotonic() - renewed[0]
    assert 2.95 <= elapsed < 6.0
    assert isinstance(raised.value.__cause__, OperationalError)


async def test_an_unanswered_renewal_is_abandoned_at_the_bound(short_leases):
    renewal, renewed = LeaseRenewal(), []

    async def renew():
        if renewed:
            await asyncio.Event().wait()
        renewed.append(time.monotonic())

    with pytest.raises(LeaseLost):
        async with asyncio.timeout(5):
            await keep_lease(renew, renewal, interval=short_leases)
    assert 2.95 <= time.monotonic() - renewed[0] < 6.0


@pytest.mark.parametrize(
    "answer",
    [
        LeaseLost("runtime lease expired"),
        IntegrityError("UPDATE", {}, ValueError("constraint")),
    ],
)
async def test_definite_answers_from_sql_stop_at_once(short_leases, answer):
    renewal, calls = LeaseRenewal(), []

    async def renew():
        calls.append(1)
        if len(calls) > 1:
            raise answer

    with pytest.raises(type(answer)):
        async with asyncio.timeout(5):
            await keep_lease(renew, renewal, interval=short_leases)
    assert len(calls) == 2


async def test_a_refusal_before_any_acknowledged_renewal_stops_as_before(
    short_leases,
):
    async def renew():
        raise refused()

    with pytest.raises(OperationalError):
        async with asyncio.timeout(5):
            await keep_lease(renew, LeaseRenewal(), interval=short_leases)


async def test_sessions_wait_for_sql_only_inside_the_installed_bound(location):
    database = Database(location, pool_settings=SETTINGS)
    store = NebulaStore(database)
    await database.initialize()
    outage = SqlOutage(database)
    try:
        await store.create(Engagement(id="before", name="Before the outage"))
        # Without a bound, a refused connection fails at once as before.
        await outage.start()
        with pytest.raises(OperationalError):
            await store.get(Engagement, "before")
        bound = [5.0]
        database.ride_out_outages(lambda: bound[0])
        write = asyncio.create_task(
            store.create(Engagement(id="during", name="Written after it"))
        )
        await asyncio.sleep(0.6)
        assert not write.done() and outage.refusals >= 3
        outage.stop()
        async with asyncio.timeout(5):
            await write
        assert (await store.get(Engagement, "during")).name == "Written after it"
        # A spent bound fails at once again.
        bound[0] = 0.0
        await outage.start()
        started = time.monotonic()
        with pytest.raises(OperationalError):
            await store.get(Engagement, "during")
        assert time.monotonic() - started < 1.0
    finally:
        outage.close()
        await store.event_history.close()
        await database.dispose()


async def test_real_heartbeat_rides_out_a_refused_database_and_never_resurrects(
    location, short_leases
):
    database = Database(location, pool_settings=SETTINGS)
    store = NebulaStore(database)
    await database.initialize()
    coordination = RuntimeCoordination(database)
    await store.create(Engagement(id="project", name="Owned project"))
    await coordination.register_runtime("worker", "fixture")
    lease = await coordination.claim_project("project", "worker")
    renewal = LeaseRenewal()
    database.ride_out_outages(renewal.remaining)
    outage = SqlOutage(database)

    async def expiries():
        outage.stop()
        async with database.session() as session:
            return (
                await session.scalar(
                    select(RuntimeInstanceRow.lease_expires_at_ms).where(
                        RuntimeInstanceRow.id == "worker"
                    )
                ),
                await session.scalar(
                    select(ProjectOwnershipRow.lease_expires_at_ms).where(
                        ProjectOwnershipRow.project_id == "project"
                    )
                ),
            )

    keeper = asyncio.create_task(
        keep_lease(
            lambda: coordination.heartbeat("worker"), renewal, interval=short_leases
        )
    )
    try:
        await asyncio.sleep(0.4)
        before = await expiries()
        await outage.start()
        await asyncio.sleep(0.8)
        assert not keeper.done(), keeper.exception()
        assert outage.refusals >= 1
        outage.stop()
        # Waiting sessions reconnect within their backoff and renew again.
        async with asyncio.timeout(5):
            while (after := await expiries())[0] <= before[0]:
                assert not keeper.done(), keeper.exception()
                await asyncio.sleep(0.05)
        assert after[1] > before[1]
        assert not keeper.done(), keeper.exception()
        async with coordination.fenced(lease):
            pass
        # An outage past the bound stops the keeper before the lease expires.
        await outage.start()
        with pytest.raises(LeaseLost):
            async with asyncio.timeout(10):
                await keeper
        outage.stop()
        async with coordination.fenced(lease):
            pass
        # Once SQL has expired the lease, a reconnected heartbeat is refused.
        async with database.session() as session:
            await session.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == "worker")
                .values(lease_expires_at_ms=1)
            )
            await session.execute(
                update(ProjectOwnershipRow)
                .where(ProjectOwnershipRow.project_id == "project")
                .values(lease_expires_at_ms=1)
            )
        with pytest.raises(LeaseLost):
            await coordination.heartbeat("worker")
        assert await expiries() == (1, 1)
        with pytest.raises(LeaseLost):
            async with coordination.fenced(lease):
                pass
    finally:
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)
        outage.close()
        await store.event_history.close()
        await database.dispose()
