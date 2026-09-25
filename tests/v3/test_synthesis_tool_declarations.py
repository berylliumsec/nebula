"""Final synthesis keeps its tools declared and turns calling off.

Chat's closing synthesis replays every tool call and result of the turn. It
used to send them with no function declarations at all, so models called
functions the request never declared, open-weight routes printed their native
call markup as the answer, and direct Anthropic and Bedrock rejected the
request outright: both require the tools a replayed ``tool_use`` / ``toolUse``
block names to be declared. Every tool turn on those profiles failed at its
last step, after the tool work had run.

Synthesis now declares the same functions its routing steps did, with
``tool_choice`` none, as LiteLLM (a dummy tool), pi-mono (``tools: []``) and
opencode (a ``_noop`` tool; tools kept on its last step) all keep a
declaration. An adapter that receives tool history with no tools from any
other caller declares the replayed names itself.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.domain import ChatTurn, ChatTurnStatus, ProviderProfile
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolCall,
    ToolDefinition,
)
from tests.v3.test_chat_tool_loop import (
    RecordingBroker,
    _prepared,
    _response,
)

ANSWER = "The stored value is a."
# ToolChoice.NONE, spelled as its wire value so this module still collects
# where the member does not exist.
NONE = "none"
CAPABILITIES = ModelCapabilities(streaming=True, tools=True, strict_tools=True)

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
FINISH = ToolDefinition(
    name="finish_response",
    description="Finish tool routing and produce the final analyst response.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
HISTORY = [
    ModelToolResult(
        call_id="call-1",
        name="lookup_asset",
        arguments={"address": "10.0.0.1"},
        output={"status": "complete", "open_ports": [22]},
    ),
    # A dotted Nebula name travels under a wire name every vendor accepts.
    ModelToolResult(
        call_id="call-2",
        name="tool_output.search",
        arguments={"query": "ssh"},
        output="no further matches",
    ),
]


def _synthesis(model: str = "test-model", **update: Any) -> ModelRequest:
    """The request chat sends to close a tool turn."""

    return ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        tools=[LOOKUP, FINISH],
        tool_choice=NONE,
        tool_results=HISTORY,
        max_output_tokens=512,
    ).model_copy(update=update)


def _tool_free_history(model: str = "test-model") -> ModelRequest:
    """Tool history with no declarations, as a caller other than chat may send."""

    return ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        tool_results=HISTORY,
        max_output_tokens=512,
    )


def _config(kind: ProviderKind, flavor: ProviderFlavor, **extra: Any):
    return ProviderConfig(
        id=f"native-{flavor.value}",
        kind=kind,
        flavor=flavor,
        base_url="https://provider.invalid",
        default_model="test-model",
        api_key_value=None if kind == ProviderKind.BEDROCK else "not-a-real-key",
        capabilities=CAPABILITIES,
        options={"retry_backoff_seconds": 0, "region": "us-east-1"},
        **extra,
    )


def _http(provider_type, kind, flavor, *bodies: dict[str, Any]):
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=bodies[min(len(sent), len(bodies)) - 1])

    provider = provider_type(
        _config(kind, flavor), transport=httpx.MockTransport(handler)
    )
    return provider, sent


class _Converse:
    """Stands in for boto3's bedrock-runtime client."""

    def __init__(self, monkeypatch, *replies: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies = list(replies) or [_converse_text("ok")]
        monkeypatch.setattr(providers.boto3, "client", lambda *a, **k: self)

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


def _converse_text(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 5, "outputTokens": 1, "totalTokens": 6},
    }


_ANTHROPIC_OK = {
    "id": "msg_1",
    "model": "test-model",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


# --- Each adapter declares the tools and turns calling off -------------------


def test_compatible_synthesis_declares_tools_with_tool_choice_none():
    provider = OpenAICompatibleProvider(
        _config(ProviderKind.OPENAI_COMPATIBLE, ProviderFlavor.CUSTOM)
    )

    payload = provider._payload(_synthesis(), "test-model")

    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "lookup_asset",
        "finish_response",
    ]
    assert payload["tool_choice"] == "none"
    replayed = [
        call["function"]["name"]
        for message in payload["messages"]
        for call in message.get("tool_calls") or ()
    ]
    # Every function the history calls is one the request declares, or its
    # wire spelling of a Nebula name.
    assert replayed == ["lookup_asset", "tool_output_search"]


@pytest.mark.parametrize(
    ("advertised", "tool_choice"),
    [
        (["tools", "tool_choice", "max_tokens"], "none"),
        # #484: with require_parameters an unadvertised control leaves no
        # eligible endpoint, so it is dropped; the declarations stay.
        (["tools", "max_tokens"], None),
    ],
)
def test_openrouter_synthesis_requires_tool_routes_and_prunes_only_unadvertised_none(
    advertised, tool_choice
):
    provider = OpenAICompatibleProvider(
        _config(
            ProviderKind.OPENAI_COMPATIBLE,
            ProviderFlavor.OPENROUTER,
            model_parameters={"vendor/model": advertised},
        )
    )

    payload = provider._payload(_synthesis("vendor/model"), "vendor/model")

    # The synthesis lands on an endpoint that serves tools, like every
    # routing step of the turn, not on any upstream of the model.
    assert payload["provider"] == {"require_parameters": True}
    assert len(payload["tools"]) == 2
    assert payload.get("tool_choice") == tool_choice


def test_responses_synthesis_declares_tools_with_tool_choice_none():
    provider = OpenAIResponsesProvider(
        _config(ProviderKind.OPENAI_RESPONSES, ProviderFlavor.OPENAI)
    )

    payload = provider._payload(_synthesis(), "test-model")

    assert [tool["name"] for tool in payload["tools"]] == [
        "lookup_asset",
        "finish_response",
    ]
    assert payload["tool_choice"] == "none"


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5",
        "claude-haiku-4-5",
        # #486 sends these auto instead of a forced choice; none is not forced
        # and stays none.
        "claude-fable-5-1",
        "claude-mythos-preview",
    ],
)
def test_anthropic_synthesis_declares_tools_with_tool_choice_none(model):
    provider, sent = _http(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        ProviderFlavor.ANTHROPIC,
        _ANTHROPIC_OK,
    )

    asyncio.run(provider.complete(_synthesis(model)))

    payload = sent[0]
    assert [tool["name"] for tool in payload["tools"]] == [
        "lookup_asset",
        "finish_response",
    ]
    assert payload["tool_choice"] == {"type": "none"}


def test_gemini_synthesis_declares_tools_with_calling_mode_none():
    provider, sent = _http(
        GeminiProvider,
        ProviderKind.GEMINI,
        ProviderFlavor.GEMINI,
        {
            "responseId": "g_1",
            "candidates": [
                {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
        },
    )

    asyncio.run(provider.complete(_synthesis()))

    payload = sent[0]
    declared = payload["tools"][0]["functionDeclarations"]
    assert [item["name"] for item in declared] == ["lookup_asset", "finish_response"]
    assert payload["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}


@pytest.mark.parametrize(
    "model",
    [
        "anthropic.claude-sonnet-5",
        "us.anthropic.claude-fable-5-1",
        "amazon.nova-pro-v1:0",
    ],
)
def test_bedrock_synthesis_declares_tools_without_a_tool_choice(monkeypatch, model):
    """Converse has no none choice; its default, auto, is the closest."""

    bedrock = _Converse(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    asyncio.run(provider.complete(_synthesis(model)))

    sent = bedrock.calls[0]
    assert [tool["toolSpec"]["name"] for tool in sent["toolConfig"]["tools"]] == [
        "lookup_asset",
        "finish_response",
    ]
    assert "toolChoice" not in sent["toolConfig"]
    # Thinking is disabled only beside a forced choice.
    assert "additionalModelRequestFields" not in sent


# --- The adapter backstop for tool history without tools ---------------------


def test_anthropic_declares_replayed_tools_when_history_arrives_without_tools():
    provider, sent = _http(
        AnthropicProvider,
        ProviderKind.ANTHROPIC,
        ProviderFlavor.ANTHROPIC,
        _ANTHROPIC_OK,
    )

    asyncio.run(provider.complete(_tool_free_history()))

    payload = sent[0]
    blocks = {
        block["type"]
        for message in payload["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    }
    assert {"tool_use", "tool_result"} <= blocks
    assert [tool["name"] for tool in payload["tools"]] == [
        "lookup_asset",
        "tool_output_search",
    ]
    assert all(tool["input_schema"] == {"type": "object"} for tool in payload["tools"])
    assert payload["tool_choice"] == {"type": "none"}

    # A request with no tool history is left as it was.
    plain = _tool_free_history().model_copy(update={"tool_results": []})
    asyncio.run(provider.complete(plain))
    assert "tools" not in sent[1] and "tool_choice" not in sent[1]


def test_bedrock_declares_replayed_tools_when_history_arrives_without_tools(
    monkeypatch,
):
    bedrock = _Converse(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    asyncio.run(provider.complete(_tool_free_history("anthropic.claude-sonnet-5")))

    sent = bedrock.calls[0]
    assert any(
        "toolUse" in block
        for message in sent["messages"]
        for block in message["content"]
    )
    tools = sent["toolConfig"]["tools"]
    assert [tool["toolSpec"]["name"] for tool in tools] == [
        "lookup_asset",
        "tool_output_search",
    ]
    assert all(
        tool["toolSpec"]["inputSchema"] == {"json": {"type": "object"}}
        for tool in tools
    )
    assert "toolChoice" not in sent["toolConfig"]

    plain = _tool_free_history().model_copy(update={"tool_results": []})
    asyncio.run(provider.complete(plain))
    assert "toolConfig" not in bedrock.calls[1]


# --- A call attempted under tool choice none reaches recovery ----------------


@pytest.mark.parametrize("reason", ["UNEXPECTED_TOOL_CALL", "MALFORMED_FUNCTION_CALL"])
def test_gemini_call_attempt_under_tool_choice_none_is_an_empty_reply(reason):
    """Gemini reports a call made with calling off as UNEXPECTED_TOOL_CALL.

    The request allowed no call, so this is the caller's final-answer
    recovery to handle, as it was when the request declared no tools; it is
    not a botched routing call to repeat.
    """

    provider, _ = _http(
        GeminiProvider,
        ProviderKind.GEMINI,
        ProviderFlavor.GEMINI,
        {
            "responseId": "g_2",
            "candidates": [{"finishReason": reason, "content": {"parts": []}}],
        },
    )

    response = asyncio.run(provider.complete(_synthesis()))

    assert response.text == ""
    assert response.tool_calls == []
    assert response.finish_reason == reason


def test_bedrock_malformed_output_under_tool_choice_none_is_an_empty_reply(
    monkeypatch,
):
    _Converse(
        monkeypatch,
        {
            "output": {"message": {"role": "assistant", "content": []}},
            "stopReason": "malformed_tool_use",
            "usage": {"inputTokens": 5, "outputTokens": 0, "totalTokens": 5},
        },
    )
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    response = asyncio.run(provider.complete(_synthesis()))

    assert response.text == ""
    assert response.finish_reason == "malformed_tool_use"


# --- What chat sends -----------------------------------------------------------


def _call(call_id: str, value: str) -> ToolCall:
    return ToolCall(id=call_id, name="safe_read", arguments={"value": value})


def _finish(call_id: str = "finish-1") -> ToolCall:
    return ToolCall(id=call_id, name="finish_response", arguments={})


def _turn_requests(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


def _dumped(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [tool.model_dump(mode="json") for tool in tools]


def test_synthesis_and_its_recovery_declare_the_routing_tools_with_calling_off(
    tmp_path,
):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(calls=[_call("call-1", "a")]),
            _response(calls=[_finish()]),
            # No answer: the turn asks once more.
            _response(text=""),
            _response(text=ANSWER),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    routing, _, synthesis, recovery = _turn_requests(provider)
    # Routing lets the model choose since #521, which also dropped the
    # finish_response tool (an older route may still call it).
    assert routing.tool_choice == "auto"
    # The same definitions, byte for byte and in the same order, so the
    # function list a provider caches for routing serves the synthesis too.
    assert _dumped(synthesis.tools) == _dumped(routing.tools)
    assert [tool.name for tool in synthesis.tools] == ["safe_read"]
    assert synthesis.tool_choice == NONE
    assert synthesis.parallel_tool_calls is False
    assert [result.call_id for result in synthesis.tool_results] == ["call-1"]
    assert recovery.metadata["final_answer_recovery"] == "missing_answer"
    assert _dumped(recovery.tools) == _dumped(routing.tools)
    assert recovery.tool_choice == NONE
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_synthesis_declares_the_full_routing_list_after_the_budget_is_spent(
    tmp_path,
):
    """Budgets trim later routing steps; calling is off at synthesis anyway."""

    broker = RecordingBroker()
    _, service, prepared, provider = _prepared(
        tmp_path,
        [_response(calls=[_call("call-1", "a")]), _response(text=ANSWER)],
        broker,
        max_tool_calls=1,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    routing, synthesis = _turn_requests(provider)
    assert _dumped(synthesis.tools) == _dumped(routing.tools)
    assert synthesis.tool_choice == NONE


def test_output_limit_recovery_is_sized_from_the_routes_that_serve_tools(tmp_path):
    """The recovery request declares tools, so only tool routes can serve it."""

    _, service, prepared, _ = _prepared(tmp_path, [], RecordingBroker())
    prepared.provider_profile = ProviderProfile(
        id="provider",
        name="OpenRouter",
        provider_type="openrouter",
        endpoint="https://openrouter.ai/api/v1",
        metadata={
            "model_descriptors": [
                {
                    "id": "model-a",
                    "context_window": 128_000,
                    "max_output_tokens": 32_000,
                    "route_limits_verified": True,
                    "route_limits": [
                        {
                            "provider_name": "tools",
                            "context_window": 128_000,
                            "max_input_tokens": 128_000,
                            "max_output_tokens": 16_000,
                            "supported_parameters": ["tools", "tool_choice"],
                            "status": 0,
                        },
                        {
                            # Serves text only: never chosen for a request
                            # that declares tools under require_parameters.
                            "provider_name": "text-only",
                            "context_window": 128_000,
                            "max_input_tokens": 128_000,
                            "max_output_tokens": 1_000,
                            "supported_parameters": [],
                            "status": 0,
                        },
                    ],
                }
            ]
        },
    )
    synthesis = ModelRequest(
        model="model-a",
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        tools=[FINISH],
        tool_results=HISTORY,
        max_output_tokens=2_048,
    )

    retry = service._final_answer_recovery_request(prepared, synthesis, "output_limit")

    assert retry.tools == synthesis.tools
    # Twice the budget, bounded by the 16,000-token tool route; the text-only
    # route's 1,000 does not apply to a request it can never serve.
    assert retry.max_output_tokens == 4_096


# --- A tool turn on the vendors that require the declarations ---------------


_VENDOR_RULE = (
    "Requests which include `tool_use` or `tool_result` blocks must define tools."
)


def test_anthropic_tool_turn_completes_where_tool_blocks_require_tools(tmp_path):
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        blocks = any(
            isinstance(message["content"], list)
            and any(
                block.get("type") in {"tool_use", "tool_result"}
                for block in message["content"]
            )
            for message in body["messages"]
        )
        if blocks and not body.get("tools"):
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": _VENDOR_RULE},
                },
            )
        step = len(sent)
        if step == 1:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "safe_read",
                    "input": {"value": "a"},
                }
            ]
        elif step == 2:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "finish_response",
                    "input": {},
                }
            ]
        else:
            content = [{"type": "text", "text": ANSWER}]
        return httpx.Response(
            200,
            json={
                "id": f"msg_{step}",
                "model": "model-a",
                "content": content,
                "stop_reason": "tool_use" if step < 3 else "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = AnthropicProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.ANTHROPIC,
            flavor=ProviderFlavor.ANTHROPIC,
            base_url="https://api.anthropic.invalid",
            default_model="model-a",
            model_allowlist=["model-a"],
            api_key_value="not-a-real-key",
            capabilities=CAPABILITIES,
        ),
        transport=httpx.MockTransport(handler),
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    assert len(sent) == 3
    synthesis = sent[-1]
    assert synthesis["tools"] == sent[0]["tools"]
    assert synthesis["tool_choice"] == {"type": "none"}


def test_bedrock_tool_turn_completes_where_tool_blocks_require_a_tool_config(
    monkeypatch, tmp_path
):
    calls: list[dict[str, Any]] = []

    class Converse:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            blocks = any(
                "toolUse" in block or "toolResult" in block
                for message in kwargs["messages"]
                for block in message["content"]
            )
            if blocks and "toolConfig" not in kwargs:
                from botocore.exceptions import ClientError

                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": "The toolConfig field must be defined when "
                            "using toolUse and toolResult content blocks.",
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 400},
                    },
                    "Converse",
                )
            step = len(calls)
            if step == 1:
                content = [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_1",
                            "name": "safe_read",
                            "input": {"value": "a"},
                        }
                    }
                ]
            elif step == 2:
                content = [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_2",
                            "name": "finish_response",
                            "input": {},
                        }
                    }
                ]
            else:
                return _converse_text(ANSWER)
            return {
                "output": {"message": {"role": "assistant", "content": content}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
            }

    monkeypatch.setattr(providers.boto3, "client", lambda *a, **k: Converse())
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = BedrockProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.BEDROCK,
            flavor=ProviderFlavor.BEDROCK,
            base_url="https://bedrock.invalid",
            default_model="model-a",
            model_allowlist=["model-a"],
            capabilities=CAPABILITIES,
            options={"region": "us-east-1", "retry_backoff_seconds": 0},
        )
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    assert len(calls) == 3
    synthesis = calls[-1]
    assert synthesis["toolConfig"]["tools"] == calls[0]["toolConfig"]["tools"]
    assert "toolChoice" not in synthesis["toolConfig"]


def test_gemini_tool_turn_recovers_a_call_made_with_calling_off(tmp_path):
    """UNEXPECTED_TOOL_CALL at synthesis is asked again, not a failed turn."""

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        step = len(sent)
        if step == 1:
            candidate = {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc-1",
                                "name": "safe_read",
                                "args": {"value": "a"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        elif step == 2:
            candidate = {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc-2",
                                "name": "finish_response",
                                "args": {},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        elif step == 3:
            candidate = {
                "content": {"parts": []},
                "finishReason": "UNEXPECTED_TOOL_CALL",
            }
        else:
            candidate = {
                "content": {"role": "model", "parts": [{"text": ANSWER}]},
                "finishReason": "STOP",
            }
        return httpx.Response(
            200, json={"responseId": f"resp-{step}", "candidates": [candidate]}
        )

    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = GeminiProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.GEMINI,
            flavor=ProviderFlavor.GEMINI,
            base_url="https://generativelanguage.invalid",
            default_model="model-a",
            model_allowlist=["model-a"],
            api_key_value="not-a-real-key",
            capabilities=CAPABILITIES,
        ),
        transport=httpx.MockTransport(handler),
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert len(sent) == 4
    for synthesis in sent[2:]:
        assert synthesis["tools"] == sent[0]["tools"]
        assert synthesis["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.request_snapshot["final_answer_recovery"]["reason"] == "missing_answer"
