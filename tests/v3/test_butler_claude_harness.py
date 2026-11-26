"""Butler as Claude: a Claude harness main agent with Anthropic provider subagents.

Fakes only: no network, no SDK process, no vendor account.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_oversight import ButlerEventPayload, _append_project_event
from nebula.v3.butler_sessions import ButlerSessionReservation, reserve_butler_session
from nebula.v3.chat_assistant_config import chat_assistant_config
from nebula.v3.context import known_model_limits, resolve_context_limits
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    EngagementStatus,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    ProviderCapabilityVerification,
    ProviderProfile,
    utc_now,
)
from nebula.v3.api import create_app
from nebula.v3.chat import ChatConfigurationError
from nebula.v3.harnesses import ClaudeAgentSdkAdapter, HarnessRuntimeService
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import AsyncTestClient as TestClient

CLAUDE_MODEL = "claude-opus-5-5"
HEADERS = {"Authorization": "Bearer test-token"}


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class AssistantMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class ResultMessage:
    session_id = "claude-diagnostic-session"
    usage = {"input_tokens": 3, "output_tokens": 1}
    is_error = False
    result = "OK"


class _Options:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Client:
    opened: list["_Client"] = []

    def __init__(self, *, options: _Options) -> None:
        self.options = options
        self.pending: asyncio.Queue[str] = asyncio.Queue()
        _Client.opened.append(self)

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.pending.put_nowait(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            await self.pending.get()
            yield AssistantMessage([_TextBlock("OK")])
            yield ResultMessage()

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": []}

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _fake_sdk() -> SimpleNamespace:
    return SimpleNamespace(
        __version__="0.2.118",
        ClaudeAgentOptions=_Options,
        ClaudeSDKClient=_Client,
    )


def _claude_harness(**updates: Any) -> HarnessProfile:
    values: dict[str, Any] = {
        "name": "Claude",
        "kind": HarnessKind.CLAUDE_AGENT_SDK,
        "default_model": CLAUDE_MODEL,
        "privacy": {
            "permits_sensitive_data": True,
            "auto_share_tool_results": True,
        },
        "capabilities": HarnessCapabilities(models=[CLAUDE_MODEL, "sonnet", "opus"]),
    }
    values.update(updates)
    return HarnessProfile(**values)


def _anthropic(*, verified: bool = True) -> ProviderProfile:
    return ProviderProfile(
        name="Anthropic",
        provider_type="anthropic",
        model_allowlist=[CLAUDE_MODEL],
        privacy={"permits_sensitive_data": True, "auto_share_tool_results": True},
        capability_verifications=(
            {
                CLAUDE_MODEL: ProviderCapabilityVerification(
                    model=CLAUDE_MODEL, status="verified"
                )
            }
            if verified
            else {}
        ),
        metadata={"default_model": CLAUDE_MODEL},
    )


def test_claude_opus_5_5_limits_resolve_for_provider_and_hosted_ids():
    assert known_model_limits(CLAUDE_MODEL) == (1_000_000, 128_000)
    for alias in (
        "anthropic/claude-opus-5.5",
        "claude-opus-5-5[1m]",
        "us.anthropic.claude-opus-5-5-v1:0",
    ):
        assert known_model_limits(alias) == (1_000_000, 128_000)
    limits = resolve_context_limits(_anthropic(), model=CLAUDE_MODEL)
    assert limits.context_window == 1_000_000
    assert limits.max_output_tokens == 128_000


async def test_claude_harness_profile_is_created_checked_and_turn_verified(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    _Client.opened.clear()
    store = NebulaStore(tmp_path / "claude-profile.db")
    credentials = CredentialStore()
    reference = credentials.create(
        CredentialCreateRequest(
            secret=SecretStr("anthropic-fixture-secret"), persistence="session"
        )
    ).reference
    app = create_app(store, auth_token="test-token", credential_store=credentials)
    client = TestClient(app)

    created = await client.post(
        "/api/v1/harnesses",
        json={
            "name": "Claude",
            "kind": "claude_agent_sdk",
            "auth_mode": "secret_ref",
            "secret_ref": reference,
            "default_model": CLAUDE_MODEL,
            "privacy": {
                "permits_sensitive_data": True,
                "auto_share_tool_results": True,
            },
        },
        headers=HEADERS,
    )
    assert created.status_code in {200, 201}, created.text
    profile_id = created.json()["id"]

    health = await client.post(
        f"/api/v1/harnesses/{profile_id}/health", headers=HEADERS
    )
    assert health.status_code == 200, health.text
    assert health.json()["healthy"] is True
    profile = await store.get(HarnessProfile, profile_id)
    # The probe keeps the operator's exact model first and as the default.
    assert profile.default_model == CLAUDE_MODEL
    assert profile.capabilities.models[0] == CLAUDE_MODEL

    turn = await client.post(
        f"/api/v1/harnesses/{profile_id}/test-turn", headers=HEADERS
    )
    assert turn.status_code == 200, turn.text
    assert turn.json()["turn_state"] == "verified", turn.json()
    assert turn.json()["session_state"] == "verified", turn.json()
    options = _Client.opened[0].options.kwargs
    assert options["model"] == CLAUDE_MODEL
    # The browser-attached credential reaches only the SDK's environment.
    assert options["env"]["ANTHROPIC_API_KEY"] == "anthropic-fixture-secret"
    assert options["tools"] == []
    profile = await store.get(HarnessProfile, profile_id)
    assert {"session_create", "model_turn", "session_resume"} <= set(
        profile.capabilities.exercised_capabilities
    )


async def test_claude_probe_advertises_exact_model_without_a_saved_default(
    monkeypatch,
):
    """Settings offers only probed models, so a new profile must see the id."""

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    health = await ClaudeAgentSdkAdapter().probe(
        HarnessProfile(
            id="claude-new", name="Claude", kind=HarnessKind.CLAUDE_AGENT_SDK
        ),
        CredentialStore(),
    )
    assert health.healthy is True
    assert CLAUDE_MODEL in health.capabilities.models


async def test_probed_claude_profile_accepts_an_explicit_reasoning_effort(
    tmp_path, monkeypatch
):
    """Butler's goal creation names an effort; a probed Claude profile takes it.

    Prod refused every Butler goal on Claude with "reasoning effort 'high' is
    not advertised for 'claude-opus-5-5'" because the probe listed no efforts.
    """

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    store = NebulaStore(tmp_path / "claude-effort.db")
    project = await store.create(Engagement(name="Selected project"))
    health = await ClaudeAgentSdkAdapter().probe(_claude_harness(), CredentialStore())
    profile = await store.create(_claude_harness(capabilities=health.capabilities))

    session = await _runtime(store, tmp_path).create_session(
        engagement_id=project.id,
        profile_id=profile.id,
        model=CLAUDE_MODEL,
        reasoning_effort="high",
    )

    assert session.metadata["runtime_options"]["reasoning_effort"] == "high"


def _runtime(store: NebulaStore, tmp_path) -> HarnessRuntimeService:
    return HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )


async def test_butler_reserves_claude_harness_with_anthropic_provider_subagents(
    tmp_path,
):
    store = NebulaStore(tmp_path / "claude-reservation.db")
    project = await store.create(Engagement(name="Selected project"))
    harness = await store.create(_claude_harness())
    provider = await store.create(_anthropic())

    session = await reserve_butler_session(
        store,
        _runtime(store, tmp_path),
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=harness.id,
            model=CLAUDE_MODEL,
            subagent_provider_id=provider.id,
            subagent_model=CLAUDE_MODEL,
            max_active_subagents=3,
            allow_cloud_tool_results=True,
        ),
    )

    assert session.backend == ChatBackend.HARNESS
    assert session.harness_profile_id == harness.id
    assert session.model == CLAUDE_MODEL
    assert session.metadata["provider_subagent"] == {
        "provider_profile_id": provider.id,
        "model": CLAUDE_MODEL,
        "max_active": 3,
    }
    vendor = await store.get(HarnessSession, session.harness_session_id)
    assert vendor.model == CLAUDE_MODEL
    # Unattended checks read only explicit choices; this one is explicit.
    config = await chat_assistant_config(store, session, butler_default=False)
    assert config["backend"] == "harness"
    assert config["allow_subagents"] is True
    assert config["subagent_provider_id"] == provider.id
    assert config["subagent_harness_profile_id"] is None
    assert config["subagent_model"] == CLAUDE_MODEL


async def test_reservation_refuses_unverified_or_unlisted_subagent_models(tmp_path):
    store = NebulaStore(tmp_path / "claude-reservation-refusals.db")
    project = await store.create(Engagement(name="Selected project"))
    harness = await store.create(_claude_harness())
    unverified = await store.create(_anthropic(verified=False))

    def request(provider_id: str, model: str) -> ButlerSessionReservation:
        return ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=harness.id,
            model=CLAUDE_MODEL,
            subagent_provider_id=provider_id,
            subagent_model=model,
            allow_cloud_tool_results=True,
        )

    with pytest.raises(ChatConfigurationError, match="tool check"):
        await reserve_butler_session(
            store, _runtime(store, tmp_path), request(unverified.id, CLAUDE_MODEL)
        )
    with pytest.raises(ChatConfigurationError, match="listed models"):
        await reserve_butler_session(
            store, _runtime(store, tmp_path), request(unverified.id, "other-model")
        )
    assert await store.list_entities(ChatSession, engagement_id=project.id) == []
    with pytest.raises(ValueError, match="both a runtime and a model"):
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=harness.id,
            subagent_provider_id=unverified.id,
        )
    with pytest.raises(ValueError, match="Assistant settings"):
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.PROVIDER,
            provider_id=unverified.id,
            subagent_provider_id=unverified.id,
            subagent_model=CLAUDE_MODEL,
        )


async def test_reservation_api_accepts_harness_main_with_provider_subagents(tmp_path):
    store = NebulaStore(tmp_path / "claude-reservation-api.db")
    project = await store.create(Engagement(name="Selected project"))
    harness = await store.create(_claude_harness())
    provider = await store.create(_anthropic())
    client = TestClient(create_app(store, auth_token="test-token"))

    created = await client.post(
        "/api/v1/chat/control-assistant/session",
        json={
            "engagement_id": project.id,
            "backend": "harness",
            "harness_profile_id": harness.id,
            "model": CLAUDE_MODEL,
            "subagent_provider_id": provider.id,
            "subagent_model": CLAUDE_MODEL,
            "allow_cloud_tool_results": True,
        },
        headers=HEADERS,
    )

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["harness_profile_id"] == harness.id
    assert body["model"] == CLAUDE_MODEL
    assert body["metadata"]["provider_subagent"]["provider_profile_id"] == provider.id


async def _recovery_case(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    selected = await store.create(
        Engagement(name="Selected", status=EngagementStatus.ACTIVE)
    )
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    own_chat = await store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Research agent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    ordinary = await ButlerCapabilities.create(
        store, artifacts, selected.id, work=app.state.control_assistant_work
    )
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(selected.id), engagement_id=selected.id, authority="act"
        )
    )
    folder = tmp_path / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_text("Recover paused goals with evidence.")
    digest = read_butler_instructions(tmp_path).sha256
    async with store.transaction(immediate=True) as transaction:
        event = await _append_project_event(
            transaction,
            engagement_id=selected.id,
            event_type="butler.global_periodic_check",
            payload=ButlerEventPayload(
                project_id=selected.id,
                source="core",
                actor_role="core",
                summary="Global work check",
                metrics={"instructions_sha256": digest},
                verification_state="core_observed",
            ),
            idempotency_key="claude-recovery-goal-case",
            now=utc_now(),
        )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id=event.id,
            source_event_sequence=event.sequence,
            authority_revision=policy.revision,
            status="investigating",
            summary="Global work check",
        )
    )
    source = await store.create(
        ChatGoal(
            engagement_id=selected.id,
            session_id=own_chat.id,
            objective="Continue existing work",
            completion_criteria=["Report result"],
            status=ChatGoalStatus.PAUSED,
        )
    )
    actions = ButlerGlobalActions(
        store, ordinary._work, tmp_path, case.id, runtime_local=True
    )
    arguments = {
        "project_id": selected.id,
        "source_goal_id": source.id,
        "expected_project_revision": selected.revision,
        "expected_authority_revision": policy.revision,
        "objective": "Unblock the existing goal",
        "completion_criteria": ["Report the safe resume path"],
        "plan": ["Inspect the paused goal", "Report evidence to Butler"],
        "start": False,
    }
    return store, selected, actions, ordinary, arguments


async def test_recovery_goal_runs_on_butlers_reserved_claude_harness(tmp_path):
    store, selected, actions, ordinary, arguments = await _recovery_case(tmp_path)
    harness = await store.create(_claude_harness(privacy={"local_only": True}))
    provider = await store.create(_anthropic())
    butler = await reserve_butler_session(
        store,
        ordinary._work.harness,
        ButlerSessionReservation(
            engagement_id=selected.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=harness.id,
            model=CLAUDE_MODEL,
            subagent_provider_id=provider.id,
            subagent_model=CLAUDE_MODEL,
        ),
    )
    assert butler.harness_profile_id == harness.id

    async def no_quota(*_args, **_kwargs):
        raise AssertionError("a Claude Butler's recovery goal reads no Codex quota")

    ordinary._work.harness_quota = no_quota

    created = await actions.create_recovery_goal(arguments)

    goal = await store.get(ChatGoal, created["goal"]["id"])
    session = await store.get(ChatSession, goal.session_id)
    assert session.backend == ChatBackend.HARNESS
    assert session.harness_profile_id == harness.id
    assert session.model == CLAUDE_MODEL
    vendor = await store.get(HarnessSession, session.harness_session_id)
    assert vendor.model == CLAUDE_MODEL
    options = vendor.metadata.get("runtime_options", {})
    assert options.get("cyber_access_program") is None
    assert options.get("reasoning_effort") is None


async def test_recovery_goal_keeps_codex_route_when_no_butler_runtime(tmp_path):
    store, _selected, actions, ordinary, arguments = await _recovery_case(tmp_path)
    (await store.create(_claude_harness(privacy={"local_only": True})))

    async def no_quota(*_args, **_kwargs):
        return {"state": "unavailable", "snapshots": []}

    ordinary._work.harness_quota = no_quota
    # Nothing is configured, so the previous Codex-only route applies and a
    # Claude harness that merely exists is not chosen.
    with pytest.raises(ValueError, match="No authorized Codex harness"):
        await actions.create_recovery_goal(arguments)
