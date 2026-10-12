"""Account figures are vendor-scoped and local turn counts are never a balance."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentAttempt,
    AgentRun,
    ChatTokenUsage,
    ChatTurn,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    ProviderProfile,
)
from nebula.v3.harnesses import CodexAppServerAdapter
from nebula.v3.runtime_usage import (
    parse_codex_usage,
    parse_deepseek_usage,
    parse_openrouter_usage,
)
from nebula.v3.storage import NebulaStore


def test_vendor_payloads_keep_key_account_and_window_scopes_separate():
    router = parse_openrouter_usage(
        {"data": {"usage": 25.5, "limit": 100, "limit_remaining": 74.5, "limit_reset": "monthly"}}
    )
    assert router.source == "OpenRouter key"
    assert router.balances[0].remaining == 74.5
    assert "not the account" in router.detail
    deepseek = parse_deepseek_usage({
        "is_available": True,
        "balance_infos": [
            {"currency": "USD", "total_balance": "12.40"},
            {"currency": "CNY", "total_balance": "86.00"},
        ],
    })
    assert [(row.currency, row.remaining) for row in deepseek.balances] == [
        ("USD", 12.4), ("CNY", 86.0)
    ]
    codex = parse_codex_usage({
        "ordinaryUsageAllowed": False,
        "rateLimits": {
            "credits": {"balance": "7.25", "hasCredits": True, "unlimited": False},
            "primary": {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
            "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 1_800_100_000},
        },
    })
    assert codex.balances[0].remaining == 7.25
    assert [window.used_percent for window in codex.windows] == [62, 20]
    assert codex.ordinary_usage_allowed is False
    assert codex.windows[0].resets_at is not None


def test_codex_account_read_sends_no_model_turn(monkeypatch, tmp_path):
    class Rpc:
        def __init__(self):
            self.calls = []
            self.closed = False

        async def request(self, method, params):
            self.calls.append((method, params))
            return {"rateLimits": {"primary": {"usedPercent": 10}}}

        async def close(self):
            self.closed = True

    rpc = Rpc()
    adapter = CodexAppServerAdapter()

    async def connect(*args, **kwargs):
        return rpc

    async def initialize(*args, **kwargs):
        return {}

    monkeypatch.setattr(adapter, "_connect", connect)
    monkeypatch.setattr(adapter, "_initialize", initialize)
    profile = HarnessProfile(id="codex", name="Codex", kind=HarnessKind.CODEX_APP_SERVER, executable="/bin/true")
    result = asyncio.run(adapter.read_account_usage(profile, object()))
    assert result["rateLimits"]["primary"]["usedPercent"] == 10
    assert rpc.calls == [("account/rateLimits/read", {"excludeResetCreditDetails": True})]
    assert rpc.closed


def test_runtime_usage_api_tracks_turns_and_refreshes_balance_without_leaking_key(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_USAGE_TEST_KEY", "secret-for-test")
    store = NebulaStore(tmp_path / "nebula.db")
    store.create(ProviderProfile(
        id="router", name="OpenRouter", provider_type="openrouter",
        endpoint="https://openrouter.ai/api/v1", secret_ref="env:NEBULA_USAGE_TEST_KEY",
    ))
    store.create(ProviderProfile(id="other", name="Other", provider_type="openai", endpoint="https://api.openai.com"))
    store.create(ProviderProfile(id="bad", name="Bad endpoint", provider_type="openrouter", endpoint="https://[::", secret_ref="env:NEBULA_USAGE_TEST_KEY"))
    store.create(ChatTurn(
        id="provider-turn", engagement_id="project", session_id="chat", provider_profile_id="router", model="model",
        usage=ChatTokenUsage(input_tokens=120, output_tokens=30, total_tokens=150, cached_input_tokens=40),
    ))
    store.create(AgentRun(id="usage-run", engagement_id="project", objective="Inspect", supervisor_provider_id="router"))
    store.create(AgentAttempt(
        id="usage-attempt", engagement_id="project", run_id="usage-run", task_id="usage-task",
        agent_role="scope_planning", attempt_number=1, tokens_used=42,
    ))
    store.create(ContextSnapshot(
        id="usage-context", engagement_id="project", owner_type=ContextOwnerType.AGENT_RUN,
        owner_id="usage-run", status=ContextSnapshotStatus.FAILED, provider_profile_id="router",
        model="model", prompt_version="test", source_sha256="a" * 64, error="fixture failure",
        usage=ChatTokenUsage(input_tokens=10, output_tokens=2, total_tokens=12),
    ))
    store.create(HarnessProfile(id="grok", name="Grok", kind=HarnessKind.GROK_ACP, executable="/bin/true"))
    store.create(HarnessSession(id="harness-session", engagement_id="project", harness_profile_id="grok", model="model"))
    store.create(HarnessTurn(
        id="harness-turn", engagement_id="project", harness_session_id="harness-session",
        origin=HarnessTurnOrigin.ANALYSIS, prompt="Inspect", usage=ChatTokenUsage(input_tokens=70, output_tokens=8, total_tokens=78),
    ))
    calls = []

    def vendor(request):
        calls.append(request)
        assert request.url == httpx.URL("https://openrouter.ai/api/v1/key")
        assert request.headers["Authorization"] == "Bearer secret-for-test"
        return httpx.Response(200, json={"data": {"usage": 4, "limit": 20, "limit_remaining": 16}})

    client = TestClient(create_app(
        store, auth_token="test-token", runtime_usage_transport=httpx.MockTransport(vendor)
    ))
    assert client.get("/api/v1/runtime-usage").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    first = client.get("/api/v1/runtime-usage", headers=headers)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    items = {item["profile_id"]: item for item in first.json()["items"]}
    assert items["router"]["recorded"] == {
        "input_tokens": 120, "output_tokens": 30, "total_tokens": 192,
        "cached_input_tokens": 40, "turns": 2,
        "scope": "Nebula chat turns, native mission attempts and context compactions",
    }
    assert items["router"]["vendor"]["balances"][0]["remaining"] == 16
    assert items["grok"]["recorded"]["total_tokens"] == 78
    assert items["grok"]["vendor"]["state"] == "unavailable"
    assert items["other"]["vendor"]["state"] == "unavailable"
    assert items["bad"]["vendor"]["state"] == "unavailable"
    assert "secret-for-test" not in first.text
    assert len(calls) == 1
    client.get("/api/v1/runtime-usage", headers=headers)
    assert len(calls) == 1
    client.get("/api/v1/runtime-usage?refresh=true", headers=headers)
    assert len(calls) == 2
    store.delete(ProviderProfile, "other")
    after_delete = client.get("/api/v1/runtime-usage", headers=headers)
    assert {item["profile_id"] for item in after_delete.json()["items"]} == {"router", "grok", "bad"}
