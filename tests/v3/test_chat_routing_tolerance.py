"""A routing step keeps the turn alive when a model deviates slightly.

Every mature harness (opencode, the AI SDK, Codex, Cline, pi-mono) answers a
deviating tool call with a result the model can act on, and keeps going. These
tests pin the same tolerance for Nebula's required-tool routing step, without
ever running a call Core could not validate.
"""

import asyncio

import httpx
import pytest

import nebula.v3.chat as chat_module
from nebula.v3.domain import ChatRole, ChatTurn, ChatTurnStatus, RiskClass
from nebula.v3.providers import ProviderError, ToolCall, ToolChoice, _safe_error
from nebula.v3.tools import ToolSpec
from tests.v3.test_chat_tool_loop import (
    RecordingBroker,
    ScriptedProvider,
    _prepared,
    _response,
)


def _call(call_id: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _finish(call_id: str = "finish-1", **arguments) -> ToolCall:
    return ToolCall(id=call_id, name="finish_response", arguments=arguments)


def _turn_requests(provider: ScriptedProvider):
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


def _stream(service, prepared):
    async def scenario():
        return [event async for event in service.stream(prepared)]

    return asyncio.run(scenario())


def _probe_spec() -> ToolSpec:
    return ToolSpec(
        name="artifact_probe",
        description="Read a stored result.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
    )


# ROUTE-1: prose in a routing response is commentary, never a failure.


def test_routing_prose_beside_tool_calls_runs_the_calls(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            text="I'll read the value first.",
            reasoning="The read is bounded.",
            calls=[_call("call-1", "safe_read", value="a")],
        ),
        _response(calls=[_finish()]),
        _response(text="Value is a."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    events = _stream(service, prepared)

    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        ("safe_read", {"value": "a"})
    ]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    # The answer is the synthesis; the prose narrated the call as thinking.
    answer = "".join(payload["delta"] for name, payload in events if name == "delta")
    assert answer == "Value is a."
    thinking = "".join(
        payload["delta"] for name, payload in events if name == "reasoning_delta"
    )
    assert thinking == "The read is bounded.\n\nI'll read the value first."
    # What the transcript shows live is what a reload reads back.
    assert turn.reasoning == thinking
    stored = [
        item
        for item in service.session_messages("session")
        if item.role == ChatRole.ASSISTANT
    ]
    assert stored[-1].content == "Value is a."
    assert stored[-1].reasoning == thinking


def test_routing_prose_beside_finish_response_finishes_through_synthesis(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(text="Hello! How can I help today?", calls=[_finish()]),
        _response(text="Hello! How can I help today?"),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Hello! How can I help today?"
    assert broker.calls == []
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_routing_prose_without_a_call_is_the_answer(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_call("call-1", "safe_read", value="a")]),
        # The route ignored the required tool choice and answered in prose.
        _response(text="The value is a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    events = _stream(service, prepared)

    answer = "".join(payload["delta"] for name, payload in events if name == "delta")
    assert answer == "The value is a."
    # The prose was the answer, not commentary on a call, so it is shown once,
    # as the answer.
    assert not [name for name, _ in events if name == "reasoning_delta"]
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["model_call_id"] for entry in turn.tool_history] == ["call-1"]
    # Both requests were routing steps; no synthesis request followed. The
    # synthesis also declares tools, with calling off, so the choice tells
    # them apart.
    assert all(
        request.tool_choice != ToolChoice.NONE for request in _turn_requests(provider)
    )
    assert len(_turn_requests(provider)) == 2


def test_unreadable_routing_control_frame_finishes_through_synthesis(tmp_path):
    broker = RecordingBroker()
    frame = (
        '<｜DSML｜ calls> <｜DSML｜ invoke name="tool_output_read">'
        "the artifact</｜DSML｜ invoke> </｜DSML｜ calls>"
    )
    responses = [_response(text=frame), _response(text="Nothing to read yet.")]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    events = _stream(service, prepared)

    shown = "".join(
        payload["delta"]
        for name, payload in events
        if name in {"delta", "reasoning_delta"}
    )
    assert shown == "Nothing to read yet."
    assert broker.calls == []
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


# ROUTE-2: the finish tool is a control signal; its arguments carry nothing.


@pytest.mark.parametrize(
    "arguments",
    [{"response": "All done."}, {"_": ""}, {"reason": "no tool needed"}],
)
def test_finish_response_arguments_are_ignored(tmp_path, monkeypatch, arguments):
    recorded = []
    original = chat_module.record_diagnostic

    def capture(level, feature, event_code, message, **fields):
        recorded.append((level, event_code, fields))
        return original(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", capture)
    broker = RecordingBroker()
    responses = [
        _response(calls=[_finish(**arguments)]),
        _response(text="Done."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Done."
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.tool_history == []
    [(level, _, fields)] = [
        item
        for item in recorded
        if item[1] == "chat.routing.finish_response_arguments_ignored"
    ]
    assert level == "warning"
    # Only the argument names are kept; a value may be the model's answer.
    assert fields["metadata"]["argument_keys"] == sorted(arguments)
    assert "All done." not in repr(fields)
    assert "no tool needed" not in repr(fields)


# ROUTE-5: an unavailable tool is answered with an error, never a failed turn.


def test_unknown_tool_is_answered_with_an_error_result(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[
                _call("call-1", "safe_read", value="a"),
                _call("call-2", "web_fetch", url="https://example.invalid"),
            ]
        ),
        _response(calls=[_finish()]),
        _response(text="Value is a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    events = _stream(service, prepared)

    # The valid call in the same batch still ran; the unknown one never
    # reached the broker.
    assert [call.tool_name for call in broker.calls] == ["safe_read"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["complete", "failed"]
    refused = turn.tool_history[1]
    assert refused["name"] == "web_fetch"
    assert refused["model_call_id"] == "call-2"
    # Refusing a call spends none of the turn's budget.
    assert turn.execution_tool_calls == 1
    replayed = _turn_requests(provider)[1].tool_results
    assert [(item.call_id, item.is_error) for item in replayed] == [
        ("call-1", False),
        ("call-2", True),
    ]
    assert "'web_fetch' is not available" in str(replayed[1].output)
    assert "safe_read" in str(replayed[1].output)
    assert "finish_response" in str(replayed[1].output)
    completed = [payload for name, payload in events if name == "tool_completed"]
    assert [item["status"] for item in completed] == ["complete", "failed"]


def test_spent_budget_tool_call_finishes_instead_of_failing(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_call("call-1", "safe_read", value="a")]),
        # The budget is spent, so safe_read is no longer offered, but the
        # model saw it in its history and calls it again.
        _response(calls=[_call("call-2", "safe_read", value="b")]),
        _response(calls=[_finish()]),
        _response(text="Read a."),
    ]
    store, service, prepared, provider = _prepared(
        tmp_path, responses, broker, max_tool_calls=1, extra_specs=[_probe_spec()]
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a."
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["complete", "failed"]
    assert turn.execution_tool_calls == 1
    replayed = _turn_requests(provider)[2].tool_results[1]
    assert replayed.is_error is True
    assert "budget" in str(replayed.output)
    assert "finish_response" in str(replayed.output)


def test_repeated_deviations_finish_through_synthesis(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_call("call-1", "bash", command="id")]),
        _response(calls=[_call("call-2", "bash", command="id")]),
        _response(calls=[_call("call-3", "bash", command="id")]),
        _response(text="I could not run a shell here."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    # Three consecutive refused routing responses end the loop instead of
    # routing forever; the synthesis answers from what the turn has.
    assert completion.message.content == "I could not run a shell here."
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["failed"] * 3
    assert turn.execution_tool_calls == 0
    requests = _turn_requests(provider)
    assert len(requests) == 4
    assert requests[-1].tool_choice == ToolChoice.NONE


# ROUTE-9: a call cut off by the output limit is never executed.


def test_length_truncated_routing_calls_are_not_executed(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[_call("call-1", "safe_read", value="partial-valu")],
            finish_reason="length",
        ),
        _response(calls=[_call("call-2", "safe_read", value="partial-value")]),
        _response(calls=[_finish()]),
        _response(text="Read partial-value."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read partial-value."
    assert [call.arguments["value"] for call in broker.calls] == ["partial-value"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["failed", "complete"]
    assert turn.execution_tool_calls == 1
    cut = _turn_requests(provider)[1].tool_results[0]
    assert cut.is_error is True
    assert "cut off" in str(cut.output)
    assert "Re-issue" in str(cut.output)


def test_finish_queued_behind_a_cut_off_call_routes_again(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[_call("call-1", "safe_read", value="a"), _finish()],
            finish_reason="max_tokens",
        ),
        _response(calls=[_call("call-2", "safe_read", value="a")]),
        _response(calls=[_finish("finish-2")]),
        _response(text="Read a."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    # The model gets to re-issue the call it wanted before the turn finishes.
    assert completion.message.content == "Read a."
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert [entry["status"] for entry in turn.tool_history] == ["failed", "complete"]


# ROUTE-10: a reused provider call id never fails the turn or repeats an effect.


def test_reused_provider_id_for_a_new_call_runs_under_a_core_id(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_call("call_0", "safe_read", value="a")]),
        # A route that numbers calls per response reuses the id.
        _response(calls=[_call("call_0", "safe_read", value="b")]),
        _response(calls=[_finish()]),
        _response(text="a and b"),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "a and b"
    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    ids = [entry["model_call_id"] for entry in turn.tool_history]
    assert ids[0] == "call_0"
    assert ids[1] != "call_0"
    assert turn.tool_history[1]["issued_call_id"] == "call_0"
    assert broker.calls[1].provider_call_id == ids[1]
    replayed = [item.call_id for item in _turn_requests(provider)[2].tool_results]
    assert replayed == ids
    assert len(set(replayed)) == 2


def test_repeated_id_inside_one_batch_runs_both_distinct_calls(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[
                _call("0", "safe_read", value="a"),
                _call("0", "safe_read", value="b"),
            ]
        ),
        _response(calls=[_finish()]),
        _response(text="a and b"),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    asyncio.run(service.complete(prepared))

    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    ids = [entry["model_call_id"] for entry in turn.tool_history]
    assert ids[0] == "0" and len(set(ids)) == 2


def test_identical_repeat_of_a_reissued_call_is_not_run_again(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(calls=[_call("call_0", "safe_read", value="a")]),
        _response(calls=[_call("call_0", "safe_read", value="b")]),
        _response(calls=[_call("call_0", "safe_read", value="b")]),
        _response(calls=[_finish()]),
        _response(text="a and b"),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    asyncio.run(service.complete(prepared))

    # The third call replays the second under the same provider id: it was
    # answered from history, not run a second time.
    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == [
        "complete",
        "complete",
        "failed",
    ]
    assert turn.execution_tool_calls == 2
    ids = [entry["model_call_id"] for entry in turn.tool_history]
    assert len(set(ids)) == 3
    assert "already ran" in str(_turn_requests(provider)[3].tool_results[2].output)


# ROUTE-15: a route that rejects tool_choice=required still routes.


class _RequiredChoiceRejectingProvider(ScriptedProvider):
    def __init__(self, responses, detail: str) -> None:
        super().__init__(responses)
        self.detail = detail

    async def complete(self, request):
        if request.tool_choice == ToolChoice.REQUIRED:
            self.requests.append(request)
            raise _safe_error(
                httpx.Response(
                    400,
                    json={
                        "object": "error",
                        "message": self.detail,
                        "type": "BadRequestError",
                        "code": 400,
                    },
                )
            )
        return await super().complete(request)


def test_routing_retries_with_auto_when_required_is_rejected(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    provider = _RequiredChoiceRejectingProvider(
        [
            _response(calls=[_call("call-1", "safe_read", value="a")]),
            _response(calls=[_finish()]),
            _response(text="Read a."),
        ],
        'tool_choice="required" is not supported!',
    )
    prepared.provider = provider

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a."
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice != ToolChoice.NONE
    ]
    # One rejected attempt, then the rest of the turn routes with auto.
    assert [request.tool_choice for request in routing] == [
        ToolChoice.REQUIRED,
        ToolChoice.AUTO,
        ToolChoice.AUTO,
    ]


def test_an_unrelated_routing_rejection_still_fails_the_turn(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    provider = _RequiredChoiceRejectingProvider([], "model is not available")
    prepared.provider = provider

    with pytest.raises(ProviderError, match="HTTP 400"):
        asyncio.run(service.complete(prepared))

    assert len(_turn_requests(provider)) == 1
    assert broker.calls == []
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.FAILED
