"""#1100: Core's goal bookkeeping lands between a goal writer's read and write.

Each test runs the real concurrent writer (a usage charge, a previous
schedule outcome) inside the window and checks that the operation still
completes on the newest revision, without replaying its effect, while an
operator's own revision on the goal stays strict.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_schedules import ChatScheduleService
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSchedule,
    ChatSession,
    ChatSubagent,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    utc_now,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import ConflictError, NebulaStore
from tests.v3 import test_harness_chat_goals as hcg
from tests.v3.revision_window import before_transaction, before_update
from tests.v3.test_chat import FakeProvider
from tests.v3.test_chat_goals import setup_goal
from tests.v3.test_chat_schedules import _running_goal, _scheduled_session

pytestmark = pytest.mark.anyio

USAGE = ChatTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)


async def _running(tmp_path, **extra):
    store, goals = await setup_goal(tmp_path)
    chat = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )
    goal = await store.create(
        ChatGoal(
            engagement_id="project",
            session_id="session",
            objective="Inspect",
            completion_criteria=["Done"],
            status=ChatGoalStatus.RUNNING,
            started_at=utc_now(),
            active_since=utc_now(),
            **extra,
        )
    )
    return store, goals, chat, goal


async def _turn(store, goal, status=ChatTurnStatus.ROUTING):
    return await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            goal_id=goal.id,
            model="model",
            provider_profile_id="provider",
            status=status,
        )
    )


def _prepared(turn):
    return SimpleNamespace(
        turn=turn, execution_claim_id=None, execution_claim_released=False
    )


async def test_release_execution_survives_goal_charge(tmp_path):
    store, _goals, chat, goal = await _running(tmp_path)
    turn = await _turn(store, goal)
    prepared = _prepared(turn)
    await chat._claim_execution(prepared)

    window = before_transaction(
        store, "_release_execution", lambda: chat._charge_goal(goal.id, USAGE)
    )
    await chat._release_execution(prepared)

    assert window.fired
    saved_turn = await store.get(ChatTurn, turn.id)
    saved_goal = await store.get(ChatGoal, goal.id)
    assert saved_turn.execution_claim_id is None
    assert saved_goal.execution_claim_id is None
    assert saved_goal.usage.total_tokens == 2
    # The goal's next turn can take the claim.
    await chat._claim_execution(_prepared(await _turn(store, saved_goal)))


async def test_claim_execution_survives_goal_charge(tmp_path):
    store, _goals, chat, goal = await _running(tmp_path)
    turn = await _turn(store, goal)
    window = before_transaction(
        store, "_claim_execution_once", lambda: chat._charge_goal(goal.id, USAGE)
    )
    prepared = _prepared(turn)
    await chat._claim_execution(prepared)

    assert window.fired
    saved_goal = await store.get(ChatGoal, goal.id)
    assert prepared.execution_claim_id
    assert saved_goal.execution_claim_id == prepared.execution_claim_id
    assert saved_goal.usage.total_tokens == 2


async def test_claim_execution_still_refuses_another_workers_claim(tmp_path):
    store, _goals, chat, goal = await _running(tmp_path)
    turn = await _turn(store, goal)
    other = ChatService(
        store,
        provider_factory=lambda _: FakeProvider("provider", local=False),
        worker_id="other-worker",
    )

    async def other_claims():
        await other._claim_execution(_prepared(await store.get(ChatTurn, turn.id)))

    before_transaction(store, "_claim_execution_once", other_claims)
    with pytest.raises(chat_module.ChatHistoryConflict, match="another Core worker"):
        await chat._claim_execution(_prepared(turn))


async def test_stop_queued_goal_turn_survives_goal_charge(tmp_path):
    store, _goals, chat, goal = await _running(tmp_path)
    turn = await _turn(store, goal, ChatTurnStatus.QUEUED)
    window = before_transaction(
        store, "cancel_turn", lambda: chat._charge_goal(goal.id, USAGE)
    )
    cancelled = await chat.cancel_turn(turn.id)

    assert window.fired
    assert cancelled.status == ChatTurnStatus.CANCELLED
    assert (await store.get(ChatTurn, turn.id)).status == ChatTurnStatus.CANCELLED
    assert (await store.get(ChatGoal, goal.id)).usage.total_tokens == 2


async def test_start_child_survives_parent_charge(tmp_path):
    store, goals, chat, goal = await _running(tmp_path, child_budget=2)
    window = before_transaction(
        store, "_start_child_once", lambda: chat._charge_goal(goal.id, USAGE)
    )
    child = await goals.start_child(
        "session", GoalCreate(objective="Child", completion_criteria=["Done"])
    )

    assert window.fired
    parent = await store.get(ChatGoal, goal.id)
    assert child.parent_goal_id == goal.id
    assert parent.children_started == 1
    assert parent.child_session_ids == [child.session_id]
    assert parent.usage.total_tokens == 2
    # The lost attempt left no second child behind.
    children = await store.find_entities(
        ChatGoal, {"parent_goal_id": goal.id}, engagement_id="project"
    )
    assert [item.id for item in children] == [child.id]


async def _with_running_child(tmp_path):
    store, goals, chat, goal = await _running(tmp_path, child_budget=2)
    draft = await goals.start_child(
        "session", GoalCreate(objective="Child", completion_criteria=["Done"])
    )
    child = await store.update(
        ChatGoal,
        draft.id,
        {
            "status": ChatGoalStatus.RUNNING,
            "started_at": utc_now(),
            "active_since": utc_now(),
        },
        expected_revision=draft.revision,
    )
    return store, goals, chat, await store.get(ChatGoal, goal.id), child


@pytest.mark.parametrize(
    ("action", "status"),
    [("pause", ChatGoalStatus.PAUSED), ("cancel", ChatGoalStatus.CANCELLED)],
)
async def test_parent_stop_survives_child_charge(tmp_path, action, status):
    store, goals, chat, parent, child = await _with_running_child(tmp_path)
    window = before_transaction(
        store, "write", lambda: chat._charge_goal(child.id, USAGE)
    )
    updated = await goals.write(
        "session", GoalWrite(expected_revision=parent.revision, action=action)
    )

    assert window.fired
    assert updated.status == status
    saved_child = await store.get(ChatGoal, child.id)
    assert saved_child.status == status
    assert saved_child.usage.total_tokens == 2


async def test_operator_revision_on_the_goal_itself_stays_strict(tmp_path):
    store, goals, chat, goal = await _running(tmp_path)
    before_transaction(store, "write", lambda: chat._charge_goal(goal.id, USAGE))
    with pytest.raises(ConflictError):
        await goals.write(
            "session", GoalWrite(expected_revision=goal.revision, action="pause")
        )
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.RUNNING


async def test_harness_goal_block_survives_goal_charge(tmp_path):
    store, _engagement, _profile, _mcp, adapter, runtime = await hcg._runtime(tmp_path)
    goals = ChatGoalService(store, runtime)
    created = await hcg._new_goal(goals, step_budget=None)
    await goals.write(
        created.session.id,
        GoalWrite(expected_revision=created.goal.revision, action="start"),
    )
    chat = ChatService(store)
    observed: dict = {}

    async def charge():
        goal = await goals.get(created.session.id)
        await chat._charge_goal(goal.id, USAGE)

    class BlockingConnection(hcg.FakeConnection):
        async def run_turn(
            self, prompt: str, *, model: str, images=None
        ) -> AsyncIterator[hcg.HarnessEvent]:
            self.prompts.append(prompt)
            yield hcg.HarnessEvent(type="started", external_session_id="blocker")
            observed["window"] = before_update(
                store, "_gateway_goal_block_call", charge, method="reserve_tool_call"
            )
            observed["blocked"] = await runtime._gateway_call(
                self.request.session, "goal.block", {"reason": "Missing worker route"}
            )
            yield hcg.HarnessEvent(type="completed", message="Waiting")

    async def open_blocking(request):
        connection = BlockingConnection(request)
        adapter.connections.append(connection)
        return connection

    adapter.open = open_blocking
    try:
        assert await runtime.dispatch_running_goal(created.session.id, "Investigate")
        for _ in range(300):
            if "blocked" in observed and not runtime._chat_turn_tasks:
                break
            await asyncio.sleep(0.01)
        goal = await goals.get(created.session.id)
        assert observed["window"].fired
        assert observed["blocked"]["isError"] is False
        assert goal.status == ChatGoalStatus.BLOCKED
        assert goal.blocked_reason == "Missing worker route"
    finally:
        await runtime.shutdown()


async def _settle(service):
    while pending := [
        record.task
        for record in service._active_provider_turns.values()
        if record.task is not None and not record.task.done()
    ]:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_schedule_records_run_after_previous_outcome(tmp_path, monkeypatch):
    caught = []
    monkeypatch.setattr(
        chat_module,
        "record_caught_exception",
        lambda *args, **kwargs: caught.append(args[1]),
    )
    store = NebulaStore(tmp_path / "schedules.db")
    await store.create(Engagement(id="project", name="Project"))
    schedule = await _scheduled_session(store)
    schedule = await store.update(
        ChatSchedule,
        schedule.id,
        {"last_turn_id": "previous-turn", "last_status": "started"},
        expected_revision=schedule.revision,
    )
    await _running_goal(store, step_budget=1)
    provider = FakeProvider("provider", local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    schedules = ChatScheduleService(store)

    async def previous_outcome():
        await schedules.record_outcome(
            schedule.id, turn_id="previous-turn", status="complete"
        )

    window = before_update(store, "record_run", previous_outcome, direct=False)
    await service.fire_due_schedules()
    await _settle(service)

    assert window.fired
    turns = await store.list_entities(ChatTurn, engagement_id="project")
    latest = await store.get(ChatSchedule, schedule.id)
    assert len(turns) == 1
    assert latest.last_turn_id == turns[0].id
    assert latest.next_run_at > utc_now()
    assert "chat.schedule.changed_while_firing" not in caught
    assert "chat.schedule.failed" not in caught
    # The next tick does not start the occurrence a second time.
    await service.fire_due_schedules()
    await _settle(service)
    assert len(await store.list_entities(ChatTurn, engagement_id="project")) == 1


# Restart recovery resumes a paused goal while its resumed child charges it.
class TextProvider(ModelProvider):
    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                id="provider",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(
                    streaming=True, tools=True, strict_tools=True
                ),
            )
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider_id="provider",
            model="model-a",
            text="Report.",
            usage=ModelUsage(input_tokens=20, output_tokens=10, total_tokens=30),
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


def _recovery_snapshot(*, child: bool) -> dict:
    snapshot = {
        "model_request": ModelRequest(
            model="model-a", messages=[{"role": "user", "content": "Continue."}]
        ).model_dump(mode="json"),
        "context_usage": {},
        "recovery": {
            "required": True,
            "cause": "core_shutdown",
            "unknown_tool_call_ids": [],
            "unknown_hook_execution_ids": [],
        },
    }
    if child:
        snapshot["subagent_child"] = True
    return snapshot


async def _state(store: NebulaStore):
    engagement = await store.create(Engagement(id="project", name="Race"))
    profile = await store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"options": {"context_window": 32768}},
            capabilities={"streaming": True, "tool_calling": True},
            capability_verifications={
                "model-a": ProviderCapabilityVerification(
                    model="model-a", status=ProviderVerificationStatus.VERIFIED
                )
            },
        )
    )
    parent_session = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Supervisor",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=engagement.id,
            session_id=parent_session.id,
            objective="Finish the review",
            completion_criteria=["Reviewed"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    # Goal turn N started the subagent and settled; the child kept working.
    starter = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=parent_session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    child_session = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Subagent",
            provider_profile_id=profile.id,
            model="model-a",
            parent_session_id=parent_session.id,
        )
    )
    child = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=child_session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.INTERRUPTED,
            error="Core stopped before this response completed. Core will resume it automatically.",
            request_snapshot=_recovery_snapshot(child=True),
        )
    )
    record = await store.create(
        ChatSubagent(
            engagement_id=engagement.id,
            parent_session_id=parent_session.id,
            parent_turn_id=starter.id,
            child_session_id=child_session.id,
            child_turn_id=child.id,
            provider_profile_id=profile.id,
            model="model-a",
            name="Investigator",
            task="Investigate the assigned item.",
        )
    )
    await store.update(
        ChatSession,
        child_session.id,
        {"metadata": {**child_session.metadata, "subagent_id": record.id}},
        expected_revision=child_session.revision,
    )
    await asyncio.sleep(0.01)
    # Goal continuation turn N+1, running when Core stopped (created later).
    parent = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=parent_session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.INTERRUPTED,
            error="Core stopped before this response completed. Core will resume it automatically.",
            request_snapshot=_recovery_snapshot(child=False),
        )
    )
    return goal, child, record, parent


async def test_restart_resume_survives_child_charge(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "resume.db")
    goal, child, _record, parent = await _state(store)
    provider = TextProvider()
    service = ChatService(store, provider_factory=lambda _: provider, worker_id="w2")
    caught: list[str] = []
    original_record = chat_module.record_caught_exception

    def capture(feature, code, message, exc, **kwargs):
        caught.append(code)
        return original_record(feature, code, message, exc, **kwargs)

    monkeypatch.setattr(chat_module, "record_caught_exception", capture)
    await service.startup()
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.PAUSED
    charged = asyncio.Event()
    original_charge = service.subagents._true_up_goal_charge

    async def charge(goal_id, subagent_id, turn):
        await original_charge(goal_id, subagent_id, turn)
        charged.set()

    service.subagents._true_up_goal_charge = charge
    original_prepare = service.prepare_resume

    async def prepare_resume(turn_id, *args, **kwargs):
        prepared = await original_prepare(turn_id, *args, **kwargs)
        if turn_id == parent.id:
            # Hold the parent between its goal read and the resume until the
            # resumed child's charge lands.
            await asyncio.wait_for(charged.wait(), 10)
        return prepared

    service.prepare_resume = prepare_resume
    try:
        resumed = await service.resume_turns_stopped_by_core()
        assert child.id in resumed
        assert parent.id in resumed
        assert "chat.core_shutdown_auto_resume_failed" not in caught
        assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.RUNNING
    finally:
        await service.shutdown()
