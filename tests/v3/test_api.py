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
    ChatGoal,
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
    rule = workspace / ".agents" / "rules" / "accuracy.md"
    rule.parent.mkdir()
    rule.write_text("Report only verified results.", encoding="utf-8")
    skill.write_text(
        "Review carefully. Read [the shared rule](../../../.agents/rules/accuracy.md).",
        encoding="utf-8",
    )
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

    attached = client.put(
        "/api/v1/chat/sessions/goal-session/goal/skills",
        headers=_auth(),
        json={
            "expected_revision": goal["revision"],
            "skills": [{"name": "review", "path": str(skill.resolve())}],
        },
    )
    assert attached.status_code == 200, attached.text
    attached_goal = attached.json()
    assert attached_goal["skill_snapshots"][0]["name"] == "review"
    assert attached_goal["skill_snapshots"][0]["resources"][0]["path"] == str(
        rule.resolve()
    )
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

    started = client.post(
        "/api/v1/chat/sessions/goal-session/goal/actions",
        headers=_auth(),
        json={"expected_revision": removed.json()["revision"], "action": "start"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == ChatGoalStatus.RUNNING.value
    assert started.json()["current_step"] == 1


def test_goal_conversation_exists_before_its_first_message(api):
    client, store, _ = api
    store.create(Engagement(id="goal-first-project", name="Goal first"))
    provider = store.create(
        ProviderProfile(
            id="goal-first-provider",
            name="Local provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )

    rejected = client.post(
        "/api/v1/chat/goal-conversations",
        headers=_auth(),
        json={
            "engagement_id": "goal-first-project",
            "provider_id": provider.id,
            "model": "missing-model",
            "objective": "Invalid model must not create partial state",
            "completion_criteria": ["Nothing is persisted"],
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert store.list_entities(ChatSession) == []
    assert store.list_entities(ChatGoal) == []

    response = client.post(
        "/api/v1/chat/goal-conversations",
        headers=_auth(),
        json={
            "engagement_id": "goal-first-project",
            "provider_id": provider.id,
            "model": "model-a",
            "tools_enabled": True,
            "hook_ids": ["audit"],
            "objective": "Investigate before the first message",
            "completion_criteria": ["The first turn is goal linked"],
            "plan": ["Inspect", "Validate"],
        },
    )

    assert response.status_code == 201, response.text
    created = response.json()
    session = created["session"]
    goal = created["goal"]
    assert session["title"] == "Investigate before the first message"
    assert session["metadata"] == {
        "tools_enabled": True,
        "mcp_server_ids": [],
        "hook_ids": ["audit"],
        "message_count": 0,
        "last_sequence": 0,
        "initial_title_state": "pending",
    }
    assert goal["session_id"] == session["id"]
    assert goal["status"] == ChatGoalStatus.DRAFT.value
    assert store.list_session_entities(ChatTurn, session["id"]) == []


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
    # The caller verifies the model and asks again instead of reading prose.
    assert response.json()["reason_code"] == "model_not_tool_verified"
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


def test_generic_delete_skips_unreadable_rows_of_unrelated_kinds(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Corrupt neighbour"))
    asset = store.create(
        Asset(engagement_id=engagement.id, name="host", asset_type="host")
    )
    unrelated = store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Will be corrupted",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    with store.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE entities SET payload = json_set(payload, '$.title', 5) "
            "WHERE id = ?",
            (unrelated.id,),
        )

    response = client.delete(f"/api/v1/assets/{asset.id}", headers=_auth())

    assert response.status_code == 204, response.text
    assert store.count(Asset) == 0


def test_run_event_websocket_completes_and_closes_after_a_terminal_run(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Finished mission"))
    run = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Already finished",
            status="complete",
        )
    )
    store.append_event(run.id, "run.completed", {}, idempotency_key="done")

    with client.websocket_connect(
        f"/api/v1/runs/{run.id}/events/ws?after=0",
        headers=_auth(),
        subprotocols=["nebula.events.v1"],
    ) as websocket:
        assert websocket.receive_json()["event"]["sequence"] == 1
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 1,
        }
        assert websocket.receive_json() == {"kind": "complete", "after_sequence": 1}
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_json()
    assert exc_info.value.code == 1000


def _exchange(store, engagement_id: str):
    from nebula.v3.domain import BrowserTrafficExchange

    return store.create(
        BrowserTrafficExchange(
            engagement_id=engagement_id,
            session_id="session-1",
            tab_id="tab-1",
            identity_id="identity-1",
            method="GET",
            url="https://target.example/login",
            scope_state="in_scope",
            scope_policy_id="scope-1",
            scope_policy_revision=1,
        )
    )


def test_resolve_resource_finds_an_existing_browser_exchange(api):
    # The resolve map named a kind no entity declares, so every exchange
    # reference was reported as inaccessible although the row existed.
    client, store, _ = api
    engagement = store.create(Engagement(name="Exchange resolve"))
    exchange = _exchange(store, engagement.id)

    response = client.post(
        "/api/v1/resources/resolve",
        headers=_auth(),
        json={
            "project_id": engagement.id,
            "kind": "browser_exchange",
            "id": exchange.id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "available"
    assert response.json()["ref"]["revision"] == exchange.revision


def test_non_ascii_credentials_are_rejected_instead_of_crashing(tmp_path):
    # Starlette decodes headers as latin-1, and hmac.compare_digest refuses
    # non-ASCII text, so a bad credential became an unhandled 500 (and a
    # pre-accept TypeError on every websocket) instead of the normal 401/403.
    store = NebulaStore(tmp_path / "non-ascii.db")
    app = create_app(store, auth_token="test-token")
    client = TestClient(app, base_url="https://127.0.0.1", client=("127.0.0.1", 50000))

    bearer = client.get(
        "/api/v1/health", headers={b"Authorization": b"Bearer \xc3\xa9"}
    )
    assert bearer.status_code == 401, bearer.text

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/runs/missing/events/ws",
            headers={b"Authorization": b"Bearer \xc3\xa9"},
            subprotocols=["nebula.events.v1"],
        ):
            pass
    assert exc_info.value.code == 4401

    pairing = client.post(
        "/api/v1/auth/pairings", headers=_auth(), json={"name": "Phone"}
    ).json()
    redeemed = client.post(
        "/api/v1/auth/pairings/redeem",
        json={
            "secret": pairing["secret"],
            "confirmation_code": pairing["confirmation_code"],
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    device_id = redeemed.json()["device"]["id"]
    csrf = redeemed.json()["csrf_token"]

    origin = client.delete(
        f"/api/v1/auth/devices/{device_id}",
        headers={"X-Nebula-CSRF": csrf, b"Origin": b"https://127.0.0.1\xc3\xa9"},
    )
    assert origin.status_code == 403, origin.text
    csrf_header = client.delete(
        f"/api/v1/auth/devices/{device_id}",
        headers={b"X-Nebula-CSRF": b"\xc3\xa9", "Origin": "https://127.0.0.1"},
    )
    assert csrf_header.status_code == 403, csrf_header.text
    assert client.get("/api/v1/auth/devices").status_code == 200


def test_harness_chat_rejects_two_browser_companions_before_persisting(
    api, monkeypatch
):
    # The 409 was raised after prepare_chat had already stored the user
    # message and a pending turn that nothing would ever start.
    from nebula.v3.harnesses import HarnessConfigurationError

    client, store, _ = api
    engagement = store.create(Engagement(name="Companions"))
    runtime = client.app.state.harness_runtime_service
    prepared: list[str] = []

    def prepare_chat(**kwargs):
        prepared.append(kwargs["prompt"])
        raise HarnessConfigurationError("prepare_chat must not run")

    monkeypatch.setattr(runtime, "prepare_chat", prepare_chat)

    def attachment(session_id: str) -> dict:
        text = f"tab for {session_id}"
        return {
            "source_kind": "browser_companion",
            "source_id": session_id,
            "source_label": session_id,
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    response = client.post(
        "/api/v1/chat/completions",
        headers=_auth(),
        json={
            "backend": "harness",
            "engagement_id": engagement.id,
            "harness_profile_id": "profile-1",
            "messages": [{"role": "user", "content": "Compare both tabs"}],
            "context_attachments": [
                attachment("companion-a"),
                attachment("companion-b"),
            ],
        },
    )

    assert response.status_code == 409, response.text
    assert prepared == []


def test_harness_turn_event_socket_sends_an_error_frame_on_stream_failure(
    api, monkeypatch
):
    # Only disconnects were caught, so a runtime failure while following the
    # turn tore the socket down with no error frame for the viewer.
    client, _, _ = api
    runtime = client.app.state.harness_runtime_service
    monkeypatch.setattr(runtime, "activity_events", lambda *args, **kwargs: None)

    async def follow_turn(turn_id: str, *, after_sequence: int = 0):
        raise RuntimeError("ledger unavailable")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(runtime, "follow_turn", follow_turn)

    with client.websocket_connect(
        "/api/v1/harness-turns/turn-1/events/ws",
        headers=_auth(),
        subprotocols=["nebula.events.v1"],
    ) as websocket:
        frame = websocket.receive_json()
    assert frame["kind"] == "error"
    assert frame["retryable"] is True
    assert "ledger unavailable" not in json.dumps(frame)


def test_browser_assessment_event_socket_completes_when_the_assessment_is_deleted(
    api,
):
    # The poll loop looked the assessment up every tick; deleting it raised
    # NotFoundError out of the handler and the viewer saw an abnormal close.
    from nebula.v3.domain import BrowserAssessment

    client, store, _ = api
    engagement = store.create(Engagement(name="Assessment stream"))
    assessment = store.create(
        BrowserAssessment(
            engagement_id=engagement.id,
            name="Login review",
            objective="Review the login flow",
            session_id="session-1",
            identity_ids=["identity-1"],
            primary_identity_id="identity-1",
            target_urls=["https://target.example"],
            scope_policy_id="scope-1",
            scope_policy_revision=1,
            status="running",
            created_by="operator",
        )
    )

    with client.websocket_connect(
        f"/api/v1/browser-assessments/{assessment.id}/events/ws",
        headers=_auth(),
        subprotocols=["nebula.events.v1"],
    ) as websocket:
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 0,
        }
        store.delete(BrowserAssessment, assessment.id)
        assert websocket.receive_json() == {"kind": "complete", "after_sequence": 0}
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_json()
    assert exc_info.value.code == 4404


def test_health_does_not_expose_host_paths(tmp_path):
    # /health answers cookie-paired devices too, and it echoed the absolute
    # log directory and settings file path of the operator's home.
    from nebula.v3.diagnostics import DiagnosticManager

    manager = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    try:
        client = TestClient(
            create_app(
                NebulaStore(tmp_path / "health.db"),
                auth_token="test-token",
                diagnostic_manager=manager,
            )
        )
        response = client.get("/api/v1/health", headers=_auth())
        assert response.status_code == 200, response.text
        body = json.dumps(response.json())
        assert "log_directory" not in response.json()["diagnostics"]
        assert "settings_path" not in response.json()["diagnostics"]
        assert str(tmp_path) not in body
    finally:
        manager.close()


def test_pairing_redeem_reports_a_wrong_code_and_lets_the_phone_retry(tmp_path):
    # The offer was popped before the code was checked, so one typo burned
    # the QR and blamed the secret instead of the code.
    from nebula.v3 import api as api_module

    store = NebulaStore(tmp_path / "pairing-retry.db")
    app = create_app(store, auth_token="test-token")
    client = TestClient(app, base_url="https://127.0.0.1", client=("127.0.0.1", 50000))
    pairing = client.post(
        "/api/v1/auth/pairings", headers=_auth(), json={"name": "Phone"}
    ).json()
    wrong_code = f"{(int(pairing['confirmation_code']) + 1) % 1_000_000:06d}"

    def redeem(code: str):
        return client.post(
            "/api/v1/auth/pairings/redeem",
            json={"secret": pairing["secret"], "confirmation_code": code},
        )

    mistyped = redeem(wrong_code)
    assert mistyped.status_code == 401
    assert mistyped.json()["detail"] == "pairing confirmation code did not match"
    assert redeem(pairing["confirmation_code"]).status_code == 200

    second = client.post(
        "/api/v1/auth/pairings", headers=_auth(), json={"name": "Tablet"}
    ).json()
    pairing = second
    for _ in range(api_module.PAIRING_CONFIRMATION_ATTEMPTS):
        assert redeem(wrong_code).status_code == 401
    exhausted = redeem(second["confirmation_code"])
    assert exhausted.status_code == 401
    assert exhausted.json()["detail"] == "pairing secret is invalid or expired"


def test_diagnostics_settings_write_failure_is_a_retryable_503(tmp_path, monkeypatch):
    # DiagnosticsError was unmapped, so an unwritable settings file became a
    # generic 500 with retryable=false although the service had an error_id.
    from nebula.v3.diagnostics import DiagnosticManager, SETTINGS_SCHEMA

    manager = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    try:
        client = TestClient(
            create_app(
                NebulaStore(tmp_path / "settings.db"),
                auth_token="test-token",
                diagnostic_manager=manager,
            ),
            raise_server_exceptions=False,
        )

        def refuse(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(manager, "_atomic_write_json", refuse)
        response = client.put(
            "/api/v1/diagnostics/settings",
            headers=_auth(),
            json={"schema": SETTINGS_SCHEMA, "global_level": "debug"},
        )
        assert response.status_code == 503, response.text
        assert response.json()["retryable"] is True
        assert "could not be saved" in response.json()["detail"]
    finally:
        manager.close()


def test_action_intents_and_handoffs_list_newest_first_with_offset(api):
    from nebula.v3.domain import (
        ActionIntent,
        HandoffEnvelope,
        ResourceKind,
        ResourceRef,
    )

    client, store, _ = api
    project = store.create(Engagement(name="Device actions"))
    base = utc_now()
    expires = base + timedelta(hours=1)
    ref = ResourceRef(project_id=project.id, kind=ResourceKind.PROJECT, id=project.id)
    store.create_many(
        [
            ActionIntent(
                id=f"intent-{index}",
                engagement_id=project.id,
                resources=[ref],
                action_id="share",
                requester="operator",
                idempotency_key=f"share-{index}",
                logical_lease_key=f"lease-{index}",
                expires_at=expires,
                created_at=base + timedelta(seconds=index),
                updated_at=base + timedelta(seconds=index),
            )
            for index in range(3)
        ]
        + [
            HandoffEnvelope(
                id=f"handoff-{index}",
                engagement_id=project.id,
                action_id="ask_nebula",
                origin_device_id="mac",
                expires_at=expires,
                created_at=base + timedelta(seconds=index),
                updated_at=base + timedelta(seconds=index),
            )
            for index in range(3)
        ]
    )

    def ids(path: str, **params) -> list[str]:
        response = client.get(
            f"/api/v1/{path}",
            headers=_auth(),
            params={"project_id": project.id, **params},
        )
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()]

    assert ids("action-intents", limit=2) == ["intent-2", "intent-1"]
    assert ids("action-intents", limit=2, offset=2) == ["intent-0"]
    assert ids("handoffs", limit=2) == ["handoff-2", "handoff-1"]
    assert ids("handoffs", limit=2, offset=2) == ["handoff-0"]


def test_delete_vpn_profile_sees_dependants_past_a_thousand_older_rows(api):
    from nebula.v3.domain import (
        AutomationProjectPolicy,
        AutomationSession,
        AutomationSessionStatus,
        VpnProfile,
    )

    client, store, _ = api
    project = store.create(Engagement(name="VPN project"))
    base = utc_now()

    def profile(name: str) -> VpnProfile:
        return store.create(
            VpnProfile(
                name=name,
                filename=f"{name}.ovpn",
                remote_host="vpn.example.test",
                remote_port=1194,
                protocol="udp",
                fingerprint="a" * 64,
                secret_ref=f"vpn:{name}",
            )
        )

    def policy(vpn_profile_id: str | None, at) -> AutomationProjectPolicy:
        return AutomationProjectPolicy(
            engagement_id=project.id, vpn_profile_id=vpn_profile_id, created_at=at
        )

    def automation_session(status, vpn_profile_id: str, at) -> AutomationSession:
        return AutomationSession(
            engagement_id=project.id,
            owner_kind="api",
            owner_id="owner",
            runtime_image="ghcr.io/example/runtime:latest",
            runtime_digest="sha256:" + "b" * 64,
            runner_profile_id="runner",
            runner_profile_revision=1,
            policy_id="policy",
            policy_revision=1,
            status=status,
            vpn_profile_id=vpn_profile_id,
            vpn_profile_revision=1,
            created_at=at,
        )

    policy_bound = profile("policy-bound")
    session_bound = profile("session-bound")
    store.create_many(
        [policy(None, base - timedelta(minutes=1)) for _ in range(1_000)]
        + [policy(policy_bound.id, base)]
    )
    store.create_many(
        [
            automation_session(
                AutomationSessionStatus.CLOSED,
                session_bound.id,
                base - timedelta(minutes=1),
            )
            for _ in range(1_000)
        ]
        + [automation_session(AutomationSessionStatus.READY, session_bound.id, base)]
    )

    def delete(profile: VpnProfile):
        return client.request(
            "DELETE",
            f"/api/v1/vpn-profiles/{profile.id}",
            headers=_auth(),
            json={"expected_revision": profile.revision},
        )

    blocked_by_policy = delete(policy_bound)
    assert blocked_by_policy.status_code == 409, blocked_by_policy.text
    assert (
        blocked_by_policy.json()["detail"]
        == "remove this VPN profile from project command policies first"
    )
    blocked_by_session = delete(session_bound)
    assert blocked_by_session.status_code == 409, blocked_by_session.text
    assert (
        blocked_by_session.json()["detail"]
        == "close active command sessions using this VPN profile first"
    )


def test_engagement_updates_validate_workspace_path_like_create(api, tmp_path):
    client, store, _ = api
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = client.post(
        "/api/v1/engagements",
        headers=_auth(),
        json={"name": "Linked", "workspace_path": str(workspace)},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    url = f"/api/v1/engagements/{project['id']}"

    patched = client.patch(
        url, headers=_auth(), json={"changes": {"workspace_path": "relative/missing"}}
    )
    assert patched.status_code == 422, patched.text
    replaced = client.put(
        url, headers=_auth(), json={**project, "workspace_path": str(tmp_path / "gone")}
    )
    assert replaced.status_code == 422, replaced.text
    assert store.get(Engagement, project["id"]).workspace_path == str(
        workspace.resolve()
    )

    moved = tmp_path / "moved"
    moved.mkdir()
    patched = client.patch(
        url,
        headers=_auth(),
        json={"changes": {"workspace_path": str(workspace / ".." / "moved")}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["workspace_path"] == str(moved.resolve())
    replaced = client.put(
        url,
        headers=_auth(),
        json={**patched.json(), "workspace_path": str(moved / ".." / "workspace")},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["workspace_path"] == str(workspace.resolve())


def test_harness_turn_interactions_are_listed_past_a_thousand_older_ones(api):
    from nebula.v3.domain import (
        HarnessInteraction,
        HarnessInteractionKind,
        HarnessSession,
        HarnessTurn,
        HarnessTurnOrigin,
    )

    client, store, _ = api
    engagement = store.create(Engagement(name="Long harness project"))
    session = store.create(
        HarnessSession(
            engagement_id=engagement.id, harness_profile_id="harness-1", model="m"
        )
    )
    older, newer = (
        store.create(
            HarnessTurn(
                engagement_id=engagement.id,
                harness_session_id=session.id,
                origin=HarnessTurnOrigin.MISSION,
                run_id="mission-run",
                prompt=prompt,
            )
        )
        for prompt in ("older", "newer")
    )
    base = utc_now()

    def interaction(turn: HarnessTurn, index: int, at) -> HarnessInteraction:
        return HarnessInteraction(
            engagement_id=engagement.id,
            harness_turn_id=turn.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.MISSION,
            run_id="mission-run",
            kind=HarnessInteractionKind.USER_INPUT,
            vendor_request_id=f"request-{turn.id}-{index}",
            created_at=at,
        )

    store.create_many(
        [
            interaction(older, index, base - timedelta(minutes=1))
            for index in range(1_000)
        ]
    )
    pending = store.create(interaction(newer, 0, base))
    url = f"/api/v1/harness-turns/{newer.id}/interactions"

    response = client.get(url, headers=_auth())

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [pending.id]
    filtered = client.get(url, headers=_auth(), params={"status": "pending"})
    assert [item["id"] for item in filtered.json()] == [pending.id]
    assert client.get(url, headers=_auth(), params={"status": "answered"}).json() == []


def test_live_paired_devices_are_listed_past_a_thousand_revoked_pairings(api):
    from nebula.v3.domain import PairedDeviceSession

    client, store, _ = api
    now = utc_now()
    expiry = now + timedelta(days=1)
    store.create_many(
        [
            PairedDeviceSession(
                id=f"revoked-{index}",
                name="Revoked phone",
                token_sha256=hashlib.sha256(f"revoked-{index}".encode()).hexdigest(),
                csrf_sha256="0" * 64,
                idle_expires_at=expiry,
                absolute_expires_at=expiry,
                revoked_at=now,
                created_at=now - timedelta(minutes=1),
            )
            for index in range(1_000)
        ]
    )
    live = store.create(
        PairedDeviceSession(
            id="live-phone",
            name="Live phone",
            token_sha256=hashlib.sha256(b"live").hexdigest(),
            csrf_sha256="0" * 64,
            idle_expires_at=expiry,
            absolute_expires_at=expiry,
        )
    )

    response = client.get("/api/v1/auth/devices", headers=_auth())

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [live.id]


def test_session_activity_covers_conversations_beyond_the_first_page(api):
    client, store, _ = api
    store.create(Engagement(id="busy-project", name="Busy project"))
    provider = store.create(
        ProviderProfile(
            id="busy-provider", name="OpenRouter", provider_type="openrouter"
        )
    )
    base = utc_now()
    store.create_many(
        [
            ChatSession(
                id=f"chat-{index:03d}",
                engagement_id="busy-project",
                title=f"Chat {index}",
                provider_profile_id=provider.id,
                model="model",
                created_at=base + timedelta(seconds=index),
                updated_at=base + timedelta(seconds=index),
            )
            for index in range(101)
        ]
    )
    store.create(
        ChatTurn(
            engagement_id="busy-project",
            session_id="chat-100",
            provider_profile_id=provider.id,
            model="model",
            status=ChatTurnStatus.WAITING_APPROVAL,
        )
    )

    response = client.get(
        "/api/v1/chat/session-activity",
        headers=_auth(),
        params={"engagement_id": "busy-project"},
    )

    assert response.status_code == 200, response.text
    states = {item["session_id"]: item["state"] for item in response.json()}
    assert len(states) == 101
    assert states["chat-100"] == "waiting"
    assert states["chat-000"] == "idle"


def test_terminal_output_page_always_advances_past_a_multibyte_character(api):
    from nebula.v3.terminal_history import CapturedTerminalCommand

    client, store, _ = api
    project = store.create(Engagement(name="Terminal paging"))
    history = client.app.state.terminal_command_history
    output = "a☃b\n".encode()
    now = utc_now()
    record = history.record_capture(
        engagement_id=project.id,
        session_id="terminal-paging",
        operator_id="operator",
        capture=CapturedTerminalCommand(
            shell_sequence="1",
            command="printf snowman",
            cwd="/workspace",
            status="completed",
            exit_code=0,
            started_at=now,
            completed_at=now,
            output=output,
            observed_output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_truncated=False,
        ),
    )
    url = f"/api/v1/engagements/{project.id}/terminal/commands/{record.id}/output"
    snowman = output.index("☃".encode())

    page = client.get(url, headers=_auth(), params={"offset": snowman, "limit": 1})

    assert page.status_code == 200, page.text
    assert page.content == "☃".encode()
    assert page.headers["X-Nebula-Output-Next"] == str(snowman + len("☃".encode()))


def test_tool_call_artifacts_are_listed_past_a_thousand_older_ones(api):
    from nebula.v3.domain import Artifact
    from nebula.v3.domain import ToolCall as ToolCallEntity

    client, store, _ = api
    engagement = store.create(Engagement(name="Artifact-heavy project"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="Collect"))
    call = store.create(
        ToolCallEntity(
            engagement_id=engagement.id,
            run_id=run.id,
            tool_name="shell",
            risk_class=RiskClass.LOCAL_READ,
        )
    )
    base = utc_now()

    def artifact(index: int, tool_call_id: str, at) -> Artifact:
        return Artifact(
            engagement_id=engagement.id,
            sha256="0" * 64,
            size=1,
            storage_path=f"artifacts/{tool_call_id}/{index}",
            metadata={"tool_call_id": tool_call_id},
            created_at=at,
        )

    store.create_many(
        [
            artifact(index, "older-call", base - timedelta(minutes=1))
            for index in range(1_000)
        ]
    )
    newest = store.create(artifact(0, call.id, base))

    response = client.get(f"/api/v1/tool-calls/{call.id}/artifacts", headers=_auth())

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [newest.id]
