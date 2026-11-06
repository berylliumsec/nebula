"""Fence admission ownership and always return capacity through cancellation."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.database import Database, ProviderTurnQueueRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, ChatTurnStatus, Engagement, ProviderProfile
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_async_core import (
    reject_synchronous_database_waits as reject_synchronous_database_waits,
)
from tests.v3.test_async_postgresql import postgresql_url as postgresql_url
from tests.v3.test_chat import FakeProvider

pytestmark = pytest.mark.anyio


async def _turn(store):
    turn = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="chat",
            provider_profile_id="provider",
            model="model-a",
            status=ChatTurnStatus.QUEUED,
            capacity_lane="background",
        )
    )
    scheduler = ProviderScheduler(
        store,
        worker_id="same-core",
        config=ProviderSchedulerConfig(provider_limit=1, background_limit=1),
    )
    await scheduler.enqueue(turn)
    return turn, scheduler


async def _chat(store):
    project = await store.create(Engagement(name="Synthetic cancellation"))
    profile = await store.create(
        ProviderProfile(
            name="Inert",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    prepared = await service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=project.id,
            model="model-a",
            messages=[{"role": "user", "content": "One synthetic response."}],
            include_knowledge=False,
            stream=True,
        )
    )
    return service, prepared, provider


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_release_failure_returns_capacity_exactly_once(
    tmp_path, monkeypatch, failure
):
    store = NebulaStore(tmp_path / "release.db")
    turn, scheduler = await _turn(store)
    admission = await scheduler.admit(turn.id)
    calls = 0

    async def failed(*_args):
        nonlocal calls
        calls += 1
        raise failure("synthetic release failure")

    monkeypatch.setattr(scheduler, "_complete", failed)
    for _ in range(2):
        with pytest.raises(failure):
            await admission.release(turn.id)
        assert scheduler._provider._value == scheduler._background._value == 1
    assert calls == 1


async def test_release_joins_durable_cleanup_after_repeated_cancellation(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "release.db")
    turn, scheduler = await _turn(store)
    admission = await scheduler.admit(turn.id)
    entered, proceed = asyncio.Event(), asyncio.Event()
    complete = scheduler._complete

    async def held(*args):
        entered.set()
        await proceed.wait()
        await complete(*args)

    monkeypatch.setattr(scheduler, "_complete", held)
    task = asyncio.create_task(admission.release(turn.id))
    await entered.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    assert scheduler._provider._value == scheduler._background._value == 0
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler._provider._value == scheduler._background._value == 1
    async with store.database.session() as session:
        row = await session.get(ProviderTurnQueueRow, turn.id)
        assert row.state != "running" and row.lease_owner is None


async def test_old_release_cannot_close_a_new_admission_from_the_same_core(tmp_path):
    store = NebulaStore(tmp_path / "release.db")
    turn, scheduler = await _turn(store)
    first = await scheduler.admit(turn.id)
    await first.release(turn.id)
    await scheduler.enqueue(await store.get(ChatTurn, turn.id))
    second = await scheduler.admit(turn.id)
    assert first.lease_owner != second.lease_owner
    await scheduler._complete(turn.id, first.lease_owner)
    async with store.database.session() as session:
        row = await session.get(ProviderTurnQueueRow, turn.id)
        assert row.state == "running" and row.lease_owner == second.lease_owner
    assert scheduler.admitted(turn.id)
    await second.release(turn.id)


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_postcommit_cancellation_reconciles_before_same_process_resume(
    tmp_path, postgresql_url, monkeypatch, dialect
):
    database = Database(
        postgresql_url if dialect == "postgresql" else tmp_path / "commit.db",
        pool_settings=PoolSettings(5, 12, 2, requested_size=2)
        if dialect == "postgresql"
        else None,
    )
    store = NebulaStore(database)
    service, prepared, provider = await _chat(store)
    committed = asyncio.Event()
    original = database.session
    gated = False

    @asynccontextmanager
    async def after_commit(*args, **kwargs):
        nonlocal gated
        async with original(*args, **kwargs) as session:
            yield session
            admitting = any(
                isinstance(row, ProviderTurnQueueRow) and row.state == "running"
                for row in session.identity_map.values()
            )
        if admitting and not gated:
            gated = True
            committed.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(database, "session", after_commit)
    turn_id = await service.start_provider_turn(prepared, automatic_recovery=True)
    runtime = service._active_provider_turns[turn_id]
    await asyncio.wait_for(committed.wait(), 5)
    service.shutting_down = True
    runtime.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runtime.task
    assert runtime.done
    assert (await store.get(ChatTurn, turn_id)).status == ChatTurnStatus.INTERRUPTED
    scheduler = service.provider_scheduler
    assert not scheduler.admitted(turn_id)
    assert scheduler._provider._value == scheduler.config.provider_limit
    assert scheduler._background._value == scheduler.config.background_limit
    assert service._automatic_recovery_slots._value == 2
    async with database.session() as session:
        row = await session.get(ProviderTurnQueueRow, turn_id)
        assert row.state != "running" and row.lease_owner is None
    assert database.pool_snapshot()["checked_out"] == 0
    assert not provider.requests
    # The same scheduler can accept the recovered turn; no Core restart is
    # needed to clear the old running lease, and no provider/tool effect ran.
    await scheduler.enqueue(await store.get(ChatTurn, turn_id))
    resumed = await scheduler.admit(turn_id)
    await resumed.release(turn_id)


async def test_cancellation_consumes_token_completed_while_position_waits(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "position.db")
    service, prepared, provider = await _chat(store)
    scheduler = service.provider_scheduler
    position = scheduler.position
    waiting = asyncio.Event()
    calls = 0

    async def held(turn_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await position(turn_id)
        while not scheduler.admitted(turn_id):
            await asyncio.sleep(0)
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "position", held)
    turn_id = await service.start_provider_turn(prepared, automatic_recovery=True)
    runtime = service._active_provider_turns[turn_id]
    await asyncio.wait_for(waiting.wait(), 5)
    service.shutting_down = True
    runtime.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runtime.task
    assert runtime.done and not scheduler.admitted(turn_id)
    assert scheduler._provider._value == scheduler.config.provider_limit
    assert service._automatic_recovery_slots._value == 2
    assert not provider.requests


async def test_release_error_still_releases_recovery_capacity_and_signals_done(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "runner.db")
    service, prepared, _provider = await _chat(store)
    scheduler = service.provider_scheduler

    async def failed(*_args):
        raise RuntimeError("synthetic release failure")

    monkeypatch.setattr(scheduler, "_complete", failed)
    turn_id = await service.start_provider_turn(prepared, automatic_recovery=True)
    runtime = service._active_provider_turns[turn_id]
    await asyncio.wait_for(asyncio.gather(runtime.task, return_exceptions=True), 10)
    assert runtime.done and runtime.error is not None
    assert scheduler._provider._value == scheduler.config.provider_limit
    assert service._automatic_recovery_slots._value == 2
