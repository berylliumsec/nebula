"""Stop and failure reporting stay reliable while a harness turn's producer saves its records.

Stop and the producer of a turn write the same turn, owner and session
records from different tasks. These tests place one writer exactly between the
other's read and write, or stop the producer inside its start bookkeeping, so
each interleaving runs every time instead of once in a few runs. They use the
inert fixture adapter only; no vendor harness or account is involved.
"""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from nebula.v3.domain import (
    AgentRun,
    ChatTurn,
    ChatTurnStatus,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTurn,
    HarnessTurnStatus,
    RunBudget,
    RunStatus,
    utc_now,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    HarnessConnection,
    HarnessEvent,
    HarnessTransportError,
)
from nebula.v3.storage import ConflictError
from tests.v3.test_harnesses import FakeAdapter, FakeConnection, _runtime

DEADLINE_SECONDS = 20


class HeldConnection(FakeConnection):
    """Accepts a prompt, then works until the turn is stopped."""

    def __init__(self, request: AdapterOpenRequest) -> None:
        super().__init__(request)
        self.prompt_accepted = asyncio.Event()
        self.hold = True

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        if not self.hold:
            async for event in super().run_turn(prompt, model=model, images=images):
                yield event
            return
        self.prompts.append(prompt)
        yield HarnessEvent(type="started")
        self.prompt_accepted.set()
        await asyncio.Event().wait()


class HeldAdapter(FakeAdapter):
    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        connection = HeldConnection(request)
        self.connections.append(connection)
        return connection


async def _held_runtime(tmp_path):
    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    adapter = HeldAdapter()
    runtime.adapter_factory = lambda _: adapter
    return store, engagement, profile, adapter, runtime


async def _prepare_chat(runtime, engagement, profile, prompt="Work until stopped"):
    return await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt=prompt,
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )


async def _finished(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=DEADLINE_SECONDS)


async def _prompt_accepted(adapter: HeldAdapter) -> HeldConnection:
    async def accepted() -> HeldConnection:
        while not adapter.connections:
            await asyncio.sleep(0.01)
        await adapter.connections[0].prompt_accepted.wait()
        return adapter.connections[0]

    return await asyncio.wait_for(accepted(), timeout=DEADLINE_SECONDS)


def _producer_write(
    record: Any,
) -> dict[str, Any]:
    """A write the producer makes on this record while its turn runs."""

    if isinstance(record, ChatTurn):
        # _start_owner, after the start and after every released approval.
        return {"status": ChatTurnStatus.ROUTING, "approval_id": None}
    if isinstance(record, HarnessTurn):
        return {"metadata": {**record.metadata, "saved_by_producer": True}}
    assert isinstance(record, HarnessSession)
    return {"last_activity_at": utc_now()}


@pytest.mark.parametrize(
    ("model", "stop_status"),
    [
        (HarnessTurn, HarnessTurnStatus.CANCELLED),
        (ChatTurn, ChatTurnStatus.CANCELLED),
        (HarnessSession, HarnessSessionStatus.IDLE),
    ],
    ids=["harness-turn", "chat-turn", "harness-session"],
)
async def test_stop_succeeds_when_the_producer_saves_a_record_first(
    tmp_path, monkeypatch, model, stop_status
):
    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    _, chat_turn, turn = await _prepare_chat(runtime, engagement, profile)
    task = await runtime.start_chat_turn(turn.id)
    connection = await _prompt_accepted(adapter)

    update = store.update
    moved: list[str] = []

    async def producer_saves_first(
        entity_model, entity_id, changes, *, expected_revision=None
    ):
        if entity_model is model and changes.get("status") == stop_status and not moved:
            # Stop has read this record and is about to write it. The
            # producer's own bookkeeping lands in between.
            moved.append(entity_id)
            current = await store.get(entity_model, entity_id)
            await update(
                entity_model,
                entity_id,
                _producer_write(current),
                expected_revision=current.revision,
            )
        return await update(
            entity_model, entity_id, changes, expected_revision=expected_revision
        )

    monkeypatch.setattr(store, "update", producer_saves_first)

    stopped = await asyncio.wait_for(
        runtime.cancel_turn(turn.id, reason="Operator stop"), timeout=DEADLINE_SECONDS
    )
    await _finished(task)

    assert len(moved) == 1
    assert stopped.status == HarnessTurnStatus.CANCELLED
    assert stopped.error == "Operator stop"
    saved_chat_turn = await store.get(ChatTurn, chat_turn.id)
    assert saved_chat_turn.status == ChatTurnStatus.CANCELLED
    assert saved_chat_turn.error == "Operator stop"
    session = await store.get(HarnessSession, turn.harness_session_id)
    assert session.status == HarnessSessionStatus.IDLE
    # Stop wrote on top of the producer's write instead of replacing it.
    if model is HarnessTurn:
        assert stopped.metadata["saved_by_producer"] is True
    assert connection.interrupted is True
    assert turn.harness_session_id not in runtime._active
    assert (await runtime.session_activity(turn.harness_session_id)).busy is False
    replay = await runtime.activity_events(turn.id)
    assert replay.events[-1].type == "turn_status"
    assert replay.events[-1].item_status == "cancelled"


async def test_stop_succeeds_when_a_mission_saves_its_run_first(tmp_path, monkeypatch):
    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    run = await runtime.start_mission(
        engagement_id=engagement.id,
        objective="Work until stopped",
        profile_id=profile.id,
        model=None,
        budget=RunBudget(),
    )
    task = runtime._mission_tasks[run.id]
    connection = await _prompt_accepted(adapter)
    [turn] = [
        item
        for item in await store.list_entities(
            HarnessTurn, engagement_id=engagement.id, limit=100
        )
        if item.run_id == run.id
    ]

    update_with_event = store.update_with_event
    moved: list[str] = []

    async def producer_saves_first(entity_model, entity_id, changes, **options):
        if entity_model is AgentRun and not moved:
            # The mission's own accounting lands between Stop's read and write.
            moved.append(entity_id)
            current = await store.get(AgentRun, entity_id)
            await store.update(
                AgentRun,
                entity_id,
                {"started_at": current.started_at or utc_now()},
                expected_revision=current.revision,
            )
        return await update_with_event(entity_model, entity_id, changes, **options)

    monkeypatch.setattr(store, "update_with_event", producer_saves_first)

    stopped = await asyncio.wait_for(
        runtime.cancel_turn(turn.id, reason="Operator mission stop"),
        timeout=DEADLINE_SECONDS,
    )
    await _finished(task)

    assert moved == [run.id]
    assert stopped.status == HarnessTurnStatus.CANCELLED
    assert (await store.get(AgentRun, run.id)).status == RunStatus.CANCELLED
    cancelled = [
        event
        for event in await store.replay_events(run.id)
        if event.event_type == "run.cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "Operator mission stop"
    assert connection.interrupted is True
    assert turn.harness_session_id not in runtime._active


async def test_stop_reports_a_record_that_never_stops_moving(tmp_path, monkeypatch):
    """The repeat is bounded: a record that changes on every attempt is reported."""

    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    _, _, turn = await _prepare_chat(runtime, engagement, profile)
    task = await runtime.start_chat_turn(turn.id)
    await _prompt_accepted(adapter)

    update = store.update
    attempts = 0

    async def always_moved(entity_model, entity_id, changes, *, expected_revision=None):
        nonlocal attempts
        if (
            entity_model is ChatTurn
            and changes.get("status") == ChatTurnStatus.CANCELLED
        ):
            attempts += 1
            raise ConflictError("revision conflict: expected 1, found 2")
        return await update(
            entity_model, entity_id, changes, expected_revision=expected_revision
        )

    monkeypatch.setattr(store, "update", always_moved)
    with pytest.raises(ConflictError):
        await runtime.cancel_turn(turn.id, reason="Operator stop")
    assert attempts > 1

    # The turn itself was saved as stopped; a repeated Stop releases its
    # producer and vendor connection.
    monkeypatch.setattr(store, "update", update)
    await runtime.cancel_turn(turn.id, reason="Operator stop")
    await _finished(task)


async def _stop_between(
    runtime, turn_id: str, monkeypatch
) -> tuple[asyncio.Task[HarnessTurn], Callable[[], None]]:
    """Run Stop up to its saved state, before it cancels the producer.

    Stop saves the turn, its owner and its session, records "Turn stopped",
    and only then cancels the producer. Holding that record leaves the
    producer running against the saved Stop, as it does for a moment in Core.
    """

    saved = asyncio.Event()
    release = asyncio.Event()
    persist = runtime._persist_activity

    async def hold_stop_record(turn, session, event):
        if event.title == "Turn stopped":
            saved.set()
            await release.wait()
        return await persist(turn, session, event)

    monkeypatch.setattr(runtime, "_persist_activity", hold_stop_record)
    stop = asyncio.create_task(runtime.cancel_turn(turn_id, reason="Operator stop"))
    await asyncio.wait_for(saved.wait(), timeout=DEADLINE_SECONDS)
    return stop, release.set


async def _assert_stopped_and_released(
    store, runtime, adapter, chat, chat_turn, turn, stopped, engagement, profile
) -> None:
    assert stopped.status == HarnessTurnStatus.CANCELLED
    assert (await store.get(ChatTurn, chat_turn.id)).status == ChatTurnStatus.CANCELLED
    session = await store.get(HarnessSession, turn.harness_session_id)
    assert session.status == HarnessSessionStatus.IDLE
    assert turn.harness_session_id not in runtime._active
    assert (await runtime.session_activity(turn.harness_session_id)).busy is False
    replay = await runtime.activity_events(turn.id)
    assert [event.type for event in replay.events if event.type == "error"] == []

    # The next message continues on the same vendor session. A session left
    # reserved would be forked instead and lose the conversation's context.
    adapter.connections[0].hold = False
    _, follow_up_owner, follow_up = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Handle the replacement direction",
        chat_session_id=chat.id,
        harness_session_id=turn.harness_session_id,
        mcp_server_ids=[],
    )
    await _finished(await runtime.start_chat_turn(follow_up.id))
    finished = await store.get(HarnessTurn, follow_up.id)
    assert finished.status == HarnessTurnStatus.COMPLETE
    assert finished.harness_session_id == turn.harness_session_id
    assert (await store.get(ChatTurn, follow_up_owner.id)).status.value == "complete"
    assert len(adapter.connections) == 1


async def test_stop_during_start_bookkeeping_releases_the_session(
    tmp_path, monkeypatch
):
    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    chat, chat_turn, turn = await _prepare_chat(runtime, engagement, profile)

    start_owner = runtime._start_owner
    entered = asyncio.Event()
    hold = True

    async def held_start_owner(harness_turn):
        if hold:
            # The turn and its session are saved as running; the producer is
            # still inside its start bookkeeping when Stop arrives.
            entered.set()
            await asyncio.Event().wait()
        await start_owner(harness_turn)

    monkeypatch.setattr(runtime, "_start_owner", held_start_owner)
    task = await runtime.start_chat_turn(turn.id)
    await asyncio.wait_for(entered.wait(), timeout=DEADLINE_SECONDS)
    assert (await store.get(HarnessTurn, turn.id)).status == HarnessTurnStatus.RUNNING

    stopped = await asyncio.wait_for(
        runtime.cancel_turn(turn.id, reason="Operator stop"), timeout=DEADLINE_SECONDS
    )
    await _finished(task)
    hold = False

    # The prompt never reached the vendor.
    assert adapter.connections[0].prompts == []
    await _assert_stopped_and_released(
        store, runtime, adapter, chat, chat_turn, turn, stopped, engagement, profile
    )


async def test_stop_saved_while_connecting_ends_the_producer_quietly(
    tmp_path, monkeypatch
):
    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    chat, chat_turn, turn = await _prepare_chat(runtime, engagement, profile)

    connect = runtime._connection
    connected = asyncio.Event()
    proceed = asyncio.Event()

    async def connected_then_held(session, harness_turn):
        connection = await connect(session, harness_turn)
        connected.set()
        await proceed.wait()
        return connection

    monkeypatch.setattr(runtime, "_connection", connected_then_held)
    task = await runtime.start_chat_turn(turn.id)
    await asyncio.wait_for(connected.wait(), timeout=DEADLINE_SECONDS)

    # Stop is saved while the turn is still queued; the producer then
    # reaches the write that marks the turn running.
    stop, release_stop = await _stop_between(runtime, turn.id, monkeypatch)
    proceed.set()
    await asyncio.wait({task}, timeout=DEADLINE_SECONDS)
    assert task.done()
    assert task.cancelled() or task.exception() is None
    release_stop()
    stopped = await asyncio.wait_for(stop, timeout=DEADLINE_SECONDS)

    assert adapter.connections[0].prompts == []
    replay = await runtime.activity_events(turn.id)
    assert replay.events[-1].type == "turn_status"
    assert replay.events[-1].item_status == "cancelled"
    await _assert_stopped_and_released(
        store, runtime, adapter, chat, chat_turn, turn, stopped, engagement, profile
    )


async def test_stop_saved_inside_owner_start_does_not_fail_the_producer(
    tmp_path, monkeypatch
):
    store, engagement, profile, adapter, runtime = await _held_runtime(tmp_path)
    chat, chat_turn, turn = await _prepare_chat(runtime, engagement, profile)

    update = store.update
    stops: list[tuple[asyncio.Task[HarnessTurn], Callable[[], None]]] = []
    stop_requested = False

    async def stop_lands_before_owner_write(
        entity_model, entity_id, changes, *, expected_revision=None
    ):
        nonlocal stop_requested
        if (
            entity_model is ChatTurn
            and changes == {"status": ChatTurnStatus.ROUTING, "approval_id": None}
            and not stop_requested
        ):
            # The producer has read its chat turn and is about to mark it
            # started. Stop saves the turn, the chat turn and the session
            # in between.
            stop_requested = True
            stops.append(await _stop_between(runtime, turn.id, monkeypatch))
        return await update(
            entity_model, entity_id, changes, expected_revision=expected_revision
        )

    monkeypatch.setattr(store, "update", stop_lands_before_owner_write)
    task = await runtime.start_chat_turn(turn.id)

    async def stop_started() -> None:
        while not stops:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(stop_started(), timeout=DEADLINE_SECONDS)
    stop, release_stop = stops[0]
    # The producer carries on until Stop cancels it; it must not have died
    # on the chat turn Stop had already saved.
    await asyncio.sleep(0.2)
    assert not task.done() or task.cancelled() or task.exception() is None
    release_stop()
    stopped = await asyncio.wait_for(stop, timeout=DEADLINE_SECONDS)
    await _finished(task)
    assert task.cancelled() or task.exception() is None

    await _assert_stopped_and_released(
        store, runtime, adapter, chat, chat_turn, turn, stopped, engagement, profile
    )


class FailingConnection(FakeConnection):
    """The vendor transport is lost right after the prompt is accepted."""

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        yield HarnessEvent(type="started")
        raise HarnessTransportError("uncertain transport loss")


class FailingAdapter(FakeAdapter):
    def __init__(self, *, on_open: bool) -> None:
        super().__init__()
        self.on_open = on_open

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        if self.on_open:
            raise HarnessTransportError("the harness process did not start")
        connection = FailingConnection(request)
        self.connections.append(connection)
        return connection


async def _error_events(runtime, turn_id: str) -> list[HarnessEvent]:
    replay = await runtime.activity_events(turn_id)
    return [event for event in replay.events if event.type == "error"]


@pytest.mark.parametrize("on_open", [False, True], ids=["mid-turn", "connecting"])
async def test_failed_turn_saves_its_error_event_before_it_ends(
    tmp_path, monkeypatch, on_open
):
    """A follower ends on the terminal status, so the error must already be saved."""

    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    adapter = FailingAdapter(on_open=on_open)
    runtime.adapter_factory = lambda _: adapter
    _, _, turn = await _prepare_chat(runtime, engagement, profile, "Lose the transport")

    fail_turn = runtime._fail_turn
    saved_before_end: list[list[HarnessEvent]] = []

    async def observed_fail_turn(turn_id, status, error, **options):
        saved_before_end.append(await _error_events(runtime, turn_id))
        await fail_turn(turn_id, status, error, **options)

    monkeypatch.setattr(runtime, "_fail_turn", observed_fail_turn)
    await _finished(await runtime.start_chat_turn(turn.id))

    failed = await store.get(HarnessTurn, turn.id)
    assert failed.status == HarnessTurnStatus.INTERRUPTED
    [[error]] = saved_before_end
    assert error.error_id.startswith("err_")
    assert error.error_id == failed.metadata["diagnostic_error_id"]
    assert error.reason_code == failed.metadata["diagnostic_reason_code"]
    if not on_open:
        assert error.reason_code == "transport_closed"
    assert await _error_events(runtime, turn.id) == [error]


async def test_follower_reads_the_ledger_again_after_the_terminal_status(
    tmp_path, monkeypatch
):
    """The error event and the terminal status land between a follower's two reads."""

    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    _, _, turn = await _prepare_chat(runtime, engagement, profile, "Lose the transport")
    session = await store.get(HarnessSession, turn.harness_session_id)

    activity_events = runtime.activity_events
    get = store.get
    empty_page_read = False
    failed = False

    async def observed_activity_events(turn_id, **options):
        nonlocal empty_page_read
        page = await activity_events(turn_id, **options)
        empty_page_read = empty_page_read or not page.events
        return page

    async def failure_lands_before_status_read(entity_model, entity_id):
        nonlocal failed
        if (
            empty_page_read
            and not failed
            and entity_model is HarnessTurn
            and entity_id == turn.id
        ):
            # The follower found nothing new in the ledger and now asks for
            # the turn's status. In between, the turn fails: its error event
            # is saved, then its terminal status.
            failed = True
            await runtime._persist_activity(
                turn,
                session,
                HarnessEvent(
                    type="error",
                    harness_session_id=session.id,
                    harness_turn_id=turn.id,
                    message="uncertain transport loss",
                    error_id="err_saved_with_the_status",
                    reason_code="transport_closed",
                    retryable=True,
                ),
            )
            await runtime._fail_turn(
                turn.id,
                HarnessTurnStatus.INTERRUPTED,
                "uncertain transport loss",
                diagnostic={
                    "error_id": "err_saved_with_the_status",
                    "reason_code": "transport_closed",
                    "remediation_id": "harnesses.transport_closed",
                },
            )
        return await get(entity_model, entity_id)

    monkeypatch.setattr(runtime, "activity_events", observed_activity_events)
    monkeypatch.setattr(store, "get", failure_lands_before_status_read)

    async def follow() -> list[HarnessEvent]:
        return [event async for event in runtime.follow_turn(turn.id)]

    events = await asyncio.wait_for(follow(), timeout=DEADLINE_SECONDS)

    assert failed
    assert (await get(HarnessTurn, turn.id)).status == HarnessTurnStatus.INTERRUPTED
    [error] = [event for event in events if event.type == "error"]
    assert error.error_id == "err_saved_with_the_status"
    assert error.reason_code == "transport_closed"
