import asyncio
from pathlib import Path

import pytest

from nebula.v3.chat import ChatError, ChatService, PreparedChat
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
    ToolCall,
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
) -> ModelResponse:
    return ModelResponse(
        provider_id="provider",
        model="model-a",
        text=text,
        reasoning=reasoning,
        tool_calls=calls or [],
        usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        finish_reason="tool_calls" if calls else "stop",
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
            _response(
                text="I will run it.",
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ],
            ),
            "routing prose",
        ),
        (
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"}),
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "b"}),
                ]
            ),
            "refusing duplicate execution",
        ),
        (
            _response(calls=[ToolCall(id="call-1", name="other_tool", arguments={})]),
            "unavailable tool",
        ),
    ],
)
def test_malformed_routing_never_reaches_the_tool_broker(tmp_path, routing, detail):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [routing], broker)

    with pytest.raises(ChatError, match=detail):
        asyncio.run(service.complete(prepared))

    assert broker.calls == []
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert failed.execution_claim_id is None


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
    store, service, prepared, _ = _prepared(
        tmp_path,
        [_response(calls=[repeated]), _response(calls=[repeated])],
        broker,
    )

    with pytest.raises(ChatError, match="refusing duplicate execution"):
        asyncio.run(service.complete(prepared))

    assert len(broker.calls) == 1
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert len(failed.tool_history) == 1
    assert failed.tool_history[0]["model_call_id"] == "call-1"


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
