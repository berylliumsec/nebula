import asyncio
from types import SimpleNamespace

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import (
    ChatMessage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ToolCall,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    AnalysisTool,
    StoreToolLedger,
    ToolBroker,
    ToolRegistry,
    ToolSpec,
)


class RoutingProvider(ModelProvider):
    def __init__(self, provider_id: str):
        super().__init__(
            ProviderConfig(
                id=provider_id,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8001/v1",
                default_model="model-a",
                local=True,
                capabilities=ModelCapabilities(tools=True, strict_tools=True),
            )
        )
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.tools and not request.tool_results:
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                tool_calls=[
                    ToolCall(
                        id="route-1",
                        name="parse.scan",
                        arguments={"items": ["one", "two"]},
                    )
                ],
                finish_reason="tool_calls",
            )
        if request.tools:
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                tool_calls=[
                    ToolCall(id="route-2", name="finish_response", arguments={})
                ],
                finish_reason="tool_calls",
            )
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text="The bounded capability counted two items.",
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


def test_chat_runs_sequential_required_tool_loop_and_persists_final_message(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "chat-tools.db")
    scope = store.create(ScopePolicy(engagement_id="eng-a"))
    engagement = store.create(
        Engagement(id="eng-a", name="Chat tools", scope_policy_id=scope.id)
    )
    profile = store.create(
        ProviderProfile(
            id="provider-a",
            name="Local verified model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8001/v1",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"tool_calling": True, "strict_structured_output": True},
            capability_verifications={
                "model-a": ProviderCapabilityVerification(
                    model="model-a",
                    status=ProviderVerificationStatus.VERIFIED,
                )
            },
            metadata={"default_model": "model-a"},
        )
    )
    provider = RoutingProvider(profile.id)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)

    async def handler(arguments):
        return {"count": len(arguments["items"])}

    spec = ToolSpec(
        name="parse.scan",
        description="Count normalized items",
        input_schema={
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        risk_class=RiskClass.LOCAL_READ,
    )
    registry = ToolRegistry()
    registry.register(AnalysisTool(spec, handler))
    broker = ToolBroker(
        registry=registry,
        policy_engine=PolicyEngine(),
        runner=object(),
        ledger=StoreToolLedger(store),
        workspace_resolver=lambda _: tmp_path,
    )

    class Platform:
        def chat_components(self, **kwargs):
            return SimpleNamespace(
                broker=broker,
                scope=scope,
                workspace=tmp_path,
                specs={spec.name: spec},
                tool_pack_digests=(),
                interface_catalog_digests=(),
            )

    service = ChatService(store, tool_platform=Platform())
    prepared = asyncio.run(
        service.prepare_async(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=engagement.id,
                model="model-a",
                messages=[{"role": "user", "content": "Count these items"}],
                tools_enabled=True,
                stream=True,
            )
        )
    )

    async def collect():
        return [item async for item in service.stream(prepared)]

    events = asyncio.run(collect())
    turn = store.get(ChatTurn, prepared.turn.id)
    messages = store.list_entities(ChatMessage, engagement_id=engagement.id, limit=100)

    assert [event for event, _ in events] == [
        "started",
        "tool_started",
        "tool_completed",
        "delta",
        "done",
    ]
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.next_step == 1
    assert len(turn.tool_call_ids) == 1
    assert messages[-1].role.value == "assistant"
    assert messages[-1].metadata["tool_call_ids"] == turn.tool_call_ids
    assert provider.requests[0].tool_choice.value == "required"
    assert provider.requests[0].parallel_tool_calls is False
    assert provider.requests[-1].tools == []
