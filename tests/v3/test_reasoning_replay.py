"""Reasoning state and batch boundaries survive a tool loop.

Each routing response is replayed as the one assistant message that issued its
calls, followed by their results, and carries back the reasoning state its
route needs: OpenRouter ``reasoning_details``, DeepSeek ``reasoning_content``,
Gemini ``thoughtSignature``, Anthropic thinking blocks and Responses reasoning
items. A browser screenshot travels with the result of the step that took it.
"""

import asyncio
import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService, PreparedChat
from nebula.v3.domain import (
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
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolResult,
    ModelUsage,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
    ToolCall,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ToolExecutionResult, ToolSpec

KEY_ENV = "NEBULA_REPLAY_TEST_KEY"
EARLIER_QUESTION = "Is 10.0.0.0/24 in scope?"
PRIOR_ANSWER = "10.0.0.0/24 is in scope."
QUESTION = "Read a and b."
ANSWER = "Read a and b: both are set."

SAFE_READ = ToolSpec(
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


class Broker:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        return ToolExecutionResult(output={"value": invocation.arguments["value"]})


class Wire:
    """A transport that answers from a script and keeps every request body."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.payloads: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        if not self.replies:
            raise AssertionError("wire script exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)


class Scripted(ModelProvider):
    """Returns prepared responses and records each request."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(_config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.VLLM))
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "secret")


def _config(
    kind: ProviderKind, flavor: ProviderFlavor, model: str = "model-a"
) -> ProviderConfig:
    return ProviderConfig(
        id="provider",
        kind=kind,
        flavor=flavor,
        base_url="https://provider.invalid",
        default_model=model,
        model_allowlist=[model],
        api_key_env=KEY_ENV,
        capabilities=ModelCapabilities(streaming=True, tools=True, strict_tools=True),
    )


def _chat(
    tmp_path: Path,
    provider: ModelProvider,
    model: str,
    *,
    vision: bool = False,
    artifact_store: ArtifactStore | None = None,
):
    """A follow-up tool turn: the conversation already has one exchange."""

    store = NebulaStore(tmp_path / "replay.db")
    project = store.create(Engagement(id="project", name="Replay"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=[model],
            capabilities={
                "streaming": True,
                "tool_calling": True,
                "vision": vision,
            },
        )
    )
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Replay",
            provider_profile_id=profile.id,
            model=model,
            metadata={
                "message_count": 3,
                "last_sequence": 3,
                "initial_title_state": "generated",
            },
        )
    )
    stored = [
        store.create(
            ChatMessage(
                id=f"message-{sequence}",
                engagement_id=project.id,
                session_id=session.id,
                sequence=sequence,
                role=role,
                content=content,
            )
        )
        for sequence, role, content in (
            (1, ChatRole.USER, EARLIER_QUESTION),
            (2, ChatRole.ASSISTANT, PRIOR_ANSWER),
            (3, ChatRole.USER, QUESTION),
        )
    ]
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model=model,
            tools_enabled=True,
            max_tool_calls=5,
        )
    )
    broker = Broker()
    prepared = PreparedChat(
        provider=provider,
        provider_profile=profile,
        model_request=ModelRequest(
            model=model,
            messages=[
                ModelMessage(role="user", content=EARLIER_QUESTION),
                ModelMessage(role="assistant", content=PRIOR_ANSWER),
                ModelMessage(role="user", content=QUESTION),
            ],
        ),
        resolved_model=model,
        citations=[],
        engagement_id=project.id,
        session=session,
        pending_session=None,
        stored_messages=stored,
        new_messages=[],
        tools_enabled=True,
        tool_components=RuntimeToolComponents(
            broker=broker,
            scope=ScopePolicy(engagement_id=project.id),
            workspace=tmp_path,
            specs={SAFE_READ.name: SAFE_READ},
            runtime_digest="test-runtime",
        ),
        turn=turn,
        inputs_persisted=True,
    )
    service = ChatService(store, worker_id="worker", artifact_store=artifact_store)
    return store, service, prepared, broker


def _run(service: ChatService, prepared: PreparedChat) -> str:
    return asyncio.run(service.complete(prepared)).message.content


# --- OpenAI-compatible wire helpers -------------------------------------


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _completion(message: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _text_stream(text: str) -> httpx.Response:
    chunks = [
        {"id": "gen-s", "choices": [{"index": 0, "delta": {"content": text}}]},
        {
            "id": "gen-s",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _after_question(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = max(
        position
        for position, message in enumerate(messages)
        if message.get("role") == "user" and message.get("content") == QUESTION
    )
    return messages[index + 1 :]


def _calls(message: dict[str, Any]) -> list[str]:
    return [call["id"] for call in message.get("tool_calls") or []]


def _prior_answer(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        message
        for message in messages
        if message.get("role") == "assistant" and message.get("content") == PRIOR_ANSWER
    )


OPENROUTER_DETAILS = [
    {
        "type": "reasoning.text",
        "text": "I need both values.",
        "signature": "sig-abc",
        "format": "anthropic-claude-v1",
        "index": 0,
    },
    {
        "type": "reasoning.encrypted",
        "data": "ENCRYPTED-BLOB",
        "id": "rs_1",
        "format": "anthropic-claude-v1",
        "index": 1,
    },
]


def test_openrouter_replays_reasoning_details_on_one_message_per_batch(tmp_path):
    model = "anthropic/claude-sonnet-5"
    wire = Wire(
        _completion(
            {
                "role": "assistant",
                "content": "Reading both values.",
                "reasoning": "I need both values.",
                "reasoning_details": OPENROUTER_DETAILS,
                "tool_calls": [
                    _call("call-1", "safe_read", {"value": "a"}),
                    _call("call-2", "safe_read", {"value": "b"}),
                ],
            },
            model,
        ),
        _completion(
            {
                "role": "assistant",
                "content": None,
                "reasoning_details": [],
                "tool_calls": [_call("finish-1", "finish_response", {})],
            },
            model,
        ),
        _text_stream(ANSWER),
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER, model),
        transport=wire.transport,
    )
    _, service, prepared, broker = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    for payload in wire.payloads[1:]:  # routing step 2, then the synthesis
        replay = _after_question(payload["messages"])
        assert [(message["role"], _calls(message)) for message in replay] == [
            ("assistant", ["call-1", "call-2"]),
            ("tool", []),
            ("tool", []),
        ]
        assert [message["tool_call_id"] for message in replay[1:]] == [
            "call-1",
            "call-2",
        ]
        # Echoed exactly as received, once for the message, with the text
        # OpenRouter's SDK sends beside non-empty details.
        assert replay[0]["reasoning_details"] == OPENROUTER_DETAILS
        assert replay[0]["reasoning"] == "I need both values."
        assert replay[0]["content"] == "Reading both values."
        # Not a DeepSeek V4 route: earlier plain answers are left alone.
        assert "reasoning_details" not in _prior_answer(payload["messages"])


def test_openrouter_deepseek_v4_gets_its_empty_reasoning_details_back(tmp_path):
    model = "deepseek/deepseek-v4-pro"
    wire = Wire(
        _completion(
            {
                "role": "assistant",
                "content": None,
                # V4 answers [] when it produced no visible reasoning.
                "reasoning_details": [],
                "reasoning": "",
                "tool_calls": [_call("call-1", "safe_read", {"value": "a"})],
            },
            model,
        ),
        _completion(
            {
                "role": "assistant",
                "content": None,
                "reasoning_details": [],
                "tool_calls": [_call("finish-1", "finish_response", {})],
            },
            model,
        ),
        _text_stream(ANSWER),
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER, model),
        transport=wire.transport,
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == ANSWER

    for payload in wire.payloads[1:]:
        replay = _after_question(payload["messages"])
        assert _calls(replay[0]) == ["call-1"]
        assert replay[0]["reasoning_details"] == []
        assert "reasoning" not in replay[0]
        # V4 needs the field on every assistant message, the earlier answer too.
        assert _prior_answer(payload["messages"])["reasoning_details"] == []


def test_deepseek_v4_sends_reasoning_content_on_every_assistant_message(tmp_path):
    model = "deepseek-v4-pro"
    wire = Wire(
        _completion(
            {
                "role": "assistant",
                "content": "Reading both values.",
                "reasoning_content": "I need both values.",
                "tool_calls": [
                    _call("call_00_a", "safe_read", {"value": "a"}),
                    _call("call_01_b", "safe_read", {"value": "b"}),
                ],
            },
            model,
        ),
        _completion(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Both are read.",
                "tool_calls": [_call("call_02_f", "finish_response", {})],
            },
            model,
        ),
        _text_stream(ANSWER),
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.DEEPSEEK, model),
        transport=wire.transport,
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    for payload in wire.payloads[1:]:
        replay = _after_question(payload["messages"])
        assert [(message["role"], _calls(message)) for message in replay] == [
            ("assistant", ["call_00_a", "call_01_b"]),
            ("tool", []),
            ("tool", []),
        ]
        assert replay[0]["reasoning_content"] == "I need both values."
        assert replay[0]["content"] == "Reading both values."
        # The earlier answer carries no captured reasoning: back-filled empty.
        assert _prior_answer(payload["messages"])["reasoning_content"] == ""
        assert all(
            "reasoning_content" in message
            for message in payload["messages"]
            if message["role"] == "assistant"
        )


def test_legacy_deepseek_reasoner_gets_no_back_filled_reasoning(tmp_path):
    model = "deepseek-reasoner"
    wire = Wire(
        _completion(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "I need a.",
                "tool_calls": [_call("call_00_a", "safe_read", {"value": "a"})],
            },
            model,
        ),
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_call("call_01_f", "finish_response", {})],
            },
            model,
        ),
        _text_stream(ANSWER),
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.DEEPSEEK, model),
        transport=wire.transport,
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == ANSWER

    payload = wire.payloads[1]
    replay = _after_question(payload["messages"])
    # The current turn's tool loop still gets its reasoning back.
    assert replay[0]["reasoning_content"] == "I need a."
    # R1-era models must not receive reasoning for earlier turns.
    assert "reasoning_content" not in _prior_answer(payload["messages"])


def _gemini(parts: list[dict[str, Any]], response_id: str) -> dict[str, Any]:
    return {
        "responseId": response_id,
        "candidates": [
            {"finishReason": "STOP", "content": {"role": "model", "parts": parts}}
        ],
        "usageMetadata": {
            "promptTokenCount": 2,
            "candidatesTokenCount": 1,
            "totalTokenCount": 3,
        },
    }


def test_gemini_replays_thought_signatures_with_the_whole_batch(tmp_path):
    model = "gemini-3-pro-preview"
    wire = Wire(
        _gemini(
            [
                {"text": "Reading both values."},
                {
                    "functionCall": {"name": "safe_read", "args": {"value": "a"}},
                    "thoughtSignature": "SIG-A",
                },
                # Gemini signs only the first call of a parallel batch.
                {"functionCall": {"name": "safe_read", "args": {"value": "b"}}},
            ],
            "resp-1",
        ),
        _gemini(
            [
                {
                    "functionCall": {"name": "finish_response", "args": {}},
                    "thoughtSignature": "SIG-F",
                }
            ],
            "resp-2",
        ),
        _gemini([{"text": ANSWER}], "resp-3"),
    )
    provider = GeminiProvider(
        _config(ProviderKind.GEMINI, ProviderFlavor.GEMINI, model),
        transport=wire.transport,
    )
    _, service, prepared, broker = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    assert [call.arguments["value"] for call in broker.calls] == ["a", "b"]
    for payload in wire.payloads[1:]:
        contents = payload["contents"]
        assert [content["role"] for content in contents] == [
            "user",
            "model",
            "user",
            "model",
            "user",
        ]
        calls, results = contents[3]["parts"], contents[4]["parts"]
        assert calls == [
            {"text": "Reading both values."},
            {
                "functionCall": {"name": "safe_read", "args": {"value": "a"}},
                "thoughtSignature": "SIG-A",
            },
            {"functionCall": {"name": "safe_read", "args": {"value": "b"}}},
        ]
        assert [part["functionResponse"]["name"] for part in results] == [
            "safe_read",
            "safe_read",
        ]


def test_gemini_3_steps_without_a_signature_carry_the_documented_sentinel():

    def replayed_calls(model: str) -> list[dict[str, Any]]:
        wire = Wire(_gemini([{"text": "done"}], "resp-1"))
        provider = GeminiProvider(
            _config(ProviderKind.GEMINI, ProviderFlavor.GEMINI, model),
            transport=wire.transport,
        )
        request = ModelRequest(
            model=model,
            messages=[ModelMessage(role="user", content=QUESTION)],
            tool_results=[
                # A step Core ran itself, then history from before signatures
                # were recorded: neither has a signature to send back.
                ModelToolResult(
                    call_id="nbd000000",
                    name="safe_read",
                    arguments={},
                    output={"value": "delivered"},
                    response_group="core-0",
                ),
                ModelToolResult(
                    call_id="call-1",
                    name="safe_read",
                    arguments={"value": "a"},
                    output={"value": "a"},
                ),
            ],
        )
        asyncio.run(provider.complete(request))
        return [
            part
            for content in wire.payloads[0]["contents"]
            if content["role"] == "model"
            for part in content["parts"]
        ]

    assert [
        part.get("thoughtSignature") for part in replayed_calls("gemini-3-flash")
    ] == ["skip_thought_signature_validator", "skip_thought_signature_validator"]
    assert all(
        "thoughtSignature" not in part for part in replayed_calls("gemini-2.5-flash")
    )


def test_anthropic_replays_thinking_blocks_unchanged_before_the_batch(tmp_path):
    model = "claude-sonnet-5"
    thinking = [
        {"type": "thinking", "thinking": "I need both values.", "signature": "SIG-T"},
        {"type": "redacted_thinking", "data": "REDACTED-BLOB"},
    ]

    def message(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
        return {
            "id": "msg_1",
            "model": model,
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }

    uses = [
        {
            "type": "tool_use",
            "id": "toolu_a",
            "name": "safe_read",
            "input": {"value": "a"},
        },
        {
            "type": "tool_use",
            "id": "toolu_b",
            "name": "safe_read",
            "input": {"value": "b"},
        },
    ]
    wire = Wire(
        message(
            [*thinking, {"type": "text", "text": "Reading both values."}, *uses],
            "tool_use",
        ),
        message(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_f",
                    "name": "finish_response",
                    "input": {},
                }
            ],
            "tool_use",
        ),
        message([{"type": "text", "text": ANSWER}], "end_turn"),
    )
    provider = AnthropicProvider(
        _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC, model),
        transport=wire.transport,
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    for payload in wire.payloads[1:]:
        messages = payload["messages"]
        assert [item["role"] for item in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert messages[3]["content"] == [
            *thinking,
            {"type": "text", "text": "Reading both values."},
            *uses,
        ]
        # All results in one user message, in call order.
        assert [
            (block["type"], block["tool_use_id"]) for block in messages[4]["content"]
        ] == [("tool_result", "toolu_a"), ("tool_result", "toolu_b")]


def test_bedrock_replays_a_batch_as_one_exchange(tmp_path, monkeypatch):
    model = "amazon.nova-pro-v1:0"
    seen: list[dict[str, Any]] = []

    def reply(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
        }

    replies = [
        reply(
            [
                {"text": "Reading both values."},
                {
                    "toolUse": {
                        "toolUseId": "tu-a",
                        "name": "safe_read",
                        "input": {"value": "a"},
                    }
                },
                {
                    "toolUse": {
                        "toolUseId": "tu-b",
                        "name": "safe_read",
                        "input": {"value": "b"},
                    }
                },
            ],
            "tool_use",
        ),
        reply(
            [
                {
                    "toolUse": {
                        "toolUseId": "tu-f",
                        "name": "finish_response",
                        "input": {},
                    }
                }
            ],
            "tool_use",
        ),
        reply([{"text": ANSWER}], "end_turn"),
    ]

    class Client:
        def converse(self, **kwargs):
            seen.append(copy.deepcopy(kwargs))
            return replies.pop(0)

    monkeypatch.setattr(providers.boto3, "client", lambda *args, **kwargs: Client())
    provider = BedrockProvider(
        _config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK, model)
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    for kwargs in seen[1:]:
        messages = kwargs["messages"]
        assert [item["role"] for item in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert messages[3]["content"][0] == {"text": "Reading both values."}
        assert [
            block["toolUse"]["toolUseId"] for block in messages[3]["content"][1:]
        ] == [
            "tu-a",
            "tu-b",
        ]
        assert [
            block["toolResult"]["toolUseId"] for block in messages[4]["content"]
        ] == ["tu-a", "tu-b"]


def test_responses_replays_encrypted_reasoning_items_before_their_calls(tmp_path):
    model = "gpt-5"
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "Need both values."}],
        "encrypted_content": "ENCRYPTED-REASONING",
    }

    def response(output: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": "resp_1",
            "model": model,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }

    def function_call(call_id: str, name: str, arguments: dict[str, Any]):
        return {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        }

    wire = Wire(
        response(
            [
                reasoning_item,
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {"type": "output_text", "text": "Reading both values."}
                    ],
                },
                function_call("call-a", "safe_read", {"value": "a"}),
                function_call("call-b", "safe_read", {"value": "b"}),
            ]
        ),
        response([function_call("call-f", "finish_response", {})]),
        response(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ANSWER}],
                }
            ]
        ),
    )
    provider = OpenAIResponsesProvider(
        _config(ProviderKind.OPENAI_RESPONSES, ProviderFlavor.OPENAI, model),
        transport=wire.transport,
    )
    _, service, prepared, _ = _chat(tmp_path, provider, model)

    assert _run(service, prepared) == "Reading both values.\n\n" + ANSWER

    # Nothing is stored server-side, so the reasoning comes back encrypted.
    assert wire.payloads[0]["include"] == ["reasoning.encrypted_content"]
    for payload in wire.payloads[1:]:
        replay = payload["input"][3:]
        assert replay[0] == reasoning_item
        assert replay[1] == {"role": "assistant", "content": "Reading both values."}
        assert [(item["type"], item["call_id"]) for item in replay[2:]] == [
            ("function_call", "call-a"),
            ("function_call", "call-b"),
            ("function_call_output", "call-a"),
            ("function_call_output", "call-b"),
        ]


def test_chat_records_the_batch_group_and_reasoning_once(tmp_path):
    state = {
        "provider_id": "provider",
        "model": "model-a",
        "reasoning_content": "Check both.",
    }
    responses = [
        ModelResponse(
            provider_id="provider",
            model="model-a",
            reasoning="Check both.",
            reasoning_state=state,
            tool_calls=[
                # Core refuses this one; it still belongs to the batch.
                ToolCall(id="call-1", name="other_tool", arguments={}),
                ToolCall(
                    id="call-2",
                    name="safe_read",
                    arguments={"value": "b"},
                    provider_metadata={"thought_signature": "SIG-2"},
                ),
            ],
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            finish_reason="tool_calls",
        ),
        ModelResponse(
            provider_id="provider",
            model="model-a",
            tool_calls=[ToolCall(id="finish-1", name="finish_response", arguments={})],
            finish_reason="tool_calls",
        ),
        ModelResponse(provider_id="provider", model="model-a", text=ANSWER),
    ]
    provider = Scripted(responses)
    store, service, prepared, _ = _chat(tmp_path, provider, "model-a")

    assert _run(service, prepared) == ANSWER

    history = store.get(ChatTurn, "turn").tool_history
    assert [entry["budget_class"] for entry in history] == ["refused", "execution"]
    group = history[0]["response_group"]
    assert group and history[1]["response_group"] == group
    assert history[0]["reasoning_state"] == state
    assert "reasoning_state" not in history[1]
    assert "provider_metadata" not in history[0]
    assert history[1]["provider_metadata"] == {"thought_signature": "SIG-2"}
    replayed = provider.requests[1].tool_results
    assert [result.response_group for result in replayed] == [group, group]
    assert replayed[0].reasoning_state == state
    assert replayed[1].provider_metadata == {"thought_signature": "SIG-2"}


def test_an_unreadable_call_stays_in_the_batch_that_issued_it(tmp_path):
    state = {"provider_id": "provider", "model": "model-a", "reasoning_content": "Go."}
    responses = [
        ModelResponse(
            provider_id="provider",
            model="model-a",
            reasoning_state=state,
            tool_calls=[
                # A call Core could not read is answered as a refused step.
                ToolCall(
                    id="call-1",
                    name="safe_read",
                    arguments={},
                    invalid_reason="arguments were not a JSON object",
                ),
                ToolCall(id="call-2", name="safe_read", arguments={"value": "b"}),
            ],
            finish_reason="tool_calls",
        ),
        ModelResponse(
            provider_id="provider",
            model="model-a",
            tool_calls=[ToolCall(id="finish-1", name="finish_response", arguments={})],
            finish_reason="tool_calls",
        ),
        ModelResponse(provider_id="provider", model="model-a", text=ANSWER),
    ]
    provider = Scripted(responses)
    store, service, prepared, broker = _chat(tmp_path, provider, "model-a")

    assert _run(service, prepared) == ANSWER

    assert [call.arguments["value"] for call in broker.calls] == ["b"]
    history = store.get(ChatTurn, "turn").tool_history
    assert [entry["budget_class"] for entry in history] == ["refused", "execution"]
    assert history[0]["response_group"] == history[1]["response_group"]
    assert history[0]["reasoning_state"] == state
    replayed = provider.requests[1].tool_results
    assert [result.call_id for result in replayed] == ["call-1", "call-2"]
    assert len({result.response_group for result in replayed}) == 1


def test_a_step_core_delivers_is_a_batch_of_its_own(tmp_path):
    store, service, prepared, _ = _chat(tmp_path, Scripted([]), "model-a")
    turn = store.get(ChatTurn, "turn")

    turn, _ = service._subagent_delivery_step(
        turn,
        prepared.tool_components,
        "safe_read",
        {"value": "delivered"},
        "Delivered one message.",
    )

    entry = turn.tool_history[-1]
    assert entry["response_group"] == "core-0"
    assert "reasoning_state" not in entry
    (result,) = ChatService._provider_tool_history(turn)
    assert result.response_group == "core-0"
    assert result.reasoning_state is None


def test_history_without_groups_still_replays_one_call_per_message():
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.VLLM)
    )

    def result(call_id: str, group: str | None) -> ModelToolResult:
        return ModelToolResult(
            call_id=call_id,
            name="safe_read",
            arguments={"value": call_id},
            output={"value": call_id},
            response_group=group,
        )

    payload = provider._payload(
        ModelRequest(
            messages=[ModelMessage(role="user", content=QUESTION)],
            tool_results=[
                result("old-1", None),
                result("old-2", None),
                result("new-1", "g1"),
                result("new-2", "g1"),
                result("new-3", "g2"),
            ],
        ),
        "model-a",
    )

    assert [
        (message["role"], _calls(message) or message.get("tool_call_id"))
        for message in payload["messages"][1:]
    ] == [
        ("assistant", ["old-1"]),
        ("tool", "old-1"),
        ("assistant", ["old-2"]),
        ("tool", "old-2"),
        ("assistant", ["new-1", "new-2"]),
        ("tool", "new-1"),
        ("tool", "new-2"),
        ("assistant", ["new-3"]),
        ("tool", "new-3"),
    ]


def test_reasoning_from_another_route_or_model_is_not_replayed():
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER, "vendor/b")
    )
    state = {
        "provider_id": "provider",
        "model": "vendor/a",
        "reasoning_details": OPENROUTER_DETAILS,
    }
    payload = provider._payload(
        ModelRequest(
            messages=[ModelMessage(role="user", content=QUESTION)],
            tool_results=[
                ModelToolResult(
                    call_id=call_id,
                    name="safe_read",
                    arguments={"value": call_id},
                    output={"value": call_id},
                    response_group="g1",
                    reasoning_state=state if call_id == "call-1" else None,
                )
                for call_id in ("call-1", "call-2")
            ],
        ),
        "vendor/b",
    )

    (assistant,) = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert _calls(assistant) == ["call-1", "call-2"]
    # A signature is only valid for the model that produced it.
    assert "reasoning_details" not in assistant


def test_openrouter_drops_reasoning_text_that_lost_its_signature():
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER, "vendor/a")
    )
    unsigned = {
        "type": "reasoning.text",
        "text": "unsigned",
        "format": "anthropic-claude-v1",
    }
    openai_text = {
        "type": "reasoning.text",
        "text": "kept",
        "format": "openai-responses-v1",
    }
    payload = provider._payload(
        ModelRequest(
            messages=[ModelMessage(role="user", content=QUESTION)],
            tool_results=[
                ModelToolResult(
                    call_id="call-1",
                    name="safe_read",
                    arguments={"value": "a"},
                    output={"value": "a"},
                    response_group="g1",
                    reasoning_state={
                        "provider_id": "provider",
                        "model": "vendor/a",
                        "reasoning_details": [unsigned, openai_text],
                    },
                )
            ],
        ),
        "vendor/a",
    )

    (assistant,) = [m for m in payload["messages"] if m["role"] == "assistant"]
    # Anthropic rejects a thinking block without its signature.
    assert assistant["reasoning_details"] == [openai_text]


def test_glm_reasoning_is_replayed_only_when_preserved_thinking_is_on():
    state = {"provider_id": "provider", "model": "glm-5", "reasoning_content": "Plan."}
    request = ModelRequest(
        messages=[ModelMessage(role="user", content=QUESTION)],
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="safe_read",
                arguments={"value": "a"},
                output={"value": "a"},
                response_group="g1",
                reasoning_state=state,
            )
        ],
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.CUSTOM, "glm-5")
    )
    payload = provider._payload(request, "glm-5")
    (assistant,) = [m for m in payload["messages"] if m["role"] == "assistant"]
    # Z.ai drops replayed reasoning unless clear_thinking is false.
    assert "reasoning_content" not in assistant

    # With preserved thinking requested, the step's reasoning goes back.
    ((_, batch_state),) = providers._replayed_batches(request, "provider", "glm-5")
    preserved = {**payload, "thinking": {"type": "enabled", "clear_thinking": False}}
    providers._replay_reasoning(
        preserved, [(assistant, batch_state)], ProviderFlavor.CUSTOM, "glm-5"
    )
    assert assistant["reasoning_content"] == "Plan."


def test_streamed_reasoning_details_and_call_signatures_are_kept_whole():
    """Fragments merge the way OpenRouter's SDK merges them."""

    chunks = [
        {
            "reasoning": "I need ",
            "reasoning_details": [
                {
                    "type": "reasoning.text",
                    "text": "I need ",
                    "format": "anthropic-claude-v1",
                    "index": 0,
                }
            ],
        },
        {
            "reasoning": "a.",
            "reasoning_details": [{"type": "reasoning.text", "text": "a.", "index": 0}],
        },
        # The signature arrives on its own, after the text.
        {
            "reasoning_details": [
                {"type": "reasoning.text", "text": "", "signature": "sig-1", "index": 0}
            ]
        },
        {
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "ENC", "id": "rs_1", "index": 1}
            ]
        },
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "safe_read", "arguments": '{"value": "a"}'},
                    "extra_content": {"google": {"thought_signature": "SIG"}},
                }
            ]
        },
    ]
    frames = [
        {"id": "gen-1", "model": "vendor/a", "choices": [{"index": 0, "delta": delta}]}
        for delta in chunks
    ] + [
        {
            "id": "gen-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    wire = Wire(
        httpx.Response(
            200,
            text=body + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.OPENROUTER, "vendor/a"),
        transport=wire.transport,
    )

    async def completed() -> ModelResponse:
        async for event in provider.stream(
            ModelRequest(messages=[ModelMessage(role="user", content=QUESTION)])
        ):
            if event.response is not None:
                return event.response
        raise AssertionError("no completed response")

    response = asyncio.run(completed())

    assert response.reasoning == "I need a."
    assert response.reasoning_state == {
        "provider_id": "provider",
        "model": "vendor/a",
        "reasoning": "I need a.",
        "reasoning_details": [
            {
                "type": "reasoning.text",
                "text": "I need a.",
                "format": "anthropic-claude-v1",
                "index": 0,
                "signature": "sig-1",
            },
            {"type": "reasoning.encrypted", "data": "ENC", "id": "rs_1", "index": 1},
        ],
    }
    assert response.tool_calls[0].provider_metadata == {
        "extra_content": {"google": {"thought_signature": "SIG"}}
    }


def test_oversized_reasoning_state_is_dropped_not_stored():
    response = ModelResponse(
        provider_id="provider",
        model="model-a",
        reasoning_state={
            "provider_id": "provider",
            "model": "model-a",
            "reasoning_content": "x" * (512 * 1024),
        },
    )

    # The step is then replayed as it was before reasoning was kept.
    assert response.reasoning_state is None


# --- HIST-10: a screenshot follows the result of the step that took it ----

LABEL = {
    "type": "text",
    "text": "Historical page screenshot captured by browser.companion.",
}
IMAGE = {
    "type": "image",
    "media_type": "image/png",
    "data": base64.b64encode(b"png").decode(),
}


def _screenshot_request() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content=QUESTION)],
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="browser_companion",
                arguments={"operation": "capture"},
                output="captured",
                response_group="g1",
                attachments=[LABEL, IMAGE],
            ),
            ModelToolResult(
                call_id="call-2",
                name="safe_read",
                arguments={"value": "b"},
                output="b",
                response_group="g1",
            ),
        ],
    )


def test_screenshot_follows_its_result_on_chat_completions():
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.VLLM)
    )
    messages = provider._payload(_screenshot_request(), "model-a")["messages"]

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    # Tool messages take text only, so the image comes right after the batch.
    assert [part["type"] for part in messages[-1]["content"]] == ["text", "image_url"]


def test_screenshot_travels_inside_its_tool_result_on_native_adapters(monkeypatch):
    request = _screenshot_request()

    responses = OpenAIResponsesProvider(
        _config(ProviderKind.OPENAI_RESPONSES, ProviderFlavor.OPENAI)
    )._payload(request, "model-a")["input"]
    first_output = responses[3]
    assert first_output["type"] == "function_call_output"
    assert [part["type"] for part in first_output["output"]] == [
        "input_text",
        "input_text",
        "input_image",
    ]
    assert responses[4]["output"] == "b"

    anthropic_wire = Wire(
        {
            "id": "msg",
            "model": "model-a",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    asyncio.run(
        AnthropicProvider(
            _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC),
            transport=anthropic_wire.transport,
        ).complete(request)
    )
    results = anthropic_wire.payloads[0]["messages"][-1]["content"]
    assert [block["type"] for block in results[0]["content"]] == [
        "text",
        "text",
        "image",
    ]
    assert results[1]["content"] == "b"

    gemini_wire = Wire(_gemini([{"text": "ok"}], "resp-1"))
    asyncio.run(
        GeminiProvider(
            _config(ProviderKind.GEMINI, ProviderFlavor.GEMINI),
            transport=gemini_wire.transport,
        ).complete(request)
    )
    parts = gemini_wire.payloads[0]["contents"][-1]["parts"]
    assert [next(iter(part)) for part in parts] == [
        "functionResponse",
        "text",
        "inline_data",
        "functionResponse",
    ]

    seen: list[dict[str, Any]] = []

    class Client:
        def converse(self, **kwargs):
            seen.append(kwargs)
            return {
                "output": {
                    "message": {"role": "assistant", "content": [{"text": "ok"}]}
                },
                "stopReason": "end_turn",
                "usage": {},
            }

    monkeypatch.setattr(providers.boto3, "client", lambda *args, **kwargs: Client())
    asyncio.run(
        BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK)).complete(
            request
        )
    )
    bedrock_results = seen[0]["messages"][-1]["content"]
    assert [
        next(iter(block)) for block in bedrock_results[0]["toolResult"]["content"]
    ] == [
        "text",
        "text",
        "image",
    ]


def test_chat_attaches_only_the_newest_screenshot_to_its_own_result(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    provider = Scripted(
        [
            ModelResponse(
                provider_id="provider",
                model="model-a",
                tool_calls=[
                    ToolCall(id="finish-1", name="finish_response", arguments={})
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(provider_id="provider", model="model-a", text=ANSWER),
        ]
    )
    store, service, prepared, _ = _chat(
        tmp_path, provider, "model-a", vision=True, artifact_store=artifacts
    )
    turn = store.get(ChatTurn, "turn")
    history = []
    for step, data in enumerate((b"older screenshot", b"newest screenshot")):
        tool_call_id = f"tool-call-{step}"
        artifact = store.create(
            artifacts.put_bytes(
                data,
                engagement_id="project",
                media_type="image/png",
                source="browser.companion",
                metadata={"chat_session_id": "session", "tool_call_id": tool_call_id},
            )
        )
        history.append(
            {
                "step": step,
                "model_call_id": f"call-{step}",
                "tool_call_id": tool_call_id,
                "name": "browser.companion",
                "arguments": {"operation": "capture"},
                "budget_class": "execution",
                "status": "complete",
                "provider_result": json.dumps({"captured": step}),
                "artifacts": [{"artifact_id": artifact.id}],
                "response_group": f"g{step}",
            }
        )
    store.update(
        ChatTurn,
        turn.id,
        {"tool_history": history, "next_step": 2, "status": ChatTurnStatus.ROUTING},
        expected_revision=turn.revision,
    )

    assert _run(service, prepared) == ANSWER

    routing = provider.requests[0]
    # No screenshot rides ahead of the calls as an extra user message.
    assert [message.content for message in routing.messages][-1] == QUESTION
    older, newest = routing.tool_results
    assert older.attachments == []
    assert newest.attachments[0]["text"].startswith("Historical page screenshot")
    assert base64.b64decode(newest.attachments[1]["data"]) == b"newest screenshot"


def test_a_text_only_route_gets_no_screenshot(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store, service, prepared, _ = _chat(
        tmp_path, Scripted([]), "model-a", vision=False, artifact_store=artifacts
    )
    turn = store.get(ChatTurn, "turn")
    artifact = store.create(
        artifacts.put_bytes(
            b"screenshot",
            engagement_id="project",
            media_type="image/png",
            source="browser.companion",
            metadata={"chat_session_id": "session", "tool_call_id": "tool-call-0"},
        )
    )
    turn = turn.model_copy(
        update={
            "tool_history": [
                {
                    "step": 0,
                    "model_call_id": "call-0",
                    "tool_call_id": "tool-call-0",
                    "name": "browser.companion",
                    "status": "complete",
                    "provider_result": "{}",
                    "artifacts": [{"artifact_id": artifact.id}],
                }
            ]
        }
    )

    (result,) = service._replayed_tool_history(prepared, turn)
    assert result.attachments == []
