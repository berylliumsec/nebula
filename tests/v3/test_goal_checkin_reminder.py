"""A quiet running goal is reminded, in its turn, to post a Work check-in."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from nebula.v3 import chat_goal_addenda
import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goal_addenda import (
    ADDENDUM_HEADING,
    CHECKIN_MINUTES_ENV,
    CHECKIN_REMINDER_METADATA,
    CHECKIN_TOOL_CALLS_ENV,
    GOAL_READ_TOOL_NAME,
    SEEN_KEY,
    CheckinReminder,
    GoalAddenda,
    checkin_reminder_thresholds,
    guidance_addendum,
)
from nebula.v3.chat_goals import ChatGoalService
from nebula.v3.domain import (
    ButlerGoalWatch,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    HarnessTurn,
    HarnessTurnOrigin,
    ProviderProfile,
    WorkItem,
    WorkUpdate,
)
from nebula.v3.harnesses import HarnessRuntimeService, HarnessStateError
from nebula.v3.providers import ModelRequest, ModelResponse, ToolChoice
from nebula.v3.butler_oversight import watch_id
from nebula.v3.storage import NebulaStore, NotFoundError
from tests.v3.test_chat import FakeProvider, _admitted_stream
from tests.v3.test_goal_supervisor_addenda import _running, _setup

WORK_TOOLS = ["goal.read", "work_check_in", "work_create", "work_list"]


async def _turn(
    store: NebulaStore,
    goal: ChatGoal,
    *,
    turn_id: str = "turn",
    tools: list[str] = WORK_TOOLS,
    calls: int = 0,
) -> ChatTurn:
    return await store.create(
        ChatTurn(
            id=turn_id,
            engagement_id="project",
            session_id="session",
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model",
            execution_tool_calls=calls,
            request_snapshot={
                "available_tool_names": tools,
                SEEN_KEY: {"guidance_revision": goal.guidance_revision},
            },
        )
    )


class Ledger:
    """The steps a provider turn saved, as the turn ledger folds them."""

    def __init__(self) -> None:
        self.steps: dict[str, list[dict[str, Any]]] = {}

    async def history(self, turn: ChatTurn) -> list[dict[str, Any]]:
        return list(self.steps.get(turn.id, []))

    def save(self, turn: ChatTurn, delivery: tuple) -> None:
        name, _, _, marker = delivery
        self.steps.setdefault(turn.id, []).append(
            {"name": name, "delivered_by_core": True, SEEN_KEY: marker}
        )


def _addenda(store: NebulaStore, ledger: Ledger, clock, **limits) -> GoalAddenda:
    return GoalAddenda(
        store,
        ledger.history,
        sources=[
            guidance_addendum,
            CheckinReminder(
                store,
                history=ledger.history,
                clock=clock,
                **{"minutes": 30, "tool_calls": 40, **limits},
            ),
        ],
    )


def _text(delivery: tuple) -> str:
    return delivery[1]["addendum"]


async def test_quiet_goal_is_reminded_once_per_window(tmp_path):
    store = await _setup(tmp_path)
    goal = await _running(ChatGoalService(store))
    assert goal.started_at is not None
    now = [goal.started_at + timedelta(minutes=10)]
    ledger = Ledger()
    addenda = _addenda(store, ledger, lambda: now[0])
    turn = await _turn(store, goal)

    assert await addenda.routing_delivery(turn, WORK_TOOLS) is None

    now[0] = goal.started_at + timedelta(minutes=47)
    delivery = await addenda.routing_delivery(turn, WORK_TOOLS)
    assert delivery is not None
    name, output, summary, marker = delivery
    assert name == GOAL_READ_TOOL_NAME
    assert summary == "Check-in reminder delivered"
    assert _text(delivery).startswith(ADDENDUM_HEADING)
    assert "since this goal started 47 min ago" in _text(delivery)
    assert "Post work_check_in" in _text(delivery)
    assert "Work check-in" in output["note"]
    assert "supervisor changed" not in output["note"]
    ledger.save(turn, delivery)

    # Not again in the same window, in this turn or the goal's next one.
    now[0] += timedelta(minutes=5)
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is None
    saved = await store.get(ChatGoal, goal.id)
    assert saved.metadata[CHECKIN_REMINDER_METADATA]["turn_id"] == turn.id
    next_turn = await _turn(store, saved, turn_id="next-turn")
    assert await addenda.routing_delivery(next_turn, WORK_TOOLS) is None

    # A full window after the reminder, it is due once more.
    now[0] = goal.started_at + timedelta(minutes=47 + 31)
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is not None


async def test_tool_calls_without_a_check_in_are_reminded(tmp_path):
    store = await _setup(tmp_path)
    goal = await _running(ChatGoalService(store))
    assert goal.started_at is not None
    now = goal.started_at + timedelta(minutes=5)
    ledger = Ledger()
    addenda = _addenda(store, ledger, lambda: now)
    turn = await _turn(store, goal, calls=39)
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is None

    turn = await store.update(ChatTurn, turn.id, {"execution_tool_calls": 52})
    delivery = await addenda.routing_delivery(turn, WORK_TOOLS)
    assert delivery is not None
    assert "52 tool calls in this turn" in _text(delivery)
    ledger.save(turn, delivery)

    # Counted again from the reminder.
    turn = await store.update(ChatTurn, turn.id, {"execution_tool_calls": 91})
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is None
    turn = await store.update(ChatTurn, turn.id, {"execution_tool_calls": 92})
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is not None


async def test_a_check_in_resets_both_windows(tmp_path):
    store = await _setup(tmp_path)
    goal = await _running(ChatGoalService(store))
    assert goal.started_at is not None
    now = goal.started_at + timedelta(minutes=50)
    ledger = Ledger()
    addenda = _addenda(store, ledger, lambda: now)
    turn = await _turn(store, goal, calls=45)
    await store.create(WorkItem(id="item", engagement_id="project", title="Audit"))
    await store.create(
        WorkUpdate(
            engagement_id="project",
            item_id="item",
            summary="Fuzzing the parser",
            status="in_progress",
            actor_kind="agent",
            actor_id="session",
            source_session_id="session",
            source_turn_id=turn.id,
            created_at=goal.started_at + timedelta(minutes=40),
            updated_at=goal.started_at + timedelta(minutes=40),
        )
    )
    # The check-in is a step of this turn, five tool calls ago.
    ledger.steps[turn.id] = [
        {"name": "shell", "budget_class": "execution", "status": "complete"},
        {"name": "work_check_in", "budget_class": "execution", "status": "complete"},
        *(
            {"name": "shell", "budget_class": "execution", "status": "complete"}
            for _ in range(5)
        ),
    ]
    assert await addenda.routing_delivery(turn, WORK_TOOLS) is None

    # Forty calls after the check-in, the count alone makes it due.
    ledger.steps[turn.id].extend(
        {"name": "shell", "budget_class": "execution", "status": "complete"}
        for _ in range(35)
    )
    delivery = await addenda.routing_delivery(turn, WORK_TOOLS)
    assert delivery is not None
    assert "No Work check-in for 10 min" in _text(delivery)
    assert "40 tool calls since your last one in this turn" in _text(delivery)


async def test_butler_reports_and_assigned_item_updates_count_as_check_ins(
    tmp_path,
):
    store = await _setup(tmp_path)
    goal = await _running(ChatGoalService(store))
    assert goal.started_at is not None
    now = goal.started_at + timedelta(minutes=45)
    ledger = Ledger()
    reminder = CheckinReminder(
        store, history=ledger.history, clock=lambda: now, minutes=30, tool_calls=40
    )
    turn = await _turn(store, goal)
    assert await reminder(goal, turn, {}) is not None

    reported = goal.started_at + timedelta(minutes=20)
    try:
        await store.update(
            ButlerGoalWatch, watch_id(goal.id), {"last_progress_at": reported}
        )
    except NotFoundError:
        await store.create(
            ButlerGoalWatch(
                id=watch_id(goal.id),
                engagement_id="project",
                goal_id=goal.id,
                session_id="session",
                status=ChatGoalStatus.RUNNING,
                last_progress_at=reported,
                coverage_reason="test",
            )
        )
    assert (
        await CheckinReminder(
            store, history=ledger.history, clock=lambda: now, minutes=30, tool_calls=40
        )(goal, turn, {})
        is None
    )

    later = goal.started_at + timedelta(minutes=55)
    await store.create(
        WorkItem(
            engagement_id="project",
            title="Assigned",
            assignee_session_id="session",
            last_update_at=goal.started_at + timedelta(minutes=30),
        )
    )
    assert (
        await CheckinReminder(
            store,
            history=ledger.history,
            clock=lambda: later,
            minutes=30,
            tool_calls=40,
        )(goal, turn, {})
        is None
    )


async def test_turn_without_work_tools_is_not_reminded(tmp_path, monkeypatch):
    store = await _setup(tmp_path)
    goal = await _running(ChatGoalService(store))
    assert goal.started_at is not None
    now = goal.started_at + timedelta(hours=3)
    ledger = Ledger()
    addenda = _addenda(store, ledger, lambda: now)
    turn = await _turn(store, goal, tools=["goal.read", "shell"], calls=200)
    assert await addenda.routing_delivery(turn, ["goal.read", "shell"]) is None

    # Thresholds come from bounded settings; 0 turns a rule off.
    monkeypatch.setenv(CHECKIN_MINUTES_ENV, "0")
    monkeypatch.setenv(CHECKIN_TOOL_CALLS_ENV, "999999999")
    assert checkin_reminder_thresholds() == (
        0,
        chat_goal_addenda.MAX_CHECKIN_TOOL_CALLS,
    )
    monkeypatch.setenv(CHECKIN_MINUTES_ENV, "soon")
    assert checkin_reminder_thresholds()[0] == chat_goal_addenda.DEFAULT_CHECKIN_MINUTES


async def test_running_harness_goal_turn_is_steered_to_check_in(tmp_path):
    store = await _setup(tmp_path, backend=ChatBackend.HARNESS)
    goal = await _running(ChatGoalService(store))
    chat_turn = await _turn(store, goal, turn_id="chat-turn", calls=40)
    turn = await store.create(
        HarnessTurn(
            engagement_id="project",
            harness_session_id="harness-session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id="session",
            chat_turn_id=chat_turn.id,
            prompt="Work",
        )
    )
    runtime = HarnessRuntimeService.__new__(HarnessRuntimeService)
    runtime.store = store
    runtime._active = {"harness-session": SimpleNamespace(turn_id=turn.id)}

    async def enabled(engagement_id: str) -> bool:
        return True

    runtime.work = SimpleNamespace(enabled=enabled)  # type: ignore[assignment]
    steered: list[dict] = []

    async def steer_turn(turn_id, text, **kwargs):
        steered.append({"turn_id": turn_id, "text": text, **kwargs})

    runtime.steer_turn = steer_turn  # type: ignore[method-assign]

    await runtime._remind_goal_check_in(turn)
    assert len(steered) == 1
    assert steered[0]["title"] == "Check-in reminder"
    assert steered[0]["text"].startswith(ADDENDUM_HEADING)
    assert "Post work.check_in" in steered[0]["text"]
    assert "create one with work.create" in steered[0]["text"]
    saved = await store.get(ChatGoal, goal.id)
    assert saved.metadata[CHECKIN_REMINDER_METADATA]["tool_calls"] == 40

    # Once per window: more calls short of the next forty are not reminded.
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 70})
    await runtime._remind_goal_check_in(turn)
    assert len(steered) == 1

    # A check-in resets the count.
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 81})
    await runtime._note_goal_check_in(turn)
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 100})
    await runtime._remind_goal_check_in(turn)
    assert len(steered) == 1
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 121})
    await runtime._remind_goal_check_in(turn)
    assert len(steered) == 2
    assert "40 tool calls since your last one" in steered[1]["text"]

    # A harness that cannot be steered (Grok) is not asked again this turn.
    async def cannot_steer(turn_id, text, **kwargs):
        steered.append({"refused": True})
        raise HarnessStateError("Grok ACP does not advertise turn steering")

    runtime.steer_turn = cannot_steer  # type: ignore[method-assign]
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 200})
    await runtime._remind_goal_check_in(turn)
    await store.update(ChatTurn, chat_turn.id, {"execution_tool_calls": 300})
    await runtime._remind_goal_check_in(turn)
    assert len(steered) == 3


async def test_provider_goal_turn_hears_one_reminder_mid_turn(tmp_path, monkeypatch):
    """Through the real routing loop, with thresholds lowered by setting."""

    monkeypatch.setenv(CHECKIN_MINUTES_ENV, "1")
    store = await _setup(tmp_path)
    goals = ChatGoalService(store)
    running = await _running(goals)
    assert running.started_at is not None
    # The goal has run for two hours with no check-in.
    await store.update(
        ChatGoal,
        running.id,
        {"started_at": running.started_at - timedelta(hours=2)},
    )

    def reminders(request: ModelRequest) -> int:
        return json.dumps(request.model_dump(mode="json")).count(
            "No Work check-in since this goal started"
        )

    class QuietProvider(FakeProvider):
        routing_calls = 0
        seen: list[int] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("operation"):
                return await super().complete(request)
            self.requests.append(request)
            if request.tool_choice == ToolChoice.NONE:
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model",
                    text="Still auditing.",
                    finish_reason="stop",
                )
            self.routing_calls += 1
            self.seen.append(reminders(request))
            if self.routing_calls >= 3:
                return ModelResponse(
                    provider_id=self.config.id,
                    model="model",
                    text="Checked in.",
                    finish_reason="stop",
                )
            return ModelResponse(
                provider_id=self.config.id,
                model="model",
                tool_calls=[
                    {
                        "id": f"read-{self.routing_calls}",
                        "name": GOAL_READ_TOOL_NAME,
                        "arguments": {},
                    }
                ],
                finish_reason="tool_calls",
            )

    profile = await store.get(ProviderProfile, "provider")
    capabilities = profile.capabilities.model_dump(mode="python")
    capabilities["tool_calling"] = True
    await store.update(
        ProviderProfile,
        profile.id,
        {
            "capabilities": capabilities,
            "capability_verifications": {
                "model": {"model": "model", "status": "verified"}
            },
        },
    )
    provider = QuietProvider("provider", local=True)
    provider.config.default_model = "model"
    provider.config.model_allowlist = ["model"]
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chat = ChatService(store, workspace_resolver=lambda _: workspace)
    prepared = await chat.prepare_async(
        ChatCompletionRequest(
            provider_id="provider",
            engagement_id="project",
            session_id="session",
            goal_id=running.id,
            messages=[{"role": "user", "content": "Keep auditing"}],
            include_knowledge=False,
            stream=True,
        )
    )
    assert "work_check_in" in prepared.turn.request_snapshot["available_tool_names"]
    async for _ in _admitted_stream(chat, prepared):
        pass
    # Delivered before the first routing call, then only kept in history.
    assert provider.seen == [1, 1, 1]
    turn = await store.get(ChatTurn, prepared.turn.id)
    history = await chat.turn_ledger.history(turn)
    delivered = [entry for entry in history if entry.get("delivered_by_core")]
    assert [entry["result_summary"] for entry in delivered] == [
        "Check-in reminder delivered"
    ]
    assert "checkin_reminded_at" in delivered[0][SEEN_KEY]
    goal = await store.get(ChatGoal, running.id)
    assert goal.metadata[CHECKIN_REMINDER_METADATA]["turn_id"] == turn.id
    await chat.shutdown()
    await asyncio.sleep(0)
