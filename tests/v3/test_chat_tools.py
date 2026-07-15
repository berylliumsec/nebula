import asyncio
import json
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
                        arguments={"items": ["one", "two"], "cwd": "/tmp"},
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

    observed_arguments = []

    async def handler(arguments):
        observed_arguments.append(arguments)
        return {"count": len(arguments["items"])}

    spec = ToolSpec(
        name="parse.scan",
        description="Count normalized items",
        input_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
            },
            "required": ["items", "cwd"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        risk_class=RiskClass.WORKSPACE_WRITE,
        filesystem_access="workspace_write",
        path_arguments=["cwd"],
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
    assert "finish_response immediately for greetings" in (
        provider.requests[0].instructions or ""
    )
    finish_definition = next(
        tool for tool in provider.requests[0].tools if tool.name == "finish_response"
    )
    assert (
        "questions about the supplied capability list" in finish_definition.description
    )
    routed_cwd = provider.requests[0].tools[0].input_schema["properties"]["cwd"]
    assert routed_cwd == {
        "type": "string",
        "const": ".",
        "description": "Engagement workspace root; supplied by Nebula Core.",
    }
    assert observed_arguments[0]["cwd"] == "/workspace"
    assert provider.requests[1].tool_results[0].output == {"count": 2}
    assert provider.requests[-1].tool_results[0].output == {"count": 2}
    assert provider.requests[-1].tools == []
    assert "bounded Toolbox turn" in (provider.requests[-1].instructions or "")
    assert "no executable tools are available" not in (
        provider.requests[-1].instructions or ""
    )
    assert "closed Markdown fence" in (provider.requests[-1].instructions or "")
    assert "separate reviewed Run action" in (provider.requests[-1].instructions or "")
    assert "report the exact" in (provider.requests[-1].instructions or "")
    assert "invent configuration" in (provider.requests[-1].instructions or "")
    assert "BEGIN TRUSTED ASSIGNED TOOLBOX CAPABILITIES (JSON)" in (
        provider.requests[-1].instructions or ""
    )
    assert '"name":"parse.scan"' in (provider.requests[-1].instructions or "")
    assert "BEGIN TRUSTED NEBULA OPERATOR HELP (JSON)" not in (
        provider.requests[-1].instructions or ""
    )


def test_environment_help_routing_canonicalizes_executable_as_top_level_path():
    manifest_digest = "a" * 64
    spec = ToolSpec(
        name="environment.help",
        description="Return exact command help",
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "command_path": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["tool", "command_path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        pack_id="io.nebula/toolbox@0.1.3",
        manifest_digest=manifest_digest,
        image="example.invalid/nebula/toolbox@sha256:" + "b" * 64,
        executable="/opt/nebula/bin/nebula-toolbox",
    )

    class Catalog:
        def canonical_command_path(self, tool_name, command_path):
            assert tool_name == "nmap"
            assert command_path == ["nmap"]
            return []

    components = SimpleNamespace(
        interface_catalogs_by_manifest={manifest_digest: Catalog()}
    )

    arguments = chat_module._normalize_routing_arguments(
        components,
        spec,
        {"tool": "nmap", "command_path": ["nmap"]},
    )

    assert arguments == {"tool": "nmap", "command_path": []}


def test_environment_help_routing_schema_explains_command_path_semantics():
    spec = SimpleNamespace(
        name="environment.help",
        path_arguments=[],
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "command_path": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    )

    schema = chat_module._routing_input_schema(spec)

    assert (
        "Exact executable inventory name" in schema["properties"]["tool"]["description"]
    )
    assert (
        "use [] for top-level command help"
        in schema["properties"]["command_path"]["description"]
    )


def test_tool_final_synthesis_retrieves_help_from_the_observed_failure(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "chat-tool-help.db")
    scope = store.create(ScopePolicy(engagement_id="eng-a"))
    engagement = store.create(
        Engagement(id="eng-a", name="Tool recovery", scope_policy_id=scope.id)
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
    spec = ToolSpec(
        name="parse.scan",
        description="Attempt a bounded parser",
        input_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
            },
            "required": ["items", "cwd"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        risk_class=RiskClass.LOCAL_READ,
        path_arguments=["cwd"],
    )

    class FailedBroker:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("container runner is unavailable")

    class Platform:
        def chat_components(self, **_kwargs):
            return SimpleNamespace(
                broker=FailedBroker(),
                scope=scope,
                workspace=tmp_path,
                specs={spec.name: spec},
                tool_pack_digests=(),
                interface_catalog_digests=(),
            )

    service = ChatService(store, tool_platform=Platform())
    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            model="model-a",
            messages=[{"role": "user", "content": "Count these items"}],
            tools_enabled=True,
        )
    )

    events = asyncio.run(_collect_stream(service, prepared))
    final_request = provider.requests[-1]
    done = next(payload for event, payload in events if event == "done")

    assert final_request.tool_results[0].is_error is True
    assert "container runner is unavailable" in str(
        final_request.tool_results[0].output
    )
    assert "BEGIN TRUSTED NEBULA OPERATOR HELP (JSON)" in (
        final_request.instructions or ""
    )
    assert "supported fixed executable paths" in (final_request.instructions or "")
    assert done["citations"][0]["source_id"] == "nebula-help:runner-setup"


async def _collect_stream(service, prepared):
    return [item async for item in service.stream(prepared)]


def test_oversized_tool_results_remain_bounded_valid_json():
    rendered = ChatService._bounded_tool_result({"data": "x" * 10_000})

    decoded = json.loads(rendered)

    assert len(rendered) <= 8_000
    assert decoded["status"] == "complete"
    assert decoded["truncated"] is True
    assert decoded["original_characters"] > 8_000
    assert decoded["preview"].startswith('{"data": "')


def test_provider_tool_history_preserves_only_explicit_trusted_results():
    turn = SimpleNamespace(
        tool_history=[
            {
                "model_call_id": "call-1",
                "name": "parse.scan",
                "arguments": {},
                "provider_result": '{"count": 2}',
                "trusted_result": True,
                "status": "complete",
            },
            {
                "model_call_id": "call-2",
                "name": "legacy.tool",
                "arguments": {},
                "provider_result": "legacy non-JSON output",
                "status": "failed",
            },
        ]
    )

    history = ChatService._provider_tool_history(turn)

    assert history[0].output == {"count": 2}
    assert history[0].is_error is False
    assert history[1].output["schema"] == "nebula.tool-result/v2"
    assert history[1].output["incomplete"] is True
    assert "legacy non-JSON output" not in json.dumps(history[1].output)
    assert history[1].is_error is True


def test_nonzero_toolbox_result_is_failed_and_summarizes_observed_error():
    result = SimpleNamespace(
        exit_code=127,
        execution={"timed_out": False},
        output={
            "protocol": "nebula.toolbox/v1",
            "exit_code": 127,
            "stdout": "",
            "stderr": "/bin/bash: missing-tool: command not found\n",
            "timed_out": False,
        },
    )

    assert ChatService._tool_result_failed(result) is True
    assert ChatService._result_summary(result.output) == (
        "Toolbox command failed with exit code 127: "
        "/bin/bash: missing-tool: command not found"
    )
