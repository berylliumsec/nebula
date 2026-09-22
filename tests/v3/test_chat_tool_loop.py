import asyncio
import base64
import json
from pathlib import Path

import pytest

from nebula.v3 import diagnostic_sensitive, diagnostics

from nebula.v3.chat import (
    _CHAT_BASE_INSTRUCTIONS,
    ChatError,
    ChatService,
    PreparedChat,
)
from nebula.v3.domain import (
    Approval,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.policy import PolicyDecision, PolicyEffect
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ProviderResponseError,
    ToolCall,
    ToolChoice,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    ApprovalRequired,
    PolicyDenied,
    ToolExecutionResult,
    ToolSpec,
)


class ScriptedProvider(ModelProvider):
    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(
            ProviderConfig(
                id="provider",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(
                    streaming=True,
                    tools=True,
                    strict_tools=True,
                ),
            )
        )
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.metadata.get("operation") == "conversation_naming":
            return _response(text="Tool result")
        if not self.responses:
            raise AssertionError("provider script was exhausted")
        return self.responses.pop(0)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


class RecordingBroker:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        if self.deny:
            raise PolicyDenied(
                PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason="project policy denied this operation",
                    rule="test-deny",
                )
            )
        return ToolExecutionResult(output={"value": invocation.arguments["value"]})


def _response(
    *,
    calls: list[ToolCall] | None = None,
    text: str = "",
    reasoning: str = "",
    finish_reason: str | None = None,
) -> ModelResponse:
    return ModelResponse(
        provider_id="provider",
        model="model-a",
        text=text,
        reasoning=reasoning,
        tool_calls=calls or [],
        usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
    )


def _prepared(
    tmp_path: Path,
    responses: list[ModelResponse],
    broker: RecordingBroker,
    *,
    max_tool_calls: int = 5,
    extra_specs: list[ToolSpec] | None = None,
):
    store = NebulaStore(tmp_path / "tool-loop.db")
    project = store.create(Engagement(id="project", name="Tool loop"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
        )
    )
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Tool loop",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={
                "message_count": 1,
                "last_sequence": 1,
                "initial_title_state": "generated",
            },
        )
    )
    user = store.create(
        ChatMessage(
            id="user-message",
            engagement_id=project.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Use the safe tool once.",
        )
    )
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            max_tool_calls=max_tool_calls,
        )
    )
    spec = ToolSpec(
        name="safe_read",
        description="Return one bounded value.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    specs = {item.name: item for item in [spec, *(extra_specs or [])]}
    provider = ScriptedProvider(responses)
    prepared = PreparedChat(
        provider=provider,
        provider_profile=profile,
        model_request=ModelRequest(
            model="model-a",
            messages=[ModelMessage(role="user", content=user.content)],
        ),
        resolved_model="model-a",
        citations=[],
        engagement_id=project.id,
        session=session,
        pending_session=None,
        stored_messages=[user],
        new_messages=[],
        tools_enabled=True,
        tool_components=RuntimeToolComponents(
            broker=broker,
            scope=ScopePolicy(engagement_id=project.id),
            workspace=tmp_path,
            specs=specs,
            runtime_digest="test-runtime",
        ),
        turn=turn,
        inputs_persisted=True,
    )
    return store, ChatService(store, worker_id="worker"), prepared, provider


@pytest.mark.parametrize(
    "routing,detail",
    [
        (
            _response(calls=[ToolCall(id="call-1", name="other_tool", arguments={})]),
            "'other_tool' is not available",
        ),
        (
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ],
                finish_reason="length",
            ),
            "cut off",
        ),
    ],
)
def test_malformed_routing_never_reaches_the_tool_broker(tmp_path, routing, detail):
    broker = RecordingBroker()
    responses = [
        routing,
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="Nothing ran."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    # Core answers the call itself, and the model routes again.
    assert completion.message.content == "Nothing ran."
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.execution_claim_id is None
    assert turn.execution_tool_calls == 0
    [refused] = provider.requests[1].tool_results
    assert refused.is_error is True
    assert detail in str(refused.output)
    assert refused.output["schema"] == "nebula.tool-failure/v1"
    if routing.tool_calls[0].name == "safe_read":
        assert (
            refused.output["effective_input_schema"]
            == prepared.tool_components.specs["safe_read"].input_schema
        )
    else:
        assert refused.output["effective_input_schema"] is None


def test_routing_prose_captures_exact_provider_response_only_in_protected_detail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diagnostic_sensitive.keyring, "get_keyring", lambda: None)
    diagnostic_dir = tmp_path / "diagnostics"
    diagnostic_dir.mkdir()
    (diagnostic_dir / "diagnostics-settings.json").write_text(
        json.dumps(
            {
                "schema": diagnostics.SETTINGS_SCHEMA,
                "global_level": "error",
                "feature_levels": {"chat": "warning"},
                "sensitive_detail_capture": True,
            }
        ),
        encoding="utf-8",
    )
    manager = diagnostics.DiagnosticManager(diagnostic_dir, watch_settings=False)
    monkeypatch.setattr(diagnostics, "_manager", manager)
    raw = {
        "id": "gen-routing-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "private routing text",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "safe_read", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
    }
    raw_body = b'{ "id" : "gen-routing-1", "choices":[] }'
    routing = _response(
        text="private routing text",
        calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})],
    ).model_copy(
        update={
            "raw": raw,
            "raw_body": raw_body,
            "provider_request_id": "gen-routing-1",
        }
    )
    broker = RecordingBroker()
    responses = [
        routing,
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="The safe tool returned a."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)
    try:
        # Prose beside a call is commentary: the call runs and the turn
        # completes, while the exact response stays in protected detail.
        completion = asyncio.run(service.complete(prepared))
        assert completion.message.content == "The safe tool returned a."
        assert manager.flush()
        records = [
            json.loads(line)
            for line in (manager.log_dir / "chat.log").read_text().splitlines()
        ]
        record = next(
            item
            for item in records
            if item["event_code"] == "chat.routing.prose_with_required_tool"
        )
        assert record["level"] == "WARNING"
        assert record["outcome"] == "fallback"
        assert record["sensitive_detail_available"] is True
        assert record["metadata"]["status"] == "text_with_tool_calls"
        assert "private routing text" not in json.dumps(records)
        detail = json.loads(
            manager.reveal_sensitive_detail(
                record["error_id"], operator_id="operator", action="reveal"
            )
        )
        assert detail["provider_response"] == raw
        assert base64.b64decode(detail["provider_response_body_base64"]) == raw_body
        assert detail["normalized"]["text"] == "private routing text"
        assert [call.arguments for call in broker.calls] == [{"value": "a"}]
        assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    finally:
        manager.close()


def test_tool_turn_prompts_never_claim_the_turn_has_no_tools(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="The safe tool returned a."),
    ]
    _, service, prepared, provider = _prepared(tmp_path, responses, broker)
    prepared.model_request = prepared.model_request.model_copy(
        update={"instructions": _CHAT_BASE_INSTRUCTIONS}
    )

    asyncio.run(service.complete(prepared))

    turn_requests = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert len(turn_requests) == 3
    for request in turn_requests:
        instructions = request.instructions or ""
        assert _CHAT_BASE_INSTRUCTIONS in instructions
        assert "No tools are available" not in instructions
    # The last request is final synthesis: calling is off, but tools did run,
    # so it must not tell the operator the turn had none.
    assert turn_requests[-1].tool_choice == ToolChoice.NONE


def test_denied_tool_is_returned_as_error_context_without_reexecution(tmp_path):
    broker = RecordingBroker(deny=True)
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="The operation was denied by project policy."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The operation was denied by project policy."
    assert len(broker.calls) == 1
    completed = store.get(ChatTurn, "turn")
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.tool_history[0]["status"] == "denied"
    assert provider.requests[1].tool_results[0].is_error is True


def test_repeated_provider_call_id_cannot_duplicate_a_tool_effect(tmp_path):
    broker = RecordingBroker()
    repeated = ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[repeated]),
            _response(calls=[repeated]),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="Read a."),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    # The replayed call is answered from history instead of running again.
    assert completion.message.content == "Read a."
    assert len(broker.calls) == 1
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["complete", "failed"]
    assert turn.tool_history[0]["model_call_id"] == "call-1"
    assert turn.tool_history[1]["model_call_id"] != "call-1"
    assert turn.execution_tool_calls == 1
    replayed = provider.requests[2].tool_results[1]
    assert replayed.is_error is True
    assert "already ran" in str(replayed.output)


def test_goal_exhaustion_after_routing_stops_before_tool_effect(tmp_path):
    broker = RecordingBroker()
    routing = _response(
        calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
    ).model_copy(
        update={
            "usage": ModelUsage(input_tokens=999, output_tokens=1, total_tokens=1_000)
        }
    )
    store, service, prepared, _ = _prepared(tmp_path, [routing], broker)
    goal = store.create(
        ChatGoal(
            id="goal",
            engagement_id="project",
            session_id="session",
            objective="Stop at the token boundary",
            completion_criteria=["No tool runs after exhaustion"],
            status=ChatGoalStatus.RUNNING,
            token_budget=1_000,
        )
    )
    prepared.turn = store.update(
        ChatTurn,
        "turn",
        {"goal_id": goal.id},
        expected_revision=prepared.turn.revision,
    )

    with pytest.raises(ChatError, match="before tool execution"):
        asyncio.run(service.complete(prepared))

    assert broker.calls == []
    assert store.get(ChatGoal, goal.id).status == ChatGoalStatus.PAUSED
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.FAILED


def test_cancelling_an_inflight_tool_turn_stops_the_owned_worker_once(tmp_path):
    class BlockingBroker(RecordingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def execute(self, invocation, scope, *, approval=None):
            del scope, approval
            self.calls.append(invocation)
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled tool execution resumed unexpectedly")

    async def scenario():
        broker = BlockingBroker()
        routing = _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        )
        store, service, prepared, _ = _prepared(tmp_path, [routing], broker)
        service.start_provider_turn(prepared)
        await asyncio.wait_for(broker.started.wait(), 2)

        stopped = await service.stop_provider_turn("turn")

        assert stopped.status == ChatTurnStatus.CANCELLED
        assert stopped.execution_claim_id is None
        assert len(broker.calls) == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_batched_calls_all_run_one_step_at_a_time(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[
                ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
            ]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="Read a and b."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    asyncio.run(service.complete(prepared))

    # One routing response, both calls executed, in the requested order.
    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["step"] for entry in turn.tool_history] == [0, 1]
    assert [entry["model_call_id"] for entry in turn.tool_history] == [
        "call-1",
        "call-2",
    ]
    assert turn.execution_tool_calls == 2
    assert len(turn.tool_call_ids) == len(set(turn.tool_call_ids)) == 2
    # The batch costs one routing round trip, and both results are replayed.
    assert provider.requests[0].parallel_tool_calls is True
    assert [item.call_id for item in provider.requests[1].tool_results] == [
        "call-1",
        "call-2",
    ]


def test_batch_queued_behind_finish_response_never_runs(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[
                ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                ToolCall(id="finish-1", name="finish_response", arguments={}),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
            ]
        ),
        _response(text="Read a."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    asyncio.run(service.complete(prepared))

    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["model_call_id"] for entry in turn.tool_history] == ["call-1"]


def test_batch_beyond_the_execution_budget_routes_again_instead_of_overspending(
    tmp_path,
):
    broker = RecordingBroker()
    query_spec = ToolSpec(
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
    responses = [
        _response(
            calls=[
                ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
                ToolCall(id="call-3", name="artifact_probe", arguments={"value": "c"}),
            ]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="Read a."),
    ]
    store, service, prepared, provider = _prepared(
        tmp_path, responses, broker, max_tool_calls=1, extra_specs=[query_spec]
    )

    asyncio.run(service.complete(prepared))

    # The second execution call exhausts the turn budget, so the rest of the
    # batch is dropped rather than spent, and routing runs again.
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["model_call_id"] for entry in turn.tool_history] == ["call-1"]
    assert [tool.name for tool in provider.requests[1].tools] == [
        "artifact_probe",
        "finish_response",
    ]


def test_routing_response_without_a_tool_call_answers_from_existing_results(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        # A required tool choice the provider ignored: no call and no prose.
        _response(),
        _response(text="Read a."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a."
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["model_call_id"] for entry in turn.tool_history] == ["call-1"]


def test_approval_inside_a_batch_pauses_and_drops_the_queued_calls(tmp_path):
    class ApprovingBroker(RecordingBroker):
        def __init__(self) -> None:
            super().__init__()
            self.approval: Approval | None = None

        async def execute(self, invocation, scope, *, approval=None):
            del scope
            self.calls.append(invocation)
            if invocation.arguments["value"] == "b" and approval is None:
                assert self.approval is not None
                raise ApprovalRequired(self.approval)
            return ToolExecutionResult(output={"value": invocation.arguments["value"]})

    broker = ApprovingBroker()
    responses = [
        _response(
            calls=[
                ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
                ToolCall(id="call-3", name="safe_read", arguments={"value": "c"}),
            ]
        )
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)
    broker.approval = store.create(
        Approval(
            id="approval-1",
            engagement_id="project",
            run_id="turn",
            risk_class=RiskClass.LOCAL_READ,
            exact_request={"tool_name": "safe_read", "arguments": {"value": "b"}},
            policy_rationale="the operator approves this read",
            requested_by="chat-assistant",
        )
    )

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    assert [name for name, _ in events][-1] == "approval_required"
    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    paused = store.get(ChatTurn, "turn")
    assert paused.status == ChatTurnStatus.WAITING_APPROVAL
    assert paused.approval_id == "approval-1"
    # The checkpoint the resume path reads is the last entry, and the call the
    # model queued behind it was dropped rather than run past the approval.
    assert [entry["status"] for entry in paused.tool_history] == [
        "complete",
        "waiting_approval",
    ]
    assert [entry["model_call_id"] for entry in paused.tool_history] == [
        "call-1",
        "call-2",
    ]


def test_routing_thoughts_reach_the_transcript(tmp_path):
    """A reasoning model thinks before each tool call, not only as it answers."""

    broker = RecordingBroker()
    responses = [
        _response(
            reasoning="The read is bounded, so run it first.",
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})],
        ),
        _response(
            reasoning="The value came back; nothing else is needed.",
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})],
        ),
        _response(text="Tool result", reasoning="Report the value plainly."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    episode = (
        "The read is bounded, so run it first."
        "\n\nThe value came back; nothing else is needed."
        "\n\nReport the value plainly."
    )
    # What the transcript shows live is what a reload reads back.
    streamed = "".join(
        payload["delta"] for name, payload in events if name == "reasoning_delta"
    )
    assert streamed == episode
    stored = [
        item
        for item in service.session_messages("session")
        if item.role == ChatRole.ASSISTANT
    ]
    assert stored[-1].reasoning == episode
    assert stored[-1].content == "Tool result"
    assert store.get(ChatTurn, "turn").reasoning == episode


def test_a_wordless_routing_step_adds_no_thinking(tmp_path):
    """Models that only think sometimes leave no blank gap in the episode."""

    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            reasoning="   ",
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})],
        ),
        _response(text="Tool result", reasoning="Only the answer needed thought."),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    streamed = "".join(
        payload["delta"] for name, payload in events if name == "reasoning_delta"
    )
    assert streamed == "Only the answer needed thought."
    assert store.get(ChatTurn, "turn").reasoning == "Only the answer needed thought."


def test_serialized_dsml_calls_run_like_any_other_tool_call(tmp_path):
    """A route that puts calls in ``content`` still gets its tools run."""

    broker = RecordingBroker()
    frame = (
        "<｜DSML｜ calls>\n"
        '  <｜DSML｜ invoke name="safe_read">\n'
        '    <｜DSML｜ parameter name="value">a</｜DSML｜ parameter>\n'
        "  </｜DSML｜ invoke>\n"
        "</｜DSML｜ calls>"
    )
    responses = [
        _response(text=frame),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="The safe tool returned a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The safe tool returned a."
    # The broker saw an ordinary invocation: the frame never reached it, and
    # the recovered call carried the arguments the model wrote.
    assert [call.tool_name for call in broker.calls] == ["safe_read"]
    assert broker.calls[0].arguments == {"value": "a"}
    completed = store.get(ChatTurn, "turn")
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.tool_history[0]["status"] == "complete"
    # The turn's own record of the step is the recovered call, not the frame.
    assert frame not in str(completed.tool_history)
    assert provider.requests[1].tool_results[0].is_error is False


def test_final_synthesis_reads_serialized_tool_calls_as_tool_calls(tmp_path):
    """A readable DSML frame is a tool call, whichever field carried it."""

    broker = RecordingBroker()
    serialized = (
        '<｜DSML｜ calls> <｜DSML｜ invoke name="tool_output_read">'
        '<｜DSML｜ parameter name="artifact_id">artifact-a</｜DSML｜ parameter>'
        "</｜DSML｜ invoke> </｜DSML｜ calls>"
    )
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text=serialized),
        _response(text="The safe tool returned a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    visible = "".join(payload["delta"] for name, payload in events if name == "delta")
    assert visible == "The safe tool returned a."
    assert serialized not in visible
    turn_requests = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert len(turn_requests) == 4
    # The frame was asking for a tool after the turn finished routing, so the
    # turn recovers the way it does from any late tool call.
    assert turn_requests[-1].metadata["final_answer_recovery"] == "tool_call"
    completed = store.get(ChatTurn, "turn")
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.request_snapshot["final_answer_recovery"] == {
        "attempts": 1,
        "reason": "tool_call",
        "responses": [
            {
                "content_characters": 0,
                "finish_reason": "stop",
                "provider_request_id": None,
                "reason": "tool_call",
                "reasoning_characters": 0,
            }
        ],
    }
    assert completed.usage.input_tokens == 8
    assert completed.usage.output_tokens == 4
    assert completed.usage.total_tokens == 12


def test_final_synthesis_recovers_from_a_provider_control_frame(tmp_path):
    """A frame Core cannot read completely is still refused, not guessed at."""

    broker = RecordingBroker()
    control_frame = (
        '<｜DSML｜ calls> <｜DSML｜ invoke name="tool_output_read">'
        "the artifact I mentioned"
        "</｜DSML｜ invoke> </｜DSML｜ calls>"
    )
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text=control_frame),
        _response(text="The safe tool returned a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    visible = "".join(payload["delta"] for name, payload in events if name == "delta")
    assert visible == "The safe tool returned a."
    assert control_frame not in visible
    turn_requests = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert len(turn_requests) == 4
    assert turn_requests[-1].metadata["final_answer_recovery"] == (
        "provider_control_frame"
    )
    completed = store.get(ChatTurn, "turn")
    assert completed.status == ChatTurnStatus.COMPLETE
    assert completed.request_snapshot["final_answer_recovery"] == {
        "attempts": 1,
        "reason": "provider_control_frame",
        "responses": [
            {
                "content_characters": len(control_frame),
                "finish_reason": "stop",
                "provider_request_id": None,
                "reason": "provider_control_frame",
                "reasoning_characters": 0,
            }
        ],
    }
    assert completed.usage.input_tokens == 8
    assert completed.usage.output_tokens == 4
    assert completed.usage.total_tokens == 12


def test_final_synthesis_retries_reasoning_exhaustion_with_more_room(tmp_path):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(reasoning="Still working it out.", finish_reason="length"),
        _response(text="The safe tool returned a."),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)
    prepared.model_request = prepared.model_request.model_copy(
        update={"max_output_tokens": 2_048}
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The safe tool returned a."
    turn_requests = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert turn_requests[-2].max_output_tokens == 2_048
    assert turn_requests[-1].max_output_tokens == 4_096
    assert turn_requests[-1].metadata["final_answer_recovery"] == "output_limit"
    completed = store.get(ChatTurn, "turn")
    assert completed.request_snapshot["final_answer_recovery"]["reason"] == (
        "output_limit"
    )


def test_final_synthesis_exhaustion_preserves_tools_and_is_retryable_provider_failure(
    tmp_path,
):
    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(reasoning="Still planning.", finish_reason="stop"),
        _response(reasoning="Still planning again.", finish_reason="stop"),
    ]
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)
    prepared.model_request = prepared.model_request.model_copy(
        update={"max_output_tokens": 2_048}
    )

    with pytest.raises(
        ProviderResponseError,
        match=(
            "no operator-facing answer after bounded recovery: the model "
            "returned only reasoning"
        ),
    ):
        asyncio.run(service.complete(prepared))

    assert len(broker.calls) == 1
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert len(failed.tool_history) == 1
    turn_requests = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert [request.max_output_tokens for request in turn_requests[-2:]] == [
        2_048,
        4_096,
    ]
    assert failed.request_snapshot["final_answer_recovery"]["attempts"] == 2
    assert failed.request_snapshot["final_answer_recovery"]["reason"] == (
        "reasoning_only"
    )
    assert failed.request_snapshot["final_answer_recovery"]["last_reason"] == (
        "reasoning_only"
    )


def test_mcp_calls_reach_the_transcript_under_a_readable_name(tmp_path):
    """An MCP tool routes under a digest; a conversation shows server and tool."""

    broker = RecordingBroker()
    mcp_spec = ToolSpec(
        name="mcp.9a4c1f0b77de.create_issue",
        description="File an issue upstream.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.WORKSPACE_WRITE,
        source_id="mcp:tracker",
        display_name="GitHub · create_issue",
    )
    responses = [
        _response(
            calls=[
                ToolCall(
                    id="call-1",
                    name="mcp.9a4c1f0b77de.create_issue",
                    arguments={"value": "a"},
                )
            ]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="Filed."),
    ]
    store, service, prepared, _ = _prepared(
        tmp_path, responses, broker, extra_specs=[mcp_spec]
    )

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    lifecycle = [
        payload
        for name, payload in events
        if name in {"tool_started", "tool_completed"}
    ]
    assert [item["capability"] for item in lifecycle] == [
        "mcp.9a4c1f0b77de.create_issue",
        "mcp.9a4c1f0b77de.create_issue",
    ]
    # The runtime name still routes the call; the readable one rides alongside.
    assert [item["display_name"] for item in lifecycle] == [
        "GitHub · create_issue",
        "GitHub · create_issue",
    ]
    # A reload reads the same name, without re-deriving it from the digest.
    stored = [
        item
        for item in service.session_messages("session")
        if item.role == ChatRole.ASSISTANT
    ]
    [result] = stored[-1].metadata["tool_results"]
    assert result["capability"] == "mcp.9a4c1f0b77de.create_issue"
    assert result["display_name"] == "GitHub · create_issue"
    [step] = store.get(ChatTurn, "turn").tool_history
    assert step["display_name"] == "GitHub · create_issue"


def test_a_fixed_capability_carries_no_display_name(tmp_path):
    """Only tools whose runtime name is unreadable carry a second name."""

    broker = RecordingBroker()
    responses = [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="Tool result"),
    ]
    store, service, prepared, _ = _prepared(tmp_path, responses, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    started = [payload for name, payload in events if name == "tool_started"]
    assert [item["display_name"] for item in started] == [None]
    [step] = store.get(ChatTurn, "turn").tool_history
    assert "display_name" not in step
