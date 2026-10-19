from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatCompletionRequest, ChatRequestMessage
from nebula.v3.butler_capabilities import BUTLER_TOOL_NAMES
from nebula.v3.control_assistant_work import WORK_TOOLS, dispatch_work_tool
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatGoal,
    ChatMessage,
    ChatQueue,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
    HarnessInteraction,
    HarnessInteractionKind,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    RiskClass,
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
from nebula.v3.storage import NebulaStore, NotFoundError


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
    expected = BUTLER_TOOL_NAMES
    assert not expected.intersection(ordinary.specs)
    assert expected.issubset(control.specs)
    assert "nebula.work.manage" not in control.specs
    assert app.state.control_assistant_work.catalog(project.id)["scope"].startswith(
        "selected project"
    )


def test_managed_goal_tools_are_discoverable_without_advertising_every_schema(tmp_path):
    store, app, runtime = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    work = app.state.control_assistant_work
    names = {name for name, *_ in WORK_TOOLS}
    assert "nebula.work.manage" in names
    assert "nebula.work.goals" not in names
    descriptor = asyncio.run(
        dispatch_work_tool(
            work, project.id, "nebula.work.catalog", {"operation": "nebula.work.goals"}
        )
    )
    assert descriptor["name"] == "nebula.work.goals"
    assert descriptor["read_only"] is True
    assert asyncio.run(
        dispatch_work_tool(
            work,
            project.id,
            "nebula.work.manage",
            {"operation": "nebula.work.goals", "arguments": {}},
        )
    ) == work.goals_overview(project.id, 0)
    with pytest.raises(ValueError, match="managed operation"):
        asyncio.run(
            dispatch_work_tool(
                work,
                project.id,
                "nebula.work.manage",
                {"operation": "nebula.work.start_mission", "arguments": {}},
            )
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
    expected = BUTLER_TOOL_NAMES
    ordinary = harness._gateway_catalog(
        store.get(HarnessSession, ordinary_turn.harness_session_id), turn=ordinary_turn
    )
    control = harness._gateway_catalog(
        store.get(HarnessSession, control_turn.harness_session_id), turn=control_turn
    )
    assert not expected.intersection(item["name"] for item in ordinary["tools"])
    assert expected.issubset(item["name"] for item in control["tools"])
    assert not any(item["name"].startswith("nebula.work.") for item in control["tools"])


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


def test_work_roster_separates_main_chats_subagents_and_goal_children(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main researcher",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Subagent · files",
            provider_profile_id=provider.id,
            model="model-a",
            parent_session_id=parent.id,
            metadata={"subagent_id": "child-1"},
        )
    )
    record = store.create(
        ChatSubagent(
            id="child-1",
            engagement_id=project.id,
            parent_session_id=parent.id,
            parent_turn_id="parent-turn",
            child_session_id=child.id,
            name="Files",
            task="Inspect files",
        )
    )
    goal_child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Goal child",
            provider_profile_id=provider.id,
            model="model-a",
            parent_session_id=parent.id,
            metadata={"delegated_from_goal_id": "parent-goal"},
        )
    )
    store.create(
        ChatGoal(
            id="parent-goal",
            engagement_id=project.id,
            session_id=parent.id,
            objective="Review project",
            completion_criteria=["Report ready"],
        )
    )
    store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=goal_child.id,
            parent_goal_id="parent-goal",
            objective="Review child topic",
            completion_criteria=["Child report ready"],
        )
    )
    work = app.state.control_assistant_work
    overview = work.overview(project.id)
    assert [item["id"] for item in overview["chats"]] == [parent.id]
    assert overview["chats"][0]["goal"]["status"] == "draft"
    assert overview["subagents"][0]["id"] == record.id
    assert overview["subagents"][0]["parent_session_id"] == parent.id
    assert work.chat_detail(project.id, child.id)["kind"] == "subagent"
    assert work.chat_detail(project.id, goal_child.id)["kind"] == "goal_child"
    assert work.subagent_detail(project.id, record.id)["view"]["status"] == "running"
    assert {item["kind"] for item in work.goals_overview(project.id)["goals"]} == {
        "main",
        "goal_child",
    }


def test_chat_fork_and_delete_are_project_bound_and_revision_guarded(tmp_path):
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
    source = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Research",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"butler_initiated": True},
        )
    )
    message = store.create(
        ChatMessage(
            engagement_id=selected.id,
            session_id=source.id,
            sequence=1,
            role=ChatRole.USER,
            content="Investigate the issue",
        )
    )
    deletable = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Old work",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    foreign = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Other work",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await work.fork_chat(
                selected.id,
                {
                    "session_id": foreign.id,
                    "expected_revision": foreign.revision,
                    "through_message_id": message.id,
                },
            )
        with pytest.raises(ValueError, match="saved transcript"):
            await work.fork_chat(
                selected.id,
                {
                    "session_id": source.id,
                    "expected_revision": source.revision,
                    "through_message_id": "invented",
                },
            )
        fork = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.fork_chat",
            {
                "session_id": source.id,
                "expected_revision": source.revision,
                "through_message_id": message.id,
            },
        )
        assert fork["parent_session_id"] == source.id
        assert (
            store.get(ChatSession, fork["session_id"]).metadata["butler_initiated"]
            is True
        )
        ordinary_fork = work.chat.fork_session(source.id, through_message_id=message.id)
        assert ordinary_fork.metadata.get("butler_initiated") is not True
        assert work.chat_detail(selected.id, fork["session_id"])["kind"] == "main"
        assert (
            work.chat_detail(selected.id, source.id)["recent_messages"][0]["id"]
            == message.id
        )
        with pytest.raises(ValueError, match="title changed"):
            await work.delete_chat(
                selected.id,
                {
                    "session_id": deletable.id,
                    "expected_revision": deletable.revision,
                    "confirm_title": "Wrong title",
                },
            )
        with pytest.raises(ValueError, match="chat changed"):
            await work.delete_chat(
                selected.id,
                {
                    "session_id": deletable.id,
                    "expected_revision": deletable.revision + 1,
                    "confirm_title": deletable.title,
                },
            )
        deleted = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.delete_chat",
            {
                "session_id": deletable.id,
                "expected_revision": deletable.revision,
                "confirm_title": deletable.title,
            },
        )
        assert deleted["deleted"] is True
        with pytest.raises(NotFoundError):
            store.get(ChatSession, deletable.id)

    asyncio.run(scenario())


def test_retry_chat_turn_reads_exact_target_and_reuses_saved_retry(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    source = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Failed chat",
            backend=ChatBackend.HARNESS,
            model="model-a",
            harness_profile_id="harness-profile",
            harness_session_id="harness-session",
        )
    )
    turn = store.create(
        HarnessTurn(
            engagement_id=selected.id,
            harness_session_id="harness-session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=source.id,
            chat_turn_id="chat-turn",
            prompt="Try the task",
            status=HarnessTurnStatus.FAILED,
        )
    )
    work = app.state.control_assistant_work
    assert (
        work.chat_detail(selected.id, source.id)["recent_harness_turns"][0]["id"]
        == turn.id
    )
    requests = []

    async def fake_request(method, path, body, **kwargs):
        requests.append((method, path, body))
        replacement = store.create(
            HarnessTurn(
                engagement_id=selected.id,
                harness_session_id="harness-session",
                origin=HarnessTurnOrigin.CHAT,
                chat_session_id=source.id,
                chat_turn_id="retry-chat-turn",
                prompt="Try the task",
                status=HarnessTurnStatus.QUEUED,
                metadata={"retry_of_turn_id": turn.id},
            )
        )
        return {"id": replacement.id}

    monkeypatch.setattr(work, "_request", fake_request)

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await work.retry_chat_turn(
                other.id,
                {
                    "session_id": source.id,
                    "turn_id": turn.id,
                    "expected_revision": turn.revision,
                },
            )
        with pytest.raises(ValueError, match="chat turn changed"):
            await work.retry_chat_turn(
                selected.id,
                {
                    "session_id": source.id,
                    "turn_id": turn.id,
                    "expected_revision": turn.revision + 1,
                },
            )
        started = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.retry_chat_turn",
            {
                "session_id": source.id,
                "turn_id": turn.id,
                "expected_revision": turn.revision,
            },
        )
        reused = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.retry_chat_turn",
            {
                "session_id": source.id,
                "turn_id": turn.id,
                "expected_revision": turn.revision,
            },
        )
        assert started["turn_id"] == reused["turn_id"]
        assert reused["retry_of_turn_id"] == turn.id
        assert reused["status"] == "queued"
        assert len(requests) == 1

    asyncio.run(scenario())


def test_goal_controls_read_core_state_and_reject_stale_or_foreign_targets(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Goal chat",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    foreign = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign chat",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        created = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.create_goal",
            {
                "session_id": chat.id,
                "objective": "Inspect code",
                "completion_criteria": ["Evidence retained"],
                "plan": ["Read", "Report"],
            },
        )
        revision = created["goal"]["revision"]
        updated = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.update_goal",
            {
                "session_id": chat.id,
                "expected_revision": revision,
                "objective": "Inspect code and summarize",
            },
        )
        assert updated["goal"]["completion_criteria"] == ["Evidence retained"]
        with pytest.raises(ValueError, match="goal changed"):
            await dispatch_work_tool(
                work,
                selected.id,
                "nebula.work.goal_action",
                {
                    "session_id": chat.id,
                    "expected_revision": revision,
                    "action": "cancel",
                },
            )
        cancelled = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.goal_action",
            {
                "session_id": chat.id,
                "expected_revision": updated["goal"]["revision"],
                "action": "cancel",
            },
        )
        assert cancelled["goal"]["status"] == "cancelled"
        assert work.goals_overview(selected.id)["goals"][0]["status"] == "cancelled"
        with pytest.raises(ValueError, match="this project's work roster"):
            await dispatch_work_tool(
                work,
                selected.id,
                "nebula.work.create_goal",
                {
                    "session_id": foreign.id,
                    "objective": "Wrong project",
                    "completion_criteria": ["No"],
                },
            )

    asyncio.run(scenario())


def test_new_goal_conversation_is_draft_until_explicit_start(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        created = await dispatch_work_tool(
            work,
            project.id,
            "nebula.work.create_goal_conversation",
            {
                "backend": "provider",
                "profile_id": provider.id,
                "model": "model-a",
                "objective": "Map the files",
                "completion_criteria": ["Report saved"],
            },
        )
        assert created["kind"] == "main"
        assert created["goal"]["status"] == "draft"
        assert work.overview(project.id)["chats"][0]["id"] == created["session_id"]
        assert work.goals_overview(project.id)["goals"][0]["status"] == "draft"
        assert (
            store.get(ChatSession, created["session_id"]).metadata["butler_initiated"]
            is True
        )

    asyncio.run(scenario())


def test_goal_child_creation_keeps_lineage_and_project_scope(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Parent",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    store.create(
        ChatGoal(
            engagement_id=selected.id,
            session_id=parent.id,
            objective="Investigate",
            completion_criteria=["Report ready"],
            status="running",
            child_budget=1,
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await dispatch_work_tool(
                work,
                other.id,
                "nebula.work.create_goal_child",
                {
                    "session_id": parent.id,
                    "objective": "Wrong project",
                    "completion_criteria": ["No"],
                },
            )
        created = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.create_goal_child",
            {
                "session_id": parent.id,
                "objective": "Review one component",
                "completion_criteria": ["Findings saved"],
            },
        )
        assert created["kind"] == "goal_child"
        assert created["goal"]["status"] == "draft"
        assert (
            store.get(ChatSession, created["session_id"]).parent_session_id == parent.id
        )
        assert (
            work.goal_detail(selected.id, parent.id)["children"][0]["session_id"]
            == created["session_id"]
        )
        assert work.overview(selected.id)["chats"][0]["id"] == parent.id

    asyncio.run(scenario())


def test_schedule_control_reads_saved_state_and_rejects_stale_or_foreign_targets(
    tmp_path,
):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Recurring review",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    foreign = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work
    assert work.schedule_detail(selected.id, chat.id)["schedule"] is None

    async def scenario():
        created = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.create_schedule",
            {
                "session_id": chat.id,
                "interval_seconds": 3600,
            },
        )
        assert created["schedule"]["enabled"] is True
        assert created["schedule"]["next_run_at"]
        paused = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.set_schedule",
            {
                "session_id": chat.id,
                "expected_revision": created["schedule"]["revision"],
                "enabled": False,
            },
        )
        assert paused["schedule"]["enabled"] is False
        with pytest.raises(ValueError, match="schedule changed"):
            await dispatch_work_tool(
                work,
                selected.id,
                "nebula.work.set_schedule",
                {
                    "session_id": chat.id,
                    "expected_revision": created["schedule"]["revision"],
                    "enabled": True,
                },
            )
        with pytest.raises(ValueError, match="this project's work roster"):
            work.schedule_detail(selected.id, foreign.id)

    asyncio.run(scenario())


def test_subagent_stop_reads_back_and_rejects_another_project(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Main",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    child = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Subagent · review",
            provider_profile_id=provider.id,
            model="model-a",
            parent_session_id=parent.id,
            metadata={"subagent_id": "sub-1"},
        )
    )
    record = store.create(
        ChatSubagent(
            id="sub-1",
            engagement_id=selected.id,
            parent_session_id=parent.id,
            parent_turn_id="parent-turn",
            child_session_id=child.id,
            name="Review",
            task="Read the code",
        )
    )
    work = app.state.control_assistant_work

    async def scenario():
        with pytest.raises(ValueError, match="this project's work roster"):
            await work.stop_subagent(other.id, record.id)
        stopped = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.stop_subagent",
            {
                "subagent_id": record.id,
            },
        )
        assert stopped["status"] == "stopped"
        assert (
            work.subagents_overview(selected.id, {})["subagents"][0]["status"]
            == "stopped"
        )

    asyncio.run(scenario())


def test_queue_control_is_revision_guarded_and_keeps_project_boundary(tmp_path):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Queued chat",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    foreign = store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign chat",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    work = app.state.control_assistant_work
    request = ChatCompletionRequest(
        engagement_id=selected.id,
        session_id=chat.id,
        provider_id=provider.id,
        model="model-a",
        messages=[ChatRequestMessage(role="user", content="First wording")],
    )
    queue = store.create(
        ChatQueue(
            id="chat-queue-" + chat.id,
            engagement_id=selected.id,
            session_id=chat.id,
            paused=True,
            items=[
                {
                    "id": "follow-up-1",
                    "key": "key-1",
                    "status": "queued",
                    "original_request": request.model_dump(mode="json"),
                    "request": request.model_dump(mode="json"),
                }
            ],
        )
    )
    assert work.queue_detail(selected.id, chat.id)["paused"] is True
    resumed = work.queue_action(
        selected.id,
        {
            "session_id": chat.id,
            "expected_revision": queue.revision,
            "action": "resume",
        },
    )
    assert resumed["paused"] is False
    edited = work.queue_action(
        selected.id,
        {
            "session_id": chat.id,
            "expected_revision": resumed["revision"],
            "action": "edit",
            "item_id": "follow-up-1",
            "text": "Revised wording",
        },
    )
    assert edited["items"][0]["text"] == "Revised wording"
    assert (
        store.get(ChatQueue, queue.id).items[0]["request"]["messages"][0]["content"]
        == "Revised wording"
    )
    with pytest.raises(ValueError, match="queue changed"):
        work.queue_action(
            selected.id,
            {
                "session_id": chat.id,
                "expected_revision": queue.revision,
                "action": "pause",
            },
        )
    with pytest.raises(ValueError, match="this project's work roster"):
        work.queue_detail(selected.id, foreign.id)


def test_control_assistant_reviews_and_decides_exact_project_approval(tmp_path):
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
    chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Working chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=selected.id,
            session_id=chat.id,
            provider_profile_id=profile.id,
            model="model-a",
            status="waiting_approval",
        )
    )
    approval = store.create(
        Approval(
            engagement_id=selected.id,
            run_id=turn.id,
            origin="chat",
            chat_session_id=chat.id,
            chat_turn_id=turn.id,
            risk_class=RiskClass.WORKSPACE_WRITE,
            exact_request={
                "tool_name": "write_file",
                "arguments": {"path": "report.md"},
            },
            target="report.md",
            expected_effects=["Write report.md"],
            policy_rationale="Workspace write requires a decision",
            requested_by="Working chat",
        )
    )
    foreign_run = store.create(
        AgentRun(engagement_id=other.id, objective="Foreign mission")
    )
    foreign = store.create(
        Approval(
            engagement_id=other.id,
            run_id=foreign_run.id,
            risk_class=RiskClass.ACTIVE_SCAN,
            exact_request={"tool_name": "scan"},
            policy_rationale="Review scan",
            requested_by="Foreign mission",
        )
    )
    work = app.state.control_assistant_work
    attention = work.attention(selected.id)
    assert [item["action_id"] for item in attention["items"]] == [approval.id]
    detail = work.approval_detail(selected.id, approval.id)
    assert detail["tool_name"] == "write_file"
    assert detail["expected_effects"] == ["Write report.md"]
    assert detail["revision"] == approval.revision
    with pytest.raises(ValueError, match="this project's attention list"):
        work.approval_detail(selected.id, foreign.id)

    async def scenario():
        with pytest.raises(ValueError, match="approval changed"):
            await dispatch_work_tool(
                work,
                selected.id,
                "nebula.work.decide_approval",
                {
                    "action_id": approval.id,
                    "expected_revision": approval.revision + 1,
                    "decision": "approve",
                },
            )
        assert store.get(Approval, approval.id).status == ApprovalStatus.PENDING
        return await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.decide_approval",
            {
                "action_id": approval.id,
                "expected_revision": detail["revision"],
                "decision": "approve",
                "reason": "Operator approved this exact file write",
            },
        )

    result = asyncio.run(scenario())
    assert result["status"] == "approved"
    assert result["decision_recorded"] is True
    assert "session=" + chat.id in result["url"]
    assert work.attention(selected.id)["count"] == 0
    assert (
        store.get(Approval, approval.id).decision_note
        == "Operator approved this exact file write"
    )


def test_control_assistant_answers_pending_harness_question_without_leaking_secret(
    tmp_path,
):
    store, app, _ = _app(tmp_path)
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    run = store.create(AgentRun(engagement_id=selected.id, objective="Audit scope"))
    harness_session = store.create(
        HarnessSession(
            engagement_id=selected.id, harness_profile_id="fixture", model="model-a"
        )
    )
    harness_turn = store.create(
        HarnessTurn(
            engagement_id=selected.id,
            harness_session_id=harness_session.id,
            origin=HarnessTurnOrigin.MISSION,
            run_id=run.id,
            prompt="Audit scope",
        )
    )
    question = store.create(
        HarnessInteraction(
            engagement_id=selected.id,
            harness_turn_id=harness_turn.id,
            harness_session_id=harness_session.id,
            origin=HarnessTurnOrigin.MISSION,
            kind=HarnessInteractionKind.USER_INPUT,
            run_id=run.id,
            vendor_request_id="question-1",
            prompt="Which area should I inspect?",
            questions=[{"id": "area", "header": "Area", "options": ["UI", "API"]}],
        )
    )
    secret = store.create(
        HarnessInteraction(
            engagement_id=selected.id,
            harness_turn_id=harness_turn.id,
            harness_session_id=harness_session.id,
            origin=HarnessTurnOrigin.MISSION,
            kind=HarnessInteractionKind.MCP_ELICITATION,
            run_id=run.id,
            vendor_request_id="question-2",
            prompt="Enter a private token",
            contains_secret=True,
        )
    )
    work = app.state.control_assistant_work
    assert {item["action_id"] for item in work.attention(selected.id)["items"]} == {
        question.id,
        secret.id,
    }
    detail = work.question_detail(selected.id, question.id)
    assert detail["prompt"] == "Which area should I inspect?"
    with pytest.raises(ValueError, match="this project's attention list"):
        work.question_detail(other.id, question.id)

    async def scenario():
        with pytest.raises(ValueError, match="contains a secret"):
            await work.answer_question(
                selected.id,
                {
                    "action_id": secret.id,
                    "expected_revision": secret.revision,
                    "action": "answer",
                    "response": {"token": "nope"},
                },
            )
        future = asyncio.get_running_loop().create_future()
        app.state.harness_runtime_service._interaction_futures[question.id] = future
        result = await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.answer_question",
            {
                "action_id": question.id,
                "expected_revision": detail["revision"],
                "action": "answer",
                "response": {"area": "API"},
            },
        )
        secret_future = asyncio.get_running_loop().create_future()
        app.state.harness_runtime_service._interaction_futures[secret.id] = (
            secret_future
        )
        declined = await work.answer_question(
            selected.id,
            {
                "action_id": secret.id,
                "expected_revision": secret.revision,
                "action": "decline",
            },
        )
        return result, future.result(), declined, secret_future.result()

    result, delivered, declined, secret_delivery = asyncio.run(scenario())
    assert result["status"] == "answered"
    assert delivered == {"action": "answer", "response": {"area": "API"}}
    assert declined["status"] == "declined"
    assert secret_delivery == {"action": "decline", "response": {}}
    assert work.question_detail(selected.id, question.id)["status"] == "answered"


def test_control_assistant_updates_chat_and_reuses_saved_mission_retry(
    tmp_path, monkeypatch
):
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
    chat = store.create(
        ChatSession(
            engagement_id=selected.id,
            title="Old title",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    run = store.create(
        AgentRun(engagement_id=selected.id, objective="Review scope", status="failed")
    )
    work = app.state.control_assistant_work

    async def update():
        return await dispatch_work_tool(
            work,
            selected.id,
            "nebula.work.update_chat",
            {
                "session_id": chat.id,
                "expected_revision": chat.revision,
                "title": "New title",
                "archived": True,
            },
        )

    changed = asyncio.run(update())
    assert changed["title"] == "New title"
    assert changed["archived"] is True
    assert work.chat_detail(selected.id, chat.id)["archived"] is True
    with pytest.raises(ValueError, match="chat changed"):
        asyncio.run(update())
    with pytest.raises(ValueError, match="this project's work roster"):
        asyncio.run(
            work.update_chat(
                other.id,
                {
                    "session_id": chat.id,
                    "expected_revision": changed["revision"],
                    "archived": False,
                },
            )
        )

    calls = []

    async def fake_retry(method, path, body):
        calls.append((method, path, body))
        saved = store.create(
            AgentRun(
                engagement_id=selected.id,
                objective=run.objective,
                metadata={"retry_of_run_id": run.id},
            )
        )
        return {"id": saved.id}

    monkeypatch.setattr(work, "_request", fake_retry)
    retry = {"run_id": run.id, "expected_revision": run.revision}
    first = asyncio.run(work.retry_mission(selected.id, retry))
    second = asyncio.run(work.retry_mission(selected.id, retry))
    assert first["run_id"] == second["run_id"]
    assert len(calls) == 1
    assert calls[0][2] == {"allow_cloud_tool_results": False}
    assert second["result"] == "an existing retry is already saved in Core"


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


def test_immediate_parent_guidance_starts_idle_turn_without_replaying_paused_queue(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
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
            engagement_id=project.id,
            title="Main",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    store.create(
        ChatQueue(
            id="chat-queue-" + session.id,
            engagement_id=project.id,
            session_id=session.id,
            paused=True,
            items=[{"id": "old", "key": "old", "status": "queued"}],
        )
    )
    work = app.state.control_assistant_work
    calls = []

    async def prepare(request):
        calls.append(request.messages[-1].content)
        return SimpleNamespace(turn=SimpleNamespace(id="new-turn"))

    monkeypatch.setattr(work.chat, "prepare_async", prepare)
    monkeypatch.setattr(
        work.chat,
        "start_provider_turn",
        lambda prepared: calls.append(prepared.turn.id),
    )
    result = asyncio.run(
        work.message_chat(
            project.id,
            {
                "session_id": session.id,
                "text": "Check the checkpoint",
                "delivery": "immediate",
            },
        )
    )
    assert result == {
        "session_id": session.id,
        "delivery": "started",
        "turn_id": "new-turn",
    }
    assert calls == ["Check the checkpoint", "new-turn"]
    assert work.queue.get(session.id).paused
    assert [item["id"] for item in work.queue.get(session.id).items] == ["old"]


def test_immediate_harness_guidance_preserves_saved_plan_mcp_and_subagent(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    old_profile = store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="old-model",
            capabilities=HarnessCapabilities(models=["old-model"], mcp=True),
        )
    )
    grok = store.create(
        HarnessProfile(
            name="Grok",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="grok-model",
            capabilities=HarnessCapabilities(models=["grok-model"], mcp=True),
        )
    )
    child = store.create(
        ProviderProfile(
            name="DeepSeek",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["child-model"],
        )
    )
    old_session = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=old_profile.id,
            model="old-model",
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Research",
            backend=ChatBackend.HARNESS,
            harness_profile_id=old_profile.id,
            harness_session_id=old_session.id,
            model="old-model",
        )
    )
    work = app.state.control_assistant_work
    mcp = app.state.agent_security_tools.register_profile()
    before = work.chat_settings(project.id, chat.id)
    saved = work.set_chat_settings(
        project.id,
        {
            "session_id": chat.id,
            "expected_revision": before["revision"],
            "settings": {
                "backend": "harness",
                "profile_id": grok.id,
                "model": "grok-model",
                "planning_enabled": True,
                "mcp_server_ids": [mcp.id],
                "allow_subagents": True,
                "subagent_provider_id": child.id,
                "subagent_model": "child-model",
            },
        },
    )
    assert saved["settings"]["profile_id"] == grok.id
    prepared = []

    def prepare_chat(**kwargs):
        prepared.append(kwargs)
        return None, SimpleNamespace(id="chat-turn"), SimpleNamespace(id="harness-turn")

    monkeypatch.setattr(work.harness, "prepare_chat", prepare_chat)
    monkeypatch.setattr(work.harness, "start_chat_turn", lambda _: None)
    result = asyncio.run(
        work.message_chat(
            project.id,
            {
                "session_id": chat.id,
                "text": "Reload AGENTS.md before resuming",
                "delivery": "immediate",
            },
        )
    )
    assert result["delivery"] == "started"
    assert len(prepared) == 1
    assert prepared[0]["profile_id"] == grok.id
    assert prepared[0]["planning_enabled"] is True
    assert prepared[0]["mcp_server_ids"] == [mcp.id]
    assert prepared[0]["provider_subagent"] == {
        "provider_profile_id": child.id,
        "model": "child-model",
    }


def test_immediate_guidance_steers_active_harness_and_holds_active_provider(
    tmp_path, monkeypatch
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    harness = store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )
    )
    session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main",
            backend=ChatBackend.HARNESS,
            harness_profile_id=harness.id,
            harness_session_id="harness-session",
            model="model-a",
        )
    )
    work = app.state.control_assistant_work
    monkeypatch.setattr(
        work.chat,
        "pending_turn",
        lambda _: SimpleNamespace(id="turn", harness_turn_id="harness-turn"),
    )
    seen = []

    async def steer(turn_id, text, **kwargs):
        seen.append((turn_id, text, kwargs["title"]))

    monkeypatch.setattr(work.harness, "steer_turn", steer)
    steered = asyncio.run(
        work.message_chat(
            project.id,
            {
                "session_id": session.id,
                "text": "Focus on saved evidence",
                "delivery": "immediate",
            },
        )
    )
    assert steered["delivery"] == "steered" and steered["turn_id"] == "turn"
    assert seen == [("harness-turn", "Focus on saved evidence", "Butler guidance")]
    assert work.queue.get(session.id).items == []

    provider = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    provider_session = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Provider",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    held = asyncio.run(
        work.message_chat(
            project.id,
            {
                "session_id": provider_session.id,
                "text": "Check evidence",
                "delivery": "immediate",
            },
        )
    )
    assert held["delivery"] == "held" and held["reason"] == "active_turn_cannot_steer"
    assert work.queue.get(provider_session.id).items == []


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
        assert saved.metadata["butler_initiated"] is True
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
        assert session.metadata["butler_initiated"] is True

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


def test_butler_can_discover_and_attach_browser_mcp_before_retrying_main_agent(
    tmp_path,
):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    profile = store.create(
        HarnessProfile(
            name="Codex",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(models=["model-a"]),
        )
    )
    harness = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="model-a",
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Research",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=harness.id,
            model="model-a",
        )
    )
    security = app.state.agent_security_tools.register_profile()
    work = app.state.control_assistant_work
    before = work.chat_settings(project.id, chat.id)
    offered = next(
        item for item in before["available"]["mcp_servers"] if item["id"] == security.id
    )
    assert "Headless Project security testing" in offered["description"]
    assert {tool["name"] for tool in offered["tool_details"]} >= {
        "navigate",
        "status",
    }
    assert offered["tools"] >= 2
    assert (
        "does not attach an MCP browser"
        in (work.catalog(project.id, "nebula.work.chat_settings")["description"])
    )
    with pytest.raises(ValueError, match="work roster"):
        work.chat_settings(other.id, chat.id)

    saved = work.set_chat_settings(
        project.id,
        {
            "session_id": chat.id,
            "expected_revision": before["revision"],
            "settings": {"mcp_server_ids": [security.id]},
        },
    )
    assert saved["settings"]["mcp_server_ids"] == [security.id]
    assert saved["settings"]["tools_enabled"] is False
    queued = asyncio.run(
        work.message_chat(
            project.id,
            {"session_id": chat.id, "text": "Use the browser tool to find the result"},
        )
    )
    assert queued["delivery"] == "queued"
    assert work.queue.get(chat.id).items[0]["request"]["mcp_server_ids"] == [
        security.id
    ]
    pending = asyncio.run(
        work.await_chat_result(
            project.id,
            {
                "session_id": chat.id,
                "item_id": queued["item_id"],
                "timeout_seconds": 0,
            },
        )
    )
    assert pending["result"] == "still in progress"
    assert "saved_reply" not in pending
    queue = work.queue.get(chat.id)
    store.update(
        ChatQueue,
        queue.id,
        {
            "items": [
                {
                    **queue.items[0],
                    "status": "complete",
                    "turn_id": "retry-turn",
                    "harness_turn_id": "harness-retry-turn",
                }
            ]
        },
        expected_revision=queue.revision,
    )
    store.create(
        ChatMessage(
            engagement_id=project.id,
            session_id=chat.id,
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="The search found a result with a source.",
            metadata={"harness_turn_id": "harness-retry-turn"},
        )
    )
    completed = asyncio.run(
        work.await_chat_result(
            project.id,
            {
                "session_id": chat.id,
                "item_id": queued["item_id"],
                "timeout_seconds": 0,
            },
        )
    )
    assert completed["queue_status"] == "complete"
    assert completed["saved_reply"] == "The search found a result with a source."
    with pytest.raises(ValueError, match="work roster"):
        asyncio.run(
            work.await_chat_result(
                other.id,
                {
                    "session_id": chat.id,
                    "item_id": queued["item_id"],
                    "timeout_seconds": 0,
                },
            )
        )


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


def test_butler_settings_api_saves_advertised_max_effort(tmp_path):
    store, app, _ = _app(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            default_model="test-model",
            executable="/bin/true",
            privacy={"local_only": True},
            capabilities=HarnessCapabilities(
                models=["test-model"],
                model_options=[
                    HarnessModelOptions(
                        model="test-model",
                        reasoning_efforts=[
                            HarnessRuntimeOption(id="low", label="Low"),
                            HarnessRuntimeOption(id="max", label="Max"),
                        ],
                    )
                ],
            ),
        )
    )
    harness = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="test-model",
            metadata={"runtime_options": {"reasoning_effort": "low"}},
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=harness.id,
            model="test-model",
            metadata={"control_assistant": True},
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    url = f"/api/v1/chat-sessions/{chat.id}/assistant-config"
    with TestClient(app) as client:
        before = client.get(url, headers=headers)
        assert before.status_code == 200
        assert before.json()["settings"]["harness_reasoning_effort"] == "low"
        concurrent = client.patch(
            url,
            headers=headers,
            json={
                "expected_revision": before.json()["revision"],
                "settings": {"harness_reasoning_effort": "low"},
            },
        )
        assert concurrent.status_code == 200, concurrent.text
        stale = client.patch(
            url,
            headers=headers,
            json={
                "expected_revision": before.json()["revision"],
                "settings": {"harness_reasoning_effort": "max"},
            },
        )
        assert stale.status_code in {400, 409, 422}
        changed = client.patch(
            url,
            headers=headers,
            json={
                "expected_revision": client.get(url, headers=headers).json()[
                    "revision"
                ],
                "settings": {"harness_reasoning_effort": "max"},
            },
        )
        assert changed.status_code == 200, changed.text
        assert (
            client.get(url, headers=headers).json()["settings"][
                "harness_reasoning_effort"
            ]
            == "max"
        )
    assert (
        store.get(HarnessSession, harness.id).metadata["runtime_options"][
            "reasoning_effort"
        ]
        == "low"
    )
