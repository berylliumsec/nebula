"""Durable producer frames survive viewers, API replacement and owner changes."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.api_worker import create_api_worker
from nebula.v3.chat import ChatCompletionRequest, ChatHistoryConflict, ChatService
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatSession, ChatTurn, ChatTurnStatus, ProviderProfile
from nebula.v3.execution_scope import execution_context
from nebula.v3.providers import ModelStreamEvent, StreamEventType
from nebula.v3.runtime_chat_stream import RuntimeChatJournal, SEGMENT_END
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LEASE_MS,
    LeaseLost,
    MAX_STREAM_BATCH,
    STREAM_EPOCH,
)
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def journal(cluster):
    coordinators, stores, clock = cluster
    lease = await coordinators[0].claim_project("project", "old")
    turn = await stores[0].create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    return SimpleNamespace(
        writer=RuntimeChatJournal(coordinators[0], stores[0]),
        reader=RuntimeChatJournal(coordinators[1], stores[1]),
        coordination=coordinators,
        stores=stores,
        clock=clock,
        lease=lease,
        turn=turn,
    )


async def publish(journal, *events, **kwargs):
    with execution_context(journal.lease):
        return await journal.writer.publish("turn", list(events), **kwargs)


async def frames(reader, turn="turn", **kwargs):
    async with asyncio.timeout(8):
        return [item async for item in reader.follow(turn, **kwargs)]


async def test_committed_batch_replays_on_independent_connection_with_stable_cursors(
    journal,
):
    first = await publish(
        journal,
        ("started", {"type": "started"}),
        ("delta", {"type": "delta", "delta": "acknowledged"}),
    )
    with execution_context(journal.lease):
        await journal.writer.close("turn", "segment")
        await journal.writer.close("turn", "segment")
    result = await frames(
        journal.reader, after_sequence=first[0].sequence, epoch=STREAM_EPOCH
    )
    assert [(kind, payload["delta"]) for kind, payload in result] == [
        ("delta", "acknowledged")
    ]
    assert result[0][1]["sequence"] == first[1].sequence
    assert result[0][1]["epoch"] == STREAM_EPOCH
    assert len(await journal.coordination[1].replay_stream("turn")) == 3


async def test_cancellation_joins_publication_and_never_undoes_acknowledged_frames(
    journal, monkeypatch
):
    original = journal.coordination[0].append_stream_batch
    committed, release = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        result = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(journal.coordination[0], "append_stream_batch", held)
    with execution_context(journal.lease):
        task = asyncio.create_task(
            journal.writer.publish("turn", [("delta", {"delta": "saved"})])
        )
    await asyncio.wait_for(committed.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await journal.coordination[1].replay_stream("turn"))[0].payload == {
        "delta": "saved"
    }


async def test_handoff_keeps_one_epoch_and_never_hides_the_next_segment(journal):
    original = await publish(journal, ("delta", {"delta": "old"}))
    with execution_context(journal.lease):
        await journal.writer.close("turn", "old-segment")
    journal.clock[0] += LEASE_MS + 1
    await journal.coordination[1].register_runtime("replacement", "second-version")
    new = await journal.coordination[1].claim_project("project", "replacement")
    with execution_context(new):
        await journal.reader.publish("turn", [("delta", {"delta": "new"})])
        await journal.reader.close("turn", "new-segment")
    result = await frames(
        journal.reader, after_sequence=original[0].sequence, epoch=STREAM_EPOCH
    )
    assert [payload["delta"] for _, payload in result] == ["new"]
    assert {payload["epoch"] for _, payload in result} == {STREAM_EPOCH}
    with execution_context(journal.lease), pytest.raises(LeaseLost):
        await journal.writer.publish("turn", [("delta", {"delta": "stale"})])


async def test_end_marker_at_page_boundary_does_not_skip_a_resumed_segment(journal):
    await publish(
        journal,
        *(("delta", {"delta": str(index)}) for index in range(MAX_STREAM_BATCH - 1)),
    )
    with execution_context(journal.lease):
        await journal.writer.close("turn", "first")
    await publish(journal, ("delta", {"delta": "resumed"}))
    with execution_context(journal.lease):
        await journal.writer.close("turn", "second")
    result = await frames(journal.reader)
    assert len(result) == MAX_STREAM_BATCH and result[-1][1]["delta"] == "resumed"


async def test_terminal_turn_commit_does_not_hide_its_later_final_frame(journal):
    await journal.stores[0].update(
        ChatTurn, "turn", {"status": ChatTurnStatus.COMPLETE}
    )
    follower = journal.reader.follow("turn", poll_interval=0.01)
    waiting = asyncio.create_task(anext(follower))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await publish(journal, ("done", {"type": "done", "message": {"content": "Final"}}))
    with execution_context(journal.lease):
        await journal.writer.close("turn", "complete")
    assert (await waiting)[0] == "done"
    await follower.aclose()


async def test_publication_without_exact_project_authority_is_rejected(journal):
    with pytest.raises(LeaseLost):
        await journal.writer.publish("turn", [("delta", {"delta": "unbound"})])
    with (
        execution_context(replace(journal.lease, fencing_token=2)),
        pytest.raises(LeaseLost),
    ):
        await journal.writer.publish("turn", [("delta", {"delta": "wrong-token"})])
    assert await journal.writer.has_history("turn") is False


async def test_cursor_after_the_committed_end_returns_without_waiting(journal):
    await publish(journal, ("delta", {"delta": "saved"}))
    with execution_context(journal.lease):
        await journal.writer.close("turn", "complete")
    end = await journal.coordination[1].stream_head("turn")
    assert (
        await frames(journal.reader, after_sequence=end.sequence, epoch=STREAM_EPOCH)
        == []
    )


async def test_cursor_ahead_of_the_journal_requires_reload(journal):
    await publish(journal, ("delta", {"delta": "saved"}))
    with pytest.raises(CoordinationConflict, match="ahead"):
        await frames(journal.reader, after_sequence=999, epoch=STREAM_EPOCH)


@pytest.mark.parametrize("prior_history", [False, True])
async def test_started_segment_seeds_legacy_prefix_once_and_default_follow_excludes_prior_segments(
    journal, monkeypatch, prior_history
):
    service = ChatService(journal.stores[0], stream_journal=journal.writer)
    if prior_history:
        await publish(journal, ("delta", {"type": "delta", "delta": "saved "}))
        with execution_context(journal.lease):
            await journal.writer.close("turn", "previous")
    prepared = SimpleNamespace(
        turn=journal.turn.model_copy(update={"content": "saved "}),
        inputs_persisted=True,
        provider_profile=SimpleNamespace(id="provider"),
        resolved_model="model-a",
    )

    async def noop(*_args, **_kwargs):
        return None

    async def stream(_prepared):
        yield "started", {"type": "started", "turn_id": "turn"}
        yield "delta", {"type": "delta", "delta": "new"}

    async def produce(prepared, runtime, **_kwargs):
        await service._produce_provider_turn(prepared, runtime)

    monkeypatch.setattr(service, "stream", stream)
    monkeypatch.setattr(service, "_run_provider_turn", produce)
    monkeypatch.setattr(service, "_start_initial_naming", noop)
    monkeypatch.setattr(service.subagents, "turn_settled", noop)
    monkeypatch.setattr(service, "_continue_running_goal_after_turn", noop)
    try:
        with execution_context(journal.lease):
            await service.start_provider_turn(prepared)
        runtime = service._active_provider_turns["turn"]
        runtime.followers = 1
        await asyncio.wait_for(runtime.task, 8)
        current = [
            payload.get("delta", "")
            async for kind, payload in service.follow_provider_turn("turn")
            if kind == "delta"
        ]
        assert current == (["new"] if prior_history else ["saved ", "new"])
        all_segments = await frames(journal.reader)
        assert (
            "".join(
                payload.get("delta", "")
                for kind, payload in all_segments
                if kind == "delta"
            )
            == "saved new"
        )
    finally:
        with execution_context(journal.lease):
            await service.shutdown()


async def test_new_segment_waits_until_the_prior_segment_end_commits(
    journal, monkeypatch
):
    service = ChatService(journal.stores[0], stream_journal=journal.writer)
    prepared = SimpleNamespace(turn=journal.turn, inputs_persisted=False)
    entered, release = asyncio.Event(), asyncio.Event()
    close = journal.writer.close

    async def noop(*_args, **_kwargs):
        return None

    async def stream(_prepared):
        yield "delta", {"type": "delta", "delta": "finished"}

    async def held_close(*args, **kwargs):
        entered.set()
        await release.wait()
        return await close(*args, **kwargs)

    async def produce(prepared, runtime, **_kwargs):
        await service._produce_provider_turn(prepared, runtime)

    monkeypatch.setattr(service, "stream", stream)
    monkeypatch.setattr(service, "_run_provider_turn", produce)
    monkeypatch.setattr(service, "_start_initial_naming", noop)
    monkeypatch.setattr(service.subagents, "turn_settled", noop)
    monkeypatch.setattr(service, "_continue_running_goal_after_turn", noop)
    monkeypatch.setattr(journal.writer, "close", held_close)
    try:
        with execution_context(journal.lease):
            await service.start_provider_turn(prepared)
        runtime = service._active_provider_turns["turn"]
        runtime.followers = 1
        await asyncio.wait_for(entered.wait(), 5)
        assert not runtime.done
        with (
            execution_context(journal.lease),
            pytest.raises(ChatHistoryConflict, match="active work"),
        ):
            await service.start_provider_turn(prepared)
        release.set()
        await asyncio.wait_for(runtime.task, 5)
        assert runtime.done
    finally:
        release.set()
        with execution_context(journal.lease):
            await service.shutdown()


async def test_stop_before_first_producer_step_persists_cancelled_end_once(
    journal, monkeypatch
):
    store = journal.stores[0]
    await store.create(
        ProviderProfile(
            id="provider", name="Inert", provider_type="vllm", is_local=True
        )
    )
    await store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Prestart stop",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    service = ChatService(store, stream_journal=journal.writer)
    entered = []

    async def forbidden(*_args, **_kwargs):
        entered.append(True)
        raise AssertionError("prestart stop released the producer")

    monkeypatch.setattr(service, "_run_provider_turn", forbidden)
    try:
        with execution_context(journal.lease):
            await service.start_provider_turn(SimpleNamespace(turn=journal.turn))
            stopped = await service.stop_provider_turn("turn")
            await service.stop_provider_turn("turn")
        assert entered == []
        assert stopped.status == ChatTurnStatus.CANCELLED
        assert not service.has_active_provider_turn("turn")
        result = await frames(journal.reader)
        assert [kind for kind, _ in result] == ["queued", "cancelled"]
        assert (
            await journal.coordination[1].stream_head("turn")
        ).event_type == SEGMENT_END
    finally:
        with execution_context(journal.lease):
            await service.shutdown()


@pytest.mark.parametrize("fail", [False, True])
async def test_actual_chat_producer_persists_frames_before_viewers_and_settles_failure(
    journal, fail
):
    store = journal.stores[0]
    profile = await store.create(
        ProviderProfile(
            id="provider",
            name="Inert local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(FakeProvider):
        async def stream(self, request):
            yield ModelStreamEvent(type=StreamEventType.STARTED)
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="First ")
            entered.set()
            await release.wait()
            if fail:
                raise RuntimeError("Synthetic provider failure")
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="last")
            response = (await super().complete(request)).model_copy(
                update={"text": "First last"}
            )
            yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)

    provider = Provider(profile.id, local=True)
    service = ChatService(
        store, provider_factory=lambda _: provider, stream_journal=journal.writer
    )
    try:
        with execution_context(journal.lease):
            prepared = await service.prepare_async(
                ChatCompletionRequest(
                    provider_id=profile.id,
                    engagement_id="project",
                    model="model-a",
                    messages=[{"role": "user", "content": "Synthetic journal"}],
                    include_knowledge=False,
                    stream=True,
                )
            )
            turn_id = await service.start_provider_turn(prepared)
        await asyncio.wait_for(entered.wait(), 8)
        saved = await journal.coordination[1].replay_stream(turn_id)
        assert any(row.payload.get("delta") == "First " for row in saved)
        assert service._active_provider_turns[turn_id].events == []
        release.set()
        await asyncio.wait_for(service._active_provider_turns[turn_id].task, 8)
        replay = await frames(journal.reader, turn_id)
        assert "".join(
            payload.get("delta", "") for kind, payload in replay if kind == "delta"
        ) == ("First " if fail else "First last")
        assert any(kind == "error" for kind, _ in replay) is fail
        stored = await store.get(ChatTurn, turn_id)
        assert stored.status == (
            ChatTurnStatus.FAILED if fail else ChatTurnStatus.COMPLETE
        )
        assert (await journal.coordination[1].replay_stream(turn_id))[
            -1
        ].event_type == SEGMENT_END
    finally:
        release.set()
        with execution_context(journal.lease):
            await service.shutdown()


async def test_api_replacement_replays_sql_without_contacting_an_execution_process(
    journal, tmp_path
):
    saved = await publish(
        journal,
        ("started", {"type": "started"}),
        ("delta", {"type": "delta", "delta": "Durable"}),
    )
    with execution_context(journal.lease):
        await journal.writer.close("turn", "complete")

    async def forbidden(*_args):
        raise AssertionError("API replay attempted to contact execution")

    for cursor, expected in ((0, ["started", "delta"]), (saved[0].sequence, ["delta"])):
        api_store = NebulaStore(
            Database(
                journal.stores[0].database.url,
                bootstrap=False,
                pool_settings=PoolSettings(5, 12, 2, 5),
            )
        )
        delivery = RuntimeDelivery(
            journal.coordination[0], RuntimeVault(tmp_path / "payloads")
        )
        app = create_api_worker(
            api_store,
            delivery,
            operator_token="fixture-token",
            project_for=forbidden,
            upstream_for=forbidden,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture"
            ) as client:
                assert (
                    await client.get("/api/v1/chat/turns/turn/events")
                ).status_code == 401
                response = await client.get(
                    f"/api/v1/chat/turns/turn/events?after={cursor}&epoch={STREAM_EPOCH}",
                    headers={"Authorization": "Bearer fixture-token"},
                )
                assert response.status_code == 200
                decoded = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert [frame["type"] for frame in decoded] == expected
                assert {frame["epoch"] for frame in decoded} == {STREAM_EPOCH}
                ahead = await client.get(
                    "/api/v1/chat/turns/turn/events?after=999&epoch=durable-v1",
                    headers={"Authorization": "Bearer fixture-token"},
                )
                assert ahead.status_code == 409 and "ahead" in ahead.json()["detail"]
