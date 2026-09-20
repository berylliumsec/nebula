import hashlib
import base64
from datetime import datetime, timedelta
import json

import httpx

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.context import resolve_context_limits
from nebula.v3.model_catalog import ModelDescriptor, ModelRouteDescriptor
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    Asset,
    ChatSession,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    NativeHookExecution,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.providers import (
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderFlavor,
    ProviderHealth,
    ToolCall,
    ToolChoice,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.version import __version__


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="test-token",
        allow_internal_event_append=True,
    )
    return TestClient(app), store, artifacts


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_provider_chat_goal_api_persists_explicit_lifecycle(api, tmp_path):
    client, store, _ = api
    workspace = tmp_path / "goal-workspace"
    skill = workspace / ".agents" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Review carefully.", encoding="utf-8")
    store.create(
        Engagement(
            id="goal-project", name="Goal project", workspace_path=str(workspace)
        )
    )
    provider = store.create(
        ProviderProfile(
            id="goal-provider", name="OpenRouter", provider_type="openrouter"
        )
    )
    store.create(
        ChatSession(
            id="goal-session",
            engagement_id="goal-project",
            title="Goal session",
            provider_profile_id=provider.id,
            model="model",
        )
    )

    created = client.post(
        "/api/v1/chat/sessions/goal-session/goal",
        headers=_auth(),
        json={
            "objective": "Finish the bounded task",
            "completion_criteria": ["Focused evidence passes"],
            "plan": ["Inspect", "Validate"],
            "step_budget": 4,
        },
    )
    assert created.status_code == 200, created.text
    goal = created.json()
    assert goal["status"] == ChatGoalStatus.DRAFT.value

    started = client.post(
        "/api/v1/chat/sessions/goal-session/goal/actions",
        headers=_auth(),
        json={"expected_revision": goal["revision"], "action": "start"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == ChatGoalStatus.RUNNING.value
    attached = client.put(
        "/api/v1/chat/sessions/goal-session/goal/skills",
        headers=_auth(),
        json={
            "expected_revision": started.json()["revision"],
            "skills": [{"name": "review", "path": str(skill.resolve())}],
        },
    )
    assert attached.status_code == 200, attached.text
    attached_goal = attached.json()
    assert attached_goal["skill_snapshots"][0]["name"] == "review"
    original_digest = attached_goal["skill_snapshots"][0]["sha256"]
    skill.write_text("Changed after attachment.", encoding="utf-8")
    loaded = client.get("/api/v1/chat/sessions/goal-session/goal", headers=_auth())
    assert loaded.status_code == 200
    assert loaded.json()["revision"] == attached_goal["revision"]
    assert loaded.json()["skill_snapshots"][0]["sha256"] == original_digest
    skill.unlink()
    retained = client.put(
        "/api/v1/chat/sessions/goal-session/goal/skills",
        headers=_auth(),
        json={
            "expected_revision": loaded.json()["revision"],
            "skills": [{"name": "review", "path": str(skill.resolve())}],
        },
    )
    assert retained.status_code == 200, retained.text
    assert retained.json()["skill_snapshots"][0]["sha256"] == original_digest

    removed = client.put(
        "/api/v1/chat/sessions/goal-session/goal/skills",
        headers=_auth(),
        json={"expected_revision": retained.json()["revision"], "skills": []},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["skill_snapshots"] == []


def test_native_skill_catalog_uses_shared_agents_root_not_harness_roots(api, tmp_path):
    client, store, _ = api
    workspace = tmp_path / "linked-project"
    shared = workspace / ".agents" / "skills" / "review" / "SKILL.md"
    codex = workspace / ".codex" / "skills" / "review" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    shared.write_text("shared instructions", encoding="utf-8")
    codex.write_text("codex-only instructions", encoding="utf-8")
    managed = tmp_path / ".agents" / "skills" / "report" / "SKILL.md"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text("managed instructions", encoding="utf-8")
    store.create(
        Engagement(
            id="skill-project",
            name="Skill project",
            workspace_path=str(workspace),
        )
    )

    response = client.get("/api/v1/skills?engagement_id=skill-project", headers=_auth())

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "name": "review",
            "path": str(shared.resolve()),
            "source": "project",
            "root": str((workspace / ".agents" / "skills").resolve()),
        },
        {
            "name": "report",
            "path": str(managed.resolve()),
            "source": "installed",
            "root": str((tmp_path / ".agents" / "skills").resolve()),
        },
    ]
    roots = client.get(
        "/api/v1/skills/catalog?engagement_id=skill-project", headers=_auth()
    )
    assert roots.status_code == 200
    assert roots.json() == {
        "project_root": str((workspace / ".agents" / "skills").resolve()),
        "managed_root": str((tmp_path / ".agents" / "skills").resolve()),
    }


def test_provider_chat_api_runs_selected_native_hooks(api, tmp_path, monkeypatch):
    client, store, _ = api
    workspace = tmp_path / "hook-run-project"
    hook = workspace / ".agents" / "hooks" / "audit"
    hook.mkdir(parents=True)
    executable = hook / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    (hook / "hook.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Audit",
                "events": ["chat.turn.started", "chat.turn.completed"],
                "command": ["run.sh"],
                "side_effects": "none",
                "failure_policy": "continue",
            }
        ),
        encoding="utf-8",
    )
    store.create(
        Engagement(
            id="hook-run-project",
            name="Hook run project",
            workspace_path=str(workspace),
        )
    )
    profile = store.create(
        ProviderProfile(
            id="hook-run-provider",
            name="Local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            privacy={"local_only": True},
            metadata={"default_model": "model-a"},
        )
    )
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

    class ApiHookProvider(ModelProvider):
        def __init__(self, provider_id: str) -> None:
            super().__init__(
                ProviderConfig(
                    id=provider_id,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="http://127.0.0.1:8000/v1",
                    default_model="model-a",
                    model_allowlist=["model-a"],
                    local=True,
                    capabilities=ModelCapabilities(streaming=True),
                )
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                text="Hooked answer",
                usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=4),
                finish_reason="stop",
                provider_request_id="hook-run",
            )

        async def health(self) -> ProviderHealth:
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=["model-a"]
            )

    monkeypatch.setattr(
        "nebula.v3.chat.provider_from_profile", lambda _: ApiHookProvider(profile.id)
    )

    response = client.post(
        "/api/v1/chat/completions",
        headers=_auth(),
        json={
            "provider_id": profile.id,
            "engagement_id": "hook-run-project",
            "hook_ids": ["audit"],
            "messages": [{"role": "user", "content": "Run the audit hook"}],
            "include_knowledge": False,
            "stream": False,
        },
    )
    assert response.status_code == 200, response.text
    executions = client.get(
        f"/api/v1/chat/sessions/{response.json()['session_id']}/hooks",
        headers=_auth(),
    )
    assert executions.status_code == 200, executions.text
    assert [(item["event_name"], item["status"]) for item in executions.json()] == [
        ("chat.turn.started", "complete"),
        ("chat.turn.completed", "complete"),
    ]


def test_native_hook_catalog_uses_only_project_agents_root(api, tmp_path):
    client, store, _ = api
    workspace = tmp_path / "hook-project"
    hook = workspace / ".agents" / "hooks" / "audit"
    hook.mkdir(parents=True)
    executable = hook / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    (hook / "hook.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Audit",
                "events": ["chat.turn.started"],
                "command": ["run.sh"],
                "timeout_seconds": 5,
                "side_effects": "none",
            }
        ),
        encoding="utf-8",
    )
    ignored = workspace / ".codex" / "hooks" / "ignored"
    ignored.mkdir(parents=True)
    store.create(
        Engagement(
            id="hook-project",
            name="Hook project",
            workspace_path=str(workspace),
        )
    )

    response = client.get("/api/v1/hooks?engagement_id=hook-project", headers=_auth())

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == ["audit"]
    assert response.json()[0]["path"] == str(hook.resolve())
    assert response.json()[0]["manifest"]["events"] == ["chat.turn.started"]


def test_native_hook_outcomes_and_reconciliation_are_visible_through_chat_api(api):
    client, store, _ = api
    store.create(Engagement(id="hook-recovery-project", name="Hook recovery"))
    provider = store.create(
        ProviderProfile(id="hook-provider", name="Provider", provider_type="ollama")
    )
    session = store.create(
        ChatSession(
            id="hook-session",
            engagement_id="hook-recovery-project",
            title="Hook session",
            provider_profile_id=provider.id,
            model="model",
        )
    )
    turn = store.create(
        ChatTurn(
            id="hook-turn",
            engagement_id="hook-recovery-project",
            session_id=session.id,
            provider_profile_id=provider.id,
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
            error="Hook outcome is unknown.",
            request_snapshot={
                "recovery": {
                    "required": True,
                    "unknown_tool_call_ids": [],
                    "unknown_hook_execution_ids": ["hook-run"],
                }
            },
        )
    )
    store.create(
        NativeHookExecution(
            id="hook-run",
            engagement_id=turn.engagement_id,
            chat_session_id=session.id,
            chat_turn_id=turn.id,
            hook_id="audit",
            hook_snapshot={},
            event_name="chat.turn.started",
            status="interrupted",
            side_effects="external",
            started_at=utc_now(),
            completed_at=utc_now(),
            error="Core restarted before the hook outcome was known.",
        )
    )

    pending = client.get(
        f"/api/v1/chat/sessions/{session.id}/pending-turn", headers=_auth()
    )
    assert pending.status_code == 200, pending.text
    # The transcript resumes its counter from the turn's own start.
    assert (
        datetime.fromisoformat(pending.json()["started_at"].replace("Z", "+00:00"))
        == turn.created_at
    )
    assert pending.json()["recovery_blocked"] is True
    assert pending.json()["unresolved_hook_execution_ids"] == ["hook-run"]
    outcomes = client.get(f"/api/v1/chat/turns/{turn.id}/hooks", headers=_auth())
    assert outcomes.status_code == 200, outcomes.text
    assert outcomes.json()[0]["hook_id"] == "audit"
    assert "hook_snapshot" not in outcomes.json()[0]

    reconciled = client.post(
        f"/api/v1/chat/turns/{turn.id}/reconcile-hook",
        headers=_auth(),
        json={
            "expected_revision": turn.revision,
            "hook_execution_id": "hook-run",
            "outcome": "complete",
            "detail": "Operator verified the external record.",
        },
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["recovery_blocked"] is False
    assert reconciled.json()["unresolved_hook_execution_ids"] == []


def test_hook_execution_summaries_page_beyond_the_store_cap(api):
    client, store, _ = api
    engagement = store.create(Engagement(id="hook-page-project", name="Hook pages"))
    provider = store.create(
        ProviderProfile(
            id="hook-page-provider", name="Provider", provider_type="ollama"
        )
    )
    session = store.create(
        ChatSession(
            id="hook-page-session",
            engagement_id=engagement.id,
            title="Hook pages",
            provider_profile_id=provider.id,
            model="model",
        )
    )
    decoy = store.create(
        ChatTurn(
            id="hook-page-decoy",
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=provider.id,
            model="model",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    turn = store.create(
        ChatTurn(
            id="hook-page-turn",
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=provider.id,
            model="model",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    started = utc_now()
    store.create_many(
        [
            NativeHookExecution(
                id=f"decoy-hook-{index}",
                engagement_id=engagement.id,
                chat_session_id=session.id,
                chat_turn_id=decoy.id,
                hook_id="noise",
                hook_snapshot={},
                event_name="chat.turn.started",
                status="complete",
                started_at=started,
                completed_at=started,
            )
            for index in range(1_000)
        ]
    )
    store.create(
        NativeHookExecution(
            id="visible-hook",
            engagement_id=engagement.id,
            chat_session_id=session.id,
            chat_turn_id=turn.id,
            hook_id="audit",
            hook_snapshot={},
            event_name="chat.turn.completed",
            status="complete",
            started_at=started,
            completed_at=started,
        )
    )

    response = client.get(f"/api/v1/chat/turns/{turn.id}/hooks", headers=_auth())

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == ["visible-hook"]
    session_hooks = client.get(
        f"/api/v1/chat/sessions/{session.id}/hooks", headers=_auth()
    )
    assert session_hooks.status_code == 200, session_hooks.text
    assert [item["id"] for item in session_hooks.json()] == ["visible-hook"]


def test_runtime_switch_preflight_keeps_incompatible_model_unselected(api):
    client, store, _ = api
    engagement = store.create(Engagement(id="switch-project", name="Switch project"))
    profile = store.create(
        ProviderProfile(
            id="switch-provider",
            name="Local provider",
            provider_type="vllm",
            model_allowlist=["model-a", "model-b"],
            metadata={
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": 32_000,
                        "max_output_tokens": 2_000,
                    },
                    {
                        "id": "model-b",
                        "context_window": 8_000,
                        "max_output_tokens": 1_000,
                    },
                ]
            },
        )
    )
    session = store.create(
        ChatSession(
            id="switch-session",
            engagement_id=engagement.id,
            title="Keep selection",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )

    response = client.post(
        "/api/v1/chat/sessions/switch-session/runtime-switch/preflight",
        headers=_auth(),
        json={
            "provider_id": profile.id,
            "model": "model-b",
            "tools_enabled": True,
            "expected_session_revision": session.revision,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["compatible"] is False
    assert "not verified for the tools" in response.json()["reason"]
    unchanged = store.get(ChatSession, session.id)
    assert unchanged.model == "model-a"
    assert unchanged.revision == session.revision

    stale = client.post(
        "/api/v1/chat/sessions/switch-session/runtime-switch/preflight",
        headers=_auth(),
        json={
            "provider_id": profile.id,
            "model": "model-b",
            "expected_session_revision": session.revision + 1,
        },
    )
    assert stale.status_code == 409, stale.text
    assert "reload" in stale.json()["detail"].lower()


def test_health_and_data_routes_require_auth(api):
    client, _, _ = api
    assert client.get("/api/v1/health").status_code == 401
    assert (
        client.get(
            "/api/v1/health", headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )
    response = client.get("/api/v1/health", headers=_auth())
    assert response.status_code == 200
    assert response.json()["journal_mode"] == "wal"
    assert response.json()["human_pty"] == "unavailable"
    assert response.json()["container_terminal"] == "unavailable"
    assert response.json()["version"] == __version__
    assert {
        "commit",
        "target",
        "build_timestamp",
        "distribution_channel",
    } <= response.json().keys()
    assert client.get("/api/v1/engagements").status_code == 401
    assert client.get("/api/v1/engagements", headers=_auth()).status_code == 200
    catalog = client.get("/api/v1/provider-catalog", headers=_auth())
    assert catalog.status_code == 200
    assert any(
        item["flavor"] == "vllm" and item["local"] is True for item in catalog.json()
    )
    assert any(
        item["flavor"] == "orcarouter"
        and item["display_name"] == "OrcaRouter"
        and item["default_base_url"] == "https://api.orcarouter.ai/v1"
        and item["suggested_key_env"] == "ORCAROUTER_API_KEY"
        for item in catalog.json()
    )


def test_action_resolution_endpoint_uses_core_resource_and_device_authority(api):
    client, store, _ = api
    project = store.create(Engagement(name="Action project"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    response = client.post(
        "/api/v1/actions/resolve",
        headers=_auth(),
        json={
            "resources": [
                {
                    "project_id": project.id,
                    "kind": "asset",
                    "id": asset.id,
                    "revision": asset.revision,
                }
            ],
            "device_capabilities": ["clipboard.write"],
        },
    )
    assert response.status_code == 200
    actions = {item["id"]: item for item in response.json()}
    assert actions["open"]["available"] is True
    assert actions["copy"]["available"] is True


def test_canonical_resource_resolution_preserves_project_identity(api):
    client, _, _ = api
    first = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "First"}
    ).json()
    second = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "Second"}
    ).json()
    asset = client.post(
        "/api/v1/assets",
        headers=_auth(),
        json={"engagement_id": first["id"], "name": "api.example.test"},
    ).json()

    available = client.post(
        "/api/v1/resources/resolve",
        headers=_auth(),
        json={"project_id": first["id"], "kind": "asset", "id": asset["id"]},
    )
    assert available.status_code == 200
    assert available.json()["state"] == "available"
    assert available.json()["ref"]["revision"] == asset["revision"]

    wrong_project = client.post(
        "/api/v1/resources/resolve",
        headers=_auth(),
        json={"project_id": second["id"], "kind": "asset", "id": asset["id"]},
    )
    assert wrong_project.status_code == 200
    assert wrong_project.json()["state"] == "wrong_project"
    assert wrong_project.json()["actual_project_id"] == first["id"]

    deleted = client.post(
        "/api/v1/resources/resolve",
        headers=_auth(),
        json={"project_id": first["id"], "kind": "asset", "id": "missing"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["state"] == "deleted"


def test_local_provider_discovery_probes_only_fixed_services(api, monkeypatch):
    client, _, _ = api
    observed: list[tuple[ProviderFlavor, str]] = []

    async def health(runtime):
        observed.append((runtime.config.flavor, runtime.config.base_url))
        if runtime.config.flavor == ProviderFlavor.VLLM:
            return ProviderHealth(
                provider_id=runtime.config.id,
                healthy=True,
                models=["security-model", "security-model"],
            )
        return ProviderHealth(provider_id=runtime.config.id, healthy=False)

    monkeypatch.setattr(OpenAICompatibleProvider, "health", health)
    response = client.get("/api/v1/providers/discover-local", headers=_auth())

    assert response.status_code == 200
    assert response.json() == [
        {
            "flavor": "vllm",
            "display_name": "vLLM",
            "endpoint": "http://127.0.0.1:8000/v1",
            "models": ["security-model"],
        }
    ]
    assert set(observed) == {
        (ProviderFlavor.OLLAMA, "http://127.0.0.1:11434/v1"),
        (ProviderFlavor.VLLM, "http://127.0.0.1:8000/v1"),
        (ProviderFlavor.LM_STUDIO, "http://127.0.0.1:1234/v1"),
    }


def test_vllm_profile_health_discovers_models_through_the_api(api, monkeypatch):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["security-model"],
        )
    )

    async def healthy(runtime):
        assert runtime.config.flavor == ProviderFlavor.VLLM
        assert runtime.config.base_url == "http://127.0.0.1:8000/v1"
        return ProviderHealth(
            provider_id=runtime.config.id,
            healthy=True,
            models=["security-model", "vision-model"],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "health", healthy)

    response = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": profile.id,
        "healthy": True,
        "models": ["security-model"],
        "model_descriptors": [],
        "unlisted_models": ["vision-model"],
        "unlisted_model_descriptors": [],
        "upstream_providers": [],
        "detail": None,
        "credential_verified": None,
        "catalog_source": None,
        "key_expires_at": None,
        "key_limit_remaining": None,
        "provider_revision": profile.revision,
    }


def test_provider_health_persists_exact_model_context_catalog(api, monkeypatch):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            id="openrouter-context",
            name="OpenRouter",
            provider_type="openrouter",
            model_allowlist=["model-a"],
        )
    )

    async def healthy(runtime):
        return ProviderHealth(
            provider_id=runtime.config.id,
            healthy=True,
            models=["model-a"],
            model_descriptors=[
                ModelDescriptor(
                    id="model-a",
                    name="Model A",
                    context_window=200_000,
                    max_output_tokens=32_000,
                )
            ],
            catalog_source="openrouter:/models/user",
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "health", healthy)

    response = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())

    assert response.status_code == 200, response.text
    stored = store.get(ProviderProfile, profile.id)
    assert response.json()["provider_revision"] == stored.revision
    assert stored.revision == profile.revision + 1
    assert stored.metadata["model_descriptors"][0]["context_window"] == 200_000
    assert stored.metadata["model_descriptors"][0]["max_output_tokens"] == 32_000
    assert len(stored.metadata["model_catalog_revision"]) == 64


def test_provider_health_keeps_verified_openrouter_route_limits(api, monkeypatch):
    client, store, _ = api
    routes = {
        "route_limits": [
            {
                "provider_name": "Provider A",
                "provider_slug": "provider-a",
                "context_window": 1_048_576,
                "max_input_tokens": 1_048_576,
                "max_output_tokens": 262_144,
                "supported_parameters": ["tools"],
                "status": 0,
            }
        ],
        "route_limits_verified": True,
        "route_limits_checked_at": "2026-09-19T20:33:00+00:00",
        "route_limits_error": None,
    }
    profile = store.create(
        ProviderProfile(
            id="openrouter-verified-routes",
            name="OpenRouter",
            provider_type="openrouter",
            model_allowlist=["deepseek/model"],
            metadata={
                "model_descriptors": [
                    {"id": "deepseek/model", "name": "Model", **routes}
                ]
            },
        )
    )

    async def healthy(runtime):
        return ProviderHealth(
            provider_id=runtime.config.id,
            healthy=True,
            models=["deepseek/model"],
            model_descriptors=[
                ModelDescriptor(
                    id="deepseek/model",
                    name="Model",
                    context_window=1_048_576,
                    max_output_tokens=262_144,
                )
            ],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "health", healthy)

    first = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())
    second = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())

    assert first.status_code == second.status_code == 200
    stored = store.get(ProviderProfile, profile.id)
    assert stored.revision == profile.revision + 1
    descriptor = stored.metadata["model_descriptors"][0]
    assert {key: descriptor[key] for key in routes} == routes
    limits = resolve_context_limits(
        stored, model="deepseek/model", required_parameters={"tools"}
    )
    assert limits.route_limits_verified is True
    assert limits.context_window == 1_048_576


def test_provider_health_reports_discovered_models_outside_the_allowlist(
    api, monkeypatch
):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            id="openrouter-unlisted",
            name="OpenRouter",
            provider_type="openrouter",
            model_allowlist=["model-a"],
        )
    )

    async def healthy(runtime):
        return ProviderHealth(
            provider_id=runtime.config.id,
            healthy=True,
            models=["model-a", "model-b", "model-c"],
            model_descriptors=[
                ModelDescriptor(id="model-a", name="Model A"),
                ModelDescriptor(id="model-b", name="Model B", context_window=400_000),
            ],
            catalog_source="openrouter:/models/user",
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "health", healthy)

    response = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["models"] == ["model-a"]
    assert [item["id"] for item in body["model_descriptors"]] == ["model-a"]
    assert body["unlisted_models"] == ["model-b", "model-c"]
    assert [
        (item["id"], item["context_window"])
        for item in body["unlisted_model_descriptors"]
    ] == [("model-b", 400_000)]
    stored = store.get(ProviderProfile, profile.id)
    assert stored.model_allowlist == ["model-a"]
    assert [item["id"] for item in stored.metadata["model_descriptors"]] == ["model-a"]


def test_exact_model_capability_probe_persists_and_runtime_edit_requires_reverification(
    api, monkeypatch
):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["coder-model"],
            metadata={"default_model": "coder-model"},
        )
    )

    async def valid_probe(_runtime, request):
        assert request.model == "coder-model"
        assert request.tool_choice == ToolChoice.REQUIRED
        assert len(request.tools) == 1
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id=profile.id,
            model="coder-model",
            tool_calls=[
                ToolCall(
                    id="probe-call",
                    name="nebula_capability_probe",
                    arguments={"nonce": nonce},
                )
            ],
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", valid_probe)
    verified = client.post(
        f"/api/v1/providers/{profile.id}/capabilities/verify",
        headers=_auth(),
        json={"model": "coder-model", "expected_revision": profile.revision},
    )

    assert verified.status_code == 200
    assert verified.json()["verification"]["status"] == "verified", verified.json()[
        "verification"
    ]["failure_detail"]
    stored = store.get(ProviderProfile, profile.id)
    assert stored.tools_verified_for("coder-model") is True
    assert stored.capabilities.tool_calling is True

    changed = client.patch(
        f"/api/v1/providers/{profile.id}",
        headers=_auth(),
        json={
            "changes": {
                "metadata": {
                    "default_model": "coder-model",
                    "options": {"timeout_seconds": 30},
                }
            },
            "expected_revision": stored.revision,
        },
    )

    assert changed.status_code == 200
    assert changed.json()["capability_verifications"] == {}
    assert changed.json()["capabilities"]["tool_calling"] is False


def test_openrouter_capability_probe_persists_verified_route_limits(api, monkeypatch):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            id="openrouter-routes",
            name="OpenRouter",
            provider_type="openrouter",
            model_allowlist=["author/model"],
            metadata={
                "default_model": "author/model",
                "model_descriptors": [
                    {
                        "id": "author/model",
                        "name": "Model",
                        "context_window": 100_000,
                        "max_output_tokens": 10_000,
                    }
                ],
            },
        )
    )

    async def valid_probe(_runtime, request):
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id=profile.id,
            model="author/model",
            tool_calls=[
                ToolCall(
                    id="probe-call",
                    name="nebula_capability_probe",
                    arguments={"nonce": nonce},
                )
            ],
            finish_reason="tool_calls",
        )

    async def routes(_runtime, model):
        assert model == "author/model"
        return [
            ModelRouteDescriptor(
                provider_name="Provider A",
                context_window=65_536,
                max_input_tokens=60_000,
                max_output_tokens=4_096,
                supported_parameters=["tools"],
            )
        ]

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", valid_probe)
    monkeypatch.setattr(OpenAICompatibleProvider, "openrouter_route_limits", routes)

    response = client.post(
        f"/api/v1/providers/{profile.id}/capabilities/verify",
        headers=_auth(),
        json={"model": "author/model", "expected_revision": profile.revision},
    )

    assert response.status_code == 200, response.text
    stored = store.get(ProviderProfile, profile.id)
    descriptor = stored.metadata["model_descriptors"][0]
    assert descriptor["route_limits_verified"] is True
    assert descriptor["route_limits"][0]["context_window"] == 65_536
    assert descriptor["route_limits"][0]["max_input_tokens"] == 60_000
    assert descriptor["route_limits_error"] is None
    assert len(stored.metadata["route_catalog_revision"]) == 64


def test_openrouter_capability_probe_measures_the_alias_target(api, monkeypatch):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            id="openrouter-alias",
            name="OpenRouter",
            provider_type="openrouter",
            model_allowlist=["~author/family-latest"],
            metadata={
                "default_model": "~author/family-latest",
                "model_descriptors": [
                    {
                        "id": "~author/family-latest",
                        "name": "Family Latest",
                        "context_window": 1_048_576,
                        "max_output_tokens": 262_144,
                        "alias_target": "author/model-a",
                    }
                ],
            },
        )
    )

    async def valid_probe(_runtime, request):
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id=profile.id,
            model="~author/family-latest",
            tool_calls=[
                ToolCall(
                    id="probe-call",
                    name="nebula_capability_probe",
                    arguments={"nonce": nonce},
                )
            ],
            finish_reason="tool_calls",
        )

    async def routes(_runtime, model):
        # The alias exposes no endpoints; discovery has to ask its target.
        assert model == "author/model-a"
        return [
            ModelRouteDescriptor(
                provider_name="Provider A",
                context_window=1_000_000,
                max_input_tokens=1_000_000,
                max_output_tokens=128_000,
                supported_parameters=["tools"],
            )
        ]

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", valid_probe)
    monkeypatch.setattr(OpenAICompatibleProvider, "openrouter_route_limits", routes)

    response = client.post(
        f"/api/v1/providers/{profile.id}/capabilities/verify",
        headers=_auth(),
        json={
            "model": "~author/family-latest",
            "expected_revision": profile.revision,
        },
    )

    assert response.status_code == 200, response.text
    stored = store.get(ProviderProfile, profile.id)
    descriptor = stored.metadata["model_descriptors"][0]
    assert descriptor["route_limits_verified"] is True
    assert descriptor["route_limits_source_model"] == "author/model-a"
    assert descriptor["route_limits_error"] is None
    limits = resolve_context_limits(
        stored, model="~author/family-latest", required_parameters={"tools"}
    )
    assert limits.context_window == 1_000_000
    assert limits.route_limits_verified is True


def test_capability_probe_never_narrows_an_empty_model_allowlist(api, monkeypatch):
    client, store, _ = api
    models = ["author/first", "author/second"]
    profile = store.create(
        ProviderProfile(
            id="openrouter-open-catalog",
            name="OpenRouter",
            provider_type="openrouter",
            metadata={
                "model_descriptors": [
                    {"id": model, "name": model, "context_window": 100_000}
                    for model in models
                ]
            },
        )
    )

    async def valid_probe(_runtime, request):
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id=profile.id,
            model=request.model or "",
            tool_calls=[
                ToolCall(
                    id="probe-call",
                    name="nebula_capability_probe",
                    arguments={"nonce": nonce},
                )
            ],
            finish_reason="tool_calls",
        )

    async def routes(_runtime, model):
        return [
            ModelRouteDescriptor(
                provider_name="Provider A",
                context_window=65_536,
                max_output_tokens=4_096,
                supported_parameters=["tools"],
            )
        ]

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", valid_probe)
    monkeypatch.setattr(OpenAICompatibleProvider, "openrouter_route_limits", routes)

    for model in models:
        revision = store.get(ProviderProfile, profile.id).revision
        response = client.post(
            f"/api/v1/providers/{profile.id}/capabilities/verify",
            headers=_auth(),
            json={"model": model, "expected_revision": revision},
        )
        assert response.status_code == 200, response.text

    stored = store.get(ProviderProfile, profile.id)
    # Verifying one model must not pin the provider to it; every model stays selectable.
    assert stored.model_allowlist == []
    assert sorted(stored.capability_verifications) == models


def test_chat_origin_approval_decision_does_not_require_an_agent_run(api):
    client, store, _ = api
    engagement = Engagement(id="eng-chat-approval", name="Chat approval")
    session = ChatSession(
        id="session-chat-approval",
        engagement_id=engagement.id,
        title="Approval chat",
        provider_profile_id="provider-chat",
        model="model-a",
    )
    turn = ChatTurn(
        id="turn-chat-approval",
        engagement_id=engagement.id,
        session_id=session.id,
        provider_profile_id="provider-chat",
        model="model-a",
        status=ChatTurnStatus.WAITING_APPROVAL,
        tools_enabled=True,
        approval_id="approval-chat",
    )
    approval = Approval(
        id="approval-chat",
        engagement_id=engagement.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session.id,
        chat_turn_id=turn.id,
        risk_class=RiskClass.ACTIVE_SCAN,
        exact_request={"tool_name": "safe.scan", "arguments": {"target": "host"}},
        policy_rationale="operator confirmation required",
        requested_by="chat-assistant",
    )
    store.create_many([engagement, session, turn, approval])

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["origin"] == "chat"
    assert response.json()["status"] == "approved"
    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.WAITING_APPROVAL


def test_disabled_provider_health_fails_closed_without_network(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "disabled-provider.db")
    profile = store.create(
        ProviderProfile(
            name="Disabled",
            provider_type="vllm",
            is_local=True,
            enabled=False,
        )
    )
    invalid = store.create(
        ProviderProfile(name="Invalid import", provider_type="not-a-provider")
    )

    async def should_not_run(_runtime):
        raise AssertionError("disabled provider health must not access the network")

    monkeypatch.setattr(OpenAICompatibleProvider, "health", should_not_run)
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.post(
        f"/api/v1/providers/{profile.id}/health",
        headers=_auth(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": profile.id,
        "healthy": False,
        "models": [],
        "model_descriptors": [],
        "unlisted_models": [],
        "unlisted_model_descriptors": [],
        "upstream_providers": [],
        "detail": "provider profile is disabled",
        "credential_verified": None,
        "catalog_source": None,
        "key_expires_at": None,
        "key_limit_remaining": None,
        "provider_revision": profile.revision,
    }
    refreshed = client.post("/api/v1/provider-health/refresh", headers=_auth())
    assert refreshed.status_code == 200
    by_id = {item["provider_id"]: item for item in refreshed.json()}
    assert by_id[profile.id]["detail"] == "provider profile is disabled"
    assert by_id[invalid.id]["healthy"] is False
    assert "unknown provider type" in by_id[invalid.id]["detail"]


def test_tauri_cors_and_audit_resources_are_fail_closed_by_default(tmp_path):
    store = NebulaStore(tmp_path / "secure.db")
    app = create_app(store, auth_token="test-token")
    client = TestClient(app)
    preflight = client.options(
        "/api/v1/engagements",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert client.post("/api/v1/runs/nope/events", headers=_auth()).status_code == 405
    assert client.post("/api/v1/approvals", headers=_auth(), json={}).status_code == 405
    assert (
        client.patch("/api/v1/tool-calls/nope", headers=_auth(), json={}).status_code
        == 405
    )
    assert client.delete("/api/v1/artifacts/nope", headers=_auth()).status_code == 405


def test_typed_crud_revision_and_overview(api):
    client, store, _ = api
    created = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "API engagement"}
    )
    assert created.status_code == 201
    engagement = created.json()
    engagement_id = engagement["id"]

    patched = client.patch(
        f"/api/v1/engagements/{engagement_id}",
        headers=_auth(),
        json={"changes": {"description": "updated"}, "expected_revision": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["revision"] == 2
    assert patched.json()["description"] == "updated"

    stale = client.patch(
        f"/api/v1/engagements/{engagement_id}",
        headers=_auth(),
        json={"changes": {"description": "stale"}, "expected_revision": 1},
    )
    assert stale.status_code == 409
    overview = client.get(
        f"/api/v1/engagements/{engagement_id}/overview", headers=_auth()
    )
    assert overview.status_code == 200
    assert overview.json()["counts"]["engagements"] == 1

    assert (
        client.delete(
            f"/api/v1/engagements/{engagement_id}", headers=_auth()
        ).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/engagements/{engagement_id}", headers=_auth()).status_code
        == 404
    )
    assert store.count(ScopePolicy) == 0
    assert (
        client.get(
            f"/api/v1/engagements/{engagement_id}/overview",
            headers=_auth(),
        ).status_code
        == 404
    )


def test_run_event_rest_and_authenticated_websocket_replay(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Event replay"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="Replay events"))
    for number in (1, 2):
        response = client.post(
            f"/api/v1/runs/{run.id}/events",
            headers=_auth(),
            json={
                "event_type": "task.progress",
                "payload": {"number": number},
                "idempotency_key": f"event-{number}",
            },
        )
        assert response.status_code == 201
        assert response.json()["sequence"] == number

    replay = client.get(f"/api/v1/runs/{run.id}/events?after=1", headers=_auth()).json()
    assert [event["sequence"] for event in replay["events"]] == [2]

    with client.websocket_connect(
        f"/api/v1/runs/{run.id}/events/ws?after=0",
        subprotocols=["nebula.events.v1", "nebula.auth.dGVzdC10b2tlbg"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "nebula.events.v1"
        assert websocket.receive_json()["event"]["sequence"] == 1
        assert websocket.receive_json()["event"]["sequence"] == 2
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 2,
        }

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/runs/{run.id}/events/ws"):
            pass
    assert exc_info.value.code == 4401


def test_artifact_content_and_openapi_contract(api):
    client, store, artifacts = api
    engagement = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "Artifacts"}
    ).json()
    artifact = artifacts.put_bytes(
        b"evidence", engagement_id=engagement["id"], filename="proof.txt"
    )
    store.create(artifact)
    response = client.get(f"/api/v1/artifacts/{artifact.id}/content", headers=_auth())
    assert response.status_code == 200
    assert response.content == b"evidence"

    schema = client.get("/openapi.json").json()
    assert "/api/v1/providers" in schema["paths"]
    assert "/api/v1/findings" in schema["paths"]
    request_schema = schema["paths"]["/api/v1/engagements"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/Engagement")


def test_static_workspace_supports_spa_reload_without_masking_missing_assets(
    tmp_path,
):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Nebula workspace</main>")
    (frontend / "app.js").write_text("console.log('nebula')")
    client = TestClient(
        create_app(
            NebulaStore(tmp_path / "spa.db"),
            auth_token="test-token",
            static_dir=frontend,
        )
    )

    assert client.get("/settings").text == "<main>Nebula workspace</main>"
    assert client.get("/app.js").status_code == 200
    assert client.get("/missing.js").status_code == 404
    api_missing = client.get("/api/v1/not-a-route")
    assert api_missing.status_code == 404
    assert "Nebula workspace" not in api_missing.text


def test_approval_decision_is_revisioned_and_recorded_in_run_ledger(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Approval API"))
    run = store.create(
        AgentRun(engagement_id=engagement.id, objective="Approved operation")
    )
    approval = store.create(
        Approval(
            engagement_id=engagement.id,
            run_id=run.id,
            risk_class=RiskClass.ACTIVE_SCAN,
            exact_request={
                "tool_name": "scan.tcp",
                "arguments": {"ports": [80]},
            },
            target="192.0.2.8",
            policy_rationale="active scan requires a scoped operator decision",
            requested_by="network-specialist",
        )
    )

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={
            "decision": "approve",
            "reason": "Approved port 443 only",
            "edited_arguments": {"ports": [443]},
        },
    )

    assert response.status_code == 200
    decided = response.json()
    assert decided["status"] == ApprovalStatus.EDITED.value
    assert decided["revision"] == approval.revision + 1
    assert decided["exact_request"]["arguments"] == {"ports": [443]}
    assert decided["decided_by"] == "system"
    assert decided["decided_at"] is not None
    persisted = store.get(Approval, approval.id)
    assert persisted.status == ApprovalStatus.EDITED
    events = store.replay_events(run.id)
    assert len(events) == 1
    assert events[0].event_type == "approval.resolved"
    assert events[0].actor_id == "system"
    assert events[0].payload == {
        "approval_id": approval.id,
        "status": "edited",
        "decided_by": "system",
    }
    assert (
        client.post(
            f"/api/v1/approvals/{approval.id}/decision",
            headers=_auth(),
            json={"decision": "reject"},
        ).status_code
        == 409
    )


def test_expired_approval_is_durably_expired_instead_of_approved(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Expired approval"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="Do not run"))
    approval = store.create(
        Approval(
            engagement_id=engagement.id,
            run_id=run.id,
            risk_class=RiskClass.CREDENTIAL_USE,
            exact_request={"tool_name": "login.test", "arguments": {}},
            policy_rationale="credential use always requires approval",
            requested_by="web-specialist",
            expires_at=utc_now() - timedelta(seconds=1),
        )
    )

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={"decision": "approve"},
    )

    assert response.status_code == 410
    expired = store.get(Approval, approval.id)
    assert expired.status == ApprovalStatus.EXPIRED
    assert expired.decided_by == "system"
    assert expired.decided_at is not None
    events = store.replay_events(run.id)
    assert [event.event_type for event in events] == ["approval.expired"]
    assert events[0].actor_id == "system"


def test_websocket_header_auth_survives_malformed_optional_auth_protocol(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Empty replay"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="No events yet"))
    with client.websocket_connect(
        f"/api/v1/runs/{run.id}/events/ws",
        headers=_auth(),
        subprotocols=["nebula.events.v1", "nebula.auth.not!base64"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "nebula.events.v1"
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 0,
        }


def test_run_event_routes_reject_a_missing_run(api):
    client, _, _ = api

    assert client.get("/api/v1/runs/missing/events", headers=_auth()).status_code == 404
    assert (
        client.post(
            "/api/v1/runs/missing/events",
            headers=_auth(),
            json={"event_type": "orphan"},
        ).status_code
        == 404
    )
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/runs/missing/events/ws",
            headers=_auth(),
            subprotocols=["nebula.events.v1"],
        ):
            pass
    assert exc_info.value.code == 4404


def test_websocket_rejects_conflicting_valid_credentials(api):
    client, _, _ = api
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/runs/empty/events/ws",
            headers=_auth(),
            subprotocols=[
                "nebula.events.v1",
                "nebula.auth.d3JvbmctdG9rZW4",
            ],
        ):
            pass
    assert exc_info.value.code == 4401


def test_chat_approval_exact_request_is_readable_by_id_and_tracks_decision(api):
    client, store, _ = api
    engagement = Engagement(id="eng-review", name="Review")
    approval = Approval(
        id="approval-review",
        engagement_id=engagement.id,
        run_id="turn-review",
        origin=ToolCallOrigin.CHAT,
        risk_class=RiskClass.PASSIVE,
        exact_request={
            "tool_name": "read_file",
            "arguments": {"path": "notes.txt"},
            "cwd": "/workspace",
        },
        policy_rationale="Operator confirmation required",
        requested_by="chat-assistant",
    )
    store.create_many([engagement, approval])
    endpoint = f"/api/v1/approvals/{approval.id}"
    assert client.get(endpoint).status_code == 401
    response = client.get(endpoint, headers=_auth())
    assert response.status_code == 200
    assert response.json()["exact_request"] == approval.exact_request
    decision = client.post(
        f"{endpoint}/decision", headers=_auth(), json={"decision": "reject"}
    )
    assert decision.status_code == 200
    assert client.get(endpoint, headers=_auth()).json()["status"] == "rejected"


def test_openrouter_upstream_directory_is_public_cached_and_located(tmp_path):
    seen: list[httpx.Request] = []

    def directory(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "slug": "gmicloud",
                        "name": "GMICloud",
                        "headquarters": "US",
                        "datacenters": ["US"],
                    },
                    {
                        "slug": "siliconflow",
                        "name": "SiliconFlow",
                        "headquarters": "SG",
                        "datacenters": ["us"],
                    },
                    {
                        "slug": "baidu",
                        "name": "Baidu",
                        "headquarters": "CN",
                        "datacenters": None,
                    },
                    {"slug": "", "name": "Broken"},
                ]
            },
        )

    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            openrouter_directory_transport=httpx.MockTransport(directory),
        )
    )
    path = "/api/v1/providers/openrouter/upstream-providers"
    assert client.get(path).status_code == 401
    first = client.get(path, headers=_auth())
    assert first.status_code == 200
    assert first.json() == [
        {"slug": "baidu", "name": "Baidu", "headquarters": "CN", "datacenters": []},
        {
            "slug": "gmicloud",
            "name": "GMICloud",
            "headquarters": "US",
            "datacenters": ["US"],
        },
        {
            "slug": "siliconflow",
            "name": "SiliconFlow",
            "headquarters": "SG",
            "datacenters": ["US"],
        },
    ]
    assert client.get(path, headers=_auth()).json() == first.json()
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/providers"


def test_openrouter_upstream_directory_failure_is_reported(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            auth_token="test-token",
            openrouter_directory_transport=httpx.MockTransport(
                lambda _request: httpx.Response(503)
            ),
        )
    )
    response = client.get(
        "/api/v1/providers/openrouter/upstream-providers", headers=_auth()
    )
    assert response.status_code == 502
    assert "provider directory is unavailable" in response.text


def test_companion_stream_closes_with_4404_for_a_missing_session(api):
    client, _, _ = api
    token = base64.urlsafe_b64encode(b"test-token").decode("ascii").rstrip("=")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/browser-companion/missing/tabs/tab-1/stream",
            subprotocols=[f"nebula.auth.{token}"],
        ):
            pass
    assert exc_info.value.code == 4404


def test_paired_device_is_recognised_beyond_the_first_thousand_sessions(api):
    from datetime import timedelta

    from nebula.v3.domain import PairedDeviceSession, utc_now

    client, store, _ = api
    expiry = utc_now() + timedelta(hours=1)
    store.create_many(
        [
            PairedDeviceSession(
                id=f"old-{index}",
                name="Older device",
                token_sha256=hashlib.sha256(f"old-{index}".encode()).hexdigest(),
                csrf_sha256="0" * 64,
                idle_expires_at=expiry,
                absolute_expires_at=expiry,
            )
            for index in range(1_000)
        ]
    )
    store.create(
        PairedDeviceSession(
            id="newest",
            name="Newest phone",
            token_sha256=hashlib.sha256(b"newest-token").hexdigest(),
            csrf_sha256="0" * 64,
            idle_expires_at=expiry,
            absolute_expires_at=expiry,
        )
    )

    response = client.get(
        "/api/v1/engagements", cookies={"nebula_device": "newest-token"}
    )

    assert response.status_code == 200, response.text
