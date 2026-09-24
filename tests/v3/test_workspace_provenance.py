import subprocess
from pathlib import Path

from nebula.v3.domain import ChatSession, Engagement
from nebula.v3.storage import NebulaStore
from nebula.v3.workspace_provenance import (
    PROVENANCE_SCHEMA,
    WorkspaceProvenanceService,
    actor_id_for,
    snapshot,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Nebula Test")
    _git(root, "config", "user.email", "nebula@example.invalid")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "baseline")
    return root


def _service(tmp_path: Path) -> tuple[WorkspaceProvenanceService, NebulaStore]:
    store = NebulaStore(tmp_path / "core.db")
    store.create(Engagement(id="eng", name="Project"))
    return WorkspaceProvenanceService(store), store


def _begin(
    service: WorkspaceProvenanceService,
    root: Path,
    *,
    scope: str,
    actor: str,
):
    return service.begin(
        root,
        engagement_id="eng",
        scope_kind="turn",
        scope_id=scope,
        actor_id=actor,
        owner_kind="chat",
        owner_id=actor,
        chat_session_id=None,
        chat_turn_id=scope,
    )


def test_provenance_attributes_only_state_changed_after_the_actor_baseline(tmp_path):
    root = _repository(tmp_path)
    (root / "preexisting.txt").write_text("someone else\n", encoding="utf-8")
    service, _ = _service(tmp_path)

    started = _begin(service, root, scope="turn-a", actor="chat:a")
    (root / "owned.txt").write_text("mine\n", encoding="utf-8")
    completed = service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
    )

    assert started.status == "active"
    assert completed.status == "complete"
    assert completed.confidence == "exact"
    assert [item["path"] for item in completed.attribution["owned"]] == ["owned.txt"]
    assert [item["path"] for item in completed.attribution["preexisting"]] == [
        "preexisting.txt"
    ]
    assert service.receipt(completed)["schema"] == PROVENANCE_SCHEMA


def test_provenance_keeps_prior_actor_ownership_across_later_turns(tmp_path):
    root = _repository(tmp_path)
    service, _ = _service(tmp_path)
    _begin(service, root, scope="turn-a", actor="chat:a")
    (root / "shared.txt").write_text("from a\n", encoding="utf-8")
    service.finish(root, engagement_id="eng", scope_kind="turn", scope_id="turn-a")

    _begin(service, root, scope="turn-b", actor="chat:b")
    completed = service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-b"
    )

    assert completed.attribution["owned"] == []
    assert [
        (item["path"], item["actor_id"]) for item in completed.attribution["other"]
    ] == [("shared.txt", "chat:a")]


def test_active_observation_survives_store_reopen(tmp_path):
    root = _repository(tmp_path)
    service, _ = _service(tmp_path)
    _begin(service, root, scope="turn-restart", actor="chat:a")
    (root / "after-restart.txt").write_text("durable\n", encoding="utf-8")

    restarted = WorkspaceProvenanceService(NebulaStore(tmp_path / "core.db"))
    completed = restarted.finish(
        root,
        engagement_id="eng",
        scope_kind="turn",
        scope_id="turn-restart",
    )

    assert completed.status == "complete"
    assert [item["path"] for item in completed.attribution["owned"]] == [
        "after-restart.txt"
    ]


def test_parallel_actors_are_uncertain_instead_of_misattributed(tmp_path):
    root = _repository(tmp_path)
    service, _ = _service(tmp_path)
    _begin(service, root, scope="turn-a", actor="chat:a")
    _begin(service, root, scope="turn-b", actor="chat:b")
    (root / "parallel.txt").write_text("ambiguous\n", encoding="utf-8")

    completed = service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-a"
    )

    assert completed.confidence == "uncertain_parallel_turns"
    assert completed.concurrent_scope_ids == ["turn-b"]
    assert [item["path"] for item in completed.attribution["uncertain"]] == [
        "parallel.txt"
    ]
    assert completed.attribution["owned"] == []


def test_non_git_workspace_is_explicitly_unsupported_and_subagent_id_is_stable(
    tmp_path,
):
    service, store = _service(tmp_path)
    store.create(
        ChatSession(
            id="child-chat",
            engagement_id="eng",
            title="Child",
            provider_profile_id="provider",
            model="model",
            metadata={"subagent_id": "child-agent"},
        )
    )
    root = tmp_path / "folder"
    root.mkdir()

    assert snapshot(root).reason == "workspace_is_not_a_git_checkout"
    assert (
        actor_id_for(
            store,
            owner_kind="chat",
            owner_id="child-chat",
            chat_session_id="child-chat",
        )
        == "subagent:child-agent"
    )
    started = _begin(service, root, scope="turn-child", actor="subagent:child-agent")
    completed = service.finish(
        root, engagement_id="eng", scope_kind="turn", scope_id="turn-child"
    )
    assert not started.supported
    assert completed.confidence == "unsupported"
    assert completed.unsupported_reason == "workspace_is_not_a_git_checkout"
