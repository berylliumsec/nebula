"""Gaps left after the provider dialect and replay fixes (#499, #502, #504, #505).

- A routing step's commentary that runs into a tool frame Core could not read
  shows only the commentary, wherever the frame starts.
- Context compaction on a provider without structured output gets the memory
  schema in its instructions.
- Context-length recovery works for a turn completed without streaming: a
  brand-new conversation and one whose new message is not stored yet.
- The mission specialist loop replays each routing response as the one
  message that issued it, with that response's reasoning.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nebula.v3.agent_tooling import BrokeredToolSpecialist
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.context import (
    ContextCompactor,
    ContextMemory,
    ContextSource,
    ContextSourceReference,
)
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.orchestration import (
    PlannedTask,
    SpecialistContext,
    SpecialistOutcome,
    SpecialistRole,
    call_records,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
    ToolCall,
    json_schema_instruction,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)
from tests.v3.test_chat import ContextRejectingProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response

# --- Routing commentary before an unreadable tool frame ----------------------

COMMENTARY = "I'll read the value first."
UNREADABLE_FRAMES = pytest.mark.parametrize(
    "frame",
    [
        # GLM: prose among the arguments.
        "<tool_call>safe_read\nthe file I mentioned\n"
        "<arg_key>value</arg_key><arg_value>a</arg_value></tool_call>",
        # DeepSeek special tokens: arguments that are not a JSON object.
        "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>safe_read<｜tool▁sep｜>"
        "value=a<｜tool▁call▁end｜><｜tool▁calls▁end｜>",
        # DSML: prose where the parameters belong.
        '<｜DSML｜ calls><｜DSML｜ invoke name="safe_read">the file I mentioned'
        "</｜DSML｜ invoke></｜DSML｜ calls>",
    ],
    ids=["glm", "deepseek", "dsml"],
)


def _stream(service: ChatService, prepared: Any) -> list[tuple[str, dict[str, Any]]]:
    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [item async for item in service.stream(prepared)]

    return asyncio.run(collect())


@UNREADABLE_FRAMES
def test_routing_commentary_stops_at_an_unreadable_frame_beside_a_real_call(
    tmp_path, frame
):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(
        tmp_path,
        [
            _response(
                text=f"{COMMENTARY}\n{frame}",
                reasoning="The operator wants the value.",
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ],
            ),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="The safe tool returned a."),
        ],
        broker,
    )

    events = _stream(service, prepared)

    shown = "".join(
        payload["delta"] for name, payload in events if name == "reasoning_delta"
    )
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        ("safe_read", {"value": "a"})
    ]
    for reasoning in (shown, turn.reasoning):
        assert "The operator wants the value." in reasoning
        # The commentary before the frame is the model's narration and stays.
        assert COMMENTARY in reasoning
        # The frame is protocol Core could not read, never operator text.
        for marker in ("<tool_call>", "arg_key", "tool▁", "DSML", "value=a"):
            assert marker not in reasoning


# --- Compaction schema without structured output -----------------------------


class _MemoryProvider(ModelProvider):
    def __init__(self, *, structured_output: bool) -> None:
        super().__init__(
            ProviderConfig(
                id="provider-memory",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(structured_output=structured_output),
            )
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text=json.dumps({"summary": "Three hosts are up in 10.0.0.0/24."}),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def _request_memory(provider: ModelProvider, tmp_path: Path) -> ContextMemory:
    compactor = ContextCompactor(NebulaStore(tmp_path / "context.db"))
    source = ContextSource(
        reference=ContextSourceReference(
            source_kind="chat_message", source_id="m1", sequence=1
        ),
        content="Operator: scan 10.0.0.0/24. Assistant: 3 hosts up.",
    )
    memory, _usage = asyncio.run(
        compactor._request_memory(
            provider,
            "model-a",
            [source],
            None,
            2048,
            input_capacity=100_000,
            prior_usage=ChatTokenUsage(),
            budget=None,
        )
    )
    return memory


SCHEMA_INSTRUCTION = json_schema_instruction(ContextMemory.model_json_schema())


def test_compaction_without_structured_output_puts_the_schema_in_the_instructions(
    tmp_path,
):
    provider = _MemoryProvider(structured_output=False)

    memory = _request_memory(provider, tmp_path)

    assert memory.summary == "Three hosts are up in 10.0.0.0/24."
    [request] = provider.requests
    # Nothing carries the schema on the wire, so the instructions must: they
    # ask for memory "matching the supplied schema".
    assert request.response_schema is None
    assert "matching the supplied schema" in (request.instructions or "")
    assert (request.instructions or "").count(SCHEMA_INSTRUCTION) == 1


def test_compaction_with_structured_output_keeps_the_schema_on_the_wire(tmp_path):
    provider = _MemoryProvider(structured_output=True)

    _request_memory(provider, tmp_path)

    [request] = provider.requests
    assert request.response_schema == ContextMemory.model_json_schema()
    assert SCHEMA_INSTRUCTION not in (request.instructions or "")


def test_compaction_schema_reaches_a_chat_completions_route_without_structured_output(
    tmp_path,
):
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"summary": "Three hosts are up."}),
                        },
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider-wire",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.CUSTOM,
            base_url="https://provider.invalid/v1",
            default_model="model-a",
            capabilities=ModelCapabilities(structured_output=False),
            options={"retry_backoff_seconds": 0},
        ),
        transport=httpx.MockTransport(handle),
    )

    memory = _request_memory(provider, tmp_path)

    assert memory.summary == "Three hosts are up."
    [payload] = payloads
    assert "response_format" not in payload
    system = "\n".join(
        str(message.get("content"))
        for message in payload["messages"]
        if message["role"] in {"system", "developer"}
    )
    assert SCHEMA_INSTRUCTION in system


# --- Context-length recovery on the non-streaming path -----------------------

QUESTION = "Is this comparison safe?"


def _recovery_profile() -> ProviderProfile:
    payload = _profile(local=False, permits_sensitive_data=True).model_dump(
        mode="python"
    )
    payload["provider_type"] = "openrouter"
    payload["model_allowlist"] = ["author/model-a"]
    payload["metadata"] = {
        "default_model": "author/model-a",
        "route_catalog_revision": "wide-routes",
        "model_descriptors": [
            {
                "id": "author/model-a",
                "context_window": 20_000,
                "max_output_tokens": 2_000,
                "route_limits_verified": True,
                "route_limits": [
                    {
                        "provider_name": "wide",
                        "context_window": 20_000,
                        "max_input_tokens": 18_000,
                        "max_output_tokens": 2_000,
                        "supported_parameters": [],
                    }
                ],
            }
        ],
    }
    return ProviderProfile.model_validate(payload)


@pytest.mark.parametrize(
    "existing", [False, True], ids=["new-conversation", "existing-conversation"]
)
def test_complete_recovers_from_a_context_length_rejection(tmp_path, existing):
    store = NebulaStore(tmp_path / "complete-recovery.db")
    engagement = store.create(Engagement(id="eng-recovery", name="Recovery"))
    profile = store.create(_recovery_profile())
    session_id: str | None = None
    history: list[tuple[str, str]] = []
    if existing:
        session = store.create(
            ChatSession(
                id="session-recovery",
                engagement_id=engagement.id,
                title="Recover context",
                provider_profile_id=profile.id,
                model="author/model-a",
                metadata={"message_count": 2, "last_sequence": 2},
            )
        )
        history = [("user", "Which hosts are up?"), ("assistant", "Three hosts.")]
        store.create_many(
            [
                ChatMessage(
                    engagement_id=engagement.id,
                    session_id=session.id,
                    sequence=index + 1,
                    role=ChatRole(role),
                    content=content,
                )
                for index, (role, content) in enumerate(history)
            ]
        )
        session_id = session.id
    provider = ContextRejectingProvider(profile.id)
    provider.config.default_model = "author/model-a"
    provider.config.model_allowlist = ["author/model-a"]
    service = ChatService(store, provider_factory=lambda _: provider)
    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session_id,
            model="author/model-a",
            allow_cloud_knowledge=True,
            include_knowledge=False,
            messages=[{"role": "user", "content": QUESTION}],
        )
    )
    # A plain, non-streamed turn keeps no durable turn record: its messages
    # are written only with the answer.
    assert prepared.turn is None

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Evidence-backed answer [source-a:chunk-a]."
    assert provider.normal_attempts == 2
    assert provider.route_refreshes == 1
    retried = next(
        request
        for request in provider.requests
        if request.metadata.get("context_length_recovery") == "1"
    )
    # The retry is the conversation as it stands, ending in this question.
    assert [(message.role, message.content) for message in retried.messages] == [
        *history,
        ("user", QUESTION),
    ]
    limits = json.loads(retried.metadata["resolved_context_limits"])
    assert limits["context_window"] == 4_000
    stored = store.list_session_entities(ChatMessage, completion.session_id)
    assert [(item.role.value, item.content) for item in stored] == [
        *history,
        ("user", QUESTION),
        ("assistant", completion.message.content),
    ]


# --- Mission specialist replay ------------------------------------------------

DEEPSEEK_KEY_ENV = "NEBULA_LEFTOVERS_TEST_KEY"


class _SpecialistBroker:
    def __init__(self) -> None:
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, scope: ScopePolicy):
        del scope
        self.calls.append(invocation)
        return ToolExecutionResult(
            output={"tool": invocation.tool_name, "ok": True},
            evidence_ids=[f"evidence-{len(self.calls)}"],
            execution={"command": ["run", invocation.tool_name]},
            exit_code=0,
        )


def _tool_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=f"{name} capability",
        risk_class=RiskClass.LOCAL_READ,
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        output_schema={"type": "object"},
        idempotency=IdempotencyBehavior.SAFE,
        budget_class="execution",
    )


def _specialist_context(prior_turns=(), turn_index: int = 1) -> SpecialistContext:
    return SpecialistContext(
        engagement_id="engagement-1",
        run_id="run-1",
        task=PlannedTask(
            id="scan",
            role=SpecialistRole.NETWORK_SERVICE,
            title="Inspect the service",
            instructions="Gather the observations the objective needs",
        ),
        objective="Map the exposed service",
        prior_results={},
        turn_index=turn_index,
        allowed_tools=frozenset({"nmap.tcp", "browser.fetch"}),
        prior_turns=list(prior_turns),
    )


def _wire_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _deepseek_step(model: str, reasoning: str, calls: list[dict[str, Any]]):
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning,
                    "tool_calls": calls,
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def test_specialist_replays_a_batch_as_one_message_with_its_reasoning(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(DEEPSEEK_KEY_ENV, "secret")
    model = "deepseek-v4-pro"
    replies = [
        _deepseek_step(
            model,
            "I need the ports and the page.",
            [
                _wire_call("call_00_a", "nmap.tcp", {"ports": [80]}),
                _wire_call("call_01_b", "browser.fetch", {"url": "http://a"}),
            ],
        ),
        _deepseek_step(
            model,
            "Both observations are in.",
            [
                _wire_call(
                    "call_02_f",
                    "nebula.finish_task",
                    {
                        "status": "complete",
                        "summary": "The service is mapped",
                        "rationale": "Both observations are in hand",
                    },
                )
            ],
        ),
    ]
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.DEEPSEEK,
            base_url="https://provider.invalid",
            default_model=model,
            model_allowlist=[model],
            api_key_env=DEEPSEEK_KEY_ENV,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(handle),
    )
    broker = _SpecialistBroker()
    specialist = BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=broker,
        scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
        workspace=tmp_path,
        specs={name: _tool_spec(name) for name in ("nmap.tcp", "browser.fetch")},
        model=model,
    )

    first = asyncio.run(specialist.run(_specialist_context()))
    second = asyncio.run(
        specialist.run(_specialist_context(prior_turns=[first], turn_index=2))
    )

    assert [call.tool_name for call in broker.calls] == ["nmap.tcp", "browser.fetch"]
    assert second.outcome is SpecialistOutcome.COMPLETE
    messages = payloads[1]["messages"]
    prompt = max(
        index for index, message in enumerate(messages) if message["role"] == "user"
    )
    replay = messages[prompt + 1 :]
    # One assistant message issued both calls, and it carries the reasoning
    # DeepSeek needs back on the next step.
    assert [
        (message["role"], [call["id"] for call in message.get("tool_calls") or []])
        for message in replay
    ] == [
        ("assistant", ["call_00_a", "call_01_b"]),
        ("tool", []),
        ("tool", []),
    ]
    assert replay[0]["reasoning_content"] == "I need the ports and the page."
    assert [message["tool_call_id"] for message in replay[1:]] == [
        "call_00_a",
        "call_01_b",
    ]


class _ReasoningRoutingProvider(ModelProvider):
    """Routing steps that each carry replay state and calls from a script."""

    def __init__(self, steps: list[list[ToolCall]]) -> None:
        super().__init__(
            ProviderConfig(
                id="provider-routing",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(),
            )
        )
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        step = len(self.requests)
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            tool_calls=self.steps.pop(0),
            reasoning_state={
                "provider_id": self.config.id,
                "model": "model-a",
                "reasoning_content": f"thinking {step}",
            },
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="tool_calls",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def test_specialist_history_keeps_each_turn_a_group_of_its_own(tmp_path):
    provider = _ReasoningRoutingProvider(
        [
            [
                ToolCall(
                    id="call-1",
                    name="nmap.tcp",
                    arguments={},
                    provider_metadata={"extra_content": {"signature": "s1"}},
                ),
                ToolCall(id="call-2", name="browser.fetch", arguments={}),
            ],
            [ToolCall(id="call-3", name="nmap.tcp", arguments={"ports": [443]})],
            [
                ToolCall(
                    id="call-4",
                    name="nebula.finish_task",
                    arguments={
                        "status": "complete",
                        "summary": "The service is mapped",
                        "rationale": "Every observation is in hand",
                    },
                )
            ],
        ]
    )
    specialist = BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=_SpecialistBroker(),
        scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
        workspace=tmp_path,
        specs={name: _tool_spec(name) for name in ("nmap.tcp", "browser.fetch")},
        model="model-a",
    )

    turns = []
    for index in range(3):
        turns.append(
            asyncio.run(
                specialist.run(
                    _specialist_context(prior_turns=turns, turn_index=index + 1)
                )
            )
        )

    replayed = provider.requests[2].tool_results
    assert [item.call_id for item in replayed] == ["call-1", "call-2", "call-3"]
    groups = [item.response_group for item in replayed]
    assert groups[0] is not None and groups[0] == groups[1]
    assert groups[2] is not None and groups[2] != groups[0]
    # Each response's state rides once, on the first call it issued.
    assert [
        (item.reasoning_state or {}).get("reasoning_content") for item in replayed
    ] == ["thinking 1", None, "thinking 2"]
    assert [item.provider_metadata for item in replayed] == [
        {"extra_content": {"signature": "s1"}},
        None,
        None,
    ]
    # The recorded turns keep the fields the next replay reads.
    assert [record.get("response_group") for record in call_records(turns[0].output)]
    assert turns[2].outcome is SpecialistOutcome.COMPLETE


def test_a_blocked_specialist_result_carries_no_replay_state(tmp_path):
    # A tool that is not offered is answered unrun; three in a row block the
    # task, and the blocked result is what later tasks and the mission read.
    provider = _ReasoningRoutingProvider(
        [
            [ToolCall(id=f"call-{index}", name="shell.exec", arguments={})]
            for index in range(3)
        ]
    )
    specialist = BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=_SpecialistBroker(),
        scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
        workspace=tmp_path,
        specs={name: _tool_spec(name) for name in ("nmap.tcp", "browser.fetch")},
        model="model-a",
    )

    turns = []
    for index in range(3):
        turns.append(
            asyncio.run(
                specialist.run(
                    _specialist_context(prior_turns=turns, turn_index=index + 1)
                )
            )
        )

    blocked = turns[-1]
    assert blocked.outcome is SpecialistOutcome.BLOCKED
    records = call_records(blocked.output)
    assert records
    for record in records:
        assert not {"response_group", "reasoning_state", "provider_metadata"} & set(
            record
        )
