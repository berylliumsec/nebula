"""Vendor stop outcomes the native adapters must not read as empty replies.

Anthropic, Gemini, Bedrock and the OpenAI Responses API all answer HTTP 200
when a safeguard refuses, a filter blocks, a tool call comes out malformed or
the output limit cuts the reply short. Each outcome says why there is no
answer; reading it as an ordinary empty reply made chat skip the call the
model attempted or end with "no operator-facing answer after bounded
recovery" instead of telling the operator what happened.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
from nebula.v3 import providers
from nebula.v3.api import create_app
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
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderContextLengthError,
    ProviderError,
    ProviderHealth,
    ProviderKind,
    ProviderMalformedToolCallError,
    ProviderOverloadedError,
    ProviderRefusalError,
    ProviderResponseError,
    StreamEventType,
    ToolChoice,
    ToolDefinition,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ToolExecutionResult, ToolSpec

KEY_ENV = "NEBULA_TEST_OUTCOME_KEY"

LOOKUP = ToolDefinition(
    name="lookup_asset",
    description="Look up one asset",
    input_schema={
        "type": "object",
        "properties": {"address": {"type": "string"}},
        "required": ["address"],
        "additionalProperties": False,
    },
)
# The finish tool chat adds to every routing step.
FINISH = ToolDefinition(
    name="finish_response",
    description="Finish tool routing and produce the final analyst response.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    strict=True,
)


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")


def _config(kind: ProviderKind, **extra: Any) -> ProviderConfig:
    return ProviderConfig(
        id="provider",
        kind=kind,
        base_url="https://provider.invalid",
        default_model="model-a",
        api_key_env=KEY_ENV,
        capabilities=ModelCapabilities(
            streaming=True, tools=True, strict_tools=True, structured_output=True
        ),
        **extra,
    )


def _routing_request() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        tools=[LOOKUP, FINISH],
        tool_choice=ToolChoice.REQUIRED,
        parallel_tool_calls=True,
        max_output_tokens=4096,
    )


def _synthesis_request() -> ModelRequest:
    """What chat sends for final synthesis: no tools."""

    return ModelRequest(
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        max_output_tokens=4096,
    )


def _complete(provider: ModelProvider, request: ModelRequest) -> ModelResponse:
    return asyncio.run(provider.complete(request))


def _http_provider(provider_type, kind: ProviderKind, *bodies: dict[str, Any]):
    """An HTTP adapter answering 200 with each body in turn (the last repeats)."""

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=bodies[min(len(sent), len(bodies)) - 1])

    return provider_type(_config(kind), transport=httpx.MockTransport(handler)), sent


def _bedrock_provider(monkeypatch, *replies: dict[str, Any]):
    sent: list[dict[str, Any]] = []

    class Runtime:
        def converse(self, **kwargs):
            sent.append(kwargs)
            return replies[min(len(sent), len(replies)) - 1]

    monkeypatch.setattr(providers.boto3, "client", lambda *args, **kwargs: Runtime())
    return BedrockProvider(_config(ProviderKind.BEDROCK)), sent


def _anthropic(content: list[dict[str, Any]], stop_reason: str, **extra: Any):
    return {
        "id": "msg_1",
        "model": "model-a",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 5, "output_tokens": 1},
        **extra,
    }


CYBER_REFUSAL = _anthropic(
    [],
    "refusal",
    stop_details={
        "type": "refusal",
        "category": "cyber",
        "explanation": "This request was declined by a cyber safeguard.",
    },
)


# --- Anthropic ---------------------------------------------------------------


def test_anthropic_refusal_without_text_is_reported_with_its_category():
    provider, _ = _http_provider(
        AnthropicProvider, ProviderKind.ANTHROPIC, CYBER_REFUSAL
    )

    with pytest.raises(ProviderRefusalError) as failure:
        _complete(provider, _synthesis_request())

    assert isinstance(failure.value, ProviderResponseError)
    assert str(failure.value) == (
        "provider blocked the response: refusal (category: cyber): "
        "This request was declined by a cyber safeguard."
    )


def test_anthropic_refusal_without_details_still_names_the_refusal():
    provider, _ = _http_provider(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        _anthropic([], "refusal", stop_details=None),
    )

    with pytest.raises(
        ProviderRefusalError, match=r"^provider blocked the response: refusal$"
    ):
        _complete(provider, _routing_request())


def test_anthropic_refusal_that_explains_itself_returns_the_explanation():
    provider, _ = _http_provider(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        _anthropic(
            [
                {"type": "text", "text": "I can't help with that request."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lookup_asset",
                    "input": {"address": "10.0.0.1"},
                },
            ],
            "refusal",
        ),
    )

    response = _complete(provider, _routing_request())

    assert response.text == "I can't help with that request."
    # A call from a response the vendor stopped as a refusal never runs.
    assert response.tool_calls == []
    assert response.finish_reason == "refusal"


def test_anthropic_context_window_stop_asks_for_compaction():
    provider, _ = _http_provider(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        _anthropic(
            [{"type": "text", "text": "partial"}], "model_context_window_exceeded"
        ),
    )

    with pytest.raises(ProviderContextLengthError, match="context window"):
        _complete(provider, _synthesis_request())


# --- Gemini ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,reason",
    [
        (
            {
                "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
                "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 10},
            },
            "prompt blocked (PROHIBITED_CONTENT)",
        ),
        (
            {
                "responseId": "r1",
                "candidates": [
                    {
                        "finishReason": "SAFETY",
                        "safetyRatings": [
                            {
                                "category": "HARM_CATEGORY_HARASSMENT",
                                "probability": "NEGLIGIBLE",
                            },
                            {
                                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                                "probability": "HIGH",
                                "blocked": True,
                            },
                        ],
                    }
                ],
            },
            "SAFETY (HARM_CATEGORY_DANGEROUS_CONTENT)",
        ),
        *[
            (
                {
                    "responseId": "r2",
                    "candidates": [
                        {
                            "finishReason": reason,
                            "content": {
                                "role": "model",
                                "parts": [{"text": "partial output"}],
                            },
                        }
                    ],
                },
                reason,
            )
            for reason in [
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
            ]
        ],
    ],
)
def test_gemini_blocks_are_reported_not_read_as_empty(body, reason):
    provider, _ = _http_provider(GeminiProvider, ProviderKind.GEMINI, body)

    with pytest.raises(ProviderRefusalError) as failure:
        _complete(provider, _synthesis_request())

    assert str(failure.value) == f"provider blocked the response: {reason}"


@pytest.mark.parametrize("reason", ["MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"])
def test_gemini_malformed_routing_call_is_a_retryable_typed_error(reason):
    body = {
        "responseId": "r3",
        "candidates": [
            {
                "finishReason": reason,
                "finishMessage": (
                    "Malformed function call: "
                    "print(default_api.lookup_asset(address='10.0.0.1'"
                ),
                "content": {"parts": []},
            }
        ],
    }
    provider, _ = _http_provider(GeminiProvider, ProviderKind.GEMINI, body)

    with pytest.raises(ProviderMalformedToolCallError) as failure:
        _complete(provider, _routing_request())

    assert str(failure.value) == f"provider returned a malformed tool call ({reason})"
    # The model's half-written call stays out of the error text.
    assert "10.0.0.1" not in str(failure.value)

    # A request that offered no tools keeps the empty reply for the caller's
    # own final-answer recovery, which asks again without tools.
    response = _complete(provider, _synthesis_request())
    assert response.text == ""
    assert response.tool_calls == []
    assert response.finish_reason == reason


def test_gemini_ordinary_stops_are_untouched():
    provider, _ = _http_provider(
        GeminiProvider,
        ProviderKind.GEMINI,
        {
            "responseId": "r4",
            "promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"},
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"role": "model", "parts": [{"text": "Port 22"}]},
                }
            ],
        },
    )

    response = _complete(provider, _synthesis_request())

    assert response.text == "Port 22"
    assert response.finish_reason == "MAX_TOKENS"


# --- Bedrock -----------------------------------------------------------------


def _converse(stop_reason: str, content: list[dict[str, Any]] | None = None):
    return {
        "output": {"message": {"role": "assistant", "content": content or []}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 5, "outputTokens": 0, "totalTokens": 5},
    }


@pytest.mark.parametrize("stop_reason", ["guardrail_intervened", "content_filtered"])
def test_bedrock_guardrail_and_filter_stops_are_reported(monkeypatch, stop_reason):
    provider, _ = _bedrock_provider(
        monkeypatch,
        _converse(stop_reason, [{"text": "Sorry, the model cannot answer this."}]),
    )

    with pytest.raises(
        ProviderRefusalError,
        match=rf"^provider blocked the response: {stop_reason}$",
    ):
        _complete(provider, _synthesis_request())


@pytest.mark.parametrize(
    "stop_reason", ["malformed_tool_use", "malformed_model_output"]
)
def test_bedrock_malformed_routing_output_is_a_retryable_typed_error(
    monkeypatch, stop_reason
):
    provider, _ = _bedrock_provider(monkeypatch, _converse(stop_reason))

    with pytest.raises(
        ProviderMalformedToolCallError,
        match=rf"^provider returned a malformed tool call \({stop_reason}\)$",
    ):
        _complete(provider, _routing_request())


def test_bedrock_context_window_stop_asks_for_compaction(monkeypatch):
    provider, _ = _bedrock_provider(
        monkeypatch, _converse("model_context_window_exceeded")
    )

    with pytest.raises(ProviderContextLengthError, match="context window"):
        _complete(provider, _synthesis_request())


# --- OpenAI Responses ----------------------------------------------------------


def _responses(output: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "model": "model-a",
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        **extra,
    }


def test_responses_refusal_part_is_the_answer_text():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "I can't help with that request.",
                        }
                    ],
                }
            ]
        ),
    )

    response = _complete(provider, _synthesis_request())

    assert response.text == "I can't help with that request."
    assert chat_module._final_answer_problem(response) is None


def test_responses_blank_refusal_is_reported():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": " "}],
                }
            ]
        ),
    )

    with pytest.raises(
        ProviderRefusalError, match=r"^provider blocked the response: refusal$"
    ):
        _complete(provider, _synthesis_request())


def test_responses_content_filter_cut_is_reported():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [],
            status="incomplete",
            incomplete_details={"reason": "content_filter"},
        ),
    )

    with pytest.raises(
        ProviderRefusalError, match=r"^provider blocked the response: content_filter$"
    ):
        _complete(provider, _synthesis_request())


@pytest.mark.parametrize(
    "error,expected,message",
    [
        (
            {"code": "cyber_policy", "message": "This request was flagged."},
            ProviderRefusalError,
            "provider blocked the response: cyber_policy: This request was flagged.",
        ),
        (
            {
                "code": "context_length_exceeded",
                "message": "Your input exceeds the context window of this model.",
            },
            ProviderContextLengthError,
            "provider reported a failed response: context_length_exceeded: "
            "Your input exceeds the context window of this model.",
        ),
        (
            {"code": "server_error", "message": "The server had an error."},
            ProviderOverloadedError,
            "provider reported a failed response: server_error: "
            "The server had an error.",
        ),
        (
            {"code": "vector_store_timeout", "message": "Search timed out."},
            ProviderError,
            "provider reported a failed response: vector_store_timeout: "
            "Search timed out.",
        ),
    ],
)
def test_responses_failed_status_is_a_typed_failure(error, expected, message):
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses([], status="failed", error=error),
    )

    with pytest.raises(ProviderError) as failure:
        _complete(provider, _synthesis_request())

    assert type(failure.value) is expected
    assert str(failure.value) == message


def test_responses_output_limit_is_recognised_for_recovery():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [{"type": "reasoning", "id": "rs_1", "summary": []}],
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        ),
    )

    response = _complete(provider, _synthesis_request())

    assert response.finish_reason == "max_output_tokens"
    # Chat's recovery enlarges the output budget only for an output limit.
    assert chat_module._final_answer_problem(response) == "output_limit"


def test_responses_cut_off_function_call_is_skipped_not_malformed():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "lookup_asset",
                    "arguments": '{"address":"10.0.0.1"}',
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "fc_2",
                    "call_id": "call_2",
                    "name": "lookup_asset",
                    "arguments": '{"address": "10.0.',
                    "status": "incomplete",
                },
            ],
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        ),
    )

    response = _complete(provider, _routing_request())

    assert [(call.id, call.arguments) for call in response.tool_calls] == [
        ("call_1", {"address": "10.0.0.1"})
    ]
    assert response.finish_reason == "max_output_tokens"


def test_responses_commentary_phase_is_reasoning_not_answer_text():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "I'll look up the host first.",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "lookup_asset",
                    "arguments": '{"address":"10.0.0.1"}',
                    "status": "completed",
                },
            ]
        ),
    )

    response = _complete(provider, _routing_request())

    assert response.text == ""
    assert response.reasoning == "I'll look up the host first."
    assert [call.name for call in response.tool_calls] == ["lookup_asset"]


def test_responses_final_answer_phase_stays_answer_text():
    provider, _ = _http_provider(
        OpenAIResponsesProvider,
        ProviderKind.OPENAI_RESPONSES,
        _responses(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Port 22 is open."}],
                }
            ]
        ),
    )

    response = _complete(provider, _synthesis_request())

    assert response.text == "Port 22 is open."
    assert response.reasoning == ""
    assert response.finish_reason == "completed"


# --- Chat --------------------------------------------------------------------


class RecordingBroker:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        return ToolExecutionResult(output={"value": invocation.arguments["value"]})


def _chat(tmp_path: Path, provider: ModelProvider):
    store = NebulaStore(tmp_path / "outcomes.db")
    project = store.create(Engagement(id="project", name="Outcomes"))
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
            title="Outcomes",
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
            max_tool_calls=5,
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
    broker = RecordingBroker()
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
            specs={spec.name: spec},
            runtime_digest="test-runtime",
        ),
        turn=turn,
        inputs_persisted=True,
    )
    return store, ChatService(store, worker_id="worker"), prepared, broker


def _gemini_call(name: str, args: dict[str, Any], response_id: str):
    return {
        "responseId": response_id,
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": name, "args": args}}],
                },
            }
        ],
    }


GEMINI_MALFORMED = {
    "responseId": "bad",
    "candidates": [
        {
            "finishReason": "MALFORMED_FUNCTION_CALL",
            "finishMessage": "Malformed function call: print(default_api.safe_read(",
            "content": {"parts": []},
        }
    ],
}


def test_malformed_routing_call_is_routed_again_instead_of_skipped(tmp_path):
    provider, sent = _http_provider(
        GeminiProvider,
        ProviderKind.GEMINI,
        GEMINI_MALFORMED,
        _gemini_call("safe_read", {"value": "a"}, "step-1"),
        _gemini_call("finish_response", {}, "step-2"),
        {
            "responseId": "final",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"text": "Read a."}]},
                }
            ],
        },
    )
    store, service, prepared, broker = _chat(tmp_path, provider)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Read a."
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    # The malformed step and its identical retry, one step after the tool,
    # then synthesis.
    assert len(sent) == 4
    assert sent[0] == sent[1]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_a_routing_call_malformed_twice_fails_the_turn_with_the_reason(tmp_path):
    provider, sent = _http_provider(
        GeminiProvider, ProviderKind.GEMINI, GEMINI_MALFORMED
    )
    store, service, prepared, broker = _chat(tmp_path, provider)

    with pytest.raises(ProviderMalformedToolCallError):
        asyncio.run(service.complete(prepared))

    assert len(sent) == 2
    assert broker.calls == []
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert failed.error == (
        "provider returned a malformed tool call (MALFORMED_FUNCTION_CALL)"
    )


def _anthropic_call(call_id: str, name: str, arguments: dict[str, Any]):
    return _anthropic(
        [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
        "tool_use",
    )


def test_refused_synthesis_reaches_the_operator_without_recovery(tmp_path):
    provider, sent = _http_provider(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        _anthropic_call("toolu_1", "safe_read", {"value": "a"}),
        _anthropic_call("toolu_2", "finish_response", {}),
        CYBER_REFUSAL,
    )
    store, service, prepared, broker = _chat(tmp_path, provider)

    # The refusal is raised as itself, as a non-streamed call raises it.
    with pytest.raises(ProviderRefusalError, match="provider blocked the response"):
        asyncio.run(service.complete(prepared))

    # Asking a refusing safeguard again gets the same refusal; the synthesis
    # request was sent once.
    assert len(sent) == 3
    assert [call.arguments["value"] for call in broker.calls] == ["a"]
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert failed.error == str(
        ProviderRefusalError(
            "refusal (category: cyber): This request was declined by a cyber safeguard."
        )
    )
    assert "final_answer_recovery" not in failed.request_snapshot


def test_refused_routing_step_fails_the_turn_with_the_refusal(tmp_path):
    provider, sent = _http_provider(
        AnthropicProvider, ProviderKind.ANTHROPIC, CYBER_REFUSAL
    )
    store, service, prepared, broker = _chat(tmp_path, provider)

    with pytest.raises(ProviderRefusalError, match="category: cyber"):
        asyncio.run(service.complete(prepared))

    # Not read as "no tool needed": nothing ran and no synthesis was asked for.
    assert len(sent) == 1
    assert broker.calls == []
    failed = store.get(ChatTurn, "turn")
    assert failed.status == ChatTurnStatus.FAILED
    assert failed.error.startswith("provider blocked the response: refusal")


def test_chat_stream_shows_a_refusal_instead_of_offering_to_finish_the_answer(
    tmp_path, monkeypatch
):
    class RefusingProvider(ModelProvider):
        def __init__(self, provider_id: str) -> None:
            super().__init__(
                ProviderConfig(
                    id=provider_id,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="http://127.0.0.1:8000/v1",
                    default_model="model-a",
                    model_allowlist=["model-a"],
                    local=True,
                    capabilities=ModelCapabilities(streaming=True),
                )
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise ProviderRefusalError("refusal (category: cyber)")

        async def stream(self, request: ModelRequest):
            # A routing step's refusal reaches the API as the typed error
            # itself, not as a stream error event.
            yield providers.ModelStreamEvent(type=StreamEventType.STARTED)
            raise ProviderRefusalError("refusal (category: cyber)")

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id=self.config.id, healthy=True)

    store = NebulaStore(tmp_path / "refusal-stream.db")
    engagement = store.create(Engagement(name="Refusal"))
    profile = store.create(
        ProviderProfile(
            id="provider-refusing",
            name="Refusing provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True},
        )
    )
    monkeypatch.setattr(
        chat_module, "provider_from_profile", lambda _: RefusingProvider(profile.id)
    )
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.post(
        "/api/v1/chat/completions",
        headers={"Authorization": "Bearer test-token"},
        json={
            "engagement_id": engagement.id,
            "provider_id": profile.id,
            "messages": [{"role": "user", "content": "Scan the host."}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    error = next(frame for frame in frames if frame.get("type") == "error")
    # "provider_final_answer_missing" makes the client hide the detail behind
    # "Finish the answer"; a refusal is shown as it is.
    assert error["code"] != "provider_final_answer_missing"
    assert error["detail"] == "provider blocked the response: refusal (category: cyber)"
