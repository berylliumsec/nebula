"""Default output budget sized from the selected model (HIST-1, HIST-13).

The chat UI never sends a maximum output, so the default is what every turn
gets, and the window minus that default is the input capacity compaction
works against. Reasoning models spend their thinking tokens from the same
allowance, so a flat 2,048 cut them off mid-thought.

#521 made the default the model's published output maximum. Many OpenRouter
endpoints publish about 90% of the window there (the figure they report when
they have no separate limit), so a turn left a tenth of the window for input
and compaction ran early. The operator decided (2026-09-25) to cap the
default at a quarter of the window: min(published limit, max(25% of the
window, 8,192)), the 8,192 floor never taking more than half the window. A
published limit at or above the window (#578) or a window without a
published limit is no separate output limit, so the window alone sizes the
default. Only an unknown window keeps the 2,048 fallback. A profile's
maximum and a request's maximum are explicit and not held to the share.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import nebula.v3.chat as chat_module
from nebula.v3.agent_tooling import BrokeredToolSpecialist
from nebula.v3.automation_tools import AutomationToolPlatform
from nebula.v3.browser_tools import BrowserAutomationToolPlatform
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.cli import app
from nebula.v3.context import (
    estimate_messages,
    estimate_model_request,
    resolve_context_limits,
)
from nebula.v3.domain import (
    AgentRun,
    ChatMessage,
    ChatRole,
    ChatSession,
    Engagement,
    ProviderPrivacy,
    ProviderProfile,
    RunBudget,
    ScopePolicy,
)
from nebula.v3.missions import MissionService
from nebula.v3.orchestration import ModelSpecialist, SpecialistRole
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
    OpenAICompatibleProvider,
    config_from_catalog,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider


def _hosted(provider_type: str, **metadata: object) -> ProviderProfile:
    return ProviderProfile(
        id="hosted",
        name="Hosted",
        provider_type=provider_type,
        endpoint="https://provider.invalid/v1",
        metadata=dict(metadata),
    )


def _local(*descriptors: dict[str, object], **options: int) -> ProviderProfile:
    return ProviderProfile(
        id="local",
        name="Local",
        provider_type="vllm",
        is_local=True,
        model_allowlist=["model-a"],
        metadata={
            "default_model": "model-a",
            "model_descriptors": list(descriptors),
            "options": options,
        },
    )


@pytest.mark.parametrize(
    ("provider_type", "model", "window", "expected"),
    [
        # Published limits above a quarter of the window: the quarter binds.
        ("openai_compatible", "glm-4.6", 202_752, 50_688),
        ("openai", "gpt-5.2", 400_000, 100_000),
        ("anthropic", "claude-opus-4-5", 200_000, 50_000),
        # Published limits below it are still the default.
        ("anthropic", "claude-opus-5", 1_000_000, 128_000),
        ("openai", "gpt-4o-mini", 128_000, 16_384),
    ],
)
def test_known_model_default_output_is_held_to_a_quarter_of_the_window(
    provider_type: str, model: str, window: int, expected: int
):
    limits = resolve_context_limits(_hosted(provider_type), model=model)

    assert limits.source == "known_model"
    assert limits.context_window == window
    assert limits.max_output_tokens == expected
    assert limits.input_capacity == window - expected


@pytest.mark.parametrize(
    ("model", "window", "published_output", "expected"),
    [
        # The operator's main models: 65,536 and 131,072 left 98,304 and
        # 71,680 tokens of input.
        ("deepseek/deepseek-v4.1-flash", 163_840, 65_536, 40_960),
        ("z-ai/glm-5.3-flash", 202_752, 131_072, 50_688),
        # About 90% of the window, OpenRouter's figure for an endpoint with
        # no separate limit: 117,964 left 13,108 tokens of input.
        ("z-ai/glm-4.7-flash", 131_072, 117_964, 32_768),
        ("moonshotai/kimi-k2.6", 262_144, 235_929, 65_536),
        ("deepseek/deepseek-chat-v3-0324", 163_840, 147_456, 40_960),
    ],
)
def test_openrouter_catalog_models_default_to_a_quarter_of_the_window(
    model: str, window: int, published_output: int, expected: int
):
    profile = _hosted(
        "openrouter",
        model_descriptors=[
            {
                "id": model,
                "context_window": window,
                "max_output_tokens": published_output,
                "primary_route_context_window": window,
            }
        ],
    )

    limits = resolve_context_limits(profile, model=model, required_parameters={"tools"})

    assert limits.context_window == window
    assert limits.max_output_tokens == expected
    assert limits.input_capacity == window - expected
    # The published figure still caps a request that asks for more.
    assert (
        resolve_context_limits(
            profile,
            model=model,
            requested_output_tokens=window,
            required_parameters={"tools"},
        ).max_output_tokens
        == published_output
    )


@pytest.mark.parametrize(
    ("window", "published_output", "expected"),
    [
        # A limit within a quarter of the window is the default.
        (131_072, 32_768, 32_768),
        (100_000, 8_000, 8_000),
        # Above it, the quarter (at least 8,192, at most half the window).
        (131_072, 65_536, 32_768),
        (16_384, 12_000, 8_192),
        (8_192, 7_372, 4_096),
        # A limit at or above the window is no separate output limit, so the
        # window alone sizes the default instead of leaving no input.
        (32_768, 32_768, 8_192),
        (16_384, 16_384, 8_192),
        (16_384, 20_000, 8_192),
    ],
)
def test_default_output_is_the_published_limit_within_the_window_share(
    window: int, published_output: int, expected: int
):
    profile = _local(
        {
            "id": "model-a",
            "context_window": window,
            "max_output_tokens": published_output,
        }
    )

    limits = resolve_context_limits(profile, model="model-a")

    assert limits.max_output_tokens == expected
    assert limits.input_capacity == window - expected
    assert limits.target_input_tokens == (window - expected) * 3 // 4


def test_unknown_window_keeps_the_2048_fallback_while_known_windows_size_it():
    known = resolve_context_limits(
        _local(
            {"id": "model-a", "context_window": 65_536, "max_output_tokens": 16_384}
        ),
        model="model-a",
    )
    assert known.max_output_tokens == 16_384

    # No limits at all: the 8,192-token fallback window.
    assert resolve_context_limits(_local(), model="model-a").max_output_tokens == 2_048
    # A known window without a published output limit: the window alone
    # bounds the reply, so it sizes the default as a limit at the window does.
    window_only = resolve_context_limits(
        _local({"id": "model-a", "context_window": 131_072}), model="model-a"
    )
    assert window_only.context_window == 131_072
    assert window_only.max_output_tokens == 32_768
    assert (
        resolve_context_limits(
            _hosted("openai_compatible"), model="grok-4"
        ).max_output_tokens
        == 64_000
    )
    configured_window = resolve_context_limits(
        _local(context_window=32_768), model="model-a"
    )
    assert configured_window.source == "configured"
    assert configured_window.max_output_tokens == 8_192
    # OpenRouter without a primary route keeps its safe 8,192 fallback window.
    unrouted = resolve_context_limits(
        _hosted(
            "openrouter",
            model_descriptors=[
                {
                    "id": "author/model-a",
                    "context_window": 200_000,
                    "max_output_tokens": 64_000,
                }
            ],
        ),
        model="author/model-a",
    )
    assert unrouted.context_window == 8_192
    assert unrouted.max_output_tokens == 2_048
    # A small window keeps half of itself for input.
    tiny = resolve_context_limits(
        _local({"id": "model-a", "context_window": 4_000, "max_output_tokens": 4_000}),
        model="model-a",
    )
    assert tiny.max_output_tokens == 2_000
    assert tiny.input_capacity == 2_000


def test_verified_route_output_limit_bounds_the_default():
    profile = _hosted(
        "openrouter",
        model_descriptors=[
            {
                "id": "author/model-a",
                "context_window": 200_000,
                "max_output_tokens": 64_000,
                "route_limits_verified": True,
                "route_limits": [
                    {
                        "provider_name": "wide",
                        "context_window": 200_000,
                        "max_input_tokens": 200_000,
                        "max_output_tokens": 64_000,
                        "supported_parameters": ["tools"],
                        "status": 0,
                    },
                    {
                        "provider_name": "bounded",
                        "context_window": 128_000,
                        "max_input_tokens": 128_000,
                        "max_output_tokens": 12_000,
                        "supported_parameters": ["tools"],
                        "status": 0,
                    },
                ],
            }
        ],
    )

    limits = resolve_context_limits(
        profile, model="author/model-a", required_parameters={"tools"}
    )

    assert limits.route_limits_verified is True
    assert limits.context_window == 128_000
    assert limits.max_output_tokens == 12_000


def test_explicit_operator_and_request_maxima_still_win():
    # The provider profile's declared maximum is the operator's own ceiling
    # and sizes the default when the model publishes none.
    declared = resolve_context_limits(
        _local(context_window=65_536, max_output_tokens=8_192), model="model-a"
    )
    assert declared.max_output_tokens == 8_192
    lowered = resolve_context_limits(
        _local(
            {"id": "model-a", "context_window": 131_072, "max_output_tokens": 32_768},
            max_output_tokens=1_000,
        ),
        model="model-a",
    )
    assert lowered.max_output_tokens == 1_000
    # It is not held to the quarter-window share either, only to a limit the
    # model publishes and to the window.
    raised = resolve_context_limits(
        _local(
            {"id": "model-a", "context_window": 131_072, "max_output_tokens": 117_964},
            max_output_tokens=100_000,
        ),
        model="model-a",
    )
    assert raised.max_output_tokens == 100_000
    assert raised.input_capacity == 31_072
    above_published = resolve_context_limits(
        _local(
            {"id": "model-a", "context_window": 131_072, "max_output_tokens": 65_536},
            max_output_tokens=100_000,
        ),
        model="model-a",
    )
    assert above_published.max_output_tokens == 65_536
    above_window = resolve_context_limits(
        _local(context_window=32_768, max_output_tokens=40_000), model="model-a"
    )
    assert above_window.max_output_tokens == 32_767

    profile = _hosted("openai_compatible")
    assert (
        resolve_context_limits(
            profile, model="glm-4.6", requested_output_tokens=1_000
        ).max_output_tokens
        == 1_000
    )
    # A request may ask for more than the default, up to the model limit.
    assert resolve_context_limits(profile, model="glm-4.6").max_output_tokens == 50_688
    assert (
        resolve_context_limits(
            profile, model="glm-4.6", requested_output_tokens=100_000
        ).max_output_tokens
        == 100_000
    )
    assert (
        resolve_context_limits(
            profile, model="glm-4.6", requested_output_tokens=200_000
        ).max_output_tokens
        == 131_072
    )


def _chat_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, window: int, output: int
) -> tuple[NebulaStore, Engagement, ProviderProfile, FakeProvider, ChatService]:
    store = NebulaStore(tmp_path / "chat-output.db")
    engagement = store.create(Engagement(id="eng-output", name="Output budget"))
    profile = store.create(
        ProviderProfile(
            id="provider-a",
            name="Provider A",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True},
            privacy=ProviderPrivacy(local_only=True),
            metadata={
                "default_model": "model-a",
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": window,
                        "max_output_tokens": output,
                    }
                ],
            },
        )
    )
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    return store, engagement, profile, provider, ChatService(store)


def test_prepared_chat_request_sends_the_model_output_default(tmp_path, monkeypatch):
    _, engagement, profile, provider, service = _chat_service(
        tmp_path, monkeypatch, window=131_072, output=32_768
    )

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "Think it through, then answer."}],
            include_knowledge=False,
            stream=True,
        )
    )

    assert prepared.model_request.max_output_tokens == 32_768
    limits = json.loads(prepared.model_request.metadata["resolved_context_limits"])
    assert limits["max_output_tokens"] == 32_768
    assert limits["input_capacity"] == 131_072 - 32_768

    asyncio.run(service.complete(prepared))
    answered = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert answered[-1].max_output_tokens == 32_768


def test_chat_turn_leaves_three_quarters_of_the_window_for_input(tmp_path, monkeypatch):
    # 117,964 of 131,072 is what OpenRouter publishes for endpoints without a
    # separate limit. As the default it left 13,108 tokens of input, so
    # compaction started after a few turns.
    _, engagement, profile, provider, service = _chat_service(
        tmp_path, monkeypatch, window=131_072, output=117_964
    )

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "Walk me through the findings."}],
            include_knowledge=False,
            stream=True,
        )
    )

    assert prepared.model_request.max_output_tokens == 32_768
    limits = json.loads(prepared.model_request.metadata["resolved_context_limits"])
    assert limits["max_output_tokens"] == 32_768
    assert limits["input_capacity"] == 98_304
    assert limits["target_input_tokens"] == 73_728
    asyncio.run(service.complete(prepared))
    answered = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert answered[-1].max_output_tokens == 32_768


def test_published_output_at_the_window_still_leaves_room_to_chat(
    tmp_path, monkeypatch
):
    # vLLM serves one combined max_model_len, so a descriptor can publish the
    # window as its output limit. That default once left one input token and
    # every message was refused as too large for the window. The window now
    # sizes it: a quarter, at least 8,192.
    _, engagement, profile, provider, service = _chat_service(
        tmp_path, monkeypatch, window=32_768, output=32_768
    )

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            messages=[{"role": "user", "content": "What changed?"}],
            include_knowledge=False,
            stream=True,
        )
    )

    assert prepared.model_request.max_output_tokens == 8_192
    limits = json.loads(prepared.model_request.metadata["resolved_context_limits"])
    assert limits["input_capacity"] == 32_768 - 8_192
    asyncio.run(service.complete(prepared))
    answered = [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]
    assert answered[-1].max_output_tokens == 8_192


def test_verified_routes_without_a_published_output_keep_input_room():
    # OpenRouter endpoints that publish no max_completion_tokens are stored
    # with their window as the output limit: no separate limit, so the
    # window's share is the default.
    profile = _hosted(
        "openrouter",
        model_descriptors=[
            {
                "id": "author/model-a",
                "context_window": 131_072,
                "route_limits_verified": True,
                "route_limits": [
                    {
                        "provider_name": "only",
                        "context_window": 131_072,
                        "max_input_tokens": 131_072,
                        "max_output_tokens": 131_072,
                        "supported_parameters": ["tools"],
                        "status": 0,
                    }
                ],
            }
        ],
    )

    limits = resolve_context_limits(
        profile, model="author/model-a", required_parameters={"tools"}
    )

    assert limits.route_limits_verified is True
    assert limits.max_output_tokens == 32_768
    assert limits.input_capacity == 131_072 - 32_768


def test_history_past_the_smaller_input_capacity_compacts_instead_of_failing(
    tmp_path, monkeypatch
):
    # 32K window: the default output grows from 2,048 to the model's 8,192,
    # so input capacity drops from 30,720 to 24,576. A conversation that
    # fitted the old capacity but not the new one is compacted, not refused
    # by pre-flight.
    store, engagement, profile, provider, service = _chat_service(
        tmp_path, monkeypatch, window=32_768, output=8_192
    )
    session = store.create(
        ChatSession(
            id="session-output",
            engagement_id=engagement.id,
            title="Long investigation",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    store.create_many(
        [
            ChatMessage(
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=index + 1,
                role=ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT,
                content=f"history-{index} " + "evidence " * 220,
            )
            for index in range(40)
        ]
    )
    history_tokens = estimate_messages(
        [
            ModelMessage(role=message.role.value, content=message.content)
            for message in store.list_entities(ChatMessage, limit=100)
        ]
    )
    assert 24_576 < history_tokens < 30_720

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session.id,
            messages=[{"role": "user", "content": "Summarise what we found."}],
            include_knowledge=False,
            stream=True,
        )
    )

    assert prepared.model_request.max_output_tokens == 8_192
    assert prepared.context_snapshot is not None
    limits = json.loads(prepared.model_request.metadata["resolved_context_limits"])
    assert limits["input_capacity"] == 24_576
    assert estimate_model_request(prepared.model_request) <= limits["input_capacity"]
    asyncio.run(service.complete(prepared))


class _MissionProvider(ModelProvider):
    def __init__(self, provider_id: str) -> None:
        super().__init__(
            ProviderConfig(
                id=provider_id,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                flavor=ProviderFlavor.VLLM,
                base_url="http://127.0.0.1:8000/v1",
                default_model="security-model",
                local=True,
                capabilities=ModelCapabilities(),
            )
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("component construction must not call the model")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def _mission_setup(tmp_path: Path) -> tuple[NebulaStore, Engagement, ProviderProfile]:
    store = NebulaStore(tmp_path / "missions.db")
    engagement = store.create(Engagement(id="eng-mission", name="Mission"))
    scope = store.create(
        ScopePolicy(
            id="scope-mission",
            engagement_id=engagement.id,
            allowed_domains=["example.test"],
        )
    )
    engagement = store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": scope.id, "workspace_path": str(tmp_path)},
        expected_revision=engagement.revision,
    )
    profile = store.create(
        ProviderProfile(
            id="mission-provider",
            name="Lab model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=["security-model"],
            metadata={
                "model_descriptors": [
                    {
                        "id": "security-model",
                        "context_window": 131_072,
                        "max_output_tokens": 32_768,
                    }
                ]
            },
        )
    )
    return store, engagement, profile


def _run(
    engagement: Engagement,
    profile: ProviderProfile,
    *,
    budget: RunBudget | None = None,
    metadata: dict[str, object] | None = None,
) -> AgentRun:
    return AgentRun(
        engagement_id=engagement.id,
        objective="Review the bounded scope",
        supervisor_provider_id=profile.id,
        supervisor_model="security-model",
        budget=budget or RunBudget(),
        metadata=metadata or {},
    )


def test_analysis_mission_specialist_uses_the_model_output_default(tmp_path):
    store, engagement, profile = _mission_setup(tmp_path)
    provider = _MissionProvider(profile.id)
    service = MissionService(store, provider_factory=lambda _: provider)

    unbounded = service._components(_run(engagement, profile), provider)
    specialist = unbounded.specialists[SpecialistRole.SCOPE_PLANNING]
    assert isinstance(specialist, ModelSpecialist)
    assert specialist.max_output_tokens == 32_768

    # A mission token budget smaller than the allowance still bounds it.
    bounded = service._components(
        _run(engagement, profile, budget=RunBudget(max_tokens=5_000)), provider
    )
    assert bounded.specialists[SpecialistRole.SCOPE_PLANNING].max_output_tokens == 5_000


def test_browser_mission_specialist_uses_the_model_output_default(tmp_path):
    store, engagement, profile = _mission_setup(tmp_path)
    platform = BrowserAutomationToolPlatform(store, automation=object())  # type: ignore[arg-type]

    components = platform.mission_components(
        _run(engagement, profile, metadata={"tool_names": ["browser.observe"]}),
        _MissionProvider(profile.id),
    )

    specialist = components.specialists[SpecialistRole.NETWORK_SERVICE]
    assert isinstance(specialist, BrokeredToolSpecialist)
    assert specialist.max_output_tokens == 32_768


def test_automation_mission_specialist_uses_the_model_output_default(tmp_path):
    store, engagement, profile = _mission_setup(tmp_path)
    platform = AutomationToolPlatform(
        manager=None,  # type: ignore[arg-type]
        store=store,
        artifact_store=None,
        workspace_resolver=lambda _engagement_id: tmp_path,
        browser_automation=object(),  # type: ignore[arg-type]
    )

    components = platform.mission_components(
        _run(
            engagement,
            profile,
            budget=RunBudget(max_tokens=20_000),
            metadata={"tool_names": ["browser.observe"], "browser_autonomy": True},
        ),
        _MissionProvider(profile.id),
    )

    specialist = components.specialists[SpecialistRole.NETWORK_SERVICE]
    assert isinstance(specialist, BrokeredToolSpecialist)
    assert specialist.max_output_tokens == 20_000


def test_mission_output_default_falls_back_without_profile_limits(tmp_path):
    from nebula.v3.context import default_output_tokens

    store, engagement, profile = _mission_setup(tmp_path)

    assert default_output_tokens(store, profile.id, "security-model") == 32_768
    assert (
        default_output_tokens(store, profile.id, "security-model", token_budget=900)
        == 900
    )
    # A provider without a stored profile, or a model without published
    # limits, keeps the 2,048-token fallback.
    assert default_output_tokens(store, "missing-profile", "security-model") == 2_048
    assert default_output_tokens(store, None, "security-model") == 2_048
    assert default_output_tokens(store, profile.id, "other-model") == 2_048


def test_cli_analysis_mission_sends_the_model_output_default(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3-data"
    store = NebulaStore(data_dir / "nebula.db")
    engagement = store.create(Engagement(name="vLLM mission"))
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=["security-model"],
            metadata={
                "model_descriptors": [
                    {
                        "id": "security-model",
                        "context_window": 131_072,
                        "max_output_tokens": 32_768,
                    }
                ]
            },
        )
    )
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload["max_tokens"])
        return httpx.Response(
            200,
            json={
                "id": "vllm-request-1",
                "model": "security-model",
                "choices": [
                    {
                        "message": {"content": "Scope is bounded and reviewable."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 5,
                    "total_tokens": 13,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id=profile.id,
            flavor=ProviderFlavor.VLLM,
            default_model="security-model",
            capabilities=ModelCapabilities(),
        ),
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("nebula.v3.cli.provider_from_profile", lambda _: provider)

    result = CliRunner().invoke(
        app,
        [
            "run",
            engagement.id,
            "Review external scope",
            "--provider",
            profile.id,
            "--data-dir",
            str(data_dir),
            "--max-tool-calls",
            "0",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert sent == [32_768]
