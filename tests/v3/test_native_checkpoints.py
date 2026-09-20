import os
import shutil

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import (
    ChatGoalStatus,
    ChatSession,
    Engagement,
    NativeCheckpoint,
    ProviderProfile,
)
from nebula.v3.native_checkpoints import (
    MAX_CHECKPOINT_FILE_BYTES,
    NativeCheckpointError,
    NativeCheckpointService,
)
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


def test_checkpoint_capture_rejection_leaves_no_partial_files(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("original\n", encoding="utf-8")
    (workspace / "dump.bin").write_bytes(b"\0" * (MAX_CHECKPOINT_FILE_BYTES + 1))
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

    with pytest.raises(NativeCheckpointError, match="exceeds"):
        service.capture("session", label="Partial", paths=["notes.md", "dump.bin"])

    checkpoints = workspace / ".agents" / "checkpoints"
    assert not checkpoints.exists() or list(checkpoints.iterdir()) == []
    assert store.list_entities(NativeCheckpoint) == []

    checkpoint = service.capture("session", label="Complete", paths=["notes.md"])
    assert [path.name for path in checkpoints.iterdir()] == [checkpoint.id]
    assert (checkpoints / checkpoint.id / "notes.md").read_bytes() == b"original\n"


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


def _checkpoint_fixture(tmp_path, workspace):
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
    return store


def test_checkpoint_restore_never_writes_through_a_dangling_symlink(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = workspace / "notes.md"
    target.write_text("original\n", encoding="utf-8")
    store = _checkpoint_fixture(tmp_path, workspace)
    service = NativeCheckpointService(store, lambda _: workspace)
    checkpoint = service.capture("session", label="Before", paths=["notes.md"])

    target.unlink()
    os.symlink(outside / "victim.txt", target)

    assert [item.status for item in service.preview(checkpoint.id)] == ["conflict"]
    with pytest.raises(ConflictError, match="later edits conflict"):
        service.restore(checkpoint.id)
    assert not (outside / "victim.txt").exists()
    assert target.is_symlink()


def test_checkpoint_restore_never_writes_through_a_symlinked_parent(tmp_path):
    workspace = tmp_path / "project"
    (workspace / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "docs" / "notes.md").write_text("original\n", encoding="utf-8")
    store = _checkpoint_fixture(tmp_path, workspace)
    service = NativeCheckpointService(store, lambda _: workspace)
    checkpoint = service.capture("session", label="Before", paths=["docs/notes.md"])

    shutil.rmtree(workspace / "docs")
    os.symlink(outside, workspace / "docs")

    assert [item.status for item in service.preview(checkpoint.id)] == ["conflict"]
    with pytest.raises(ConflictError, match="later edits conflict"):
        service.restore(checkpoint.id)
    assert list(outside.iterdir()) == []


def test_checkpoint_restore_recreates_missing_files_and_directories(tmp_path):
    workspace = tmp_path / "project"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "notes.md").write_text("original\n", encoding="utf-8")
    store = _checkpoint_fixture(tmp_path, workspace)
    service = NativeCheckpointService(store, lambda _: workspace)
    checkpoint = service.capture("session", label="Before", paths=["docs/notes.md"])

    shutil.rmtree(workspace / "docs")
    assert [item.status for item in service.preview(checkpoint.id)] == ["missing"]

    restored = service.restore(checkpoint.id)
    assert [item.status for item in restored] == ["unchanged"]
    assert (workspace / "docs" / "notes.md").read_bytes() == b"original\n"
    assert not (workspace / "docs" / "notes.md").is_symlink()
    assert not list((workspace / "docs").glob(".*"))


def test_checkpoint_http_preview_maps_checkpoint_errors_to_409(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("original\n", encoding="utf-8")
    store = _checkpoint_fixture(tmp_path, workspace)
    client = TestClient(
        create_app(store, auth_token="test-token"), raise_server_exceptions=False
    )
    headers = {"Authorization": "Bearer test-token"}
    captured = client.post(
        "/api/v1/chat/sessions/session/checkpoints",
        headers=headers,
        json={"label": "notes", "paths": ["notes.md"]},
    )
    assert captured.status_code == 200, captured.text

    def failing_preview(self, checkpoint_id):
        raise NativeCheckpointError(
            f"checkpoint path escapes the workspace: {checkpoint_id}"
        )

    monkeypatch.setattr(NativeCheckpointService, "preview", failing_preview)
    preview = client.get(
        f"/api/v1/chat/checkpoints/{captured.json()['id']}/preview", headers=headers
    )
    assert preview.status_code == 409, preview.text
    assert "escapes the workspace" in preview.json()["detail"]
