"""A conversation nearing its target is compacted in the background after a turn.

The first compaction of a conversation can take a minute or more, and the turn
that crossed the target used to wait for it. Once a turn's answer is saved and
the conversation is past a share of the target, the boundary the next turn
would choose is compacted in the background; that turn reuses the snapshot
without a model call, or waits for a compaction still running. The background
work never fails or delays the turn that triggered it.
"""

import asyncio
import json

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.context import ContextCompactionError, resolve_context_limits
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ContextSnapshot,
    ContextSnapshotStatus,
    Engagement,
)
from nebula.v3.providers import ModelRequest, ModelResponse
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import _profile
from tests.v3.test_chat_context_assembly import ReportingProvider

MEMORY_HEADING = "EARLIER CONVERSATION, COMPACTED BY NEBULA"


class GatedProvider(ReportingProvider):
    """Compacts only once the test opens the gate, so a turn can arrive meanwhile."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if (
            request.metadata.get("operation") == "context_compaction"
            and self.gate is not None
        ):
            assert self.entered is not None
            self.entered.set()
            await self.gate.wait()
        return await super().complete(request)


def _chat(tmp_path, *, share: float):
    """A saved conversation at about ``share`` of the fallback target."""

    store = NebulaStore(tmp_path / "precompaction.db")
    engagement = store.create(Engagement(id="eng-pre", name="Precompaction"))
    profile = store.create(_profile(local=True))
    session = store.create(
        ChatSession(
            id="session-pre",
            engagement_id=engagement.id,
            title="Precompaction",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    target = resolve_context_limits(profile).target_input_tokens
    count = 12
    size = int(target * share * 3) // count
    store.create_many(
        [
            ChatMessage(
                id=f"history-{sequence:02d}",
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=f"Note {sequence} about the harbor rollout: "
                + "x" * max(0, size - 40),
            )
            for sequence in range(1, count + 1)
        ]
    )
    provider = GatedProvider(profile.id)
    service = ChatService(store, provider_factory=lambda _: provider)
    return store, service, session, profile, provider


async def _turn(service, session, profile, content, **fields):
    prepared = await service.prepare_async(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": content}],
            **fields,
        )
    )
    completion = await service.complete(prepared)
    return prepared, completion


async def _background(service, session_id):
    running = service._precompactions.get(session_id)
    if running is not None and running.task is not None:
        await asyncio.wait({running.task})


def _compactions(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]


def _snapshots(store) -> list[ContextSnapshot]:
    return store.list_entities(ContextSnapshot, limit=20)


def _long_question(profile, share: float) -> str:
    target = resolve_context_limits(profile).target_input_tokens
    return "What changed in the rollout? " + "y" * int(target * share * 3)


def test_the_turn_past_the_target_reuses_the_background_snapshot(tmp_path):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)

    async def scenario():
        first, completion = await _turn(service, session, profile, "Noted?")
        # The turn itself sent everything and compacted nothing.
        assert first.context_snapshot is None
        assert completion.message.content
        await _background(service, session.id)
        prepared = _compactions(provider)
        assert prepared, "a conversation past the threshold is compacted after the turn"
        [snapshot] = _snapshots(store)
        assert snapshot.status == ContextSnapshotStatus.READY
        # The next request still fits the target and is sent whole, so the
        # context status does not claim the prepared snapshot serves it.
        status = service.context_status(session.id)
        assert status.status == "not_needed"
        assert status.snapshot is None

        second, _ = await _turn(service, session, profile, _long_question(profile, 0.3))
        return snapshot, len(prepared), second

    snapshot, compaction_calls, second = asyncio.run(scenario())

    # The turn that crossed the target made no compactor call of its own.
    assert len(_compactions(provider)) == compaction_calls
    assert second.context_snapshot is not None
    assert second.context_snapshot.id == snapshot.id
    assert second.model_request.messages[0].content.startswith(MEMORY_HEADING)
    assert service.context_status(session.id).status == "ready"


def test_a_turn_arriving_during_the_background_compaction_waits_and_reuses_it(
    tmp_path,
):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)

    async def scenario():
        provider.gate = asyncio.Event()
        provider.entered = asyncio.Event()
        await _turn(service, session, profile, "Noted?")
        await asyncio.wait_for(provider.entered.wait(), 5)
        running = service._precompactions[session.id]
        assert running.started
        background_calls: list[int] = []
        running.task.add_done_callback(
            lambda _: background_calls.append(len(_compactions(provider)))
        )
        second = asyncio.create_task(
            _turn(service, session, profile, _long_question(profile, 0.3))
        )
        await asyncio.sleep(0.05)
        # The turn waits for the compaction already running rather than
        # starting its own.
        assert not second.done()
        provider.gate.set()
        prepared, completion = await asyncio.wait_for(second, 10)
        return prepared, completion, background_calls

    prepared, completion, background_calls = asyncio.run(scenario())

    [snapshot] = _snapshots(store)
    assert prepared.context_snapshot is not None
    assert prepared.context_snapshot.id == snapshot.id
    # Only the background task called the compactor; the turn reused it.
    assert background_calls and background_calls[0] > 0
    assert len(_compactions(provider)) == background_calls[0]
    assert completion.message.content


def test_a_turn_before_the_background_compaction_starts_compacts_for_itself(
    tmp_path,
):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)

    async def scenario():
        # Every background slot is busy, so this conversation's compaction
        # is still queued when the next turn needs a snapshot.
        for _ in range(chat_module._PRECOMPACTION_CONCURRENCY):
            await service._precompaction_slots.acquire()
        await _turn(service, session, profile, "Noted?")
        queued = service._precompactions[session.id]
        assert not queued.started
        second, _ = await _turn(service, session, profile, _long_question(profile, 0.3))
        await asyncio.wait({queued.task})
        return second, queued

    second, queued = asyncio.run(scenario())

    assert queued.task.cancelled()
    assert second.context_snapshot is not None
    assert [item.id for item in _snapshots(store)] == [second.context_snapshot.id]


def test_a_failed_background_compaction_leaves_the_turns_alone(tmp_path, monkeypatch):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)
    failures: list[str] = []
    original = chat_module.ContextCompactor.compact
    calls = 0

    async def failing_once(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ContextCompactionError("the compactor model is unavailable")
        return await original(self, **kwargs)

    recorded = chat_module.record_caught_exception

    def capture(feature, event_code, *args, **kwargs):
        failures.append(event_code)
        return recorded(feature, event_code, *args, **kwargs)

    monkeypatch.setattr(chat_module.ContextCompactor, "compact", failing_once)
    monkeypatch.setattr(chat_module, "record_caught_exception", capture)

    async def scenario():
        _, completion = await _turn(service, session, profile, "Noted?")
        await _background(service, session.id)
        second, _ = await _turn(service, session, profile, _long_question(profile, 0.3))
        return completion, second

    completion, second = asyncio.run(scenario())

    assert "chat.context.precompaction_failed" in failures
    assert completion.message.content
    saved = service.session_messages(session.id)
    assert saved[-1].role == ChatRole.ASSISTANT
    # The next turn compacted for itself, as it did before.
    assert second.context_snapshot is not None
    assert calls == 2


def test_below_the_threshold_nothing_is_compacted_in_the_background(tmp_path):
    store, service, session, profile, provider = _chat(tmp_path, share=0.3)

    async def scenario():
        await _turn(service, session, profile, "Noted?")
        await _background(service, session.id)

    asyncio.run(scenario())

    assert _compactions(provider) == []
    assert _snapshots(store) == []


def test_a_goal_pays_for_its_background_compaction_and_one_it_cannot_afford_is_skipped(
    tmp_path,
):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)
    goal = store.create(
        ChatGoal(
            id="goal-pre",
            engagement_id=session.engagement_id,
            session_id=session.id,
            objective="Summarise the harbor rollout",
            completion_criteria=["The rollout is summarised"],
            status=ChatGoalStatus.RUNNING,
            token_budget=1_000_000,
        )
    )

    async def scenario():
        await _turn(service, session, profile, "Noted?", goal_id=goal.id, stream=True)
        before = store.get(ChatGoal, goal.id).usage.total_tokens
        await _background(service, session.id)
        return before

    before = asyncio.run(scenario())

    [snapshot] = _snapshots(store)
    [compaction] = _compactions(provider)
    # The goal is what the next turn continues, so it guides the memory ...
    assert json.loads(compaction.messages[0].content)["objective"] == (
        "Summarise the harbor rollout"
    )
    # ... and pays for it, like a compaction its turn needed.
    assert snapshot.usage.total_tokens > 0
    assert (
        store.get(ChatGoal, goal.id).usage.total_tokens
        == before + snapshot.usage.total_tokens
    )

    # A goal left unable to pay for the archive is not compacted for.
    (tmp_path / "poor").mkdir()
    store2, service2, session2, profile2, provider2 = _chat(
        tmp_path / "poor", share=0.8
    )
    poor = store2.create(
        ChatGoal(
            id="goal-poor",
            engagement_id=session2.engagement_id,
            session_id=session2.id,
            objective="Summarise the harbor rollout",
            completion_criteria=["The rollout is summarised"],
            status=ChatGoalStatus.RUNNING,
            token_budget=1_000_000,
        )
    )

    async def poor_scenario():
        await _turn(
            service2, session2, profile2, "Noted?", goal_id=poor.id, stream=True
        )
        # The turn spent nearly all of it before the background task ran.
        spent = store2.get(ChatGoal, poor.id)
        store2.update(
            ChatGoal,
            poor.id,
            {
                "usage": spent.usage.model_copy(
                    update={"total_tokens": spent.token_budget - 500}
                )
            },
            expected_revision=spent.revision,
        )
        await _background(service2, session2.id)

    asyncio.run(poor_scenario())

    assert _compactions(provider2) == []
    assert _snapshots(store2) == []


def test_core_shutdown_cancels_a_background_compaction_and_persists_nothing(
    tmp_path,
):
    store, service, session, profile, provider = _chat(tmp_path, share=0.8)

    async def scenario():
        provider.gate = asyncio.Event()
        provider.entered = asyncio.Event()
        await _turn(service, session, profile, "Noted?")
        await asyncio.wait_for(provider.entered.wait(), 5)
        await service.shutdown()

    asyncio.run(scenario())

    assert service._precompactions == {}
    # Nothing half-done is stored: the next turn after a restart compacts
    # for itself.
    assert _snapshots(store) == []
