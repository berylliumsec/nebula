"""Deterministic vLLM integration plus optional live Qwen/Hermes acceptance.

The HTTP fixture checks Core's adapter, capability probe, broker, and chat loop;
only the opt-in live test checks actual model inference and vLLM's parser.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from nebula.v3.api import _verify_provider_capability
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallStatus,
)
from nebula.v3.policy import PolicyEngine
from nebula.v3.providers import build_provider, provider_from_profile
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.sandbox import AnalysisOnlyRunner
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    AnalysisTool,
    StoreToolLedger,
    ToolBroker,
    ToolRegistry,
    ToolSpec,
)


@pytest.fixture
def qwen_http_fixture():
    model = "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"
    nonce = "qwen-fixture-safe-echo"
    requests = []
    responses = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == model
        stage = len(requests)
        if stage == 1:
            assert payload["tool_choice"] == "required"
            assert payload["parallel_tool_calls"] is False
            assert payload["temperature"] == 0
            [tool] = payload["tools"]
            assert tool["function"]["name"] == "nebula_capability_probe"
            arguments = {
                "nonce": tool["function"]["parameters"]["properties"]["nonce"]["enum"][
                    0
                ]
            }
            name = "nebula_capability_probe"
        elif stage == 2:
            assert payload.get("tool_choice", "auto") == "auto"
            echo = next(
                tool["function"]
                for tool in payload["tools"]
                if tool["function"]["name"] == "nebula_safe_echo"
            )
            assert echo["parameters"]["properties"]["nonce"]["enum"] == [nonce]
            name, arguments = "nebula_safe_echo", {"nonce": nonce}
        elif stage == 3:
            assert payload.get("tool_choice", "auto") == "auto"
            name, arguments = "finish_response", {}
        elif stage == 4:
            # Synthesis retains declarations for valid history replay while
            # disabling every callable tool.
            assert payload["tool_choice"] == "none"
            assert payload["parallel_tool_calls"] is False
            name, arguments = None, None
        else:
            raise AssertionError(f"Unexpected provider request {stage}")
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"qwen-call-{stage}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            if name is not None
            else {
                "role": "assistant",
                "content": f"The safe runtime echoed {nonce} successfully.",
            }
        )
        body = {
            "id": f"qwen-response-{stage}",
            "model": model,
            "choices": [
                {
                    "message": message,
                    "finish_reason": "tool_calls" if name else "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        }
        responses.append(body)
        if stage == 4:
            assert payload["stream"] is True
            frames = [
                {
                    "id": body["id"],
                    "model": model,
                    "choices": [{"delta": message}],
                },
                {
                    "id": body["id"],
                    "model": model,
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                },
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
                + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json=body)

    return SimpleNamespace(
        model=model,
        nonce=nonce,
        requests=requests,
        responses=responses,
        transport=httpx.MockTransport(handler),
    )


async def test_qwen_fixture_required_probe_safe_call_and_final_response(
    tmp_path, qwen_http_fixture
):
    fixture = qwen_http_fixture
    store = NebulaStore(tmp_path / "qwen-fixture.db")
    project = await store.create(Engagement(name="Qwen fixture"))
    profile = await store.create(
        ProviderProfile(
            name="Fixture vLLM",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=[fixture.model],
            metadata={"default_model": fixture.model},
        )
    )

    def provider_factory(candidate):
        return build_provider(
            provider_from_profile(candidate).config, transport=fixture.transport
        )

    executed = []

    async def echo(arguments):
        executed.append(arguments)
        return {"echo": arguments["nonce"], "status": "complete"}

    spec = ToolSpec(
        name="nebula_safe_echo",
        description="Echo a harmless verification nonce.",
        input_schema={
            "type": "object",
            "properties": {"nonce": {"type": "string", "enum": [fixture.nonce]}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    registry = ToolRegistry()
    registry.register(AnalysisTool(spec, echo))
    components = RuntimeToolComponents(
        broker=ToolBroker(
            registry=registry,
            policy_engine=PolicyEngine(),
            runner=AnalysisOnlyRunner(),
            ledger=StoreToolLedger(store, enforce_run_budget=False),
            workspace_resolver=lambda _: tmp_path,
        ),
        scope=(
            await store.create(
                ScopePolicy(engagement_id=project.id, on_demand_tools=False)
            )
        ),
        workspace=tmp_path,
        specs={spec.name: spec},
    )
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Qwen fixture acceptance",
            provider_profile_id=profile.id,
            model=fixture.model,
            metadata={"initial_title_state": "generated"},
        )
    )
    service = ChatService(
        store,
        provider_factory=provider_factory,
        automation_tool_platform=SimpleNamespace(
            chat_components=lambda **_: components
        ),
        workspace_resolver=lambda _: tmp_path,
    )

    async def scenario():
        verification = await _verify_provider_capability(
            store, profile, fixture.model, provider_factory
        )
        assert verification.verification.status == ProviderVerificationStatus.VERIFIED
        assert (await store.get(ProviderProfile, profile.id)).tools_verified_for(
            fixture.model
        )
        prepared = await service.prepare_async(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=project.id,
                session_id=session.id,
                model=fixture.model,
                messages=[
                    {
                        "role": "user",
                        "content": f"Verify the local tool runtime with {fixture.nonce}.",
                    }
                ],
                tools_enabled=True,
                allowed_tool_names=[spec.name],
                include_knowledge=False,
                allow_agent_messaging=False,
                planning_enabled=False,
                temperature=0,
            )
        )
        return await service.complete(prepared)

    completion = await scenario()

    assert len(fixture.requests) == 4
    message = fixture.responses[1]["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    assert len(calls) == 1
    call = calls[0]
    assert call["function"]["name"] == "nebula_safe_echo"
    assert json.loads(call["function"]["arguments"]) == {"nonce": fixture.nonce}
    assert executed == [{"nonce": fixture.nonce}]
    [stored_call] = await store.list_entities(ToolCall)
    assert stored_call.tool_name == "nebula_safe_echo"
    assert stored_call.status == ToolCallStatus.COMPLETE
    assert stored_call.result == {"echo": fixture.nonce, "status": "complete"}
    [turn] = await store.list_entities(ChatTurn)
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.execution_tool_calls == 1

    for payload in fixture.requests[2:]:
        replayed = next(
            item for item in payload["messages"] if item.get("tool_calls") == calls
        )
        index = payload["messages"].index(replayed)
        result = payload["messages"][index + 1]
        assert result["role"] == "tool"
        assert result["tool_call_id"] == call["id"]
        assert json.loads(result["content"]) == {
            "echo": fixture.nonce,
            "status": "complete",
        }
    final_message = fixture.responses[-1]["choices"][0]["message"]
    assert final_message.get("content", "").strip()
    assert not final_message.get("tool_calls")
    assert completion.message.content == final_message["content"]


@pytest.mark.skipif(
    not os.getenv("NEBULA_LIVE_QWEN_URL"),
    reason="set NEBULA_LIVE_QWEN_URL to run the live vLLM acceptance",
)
def test_live_qwen_required_probe_safe_call_and_final_response():
    base_url = os.environ["NEBULA_LIVE_QWEN_URL"].rstrip("/")
    model = os.getenv("NEBULA_LIVE_QWEN_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ")
    nonce = uuid4().hex
    tool = {
        "type": "function",
        "function": {
            "name": "nebula_safe_echo",
            "description": "Echo a harmless verification nonce.",
            "parameters": {
                "type": "object",
                "properties": {"nonce": {"type": "string", "enum": [nonce]}},
                "required": ["nonce"],
                "additionalProperties": False,
            },
        },
    }
    with httpx.Client(base_url=base_url, timeout=120) as client:
        probe = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Call the supplied function exactly once; no prose.",
                    }
                ],
                "tools": [tool],
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "temperature": 0,
            },
        )
        probe.raise_for_status()
        message = probe.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        assert len(calls) == 1
        call = calls[0]
        assert call["function"]["name"] == "nebula_safe_echo"
        assert json.loads(call["function"]["arguments"]) == {"nonce": nonce}

        # This is the harmless synthetic broker result. The final request uses
        # a valid assistant-call/tool-result pair and exposes no callable tools.
        safe_result = json.dumps({"echo": nonce, "status": "complete"})
        final = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Summarize the safe synthetic result in one sentence.",
                    },
                    {
                        "role": "user",
                        "content": "Verify the local tool runtime.",
                    },
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": calls,
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": safe_result,
                    },
                ],
                "temperature": 0,
            },
        )
        final.raise_for_status()
        final_message = final.json()["choices"][0]["message"]
        assert final_message.get("content", "").strip()
        assert not final_message.get("tool_calls")


pytestmark = pytest.mark.anyio
