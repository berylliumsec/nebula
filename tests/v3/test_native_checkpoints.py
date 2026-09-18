import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import ChatGoalStatus, ChatSession, Engagement, ProviderProfile
from nebula.v3.native_checkpoints import NativeCheckpointService
from nebula.v3.storage import ConflictError, NebulaStore


def test_checkpoint_restore_rejects_later_edits(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "notes.md"
    target.write_text("original\n", encoding="utf-8")
    store = NebulaStore(tmp_path / "checkpoints.db")
    store.create(
        Engagement(id="project", name="Project", workspace_path=str(workspace))
    )
    store.create(
        ProviderProfile(
            id="provider", name="Provider", provider_type="vllm", is_local=True
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Checkpoint chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    service = NativeCheckpointService(store, lambda _: workspace)
    checkpoint = service.capture("session", label="Before edits", paths=["notes.md"])
    preview = service.preview(checkpoint.id)
    assert [item.status for item in preview] == ["unchanged"]
    target.write_text("operator edit\n", encoding="utf-8")
    assert [item.status for item in service.preview(checkpoint.id)] == ["conflict"]
    with pytest.raises(ConflictError, match="later edits conflict"):
        service.restore(checkpoint.id)
    assert target.read_text(encoding="utf-8") == "operator edit\n"


def test_fork_copies_goal_as_independent_draft_without_running_state(tmp_path):
    store = NebulaStore(tmp_path / "fork-goal.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider", name="Provider", provider_type="vllm", is_local=True
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Source",
            provider_profile_id="provider",
            model="model",
        )
    )
    from nebula.v3.domain import ChatMessage

    store.create(
        ChatMessage(
            id="msg-1",
            engagement_id="project",
            session_id="session",
            sequence=1,
            role="user",
            content="Start the work",
        )
    )
    goals = ChatGoalService(store)
    created = goals.create(
        "session",
        GoalCreate(
            objective="Parent objective",
            completion_criteria=["Done"],
            child_budget=1,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=created.revision, action="start")
    )
    fork = ChatService(store).fork_session("session", through_message_id="msg-1")
    child = goals.get(fork.id)
    assert child.status == ChatGoalStatus.DRAFT
    assert child.id != running.id
    assert child.linked_turn_ids == []
    assert child.usage.total_tokens == 0
    assert goals.get("session").status == ChatGoalStatus.RUNNING


def test_checkpoint_http_capture_uses_json_body(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("original\n", encoding="utf-8")
    store = NebulaStore(tmp_path / "checkpoint-http.db")
    store.create(
        Engagement(id="project", name="Project", workspace_path=str(workspace))
    )
    store.create(
        ProviderProfile(
            id="provider", name="Provider", provider_type="vllm", is_local=True
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Checkpoint chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    captured = client.post(
        "/api/v1/chat/sessions/session/checkpoints",
        headers={"Authorization": "Bearer test-token"},
        json={"label": "notes", "paths": ["notes.md"]},
    )
    assert captured.status_code == 200, captured.text
    assert captured.json()["label"] == "notes"
