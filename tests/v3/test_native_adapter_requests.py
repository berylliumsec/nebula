"""Requests the native adapters send must be ones current vendors accept.

Covers the Anthropic Messages, Bedrock Converse, Gemini and OpenAI Responses
adapters: model-gated sampling and forced tool choice for current Claude
models, Gemini tool schemas, request timeouts, retryable vendor overloads,
Responses strict schemas and storage, and Converse role alternation.
"""

import asyncio
import json

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.api import _verify_provider_capability
from nebula.v3.domain import (
    ContextMemory,
    ProviderProfile,
    ProviderVerificationStatus,
)
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    ProviderOverloadedError,
    StreamEventType,
    ToolChoice,
    ToolDefinition,
    build_provider,
    provider_from_profile,
)
from nebula.v3.storage import NebulaStore

KEY_ENV = "NEBULA_NATIVE_ADAPTER_TEST_KEY"

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

ANTHROPIC_OK = {
    "id": "msg_1",
    "model": "m",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
RESPONSES_OK = {
    "id": "resp_1",
    "model": "m",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
GEMINI_OK = {
    "responseId": "g_1",
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
}


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    monkeypatch.delenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS", raising=False)


def _config(kind, flavor, model="test-model", **extra):
    return ProviderConfig(
        id=f"native-{flavor.value}",
        kind=kind,
        flavor=flavor,
        base_url="https://provider.invalid",
        default_model=model,
        api_key_env=None if kind == ProviderKind.BEDROCK else KEY_ENV,
        capabilities=ModelCapabilities(
            tools=True, strict_tools=True, structured_output=True
        ),
        # Zero backoff keeps retries under test without real waiting.
        options={"retry_backoff_seconds": 0, **extra.pop("options", {})},
        **extra,
    )


def _routing(model, **update):
    return ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="scan 10.0.0.1")],
        tools=[LOOKUP, FINISH],
        tool_choice=ToolChoice.REQUIRED,
        parallel_tool_calls=True,
        max_output_tokens=512,
        temperature=0,
    ).model_copy(update=update)


class _Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads: list[dict] = []
        self.timeouts: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        self.timeouts.append(request.extensions["timeout"])
        status, body = self.responses[min(len(self.payloads), len(self.responses)) - 1]
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _anthropic_payload(model, request):
    recorder = _Recorder((200, ANTHROPIC_OK))
    provider = AnthropicProvider(
        _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC, model),
        transport=recorder.transport,
    )
    asyncio.run(provider.complete(request))
    return recorder.payloads[0]


class _Bedrock:
    """Stands in for boto3: records clients built and Converse calls made."""

    def __init__(self, monkeypatch, *failures):
        self.clients: list[tuple[str, dict]] = []
        self.calls: list[dict] = []
        self.failures = list(failures)
        monkeypatch.setattr(providers.boto3, "client", self.client)

    def client(self, service, **kwargs):
        self.clients.append((service, kwargs))
        return self

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }


def _bedrock_error(code: str, status: int):
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} from Bedrock"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Converse",
    )


# --- NAT-2: sampling parameters on Claude models that reject them -----------


@pytest.mark.parametrize(
    ("model", "keeps_temperature"),
    [
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-opus-5", False),
        ("claude-sonnet-5", False),
        ("claude-fable-5", False),
        ("claude-fable-5-1", False),
        ("claude-mythos-5-1", False),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-6", True),
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-5-20250929", True),
    ],
)
def test_anthropic_omits_temperature_for_models_that_reject_sampling(
    model, keeps_temperature
):
    payload = _anthropic_payload(model, _routing(model))

    if keeps_temperature:
        assert payload["temperature"] == 0
    else:
        assert "temperature" not in payload
    assert payload["max_tokens"] == 512


@pytest.mark.parametrize(
    ("model", "keeps_temperature"),
    [
        ("anthropic.claude-sonnet-5", False),
        ("us.anthropic.claude-opus-5", False),
        ("global.anthropic.claude-opus-4-7", False),
        ("eu.anthropic.claude-fable-5-1", False),
        ("anthropic.claude-opus-4-8-v1:0", False),
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
            "us.anthropic.claude-sonnet-5",
            False,
        ),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", True),
        ("us.anthropic.claude-sonnet-4-6", True),
        ("amazon.nova-pro-v1:0", True),
    ],
)
def test_bedrock_omits_temperature_for_claude_models_that_reject_sampling(
    monkeypatch, model, keeps_temperature
):
    bedrock = _Bedrock(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    asyncio.run(provider.complete(_routing(model, tools=[], tool_choice="auto")))

    inference = bedrock.calls[0]["inferenceConfig"]
    assert inference["maxTokens"] == 512
    assert ("temperature" in inference) is keeps_temperature


# --- NAT-3: forced tool choice ---------------------------------------------


@pytest.mark.parametrize(
    ("model", "choice"),
    [
        ("claude-fable-5-1", "auto"),
        ("claude-mythos-5-1", "auto"),
        ("claude-mythos-preview", "auto"),
        ("claude-fable-5", "any"),
        ("claude-opus-5", "any"),
        ("claude-haiku-4-5", "any"),
    ],
)
def test_anthropic_forced_tool_choice_falls_back_to_auto_where_rejected(model, choice):
    payload = _anthropic_payload(model, _routing(model, parallel_tool_calls=False))

    assert payload["tool_choice"] == {
        "type": choice,
        "disable_parallel_tool_use": True,
    }


@pytest.mark.parametrize(
    ("model", "tool_choice", "thinking"),
    [
        # Forced tool choice on Bedrock requires thinking off where it is on
        # by default.
        ("anthropic.claude-sonnet-5", {"any": {}}, {"type": "disabled"}),
        ("us.anthropic.claude-opus-5", {"any": {}}, {"type": "disabled"}),
        # These models reject forced tool choice everywhere.
        ("anthropic.claude-mythos-5-1", {"auto": {}}, None),
        ("us.anthropic.claude-fable-5-1", {"auto": {}}, None),
        # Thinking is off by default here; nothing extra is sent.
        ("anthropic.claude-haiku-4-5-20251001-v1:0", {"any": {}}, None),
        ("anthropic.claude-opus-4-8", {"any": {}}, None),
        ("amazon.nova-pro-v1:0", {"any": {}}, None),
    ],
)
def test_bedrock_forced_tool_choice_is_model_gated(
    monkeypatch, model, tool_choice, thinking
):
    bedrock = _Bedrock(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    asyncio.run(provider.complete(_routing(model)))

    sent = bedrock.calls[0]
    assert sent["toolConfig"]["toolChoice"] == tool_choice
    extra = sent.get("additionalModelRequestFields") or {}
    assert extra.get("thinking") == thinking


def test_bedrock_auto_tool_choice_keeps_default_thinking(monkeypatch):
    bedrock = _Bedrock(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    asyncio.run(
        provider.complete(
            _routing("anthropic.claude-sonnet-5", tool_choice=ToolChoice.AUTO)
        )
    )

    assert "toolChoice" not in bedrock.calls[0]["toolConfig"]
    assert "additionalModelRequestFields" not in bedrock.calls[0]


@pytest.mark.parametrize(
    ("model", "status"),
    [
        ("claude-fable-5-1", ProviderVerificationStatus.VERIFIED),
        # A model that accepts forced tool choice must still answer with the
        # call alone.
        ("claude-opus-5", ProviderVerificationStatus.FAILED),
    ],
)
def test_capability_probe_verifies_models_that_only_accept_auto_tool_choice(
    tmp_path, model, status
):
    store = NebulaStore(tmp_path / "nebula.db")
    profile = store.create(
        ProviderProfile(
            name="Anthropic",
            provider_type="anthropic",
            secret_ref=f"env:{KEY_ENV}",
            model_allowlist=[model],
            metadata={"default_model": model},
        )
    )
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        tool = payload["tools"][0]
        nonce = tool["input_schema"]["properties"]["nonce"]["enum"][0]
        return httpx.Response(
            200,
            json={
                "id": "msg_probe",
                "model": model,
                "content": [
                    {"type": "text", "text": "Making the verification call."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": tool["name"],
                        "input": {"nonce": nonce},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def factory(candidate):
        return build_provider(
            provider_from_profile(candidate).config,
            transport=httpx.MockTransport(handler),
        )

    result = asyncio.run(_verify_provider_capability(store, profile, model, factory))

    assert result.verification.status == status, result.verification.failure_detail
    assert "temperature" not in sent[0]
    expected_choice = "auto" if status == ProviderVerificationStatus.VERIFIED else "any"
    assert sent[0]["tool_choice"]["type"] == expected_choice


# --- NAT-4: Gemini tool schemas --------------------------------------------


def test_gemini_declares_tools_with_json_schema():
    cwd_tool = ToolDefinition(
        name="run_command",
        description="Run a command in the workspace",
        input_schema={
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "const": "."},
                "command": {"type": "string"},
            },
            "required": ["cwd", "command"],
            "additionalProperties": False,
        },
    )
    recorder = _Recorder((200, GEMINI_OK))
    provider = GeminiProvider(
        _config(ProviderKind.GEMINI, ProviderFlavor.GEMINI, "gemini-2.5-pro"),
        transport=recorder.transport,
    )

    asyncio.run(provider.complete(_routing("gemini-2.5-pro", tools=[cwd_tool, FINISH])))

    declarations = recorder.payloads[0]["tools"][0]["functionDeclarations"]
    assert [item["name"] for item in declarations] == ["run_command", "finish_response"]
    for declaration, tool in zip(declarations, [cwd_tool, FINISH]):
        assert "parameters" not in declaration
        assert declaration["parametersJsonSchema"] == tool.input_schema


# --- NAT-6 / NAT-7: request timeouts ---------------------------------------


def _profile(provider_type, **options):
    return ProviderProfile(
        name=provider_type,
        provider_type=provider_type,
        secret_ref=None if provider_type == "bedrock" else f"env:{KEY_ENV}",
        model_allowlist=["m"],
        metadata={"default_model": "m", "options": options},
    )


@pytest.mark.parametrize(
    ("provider_type", "body"),
    [
        ("anthropic", ANTHROPIC_OK),
        ("openai", RESPONSES_OK),
        ("gemini", GEMINI_OK),
    ],
)
def test_native_adapters_honour_the_profile_request_timeout(
    monkeypatch, provider_type, body
):
    request = ModelRequest(messages=[ModelMessage(role="user", content="hello")])

    def timeout_for(profile):
        recorder = _Recorder((200, body))
        provider = build_provider(
            provider_from_profile(profile).config, transport=recorder.transport
        )
        asyncio.run(provider.complete(request))
        return recorder.timeouts[0]

    configured = timeout_for(_profile(provider_type, request_timeout_seconds=1800))
    assert configured["read"] == 1800
    assert configured["connect"] == 10

    # A whole non-streamed generation gets far longer than the 120 s default.
    default = timeout_for(_profile(provider_type))
    assert default["read"] == 600
    assert default["connect"] == 10

    monkeypatch.setenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS", "900")
    assert timeout_for(_profile(provider_type))["read"] == 900


def test_bedrock_client_uses_profile_timeout_and_no_socket_replay(monkeypatch):
    bedrock = _Bedrock(monkeypatch)
    request = ModelRequest(messages=[ModelMessage(role="user", content="hello")])

    asyncio.run(
        provider_from_profile(
            _profile("bedrock", region="us-east-1", request_timeout_seconds=900)
        ).complete(request)
    )
    asyncio.run(
        provider_from_profile(_profile("bedrock", region="us-east-1")).complete(request)
    )

    configured, default = (kwargs for _service, kwargs in bedrock.clients)
    assert bedrock.clients[0][0] == "bedrock-runtime"
    assert configured["region_name"] == "us-east-1"
    assert configured["config"].read_timeout == 900
    assert configured["config"].connect_timeout == 10
    # botocore must not re-send a whole generation on its own; Nebula decides.
    assert configured["config"].retries == {"mode": "standard", "max_attempts": 1}
    assert default["config"].read_timeout == 600


# --- NAT-8: Anthropic 529 overloaded_error ---------------------------------

OVERLOADED = (
    529,
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
)


def test_anthropic_529_overloaded_is_retried():
    recorder = _Recorder(OVERLOADED, (200, ANTHROPIC_OK))
    provider = AnthropicProvider(
        _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC),
        transport=recorder.transport,
    )

    response = asyncio.run(provider.complete(_routing("claude-haiku-4-5")))

    assert response.text == "ok"
    assert len(recorder.payloads) == 2

    exhausted = _Recorder(OVERLOADED)
    provider = AnthropicProvider(
        _config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC),
        transport=exhausted.transport,
    )
    with pytest.raises(ProviderOverloadedError, match="after 3 attempts"):
        asyncio.run(provider.complete(_routing("claude-haiku-4-5")))

    async def collect():
        return [event async for event in provider.stream(_routing("claude-haiku-4-5"))]

    error = [e for e in asyncio.run(collect()) if e.type == StreamEventType.ERROR]
    assert error[0].retryable is True


# --- NAT-11 and storage: OpenAI Responses ----------------------------------


def _responses_payload(request):
    recorder = _Recorder((200, RESPONSES_OK))
    provider = OpenAIResponsesProvider(
        _config(ProviderKind.OPENAI_RESPONSES, ProviderFlavor.OPENAI, "gpt-5"),
        transport=recorder.transport,
    )
    asyncio.run(provider.complete(request))
    return recorder.payloads[0]


def test_responses_response_schema_strict_only_when_acceptable():
    compaction = ModelRequest(
        messages=[ModelMessage(role="user", content="compact")],
        response_schema=ContextMemory.model_json_schema(),
    )
    strict_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    loose = _responses_payload(compaction)["text"]["format"]
    strict = _responses_payload(
        compaction.model_copy(update={"response_schema": strict_schema})
    )["text"]["format"]

    # ContextMemory has optional fields and defaults strict mode rejects.
    assert loose["strict"] is False
    assert loose["schema"] == ContextMemory.model_json_schema()
    assert strict["strict"] is True


def test_responses_requests_are_not_stored():
    payload = _responses_payload(
        ModelRequest(messages=[ModelMessage(role="user", content="hello")])
    )

    assert payload["store"] is False


# --- NAT-15: Converse role alternation -------------------------------------


def test_bedrock_merges_consecutive_same_role_messages(monkeypatch):
    bedrock = _Bedrock(monkeypatch)
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))
    request = ModelRequest(
        messages=[
            ModelMessage(role="user", content="an earlier turn that failed"),
            ModelMessage(role="user", content="scan 10.0.0.1"),
            ModelMessage(
                role="user",
                content=[{"type": "text", "text": "Screenshot captured."}],
            ),
            ModelMessage(role="assistant", content="Looking it up."),
        ],
        tools=[LOOKUP],
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="lookup_asset",
                arguments={"address": "10.0.0.1"},
                output={"open": [22]},
            )
        ],
    )

    asyncio.run(provider.complete(request))

    messages = bedrock.calls[0]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == [
        {"text": "an earlier turn that failed"},
        {"text": "scan 10.0.0.1"},
        {"text": "Screenshot captured."},
    ]
    assert messages[1]["content"][0] == {"text": "Looking it up."}
    assert messages[1]["content"][1]["toolUse"]["toolUseId"] == "call-1"
    assert messages[2]["content"][0]["toolResult"]["toolUseId"] == "call-1"
    # The caller's request is not mutated by the merge.
    assert request.messages[0].content == "an earlier turn that failed"


# --- NAT-16: Bedrock throttling and unavailability -------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("ThrottlingException", 429),
        ("ServiceUnavailableException", 503),
        ("ModelNotReadyException", 429),
        ("InternalServerException", 500),
        ("ModelTimeoutException", 408),
    ],
)
def test_bedrock_throttling_and_unavailability_are_retryable_overloads(
    monkeypatch, code, status
):
    request = ModelRequest(messages=[ModelMessage(role="user", content="hello")])
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    recovered = _Bedrock(monkeypatch, _bedrock_error(code, status))
    assert asyncio.run(provider.complete(request)).text == "ok"
    assert len(recovered.calls) == 2

    exhausted = _Bedrock(monkeypatch, *[_bedrock_error(code, status)] * 3)
    with pytest.raises(ProviderOverloadedError, match=code) as failure:
        asyncio.run(provider.complete(request))
    assert len(exhausted.calls) == 3
    assert failure.value.status_code == status

    _Bedrock(monkeypatch, *[_bedrock_error(code, status)] * 3)

    async def collect():
        return [event async for event in provider.stream(request)]

    error = [e for e in asyncio.run(collect()) if e.type == StreamEventType.ERROR]
    assert error[0].retryable is True


def test_bedrock_request_defects_are_not_retried(monkeypatch):
    bedrock = _Bedrock(monkeypatch, _bedrock_error("ValidationException", 400))
    provider = BedrockProvider(_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK))

    with pytest.raises(ProviderError) as failure:
        asyncio.run(
            provider.complete(
                ModelRequest(messages=[ModelMessage(role="user", content="hello")])
            )
        )

    assert not isinstance(failure.value, ProviderOverloadedError)
    assert len(bedrock.calls) == 1
