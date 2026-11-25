"""A model refusal pauses its goal visibly and nothing continues it."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from nebula.v3 import goal_runtime_routing as routing
from nebula.v3.butler_board import butler_board_router
from nebula.v3.butler_goal_hygiene import classify_reason
from nebula.v3.butler_oversight import project_stream_id
from nebula.v3.chat_goals import GoalWrite, goals_router
from nebula.v3.diagnostic_guidance import load_catalog, reason_code_for
from nebula.v3.domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    HarnessTurn,
)
from nebula.v3.harnesses import (
    HarnessTurnFailedError,
    _claude_turn_failure,
    _harness_goal_retryable_failure,
)
from nebula.v3.model_refusal import (
    EVENT_TYPE,
    GOAL_METADATA_KEY,
    OPERATOR_ACTION,
    ModelRefusal,
    record_goal_model_refusal,
    refusal_from_error,
    refusal_from_text,
)
from nebula.v3.operator_action_requests import (
    list_requests,
    sweep_operator_actions,
)
from nebula.v3.providers import ProviderRefusalError, _anthropic_refusal
from nebula.v3.recovery_markers import GOAL_PROVIDER_RETRY
from nebula.v3.work import WorkCheckIn, WorkService
from tests.v3.test_goal_runtime_routing import (
    _enable,
    _env,
    _evaluate,
    _manual_harness,
    _provider_goal,
    _stage,
    _state,
)
from tests.v3.test_harness_goal_auto_retry import _reconcile, _scenario, _settled

REFUSAL_TEXT = (
    "API Error: Model X can't help with this. Start a new session to continue. "
    "Details: [category-a]"
)


def _claude_refusal(api_error_status: int | None = 400) -> HarnessTurnFailedError:
    return _claude_turn_failure(
        SimpleNamespace(
            errors=[REFUSAL_TEXT],
            result=None,
            subtype="error_during_execution",
            terminal_reason=None,
            api_error_status=api_error_status,
        )
    )


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [None, 400, 403, 429, 529])
def test_claude_refusal_text_is_a_model_refusal(status):
    error = _claude_refusal(status)
    assert error.reason_code == "model_refusal"
    assert reason_code_for(error, supplied=error.reason_code) == "model_refusal"
    # Never retried as transient, whatever HTTP status rides along.
    assert _harness_goal_retryable_failure(error) is False
    assert refusal_from_text(str(error)) == ModelRefusal("category-a", "Model X")
    assert refusal_from_error(error) == ModelRefusal("category-a", "Model X")


def test_claude_refusal_without_details_keeps_a_category():
    refusal = refusal_from_text("API Error: Model Y cannot help with this.")
    assert refusal == ModelRefusal("unspecified", "Model Y")
    assert refusal_from_text("API Error: 500 internal server error") is None


def test_claude_terminal_refusal_reason_is_a_model_refusal():
    error = _claude_turn_failure(
        SimpleNamespace(
            errors=[],
            result="stopped",
            subtype="error_during_execution",
            terminal_reason="refusal",
            api_error_status=None,
        )
    )
    assert error.reason_code == "model_refusal"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            _anthropic_refusal(
                {
                    "stop_reason": "refusal",
                    "stop_details": {
                        "category": "category-b",
                        "explanation": "synthetic explanation",
                    },
                }
            ),
            "category-b",
        ),
        (ProviderRefusalError("refusal"), "refusal"),
        (ProviderRefusalError("content_filter"), "content_filter"),
        (ProviderRefusalError("guardrail_intervened"), "guardrail_intervened"),
        (
            ProviderRefusalError("policy_code: synthetic provider detail"),
            "policy_code",
        ),
    ],
)
def test_provider_refusal_is_a_model_refusal(error, category):
    assert reason_code_for(error, feature="providers") == "model_refusal"
    refusal = refusal_from_error(error)
    assert refusal is not None and refusal.category == category
    # The model's explanation is not kept.
    assert "synthetic" not in refusal.category


def test_catalog_marks_model_refusal_not_retryable():
    entry = load_catalog()["reason_families"]["model_refusal"]
    assert entry["retryable"] is False


def test_refusal_is_never_a_rate_limit():
    turn = HarnessTurn.model_construct(
        # Even text that mentions a limit and a 429 status stays a refusal.
        error="Claude turn failed: " + REFUSAL_TEXT + " (limit reached)",
        metadata={
            "diagnostic": {
                "reason_code": "model_refusal",
                "vendor_error_info": {"api_error_status": 429},
            }
        },
    )
    assert routing.is_rate_limit_failure(turn) is False


def test_hygiene_never_resumes_a_refusal_pause():
    goal = ChatGoal.model_construct(
        status=ChatGoalStatus.PAUSED,
        blocked_reason=(
            "Model refusal (category-a) on Model X in turn t1: needs operator review"
        ),
    )
    kind, _label = classify_reason(goal)
    assert kind == "never"


# --------------------------------------------------------------------------
# Harness goal turn
# --------------------------------------------------------------------------


async def _project_events(store, project_id: str, event_type: str):
    return [
        event
        for event in await store.replay_operation_events(project_stream_id(project_id))
        if event.event_type == event_type
    ]


async def _refusal_requests(store, session_id: str, *, include_closed=False):
    return [
        view
        for view in await list_requests(store, include_closed=include_closed)
        if view.record is not None
        and view.record.action == OPERATOR_ACTION
        and view.goal_session_id == session_id
    ]


async def test_harness_refusal_pauses_goal_visibly_without_retry(tmp_path):
    store, runtime, goals, session_id, prompts = await _scenario(
        tmp_path, [_claude_refusal], step_budget=5
    )
    try:
        await runtime.dispatch_running_goal(session_id, "Begin work")
        await _settled(runtime)

        [turn] = await store.list_session_entities(ChatTurn, session_id)
        goal = await goals.get(session_id)
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.blocked_reason == (
            f"Model refusal (category-a) on Model X in turn {turn.id}: "
            "needs operator review"
        )
        harness_turn = await store.get(HarnessTurn, turn.harness_turn_id)
        diagnostic = harness_turn.metadata["diagnostic"]
        assert diagnostic["reason_code"] == "model_refusal"
        assert "goal_retryable" not in diagnostic
        assert GOAL_PROVIDER_RETRY not in goal.metadata

        # Counter on the goal.
        state = goal.metadata[GOAL_METADATA_KEY]
        assert state["count"] == 1
        assert state["last"]["turn_id"] == turn.id
        assert state["last"]["harness_turn_id"] == harness_turn.id
        assert state["last"]["category"] == "category-a"
        assert state["last"]["model"] == "Model X"
        assert state["last"]["runtime"] == "harness:harness-a"

        # Event in the project's stream.
        [event] = await _project_events(store, goal.engagement_id, EVENT_TYPE)
        assert event.payload["goal_id"] == goal.id
        assert event.payload["session_id"] == session_id
        assert event.payload["metrics"]["turn_id"] == turn.id
        assert event.payload["metrics"]["category"] == "category-a"
        assert event.payload["metrics"]["model"] == "Model X"
        assert event.payload["metrics"]["runtime"] == "harness:harness-a"
        assert event.payload["metrics"]["at"]

        # One operator action request linking the goal and turn.
        [request] = await _refusal_requests(store, session_id)
        assert request.record is not None
        assert request.record.urgency == "high"
        assert turn.id in request.record.reason
        assert state["request_id"] == request.id

        # Nothing continues it: no retry, no reconcile dispatch, no second
        # prompt.
        assert await _reconcile(runtime) == []
        await _settled(runtime)
        assert len(await store.list_session_entities(ChatTurn, session_id)) == 1
        assert len(prompts) == 1
        assert (await goals.get(session_id)).status == ChatGoalStatus.PAUSED

        # Recording the same turn again changes nothing.
        await record_goal_model_refusal(
            store,
            session_id=session_id,
            chat_turn_id=turn.id,
            refusal=ModelRefusal("category-a", "Model X"),
            runtime="harness:harness-a",
            model=turn.model,
        )
        again = await goals.get(session_id)
        assert again.metadata[GOAL_METADATA_KEY]["count"] == 1
        assert len(await _project_events(store, goal.engagement_id, EVENT_TYPE)) == 1
    finally:
        await runtime.shutdown()


async def test_request_is_deduplicated_and_closing_it_never_resumes(tmp_path):
    store, runtime, goals, session_id, _prompts = await _scenario(
        tmp_path, [_claude_refusal], step_budget=5
    )
    try:
        await runtime.dispatch_running_goal(session_id, "Begin work")
        await _settled(runtime)
        goal = await goals.get(session_id)
        # A second refusal on the paused goal (another turn) counts, records
        # its event, and reuses the open request.
        await record_goal_model_refusal(
            store,
            session_id=session_id,
            chat_turn_id="turn-second",
            refusal=ModelRefusal("category-c"),
            runtime="harness:harness-a",
            model="test-model",
        )
        goal = await goals.get(session_id)
        assert goal.metadata[GOAL_METADATA_KEY]["count"] == 2
        assert len(await _project_events(store, goal.engagement_id, EVENT_TYPE)) == 2
        [request] = await _refusal_requests(store, session_id)

        # The operator marks the request done on the board: it closes, and
        # the goal stays paused.
        await WorkService(store).check_in(
            goal.engagement_id,
            request.id,
            WorkCheckIn(summary="Operator marked this request done.", status="done"),
            actor_kind="operator",
            actor_id="operator",
        )
        await sweep_operator_actions(store)
        assert await _refusal_requests(store, session_id) == []
        assert (await goals.get(session_id)).status == ChatGoalStatus.PAUSED

        # A later refusal opens a new request.
        await record_goal_model_refusal(
            store,
            session_id=session_id,
            chat_turn_id="turn-third",
            refusal=ModelRefusal("category-c"),
            runtime="harness:harness-a",
            model="test-model",
        )
        [reopened] = await _refusal_requests(store, session_id)
        assert reopened.id != request.id
        assert (await goals.get(session_id)).status == ChatGoalStatus.PAUSED
    finally:
        await runtime.shutdown()


async def test_operator_resume_closes_request_and_board_shows_refusal(tmp_path):
    store, runtime, goals, session_id, _prompts = await _scenario(
        tmp_path, [_claude_refusal], step_budget=5
    )
    try:
        await runtime.dispatch_running_goal(session_id, "Begin work")
        await _settled(runtime)
        app = FastAPI()
        app.include_router(goals_router(store), prefix="/api/v1")
        app.include_router(butler_board_router(store), prefix="/api/v1")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            board = (await client.get("/api/v1/butler/board")).json()
            [card] = [
                item for item in board["items"] if item["session_id"] == session_id
            ]
            assert card["section"] == "idle"
            refusal = card["goal"]["model_refusal"]
            assert refusal["paused"] is True
            assert refusal["count"] == 1
            assert refusal["reason"].startswith("Model refusal (category-a)")
            assert card["now"] == "Model refusal (1): needs operator review"
            [action] = [
                item
                for item in board["operator_actions"]
                if item["goal"] and item["goal"]["session_id"] == session_id
            ]
            assert action["action"] == OPERATOR_ACTION
            assert action["action_label"] == "Review model refusal"

            goal = await goals.get(session_id)
            answer = await client.post(
                f"/api/v1/chat/sessions/{session_id}/goal/actions",
                json={"expected_revision": goal.revision, "action": "resume"},
            )
            assert answer.status_code == 200, answer.text
            assert await _refusal_requests(store, session_id) == []
            [closed] = await _refusal_requests(store, session_id, include_closed=True)
            assert closed.status == "closed"

            board = (await client.get("/api/v1/butler/board")).json()
            [card] = [
                item for item in board["items"] if item["session_id"] == session_id
            ]
            # Resumed: the count stays, the paused badge does not.
            assert card["goal"]["model_refusal"] == {
                "paused": False,
                "count": 1,
                "reason": None,
            }
    finally:
        await runtime.shutdown()


# --------------------------------------------------------------------------
# Routing fallback and provider turns
# --------------------------------------------------------------------------


async def test_refusal_does_not_trigger_rate_limit_fallback(tmp_path, monkeypatch):
    _enable(monkeypatch)
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env, mcp=False, step_budget=5)
        await _stage(env, session, "validation")
        session = await _manual_harness(env, session)
        await _evaluate(env, session)
        # A refusal carrying a 429 status is still a refusal, not a limit.
        env.adapter.failures.append(_claude_refusal(429))
        goal = await env.goals.get(session.id)
        await env.goals.write(
            session.id,
            GoalWrite(expected_revision=goal.revision, action="start"),
        )
        assert await env.runtime.dispatch_running_goal(session.id, "Begin validation")
        await env.settled()

        saved = await env.store.get(ChatSession, session.id)
        assert saved.backend == ChatBackend.HARNESS
        state = _state(saved)
        assert "cooldown" not in state
        assert all(
            event.get("action") != "rate_limit_fallback"
            for event in state.get("events") or []
        )
        goal = await env.goals.get(session.id)
        assert goal.status == ChatGoalStatus.PAUSED
        assert (goal.blocked_reason or "").startswith("Model refusal (category-a)")
        turns = await env.store.list_session_entities(ChatTurn, session.id)
        assert len(turns) == 1
        # No provider turn ran in its place.
        assert [
            request
            for request in env.provider.requests
            if request.metadata.get("operation") != "conversation_naming"
        ] == []
        assert len(env.adapter.prompts) == 1
    finally:
        await env.close()


async def test_provider_refusal_pauses_goal_without_retry(tmp_path):
    env = await _env(tmp_path)
    try:
        session = await _provider_goal(env, subagents=False, mcp=False, step_budget=5)
        calls: list[str] = []
        original = env.provider.complete

        async def refuse(request):
            if request.metadata.get("operation") == "conversation_naming":
                return await original(request)
            calls.append("main")
            raise _anthropic_refusal(
                {"stop_details": {"category": "category-b", "explanation": "x"}}
            )

        env.provider.complete = refuse  # type: ignore[method-assign]
        goal = await env.goals.get(session.id)
        await env.goals.write(
            session.id, GoalWrite(expected_revision=goal.revision, action="start")
        )
        await env.chat.dispatch_running_goal(session.id, "Begin")
        for _ in range(500):
            if (await env.goals.get(session.id)).status != ChatGoalStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        # Leave time for any (wrongly) armed retry or continuation to start.
        await asyncio.sleep(0.2)

        [turn] = await env.store.list_session_entities(ChatTurn, session.id)
        goal = await env.goals.get(session.id)
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.blocked_reason == (
            f"Model refusal (category-b) on model-a in turn {turn.id}: "
            "needs operator review"
        )
        assert GOAL_PROVIDER_RETRY not in goal.metadata
        state = goal.metadata[GOAL_METADATA_KEY]
        assert state["count"] == 1
        assert state["last"]["runtime"] == "provider:bulk"
        assert state["last"]["category"] == "category-b"
        assert len(await _project_events(env.store, "project", EVENT_TYPE)) == 1
        assert len(await _refusal_requests(env.store, session.id)) == 1
        assert classify_reason(goal)[0] == "never"
        assert calls == ["main"]
    finally:
        await env.close()
