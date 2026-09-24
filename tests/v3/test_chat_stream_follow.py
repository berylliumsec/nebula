"""A viewer keeps following provider work across pauses, restarts and recovery.

These cover what the chat page relies on to show turns it did not start: frames
name the runtime that produced them, a runtime's last frames stay readable for a
reconnecting viewer, and the authorities agree on an interrupted turn.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import nebula.v3.api as api_module
from nebula.v3.api import create_app
from nebula.v3.chat import ChatService, _ActiveProviderTurn
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import (
    ChatGoal,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
)
from nebula.v3.session_state import session_state
from nebula.v3.storage import NebulaStore

AUTH = {"Authorization": "Bearer test-token"}


def _frames(response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _conversation(tmp_path) -> NebulaStore:
    store = NebulaStore(tmp_path / "core.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    store.create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Follow",
            provider_profile_id="provider",
            model="model",
        )
    )
    return store


def _interrupted_turn(store: NebulaStore, recovery: dict, goal_id: str | None = None):
    return store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            goal_id=goal_id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
            error="Core restarted before this response completed.",
            request_snapshot={"recovery": recovery},
        )
    )


def test_a_cursor_from_another_runtime_replays_this_runtime_from_its_first_frame(
    tmp_path,
):
    async def scenario():
        service = ChatService(NebulaStore(tmp_path / "core.db"))
        # The runtime a restarted Core resumed: its sequences start at 1 again.
        runtime = _ActiveProviderTurn(
            events=[
                ("started", {"type": "started", "turn_id": "turn"}),
                ("delta", {"type": "delta", "delta": "saved "}),
                ("delta", {"type": "delta", "delta": "new"}),
            ],
            done=True,
        )
        service._active_provider_turns["turn"] = runtime

        # The viewer had read 40 frames of the runtime that ran before.
        stale = [
            payload
            async for _, payload in service.follow_provider_turn(
                "turn", after_sequence=40, epoch="previous-runtime"
            )
        ]
        assert [payload["sequence"] for payload in stale] == [1, 2, 3]
        assert {payload["epoch"] for payload in stale} == {runtime.epoch}

        # The same runtime's cursor still resumes exactly after what it saw.
        same = [
            payload
            async for _, payload in service.follow_provider_turn(
                "turn", after_sequence=2, epoch=runtime.epoch
            )
        ]
        assert [(payload["sequence"], payload["delta"]) for payload in same] == [
            (3, "new")
        ]
        assert _ActiveProviderTurn().epoch != runtime.epoch
        await service.shutdown()

    asyncio.run(scenario())


def test_a_reconnecting_viewer_receives_the_retained_pause_frame(tmp_path, monkeypatch):
    services: list[ChatService] = []

    class RecordedChatService(ChatService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            services.append(self)

    monkeypatch.setattr(api_module, "ChatService", RecordedChatService)
    store = _conversation(tmp_path)
    store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.WAITING_CALLBACK,
        )
    )
    with TestClient(create_app(store, auth_token="test-token")) as client:
        # The turn parked on a subagent wait; its runtime ended right after
        # the pause frame, which the viewer's dropped connection never got.
        runtime = _ActiveProviderTurn(
            events=[
                ("started", {"type": "started", "turn_id": "turn"}),
                (
                    "callback_required",
                    {
                        "type": "callback_required",
                        "turn_id": "turn",
                        "tool_call_id": "call",
                        "subagent_ids": ["child"],
                        "summary": "Waiting for 1 subagent to report.",
                    },
                ),
            ],
            done=True,
        )
        services[0]._active_provider_turns["turn"] = runtime

        response = client.get("/api/v1/chat/turns/turn/events?after=1", headers=AUTH)
        assert response.status_code == 200, response.text
        frames = _frames(response)
        assert [frame["type"] for frame in frames] == ["callback_required"]
        assert frames[0]["sequence"] == 2
        epoch = frames[0]["epoch"]
        assert (
            _frames(
                client.get(
                    f"/api/v1/chat/turns/turn/events?after=2&epoch={epoch}",
                    headers=AUTH,
                )
            )
            == []
        )

        # Another tab attaching after that viewer left still reads the frames.
        again = client.get("/api/v1/chat/turns/turn/events?after=0", headers=AUTH)
        assert [frame["type"] for frame in _frames(again)] == [
            "started",
            "callback_required",
        ]
        assert store.get(ChatTurn, "turn").status == ChatTurnStatus.WAITING_CALLBACK


@pytest.mark.parametrize(
    "recovery,execution,activity",
    [
        ({"required": True, "cause": "core_restart"}, "recovering", "working"),
        # Unresolved effects are recorded as uncertain before Core resumes.
        (
            {
                "required": True,
                "cause": "core_restart",
                "unknown_tool_call_ids": ["uncertain-call"],
            },
            "recovering",
            "working",
        ),
        # Core already tried once; only the operator can settle it now.
        (
            {
                "required": True,
                "cause": "core_restart",
                "auto_resume_attempted_at": "2026-09-24T10:00:00+00:00",
            },
            "needs_stop",
            "waiting",
        ),
    ],
)
def test_an_interrupted_turn_awaiting_recovery_is_busy_and_stoppable(
    tmp_path, recovery, execution, activity
):
    store = _conversation(tmp_path)
    _interrupted_turn(store, recovery)

    state = session_state(store, store.get(ChatSession, "chat"))
    assert state["turn_id"] == "turn"
    assert state["execution"] == execution
    assert state["busy"] is True
    assert "stop" in state["actions"]

    # Lifespan is left out: its startup pass would try to resume the turn.
    client = TestClient(create_app(store, auth_token="test-token"))
    listed = client.get(
        "/api/v1/chat/session-activity?engagement_id=project", headers=AUTH
    ).json()
    assert [(item["session_id"], item["state"]) for item in listed] == [
        ("chat", activity)
    ]
    pending = client.get("/api/v1/chat/sessions/chat/pending-turn", headers=AUTH)
    assert pending.json()["id"] == "turn"
    assert pending.json()["recoverable"] is (execution == "recovering")


def test_a_waiting_turn_says_whether_it_waits_for_subagents_or_a_command(tmp_path):
    store = _conversation(tmp_path)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.WAITING_CALLBACK,
            tool_history=[
                {
                    "step": 0,
                    "tool_call_id": "wait-call",
                    "name": "wait_subagents",
                    "status": "waiting_callback",
                    "subagent_wait": {"ids": ["child-a", "child-b"], "mode": "all"},
                    "result_summary": "Waiting for 2 subagents to report.",
                }
            ],
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    waiting = client.get("/api/v1/chat/sessions/chat/pending-turn", headers=AUTH).json()
    assert waiting["wait_kind"] == "subagents"
    assert waiting["wait_summary"] == "Waiting for 2 subagents to report."
    assert waiting["subagent_ids"] == ["child-a", "child-b"]
    assert waiting["results_url"] is None

    store.update(
        ChatTurn,
        turn.id,
        {
            "tool_history": [
                {
                    "step": 0,
                    "tool_call_id": "command-call",
                    "name": "run_command",
                    "status": "waiting_callback",
                    "process_id": "process",
                    "results_url": "http://core/api/v1/automation-processes/p/results",
                }
            ]
        },
        expected_revision=turn.revision,
    )
    command = client.get("/api/v1/chat/sessions/chat/pending-turn", headers=AUTH).json()
    assert command["wait_kind"] == "process"
    assert command["wait_summary"] is None
    assert command["subagent_ids"] == []
    assert command["process_id"] == "process"


@pytest.mark.parametrize("policy", ["goal cancelled", "time budget spent"])
def test_restart_recovery_settles_a_goal_turn_it_will_never_resume(tmp_path, policy):
    store = _conversation(tmp_path)
    goals = ChatGoalService(store)
    goal = goals.create(
        "chat",
        GoalCreate(
            objective="Continue",
            completion_criteria=["Done"],
            time_budget_seconds=60,
        ),
    )
    goal = goals.write(
        "chat", GoalWrite(expected_revision=goal.revision, action="start")
    )
    goal = goals.write(
        "chat", GoalWrite(expected_revision=goal.revision, action="pause")
    )
    _interrupted_turn(store, {"required": True, "cause": "core_restart"}, goal.id)
    if policy == "goal cancelled":
        goals.write("chat", GoalWrite(expected_revision=goal.revision, action="cancel"))
    else:
        store.update(
            ChatGoal, goal.id, {"elapsed_seconds": 60}, expected_revision=goal.revision
        )
    service = ChatService(store)
    assert service.pending_turn("chat") is not None

    assert service.resume_turns_stopped_by_core() == []

    settled = store.get(ChatTurn, "turn")
    assert settled.status == ChatTurnStatus.CANCELLED
    assert settled.error is not None
    assert settled.error.startswith("Core did not resume this interrupted response")
    assert service.pending_turn("chat") is None
    notes = [
        message
        for message in store.list_session_entities(ChatMessage, "chat")
        if message.metadata.get("chat_turn_id") == "turn"
    ]
    assert len(notes) == 1
    assert "Core did not resume this interrupted response" in notes[0].content
    # A later pass has nothing left to settle or resume.
    assert service.resume_turns_stopped_by_core() == []
    assert store.get(ChatTurn, "turn").revision == settled.revision


def test_restart_recovery_still_resumes_a_paused_goal_with_budget_left(tmp_path):
    store = _conversation(tmp_path)
    goals = ChatGoalService(store)
    goal = goals.create(
        "chat", GoalCreate(objective="Continue", completion_criteria=["Done"])
    )
    goal = goals.write(
        "chat", GoalWrite(expected_revision=goal.revision, action="start")
    )
    goals.write("chat", GoalWrite(expected_revision=goal.revision, action="pause"))
    _interrupted_turn(store, {"required": True, "cause": "core_restart"}, goal.id)

    ChatService(store).resume_turns_stopped_by_core()

    # Resuming may fail here (no provider), but the turn is never settled.
    assert store.get(ChatTurn, "turn").status != ChatTurnStatus.CANCELLED
