"""Delegated Settings acceptance shares lock order with atomic Work batches.

Only SQL, encrypted delivery and inert canonical catalog handlers are used.
PostgreSQL checks the actual blocking backend; SQLite observes its writer wait.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import replace
import json

import pytest
from sqlalchemy import event, select, text

from nebula.v3.database import Database, DeploymentHostRow, RuntimeCommandRow
from nebula.v3.domain import Engagement, ProviderProfile
from nebula.v3.runtime_coordination import CoordinationConflict, LEASE_MS, LeaseLost
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.runtime_host_delivery import HostEffectDelivery
from nebula.v3.work import WorkProjectBatchUpdate
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_work_batches import (
    accepted,
    batch as batch,
    page,
    runtime,
    SETTINGS,
)

pytestmark = pytest.mark.anyio
_operation = ContextVar("work_lock_test_operation", default="")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=["sql", "host-effect"])
async def delegated(request, batch):
    application = RuntimeGlobalApi(batch.database)
    handler = (GlobalSqlDelivery if request.param == "sql" else HostEffectDelivery)(
        batch.delivery,
        application,
    )
    if isinstance(handler, HostEffectDelivery):
        await handler.start()
    try:
        yield handler
    finally:
        if isinstance(handler, HostEffectDelivery):
            await handler.close()
        await application.close()


def provider_intent(name):
    return HttpIntent.capture(
        "POST",
        "/api/v1/providers",
        b"",
        [(b"content-type", b"application/json")],
        json.dumps({"name": name, "provider_type": "vllm", "is_local": True}).encode(),
    )


async def settled(handler):
    if isinstance(handler, HostEffectDelivery):
        # Acceptance owns these tasks; joining doesn't re-dispatch an effect.
        async with asyncio.timeout(3):
            await asyncio.gather(*tuple(handler.tasks.values()))


@pytest.fixture
async def lock_observer(batch):
    if batch.database.engine.dialect.name != "postgresql":
        yield None
        return
    # The two application slots are intentionally occupied by a holder and
    # waiter. Charge this independent serial observer to declared OTHER users,
    # without enlarging the application's canonical write pool.
    database = Database(
        batch.location,
        bootstrap=False,
        pool_settings=replace(SETTINGS.auxiliary(), timeout=3),
    )
    try:
        async with asyncio.timeout(3):
            await database.initialize()
        yield database
    finally:
        async with asyncio.timeout(3):
            await database.dispose()


async def postgres_waits_on(database, blocked_pid, holder_pid):
    async with asyncio.timeout(3):
        while True:
            async with database.session() as session:
                blockers = await session.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": blocked_pid()}
                )
            if holder_pid in blockers:
                return
            await asyncio.sleep(0.01)


async def test_delegated_owner_hold_serializes_real_batch_without_deadlock(
    batch, delegated, lock_observer, monkeypatch
):
    await runtime(batch, "worker")
    lease = await batch.coordination.claim_project("one", "worker")
    receipt = await accepted(batch)
    body = WorkProjectBatchUpdate(project_ids=["one", "two"], work_enabled=True)
    assert await batch.service._stage(receipt.command_id, body)
    entered, release, batch_attempted = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    backend = {}
    original_check = batch.coordination._check
    original_locks = batch.service._locks
    postgres = batch.database.engine.dialect.name == "postgresql"

    async def check(session, current, *, lock):
        await original_check(session, current, lock=lock)
        if _operation.get() == "delegate" and lock and not entered.is_set():
            if postgres:
                backend["delegate"] = await session.scalar(
                    text("SELECT pg_backend_pid()")
                )
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=4)

    async def locks(session):
        if postgres and _operation.get() == "batch":
            backend["batch"] = await session.scalar(text("SELECT pg_backend_pid()"))
            batch_attempted.set()
        await original_locks(session)

    def before_cursor(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            not postgres
            and _operation.get() == "batch"
            and statement == "BEGIN IMMEDIATE"
        ):
            batch_attempted.set()

    monkeypatch.setattr(batch.coordination, "_check", check)
    monkeypatch.setattr(batch.service, "_locks", locks)
    event.listen(
        batch.database.engine.sync_engine, "before_cursor_execute", before_cursor
    )

    async def delegate():
        token = _operation.set("delegate")
        try:
            return await delegated.execute(
                provider_intent("Held delegate"),
                actor_id="operator",
                idempotency_key="held-delegate",
                authority=lease,
            )
        finally:
            _operation.reset(token)

    async def progress():
        token = _operation.set("batch")
        try:
            await batch.service.reconcile()
        finally:
            _operation.reset(token)

    first, second = None, None
    try:
        first = asyncio.create_task(delegate(), name="held-settings-delegate")
        await asyncio.wait_for(entered.wait(), timeout=3)
        second = asyncio.create_task(progress(), name="waiting-work-batch")
        await asyncio.wait_for(batch_attempted.wait(), timeout=3)
        if postgres:
            # Old code holds owner then waits global SQL; the batch holds that
            # global lock and waits owner. Release must not create this cycle.
            await postgres_waits_on(
                lock_observer, lambda: backend["batch"], backend["delegate"]
            )
        assert not second.done()
        release.set()
        async with asyncio.timeout(5):
            delegated_receipt, _ = await asyncio.gather(first, second)
            await settled(delegated)
        assert (await page(batch, receipt))[1:] == (200, {"updated": 2})
        assert (
            await batch.delivery.page("operator", delegated_receipt.command_id)
        ).state == "succeeded"
        assert await batch.store.count(ProviderProfile) == 1
        assert (await batch.store.get(Engagement, "one")).work_enabled
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        async with asyncio.timeout(5):
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )
        event.remove(
            batch.database.engine.sync_engine, "before_cursor_execute", before_cursor
        )


async def test_shutdown_replay_keeps_exact_live_authority_and_refuses_new_intent(
    batch, delegated
):
    await runtime(batch, "worker")
    lease = await batch.coordination.claim_project("one", "worker")
    request = provider_intent("Accepted before shutdown")
    receipt = await delegated.execute(
        request, actor_id="operator", idempotency_key="before-shutdown", authority=lease
    )
    await settled(delegated)
    async with batch.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).shutdown_requested = True
    replay = await delegated.execute(
        request, actor_id="operator", idempotency_key="before-shutdown", authority=lease
    )
    assert replay == receipt
    assert await batch.store.count(ProviderProfile) == 1
    with pytest.raises(CoordinationConflict, match="shutdown"):
        await delegated.execute(
            provider_intent("Must not appear"),
            actor_id="operator",
            idempotency_key="new-after-shutdown",
            authority=lease,
        )
    batch.clock[0] += LEASE_MS + 1
    with pytest.raises(LeaseLost):
        await delegated.execute(
            request,
            actor_id="operator",
            idempotency_key="before-shutdown",
            authority=lease,
        )
    async with batch.database.session() as session:
        commands = list(await session.scalars(select(RuntimeCommandRow)))
        assert len(commands) == 1 and commands[0].id == receipt.command_id
    assert (
        await batch.delivery.page("operator", receipt.command_id)
    ).state == "succeeded"
