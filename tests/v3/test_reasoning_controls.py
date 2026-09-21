"""Reasoning effort reaches every provider family in the shape it takes.

The operator's Effort level, a subagent's level and Core's own ``none`` (final
answer recovery, the capability probe, naming, compaction, retrieval planning
and scope import) all travel as ``ModelRequest.reasoning_effort``. Only
OpenRouter's ``reasoning`` object and OpenAI's o-series/gpt-5 names used to
receive it; everywhere else it was dropped, so thinking could be neither
turned down nor off. Each case below names a route, a model and a level and
the controls that route is sent:

- DeepSeek: ``thinking.type`` plus ``reasoning_effort`` low|high|max (Vercel
  ``packages/deepseek``, LiteLLM, pi-mono ``openai-completions.ts``).
- Z.ai/BigModel: ``thinking`` with ``clear_thinking: false`` (opencode,
  pi-mono), plus ``reasoning_effort`` high|max on GLM-5.2 and later.
- vLLM/SGLang chat templates: ``chat_template_kwargs`` ``enable_thinking``
  (GLM, Qwen3) or ``thinking`` (DeepSeek V3.1+); Ollama: ``reasoning_effort``.
- Anthropic and Bedrock Claude: ``output_config.effort`` and adaptive or
  disabled ``thinking`` per model (never ``budget_tokens`` on 4.6+; Fable and
  Mythos always think).
- Gemini: ``thinkingConfig`` ``thinkingLevel`` (3.x) or ``thinkingBudget``
  (2.5) with ``includeThoughts``; thought parts are reasoning, not answer.
- OpenAI Responses: ``reasoning.effort`` with ``summary: auto``, and the
  summaries read back as reasoning.
- OpenRouter: a model whose reasoning is mandatory is not asked for ``none``,
  and Fable 5.1 is sent ``tool_choice: auto`` instead of a forced choice.
"""

import asyncio
import json

import httpx
import pytest

from nebula.v3 import providers
from nebula.v3.domain import ProviderProfile
from nebula.v3.model_catalog import openrouter_models
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
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    ToolChoice,
    ToolDefinition,
    build_provider,
    provider_from_profile,
)

KEY_ENV = "NEBULA_REASONING_CONTROLS_TEST_KEY"
CHAT_CONTROLS = ("reasoning", "reasoning_effort", "thinking", "chat_template_kwargs")
CLAUDE_CONTROLS = ("thinking", "output_config")
ADAPTIVE = {"type": "adaptive", "display": "summarized"}
ENABLED = {"type": "enabled"}
DISABLED = {"type": "disabled"}
PRESERVED = {"type": "enabled", "clear_thinking": False}

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
CHAT_OK = {
    "id": "gen-1",
    "model": "m",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"},
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    monkeypatch.delenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS", raising=False)


def _ask(model: str, effort, **update) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Summarize the scan.")],
        reasoning_effort=effort,
        max_output_tokens=16_000,
    ).model_copy(update=update)


def _routing(model: str, effort, **update) -> ModelRequest:
    return _ask(
        model,
        effort,
        tools=[LOOKUP],
        tool_choice=ToolChoice.REQUIRED,
        **update,
    )


def _pick(payload: dict, keys: tuple[str, ...]) -> dict:
    return {key: payload[key] for key in keys if key in payload}


class _Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        status, body = self.responses[min(len(self.payloads), len(self.responses)) - 1]
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- Chat Completions routes ---------------------------------------------------

DEEPSEEK = (ProviderFlavor.DEEPSEEK, "https://api.deepseek.com", False)
DEEPSEEK_HOST = (ProviderFlavor.CUSTOM, "https://api.deepseek.com/v1", False)
ZAI = (ProviderFlavor.CUSTOM, "https://api.z.ai/api/paas/v4", False)
ZAI_CODING = (ProviderFlavor.CUSTOM, "https://api.z.ai/api/coding/paas/v4", False)
BIGMODEL = (ProviderFlavor.CUSTOM, "https://open.bigmodel.cn/api/paas/v4", False)
VLLM = (ProviderFlavor.VLLM, "http://127.0.0.1:8000/v1", True)
SGLANG = (ProviderFlavor.SGLANG, "http://127.0.0.1:30000/v1", True)
OLLAMA = (ProviderFlavor.OLLAMA, "http://127.0.0.1:11434/v1", True)


def _compatible(route, model: str, *, transport=None, **extra):
    flavor, base_url, local = route
    provider = build_provider(
        ProviderConfig(
            id="compatible",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=flavor,
            base_url=base_url,
            default_model=model,
            model_allowlist=[model],
            api_key_env=KEY_ENV,
            local=local,
            options={"retry_backoff_seconds": 0},
            **extra,
        ),
        transport=transport,
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    return provider


@pytest.mark.parametrize(
    ("route", "model", "effort", "expected"),
    [
        # DeepSeek: none switches thinking off and sends no level.
        (DEEPSEEK, "deepseek-v4-flash", "none", {"thinking": DISABLED}),
        (
            DEEPSEEK,
            "deepseek-v4-pro",
            "minimal",
            {"thinking": ENABLED, "reasoning_effort": "low"},
        ),
        (
            DEEPSEEK,
            "deepseek-v4-pro",
            "low",
            {"thinking": ENABLED, "reasoning_effort": "low"},
        ),
        (
            DEEPSEEK,
            "deepseek-v4-pro",
            "medium",
            {"thinking": ENABLED, "reasoning_effort": "high"},
        ),
        (
            DEEPSEEK,
            "deepseek-v4-flash",
            "high",
            {"thinking": ENABLED, "reasoning_effort": "high"},
        ),
        (
            DEEPSEEK,
            "deepseek-chat",
            "xhigh",
            {"thinking": ENABLED, "reasoning_effort": "max"},
        ),
        # A Custom profile on DeepSeek's host speaks the same dialect.
        (DEEPSEEK_HOST, "deepseek-v4-flash", "none", {"thinking": DISABLED}),
        # Z.ai and BigModel: GLM 4.5+ thinking, kept across steps; a level
        # only on GLM-5.2 and later, which take high and max.
        (ZAI, "glm-5.1", "none", {"thinking": DISABLED}),
        (ZAI, "glm-5.1", "high", {"thinking": PRESERVED}),
        (ZAI, "glm-5.2", "high", {"thinking": PRESERVED, "reasoning_effort": "high"}),
        (
            ZAI_CODING,
            "glm-5.3",
            "xhigh",
            {"thinking": PRESERVED, "reasoning_effort": "max"},
        ),
        (ZAI, "glm-5.3", "low", {"thinking": PRESERVED}),
        (BIGMODEL, "glm-4.6", "medium", {"thinking": PRESERVED}),
        (ZAI, "glm-4-plus", "high", {}),
        # Self-hosted chat templates take a boolean.
        (
            VLLM,
            "zai-org/GLM-5.1-FP8",
            "none",
            {"chat_template_kwargs": {"enable_thinking": False}},
        ),
        (
            VLLM,
            "zai-org/GLM-4.5-Air",
            "high",
            {"chat_template_kwargs": {"enable_thinking": True}},
        ),
        (
            VLLM,
            "deepseek-ai/DeepSeek-V3.1",
            "high",
            {"chat_template_kwargs": {"thinking": True}},
        ),
        (
            SGLANG,
            "deepseek-ai/DeepSeek-V4-Flash",
            "none",
            {"chat_template_kwargs": {"thinking": False}},
        ),
        (
            SGLANG,
            "Qwen/Qwen3-32B",
            "low",
            {"chat_template_kwargs": {"enable_thinking": True}},
        ),
        # Templates with no thinking switch are left alone.
        (VLLM, "deepseek-ai/DeepSeek-V3-0324", "none", {}),
        (VLLM, "meta-llama/Llama-3.3-70B-Instruct", "high", {}),
        # Ollama's OpenAI endpoint takes reasoning_effort for thinking models.
        (OLLAMA, "glm-4.7-flash", "none", {"reasoning_effort": "none"}),
        (OLLAMA, "qwen3:8b", "medium", {"reasoning_effort": "medium"}),
        (OLLAMA, "deepseek-v3.1:671b", "xhigh", {"reasoning_effort": "max"}),
        (OLLAMA, "llama3.2", "high", {}),
    ],
)
def test_chat_completions_routes_translate_reasoning_effort(
    route, model, effort, expected
):
    provider = _compatible(route, model)

    payload = provider._payload(_ask(model, effort), model)

    assert _pick(payload, CHAT_CONTROLS) == expected


@pytest.mark.parametrize(
    ("route", "model"),
    [
        (DEEPSEEK, "deepseek-v4-flash"),
        (ZAI, "glm-5.3"),
        (VLLM, "zai-org/GLM-5.1-FP8"),
        (SGLANG, "deepseek-ai/DeepSeek-V4-Flash"),
        (OLLAMA, "qwen3:8b"),
    ],
)
def test_an_unset_level_leaves_the_model_default(route, model):
    provider = _compatible(route, model)

    payload = provider._payload(_ask(model, None), model)

    assert _pick(payload, CHAT_CONTROLS) == {}


def test_zai_steps_get_their_reasoning_back_once_it_is_kept():
    """clear_thinking:false is what makes Z.ai use replayed reasoning (#502)."""

    model = "glm-5.3"
    provider = _compatible(ZAI, model)
    request = _ask(
        model,
        "high",
        tools=[LOOKUP],
        tool_results=[
            ModelToolResult(
                call_id="call-1",
                name="lookup_asset",
                arguments={"address": "10.0.0.1"},
                output={"open": [22]},
                response_group="g1",
                reasoning_state={
                    "provider_id": "compatible",
                    "model": model,
                    "reasoning_content": "Check the host first.",
                },
            )
        ],
    )

    payload = provider._payload(request, model)

    (assistant,) = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert payload["thinking"] == PRESERVED
    assert assistant["reasoning_content"] == "Check the host first."


@pytest.mark.parametrize(
    ("route", "model", "sent", "resent"),
    [
        (DEEPSEEK, "deepseek-reasoner", {"thinking": DISABLED}, {}),
        (
            VLLM,
            "deepseek-ai/DeepSeek-V3.1",
            {"chat_template_kwargs": {"thinking": False}},
            {},
        ),
    ],
)
def test_a_route_that_refuses_to_skip_thinking_is_asked_again(
    route, model, sent, resent
):
    """``none`` is a preference: a 400 naming thinking gets the default (#498)."""

    recorder = _Recorder(
        (400, {"error": {"message": "thinking cannot be disabled for this model"}}),
        (200, CHAT_OK),
    )
    provider = _compatible(route, model, transport=recorder.transport)

    response = asyncio.run(provider.complete(_ask(model, "none")))

    assert response.text == "ok"
    assert [_pick(p, CHAT_CONTROLS) for p in recorder.payloads] == [sent, resent]


def test_a_level_the_operator_chose_is_never_dropped():
    recorder = _Recorder(
        (400, {"error": {"message": "reasoning_effort max is not supported"}}),
        (200, CHAT_OK),
    )
    provider = _compatible(DEEPSEEK, "deepseek-chat", transport=recorder.transport)

    with pytest.raises(ProviderError):
        asyncio.run(provider.complete(_ask("deepseek-chat", "xhigh")))

    assert len(recorder.payloads) == 1


# --- OpenRouter ------------------------------------------------------------------

OPENROUTER = (ProviderFlavor.OPENROUTER, "https://openrouter.ai/api/v1", False)


def test_openrouter_does_not_ask_a_mandatory_reasoning_model_to_skip_it():
    model = "moonshotai/kimi-k3-thinking"
    provider = _compatible(
        OPENROUTER,
        model,
        model_parameters={model: ["reasoning", "tools", "tool_choice"]},
        reasoning_mandatory_models=[model],
    )

    off = provider._payload(_ask(model, "none"), model)
    chosen = provider._payload(_ask(model, "high"), model)

    assert off["reasoning"] == {"exclude": False}
    assert chosen["reasoning"] == {"exclude": False, "effort": "high"}


def test_openrouter_catalog_flag_reaches_the_request():
    catalog = openrouter_models(
        {
            "data": [
                {
                    "id": "moonshotai/kimi-k3-thinking",
                    "supported_parameters": ["reasoning", "tools"],
                    "reasoning": {
                        "mandatory": True,
                        "supported_efforts": ["low", "medium", "high"],
                    },
                },
                {
                    "id": "deepseek/deepseek-v4.1-flash",
                    "supported_parameters": ["reasoning", "tools"],
                    "reasoning": {"mandatory": False},
                },
            ]
        }
    )
    assert [item.reasoning_mandatory for item in catalog] == [True, False]
    provider = provider_from_profile(
        ProviderProfile(
            name="OpenRouter",
            provider_type="openrouter",
            secret_ref=f"env:{KEY_ENV}",
            model_allowlist=[item.id for item in catalog],
            metadata={
                "default_model": catalog[0].id,
                "model_descriptors": [item.model_dump(mode="json") for item in catalog],
            },
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)

    mandatory = provider._payload(_ask(catalog[0].id, "none"), catalog[0].id)
    optional = provider._payload(_ask(catalog[1].id, "none"), catalog[1].id)

    assert mandatory["reasoning"] == {"exclude": False}
    assert optional["reasoning"] == {"exclude": False, "effort": "none"}


@pytest.mark.parametrize(
    "model", ["anthropic/claude-fable-5.1", "anthropic/claude-mythos-5.1"]
)
def test_openrouter_fable_is_never_asked_to_skip_thinking_or_forced_to_call(model):
    provider = _compatible(
        OPENROUTER,
        model,
        model_parameters={model: ["reasoning", "tools", "tool_choice", "max_tokens"]},
    )

    payload = provider._payload(_routing(model, "none"), model)

    assert payload["reasoning"] == {"exclude": False}
    # Forced choice becomes Anthropic "any", which these models reject (#486).
    assert payload["tool_choice"] == "auto"


def test_openrouter_keeps_forced_choice_for_models_that_accept_it():
    model = "anthropic/claude-opus-5"
    provider = _compatible(
        OPENROUTER,
        model,
        model_parameters={model: ["reasoning", "tools", "tool_choice", "max_tokens"]},
    )

    payload = provider._payload(_routing(model, "none"), model)

    assert payload["tool_choice"] == "required"
    assert payload["reasoning"] == {"exclude": False, "effort": "none"}


# --- Anthropic Messages -------------------------------------------------------------


def _native_config(kind, flavor, model):
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
        options={"retry_backoff_seconds": 0},
    )


def _anthropic(model, request, *responses):
    recorder = _Recorder(*(responses or ((200, ANTHROPIC_OK),)))
    provider = AnthropicProvider(
        _native_config(ProviderKind.ANTHROPIC, ProviderFlavor.ANTHROPIC, model),
        transport=recorder.transport,
    )
    response = asyncio.run(provider.complete(request))
    return recorder.payloads, response


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        # none: thinking off where the model allows it.
        ("claude-opus-5", "none", {"thinking": DISABLED}),
        ("claude-sonnet-5", "none", {"thinking": DISABLED}),
        ("claude-opus-4-8", "none", {"thinking": DISABLED}),
        ("claude-haiku-4-5", "none", {"thinking": DISABLED}),
        # Fable and Mythos always think: the lowest effort instead.
        ("claude-fable-5-1", "none", {"output_config": {"effort": "low"}}),
        ("claude-mythos-5-1", "none", {"output_config": {"effort": "low"}}),
        # Adaptive thinking with the effort level on 4.6 and later.
        (
            "claude-opus-5",
            "high",
            {"thinking": ADAPTIVE, "output_config": {"effort": "high"}},
        ),
        (
            "claude-opus-5",
            "xhigh",
            {"thinking": ADAPTIVE, "output_config": {"effort": "xhigh"}},
        ),
        (
            "claude-fable-5-1",
            "medium",
            {"thinking": ADAPTIVE, "output_config": {"effort": "medium"}},
        ),
        (
            "claude-opus-4-7",
            "minimal",
            {"thinking": ADAPTIVE, "output_config": {"effort": "low"}},
        ),
        # 4.6 has no xhigh; max is its top level.
        (
            "claude-sonnet-4-6",
            "xhigh",
            {"thinking": ADAPTIVE, "output_config": {"effort": "max"}},
        ),
        # Before 4.6 thinking takes a budget; Opus 4.5 also takes effort.
        (
            "claude-opus-4-5",
            "xhigh",
            {
                "thinking": {"type": "enabled", "budget_tokens": 14_400},
                "output_config": {"effort": "high"},
            },
        ),
        (
            "claude-haiku-4-5",
            "high",
            {"thinking": {"type": "enabled", "budget_tokens": 9_600}},
        ),
        (
            "claude-sonnet-4-5-20250929",
            "minimal",
            {"thinking": {"type": "enabled", "budget_tokens": 1_024}},
        ),
        # An Anthropic-compatible endpoint serving another model.
        ("deepseek-v4-pro", "none", {"thinking": DISABLED}),
        ("deepseek-v4-pro", "high", {}),
        # No level: the model's own default.
        ("claude-opus-5", None, {}),
    ],
)
def test_anthropic_translates_reasoning_effort(model, effort, expected):
    (payload,), _ = _anthropic(model, _ask(model, effort))

    assert _pick(payload, CLAUDE_CONTROLS) == expected


def test_anthropic_budget_follows_an_explicit_reasoning_ceiling():
    model = "claude-haiku-4-5"
    (payload,), _ = _anthropic(model, _ask(model, "high", reasoning_max_tokens=4_000))

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4_000}


def test_anthropic_forced_routing_sends_effort_without_thinking():
    """Thinking cannot be switched on beside a forced tool choice."""

    model = "claude-sonnet-4-6"
    (payload,), _ = _anthropic(model, _routing(model, "high", temperature=0))

    assert _pick(payload, CLAUDE_CONTROLS) == {"output_config": {"effort": "high"}}
    assert payload["tool_choice"]["type"] == "any"
    assert payload["temperature"] == 0


def test_anthropic_thinking_replaces_sampling():
    model = "claude-sonnet-4-6"
    (payload,), _ = _anthropic(model, _ask(model, "high", temperature=0.2))

    assert payload["thinking"] == ADAPTIVE
    assert "temperature" not in payload


def test_anthropic_thinking_text_is_reasoning():
    model = "claude-opus-5"
    body = {
        **ANTHROPIC_OK,
        "content": [
            {"type": "thinking", "thinking": "Port 22 first.", "signature": "SIG"},
            {"type": "text", "text": "Port 22 is open."},
        ],
    }
    _, response = _anthropic(model, _ask(model, "high"), (200, body))

    assert response.text == "Port 22 is open."
    assert response.reasoning == "Port 22 first."


# --- Bedrock Converse ------------------------------------------------------------------


class _Bedrock:
    def __init__(self, monkeypatch, content=None):
        self.calls: list[dict] = []
        self.content = content or [{"text": "ok"}]
        monkeypatch.setattr(providers.boto3, "client", self.client)

    def client(self, service, **kwargs):
        return self

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"role": "assistant", "content": self.content}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }


def _bedrock(monkeypatch, model, request, content=None):
    bedrock = _Bedrock(monkeypatch, content)
    response = asyncio.run(
        BedrockProvider(
            _native_config(ProviderKind.BEDROCK, ProviderFlavor.BEDROCK, model)
        ).complete(request)
    )
    return bedrock.calls[0], response


@pytest.mark.parametrize(
    ("model", "effort", "routing", "expected"),
    [
        ("us.anthropic.claude-opus-5", "none", False, {"thinking": DISABLED}),
        (
            "anthropic.claude-sonnet-5",
            "high",
            False,
            {"thinking": ADAPTIVE, "output_config": {"effort": "high"}},
        ),
        (
            "anthropic.claude-fable-5-1",
            "none",
            False,
            {"output_config": {"effort": "low"}},
        ),
        # Bedrock takes a forced choice only with thinking off (#486), and
        # Opus 5 takes thinking off only at effort high or below.
        (
            "anthropic.claude-opus-5",
            "xhigh",
            True,
            {"thinking": DISABLED, "output_config": {"effort": "high"}},
        ),
        (
            "anthropic.claude-sonnet-5",
            "low",
            True,
            {"thinking": DISABLED, "output_config": {"effort": "low"}},
        ),
        (
            "anthropic.claude-opus-4-7",
            "high",
            True,
            {"output_config": {"effort": "high"}},
        ),
        ("amazon.nova-pro-v1:0", "high", False, None),
    ],
)
def test_bedrock_translates_reasoning_effort(
    monkeypatch, model, effort, routing, expected
):
    request = (_routing if routing else _ask)(model, effort)

    sent, _ = _bedrock(monkeypatch, model, request)

    assert sent.get("additionalModelRequestFields") == expected


def test_bedrock_reasoning_content_is_reasoning(monkeypatch):
    model = "anthropic.claude-sonnet-5"
    _, response = _bedrock(
        monkeypatch,
        model,
        _ask(model, "high"),
        [
            {
                "reasoningContent": {
                    "reasoningText": {"text": "Check 22.", "signature": "s"}
                }
            },
            {"text": "Port 22 is open."},
        ],
    )

    assert response.text == "Port 22 is open."
    assert response.reasoning == "Check 22."


# --- Gemini --------------------------------------------------------------------------------


def _gemini(model, request, body=GEMINI_OK):
    recorder = _Recorder((200, body))
    provider = GeminiProvider(
        _native_config(ProviderKind.GEMINI, ProviderFlavor.GEMINI, model),
        transport=recorder.transport,
    )
    response = asyncio.run(provider.complete(request))
    return recorder.payloads[0], response


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        # Gemini 3 cannot switch thinking off: its lowest level instead.
        ("gemini-3-flash-preview", "none", {"thinkingLevel": "minimal"}),
        ("gemini-3-pro-preview", "none", {"thinkingLevel": "low"}),
        (
            "gemini-3-flash-preview",
            "minimal",
            {"thinkingLevel": "minimal", "includeThoughts": True},
        ),
        (
            "gemini-3.1-pro-preview",
            "high",
            {"thinkingLevel": "high", "includeThoughts": True},
        ),
        (
            "gemini-3-flash-preview",
            "xhigh",
            {"thinkingLevel": "high", "includeThoughts": True},
        ),
        # Gemini 2.5 takes a token budget; 2.5 Pro cannot go below 128.
        ("gemini-2.5-flash", "none", {"thinkingBudget": 0}),
        ("gemini-2.5-pro", "none", {"thinkingBudget": 128}),
        (
            "gemini-2.5-flash",
            "medium",
            {"thinkingBudget": 8_192, "includeThoughts": True},
        ),
        (
            "gemini-2.5-flash-lite",
            "minimal",
            {"thinkingBudget": 512, "includeThoughts": True},
        ),
        (
            "gemini-2.5-pro",
            "xhigh",
            {"thinkingBudget": 32_768, "includeThoughts": True},
        ),
        # No thinking control on older models, and none without a level.
        ("gemini-2.0-flash", "high", None),
        ("gemini-3-flash-preview", None, None),
    ],
)
def test_gemini_translates_reasoning_effort(model, effort, expected):
    payload, _ = _gemini(model, _ask(model, effort))

    assert payload["generationConfig"].get("thinkingConfig") == expected


def test_gemini_budget_follows_an_explicit_reasoning_ceiling():
    model = "gemini-2.5-pro"
    payload, _ = _gemini(model, _ask(model, "high", reasoning_max_tokens=4_000))

    assert payload["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 4_000,
        "includeThoughts": True,
    }


def test_gemini_thought_parts_are_reasoning_not_text():
    model = "gemini-3-flash-preview"
    body = {
        "responseId": "g_2",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "**Planning** port 22 first.", "thought": True},
                        {"text": "Port 22 is open."},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
    }

    _, response = _gemini(model, _ask(model, "high"), body)

    assert response.text == "Port 22 is open."
    assert response.reasoning == "**Planning** port 22 first."


# --- OpenAI Responses ----------------------------------------------------------------------


def _responses(model, request, *responses):
    recorder = _Recorder(*(responses or ((200, RESPONSES_OK),)))
    provider = OpenAIResponsesProvider(
        _native_config(ProviderKind.OPENAI_RESPONSES, ProviderFlavor.OPENAI, model),
        transport=recorder.transport,
    )
    response = asyncio.run(provider.complete(request))
    return recorder.payloads, response


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gpt-5", "high", {"effort": "high", "summary": "auto"}),
        ("o4-mini", "low", {"effort": "low", "summary": "auto"}),
        ("gpt-5.1", "none", {"effort": "none"}),
        ("gpt-5", None, None),
        ("gpt-4.1", "high", None),
    ],
)
def test_responses_translate_reasoning_effort(model, effort, expected):
    (payload,), _ = _responses(model, _ask(model, effort))

    assert payload.get("reasoning") == expected


def test_responses_reasoning_summaries_are_reasoning():
    body = {
        **RESPONSES_OK,
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Checked both hosts."}],
                "encrypted_content": "ENC",
            },
            *RESPONSES_OK["output"],
        ],
    }

    _, response = _responses("gpt-5", _ask("gpt-5", "high"), (200, body))

    assert response.text == "ok"
    assert response.reasoning == "Checked both hosts."


def test_responses_model_without_none_is_asked_again_with_its_default():
    refusal = {
        "error": {
            "message": "Unsupported value: 'none' is not supported with the "
            "'gpt-5' model for reasoning.effort.",
            "param": "reasoning.effort",
        }
    }

    payloads, response = _responses(
        "gpt-5", _ask("gpt-5", "none"), (400, refusal), (200, RESPONSES_OK)
    )

    assert response.text == "ok"
    assert payloads[0]["reasoning"] == {"effort": "none"}
    assert "reasoning" not in payloads[1]


def test_responses_keep_the_level_when_summaries_are_refused():
    refusal = {
        "error": {
            "message": "Your organization must be verified to generate "
            "reasoning summaries.",
            "param": "reasoning.summary",
        }
    }

    payloads, response = _responses(
        "gpt-5", _ask("gpt-5", "high"), (400, refusal), (200, RESPONSES_OK)
    )

    assert response.text == "ok"
    assert [p["reasoning"] for p in payloads] == [
        {"effort": "high", "summary": "auto"},
        {"effort": "high"},
    ]
