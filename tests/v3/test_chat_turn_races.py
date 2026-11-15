"""Core's own timing never reaches the operator as a wrong state or a refusal.

A streamed turn's followers are told it ended only once the turn and its goal
are saved as ended, and a provider or model switch sent with a message is not
refused because Core named or compacted the conversation in the meantime.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import pytest

from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatHistoryConflict,
    ChatRuntimeSwitchPreflightRequest,
    ChatRuntimeSwitchRequest,
    ChatService,
)
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    utc_now,
)
from nebula.v3.providers import ModelRequest, ModelStreamEvent, StreamEventType
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _complete_and_name, _profile


class FailingStreamProvider(FakeProvider):
    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        raise RuntimeError("provider billing exhausted")


class BlockingStreamProvider(FakeProvider):
    def __init__(self, provider_id: str, *, local: bool) -> None:
        super().__init__(provider_id, local=local)
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest):
        del request
        self.started.set()
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="Working. ")
        await asyncio.Event().wait()


async def _streamed_goal_turn(tmp_path, provider_type):
    """A queued streamed turn of a running goal, on a service with no journal."""

    store = NebulaStore(tmp_path / "turn-races.db")
    project = await store.create(Engagement(name="Turn races"))
    profile = await store.create(_profile(local=True))
    provider = provider_type(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    prepared = await service.prepare_async(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=project.id,
            messages=[{"role": "user", "content": "Answer, then end the turn."}],
            include_knowledge=False,
            stream=True,
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=prepared.session.id,
            objective="Finish the answer",
            completion_criteria=["The answer is complete"],
            status=ChatGoalStatus.RUNNING,
            time_budget_seconds=3600,
            active_since=utc_now(),
        )
    )
    prepared.turn = await store.update(
        ChatTurn,
        prepared.turn.id,
        {"goal_id": goal.id},
        expected_revision=prepared.turn.revision,
    )
    return store, service, provider, prepared, goal


async def _saved_state(store, turn_id, goal_id):
    turn = await store.get(ChatTurn, turn_id)
    goal = await store.get(ChatGoal, goal_id)
    return turn.status, goal.status


async def test_a_follower_learns_of_a_failed_turn_after_it_and_its_goal_are_saved(
    tmp_path,
):
    store, service, _, prepared, goal = await _streamed_goal_turn(
        tmp_path, FailingStreamProvider
    )
    turn_id = await service.start_provider_turn(prepared)

    with pytest.raises(Exception, match="provider billing exhausted"):
        async for _ in service.follow_provider_turn(turn_id):
            pass

    # What the stream's caller reads next, with nothing awaited in between.
    assert await _saved_state(store, turn_id, goal.id) == (
        ChatTurnStatus.FAILED,
        ChatGoalStatus.PAUSED,
    )
    await service.shutdown()


async def test_a_follower_learns_of_a_stopped_turn_after_it_and_its_goal_are_saved(
    tmp_path,
):
    store, service, provider, prepared, goal = await _streamed_goal_turn(
        tmp_path, BlockingStreamProvider
    )
    turn_id = await service.start_provider_turn(prepared)

    async def follow():
        frames = [kind async for kind, _ in service.follow_provider_turn(turn_id)]
        return frames, await _saved_state(store, turn_id, goal.id)

    follower = asyncio.create_task(follow())
    await asyncio.wait_for(provider.started.wait(), timeout=30)
    await service.stop_provider_turn(turn_id)
    frames, seen = await asyncio.wait_for(follower, timeout=30)

    assert frames[-1] == "cancelled"
    assert seen == (ChatTurnStatus.CANCELLED, ChatGoalStatus.PAUSED)
    await service.shutdown()


async def test_a_settle_that_fails_still_ends_the_stream_with_the_responses_failure(
    tmp_path, monkeypatch
):
    store, service, _, prepared, _ = await _streamed_goal_turn(
        tmp_path, FailingStreamProvider
    )

    async def unavailable(turn_id):
        raise RuntimeError("the outcome note could not be saved")

    monkeypatch.setattr(service, "record_turn_outcome", unavailable)
    turn_id = await service.start_provider_turn(prepared)

    async def follow():
        async for _ in service.follow_provider_turn(turn_id):
            pass

    # Followers neither wait forever nor get the cleanup's error in place of
    # the reason the response ended.
    with pytest.raises(Exception, match="provider billing exhausted"):
        await asyncio.wait_for(follow(), timeout=30)
    assert not service.has_active_provider_turn(turn_id)
    await service.shutdown()


async def _answered_conversation(tmp_path):
    """One answered turn on provider-a, with provider-b ready to switch to."""

    store = NebulaStore(tmp_path / "switch-races.db")
    project = await store.create(Engagement(name="Switch races"))
    first = await store.create(_profile(local=True))
    others = [
        await store.create(
            first.model_copy(
                update={
                    "id": f"provider-{letter}",
                    "model_allowlist": [f"model-{letter}"],
                    "metadata": {"default_model": f"model-{letter}"},
                }
            )
        )
        for letter in ("b", "c")
    ]
    providers = {first.id: FakeProvider(first.id, local=True)}
    for profile in others:
        model = profile.model_allowlist[0]
        provider = FakeProvider(profile.id, local=True)
        provider.config = provider.config.model_copy(
            update={"model_allowlist": [model], "default_model": model}
        )
        providers[profile.id] = provider
    service = ChatService(store, provider_factory=lambda profile: providers[profile.id])
    answered = await _complete_and_name(
        service,
        await service.prepare_async(
            ChatCompletionRequest(
                provider_id=first.id,
                engagement_id=project.id,
                model="model-a",
                messages=[{"role": "user", "content": "Remember this note"}],
                include_knowledge=False,
            )
        ),
    )
    switch = ChatCompletionRequest(
        provider_id="provider-b",
        engagement_id=project.id,
        session_id=answered.session_id,
        model="model-b",
        messages=[{"role": "user", "content": "Continue with the new model"}],
        include_knowledge=False,
    )
    return store, service, answered.session_id, switch


def _during_prepare(monkeypatch, service, write):
    """Run ``write`` once, after prepare read the conversation and before it reviews the switch."""

    original = service.pending_turn
    ran = False

    async def pending_turn(session_id):
        nonlocal ran
        if not ran:
            ran = True
            await write()
        return await original(session_id)

    monkeypatch.setattr(service, "pending_turn", pending_turn)


@pytest.mark.parametrize("saves", [1, 2])
async def test_core_saving_the_conversation_during_prepare_does_not_refuse_a_switch(
    tmp_path, monkeypatch, saves
):
    store, service, session_id, switch = await _answered_conversation(tmp_path)
    read_by_operator = await store.get(ChatSession, session_id)

    async def core_saves():
        # As conversation naming and then precompaction do: a new title and
        # Core's own metadata, each save one revision later.
        for index in range(saves):
            latest = await store.get(ChatSession, session_id)
            await store.update(
                ChatSession,
                session_id,
                {
                    "title": f"Named by Core {index}",
                    "metadata": {**latest.metadata, f"core_bookkeeping_{index}": True},
                },
                expected_revision=latest.revision,
            )

    _during_prepare(monkeypatch, service, core_saves)
    await service.complete(await service.prepare_async(switch))

    saved = await store.get(ChatSession, session_id)
    assert (saved.provider_profile_id, saved.model) == ("provider-b", "model-b")
    assert saved.title == f"Named by Core {saves - 1}"
    assert all(saved.metadata[f"core_bookkeeping_{index}"] for index in range(saves))
    assert len(await service.session_messages(session_id)) == 4

    # A revision the operator's page supplied is still held to exactly.
    with pytest.raises(ChatHistoryConflict, match="conversation changed"):
        await service.runtime_switch_preflight(
            session_id,
            ChatRuntimeSwitchPreflightRequest(
                provider_id="provider-c",
                model="model-c",
                expected_session_revision=read_by_operator.revision,
            ),
        )
    await service.shutdown()


@pytest.mark.parametrize("change", ["runtime", "assistant_settings", "pending_switch"])
async def test_a_runtime_selected_elsewhere_during_prepare_still_refuses_the_switch(
    tmp_path, monkeypatch, change
):
    store, service, session_id, switch = await _answered_conversation(tmp_path)

    async def selected_elsewhere():
        latest = await store.get(ChatSession, session_id)
        if change == "runtime":
            await service.apply_runtime_switch(
                session_id,
                ChatRuntimeSwitchRequest(
                    provider_id="provider-c",
                    model="model-c",
                    expected_session_revision=latest.revision,
                ),
            )
            return
        key = (
            "assistant_config"
            if change == "assistant_settings"
            else "pending_runtime_switch"
        )
        await store.update(
            ChatSession,
            session_id,
            {"metadata": {**latest.metadata, key: {"model": "model-c"}}},
            expected_revision=latest.revision,
        )

    _during_prepare(monkeypatch, service, selected_elsewhere)
    with pytest.raises(ChatHistoryConflict, match="conversation changed; reload it"):
        await service.prepare_async(switch)

    saved = await store.get(ChatSession, session_id)
    assert (saved.provider_profile_id, saved.model) != ("provider-b", "model-b")
    assert len(await service.session_messages(session_id)) == 2
    await service.shutdown()
