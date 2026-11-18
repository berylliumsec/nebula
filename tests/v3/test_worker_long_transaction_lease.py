"""A worker's own long transaction never costs it its lease, and fencing holds.

Project-scoped execution transactions hold their ownership rows key-shared, so
the heartbeat's renewal and API command submission (non-key updates) proceed
while one runs, while claims and transfers (FOR UPDATE) still wait for it.
"""

import asyncio
import re
import time

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from nebula.v3 import execution_scope, runtime_coordination
from nebula.v3.database import EntityRow, ProjectOwnershipRow
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import ExecutionTransactionTooLong, execution_context
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LeaseLost,
    LeaseRenewal,
)
from nebula.v3.runtime_worker import keep_lease
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _postgres_only(runtime):
    if runtime.database.engine.dialect.name != "postgresql":
        pytest.skip("row-lock modes are PostgreSQL behavior; SQLite has one writer")


async def _lease_expiry(control, project_id="mine"):
    async with control.database.session() as session:
        return await session.scalar(
            select(ProjectOwnershipRow.lease_expires_at_ms).where(
                ProjectOwnershipRow.project_id == project_id
            )
        )


async def test_heartbeat_renews_while_the_worker_holds_a_long_transaction(execution):
    control, runtime, coordinator, lease = execution
    _postgres_only(runtime)
    before = await _lease_expiry(control)
    with execution_context(lease):
        async with runtime.database.session() as session:
            # The worker's own transaction is open and holds its ownership row.
            await session.execute(select(EntityRow.id).where(EntityRow.id == "mine"))
            await asyncio.sleep(0.05)
            async with asyncio.timeout(5):
                await coordinator.heartbeat("execution")
            assert await _lease_expiry(control) > before
            # API acceptance does not wait for the worker's transaction either.
            async with asyncio.timeout(5):
                receipt = await coordinator.submit(
                    project_id="mine",
                    actor_id="operator",
                    idempotency_key="accepted-while-busy",
                    name="project.rename/v1",
                    payload={"name": "renamed"},
                )
            assert receipt.project_id == "mine" and receipt.sequence >= 1
    assert coordinator.renewal_wait is None


async def test_stale_owner_cannot_commit_after_a_new_owner_claims(execution):
    control, runtime, coordinator, lease = execution
    _postgres_only(runtime)
    await coordinator.register_runtime("successor", "fixture")
    with pytest.raises(LeaseLost):
        with execution_context(lease):
            async with runtime.database.session() as session:
                await session.execute(
                    update(EntityRow)
                    .where(EntityRow.id == "turn-mine")
                    .values(revision=77)
                )
                # The lease lapses while the write is uncommitted. A lease
                # change of non-key columns does not wait for the worker.
                async with asyncio.timeout(5):
                    async with control.database.session() as host:
                        await host.execute(
                            update(ProjectOwnershipRow)
                            .where(ProjectOwnershipRow.project_id == "mine")
                            .values(lease_expires_at_ms=0)
                        )
                # A claim waits for the stale transaction instead of granting
                # a new fence underneath it.
                claim = asyncio.create_task(
                    coordinator.claim_project("mine", "successor")
                )
                await asyncio.sleep(0.5)
                assert not claim.done()
                await session.commit()
    successor = await asyncio.wait_for(claim, 10)
    assert successor.fencing_token == lease.fencing_token + 1
    async with control.database.session() as session:
        assert (
            await session.scalar(
                select(EntityRow.revision).where(EntityRow.id == "turn-mine")
            )
            == 1
        )


async def test_claim_ordered_before_commit_check_fences_the_stale_write(execution):
    control, runtime, coordinator, lease = execution
    await coordinator.register_runtime("successor", "fixture")
    async with control.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "mine")
            .values(lease_expires_at_ms=0)
        )
    await coordinator.claim_project("mine", "successor")
    with pytest.raises((LeaseLost, CoordinationConflict)):
        with execution_context(lease):
            await runtime.update(
                Engagement, "mine", {"name": "stale"}, expected_revision=1
            )
    assert (await control.get(Engagement, "mine")).name == "mine"


async def test_a_transaction_past_its_bound_is_abandoned(execution, monkeypatch):
    control, runtime, _, lease = execution
    monkeypatch.setattr(execution_scope, "EXECUTION_TRANSACTION_SECONDS", 0.3)
    # PostgreSQL's idle-in-transaction timeout may end it first.
    with pytest.raises((ExecutionTransactionTooLong, DBAPIError)):
        with execution_context(lease):
            async with runtime.database.session() as session:
                await session.execute(
                    update(EntityRow)
                    .where(EntityRow.id == "turn-mine")
                    .values(revision=55)
                )
                await asyncio.sleep(0.5)
                await session.execute(select(EntityRow.id).limit(1))
    async with control.database.session() as session:
        assert (
            await session.scalar(
                select(EntityRow.revision).where(EntityRow.id == "turn-mine")
            )
            == 1
        )
    # The next transaction starts fresh and works.
    with execution_context(lease):
        await runtime.update(Engagement, "mine", {"name": "next"}, expected_revision=1)
    assert (await control.get(Engagement, "mine")).name == "next"


async def test_one_slow_statement_is_cancelled_at_the_bound(execution, monkeypatch):
    control, runtime, _, lease = execution
    _postgres_only(runtime)
    monkeypatch.setattr(execution_scope, "EXECUTION_TRANSACTION_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(Exception, match="statement timeout"):
        with execution_context(lease):
            async with runtime.database.session() as session:
                await session.execute(text("SELECT pg_sleep(30)"))
    assert time.monotonic() - started < 10


async def test_renewal_blocked_on_a_lock_names_it(execution):
    control, runtime, coordinator, lease = execution
    _postgres_only(runtime)
    locked, release = asyncio.Event(), asyncio.Event()

    async def hold():
        # Its own task, so it has its own connection, as another process would.
        async with control.database.session() as holder:
            await holder.execute(
                select(func.set_config("application_name", "stuck-holder", True))
            )
            await holder.execute(
                select(ProjectOwnershipRow.project_id)
                .where(ProjectOwnershipRow.project_id == "mine")
                .with_for_update()
            )
            locked.set()
            await release.wait()

    async def queue_ahead():
        # A claim queued first: the renewal waits behind it, it behind the holder.
        async with control.database.session() as waiter:
            await waiter.execute(
                select(ProjectOwnershipRow.project_id)
                .where(ProjectOwnershipRow.project_id == "mine")
                .with_for_update()
            )

    holding = asyncio.create_task(hold())
    queued = None
    try:
        await locked.wait()
        queued = asyncio.create_task(queue_ahead())
        await asyncio.sleep(0.3)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(4):
                await coordinator.heartbeat("execution")
        assert coordinator.renewal_wait is not None
        assert "lock wait" in coordinator.renewal_wait
        assert "FOR UPDATE" in coordinator.renewal_wait.upper()
        # The session that holds the lock is named first, not a queued waiter.
        assert re.search(
            r"behind backend \d+ \(stuck-holder, idle in transaction",
            coordinator.renewal_wait,
        )
    finally:
        release.set()
        await holding
        if queued is not None:
            await queued
    async with asyncio.timeout(5):
        await coordinator.heartbeat("execution")
    assert coordinator.renewal_wait is None


async def test_lease_lost_to_a_lock_wait_does_not_blame_sql_reachability(
    monkeypatch,
):
    monkeypatch.setattr(runtime_coordination, "LEASE_MS", 6_000)
    monkeypatch.setattr(runtime_coordination, "LEASE_SAFETY_MS", 3_000)
    renewal, renewed = LeaseRenewal(), []

    async def renew():
        if renewed:
            await asyncio.sleep(60)
        renewed.append(time.monotonic())

    with pytest.raises(LeaseLost, match="blocked on a SQL lock") as raised:
        async with asyncio.timeout(10):
            await keep_lease(
                renew,
                renewal,
                interval=0.1,
                lock_wait=lambda: "lock wait (Lock:transactionid) behind backend 7",
            )
    assert "unreachable" not in str(raised.value)
    assert "backend 7" in str(raised.value)


async def test_stale_projection_repair_pages_in_a_worker(execution, monkeypatch):
    """A drifted search projection is repaired a bounded page per transaction."""
    from nebula.v3 import search
    from nebula.v3.action_registry import ActionRegistry
    from nebula.v3.database import SearchDocumentRow
    from nebula.v3.domain import Observation

    control, runtime, _, lease = execution
    monkeypatch.setattr(search, "REPAIR_PAGE", 3)
    with execution_context(lease):
        created = [
            await runtime.create(
                Observation(
                    engagement_id="mine", observation_type="service", title=f"obs {i}"
                )
            )
            for i in range(8)
        ]
    stale, missing = created[1], created[5]
    async with control.database.session() as session:
        await session.execute(
            update(EntityRow)
            .where(EntityRow.id == stale.id)
            .values(
                revision=2,
                payload={
                    **stale.model_dump(mode="json"),
                    "title": "renamed",
                    "revision": 2,
                },
            )
        )
        await session.execute(
            delete(SearchDocumentRow).where(SearchDocumentRow.id == missing.id)
        )
        template = await session.get(SearchDocumentRow, created[6].id)
        await session.execute(
            insert(SearchDocumentRow).values(
                id="gone-observation",
                project_id="mine",
                resource_kind=template.resource_kind,
                resource_id="gone-observation",
                revision=1,
                label="gone",
                description="",
                breadcrumb="",
                content="",
                updated_at=template.updated_at,
            )
        )
    transactions = []
    original = runtime.database.session

    def counted(*args, **kwargs):
        transactions.append(kwargs.get("bulk"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.database, "session", counted)
    finder = search.FederatedSearch(runtime, ActionRegistry(runtime))
    with execution_context(lease):
        assert await finder.repair_stale_projection() is True
    # Several bounded transactions, not one over the whole table.
    assert len(transactions) > 4
    async with control.database.session() as session:
        documents = {
            row.id: row
            for row in await session.scalars(
                select(SearchDocumentRow).where(SearchDocumentRow.project_id == "mine")
            )
        }
    assert documents[stale.id].revision == 2
    assert documents[stale.id].label == "renamed"
    assert missing.id in documents
    assert "gone-observation" not in documents
    assert {item.id for item in created} <= set(documents)
    with execution_context(lease):
        assert await finder.repair_stale_projection() is False


async def test_lease_lost_to_a_blocked_event_loop_says_so(monkeypatch):
    monkeypatch.setattr(runtime_coordination, "LEASE_MS", 6_000)
    monkeypatch.setattr(runtime_coordination, "LEASE_SAFETY_MS", 3_000)
    renewal, renewed = LeaseRenewal(), []

    async def renew():
        renewed.append(time.monotonic())

    async def hog():
        await asyncio.sleep(0.3)
        time.sleep(4.5)  # CPU-bound work holding the event loop

    blocker = asyncio.create_task(hog())
    with pytest.raises(LeaseLost, match="event loop was blocked") as raised:
        async with asyncio.timeout(15):
            await keep_lease(renew, renewal, interval=0.1)
    await blocker
    assert "unreachable" not in str(raised.value)


async def test_search_answers_from_a_partial_index_while_a_long_repair_runs(
    execution, monkeypatch
):
    from nebula.v3 import search
    from nebula.v3.action_registry import ActionRegistry
    from nebula.v3.domain import SearchScope

    _, runtime, _, lease = execution
    monkeypatch.setattr(search, "REPAIR_WAIT_SECONDS", 0.2)
    release, calls = asyncio.Event(), []

    async def slow_repair(self):
        calls.append(time.monotonic())
        await release.wait()
        return len(calls) == 1

    monkeypatch.setattr(search.FederatedSearch, "repair_stale_projection", slow_repair)
    finder = search.FederatedSearch(runtime, ActionRegistry(runtime))

    async def ask():
        with execution_context(lease):
            return await finder.search(
                query="mine",
                active_project="mine",
                scope=SearchScope.ACTIVE,
                kinds=[],
                cursor=None,
                limit=10,
            )

    started = time.monotonic()
    first = await asyncio.wait_for(ask(), 5)
    second = await asyncio.wait_for(ask(), 5)
    assert time.monotonic() - started < 3
    assert first.partial_index and second.partial_index
    # Both searches share the one running repair.
    assert len(calls) == 1
    release.set()
    third = await asyncio.wait_for(ask(), 5)
    assert third.partial_index is False
