"""A goal turn that fails on a transient provider error continues once on its own.

Overnight goals paused whenever a provider stream was cut or an overload
outlasted the provider's own retries. Core now keeps the goal running once,
notes the automatic retry in the transcript, and lets the idle-goal
reconciler start the continuation; a second failure in a row pauses it.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio

import pytest

from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_turn_outcomes import AUTO_RETRY_NOTE
from nebula.v3.domain import (
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProjectHold,
    ProviderProfile,
)
from nebula.v3.providers import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderOverloadedError,
    ProviderQuotaError,
    ProviderStreamInterruptedError,
)
from nebula.v3.recovery_markers import GOAL_PROVIDER_RETRY
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider

pytestmark = pytest.mark.anyio

_FAILED_PAUSE = (
    "Response failed before the goal finished. Review the error, then resume the goal."
)


class ScriptedProvider(FakeProvider):
    """Answers goal turns from a script: an exception fails that turn."""

    def __init__(self, script, *, before_failure=None) -> None:
        super().__init__("provider", local=False)
        self.config.model_allowlist.append("model")
        self.script = list(script)
        self.goal_requests = 0
        self.before_failure = before_failure

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.goal_requests += 1
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            if self.before_failure is not None:
                await self.before_failure()
            raise step
        return await super().complete(request)


async def _setup(tmp_path, provider, *, step_budget=6):
    store = NebulaStore(tmp_path / "retry.db")
    await store.create(Engagement(id="project", name="Project"))
    await store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    await store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    goals = ChatGoalService(store)
    draft = await goals.create(
        "session",
        GoalCreate(
            objective="Work through bounded steps",
            completion_criteria=["Every step is evidenced"],
            step_budget=step_budget,
        ),
    )
    await goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    chat = ChatService(store, provider_factory=lambda _: provider)
    return store, goals, chat


async def _turns(store) -> list[ChatTurn]:
    turns = await store.list_entities(ChatTurn, engagement_id="project")
    return sorted(turns, key=lambda turn: turn.created_at)


async def _settled(store, chat, count: int) -> list[ChatTurn]:
    """Wait until ``count`` turns exist and none of them is still running."""

    for _ in range(400):
        turns = await _turns(store)
        if len(turns) >= count and not any(
            chat.has_active_provider_turn(turn.id)
            or turn.status in {ChatTurnStatus.QUEUED, ChatTurnStatus.ROUTING}
            for turn in turns
        ):
            return turns
        await asyncio.sleep(0.01)
    pytest.fail(f"expected {count} settled turns, found {len(await _turns(store))}")


async def _reconcile(chat) -> list[str]:
    # A goal must look idle on two consecutive passes before it is acted on.
    first = await chat.reconcile_idle_running_goals()
    assert first == []
    return await chat.reconcile_idle_running_goals()


async def _notes(chat) -> list[str]:
    return [
        message.content
        for message in await chat.session_messages("session")
        if message.metadata.get("kind") == "turn_outcome"
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ProviderOverloadedError(
            "provider overloaded after 3 attempts", status_code=503
        ),
        ProviderStreamInterruptedError(
            "provider stream ended before the reply completed: no finish reason "
            "or [DONE] frame arrived"
        ),
    ],
    ids=["overloaded", "stream-cut"],
)
async def test_transient_failure_continues_once_and_success_rearms(tmp_path, failure):
    provider = ScriptedProvider([failure, None, failure])
    store, goals, chat = await _setup(tmp_path, provider)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        [failed] = await _settled(store, chat, 1)

        assert failed.status == ChatTurnStatus.FAILED
        goal = await goals.get("session")
        # The goal keeps running with a one-shot mark for this failed turn.
        assert goal.status == ChatGoalStatus.RUNNING
        assert goal.metadata[GOAL_PROVIDER_RETRY]["failed_turn_id"] == failed.id
        [note] = await _notes(chat)
        assert note.startswith("Response failed:")
        assert AUTO_RETRY_NOTE in note

        # The reconciler starts one continuation; it completes, which clears
        # the mark, and the goal continues into a turn that fails again.
        assert await _reconcile(chat) == [goal.id]
        turns = await _settled(store, chat, 3)
        assert [turn.status for turn in turns] == [
            ChatTurnStatus.FAILED,
            ChatTurnStatus.COMPLETE,
            ChatTurnStatus.FAILED,
        ]
        goal = await goals.get("session")
        # A completed turn ended the streak, so this failure retries again.
        assert goal.status == ChatGoalStatus.RUNNING
        assert goal.metadata[GOAL_PROVIDER_RETRY]["failed_turn_id"] == turns[2].id
        assert len(await _notes(chat)) == 2
    finally:
        await chat.shutdown()


async def test_second_consecutive_transient_failure_pauses_the_goal(tmp_path):
    overload = ProviderOverloadedError("upstream overloaded", status_code=429)
    provider = ScriptedProvider([overload, overload])
    store, goals, chat = await _setup(tmp_path, provider)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        await _settled(store, chat, 1)
        goal = await goals.get("session")
        assert goal.status == ChatGoalStatus.RUNNING

        assert await _reconcile(chat) == [goal.id]
        turns = await _settled(store, chat, 2)

        assert [turn.status for turn in turns] == [ChatTurnStatus.FAILED] * 2
        goal = await goals.get("session")
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.blocked_reason == _FAILED_PAUSE
        notes = await _notes(chat)
        assert AUTO_RETRY_NOTE in notes[0]
        assert AUTO_RETRY_NOTE not in notes[1]
        # Nothing else is dispatched for the paused goal.
        assert await chat.reconcile_idle_running_goals() == []
        assert await chat.reconcile_idle_running_goals() == []
        assert len(await _turns(store)) == 2

        # The operator's resume starts a new streak.
        resumed = await goals.write(
            "session", GoalWrite(expected_revision=goal.revision, action="resume")
        )
        assert GOAL_PROVIDER_RETRY not in resumed.metadata
    finally:
        await chat.shutdown()


@pytest.mark.parametrize(
    "failure",
    [
        ProviderError("provider rejected the request (400): unsupported parameter"),
        ProviderQuotaError("provider credits are exhausted"),
    ],
    ids=["invalid-request", "quota"],
)
async def test_non_transient_failure_pauses_as_before(tmp_path, failure):
    provider = ScriptedProvider([failure])
    store, goals, chat = await _setup(tmp_path, provider)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        [failed] = await _settled(store, chat, 1)

        assert failed.status == ChatTurnStatus.FAILED
        goal = await goals.get("session")
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.blocked_reason == _FAILED_PAUSE
        assert GOAL_PROVIDER_RETRY not in goal.metadata
        [note] = await _notes(chat)
        assert AUTO_RETRY_NOTE not in note
    finally:
        await chat.shutdown()


async def test_goal_paused_during_the_failing_turn_is_not_retried(tmp_path):
    goals_ref: list[ChatGoalService] = []

    async def operator_pauses() -> None:
        goals = goals_ref[0]
        goal = await goals.get("session")
        await goals.write(
            "session",
            GoalWrite(expected_revision=goal.revision, action="pause", reason="Wait"),
        )

    provider = ScriptedProvider(
        [ProviderOverloadedError("upstream overloaded", status_code=503)],
        before_failure=operator_pauses,
    )
    store, goals, chat = await _setup(tmp_path, provider)
    goals_ref.append(goals)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        await _settled(store, chat, 1)

        goal = await goals.get("session")
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.blocked_reason == "Wait"
        assert GOAL_PROVIDER_RETRY not in goal.metadata
        assert await chat.reconcile_idle_running_goals() == []
        assert await chat.reconcile_idle_running_goals() == []
        assert len(await _turns(store)) == 1
        assert provider.goal_requests == 1
    finally:
        await chat.shutdown()


async def test_project_hold_blocks_the_automatic_retry(tmp_path):
    stores: list[NebulaStore] = []

    async def hold_project() -> None:
        await stores[0].create(ProjectHold(engagement_id="project", reason="deploy"))

    provider = ScriptedProvider(
        [ProviderOverloadedError("upstream overloaded", status_code=503)],
        before_failure=hold_project,
    )
    store, goals, chat = await _setup(tmp_path, provider)
    stores.append(store)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        await _settled(store, chat, 1)

        # Held when the turn failed: the goal pauses as before.
        goal = await goals.get("session")
        assert goal.status == ChatGoalStatus.PAUSED
        assert GOAL_PROVIDER_RETRY not in goal.metadata
        assert len(await _turns(store)) == 1
    finally:
        await chat.shutdown()


async def test_hold_placed_after_arming_defers_the_continuation(tmp_path):
    provider = ScriptedProvider(
        [ProviderOverloadedError("upstream overloaded", status_code=503)]
    )
    store, goals, chat = await _setup(tmp_path, provider)
    try:
        await chat.dispatch_running_goal("session", "Begin the goal.")
        await _settled(store, chat, 1)
        assert GOAL_PROVIDER_RETRY in (await goals.get("session")).metadata

        await store.create(ProjectHold(engagement_id="project", reason="deploy"))
        assert await chat.reconcile_idle_running_goals() == []
        assert await chat.reconcile_idle_running_goals() == []
        assert len(await _turns(store)) == 1
        assert provider.goal_requests == 1
        # Left for the hold, which pauses the goal and resumes it on release.
        assert (await goals.get("session")).status == ChatGoalStatus.RUNNING
    finally:
        await chat.shutdown()
