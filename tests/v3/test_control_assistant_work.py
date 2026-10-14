from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.control_assistant_work import WORK_TOOLS, dispatch_work_tool
from nebula.v3.domain import (
    AgentRun,
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
    ProviderProfile,
    ScopePolicy,
)
from nebula.v3.runtime_platform import RuntimePlatform, _ControlAssistantScopeBroker
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore


def _app(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    return store, app, runtime


def test_work_tools_are_bound_to_control_assistant_and_not_ordinary_chat(tmp_path):
    store, app, runtime = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    common = dict(
        engagement_id=project.id,
        turn_id="turn",
        provider=None,
        model="model-a",
        allow_empty=True,
    )
    ordinary = runtime.chat_components(**common)
    control = runtime.chat_components(**common, settings_assistant=True)
    expected = {name for name, *_ in WORK_TOOLS}
    assert not expected.intersection(ordinary.specs)
    assert expected.issubset(control.specs)
    assert app.state.control_assistant_work.catalog(project.id)["scope"].startswith(
        "selected project"
    )


def test_harness_control_session_advertises_work_tools_but_ordinary_chat_does_not(
    tmp_path,
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Local fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(models=["model-a"]),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    harness = app.state.harness_runtime_service
    _, _, ordinary_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="ordinary chat",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    _, _, control_turn = harness.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="check project work",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        control_assistant=True,
    )
    expected = {name for name, *_ in WORK_TOOLS}
    ordinary = harness._gateway_catalog(
        store.get(HarnessSession, ordinary_turn.harness_session_id), turn=ordinary_turn
    )
    control = harness._gateway_catalog(
        store.get(HarnessSession, control_turn.harness_session_id), turn=control_turn
    )
    assert not expected.intersection(item["name"] for item in ordinary["tools"])
    assert expected.issubset(item["name"] for item in control["tools"])


def test_work_roster_and_details_reject_another_project_or_control_chat(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    profile = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    own = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Investigate",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    private = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Nebula assistant",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"control_assistant": True},
        )
    )
    foreign = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Other chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    run = store.create(AgentRun(engagement_id=selected.id, objective="Check status"))
    foreign_run = store.create(AgentRun(engagement_id=other.id, objective="Other work"))
    work = app.state.control_assistant_work
    overview = work.overview(selected.id)
    assert [item["id"] for item in overview["chats"]] == [own.id]
    assert [item["id"] for item in overview["missions"]] == [run.id]
    assert overview["chats"][0]["url"].endswith(f"session={own.id}")
    assert overview["missions"][0]["url"].endswith(f"mission={run.id}")
    assert work.chat_detail(selected.id, own.id)["pending_turn"] is None
    assert work.mission_detail(selected.id, run.id)["status"] == "queued"
    for target in (private.id, foreign.id):
        with pytest.raises(ValueError, match="this project's work roster"):
            work.chat_detail(selected.id, target)
    with pytest.raises(ValueError, match="this project's work roster"):
        work.mission_detail(selected.id, foreign_run.id)


def test_chat_follow_up_is_durable_and_project_bound(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    profile = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    session = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Investigate",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await dispatch_work_tool(
                work,
                other.id,
                "nebula.work.message_chat",
                {"session_id": session.id, "text": "Continue"},
            )
        queued = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.message_chat",
            {"session_id": session.id, "text": "Check the latest result"},
        )
        assert queued["delivery"] == "queued"
        saved = work.queue.get(session.id)
        assert saved.items[0]["id"] == queued["item_id"]
        assert (
            saved.items[0]["request"]["messages"][0]["content"]
            == "Check the latest result"
        )
        assert (
            work.chat_detail(selected.id, session.id)["queued_follow_ups"][0]["status"]
            == "queued"
        )
        with pytest.raises(ValueError, match="active harness chat"):
            await work.message_chat(
                selected.id,
                {"session_id": session.id, "text": "Interrupt", "delivery": "steer"},
            )

    asyncio.run(scenario())


def test_start_chat_saves_a_separate_turn_for_core_owned_work(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    work = app.state.control_assistant_work

    class InertProvider(ModelProvider):
        def __init__(self, profile_id):
            super().__init__(
                ProviderConfig(
                    id=profile_id,
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="http://127.0.0.1:1/v1",
                    default_model="model-a",
                    model_allowlist=["model-a"],
                    local=True,
                    capabilities=ModelCapabilities(streaming=True),
                )
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("This test prepares work; no provider request is made")

        async def health(self) -> ProviderHealth:
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=["model-a"]
            )

    monkeypatch.setattr(
        work.chat, "provider_factory", lambda profile: InertProvider(profile.id)
    )
    starts = []
    monkeypatch.setattr(
        work.chat,
        "start_provider_turn",
        lambda prepared: starts.append(prepared.turn.id),
    )

    async def scenario():
        result = await work.start_chat(
            project.id, {"prompt": "Investigate the project state"}
        )
        assert result["status"] == "started"
        assert result["url"].endswith(f"session={result['session_id']}")
        assert starts == [result["turn_id"]]
        saved = store.get(ChatSession, result["session_id"])
        assert saved.engagement_id == project.id
        assert saved.metadata.get("control_assistant") is not True
        assert (
            work.chat_detail(project.id, saved.id)["pending_turn"]["id"]
            == result["turn_id"]
        )

    asyncio.run(scenario())


def test_start_remote_harness_chat_keeps_peer_messaging_off(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Remote Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(models=["model-a"], mcp=True),
            privacy={"local_only": False, "permits_sensitive_data": True},
        )
    )
    work = app.state.control_assistant_work
    started = []
    monkeypatch.setattr(
        work.harness, "start_chat_turn", lambda turn_id: started.append(turn_id)
    )

    async def scenario():
        result = await work.start_chat(
            project.id,
            {
                "backend": "harness",
                "profile_id": profile.id,
                "prompt": "Reply with one line without tools",
            },
        )
        assert result["backend"] == "harness"
        assert started
        session = store.get(ChatSession, result["session_id"])
        assert session.metadata.get("allow_agent_messaging") is False
        assert session.metadata.get("control_assistant") is not True

    asyncio.run(scenario())


def test_mission_start_uses_existing_core_route_and_reads_back(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    calls = []

    async def start_mission(**kwargs):
        calls.append(kwargs)
        return store.create(
            AgentRun(
                engagement_id=kwargs["engagement_id"],
                objective=kwargs["objective"],
                metadata={"name": kwargs["name"]},
            )
        )

    monkeypatch.setattr(app.state.mission_service, "start_mission", start_mission)
    work = app.state.control_assistant_work

    async def scenario():
        result = await work.start_mission(
            selected.id, {"name": "Survey", "objective": "Inspect the saved project"}
        )
        assert result["status"] == "queued"
        assert result["run_id"]
        assert result["url"].endswith(f"mission={result['run_id']}")
        assert calls[0]["engagement_id"] == selected.id
        assert calls[0]["tool_names"] == []
        assert work.mission_detail(selected.id, result["run_id"])["name"] == "Survey"

    asyncio.run(scenario())


def test_selected_harness_chat_can_be_steered_and_stopped(tmp_path, monkeypatch):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    profile = store.create(
        HarnessProfile(
            name="Codex", kind=HarnessKind.CODEX_APP_SERVER, executable="/bin/true"
        )
    )
    session = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Running chat",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id="harness-session",
            model="model-a",
        )
    )
    work = app.state.control_assistant_work
    active = [SimpleNamespace(id="chat-turn", harness_turn_id="harness-turn")]
    calls = []
    monkeypatch.setattr(
        work.chat, "pending_turn", lambda _id: active[0] if active else None
    )

    async def steer(turn_id, text, *, actor_id):
        calls.append(("steer", turn_id, text, actor_id))

    async def cancel(turn_id, *, reason):
        calls.append(("stop", turn_id, reason))
        active.clear()

    monkeypatch.setattr(work.harness, "steer_turn", steer)
    monkeypatch.setattr(work.harness, "cancel_turn", cancel)

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await work.message_chat(
                other.id,
                {"session_id": session.id, "text": "Pause", "delivery": "steer"},
            )
        guided = await work.message_chat(
            selected.id,
            {
                "session_id": session.id,
                "text": "Check only the summary",
                "delivery": "steer",
            },
        )
        assert guided == {
            "session_id": session.id,
            "delivery": "steered",
            "turn_id": "chat-turn",
        }
        assert calls[0] == (
            "steer",
            "harness-turn",
            "Check only the summary",
            "control-assistant",
        )
        stopped = await work.stop_chat(selected.id, session.id)
        assert stopped["stopped"] is True
        assert calls[1][:2] == ("stop", "harness-turn")

    asyncio.run(scenario())


def test_mission_interrupt_uses_core_route_and_rejects_foreign_target(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    run = store.create(
        AgentRun(
            engagement_id=selected.id,
            objective="Inspect synthetic data",
            metadata={"origin": "api"},
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await work.stop_mission(other.id, run.id)
        with pytest.raises(ValueError, match="active harness missions only"):
            await work.steer_mission(
                selected.id, {"run_id": run.id, "text": "Focus on summary"}
            )
        stopped = await work.stop_mission(selected.id, run.id)
        assert stopped["run_id"] == run.id
        assert stopped["stopped"] is True
        assert work.mission_detail(selected.id, run.id)["status"] == "cancelled"

    asyncio.run(scenario())


def test_chat_settings_are_project_bound_revision_guarded_and_used_by_next_message(
    tmp_path,
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    profile = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Research",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work
    before = work.chat_settings(project.id, chat.id)
    assert before["revision"] == chat.revision
    assert before["settings"]["model"] == "model-a"
    with pytest.raises(ValueError, match="work roster"):
        work.chat_settings(other.id, chat.id)
    with pytest.raises(ValueError, match="conversation changed"):
        work.set_chat_settings(
            project.id,
            {
                "session_id": chat.id,
                "expected_revision": chat.revision + 1,
                "settings": {"reasoning_effort": "high"},
            },
        )
    changed = work.set_chat_settings(
        project.id,
        {
            "session_id": chat.id,
            "expected_revision": chat.revision,
            "settings": {
                "reasoning_effort": "high",
                "include_knowledge": False,
                "allow_agent_messaging": True,
            },
        },
    )
    assert changed["revision"] > chat.revision
    assert changed["settings"]["reasoning_effort"] == "high"
    assert (
        store.get(ChatSession, chat.id).metadata["assistant_config"][
            "include_knowledge"
        ]
        is False
    )

    async def queue_next():
        await work.message_chat(
            project.id,
            {"session_id": chat.id, "text": "Continue with the new settings"},
        )

    asyncio.run(queue_next())
    request = work.queue.get(chat.id).items[0]["request"]
    assert request["reasoning_effort"] == "high"
    assert request["include_knowledge"] is False
    assert request["allow_agent_messaging"] is True


def test_nebula_can_change_its_own_advertised_harness_choices(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(
                models=["model-a"],
                modes=["plan"],
                model_options=[
                    HarnessModelOptions(
                        model="model-a",
                        reasoning_efforts=[
                            HarnessRuntimeOption(id="high", label="High")
                        ],
                        service_tiers=[HarnessRuntimeOption(id="fast", label="Fast")],
                        cyber_access_programs=["standard"],
                    )
                ],
            ),
        )
    )
    harness = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="model-a",
        )
    )
    control = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Nebula assistant",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=harness.id,
            model="model-a",
            metadata={"control_assistant": True},
        )
    )
    work = app.state.control_assistant_work
    before = work.chat_settings(project.id, "self")
    assert before["session_id"] == control.id
    assert before["available"]["harness_service_tiers"][0]["id"] == "fast"
    changed = work.set_chat_settings(
        project.id,
        {
            "session_id": "self",
            "expected_revision": before["revision"],
            "settings": {
                "harness_reasoning_effort": "high",
                "harness_service_tier": "fast",
                "harness_mode": "plan",
                "harness_cyber_access_program": "standard",
            },
        },
    )
    assert changed["settings"]["harness_service_tier"] == "fast"
    assert changed["settings"]["harness_cyber_access_program"] == "standard"
    with pytest.raises(ValueError, match="does not advertise that speed"):
        work.set_chat_settings(
            project.id,
            {
                "session_id": "self",
                "expected_revision": changed["revision"],
                "settings": {"harness_service_tier": "imaginary"},
            },
        )


def test_control_tool_scope_bypass_is_limited_to_product_control_names(tmp_path):
    project = Engagement(name="Selected")
    scope = ScopePolicy(engagement_id=project.id, bypass_permissions=False)
    observed = []

    class Recorder:
        async def execute(self, invocation, effective_scope, *, approval=None):
            observed.append((invocation.tool_name, effective_scope.bypass_permissions))
            return None

    broker = _ControlAssistantScopeBroker(Recorder(), {"nebula.work.chat_settings"})

    async def run():
        await broker.execute(
            SimpleNamespace(tool_name="nebula.work.chat_settings"), scope
        )
        await broker.execute(SimpleNamespace(tool_name="mcp.other.change"), scope)

    asyncio.run(run())
    assert observed == [
        ("nebula.work.chat_settings", True),
        ("mcp.other.change", False),
    ]


def test_chat_settings_api_reads_back_durable_next_turn_choice(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(
            name="Local", provider_type="ollama", is_local=True, model_allowlist=["model-a"]
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Research",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    client = TestClient(app)
    url = f"/api/v1/chat-sessions/{chat.id}/assistant-config"
    assert client.get(url).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    before = client.get(url, headers=headers)
    assert before.status_code == 200, before.text
    changed = client.patch(
        url,
        headers=headers,
        json={
            "expected_revision": before.json()["revision"],
            "settings": {"reasoning_effort": "high", "include_knowledge": False},
        },
    )
    assert changed.status_code == 200, changed.text
    after = client.get(url, headers=headers).json()
    assert after["settings"]["reasoning_effort"] == "high"
    assert after["settings"]["include_knowledge"] is False
    stale = client.patch(
        url,
        headers=headers,
        json={
            "expected_revision": before.json()["revision"],
            "settings": {"reasoning_effort": "low"},
        },
    )
    assert stale.status_code in {400, 409, 422}
