"""Public-commit review on Butler's harness profiles, with inert adapters."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from nebula.v3.domain import (
    HarnessCapabilities,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    ProviderPrivacy,
)
from nebula.v3.public_commit_review import PROFILES_ENV, review_public_commit
from nebula.v3.runtime_host_settings import is_host_settings_route
from nebula.v3.settings_assistant_api import _template_is_allowed
from tests.v3.async_test_support import close_database_handles as close_database_handles
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
ROUTE = "harnesses/public-commit-review"
PROMPT = "Review this synthetic staged segment.\nSTAGED_COMMIT_DATA_JSON:\n{}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def reviewer(
    settings,
    profile_id,
    *,
    kind="codex_app_server",
    model="synthetic",
    enabled=True,
    cleared=True,
):
    return await settings.store.create(
        HarnessProfile(
            id=profile_id,
            name=f"Butler reviewer {profile_id}",
            kind=kind,
            executable="/inert/harness",
            home_directory=f"/inert/accounts/{profile_id}",
            enabled=enabled,
            default_model=model,
            privacy=ProviderPrivacy(permits_sensitive_data=cleared),
            capabilities=HarnessCapabilities(
                authentication_state="verified", models=[model] if model else []
            ),
        )
    )


def adapter(settings, replies):
    """Each opened connection answers with the next reply for its profile."""
    calls = []

    class Connection:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        async def run_turn(self, prompt, *, model):
            calls.append(("turn", self.profile_id, prompt, model))
            reply = replies[self.profile_id]
            if isinstance(reply, Exception):
                raise reply
            yield SimpleNamespace(
                type="tool_started" if reply == "TOOL" else "completed",
                item_kind=None,
                message=reply,
                delta=None,
            )

        async def close(self):
            calls.append(("close", self.profile_id))

    class Adapter:
        async def open_memory_managed(self, request, controller):
            assert controller is settings.host.observations.memory_compute
            assert request.credential_store is settings.host.credentials
            assert request.mcp_profiles == () and request.gateway_config == {}
            assert request.profile.native_capabilities == HarnessNativeCapabilities()
            assert request.session.engagement_id == "independent-review"
            calls.append(("open", request.profile.id))
            return Connection(request.profile.id)

        async def open(self, request):
            pytest.fail("A host review bypassed the memory runner")

    settings.host.harness_factory = lambda _kind: Adapter()
    return calls


ALLOW = json.dumps(
    {"decision": "allow", "category": "none", "reviewed_files": ["0001"]}
)


async def test_route_reviews_on_first_configured_profile_tool_free(
    settings, monkeypatch
):
    monkeypatch.setenv(PROFILES_ENV, "codex-reviewer,claude-reviewer")
    await reviewer(settings, "codex-reviewer")
    await reviewer(settings, "claude-reviewer", kind="claude_agent_sdk")
    calls = adapter(settings, {"codex-reviewer": ALLOW, "claude-reviewer": ALLOW})
    assert (
        await settings.client.post("/api/v1/" + ROUTE, json={"prompt": PROMPT})
    ).status_code == 428
    response = await settings.mutate(
        ROUTE, body={"prompt": PROMPT, "schema": {}}, key="review-1"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verdict"] == json.loads(ALLOW)
    assert result["reviewer"]["harness_profile_id"] == "codex-reviewer"
    assert result["reviewer"]["model"] == "synthetic"
    assert [call[:2] for call in calls] == [
        ("open", "codex-reviewer"),
        ("turn", "codex-reviewer"),
        ("close", "codex-reviewer"),
    ]
    prompt = calls[1][2]
    assert prompt.startswith(PROMPT) and '"reviewed_files"' in prompt
    for model in (HarnessSession, HarnessTurn):
        assert await settings.store.count(model) == 0
    assert is_host_settings_route("POST", "/api/v1/" + ROUTE)


@pytest.mark.parametrize(
    "problem", ["disabled", "uncleared", "no_model", "failed", "tool", "prose"]
)
async def test_unavailable_profile_points_to_the_next_one(
    settings, monkeypatch, problem
):
    from nebula.v3.harnesses import HarnessTransportError

    monkeypatch.setenv(PROFILES_ENV, "first,second")
    await reviewer(
        settings,
        "first",
        enabled=problem != "disabled",
        cleared=problem != "uncleared",
        model=None if problem == "no_model" else "synthetic",
    )
    await reviewer(settings, "second")
    first_reply = {
        "failed": HarnessTransportError("synthetic sign-in expired"),
        "tool": "TOOL",
        "prose": "Looks fine to me.",
    }.get(problem, ALLOW)
    adapter(settings, {"first": first_reply, "second": ALLOW})
    first = await settings.mutate(
        ROUTE, body={"prompt": PROMPT}, key=f"first-{problem}"
    )
    assert first.status_code == 200, first.text
    assert "verdict" not in first.json()
    assert first.json()["next_attempt"] == 1 and "first" in first.json()["unavailable"]
    second = await settings.mutate(
        ROUTE, body={"prompt": PROMPT, "attempt": 1}, key=f"second-{problem}"
    )
    assert second.json()["reviewer"]["harness_profile_id"] == "second"


async def test_last_unavailable_profile_ends_the_attempts(settings, monkeypatch):
    monkeypatch.setenv(PROFILES_ENV, "only")
    await reviewer(settings, "only", enabled=False)
    adapter(settings, {})
    response = await settings.mutate(ROUTE, body={"prompt": PROMPT}, key="only")
    assert response.json()["next_attempt"] is None
    past = await settings.mutate(
        ROUTE, body={"prompt": PROMPT, "attempt": 1}, key="past"
    )
    assert past.status_code == 422 and "no_such_attempt" in past.text


async def test_unconfigured_review_is_refused(settings, monkeypatch):
    monkeypatch.delenv(PROFILES_ENV, raising=False)
    monkeypatch.delenv("NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID", raising=False)
    adapter(settings, {})
    response = await settings.mutate(ROUTE, body={"prompt": PROMPT}, key="unconfigured")
    assert response.status_code == 422 and "not_configured" in response.text


async def test_butler_review_harness_is_the_default_profile(settings, monkeypatch):
    monkeypatch.delenv(PROFILES_ENV, raising=False)
    monkeypatch.setenv("NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID", "butler-review")
    await reviewer(settings, "butler-review")
    adapter(settings, {"butler-review": "```json\n" + ALLOW + "\n```"})
    response = await settings.mutate(ROUTE, body={"prompt": PROMPT}, key="default")
    assert response.json()["reviewer"]["harness_profile_id"] == "butler-review"
    assert response.json()["verdict"]["decision"] == "allow"


def test_butler_cannot_reach_the_review_through_its_settings_catalog():
    assert not _template_is_allowed("/api/v1/harnesses/public-commit-review")


async def test_review_timeout_is_unavailable_not_a_verdict():
    async def runner(profile, model, prompt, timeout_seconds):
        assert timeout_seconds <= 120
        raise TimeoutError

    class Store:
        async def get(self, _model, profile_id):
            return HarnessProfile(
                id=profile_id,
                name="slow",
                kind="codex_app_server",
                executable="/inert/codex",
                default_model="synthetic",
                privacy=ProviderPrivacy(local_only=True),
            )

    result = await review_public_commit(
        Store(), PROMPT, runner, environ={PROFILES_ENV: "slow"}
    )
    assert (
        result["unavailable"] == "slow (slow): timed out"
        and result["next_attempt"] is None
    )
    await asyncio.sleep(0)


async def test_kernel_route_reviews_on_the_kernel_harness_runtime(
    tmp_path, monkeypatch
):
    from nebula.v3 import harness_diagnostics
    from nebula.v3.api import create_app
    from nebula.v3.storage import NebulaStore
    from tests.v3.async_test_support import AsyncTestClient as TestClient

    seen = {}

    async def one_shot(
        profile,
        model,
        prompt,
        *,
        credentials,
        adapter_factory,
        open_connection,
        timeout_seconds,
    ):
        seen.update(
            profile=profile.id,
            model=model,
            timeout=timeout_seconds,
            managed=getattr(open_connection, "__name__", ""),
        )
        return ALLOW

    monkeypatch.setattr(harness_diagnostics, "run_harness_one_shot", one_shot)
    monkeypatch.setenv(PROFILES_ENV, "kernel-reviewer")
    store = NebulaStore(tmp_path / "public-review.db")
    await store.create(
        HarnessProfile(
            id="kernel-reviewer",
            name="Kernel reviewer",
            kind="codex_app_server",
            executable="/inert/codex",
            default_model="synthetic",
            privacy=ProviderPrivacy(permits_sensitive_data=True),
        )
    )
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        response = await client.post(
            "/api/v1/harnesses/public-commit-review",
            json={"prompt": PROMPT},
            headers={"Authorization": "Bearer test-token"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["verdict"]["decision"] == "allow"
    assert seen == {
        "profile": "kernel-reviewer",
        "model": "synthetic",
        "timeout": 110,
        "managed": "_open_memory_managed",
    }
