"""A project hold stops all chat work in a project and restores exactly it.

The deployment of 2026-11-20 showed why: goals paused one by one were
resumed again by Butler's own checks, subagent reports kept starting new
parent turns, and nothing recorded what had been paused. These tests drive
the real goal service, chat service and hold service on a real store.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatService, ProjectHeld
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.chat_queue import ChatQueueService
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatQueue,
    ChatSchedule,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ProjectHold,
    utc_now,
)
from nebula.v3.project_hold_gate import HOLD_MARKER, ProjectHoldConflict
from nebula.v3.project_holds import HoldPlace, HoldRelease, ProjectHoldService
from nebula.v3.storage import ConflictError
from tests.v3.test_chat import FakeProvider
from tests.v3.test_chat_goals import setup_goal

pytestmark = pytest.mark.anyio


class Recorder:
    def __init__(self) -> None:
        self.provider_stops: list[str] = []
        self.harness_stops: list[tuple[str, str]] = []
        self.dispatched: list[str] = []
        self.woken: list[str] = []
        self.queues_woken = 0

    async def stop_provider(self, turn_id: str) -> None:
        self.provider_stops.append(turn_id)

    async def stop_harness(self, harness_turn_id: str, reason: str) -> None:
        self.harness_stops.append((harness_turn_id, reason))

    async def dispatch(self, session_id: str, _instruction: str) -> None:
        self.dispatched.append(session_id)

    async def wake_subagents(self, project_id: str) -> None:
        self.woken.append(project_id)

    def wake_queues(self) -> None:
        self.queues_woken += 1


async def _setup(tmp_path):
    store, goals = await setup_goal(tmp_path)
    recorder = Recorder()
    service = ProjectHoldService(
        store,
        stop_provider_turn=recorder.stop_provider,
        stop_harness_turn=recorder.stop_harness,
        dispatch_goal=recorder.dispatch,
        wake_subagents=recorder.wake_subagents,
        wake_queues=recorder.wake_queues,
    )
    return store, goals, service, recorder


async def _goal(store, session_id="session", **extra) -> ChatGoal:
    if session_id != "session":
        await store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title=session_id,
                provider_profile_id="provider",
                model="model",
            )
        )
    return await store.create(
        ChatGoal(
            engagement_id="project",
            session_id=session_id,
            objective="Inspect",
            completion_criteria=["Done"],
            status=extra.pop("status", ChatGoalStatus.RUNNING),
            started_at=utc_now(),
            active_since=utc_now(),
            **extra,
        )
    )


async def test_hold_pauses_running_goals_and_only_its_release_resumes_them(tmp_path):
    store, goals, service, recorder = await _setup(tmp_path)
    running = await _goal(store)
    already_paused = await _goal(store, "other", status=ChatGoalStatus.PAUSED)

    hold = await service.place(
        "project", HoldPlace(reason="Core deployment", deployment_id="deploy-1")
    )

    paused = await goals.get("session")
    assert paused.status == ChatGoalStatus.PAUSED
    assert paused.metadata[HOLD_MARKER] == hold.id
    assert [item.goal_id for item in hold.paused_goals] == [running.id]
    # The same deployment placing it again gets the same hold back.
    again = await service.place(
        "project", HoldPlace(reason="Core deployment", deployment_id="deploy-1")
    )
    assert again.id == hold.id
    with pytest.raises(ConflictError, match="already has hold"):
        await service.place("project", HoldPlace(reason="Another stop"))

    # Nobody else resumes it while held: not an operator, not Butler (which
    # writes through this same goal action), not restart recovery.
    with pytest.raises(ProjectHoldConflict) as refused:
        await goals.write(
            "session", GoalWrite(expected_revision=paused.revision, action="resume")
        )
    assert refused.value.code == "project_held"
    with pytest.raises(ProjectHoldConflict):
        await goals.write_latest("session", "resume", allow_pending_recovery=True)

    released = await service.release("project", hold.id, HoldRelease(reason="done"))

    assert released.status == "released"
    resumed = await goals.get("session")
    assert resumed.status == ChatGoalStatus.RUNNING
    assert HOLD_MARKER not in resumed.metadata
    # A goal that was paused before the hold stays paused.
    assert (await goals.get("other")).status == ChatGoalStatus.PAUSED
    assert already_paused.id not in {item.goal_id for item in released.restored_goals}
    assert [(item.goal_id, item.outcome) for item in released.restored_goals] == [
        (running.id, "resumed")
    ]
    assert recorder.dispatched == ["session"]
    assert recorder.woken == ["project"] and recorder.queues_woken == 1


async def test_held_project_refuses_new_and_resumed_chat_turns(tmp_path):
    store, _goals, service, _recorder = await _setup(tmp_path)
    chat = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )
    interrupted = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
        )
    )
    hold = await service.place("project", HoldPlace(reason="Core deployment"))

    with pytest.raises(ProjectHeld, match="Core deployment") as refused:
        await chat.prepare_async(
            ChatCompletionRequest(
                provider_id="provider",
                engagement_id="project",
                session_id="session",
                messages=[{"role": "user", "content": "Continue"}],
                include_knowledge=False,
                stream=True,
            )
        )
    assert refused.value.code == "project_held"
    with pytest.raises(ProjectHeld):
        await chat.prepare_resume(interrupted.id)
    # Refused before anything was written to the conversation.
    assert (await store.list_session_entities(ChatTurn, "session")) == [interrupted]

    await service.release("project", hold.id, HoldRelease())
    await chat.require_project_open("project")


async def test_goal_start_is_refused_while_held(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    draft = await _goal(store, status=ChatGoalStatus.DRAFT)
    await service.place("project", HoldPlace(reason="Core deployment"))

    with pytest.raises(ProjectHoldConflict):
        await goals.write(
            "session", GoalWrite(expected_revision=draft.revision, action="start")
        )
    assert (await goals.get("session")).status == ChatGoalStatus.DRAFT


async def test_running_turns_get_their_grace_then_core_stops_them(tmp_path):
    store, _goals, service, recorder = await _setup(tmp_path)
    turn = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.ROUTING,
        )
    )
    hold = await service.place(
        "project", HoldPlace(reason="Core deployment", grace_seconds=600)
    )
    assert hold.status == "holding"
    assert recorder.provider_stops == []

    # The grace period ends: the next pass stops what is still running.
    await store.update(
        ProjectHold,
        hold.id,
        {"grace_until": utc_now() - timedelta(seconds=1)},
        expected_revision=hold.revision,
    )
    still = await service.tick("project")
    assert recorder.provider_stops == [turn.id]
    assert still is not None and still.status == "holding"
    assert [(item.turn_id, item.outcome) for item in still.interrupted_turns] == [
        (turn.id, "stopped")
    ]

    # Once the stop settles the turn, the project is held: nothing is active.
    await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.CANCELLED},
        expected_revision=turn.revision,
    )
    held = await service.tick("project")
    assert held is not None and held.status == "held" and held.held_at is not None
    view = await service.view("project")
    assert view["held"] is True and view["active_turns"] == 0


async def test_goal_resumed_between_place_and_tick_is_paused_under_the_hold(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    hold = await service.place("project", HoldPlace(reason="Core deployment"))
    # A goal that reached running without the gate (written directly, as an
    # older worker could) is caught by the next pass and marked.
    late = await _goal(store, "late")

    ticked = await service.tick("project")

    assert (await goals.get("late")).metadata[HOLD_MARKER] == hold.id
    assert late.id in {item.goal_id for item in ticked.paused_goals}


async def test_release_leaves_an_unresumable_goal_paused_without_the_mark(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    await _goal(store, step_budget=1, current_step=1)
    hold = await service.place("project", HoldPlace(reason="Core deployment"))

    released = await service.release("project", hold.id, HoldRelease())

    goal = await goals.get("session")
    assert goal.status == ChatGoalStatus.PAUSED
    # Its operator can resume it again once they raise the budget.
    assert HOLD_MARKER not in goal.metadata
    assert released.restored_goals[0].outcome.startswith("left paused:")


async def test_due_schedule_waits_out_the_hold_instead_of_skipping(tmp_path):
    store, _goals, service, _recorder = await _setup(tmp_path)
    await _goal(store)
    due = utc_now() - timedelta(minutes=1)
    schedule = await store.create(
        ChatSchedule(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            interval_seconds=3600,
            next_run_at=due,
        )
    )
    await service.place("project", HoldPlace(reason="Core deployment"))
    chat = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )

    await chat.fire_due_schedules()

    unchanged = await store.get(ChatSchedule, schedule.id)
    # Not skipped (a skip would move next_run_at and record a reason): the
    # occurrence fires on the first pass after the release.
    assert unchanged.next_run_at == due
    assert unchanged.skip_reason is None


async def test_queued_follow_up_stays_queued_while_held(tmp_path):
    store, _goals, service, _recorder = await _setup(tmp_path)
    item = {"id": "item-1", "status": "queued", "request": {}}
    queue = await store.create(
        ChatQueue(engagement_id="project", session_id="session", items=[item])
    )
    await service.place("project", HoldPlace(reason="Core deployment"))
    chat = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )
    queues = ChatQueueService(store, chat, None)

    await queues.step(queue)

    # Neither claimed nor sent to review: release wakes it unchanged.
    assert (await store.get(ChatQueue, queue.id)).items == [item]


async def test_subagent_report_does_not_fail_a_waiting_parent_while_held(
    tmp_path, monkeypatch
):
    store, _goals, service, _recorder = await _setup(tmp_path)
    waiting = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.WAITING_CALLBACK,
        )
    )
    chat = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )
    subagents = chat.subagents

    async def ready_wait(_turn):
        return {"kind": "subagents"}

    async def ready(_wait):
        return True

    async def must_not_resume(_turn_id):
        raise AssertionError("a held project must not resume the parent")

    async def must_not_fail(_turn, _exc):
        raise AssertionError("a held project must not fail the parent")

    monkeypatch.setattr(subagents, "pending_wait", ready_wait)
    monkeypatch.setattr(subagents, "wait_ready", ready)
    monkeypatch.setattr(chat, "prepare_resume", must_not_resume)
    monkeypatch.setattr(subagents, "_fail_unresumable", must_not_fail)
    await service.place("project", HoldPlace(reason="Core deployment"))

    await subagents._resume_waiting_turn(waiting)

    assert (await store.get(ChatTurn, waiting.id)).status == (
        ChatTurnStatus.WAITING_CALLBACK
    )


async def test_hold_api_is_authenticated_and_refuses_goal_resume_with_its_code(
    tmp_path,
):
    from tests.v3.async_test_support import AsyncTestClient as TestClient
    from tests.v3.test_control_assistant_work import _app
    from nebula.v3.chat_goals import GoalCreate
    from nebula.v3.domain import Engagement, ProviderProfile

    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Held project"))
    profile = await store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True,
            model_allowlist=["model-a"],
        )
    )
    main = await store.create(
        ChatSession(
            engagement_id=project.id, title="Main agent",
            provider_profile_id=profile.id, model="model-a",
        )
    )
    goals = ChatGoalService(store)
    draft = await goals.create(
        main.id, GoalCreate(objective="Inspect", completion_criteria=["Done"])
    )
    await store.update(
        ChatGoal,
        draft.id,
        {"status": ChatGoalStatus.RUNNING, "started_at": utc_now(), "active_since": utc_now()},
        expected_revision=draft.revision,
    )
    client = TestClient(app)
    path = f"/api/v1/engagements/{project.id}/hold"
    auth = {"Authorization": "Bearer test-token"}
    assert (await client.post(path, json={"reason": "Core deployment"})).status_code == 401

    placed = await client.post(
        path, headers=auth, json={"reason": "Core deployment", "grace_seconds": 0}
    )
    assert placed.status_code == 200
    body = placed.json()
    assert body["held"] is True and body["running_goals"] == 0
    hold_id = body["hold"]["id"]
    paused = await goals.get(main.id)
    assert paused.status == ChatGoalStatus.PAUSED

    refused = await client.post(
        f"/api/v1/chat/sessions/{main.id}/goal/actions",
        headers=auth,
        json={"expected_revision": paused.revision, "action": "resume"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "project_held"

    released = await client.post(
        f"{path}/{hold_id}/release", headers=auth, json={"reason": "deployed"}
    )
    assert released.status_code == 200
    assert released.json()["held"] is False
    assert released.json()["hold"]["status"] == "released"
    assert (await goals.get(main.id)).status == ChatGoalStatus.RUNNING
