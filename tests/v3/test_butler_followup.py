from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.control_assistant_work import dispatch_work_tool
from nebula.v3.domain import (
    ChatSession,
    ChatSubagent,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
)
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore


def _work(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    return store, app.state.control_assistant_work


def test_butler_messages_parent_and_rejects_child_queue_paths(tmp_path):
    store, work = _work(tmp_path)
    project = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Other"))
    profile = store.create(
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
            title="Main",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Delegated child",
            provider_profile_id=profile.id,
            model="model-a",
            parent_session_id=parent.id,
            metadata={"subagent_id": "sub-1"},
        )
    )
    goal_child = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Goal child",
            provider_profile_id=profile.id,
            model="model-a",
            parent_session_id=parent.id,
            metadata={"delegated_from_goal_id": "goal-1"},
        )
    )
    store.create(
        ChatSubagent(
            id="sub-1",
            engagement_id=project.id,
            parent_session_id=parent.id,
            parent_turn_id="parent-turn",
            child_session_id=child.id,
            name="Files",
            task="Inspect files",
        )
    )
    nested = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Nested delegated child",
            provider_profile_id=profile.id,
            model="model-a",
            parent_session_id=child.id,
            metadata={"subagent_id": "sub-2"},
        )
    )
    store.create(
        ChatSubagent(
            id="sub-2",
            engagement_id=project.id,
            parent_session_id=child.id,
            parent_turn_id="child-turn",
            child_session_id=nested.id,
            name="Nested",
            task="Check details",
        )
    )
    assert work.subagent_detail(project.id, "sub-1")["parent_session_id"] == parent.id
    assert work.subagent_detail(project.id, "sub-1")["parent_url"].endswith(
        f"session={parent.id}"
    )
    assert (
        work.subagent_detail(project.id, "sub-2")["message_via_session_id"] == parent.id
    )
    assert work.chat_detail(project.id, goal_child.id)["parent_url"].endswith(
        f"session={parent.id}"
    )
    assert (
        work.chat_detail(project.id, goal_child.id)["message_via_session_id"]
        == parent.id
    )

    async def scenario():
        with pytest.raises(ValueError, match="delegated subagent"):
            await work.message_chat(
                project.id,
                {
                    "session_id": child.id,
                    "text": "Work on another task",
                },
            )
        with pytest.raises(ValueError, match="delegated subagent"):
            work.queue_detail(project.id, child.id)
        with pytest.raises(ValueError, match="delegated subagent"):
            work.queue_action(
                project.id,
                {
                    "session_id": child.id,
                    "expected_revision": 0,
                    "action": "resume",
                },
            )
        with pytest.raises(ValueError, match="delegated subagent"):
            await work.stop_chat(project.id, child.id)
        with pytest.raises(ValueError, match="child goal chat"):
            await work.message_chat(
                project.id,
                {
                    "session_id": goal_child.id,
                    "text": "Direct child instruction",
                },
            )
        with pytest.raises(ValueError, match="child goal chat"):
            work.queue_action(
                project.id,
                {
                    "session_id": goal_child.id,
                    "expected_revision": 0,
                    "action": "resume",
                },
            )
        with pytest.raises(ValueError, match="project's work roster"):
            await work.message_chat(
                foreign.id,
                {
                    "session_id": parent.id,
                    "text": "Steer work",
                },
            )
        sent = await work.message_chat(
            project.id,
            {
                "session_id": parent.id,
                "text": "Please redirect Files to the new question",
            },
        )
        assert sent["delivery"] == "queued"
        assert work.queue.get(parent.id).items[0]["request"]["messages"][0][
            "content"
        ] == ("Please redirect Files to the new question")

    asyncio.run(scenario())
    actions = work.catalog(project.id)["actions"]
    assert {
        "nebula.work.fork_chat",
        "nebula.work.delete_chat",
        "nebula.work.retry_chat_turn",
    }.issubset(actions)
    assert "nebula.work.message_subagent" not in actions


def test_butler_native_harness_subagent_roster_and_stop(tmp_path, monkeypatch):
    store, work = _work(tmp_path)
    project = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Other"))
    profile = store.create(
        HarnessProfile(
            name="Claude fixture",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(models=["model-a"]),
            native_capabilities={"subagents": True},
        )
    )
    session = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="model-a",
            metadata={"native_capabilities": {"subagents": True}},
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main harness",
            backend="harness",
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            model="model-a",
        )
    )
    turn = store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=chat.id,
            chat_turn_id="chat-turn",
            prompt="Inspect",
            status=HarnessTurnStatus.RUNNING,
        )
    )
    work.harness._active[session.id] = SimpleNamespace(turn_id=turn.id)
    store.append_operation_event(
        turn.id,
        "harness_turn",
        project.id,
        "harness.item_upsert",
        {
            "type": "item_upsert",
            "item_kind": "subagent",
            "item_id": "task-1",
            "item_status": "running",
            "title": "Code review",
            "summary": "Checking repository",
        },
    )
    roster = work.subagents_overview(project.id, {})
    native = roster["native_subagents"][0]
    assert native["source"] == "native_harness"
    assert native["parent_session_id"] == chat.id
    assert native["can_stop"] is True
    assert native["can_message"] is False
    assert work.overview(project.id)["native_subagents"][0]["task_id"] == "task-1"
    with pytest.raises(ValueError, match="project's work roster"):
        work.native_subagent_detail(
            foreign.id,
            {
                "turn_id": turn.id,
                "task_id": "task-1",
            },
        )

    async def fake_request(method, path, body, **kwargs):
        assert method == "POST"
        assert path.endswith(f"/harness-turns/{turn.id}/tasks/task-1/stop")
        store.append_operation_event(
            turn.id,
            "harness_turn",
            project.id,
            "harness.item_upsert",
            {
                "type": "item_upsert",
                "item_kind": "subagent",
                "item_id": "task-1",
                "item_status": "cancelled",
                "title": "Code review",
            },
        )
        return {}

    monkeypatch.setattr(work, "_request", fake_request)
    stopped = asyncio.run(
        dispatch_work_tool(
            work,
            project.id,
            "nebula.work.stop_native_subagent",
            {"turn_id": turn.id, "task_id": "task-1"},
        )
    )
    assert stopped["status"] == "cancelled"
    assert stopped["can_stop"] is False


def test_butler_labels_native_harness_goals_as_read_only(tmp_path):
    store, work = _work(tmp_path)
    project = store.create(Engagement(name="Selected"))
    profile = store.create(
        HarnessProfile(
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(models=["model-a"]),
        )
    )
    session = store.create(
        HarnessSession(
            engagement_id=project.id,
            harness_profile_id=profile.id,
            model="model-a",
        )
    )
    chat = store.create(
        ChatSession(
            engagement_id=project.id,
            title="Main harness",
            backend="harness",
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            model="model-a",
        )
    )
    turn = store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=chat.id,
            chat_turn_id="chat-turn",
            prompt="Inspect",
            status=HarnessTurnStatus.COMPLETE,
        )
    )
    store.update(
        HarnessSession,
        session.id,
        {"last_turn_id": turn.id},
        expected_revision=session.revision,
    )
    store.append_operation_event(
        turn.id,
        "harness_turn",
        project.id,
        "harness.item_upsert",
        {
            "type": "item_upsert",
            "item_kind": "goal",
            "item_id": "native-goal",
            "item_status": "running",
            "goal": {
                "objective": "Audit code",
                "status": "running",
                "progress": 0.4,
            },
        },
    )
    detail = work.goal_detail(project.id, chat.id)
    assert detail["goal"] is None
    assert detail["native_harness_goal"]["objective"] == "Audit code"
    assert detail["native_harness_goal"]["control_supported"] is False
    assert (
        work.goals_overview(project.id)["native_harness_goals"][0]["session_id"]
        == chat.id
    )
    assert (
        work.overview(project.id)["chats"][0]["native_harness_goal"]["source"]
        == "native_harness"
    )
