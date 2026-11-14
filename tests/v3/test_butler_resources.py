"""Observed OpenRouter credits become project evidence and threshold incidents."""

import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import time

import httpx
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_oversight import (
    acknowledge_incident,
    incident_id,
    project_stream_id,
)
from nebula.v3.butler_resources import (
    OpenRouterCreditObserver,
    account_key,
    credit_state_id,
    record_openrouter_credit,
)
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    AgentAttempt,
    AgentRun,
    ButlerAccountCredit,
    ButlerIncident,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.provider_credits import OpenRouterCreditSnapshot
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore


MANAGEMENT_REF = "env:NEBULA_MANAGEMENT_TEST_KEY"


async def _watched_project(
    store: NebulaStore, project_id: str, provider_id: str
) -> None:
    (await store.create(Engagement(id=project_id, name=project_id)))
    (
        await store.create(
            ChatSession(
                id=f"session-{project_id}",
                engagement_id=project_id,
                title="Goal chat",
                provider_profile_id=provider_id,
                model="model",
            )
        )
    )
    goals = ChatGoalService(store)
    draft = await goals.create(
        f"session-{project_id}",
        GoalCreate(objective="Research", completion_criteria=["Evidence"]),
    )
    (
        await goals.write(
            f"session-{project_id}",
            GoalWrite(expected_revision=draft.revision, action="start"),
        )
    )


def _snapshot(provider_id: str, remaining: float) -> OpenRouterCreditSnapshot:
    now = utc_now()
    return OpenRouterCreditSnapshot(
        provider_id=provider_id,
        status="observed",
        checked_at=now,
        observed_at=now,
        total_credits_usd=100,
        total_usage_usd=100 - remaining,
        remaining_usd=remaining,
    )


async def _events(store: NebulaStore, project_id: str) -> list[str]:
    return [
        item.event_type
        for item in (await store.replay_operation_events(project_stream_id(project_id)))
    ]


async def test_low_credit_is_guaranteed_at_boundary_and_deduped_across_projects(
    tmp_path,
):
    store = NebulaStore(tmp_path / "credits.db")
    (
        await store.create(
            ProviderProfile(
                id="openrouter-a",
                name="OpenRouter A",
                provider_type="openrouter",
                openrouter_management_secret_ref=MANAGEMENT_REF,
            )
        )
    )
    (
        await store.create(
            ProviderProfile(
                id="openrouter-b",
                name="OpenRouter B",
                provider_type="openrouter",
                openrouter_management_secret_ref=MANAGEMENT_REF,
            )
        )
    )
    (
        await store.create(
            ProviderProfile(
                id="other-account",
                name="Other account",
                provider_type="openrouter",
                openrouter_management_secret_ref="env:OTHER_MANAGEMENT_KEY",
            )
        )
    )
    (await _watched_project(store, "one", "openrouter-a"))
    (await _watched_project(store, "two", "openrouter-b"))
    (await _watched_project(store, "unrelated", "other-account"))

    first = _snapshot("openrouter-a", 20.0)
    assert (
        await record_openrouter_credit(
            store, snapshot=first, management_ref=MANAGEMENT_REF
        )
    ) == 2
    assert (
        await record_openrouter_credit(
            store, snapshot=first, management_ref=MANAGEMENT_REF
        )
    ) == 0
    key = account_key(MANAGEMENT_REF)
    for project_id in ("one", "two"):
        credit = await store.get(ButlerAccountCredit, credit_state_id(project_id, key))
        assert credit.threshold_state == "at_or_below"
        assert credit.remaining_usd == 20.0
        incident = await store.get(
            ButlerIncident,
            incident_id(project_id, f"openrouter-low-credit:{key}"),
        )
        assert incident is not None and incident.status == "open"
        assert incident.verification_state == "provider_observed"
        assert len(incident.goal_ids) == 1
        assert len(incident.session_ids) == 1
        assert (await _events(store, project_id)) == [
            "goal.started",
            "resource.openrouter_balance_observed",
            "incident.opened",
        ]
    assert (await _events(store, "unrelated")) == ["goal.started"]

    one = await store.get(
        ButlerIncident, incident_id("one", f"openrouter-low-credit:{key}")
    )
    acknowledged = await acknowledge_incident(
        store,
        engagement_id="one",
        record_id=one.id,
        expected_revision=one.revision,
    )
    (
        await record_openrouter_credit(
            store,
            snapshot=_snapshot("openrouter-b", 18.0),
            management_ref=MANAGEMENT_REF,
        )
    )
    assert (await store.get(ButlerIncident, one.id)).status == "acknowledged"
    assert (
        await store.get(ButlerIncident, one.id)
    ).acknowledged_at == acknowledged.acknowledged_at

    (
        await record_openrouter_credit(
            store,
            snapshot=_snapshot("openrouter-b", 21.0),
            management_ref=MANAGEMENT_REF,
        )
    )
    assert (await store.get(ButlerIncident, one.id)).status == "resolved"
    assert (
        await record_openrouter_credit(
            store, snapshot=first, management_ref=MANAGEMENT_REF
        )
    ) == 0
    assert (await store.get(ButlerIncident, one.id)).status == "resolved"
    (
        await record_openrouter_credit(
            store,
            snapshot=_snapshot("openrouter-a", 20.0),
            management_ref=MANAGEMENT_REF,
        )
    )
    reopened = await store.get(ButlerIncident, one.id)
    assert reopened.status == "open" and reopened.acknowledged_at is None
    assert (await _events(store, "one")).count("incident.opened") == 1
    assert (await _events(store, "one")).count("incident.reopened") == 1


async def test_credit_reader_is_coalesced_by_management_ref_and_saved_after_read(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    store = NebulaStore(tmp_path / "credits.db")
    for provider_id, project_id in (("a", "one"), ("b", "two")):
        (
            await store.create(
                ProviderProfile(
                    id=provider_id,
                    name=provider_id,
                    provider_type="openrouter",
                    openrouter_management_secret_ref=MANAGEMENT_REF,
                )
            )
        )
        (await _watched_project(store, project_id, provider_id))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"total_credits": 40, "total_usage": 20}}
        )

    observer = OpenRouterCreditObserver(
        store, CredentialStore(), transport=httpx.MockTransport(handler)
    )

    async def read_both():
        return await asyncio.gather(observer.refresh("a"), observer.refresh("b"))

    snapshots = await read_both()
    assert [item.provider_id for item in snapshots] == ["a", "b"]
    assert len(requests) == 1
    assert (await store.count_entities(ButlerAccountCredit)) == 2
    assert (await _events(store, "one"))[-2:] == [
        "resource.openrouter_balance_observed",
        "incident.opened",
    ]


async def test_provider_setup_schedules_credit_check_and_overview_shows_saved_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    store = NebulaStore(tmp_path / "credits.db")
    (
        await store.create(
            ProviderProfile(
                id="provider", name="OpenRouter", provider_type="openrouter"
            )
        )
    )
    (await _watched_project(store, "project", "provider"))
    app = create_app(
        store=store,
        auth_token="test-token",
        openrouter_credit_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": {"total_credits": 50, "total_usage": 30}}
            )
        ),
    )
    headers = {"Authorization": "Bearer test-token"}
    async with TestClient(app) as client:
        current = await store.get(ProviderProfile, "provider")
        updated = await client.patch(
            "/api/v1/providers/provider",
            headers=headers,
            json={
                "changes": {"openrouter_management_secret_ref": MANAGEMENT_REF},
                "expected_revision": current.revision,
            },
        )
        assert updated.status_code == 200, updated.text
        state_id = credit_state_id("project", account_key(MANAGEMENT_REF))
        deadline = time.monotonic() + 3
        while (await store.find_entity(ButlerAccountCredit, "id", state_id)) is None:
            assert time.monotonic() < deadline, "setup did not persist a credit check"
            time.sleep(0.02)
        overview = await client.get(
            "/api/v1/engagements/project/butler/overview", headers=headers
        )
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["resources"][0]["kind"] == "openrouter_account"
        assert body["resources"][0]["remaining_usd"] == 20.0
        assert body["resources"][0]["threshold_state"] == "at_or_below"
        assert body["summary"]["account_resources"] == 1
        assert body["summary"]["open_incidents"] == 1
        assert body["next_resource_offset"] is None


async def test_durable_provider_usage_calls_credit_observer_without_failing_turn(
    tmp_path,
):
    store = NebulaStore(tmp_path / "credits.db")
    (await store.create(Engagement(id="project", name="Project")))
    (
        await store.create(
            ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
        )
    )
    (
        await store.create(
            ChatSession(
                id="session",
                engagement_id="project",
                title="Chat",
                provider_profile_id="provider",
                model="model",
            )
        )
    )
    turn = await store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
        )
    )
    called = []
    chat = ChatService(store, provider_usage_observer=called.append)
    response = ModelResponse(
        provider_id="provider",
        model="model",
        text="Done",
        usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=4),
    )
    updated = await chat._add_usage(turn, response)
    assert updated.usage.total_tokens == 4
    assert called == ["provider"]


async def test_ordinary_openrouter_completion_checks_credits_after_exchange_commit(
    tmp_path,
):
    class Provider(ModelProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                provider_id=self.config.id,
                model=request.model or "model",
                text="Done",
                usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=4),
            )

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id=self.config.id, healthy=True)

    store = NebulaStore(tmp_path / "ordinary-credits.db")
    (await store.create(Engagement(id="project", name="Project")))
    profile = await store.create(
        ProviderProfile(
            id="provider",
            name="OpenRouter",
            provider_type="openrouter",
            endpoint="https://openrouter.ai/api/v1",
            model_allowlist=["model"],
            metadata={"default_model": "model"},
        )
    )
    (
        await store.create(
            ChatSession(
                id="session",
                engagement_id="project",
                title="Chat",
                provider_profile_id=profile.id,
                model="model",
            )
        )
    )
    provider = Provider(
        ProviderConfig(
            id=profile.id,
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://openrouter.ai/api/v1",
            default_model="model",
            model_allowlist=["model"],
            capabilities=ModelCapabilities(streaming=True),
        )
    )
    observed: list[tuple[str, int]] = []
    scheduled: list[asyncio.Task] = []

    async def read_after_commit(provider_id: str) -> None:
        messages = await store.list_session_entities(ChatMessage, "session")
        observed.append(
            (
                provider_id,
                sum(message.role == ChatRole.ASSISTANT for message in messages),
            )
        )

    def check_after_commit(provider_id: str) -> None:
        # The observer is a synchronous scheduler, like the real credit check.
        scheduled.append(asyncio.ensure_future(read_after_commit(provider_id)))

    chat = ChatService(
        store,
        provider_factory=lambda _: provider,
        provider_usage_observer=check_after_commit,
    )
    prepared = await chat.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id="project",
            session_id="session",
            model="model",
            messages=[{"role": "user", "content": "Reply."}],
            include_knowledge=False,
            stream=False,
        )
    )
    assert prepared.turn is None
    assert prepared.tools_enabled is False
    completion = await chat.complete(prepared)
    assert completion.usage.total_tokens == 4
    await asyncio.gather(*scheduled)
    assert observed == [(profile.id, 1)]


async def test_native_mission_attempt_usage_signals_account_check_after_commit(
    tmp_path,
):
    store = NebulaStore(tmp_path / "mission-credits.db")
    (await store.create(Engagement(id="project", name="Project")))
    (
        await store.create(
            ProviderProfile(
                id="provider", name="OpenRouter", provider_type="openrouter"
            )
        )
    )
    (
        await store.create(
            AgentRun(
                id="run",
                engagement_id="project",
                objective="Investigate",
                supervisor_provider_id="provider",
            )
        )
    )
    attempt = await store.create(
        AgentAttempt(
            id="attempt",
            engagement_id="project",
            run_id="run",
            task_id="task",
            agent_role="specialist",
            attempt_number=1,
        )
    )
    observed = []

    async def record_usage(profile_id):
        observed.append(
            (profile_id, (await store.get(AgentAttempt, attempt.id)).tokens_used)
        )

    store.on_provider_usage_change(record_usage)
    updated = await store.update(AgentAttempt, attempt.id, {"tokens_used": 4})
    assert updated.tokens_used == 4
    assert observed == [("provider", 4)]
    (await store.update(AgentAttempt, attempt.id, {"tokens_used": 4}))
    assert observed == [("provider", 4)]


async def test_use_of_a_provider_without_account_credits_schedules_no_failed_check(
    tmp_path, monkeypatch
):
    """Every provider's use signals the observer; only OpenRouter is read."""

    monkeypatch.setenv("NEBULA_MANAGEMENT_TEST_KEY", "management-only")
    store = NebulaStore(tmp_path / "credits.db")
    (
        await store.create(
            ProviderProfile(
                id="local", name="Local model", provider_type="openai_compatible"
            )
        )
    )
    (
        await store.create(
            ProviderProfile(
                id="openrouter",
                name="OpenRouter",
                provider_type="openrouter",
                openrouter_management_secret_ref=MANAGEMENT_REF,
            )
        )
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"total_credits": 40, "total_usage": 20}}
        )

    observer = OpenRouterCreditObserver(
        store, CredentialStore(), transport=httpx.MockTransport(handler)
    )
    (await observer.startup())
    try:
        # A local model's use, a profile deleted after its use was signaled,
        # and an OpenRouter profile's use.
        for provider_id in ("local", "deleted", "openrouter"):
            observer.schedule(provider_id)
        await asyncio.sleep(0)
        scheduled = list(observer._scheduled)
        assert len(scheduled) == 3
        outcomes = await asyncio.gather(*scheduled, return_exceptions=True)
    finally:
        (await observer.shutdown())

    # Before the fix, the local and deleted profiles' checks failed and were
    # logged as Butler errors after every chat turn.
    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert [o.provider_id for o in outcomes if o is not None] == ["openrouter"]
    assert len(requests) == 1


pytestmark = pytest.mark.anyio
