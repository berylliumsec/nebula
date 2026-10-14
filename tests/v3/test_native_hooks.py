import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from nebula.v3.native_hooks import (
    MAX_INHERITED_ANCESTORS,
    NativeHookError,
    NativeHookRunner,
    discover_native_hooks,
    snapshot_native_hook,
)
from nebula.v3.storage import NebulaStore


def _hook(
    workspace: Path,
    *,
    name: str = "audit",
    script: str = "#!/bin/sh\npython3 -c 'import json,sys; print(json.load(sys.stdin)[\"event\"])'\n",
    timeout_seconds: int = 5,
    inherit_to_descendants: bool = False,
) -> Path:
    directory = workspace / ".agents" / "hooks" / name
    directory.mkdir(parents=True)
    executable = directory / "run.sh"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    (directory / "hook.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Audit lifecycle",
                "description": "Records native chat lifecycle events.",
                "events": ["chat.turn.started", "chat.turn.completed"],
                "command": ["run.sh"],
                "timeout_seconds": timeout_seconds,
                "side_effects": "workspace",
                "failure_policy": "continue",
                "inherit_to_descendants": inherit_to_descendants,
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_native_hook_catalog_uses_agents_root_and_freezes_source_digests(tmp_path):
    workspace = tmp_path / "workspace"
    directory = _hook(workspace)
    _hook(workspace / ".codex", name="ignored")
    _hook(workspace / ".grok", name="ignored")
    catalog = discover_native_hooks(workspace)

    assert [item.id for item in catalog] == ["audit"]
    assert catalog[0].path == str(directory.resolve())
    snapshot = snapshot_native_hook("audit", catalog)
    assert snapshot.manifest.events == ["chat.turn.started", "chat.turn.completed"]
    assert snapshot.manifest_sha256 == catalog[0].manifest_sha256
    assert snapshot.executable_sha256 == catalog[0].executable_sha256

    (directory / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(NativeHookError, match="executable changed"):
        asyncio.run(
            NativeHookRunner(NebulaStore(tmp_path / "tamper.db")).run(
                snapshot,
                engagement_id="eng",
                chat_session_id="session",
                chat_turn_id="turn",
                event_name="chat.turn.started",
                payload={},
            )
        )


def test_native_hook_exact_git_root_does_not_require_inheritance_opt_in(tmp_path):
    workspace = tmp_path / "rnd-root"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    _hook(workspace, name="rnd-exact", inherit_to_descendants=False)

    [descriptor] = discover_native_hooks(workspace)

    assert descriptor.id == "rnd-exact"
    assert descriptor.manifest.inherit_to_descendants is False


def test_native_hooks_inherit_opted_in_ancestor_across_nested_git_marker(tmp_path):
    repository = tmp_path / "shared-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    workspace = repository / "projects" / "project-a"
    workspace.mkdir(parents=True)
    # A valid nested repository must not suppress policy inherited from the
    # explicitly opted-in shared ancestor.
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    _hook(
        repository,
        name="shared-repo-search-routing",
        inherit_to_descendants=True,
    )
    _hook(repository, name="root-local-only")
    _hook(workspace, name="workspace-local")

    catalog = discover_native_hooks(workspace)

    assert [item.id for item in catalog] == [
        "shared-repo-search-routing",
        "workspace-local",
    ]
    assert catalog[0].path == str(
        (
            repository
            / ".agents"
            / "hooks"
            / "shared-repo-search-routing"
        ).resolve()
    )


def test_native_hooks_ignore_non_opted_in_ancestors_and_unrelated_workspaces(
    tmp_path,
):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".git").mkdir()
    _hook(outer, name="outer-local-only")

    nested = outer / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    nested_workspace = nested / "project"
    nested_workspace.mkdir()
    assert discover_native_hooks(nested_workspace) == []

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / ".git").mkdir()
    assert discover_native_hooks(unrelated) == []


def test_native_hook_discovery_stops_at_the_ancestor_bound(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _hook(repository, name="shared", inherit_to_descendants=True)
    workspace = repository
    for index in range(MAX_INHERITED_ANCESTORS + 1):
        workspace = workspace / f"nested-{index}"
    workspace.mkdir(parents=True)

    assert discover_native_hooks(workspace) == []


def test_native_hook_duplicate_ids_fail_closed_between_ancestor_and_workspace(
    tmp_path,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    workspace = repository / "project"
    workspace.mkdir()
    _hook(repository, name="duplicate", inherit_to_descendants=True)
    _hook(workspace, name="duplicate")

    with pytest.raises(NativeHookError, match="duplicate native hook id"):
        discover_native_hooks(workspace)


def test_native_hook_discovery_rejects_symlinked_shared_agents_root(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    workspace = repository / "project"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _hook(outside, name="shared", inherit_to_descendants=True)
    (repository / ".agents").symlink_to(outside / ".agents", target_is_directory=True)

    with pytest.raises(NativeHookError, match=r"\.agents must be a regular directory"):
        discover_native_hooks(workspace)


def test_native_hook_discovery_rejects_symlinked_inherited_hook_directory(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    workspace = repository / "projects" / "project"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    _hook(outside, name="shared", inherit_to_descendants=True)
    hook_root = repository / ".agents" / "hooks"
    hook_root.mkdir(parents=True)
    (hook_root / "shared").symlink_to(
        outside / ".agents" / "hooks" / "shared", target_is_directory=True
    )

    with pytest.raises(NativeHookError, match="hook directory cannot be a symlink"):
        discover_native_hooks(workspace)


def test_native_hook_discovery_rejects_workspace_symlink_escape(tmp_path):
    repository = tmp_path / "repository"
    linked_projects = repository / "projects"
    linked_projects.mkdir(parents=True)
    _hook(repository, name="shared", inherit_to_descendants=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_link = linked_projects / "project-link"
    workspace_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        NativeHookError, match="workspace path escapes an opted-in ancestor hook root"
    ):
        discover_native_hooks(workspace_link)


def test_native_hook_discovery_preserves_symlinked_workspace_inside_shared_root(
    tmp_path,
):
    repository = tmp_path / "repository"
    workspace = repository / "projects" / "project"
    workspace.mkdir(parents=True)
    _hook(repository, name="shared", inherit_to_descendants=True)
    alias = repository / "projects" / "linked-project"
    alias.symlink_to(workspace, target_is_directory=True)

    [descriptor] = discover_native_hooks(alias)

    assert descriptor.id == "shared"
    assert descriptor.path == str(
        (repository / ".agents" / "hooks" / "shared").resolve()
    )


def test_native_hook_runner_persists_versioned_bounded_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(workspace)
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "hooks.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="session",
            chat_turn_id="turn",
            event_name="chat.turn.started",
            payload={"model": "author/model"},
        )
    )

    assert execution.status == "complete"
    assert execution.event_version == 1
    assert execution.stdout.strip() == "chat.turn.started"
    assert execution.side_effects == "workspace"
    assert store.get(type(execution), execution.id) == execution


def test_native_hook_envelope_exposes_generic_actor_and_workspace_provenance(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(
        workspace,
        script=(
            "#!/bin/sh\n"
            "python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))'\n"
        ),
    )
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "envelope.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="child-session",
            chat_turn_id="child-turn",
            owner_kind="chat",
            owner_id="child-session",
            event_name="chat.turn.completed",
            payload={"model": "model"},
            workspace_provenance={
                "schema": "nebula.workspace-provenance/v1",
                "actor_id": "subagent:child-agent",
                "supported": True,
                "confidence": "exact",
                "attribution": {"owned": [{"path": "owned.txt"}]},
            },
        )
    )
    envelope = json.loads(execution.stdout)

    assert envelope["actor"] == {
        "id": "subagent:child-agent",
        "owner_kind": "chat",
        "owner_id": "child-session",
        "chat_session_id": "child-session",
        "chat_turn_id": "child-turn",
    }
    assert envelope["workspace_provenance"]["schema"] == (
        "nebula.workspace-provenance/v1"
    )
    assert envelope["workspace_provenance"]["attribution"]["owned"] == [
        {"path": "owned.txt"}
    ]


def test_native_hook_runner_times_out_without_treating_output_as_approval(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(
        workspace,
        script="#!/bin/sh\nprintf '{\"approval\":true}'\nsleep 2\n",
        timeout_seconds=1,
    )
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "timeout.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="session",
            chat_turn_id="turn",
            event_name="chat.turn.started",
            payload={"approval_id": "approval"},
        )
    )

    assert execution.status == "timed_out"
    assert execution.error == "hook timed out"
    assert execution.stdout == ""
    assert execution.reconciliation is None


def test_native_hook_catalog_accepts_project_tool_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    directory = _hook(workspace)
    manifest = json.loads((directory / "hook.json").read_text(encoding="utf-8"))
    manifest["events"] = ["tool.before", "tool.after"]
    (directory / "hook.json").write_text(json.dumps(manifest), encoding="utf-8")

    [descriptor] = discover_native_hooks(workspace)

    assert descriptor.manifest.events == ["tool.before", "tool.after"]
