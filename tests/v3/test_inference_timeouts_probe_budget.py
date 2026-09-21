"""Non-streamed inference and short utility calls must leave thinking models room.

A non-streamed Chat Completions request receives the whole generation as one
response, so its read timeout bounds the entire answer: the OpenAI-compatible
adapter (OpenRouter, local runtimes, gateways) gets the long, profile-tunable
timeout #486 gave the native adapters, and so do the mission specialists that
share it. The capability probe gets enough output for a thinking model to
reason and still make its one call, a timeout that lets that output arrive, and
asks the route to skip thinking. Naming, retrieval planning, compaction and
scope import ask for no reasoning too, and a route whose model must reason is
asked again with its default instead of failing the call.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import nebula.v3.api as api_module
import nebula.v3.chat as chat_module
from nebula.v3.agent_tooling import BrokeredToolSpecialist
from nebula.v3.api import _verify_provider_capability
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.context import ContextCompactor, ContextSource, estimate_model_request
from nebula.v3.domain import (
    ContextOwnerType,
    ContextSourceReference,
    Engagement,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.orchestration import (
    ModelSpecialist,
    PlannedTask,
    SpecialistContext,
    SpecialistRole,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderError,
    ProviderFlavor,
    ProviderKind,
    build_provider,
    provider_from_profile,
)
from nebula.v3.scope_import import ScopeImportService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import IdempotencyBehavior, ToolSpec
from tests.v3.test_chat import FakeProvider, _profile as _chat_profile, _source
from tests.v3.test_context import MemoryProvider, _message, _owner
from tests.v3.test_context import _profile as _context_profile
from tests.v3.test_scope_import import (
    StructuredProvider,
    _create_import,
    _structured_profile,
)

KEY_ENV = "NEBULA_INFERENCE_TIMEOUT_TEST_KEY"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"
GLM = "z-ai/glm-5.3-flash"
# What an OpenRouter reasoning route advertises for these models.
REASONING_ROUTE = [
    "include_reasoning",
    "max_tokens",
    "reasoning",
    "response_format",
    "structured_outputs",
    "temperature",
    "tool_choice",
    "tools",
]
CHAT_OK = {
    "id": "gen-1",
    "model": "m",
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
# Tokens a thinking model spends reasoning before its one call, whatever
# reasoning level it was asked for.
THINKING_TOKENS = 400


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    monkeypatch.delenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS", raising=False)


class _Recorder:
    """Answers every request with one body and records what was sent."""

    def __init__(self, body=None, *, status=200, content=None, headers=None):
        self.body = CHAT_OK if body is None else body
        self.status = status
        self.content = content
        self.headers = headers
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.content is not None:
            return httpx.Response(
                self.status, content=self.content, headers=self.headers
            )
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def timeouts(self) -> list[dict]:
        return [request.extensions["timeout"] for request in self.requests]

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


def _profile(provider_type: str, *, model: str = "m", **options) -> ProviderProfile:
    local = provider_type == "vllm"
    return ProviderProfile(
        name=provider_type,
        provider_type=provider_type,
        endpoint="http://127.0.0.1:8001/v1" if local else None,
        is_local=local,
        secret_ref=None if local else f"env:{KEY_ENV}",
        model_allowlist=[model],
        metadata={
            "default_model": model,
            "options": {"retry_backoff_seconds": 0, **options},
        },
    )


def _runtime(profile: ProviderProfile, recorder: _Recorder, *, tools=False):
    """The adapter missions and chat build from a stored profile."""

    config = provider_from_profile(profile).config
    if tools:
        # Tools are enabled for a model once its capability probe verifies.
        config = config.model_copy(
            update={"capabilities": ModelCapabilities(tools=True, strict_tools=True)}
        )
    return build_provider(config, transport=recorder.transport)


def _hello(model: str = "m", **update) -> ModelRequest:
    return ModelRequest(
        model=model, messages=[ModelMessage(role="user", content="hello")], **update
    )


# --- Non-streamed Chat Completions get the whole-generation timeout ----------


@pytest.mark.parametrize("provider_type", ["openrouter", "vllm"])
def test_non_streamed_chat_completions_get_the_generation_timeout(
    monkeypatch, provider_type
):
    def timeout_for(profile):
        recorder = _Recorder()
        asyncio.run(_runtime(profile, recorder).complete(_hello()))
        return recorder.timeouts[0]

    # A thinking model's whole answer arrives as one response; the 120 s
    # client default used to bound all of it.
    default = timeout_for(_profile(provider_type))
    assert default["read"] == 600
    assert default["connect"] == 10

    configured = timeout_for(_profile(provider_type, request_timeout_seconds=1800))
    assert configured["read"] == 1800
    assert configured["connect"] == 10

    monkeypatch.setenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS", "900")
    assert timeout_for(_profile(provider_type))["read"] == 900
    monkeypatch.delenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS")

    # A config built with its own timeout keeps it, with the short connect.
    recorder = _Recorder()
    explicit = build_provider(
        ProviderConfig(
            id="explicit",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:8001/v1",
            default_model="m",
            local=True,
            timeout_seconds=45,
        ),
        transport=recorder.transport,
    )
    asyncio.run(explicit.complete(_hello()))
    assert recorder.timeouts[0]["read"] == 45
    assert recorder.timeouts[0]["connect"] == 10


def test_streamed_and_discovery_requests_keep_their_short_timeouts():
    """Guard: only the non-streamed generation gets the long timeout.

    A stream's read timeout already bounds each chunk, not the answer, and
    catalog and health calls must fail fast.
    """

    frames = (
        b'data: {"id":"s1","model":"m","choices":[{"delta":{"content":"ok"},'
        b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )
    stream = _Recorder(content=frames, headers={"content-type": "text/event-stream"})

    async def drain():
        async for _event in _runtime(_profile("vllm"), stream).stream(_hello()):
            pass

    asyncio.run(drain())
    assert stream.timeouts[0]["read"] == 120

    catalog = _Recorder({"data": [{"id": "m"}]})
    health = asyncio.run(_runtime(_profile("vllm"), catalog).health())
    assert health.healthy
    assert catalog.timeouts[0]["read"] == 120


# --- Mission specialists share the adapter and its timeout -------------------


FINISH_ARGUMENTS = {
    "status": "complete",
    "summary": "The service is mapped",
    "rationale": "Every observation the objective needs is in hand",
}


class _FinishRecorder(_Recorder):
    """Answers a specialist routing step with its finish call."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return _finish_call(json.loads(request.content))


def _finish_call(payload: dict) -> httpx.Response:
    finish = next(
        tool["function"]["name"]
        for tool in payload["tools"]
        if "finish_task" in tool["function"]["name"]
    )
    return httpx.Response(
        200,
        json={
            "id": "gen-finish",
            "model": DEEPSEEK,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": finish,
                                    "arguments": json.dumps(FINISH_ARGUMENTS),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _specialist_context() -> SpecialistContext:
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
        allowed_tools=frozenset({"nmap.tcp"}),
    )


@pytest.mark.parametrize("specialist", ["analysis", "brokered"])
def test_mission_specialists_get_the_generation_timeout(tmp_path, specialist):
    profile = _profile("openrouter", model=DEEPSEEK)
    if specialist == "analysis":
        recorder = _Recorder({**CHAT_OK, "model": DEEPSEEK})
        runner = ModelSpecialist(_runtime(profile, recorder), model=DEEPSEEK)
    else:
        recorder = _FinishRecorder()
        runner = BrokeredToolSpecialist(
            _runtime(profile, recorder, tools=True),
            role=SpecialistRole.NETWORK_SERVICE,
            # The finish call executes nothing, so no broker is reached.
            broker=None,  # type: ignore[arg-type]
            scope=ScopePolicy(id="scope-1", engagement_id="engagement-1"),
            workspace=Path(tmp_path),
            specs={
                "nmap.tcp": ToolSpec(
                    name="nmap.tcp",
                    version="1.0.0",
                    description="nmap.tcp capability",
                    risk_class=RiskClass.LOCAL_READ,
                    input_schema={"type": "object", "properties": {}},
                    output_schema={"type": "object"},
                    idempotency=IdempotencyBehavior.SAFE,
                    budget_class="execution",
                )
            },
            model=DEEPSEEK,
        )

    asyncio.run(runner.run(_specialist_context()))

    assert recorder.timeouts[0]["read"] == 600
    assert recorder.timeouts[0]["connect"] == 10


# --- The capability probe leaves a thinking model room ------------------------


def _openrouter_probe_profile(store, model, parameters=None, **descriptor):
    return store.create(
        ProviderProfile(
            name="OpenRouter",
            provider_type="openrouter",
            secret_ref=f"env:{KEY_ENV}",
            model_allowlist=[model],
            metadata={
                "default_model": model,
                "options": {"retry_backoff_seconds": 0},
                "model_descriptors": [
                    {
                        "id": model,
                        "name": model,
                        "context_window": 1_048_576,
                        "max_output_tokens": 131_072,
                        "supported_parameters": (
                            REASONING_ROUTE if parameters is None else parameters
                        ),
                        **descriptor,
                    }
                ],
            },
        )
    )


def _anthropic_probe_profile(store, model):
    return store.create(
        ProviderProfile(
            name="Anthropic",
            provider_type="anthropic",
            secret_ref=f"env:{KEY_ENV}",
            model_allowlist=[model],
            metadata={"default_model": model},
        )
    )


def _thinking_openrouter(request: httpx.Request) -> httpx.Response:
    """A model that thinks before its call, whatever it was asked for."""

    if not request.url.path.endswith("/chat/completions"):
        return httpx.Response(404, json={"error": {"message": "not found"}})
    payload = json.loads(request.content)
    budget = payload.get("max_tokens") or 0
    if budget <= THINKING_TOKENS:
        return httpx.Response(
            200,
            json={
                "id": "gen-cut",
                "model": payload["model"],
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "The operator wants the verification call.",
                        },
                        "finish_reason": "length",
                    }
                ],
                "usage": {"completion_tokens": budget},
            },
        )
    function = payload["tools"][0]["function"]
    nonce = function["parameters"]["properties"]["nonce"]["enum"][0]
    return httpx.Response(
        200,
        json={
            "id": "gen-call",
            "model": payload["model"],
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "The operator wants the verification call.",
                        "tool_calls": [
                            {
                                "id": "call_probe",
                                "type": "function",
                                "function": {
                                    "name": function["name"],
                                    "arguments": json.dumps({"nonce": nonce}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"completion_tokens": THINKING_TOKENS + 20},
        },
    )


def _thinking_anthropic(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    thinking = {"type": "thinking", "thinking": "Call the verification tool."}
    if payload["max_tokens"] <= THINKING_TOKENS:
        return httpx.Response(
            200,
            json={
                "id": "msg_cut",
                "model": payload["model"],
                "content": [thinking],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 1, "output_tokens": payload["max_tokens"]},
            },
        )
    tool = payload["tools"][0]
    nonce = tool["input_schema"]["properties"]["nonce"]["enum"][0]
    return httpx.Response(
        200,
        json={
            "id": "msg_call",
            "model": payload["model"],
            "content": [
                thinking,
                {
                    "type": "tool_use",
                    "id": "toolu_probe",
                    "name": tool["name"],
                    "input": {"nonce": nonce},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": THINKING_TOKENS + 20},
        },
    )


def _probe(store, profile, model, handler):
    sent: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent.append(json.loads(request.content))
        return handler(request)

    def factory(candidate):
        return build_provider(
            provider_from_profile(candidate).config,
            transport=httpx.MockTransport(recording),
        )

    result = asyncio.run(_verify_provider_capability(store, profile, model, factory))
    return result, sent


@pytest.mark.parametrize(
    ("provider_type", "model"),
    [
        ("openrouter", DEEPSEEK),
        ("openrouter", GLM),
        # Fable 5.1 always thinks; Opus 5 thinks by default.
        ("anthropic", "claude-fable-5-1"),
        ("anthropic", "claude-opus-5"),
    ],
)
def test_capability_probe_verifies_a_thinking_model(tmp_path, provider_type, model):
    store = NebulaStore(tmp_path / "nebula.db")
    if provider_type == "openrouter":
        profile = _openrouter_probe_profile(store, model)
        handler = _thinking_openrouter
    else:
        profile = _anthropic_probe_profile(store, model)
        handler = _thinking_anthropic

    result, sent = _probe(store, profile, model, handler)

    assert result.verification.status == ProviderVerificationStatus.VERIFIED, (
        result.verification.failure_detail
    )
    # Room for Anthropic's smallest thinking budget (1,024) plus the call.
    assert sent[0]["max_tokens"] == 2_048


@pytest.mark.parametrize(
    ("parameters", "reasoning"),
    [
        # The route takes the control: ask it to skip thinking for the probe.
        (REASONING_ROUTE, {"exclude": False, "effort": "none"}),
        # A route that does not advertise it is never sent one; the allowance
        # alone lets the model think and still call.
        ([p for p in REASONING_ROUTE if p != "reasoning"], None),
    ],
    ids=["advertised", "not-advertised"],
)
def test_capability_probe_asks_a_reasoning_route_to_skip_thinking(
    tmp_path, parameters, reasoning
):
    store = NebulaStore(tmp_path / "nebula.db")
    profile = _openrouter_probe_profile(store, DEEPSEEK, parameters)

    result, sent = _probe(store, profile, DEEPSEEK, _thinking_openrouter)

    assert result.verification.status == ProviderVerificationStatus.VERIFIED
    assert sent[0].get("reasoning") == reasoning


class _RecordingProbe:
    """A provider that answers the probe correctly and keeps its request."""

    def __init__(self):
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id="probe",
            model=request.model or "m",
            tool_calls=[
                {
                    "id": "call_probe",
                    "name": request.tools[0].name,
                    "arguments": {"nonce": nonce},
                }
            ],
            finish_reason="tool_calls",
        )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        # The operator's output cap for the profile still binds.
        ({"max_output_tokens": 768}, lambda request: 768),
        # A small served window keeps room for the probe's own prompt.
        (
            {"context_window": 2_048},
            lambda request: 2_048 - estimate_model_request(request),
        ),
        # Nothing known: the full probe allowance.
        ({}, lambda request: 2_048),
    ],
    ids=["operator-cap", "small-window", "unknown"],
)
def test_capability_probe_budget_is_bounded_by_the_model_limits(
    tmp_path, options, expected
):
    store = NebulaStore(tmp_path / "nebula.db")
    profile = store.create(_profile("vllm", **options))
    probe = _RecordingProbe()

    result = asyncio.run(
        _verify_provider_capability(store, profile, "m", lambda _: probe)
    )

    assert result.verification.status == ProviderVerificationStatus.VERIFIED
    request = probe.requests[0]
    assert request.max_output_tokens == expected(request)
    assert request.reasoning_effort == "none"


class _TimedAsyncio:
    """Records the deadline the probe gives its provider call."""

    def __init__(self):
        self.timeouts: list[float] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        self.timeouts.append(timeout)
        return await asyncio.wait_for(awaitable, timeout)


def test_capability_probe_waits_long_enough_for_its_budget(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "nebula.db")
    profile = store.create(_profile("vllm"))
    probe = _RecordingProbe()
    timed = _TimedAsyncio()
    monkeypatch.setattr(api_module, "asyncio", timed)

    asyncio.run(_verify_provider_capability(store, profile, "m", lambda _: probe))

    budget = probe.requests[0].max_output_tokens
    assert budget is not None and budget >= 1_024
    # The whole allowance still arrives from a route producing 20 tokens/s.
    assert timed.timeouts[0] >= budget / 20


# --- Utility calls ask for no reasoning ---------------------------------------


def test_naming_and_retrieval_planning_ask_for_no_reasoning(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "chat.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(_chat_profile(local=True))
    store.create(_source(engagement.id, text="Relevant port is 443."))
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store)

    prepared = service.prepare(
        ChatCompletionRequest(
            engagement_id=engagement.id,
            provider_id=profile.id,
            messages=[{"role": "user", "content": "What port is relevant?"}],
        )
    )
    asyncio.run(service.complete(prepared))

    efforts = {
        request.metadata.get("operation") or "answer": request.reasoning_effort
        for request in provider.requests
    }
    assert efforts == {
        "agentic_knowledge_retrieval": "none",
        "conversation_naming": "none",
        # The operator's answer keeps the model's own reasoning.
        "answer": None,
    }


def test_context_compaction_asks_for_no_reasoning(tmp_path):
    store = NebulaStore(tmp_path / "context.db")
    profile = _context_profile()
    session = _owner(store, profile)
    _message(
        store,
        session,
        message_id="message-1",
        sequence=1,
        content="Use port 8443 for this review.",
    )
    provider = MemoryProvider(profile.id)

    asyncio.run(
        ContextCompactor(store).compact(
            owner_type=ContextOwnerType.CHAT_SESSION,
            owner_id=session.id,
            engagement_id=session.engagement_id,
            provider_profile=profile,
            provider=provider,
            model="model-a",
            sources=[
                ContextSource(
                    ContextSourceReference(
                        source_kind="chat_message",
                        source_id="message-1",
                        sequence=1,
                    ),
                    "Use port 8443 for this review.",
                )
            ],
            compacted_through=1,
        )
    )

    assert provider.requests
    assert {request.reasoning_effort for request in provider.requests} == {"none"}


def test_scope_import_asks_for_no_reasoning(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-1", name="Scope"))
    profile = _structured_profile(store)
    provider = StructuredProvider(profile.id)
    service = ScopeImportService(
        store=store, artifact_store=artifacts, provider_factory=lambda _: provider
    )

    _create_import(service, engagement.id, profile.id, b"In scope: 192.0.2.7\n")

    assert provider.requests
    assert {request.reasoning_effort for request in provider.requests} == {"none"}


# --- A route whose model must reason is asked again with its default ----------

MANDATORY = {
    "error": {
        "code": 400,
        "message": "Reasoning is mandatory for this endpoint and cannot be disabled.",
    }
}
NO_NONE_LEVEL = {
    "error": {
        "message": (
            "Unsupported value: 'reasoning_effort' does not support 'none' with "
            "this model. Supported values are: 'low', 'medium', and 'high'."
        ),
        "type": "invalid_request_error",
        "param": "reasoning_effort",
        "code": "unsupported_value",
    }
}


class _RefusesReasoningOff(_Recorder):
    def __init__(self, refusal):
        super().__init__()
        self.refusal = refusal

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content)
        asked_off = (payload.get("reasoning") or {}).get("effort") == "none" or (
            payload.get("reasoning_effort") == "none"
        )
        if asked_off:
            return httpx.Response(400, json=self.refusal)
        return httpx.Response(200, json=CHAT_OK)


@pytest.mark.parametrize(
    ("config", "model", "refusal", "retried"),
    [
        (
            {
                "flavor": ProviderFlavor.OPENROUTER,
                "base_url": "https://openrouter.ai/api/v1",
            },
            "google/gemini-2.5-pro",
            MANDATORY,
            {"exclude": False},
        ),
        (
            {"base_url": "https://gateway.invalid/v1"},
            "o3",
            NO_NONE_LEVEL,
            None,
        ),
    ],
    ids=["openrouter-mandatory-reasoning", "openai-no-none-level"],
)
def test_a_route_that_refuses_reasoning_off_answers_with_its_default(
    config, model, refusal, retried
):
    recorder = _RefusesReasoningOff(refusal)
    provider = build_provider(
        ProviderConfig(
            id="refuses-off",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            default_model=model,
            api_key_env=KEY_ENV,
            options={"retry_backoff_seconds": 0},
            **config,
        ),
        transport=recorder.transport,
    )

    response = asyncio.run(provider.complete(_hello(model, reasoning_effort="none")))

    assert response.text == "ok"
    first, second = recorder.payloads
    asked = (
        first["reasoning"]["effort"]
        if "reasoning" in first
        else first["reasoning_effort"]
    )
    assert asked == "none"
    assert second.get("reasoning") == retried
    assert "reasoning_effort" not in second


def test_other_refusals_and_chosen_levels_are_not_resent():
    """Guard: only a refused ``none`` is resent, and only once."""

    unrelated = _Recorder({"error": {"message": "invalid tools"}}, status=400)
    provider = build_provider(
        ProviderConfig(
            id="openrouter",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model=DEEPSEEK,
            api_key_env=KEY_ENV,
        ),
        transport=unrelated.transport,
    )
    with pytest.raises(ProviderError):
        asyncio.run(provider.complete(_hello(DEEPSEEK, reasoning_effort="none")))
    assert len(unrelated.requests) == 1

    # An operator-chosen level the route refuses is reported, not dropped.
    chosen = _Recorder(MANDATORY, status=400)
    provider = build_provider(provider.config, transport=chosen.transport)
    with pytest.raises(ProviderError):
        asyncio.run(provider.complete(_hello(DEEPSEEK, reasoning_effort="high")))
    assert len(chosen.requests) == 1
