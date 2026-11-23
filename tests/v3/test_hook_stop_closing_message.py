"""A turn a project hook stopped ends on Core's closing message.

Overnight goals stalled most often here: the same hook refused three calls,
routing stopped, and the tool-free synthesis came back empty. Final-answer
recovery asked again with a prompt that again said tools were unavailable,
until the stall limit blocked the goal. A hook stop with nothing to say now
completes at once with a message naming the hook and what it refused, so the
goal continues with its next turn. Empty replies also record the HTTP status
and upstream route they came from.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json

import httpx
import pytest

from nebula.v3.chat import (
    _final_answer_response_record,
    _hook_stop_closing_answer,
)
from nebula.v3.domain import ChatGoal, ChatGoalStatus, ChatTurn, ChatTurnStatus
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderFlavor,
    ProviderResponseError,
    StreamEventType,
    ToolChoice,
    config_from_catalog,
)
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_hook_denial_routing import (
    SEARCH_HOOK,
    HookBroker,
    _finish,
    _read,
    _turn_requests,
)


def _three_denials() -> list[ModelResponse]:
    return [
        _response(calls=[_read("call-1", f"deny:{SEARCH_HOOK}:1")]),
        _response(calls=[_read("call-2", "a")]),
        _response(calls=[_read("call-3", f"deny:{SEARCH_HOOK}:2")]),
        _response(calls=[_read("call-4", f"deny:{SEARCH_HOOK}:3")]),
    ]


def _routed_upstream(response: ModelResponse) -> ModelResponse:
    return response.model_copy(
        update={"http_status": 200, "upstream_provider": "DeepInfra"}
    )


async def _goal_turn(store, prepared) -> ChatGoal:
    goal = await store.create(
        ChatGoal(
            id="goal",
            engagement_id="project",
            session_id="session",
            objective="Search the repository",
            completion_criteria=["The search is reported"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    prepared.turn = await store.update(
        ChatTurn,
        "turn",
        {"goal_id": goal.id},
        expected_revision=prepared.turn.revision,
    )
    return goal


async def test_hook_stop_with_an_empty_answer_completes_and_the_goal_continues(
    tmp_path,
):
    empty = _routed_upstream(_response())
    store, service, prepared, provider = await _prepared(
        tmp_path, [*_three_denials(), empty], HookBroker(), max_tool_calls=20
    )
    goal = await _goal_turn(store, prepared)
    continued: list[tuple[ChatTurnStatus, ChatGoalStatus]] = []

    async def continue_goal(settled):
        # The settled turn and goal as the continuation reads them.
        continued.append(
            (
                (await store.get(ChatTurn, settled.turn.id)).status,
                (await store.get(ChatGoal, goal.id)).status,
            )
        )
        return None

    service._continue_running_goal_after_turn = continue_goal
    turn_id = await service.start_provider_turn(prepared)
    runtime = service._active_provider_turns[turn_id]
    events = [name async for name, _ in service.follow_provider_turn(turn_id)]
    # Followers are told before the goal continues; the run ends after it.
    await runtime.task
    await service.shutdown()

    assert events[-1] == "done"
    turn = await store.get(ChatTurn, turn_id)
    assert turn.status == ChatTurnStatus.COMPLETE
    final = turn.request_snapshot["final_answer_recovery"]
    assert final["attempts"] == 1
    assert final["responses"][0]["reason"] == "missing_answer"
    assert final["responses"][0]["http_status"] == 200
    assert final["responses"][0]["upstream_provider"] == "DeepInfra"
    # One synthesis, no re-ask: four routing steps and the empty answer.
    requests = _turn_requests(provider)
    assert len(requests) == 5
    assert requests[-1].tool_choice == ToolChoice.NONE
    assert "final_answer_recovery" not in requests[-1].metadata
    assert (await store.get(ChatGoal, goal.id)).status == ChatGoalStatus.RUNNING
    assert continued == [(ChatTurnStatus.COMPLETE, ChatGoalStatus.RUNNING)]


async def test_hook_stop_closing_message_names_the_hook_and_what_it_refused(
    tmp_path,
):
    store, service, prepared, provider = await _prepared(
        tmp_path,
        [*_three_denials(), _response(reasoning="Blocked again; nothing to add.")],
        HookBroker(),
        max_tool_calls=20,
    )

    completion = await service.complete(prepared)

    assert completion.message.content == (
        f"Stopped: project hook `{SEARCH_HOOK}` refused 3 calls in this turn "
        "(safe_read ×3), and the model wrote no answer. The next turn can try "
        "a different approach."
    )
    assert (await store.get(ChatTurn, "turn")).status == ChatTurnStatus.COMPLETE
    assert len(_turn_requests(provider)) == 5


async def test_hook_stop_keeps_a_real_answer(tmp_path):
    store, service, prepared, provider = await _prepared(
        tmp_path,
        [*_three_denials(), _response(text="The hook routes searches elsewhere.")],
        HookBroker(),
        max_tool_calls=20,
    )

    completion = await service.complete(prepared)

    assert completion.message.content == "The hook routes searches elsewhere."
    turn = await store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert "final_answer_recovery" not in turn.request_snapshot


async def test_an_empty_answer_without_a_hook_stop_still_retries(tmp_path):
    store, service, prepared, provider = await _prepared(
        tmp_path,
        [
            _response(calls=[_read("call-1", "a")]),
            _response(calls=[_finish()]),
            _response(),
            _response(),
        ],
        RecordingBroker(),
    )

    with pytest.raises(ProviderResponseError, match="no operator-facing answer"):
        await service.complete(prepared)

    requests = _turn_requests(provider)
    # The first synthesis and its one automatic re-ask.
    assert len(requests) == 4
    assert requests[-1].metadata["final_answer_recovery"] == "missing_answer"
    turn = await store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.request_snapshot["final_answer_recovery"]["attempts"] == 2


def test_closing_message_counts_each_refused_tool():
    response = _response()
    closing = _hook_stop_closing_answer(
        response, "guard", ["run_command", "workspace.write", "run_command"]
    )
    assert closing.text == (
        "Stopped: project hook `guard` refused 3 calls in this turn "
        "(run_command ×2, workspace.write), and the model wrote no answer. "
        "The next turn can try a different approach."
    )
    assert closing.tool_calls == []


def test_response_record_carries_status_and_route_and_tolerates_none():
    record = _final_answer_response_record(
        _routed_upstream(_response()), "missing_answer"
    )
    assert record["http_status"] == 200
    assert record["upstream_provider"] == "DeepInfra"
    plain = _final_answer_response_record(_response(), "missing_answer")
    assert plain["http_status"] is None
    assert plain["upstream_provider"] is None


def _openrouter(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        config_from_catalog(
            provider_id="openrouter",
            flavor=ProviderFlavor.OPENROUTER,
            default_model="deepseek/deepseek-v4.1-flash",
            capabilities=ModelCapabilities(streaming=True),
        ),
        transport=httpx.MockTransport(handler),
    )


def _request() -> ModelRequest:
    return ModelRequest(messages=[ModelMessage(role="user", content="hello")])


async def test_openrouter_stream_and_completion_report_status_and_upstream(
    monkeypatch,
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    frame = {
        "id": "gen-1",
        "model": "deepseek/deepseek-v4.1-flash",
        "provider": "DeepInfra",
    }
    body = "\n\n".join(
        [
            "data: "
            + json.dumps(
                {**frame, "choices": [{"delta": {}, "finish_reason": "stop"}]}
            ),
            "data: [DONE]",
        ]
    )

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    events = [event async for event in _openrouter(streamed).stream(_request())]
    assert events[-1].type == StreamEventType.COMPLETED, events[-1].error
    assert events[-1].response.http_status == 200
    assert events[-1].response.upstream_provider == "DeepInfra"

    def completed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **frame,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    response = await _openrouter(completed).complete(_request())
    assert response.http_status == 200
    assert response.upstream_provider == "DeepInfra"


pytestmark = pytest.mark.anyio
