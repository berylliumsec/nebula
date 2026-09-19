import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nebula.v3.domain import Engagement, GuideProgress
from nebula.v3.guides import guides_router, progress_id
from nebula.v3.operators import OperatorProfileService
from nebula.v3.storage import ConflictError, NebulaStore


def setup(tmp_path):
    store = NebulaStore(tmp_path / "guides.db")
    store.create(Engagement(id="p", name="Project"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    operators = OperatorProfileService(store)
    app = FastAPI()
    app.include_router(guides_router(store, operators, lambda _: workspace))
    return store, operators, workspace, TestClient(app)


def test_progress_is_created_advanced_completed_and_reset(tmp_path):
    store, _, _, client = setup(tmp_path)
    assert client.get("/guides/progress").json() == []

    started = client.put(
        "/guides/progress/lifecycle-hooks",
        json={"status": "in_progress", "step_index": 0, "expected_revision": 0},
    ).json()
    assert started["operator_key"] == "local" and started["revision"] == 1

    done = client.put(
        "/guides/progress/lifecycle-hooks",
        json={"status": "completed", "step_index": 4, "expected_revision": 1},
    ).json()
    assert done["status"] == "completed" and done["completed_at"]

    replay = client.put(
        "/guides/progress/lifecycle-hooks",
        json={"status": "completed", "step_index": 4, "expected_revision": 2},
    ).json()
    assert replay["completed_at"] == done["completed_at"]

    listed = client.get("/guides/progress").json()
    assert [item["guide_id"] for item in listed] == ["lifecycle-hooks"]

    assert client.delete("/guides/progress/lifecycle-hooks").status_code == 204
    assert client.delete("/guides/progress/lifecycle-hooks").status_code == 204
    assert client.get("/guides/progress").json() == []


def test_stale_revision_is_rejected(tmp_path):
    _, _, _, client = setup(tmp_path)
    body = {"status": "in_progress", "step_index": 1, "expected_revision": 0}
    client.put("/guides/progress/agents-md", json=body)
    with pytest.raises(ConflictError):
        client.put("/guides/progress/agents-md", json=body)


def test_progress_follows_the_active_operator(tmp_path):
    store, operators, _, client = setup(tmp_path)
    first = operators.create_profile(display_name="First")
    client.put(
        "/guides/progress/shortcuts",
        json={"status": "completed", "step_index": 3, "expected_revision": 0},
    )
    assert store.get(GuideProgress, progress_id(first.id, "shortcuts"))

    second = operators.create_profile(display_name="Second")
    operators.activate_profile(second.id, expected_revision=second.revision)
    assert client.get("/guides/progress").json() == []


def test_guide_identity_is_validated(tmp_path):
    _, _, _, client = setup(tmp_path)
    body = {"status": "in_progress", "step_index": 0, "expected_revision": 0}
    assert client.put("/guides/progress/Bad_ID", json=body).status_code == 422
    assert client.delete("/guides/progress/Bad_ID").status_code == 422
    too_far = {"status": "in_progress", "step_index": 65, "expected_revision": 0}
    assert client.put("/guides/progress/ok", json=too_far).status_code == 422


def test_project_instructions_status(tmp_path):
    _, _, workspace, client = setup(tmp_path)
    path = "/project-instructions?engagement_id=p"
    assert client.get(path).json()["present"] is False

    (workspace / "AGENTS.md").write_text("Stay in scope.\n", encoding="utf-8")
    status = client.get(path).json()
    assert status["present"] and status["size_bytes"] == 15 and not status["error"]

    (workspace / "AGENTS.md").unlink()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    os.symlink(outside, workspace / "AGENTS.md")
    status = client.get(path).json()
    assert status["present"] and "parent folders" in status["error"]


def test_core_app_serves_guide_progress_with_auth_and_conflicts(tmp_path):
    from nebula.v3.api import create_app

    store = NebulaStore(tmp_path / "app.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    body = {"status": "in_progress", "step_index": 2, "expected_revision": 0}
    path = "/api/v1/guides/progress"
    assert client.get(path).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert (
        client.put(f"{path}/project-skills", json=body, headers=headers).status_code
        == 200
    )
    assert (
        client.put(f"{path}/project-skills", json=body, headers=headers).status_code
        == 409
    )
    assert client.get("/api/v1/guide_progress", headers=headers).status_code == 404
    assert [
        item["step_index"] for item in client.get(path, headers=headers).json()
    ] == [2]


def test_starter_files_are_discovered_and_never_overwritten(tmp_path):
    from nebula.v3.native_hooks import discover_native_hooks
    from nebula.v3.skill_catalog import discover_skills, native_skill_roots

    _, _, workspace, client = setup(tmp_path)
    old_umask = os.umask(0o077)
    try:
        hook = client.post(
            "/guides/starter-files",
            json={"engagement_id": "p", "kind": "hook", "name": "audit"},
        )
    finally:
        os.umask(old_umask)
    assert hook.status_code == 201
    assert hook.json()["paths"] == [
        ".agents/hooks/audit/hook.json",
        ".agents/hooks/audit/run.sh",
    ]
    assert os.access(workspace / ".agents/hooks/audit/run.sh", os.X_OK)
    assert [item.id for item in discover_native_hooks(workspace)] == ["audit"]

    skill = client.post(
        "/guides/starter-files",
        json={"engagement_id": "p", "kind": "skill", "name": "recon-checklist"},
    )
    assert skill.json()["paths"] == [".agents/skills/recon-checklist/SKILL.md"]
    names = [
        item.name
        for item in discover_skills(native_skill_roots(workspace, tmp_path / "managed"))
    ]
    assert names == ["recon-checklist"]

    agents = client.post(
        "/guides/starter-files", json={"engagement_id": "p", "kind": "agents_md"}
    )
    assert agents.json()["paths"] == ["AGENTS.md"]
    assert client.get("/project-instructions?engagement_id=p").json()["present"]

    (workspace / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    with pytest.raises(ConflictError):
        client.post(
            "/guides/starter-files", json={"engagement_id": "p", "kind": "agents_md"}
        )
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"


def test_starter_files_reject_bad_names_and_symlinked_folders(tmp_path):
    _, _, workspace, client = setup(tmp_path)
    with pytest.raises(ValueError):
        client.post(
            "/guides/starter-files",
            json={"engagement_id": "p", "kind": "hook", "name": "../x"},
        )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, workspace / ".agents")
    with pytest.raises(ConflictError):
        client.post(
            "/guides/starter-files",
            json={"engagement_id": "p", "kind": "skill", "name": "x"},
        )
    assert list(elsewhere.iterdir()) == []
