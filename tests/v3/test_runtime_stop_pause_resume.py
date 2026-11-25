"""A project runtime stop is named as such, and its goals resume or say why not.

A deployment handoff stops a project's worker while Core keeps running. The
interrupted responses must not claim a Core restart, the promise of an
automatic resume must hold, and a failed automatic resume must say so
instead of still promising one. Dead-owner reconciliation of the same stop is
covered in test_runtime_orphan_recovery.py, which runs against PostgreSQL.
"""

from tests.v3.async_test_support import (
    close_database_handles as close_database_handles,
)

import asyncio

import pytest

from nebula.v3.butler_goal_hygiene import classify_reason
from nebula.v3.chat import ChatHistoryConflict, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    HarnessTurn,
    HarnessTurnStatus,
    utc_now,
)
from nebula.v3.domain import GOAL_COMPLETION_REVIEW_REASON
from nebula.v3.model_refusal import PAUSE_PREFIX as MODEL_REFUSAL_PAUSE_PREFIX
from nebula.v3.chat import RETRIEVAL_TOOL_NAMES
from nebula.v3.recovery_markers import (
    HARNESS_CORE_RESTART_PAUSE,
    HARNESS_RUNTIME_STOP,
    PROJECT_RUNTIME_STOP_CAUSE,
    harness_stop_cause,
)
from tests.v3.test_chat import FakeProvider
from tests.v3.test_harness_chat_goals import _new_goal
from tests.v3.test_harnesses import _runtime
from tests.v3.test_restart_recovery_lifecycle import (
    _conversation,
    _model_request,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- harness startup settlement ---------------------------------------------


class _Turn:
    def __init__(self, metadata):
        self.metadata = metadata


def test_harness_stop_cause_names_the_runtime_stop():
    restart = harness_stop_cause(_Turn({}))
    assert restart.goal_pause == HARNESS_CORE_RESTART_PAUSE
    assert restart.reason == "core_restart"
    deploy = harness_stop_cause(
        _Turn({HARNESS_RUNTIME_STOP: {"deployment_handoff": True}})
    )
    assert deploy.reason == PROJECT_RUNTIME_STOP_CAUSE
    assert deploy.goal_pause.startswith(
        "Project runtime stopped for a deployment handoff during a harness goal turn;"
    )
    assert "Core did not restart" in deploy.goal_pause
    assert "Core restarted" not in deploy.goal_pause + deploy.queued_goal_pause
    crash = harness_stop_cause(
        _Turn({HARNESS_RUNTIME_STOP: {"deployment_handoff": False}})
    )
    assert crash.goal_pause.startswith(
        "Project runtime stopped during a harness goal turn;"
    )
    assert "deployment" not in crash.goal_pause + crash.queued_goal_pause


@pytest.mark.parametrize("deployment", [False, True])
def test_butler_hygiene_keeps_classifying_harness_stop_pauses(deployment):
    cause = harness_stop_cause(
        _Turn({HARNESS_RUNTIME_STOP: {"deployment_handoff": deployment}})
    )
    for reason in (cause.goal_pause, cause.queued_goal_pause):
        goal = ChatGoal(
            engagement_id="one",
            session_id="synthetic-session",
            objective="Synthetic",
            completion_criteria=["done"],
            status=ChatGoalStatus.PAUSED,
            blocked_reason=reason,
        )
        assert classify_reason(goal) == ("transient", "core_transient")


@pytest.mark.parametrize("deployment", [None, False, True])
@pytest.mark.parametrize("running", [False, True])
async def test_harness_goal_pause_names_what_stopped(tmp_path, deployment, running):
    store, engagement, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    goals = ChatGoalService(store, runtime)
    created = await _new_goal(goals)
    (
        await goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
    )
    _chat, turn, harness_turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model="test-model",
        prompt="Accepted before the stop",
        chat_session_id=created.session.id,
        goal_id=created.goal.id,
        harness_session_id=None,
        mcp_server_ids=None,
        include_knowledge=False,
    )
    changes: dict = {}
    if running:
        changes["status"] = HarnessTurnStatus.RUNNING
        changes["started_at"] = utc_now()
    if deployment is not None:
        changes["metadata"] = {
            **harness_turn.metadata,
            HARNESS_RUNTIME_STOP: {
                "runtime_id": "synthetic-runtime",
                "fencing_token": 1,
                "deployment_handoff": deployment,
                "stopped_at_ms": 0,
            },
        }
    if changes:
        harness_turn = await store.update(
            HarnessTurn,
            harness_turn.id,
            changes,
            expected_revision=harness_turn.revision,
        )
    await runtime.startup()
    settled = await store.get(HarnessTurn, harness_turn.id)
    assert settled.status == HarnessTurnStatus.INTERRUPTED
    # Settled once; a later real Core restart must not read a stale mark.
    assert HARNESS_RUNTIME_STOP not in settled.metadata
    assert (await store.get(ChatTurn, turn.id)).status == ChatTurnStatus.INTERRUPTED
    goal = await goals.get(created.session.id)
    assert goal.status == ChatGoalStatus.PAUSED
    reason = goal.blocked_reason or ""
    if deployment is None:
        assert reason.startswith("Core restarted")
        assert (settled.error or "").startswith("Nebula Core restarted")
    else:
        assert "Core restarted" not in reason
        assert "Core did not restart" in reason
        assert ("deployment handoff" in reason) is deployment
        assert (settled.error or "").startswith("The project runtime stopped")
    assert classify_reason(goal)[0] == "transient"
    await runtime.shutdown()


# --- automatic resume ---------------------------------------------------------


async def _stopped_goal_turn(tmp_path, name):
    store, engagement, profile, session = await _conversation(tmp_path, name)
    reason = (
        "Project runtime stopped for a deployment handoff while an effect outcome "
        "was unknown. Core will carry the uncertainty forward and resume "
        "automatically without replaying the effect."
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=engagement.id,
            session_id=session.id,
            objective="Keep going",
            completion_criteria=["done"],
            status=ChatGoalStatus.PAUSED,
            paused_at=utc_now(),
            blocked_reason=reason,
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.INTERRUPTED,
            error=reason,
            request_snapshot={
                "model_request": _model_request(),
                "context_usage": {},
                "recovery": {
                    "required": True,
                    "cause": PROJECT_RUNTIME_STOP_CAUSE,
                    "unknown_tool_call_ids": [],
                },
            },
        )
    )
    service = ChatService(
        store, provider_factory=lambda _: FakeProvider(profile.id, local=True)
    )
    return store, goal, turn, service


async def _resume_and_collect(service, turn):
    continued: list = []
    original_start = service.start_provider_turn

    async def start(prepared, **kwargs):
        if prepared.turn.id != turn.id:
            continued.append(prepared)
            return prepared.turn.id
        return await original_start(prepared, **kwargs)

    service.start_provider_turn = start  # type: ignore[method-assign]
    resumed = await service.resume_turns_stopped_by_core()
    for _ in range(200):
        if continued or not resumed:
            break
        await asyncio.sleep(0.02)
    return resumed, continued


async def test_goal_paused_by_a_runtime_stop_resumes_automatically(tmp_path):
    store, goal, turn, service = await _stopped_goal_turn(tmp_path, "runtime-stop")

    async def scenario():
        await service.startup()
        resumed, continued = await _resume_and_collect(service, turn)
        await service.shutdown()
        return resumed, continued

    resumed, continued = await scenario()
    assert resumed == [turn.id]
    assert (await store.get(ChatTurn, turn.id)).status == ChatTurnStatus.COMPLETE
    assert continued and continued[0].turn.goal_id == goal.id


async def test_failed_automatic_resume_stops_promising_one(tmp_path):
    store, goal, turn, service = await _stopped_goal_turn(tmp_path, "resume-fails")
    original_prepare = service.prepare_resume
    failures = [ChatHistoryConflict("synthetic refusal")]

    async def prepare(turn_id):
        if failures:
            raise failures.pop()
        return await original_prepare(turn_id)

    service.prepare_resume = prepare  # type: ignore[method-assign]

    async def scenario():
        await service.startup()
        first = await service.resume_turns_stopped_by_core()
        waiting = await store.get(ChatGoal, goal.id)
        parked = await store.get(ChatTurn, turn.id)
        # The next recovery pass retries it and the goal continues.
        resumed, continued = await _resume_and_collect(service, turn)
        await service.shutdown()
        return first, waiting, parked, resumed, continued

    first, waiting, parked, resumed, continued = await scenario()
    assert first == []
    assert waiting.status == ChatGoalStatus.PAUSED
    assert waiting.blocked_reason == (
        "Automatic recovery is waiting for the next Core recovery pass "
        "(the last attempt failed: ChatHistoryConflict)."
    )
    assert "resume automatically" not in waiting.blocked_reason
    assert parked.status == ChatTurnStatus.INTERRUPTED
    assert parked.request_snapshot["recovery"]["automatic_retry_pending"] is True
    assert resumed == [turn.id]
    assert continued and continued[0].turn.goal_id == goal.id


async def test_failed_resume_keeps_an_operator_pause_reason(tmp_path):
    from nebula.v3.recovery_markers import OPERATOR_RECOVERY_PAUSE

    store, goal, turn, service = await _stopped_goal_turn(tmp_path, "operator-pause")
    goal = await store.update(
        ChatGoal,
        goal.id,
        {"metadata": {OPERATOR_RECOVERY_PAUSE: True}},
        expected_revision=goal.revision,
    )
    await service._restate_recovery_pause(
        goal, "Automatic recovery is waiting.", interruption=turn.error
    )
    assert (await store.get(ChatGoal, goal.id)).blocked_reason == goal.blocked_reason


@pytest.mark.parametrize(
    "reason",
    [
        GOAL_COMPLETION_REVIEW_REASON,
        MODEL_REFUSAL_PAUSE_PREFIX + "synthetic): review the refused request.",
    ],
)
async def test_failed_resume_keeps_a_pause_reason_it_does_not_own(tmp_path, reason):
    store, goal, turn, service = await _stopped_goal_turn(tmp_path, "foreign-pause")
    goal = await store.update(
        ChatGoal, goal.id, {"blocked_reason": reason}, expected_revision=goal.revision
    )
    attempts = []

    async def prepare(turn_id):
        attempts.append(turn_id)
        raise ChatHistoryConflict("synthetic refusal")

    service.prepare_resume = prepare  # type: ignore[method-assign]

    async def scenario():
        await service.startup()
        result = await service.resume_turns_stopped_by_core()
        await service.shutdown()
        return result

    assert await scenario() == []
    assert attempts == [turn.id]
    kept = await store.get(ChatGoal, goal.id)
    assert kept.status == ChatGoalStatus.PAUSED
    assert kept.blocked_reason == reason


@pytest.mark.parametrize("runtime_stop", [False, True])
def test_unknown_step_tells_the_model_what_stopped(runtime_stop):
    from nebula.v3.chat import _intent_only_restart_entry

    for name in ("workspace.write", sorted(RETRIEVAL_TOOL_NAMES)[0]):
        entry, _event = _intent_only_restart_entry(
            {"tool_call_id": "synthetic-call", "name": name, "step": 1},
            runtime_stop=runtime_stop,
        )
        text = entry["provider_result"] + entry["result_summary"]
        assert ("Core restart" in text) is not runtime_stop
        assert ("project runtime stopped" in text) is runtime_stop
