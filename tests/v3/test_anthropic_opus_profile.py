"""A native Anthropic profile pinned to ``claude-opus-5-5``, end to end offline.

Covers the path such a profile takes: published limits, the exact-model
tool-calling verification probe, and an ordinary reasoning request.  The
Messages API is a fake transport; no network is used.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from typing import Any

import httpx
import pytest

from nebula.v3.context import known_model_limits, resolve_context_limits
from nebula.v3.domain import ProviderProfile, ProviderVerificationStatus
from nebula.v3.provider_settings import _verify_provider_capability
from nebula.v3.providers import (
    AnthropicProvider,
    ModelMessage,
    ModelRequest,
    ModelCapabilities,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolChoice,
    ToolDefinition,
    _claude_always_thinks,
    provider_from_profile,
    rejects_forced_tool_choice,
)
from nebula.v3.storage import NebulaStore

MODEL = "claude-opus-5-5"
KEY_ENV = "NEBULA_TEST_ANTHROPIC_KEY"


class FakeMessages:
    """Records Messages API payloads and answers like the API would.

    It refuses the request shapes the model rejects with HTTP 400, so a
    regression shows up as a failed call rather than a silent payload diff.
    """

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        payload = json.loads(request.content)
        self.payloads.append(payload)
        self.headers.append(request.headers)
        problems = []
        if payload.get("tool_choice", {}).get("type") in {"any", "tool"}:
            problems.append("tool_choice any/tool is not supported for this model")
        thinking = payload.get("thinking") or {}
        if thinking.get("type") in {"disabled", "enabled"}:
            problems.append("thinking must be adaptive on this model")
        if any(key in payload for key in ("temperature", "top_p", "top_k")):
            problems.append("sampling parameters are not supported")
        if payload.get("max_tokens", 0) > 128_000:
            problems.append("max_tokens exceeds the model limit")
        if problems:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "; ".join(problems),
                    },
                },
            )
        content: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": "", "signature": "sig"}
        ]
        stop = "end_turn"
        if payload.get("tools"):
            tool = payload["tools"][0]
            nonce = tool["input_schema"]["properties"]["nonce"]["enum"][0]
            content.append(
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": tool["name"],
                    "input": {"nonce": nonce},
                }
            )
            stop = "tool_use"
        else:
            content.append({"type": "text", "text": "ok"})
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": content,
                "stop_reason": stop,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )


def _profile() -> ProviderProfile:
    return ProviderProfile(
        id="anthropic-opus",
        name="Anthropic Opus",
        provider_type="anthropic",
        endpoint="https://api.anthropic.com",
        secret_ref=f"env:{KEY_ENV}",
        model_allowlist=[MODEL],
        metadata={"default_model": MODEL},
    )


def test_model_limits_and_capability_flags() -> None:
    assert known_model_limits(MODEL) == (1_000_000, 128_000)
    limits = resolve_context_limits(_profile(), model=MODEL)
    assert limits.context_window == 1_000_000
    assert 0 < limits.max_output_tokens <= 128_000
    assert rejects_forced_tool_choice(MODEL)
    assert _claude_always_thinks(MODEL)
    # Neighbors keep their own documented behavior.
    assert rejects_forced_tool_choice("claude-sonnet-5-5")
    assert not rejects_forced_tool_choice("claude-opus-5")
    assert not _claude_always_thinks("claude-opus-5")
    assert not rejects_forced_tool_choice("claude-haiku-4-5")
    assert rejects_forced_tool_choice("claude-fable-5-1")


async def test_tool_verification_probe_succeeds(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(KEY_ENV, "test-key")
    store = NebulaStore(tmp_path / "providers.db")
    profile = await store.create(_profile())
    fake = FakeMessages()

    def factory(candidate: ProviderProfile) -> Any:
        provider = provider_from_profile(candidate)
        assert isinstance(provider, AnthropicProvider)
        provider._transport = httpx.MockTransport(fake)
        return provider

    result = await _verify_provider_capability(store, profile, MODEL, factory)

    assert result.verification.status == ProviderVerificationStatus.VERIFIED
    assert (await store.get(ProviderProfile, profile.id)).tools_verified_for(MODEL)
    assert len(fake.payloads) == 1
    payload = fake.payloads[0]
    assert payload["model"] == MODEL
    assert payload["tool_choice"]["type"] == "auto"
    assert payload["tool_choice"]["disable_parallel_tool_use"] is True
    assert "thinking" not in payload or payload["thinking"]["type"] == "adaptive"
    assert payload["output_config"] == {"effort": "low"}
    assert "temperature" not in payload
    assert fake.headers[0]["x-api-key"] == "test-key"
    assert fake.headers[0]["anthropic-version"]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
async def test_reasoning_request_uses_adaptive_thinking(effort: str) -> None:
    fake = FakeMessages()
    provider = AnthropicProvider(
        ProviderConfig(
            id="anthropic",
            kind=ProviderKind.ANTHROPIC,
            flavor=ProviderFlavor.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key_env=KEY_ENV,
            default_model=MODEL,
        ),
        transport=httpx.MockTransport(fake),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(KEY_ENV, "test-key")
        response = await provider.complete(
            ModelRequest(
                model=MODEL,
                messages=[ModelMessage(role="user", content="hello")],
                reasoning_effort=effort,
                temperature=0.2,
                max_output_tokens=64_000,
            )
        )
    assert response.text == "ok"
    payload = fake.payloads[0]
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": effort}
    assert payload["max_tokens"] == 64_000
    assert "temperature" not in payload


async def test_required_tool_choice_with_reasoning_sends_auto() -> None:
    fake = FakeMessages()
    provider = AnthropicProvider(
        ProviderConfig(
            id="anthropic",
            kind=ProviderKind.ANTHROPIC,
            flavor=ProviderFlavor.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key_env=KEY_ENV,
            default_model=MODEL,
            capabilities=ModelCapabilities(tools=True, strict_tools=True),
        ),
        transport=httpx.MockTransport(fake),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(KEY_ENV, "test-key")
        response = await provider.complete(
            ModelRequest(
                model=MODEL,
                messages=[ModelMessage(role="user", content="call it")],
                tools=[
                    ToolDefinition(
                        name="probe",
                        description="Echo a nonce.",
                        input_schema={
                            "type": "object",
                            "properties": {"nonce": {"type": "string", "enum": ["n"]}},
                            "required": ["nonce"],
                            "additionalProperties": False,
                        },
                    )
                ],
                tool_choice=ToolChoice.REQUIRED,
                reasoning_effort="high",
            )
        )
    assert [call.name for call in response.tool_calls] == ["probe"]
    payload = fake.payloads[0]
    assert payload["tool_choice"]["type"] == "auto"
    assert payload["thinking"]["type"] == "adaptive"
    assert payload["output_config"] == {"effort": "high"}
