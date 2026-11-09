"""Canonical host usage/diagnostic routes with inert account and adapter IO."""

import asyncio
from datetime import timedelta
import hashlib
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.database import MemoryJobRow, ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.domain import (
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    PairedDeviceSession,
    ProviderProfile,
    utc_now,
)
from nebula.v3.runtime_host_settings import is_host_settings_route
from tests.v3.test_runtime_host_settings import MemoryKeyring, settings as settings
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def profile(settings, *, enabled=True, model="synthetic"):
    return await settings.store.create(
        HarnessProfile(
            id="diagnostic-profile",
            name="Inert diagnostic harness",
            kind="codex_app_server",
            executable="/inert/codex",
            enabled=enabled,
            default_model=model,
            capabilities=HarnessCapabilities(
                authentication_state="verified", models=[model] if model else []
            ),
        )
    )


def adapter(settings, *, mode="normal"):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()

    class Connection:
        external_session_id = "inert-protected-native-session"

        async def run_turn(self, prompt, *, model):
            calls.append(("turn", prompt, model))
            entered.set()
            await release.wait()
            yield SimpleNamespace(
                type="tool_started" if mode == "tool" else "completed",
                item_kind=None,
                message="x" * 5000 if mode == "large" else "OK",
                delta=None,
            )

        async def close(self):
            calls.append(("close",))

    class Adapter:
        async def open(self, request):
            pytest.fail("A host diagnostic fell back to unprotected native launch")

        async def open_memory_managed(self, request, controller):
            assert controller is settings.host.observations.memory_compute
            assert request.credential_store is settings.host.credentials
            assert request.mcp_profiles == () and request.gateway_config == {}
            assert request.profile.native_capabilities == HarnessNativeCapabilities()
            calls.append(("open", request.session.external_session_id))
            return Connection()

    settings.host.harness_factory = lambda _kind: Adapter()
    return calls, entered, release


async def test_host_usage_uses_canonical_credentials_refresh_cache_and_recorded_tokens(
    settings,
):
    first = settings.host.credentials.create(
        CredentialCreateRequest(
            secret=SecretStr("synthetic-account-one"), persistence="session"
        )
    )
    provider = await settings.store.create(
        ProviderProfile(
            id="account",
            name="Read-only account",
            provider_type="openrouter",
            endpoint="https://openrouter.ai/api/v1",
            secret_ref=first.reference,
            model_allowlist=["synthetic"],
        )
    )
    project = await settings.store.create(Engagement(name="Recorded usage"))
    session = await settings.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Recorded turn",
            provider_profile_id=provider.id,
            model="synthetic",
        )
    )
    await settings.store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=provider.id,
            model="synthetic",
            usage=ChatTokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
        )
    )
    calls = []

    async def account(request):
        calls.append(
            (request.method, str(request.url), request.headers.get("authorization"))
        )
        return httpx.Response(
            200, json={"data": {"usage": 2, "limit": 10, "limit_remaining": 8}}
        )

    settings.host.usage_transport = httpx.MockTransport(account)
    for refresh in (False, False, True):
        response = await settings.client.get(
            "/api/v1/runtime-usage", params={"refresh": str(refresh).lower()}
        )
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers["cache-control"]
        item = response.json()["items"][0]
        assert (
            item["recorded"]["total_tokens"] == 6
            and item["vendor"]["balances"][0]["remaining"] == 8
        )
        assert (
            "synthetic-account-one" not in response.text
            and first.reference not in response.text
        )
    assert len(calls) == 2 and all(
        value
        == ("GET", "https://openrouter.ai/api/v1/key", "Bearer synthetic-account-one")
        for value in calls
    )
    replacement = CredentialStore(MemoryKeyring())
    second = replacement.create(
        CredentialCreateRequest(
            secret=SecretStr("synthetic-account-two"), persistence="session"
        )
    )
    await settings.store.update(
        ProviderProfile, provider.id, {"secret_ref": second.reference}
    )
    settings.host.credentials = replacement
    response = await settings.client.get("/api/v1/runtime-usage?refresh=true")
    assert (
        response.status_code == 200 and calls[-1][-1] == "Bearer synthetic-account-two"
    )
    assert settings.calls == []
    assert is_host_settings_route("GET", "/api/v1/runtime-usage")


async def test_host_usage_paired_device_auth_and_revocation_without_vendor_or_model(
    settings,
):
    await settings.store.create(
        ProviderProfile(
            id="local",
            name="Local runtime",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["synthetic"],
        )
    )
    now = utc_now()
    await settings.store.create(
        PairedDeviceSession(
            id="usage-viewer",
            name="Usage viewer",
            token_sha256=hashlib.sha256(b"usage-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"usage-csrf").hexdigest(),
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings.root.app), base_url="https://fixture"
    ) as client:
        assert (await client.get("/api/v1/runtime-usage")).status_code == 401
        client.cookies.set("nebula_device", "usage-token")
        response = await client.get("/api/v1/runtime-usage")
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["vendor"]["state"] == "unavailable"
        await settings.store.update(
            PairedDeviceSession, "usage-viewer", {"revoked_at": utc_now()}
        )
        assert (
            await client.get("/api/v1/runtime-usage?refresh=true")
        ).status_code == 401
    assert settings.calls == []


async def test_host_diagnostic_turn_replays_one_protected_turn_without_project_session(
    settings,
):
    projects = {
        item.id: item.revision for item in await settings.store.list_entities(Engagement)
    }
    await profile(settings)
    calls, _, _ = adapter(settings)
    path = "harnesses/diagnostic-profile/test-turn"
    first, retry = await asyncio.gather(
        settings.mutate(path, key="diagnostic-once"),
        settings.mutate(path, key="diagnostic-once"),
    )
    assert first.status_code == retry.status_code == 200, first.text
    assert first.json() == retry.json() and first.json()["healthy"]
    assert first.json()["session_state"] == first.json()["turn_state"] == "verified"
    assert [entry[0] for entry in calls] == ["open", "turn", "close", "open", "close"]
    assert calls[1][1:] == ("Reply with exactly OK. Do not use tools.", "synthetic")
    assert {
        item.id: item.revision for item in await settings.store.list_entities(Engagement)
    } == projects
    for model in (HarnessSession, HarnessTurn, ChatTurn):
        assert await settings.store.count(model) == 0
    async with settings.managed.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
    assert is_host_settings_route("POST", "/api/v1/" + path)


async def test_legacy_diagnostic_entry_reuses_extracted_canonical_helper(settings):
    from nebula.v3.harnesses import HarnessRuntimeService

    await profile(settings)
    calls, _, _ = adapter(settings)

    async def protected_open(selected, request):
        return await selected.open_memory_managed(
            request, settings.host.observations.memory_compute
        )

    legacy = SimpleNamespace(
        store=settings.store,
        credential_store=settings.host.credentials,
        adapter_factory=settings.host.harness_factory,
        _open_memory_managed=protected_open,
    )
    result = await HarnessRuntimeService.test_turn(legacy, "diagnostic-profile")
    assert result.healthy and len([entry for entry in calls if entry[0] == "turn"]) == 1


async def test_slow_host_diagnostic_returns_receipt_and_recovers_without_resubmission(
    settings, monkeypatch
):
    await profile(settings)
    calls, entered, release = adapter(settings)
    release.clear()
    original = settings.host.effect.response

    async def response(*args, **kwargs):
        kwargs.setdefault("wait_seconds", 0.01)
        return await original(*args, **kwargs)

    monkeypatch.setattr(settings.host.effect, "response", response)
    try:
        accepted = await settings.mutate(
            "harnesses/diagnostic-profile/test-turn", key="slow-diagnostic"
        )
        assert accepted.status_code == 202, accepted.text
        await asyncio.wait_for(entered.wait(), 1)
        identity = accepted.headers["X-Nebula-Command-Id"]
        release.set()
        result = await original("operator", identity, wait_seconds=2)
        assert result.status_code == 200
        replay = await settings.mutate(
            "harnesses/diagnostic-profile/test-turn", key="slow-diagnostic"
        )
        assert (
            replay.status_code == 200
            and len([value for value in calls if value[0] == "turn"]) == 1
        )
        async with settings.managed.database.session() as session:
            command = await session.get(RuntimeCommandRow, identity)
            assert command.name == "global.host/v1" and command.state == "succeeded"
    finally:
        release.set()


async def test_host_diagnostic_without_protected_adapter_records_global_memory_refusal(
    settings,
):
    projects = {
        item.id: item.revision for item in await settings.store.list_entities(Engagement)
    }
    await profile(settings)

    class Unsupported:
        async def open(self, request):
            pytest.fail("Unsupported native adapter was launched")

    settings.host.harness_factory = lambda _kind: Unsupported()
    response = await settings.mutate(
        "harnesses/diagnostic-profile/test-turn", key="unsupported-diagnostic"
    )
    assert response.status_code == 200 and not response.json()["healthy"], response.text
    async with settings.managed.database.session() as session:
        jobs = list(await session.scalars(select(MemoryJobRow)))
        assert (
            jobs
            and jobs[-1].project_id is None
            and jobs[-1].resource_kind == "harness_worker"
        )
    assert {
        item.id: item.revision for item in await settings.store.list_entities(Engagement)
    } == projects
    for model in (HarnessSession, HarnessTurn, ChatTurn):
        assert await settings.store.count(model) == 0
    async with settings.managed.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )


async def test_host_diagnostic_requires_key_enabled_profile_and_known_model(settings):
    await profile(settings, enabled=False)
    calls, _, _ = adapter(settings)
    path = "/api/v1/harnesses/diagnostic-profile/test-turn"
    assert (await settings.client.post(path)).status_code == 428
    disabled = await settings.mutate(path.removeprefix("/api/v1/"), key="disabled")
    assert disabled.status_code == 422 and "Enable" in disabled.text
    await settings.store.update(
        HarnessProfile,
        "diagnostic-profile",
        {"enabled": True, "default_model": None, "capabilities": HarnessCapabilities()},
    )
    missing = await settings.mutate(path.removeprefix("/api/v1/"), key="missing-model")
    assert missing.status_code == 422 and "discover a model" in missing.text
    assert calls == []


@pytest.mark.parametrize("mode", ["tool", "large"])
async def test_host_diagnostic_refuses_tools_and_oversized_answers(settings, mode):
    await profile(settings)
    calls, _, _ = adapter(settings, mode=mode)
    response = await settings.mutate(
        "harnesses/diagnostic-profile/test-turn", key="unsafe-" + mode
    )
    assert response.status_code == 200 and not response.json()["healthy"], response.text
    assert len([value for value in calls if value[0] == "turn"]) == 1
    assert calls[-1][0] == "close"
