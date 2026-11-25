from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import timedelta
from typing import Any

import pytest

from nebula.v3.butler_stuck_turns import (
    RECOVERY_HISTORY_KEY,
    ButlerStuckTurns,
    find_stuck_turns,
    recover_turn,
    sweep_stuck_turns,
    threshold_minutes,
)
from nebula.v3.butler_supervisor_policy import SupervisorDisabled, load_policy
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore

LATER = timedelta(minutes=20)


class FakeChat:
    """ChatService's pending-turn and stop paths, without a provider."""

    def __init__(self, store: NebulaStore, goals: ChatGoalService) -> None:
        self.store = store
        self.goals = goals
        self.stopped: list[str] = []
        self.before_pending: Any = None

    async def pending_turn(self, session_id: str) -> ChatTurn | None:
        if self.before_pending is not None:
            hook, self.before_pending = self.before_pending, None
            await hook()
        turns = await self.store.find_entities(
            ChatTurn, {}, session_id=session_id, newest_first=True
        )
        for turn in turns:
            if turn.status not in {
                ChatTurnStatus.COMPLETE,
                ChatTurnStatus.FAILED,
                ChatTurnStatus.CANCELLED,
            }:
                return turn
        return None

    async def stop_provider_turn(self, turn_id: str) -> None:
        # Core's cancel_turn: cancel the turn, then pause its running goal.
        self.stopped.append(turn_id)
        turn = await self.store.get(ChatTurn, turn_id)
        await self.store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.CANCELLED, "error": "stopped"},
            expected_revision=turn.revision,
        )
        goal = await self.goals.get(turn.session_id)
        await self.goals.write(
            turn.session_id,
            GoalWrite(
                expected_revision=goal.revision,
                action="pause",
                reason="Response stopped by the operator. Resume the goal when ready.",
            ),
        )


class FakeWork:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.goals = ChatGoalService(store)
        self.chat = FakeChat(store, self.goals)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def goal_action(self, project_id: str, arguments: dict[str, Any]) -> None:
        self.calls.append(("goal_action", arguments))
        await self.goals.write(
            arguments["session_id"],
            GoalWrite.model_validate(
                {k: v for k, v in arguments.items() if k != "session_id"}
            ),
        )


def _policy(tmp_path, project_ids=(), *, enabled=True):
    env = {
        "NEBULA_V3_DATA_DIR": str(tmp_path / "data"),
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "NEBULA_BUTLER_HYGIENE_PROJECT_IDS": ",".join(project_ids),
    }
    if enabled:
        env["NEBULA_BUTLER_SUPERVISOR_ENABLED"] = "1"
    return load_policy(env)


def _audit(tmp_path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _setup(store: NebulaStore, *, goal_status=ChatGoalStatus.RUNNING):
    project = await store.create(Engagement(name="Research"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Goal chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    now = utc_now()
    goal = await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective="Map the surface",
            completion_criteria=["Done"],
            status=goal_status,
            started_at=now,
            active_since=now if goal_status == ChatGoalStatus.RUNNING else None,
            paused_at=now if goal_status == ChatGoalStatus.PAUSED else None,
            blocked_reason="Paused" if goal_status == ChatGoalStatus.PAUSED else None,
        )
    )
    return project, profile, session, goal


async def _turn(store, project, profile, session, goal, status, *, goal_turn=True):
    return await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            goal_id=goal.id if goal_turn else None,
            provider_profile_id=profile.id,
            model="model-a",
            status=status,
        )
    )


def test_threshold_is_clamped():
    assert threshold_minutes({}) == 12
    assert threshold_minutes({"NEBULA_BUTLER_STUCK_TURN_MINUTES": "3"}) == 10
    assert threshold_minutes({"NEBULA_BUTLER_STUCK_TURN_MINUTES": "500"}) == 60
    assert threshold_minutes({"NEBULA_BUTLER_STUCK_TURN_MINUTES": "25"}) == 25
    assert threshold_minutes({"NEBULA_BUTLER_STUCK_TURN_MINUTES": "x"}) == 12


async def test_find_lists_only_stale_goal_turns_in_stuck_states(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    stuck = await _turn(
        store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK
    )
    # Waiting for the operator by design; never listed.
    await _turn(store, project, profile, session, goal, ChatTurnStatus.WAITING_APPROVAL)
    await _turn(
        store,
        project,
        profile,
        session,
        goal,
        ChatTurnStatus.WAITING_CALLBACK,
        goal_turn=False,
    )
    # Fresh: nothing is stuck yet.
    assert await find_stuck_turns(store, [project.id]) == []
    found = await find_stuck_turns(store, [project.id], now=utc_now() + LATER)
    assert [item.turn_id for item in found] == [stuck.id]
    assert found[0].auto_eligible
    assert found[0].status == "waiting_callback"
    assert found[0].threshold_minutes == 12
    assert found[0].last_progress_at == stuck.updated_at.isoformat()
    # Every project when none are named.
    assert [
        item.turn_id for item in await find_stuck_turns(store, now=utc_now() + LATER)
    ] == [stuck.id]


async def test_paused_goal_turn_is_not_stuck(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(
        store, goal_status=ChatGoalStatus.PAUSED
    )
    await _turn(store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK)
    assert await find_stuck_turns(store, [project.id], now=utc_now() + LATER) == []


async def test_recover_stops_resumes_and_records_backoff(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    turn = await _turn(
        store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK
    )
    work = FakeWork(store)
    policy = _policy(tmp_path, [project.id])
    receipt = await recover_turn(
        store,
        work,
        session_id=session.id,
        turn_id=turn.id,
        expected_last_progress_at=turn.updated_at.isoformat(),
        policy=policy,
        now=utc_now() + LATER,
    )
    assert receipt["outcome"] == "recovered", receipt
    assert receipt["detail"] == "stopped_and_resumed"
    assert receipt["resumed"] is True
    assert receipt["cancelled_status"] == "cancelled"
    assert work.chat.stopped == [turn.id]
    assert work.calls[-1][1]["action"] == "resume"
    saved = await store.get(ChatGoal, goal.id)
    assert saved.status == ChatGoalStatus.RUNNING
    assert len(saved.metadata[RECOVERY_HISTORY_KEY]) == 1
    (entry,) = _audit(tmp_path)
    assert entry["tool"] == "stuck_turn_recovery" and entry["allowed"] is True


async def test_recover_refuses_changed_progress_and_raced_turn(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    turn = await _turn(
        store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK
    )
    work = FakeWork(store)
    later = utc_now() + LATER
    refused = await recover_turn(
        store,
        work,
        session_id=session.id,
        turn_id=turn.id,
        expected_last_progress_at=(turn.updated_at - timedelta(seconds=1)).isoformat(),
        now=later,
    )
    assert refused["outcome"] == "refused"
    assert refused["detail"] == "progress_changed"

    # The turn writes progress between the check and the stop.
    async def progressed() -> None:
        current = await store.get(ChatTurn, turn.id)
        await store.update(
            ChatTurn,
            current.id,
            {"content": "moved"},
            expected_revision=current.revision,
        )

    work.chat.before_pending = progressed
    raced = await recover_turn(
        store,
        work,
        session_id=session.id,
        turn_id=turn.id,
        expected_last_progress_at=turn.updated_at.isoformat(),
        now=later,
    )
    assert raced["outcome"] == "refused"
    assert raced["detail"] == "turn_changed"
    assert work.chat.stopped == []
    assert (
        await store.get(ChatTurn, turn.id)
    ).status == ChatTurnStatus.WAITING_CALLBACK

    not_stuck = await recover_turn(store, work, session_id=session.id, turn_id=turn.id)
    assert not_stuck["detail"] == "not_stuck"


async def test_recover_backs_off_after_three_per_hour(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    now = utc_now()
    await store.update(
        ChatGoal,
        goal.id,
        {
            "metadata": {
                RECOVERY_HISTORY_KEY: [
                    (now - timedelta(minutes=m)).isoformat() for m in (5, 10, 15)
                ]
            }
        },
        expected_revision=goal.revision,
    )
    turn = await _turn(
        store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK
    )
    work = FakeWork(store)
    receipt = await recover_turn(
        store,
        work,
        session_id=session.id,
        turn_id=turn.id,
        now=now + LATER,
    )
    # All three are within the hour at now + 20 minutes.
    assert receipt["detail"] == "backoff"
    assert work.chat.stopped == []


async def test_sweep_is_opt_in_and_recovers(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    turn = await _turn(store, project, profile, session, goal, ChatTurnStatus.ROUTING)
    work = FakeWork(store)
    policy = _policy(tmp_path, [project.id])
    off = await sweep_stuck_turns(
        store, work, policy=policy, now=utc_now() + LATER, environ={}
    )
    assert off == {"enabled": False, "recoveries": [], "skipped": []}
    assert work.chat.stopped == []

    async def no_sleep(_seconds: float) -> None:
        return None

    on = await sweep_stuck_turns(
        store,
        work,
        policy=policy,
        now=utc_now() + LATER,
        environ={"NEBULA_BUTLER_STUCK_TURN_AUTO": "1"},
        sleep=no_sleep,
    )
    assert on["enabled"] is True
    assert [item["turn_id"] for item in on["recoveries"]] == [turn.id]
    assert on["recoveries"][0]["outcome"] == "recovered"
    assert work.chat.stopped == [turn.id]


async def test_butler_tool_lists_and_recovers(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project, profile, session, goal = await _setup(store)
    turn = await _turn(
        store, project, profile, session, goal, ChatTurnStatus.WAITING_CALLBACK
    )
    work = FakeWork(store)
    disabled = ButlerStuckTurns(
        store, work, project.id, policy_loader=lambda: _policy(tmp_path, enabled=False)
    )
    (action,) = disabled.actions()
    assert action.spec.name == "nebula.supervisor.recover_stuck_turn"
    with pytest.raises(SupervisorDisabled):
        await action._handler({"action": "list"})

    tool = ButlerStuckTurns(
        store, work, project.id, policy_loader=lambda: _policy(tmp_path)
    )
    listed = await tool.run({"action": "list"})
    assert listed["stuck_turns"] == []
    with pytest.raises(PermissionError):
        await tool.run({"action": "list", "project_ids": ["elsewhere"]})
    with pytest.raises(ValueError):
        await tool.run({"action": "recover", "turn_id": turn.id})
    receipt = await tool.run(
        {
            "action": "recover",
            "session_id": session.id,
            "turn_id": turn.id,
            "expected_last_progress_at": turn.updated_at.isoformat(),
        }
    )
    # Not stale yet under the real clock: refused, and nothing stopped.
    assert receipt["outcome"] == "refused" and receipt["detail"] == "not_stuck"
    assert work.chat.stopped == []
