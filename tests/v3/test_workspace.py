from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Artifact,
    Engagement,
    ExecutionOrigin,
    ExecutionRuntimeSnapshot,
    OperatorExecution,
    OperatorExecutionStatus,
    RunnerIsolation,
    RunnerRuntime,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.workspace import WorkspaceResetRequest, WorkspaceService

AUTH = {"Authorization": "Bearer test-token"}


class StubWorkspacePlatform:
    def __init__(self, root):
        self.root = root

    def workspace_for(self, engagement_id: str):
        path = self.root / engagement_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _services(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    platform = StubWorkspacePlatform(tmp_path / "workspaces")
    workspace = WorkspaceService(
        store=store,
        artifact_store=artifacts,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    engagement = store.create(Engagement(name="Workspace Lab"))
    client = TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            workspace_service=workspace,
        )
    )
    return store, artifacts, platform, workspace, engagement, client


def test_workspace_lists_previews_downloads_and_rejects_symlinks(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / "notes").mkdir()
    exact = "first line\nUnicode: λ\n"
    (root / "notes" / "result.txt").write_text(exact, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must remain private", encoding="utf-8")
    os.symlink(outside, root / "escape")

    with client:
        listing = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace", headers=AUTH
        )
        assert listing.status_code == 200
        assert [(row["name"], row["kind"]) for row in listing.json()["entries"]] == [
            ("notes", "directory"),
            ("escape", "symlink"),
        ]

        preview = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/preview",
            headers=AUTH,
            params={"path": "notes/result.txt"},
        )
        assert preview.status_code == 200
        assert preview.json()["text"] == exact

        download = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/download",
            headers=AUTH,
            params={"path": "notes/result.txt"},
        )
        assert download.status_code == 200
        assert download.content == exact.encode()
        assert download.headers["x-content-type-options"] == "nosniff"

        escaped = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/preview",
            headers=AUTH,
            params={"path": "escape"},
        )
        assert escaped.status_code == 404
        assert escaped.json()["code"] == "workspace_path_invalid"


def test_workspace_discovers_manifest_and_test_tasks_without_following_links(tmp_path):
    _store, _artifacts, platform, workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run","build":"vite build","odd name":"echo ok"}}',
        encoding="utf-8",
    )
    (root / "Makefile").write_text("lint:\n\tcheck\nrelease: build\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_probe.py").write_text(
        "def test_probe(): pass\n", encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_secret.py").write_text("secret\n", encoding="utf-8")
    os.symlink(outside, root / "linked-tests")

    result = workspace.tasks(engagement.id)
    commands = {task.command for task in result.tasks}
    assert {
        "npm run test",
        "npm run build",
        "npm run 'odd name'",
        "make lint",
        "make release",
        "python -m pytest",
        "python -m pytest tests/test_probe.py",
    } <= commands
    assert all("secret" not in (task.path or "") for task in result.tasks)

    response = client.get(
        f"/api/v1/engagements/{engagement.id}/workspace/tasks", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["engagement_id"] == engagement.id


def test_workspace_normalizes_vscode_tasks_into_reviewed_commands(tmp_path):
    _store, _artifacts, platform, workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / ".vscode").mkdir()
    (root / ".vscode" / "tasks.json").write_text(
        """
        {
          // JSON-with-comments and trailing commas are standard VS Code configuration.
          tasks: [
            {
              label: 'Scan fixture',
              type: 'shell',
              command: 'python',
              args: ['tools/scan.py', '--target', '${workspaceFolder}/fixture.bin'],
              options: { cwd: '${workspaceFolder}/research', env: { MODE: 'safe' } },
              group: 'test',
            },
            { label: 'Extension task', type: 'npm', command: 'test' },
            { label: 'Background watcher', type: 'shell', command: 'watch', isBackground: true },
          ],
        }
        """,
        encoding="utf-8",
    )

    result = workspace.tasks(engagement.id)
    configured = {task.label: task for task in result.tasks}
    assert configured["Scan fixture"].command == (
        "cd -- /workspace/research && env MODE=safe "
        "python tools/scan.py --target /workspace/fixture.bin"
    )
    assert configured["Scan fixture"].kind == "test"
    assert configured["Scan fixture"].source == ".vscode/tasks.json"
    assert configured["Scan fixture"].supported is True
    assert configured["Extension task"].supported is False
    assert "requires a VS Code extension" in (
        configured["Extension task"].unsupported_reason or ""
    )
    assert configured["Background watcher"].supported is False
    assert "terminal result" in (
        configured["Background watcher"].unsupported_reason or ""
    )

    response = client.get(
        f"/api/v1/engagements/{engagement.id}/workspace/tasks", headers=AUTH
    )
    assert response.status_code == 200
    rows = {task["label"]: task for task in response.json()["tasks"]}
    assert rows["Scan fixture"]["supported"] is True
    assert rows["Extension task"]["supported"] is False

    (root / ".vscode" / "tasks.json").write_bytes(b" " * (129 * 1024))
    oversized = workspace.tasks(engagement.id)
    notice = next(
        task for task in oversized.tasks if task.label == "VS Code tasks need attention"
    )
    assert notice.supported is False
    assert "128 KiB configuration limit" in (notice.unsupported_reason or "")


def test_workspace_imports_only_launch_profiles_inside_debug_boundary(tmp_path):
    _store, _artifacts, platform, workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / ".vscode").mkdir()
    (root / ".vscode" / "launch.json").write_text(
        """
        {
          configurations: [
            {
              name: 'Active parser', type: 'debugpy', request: 'launch',
              program: '${file}', args: ['--fixture', '${workspaceFolder}/sample.bin'],
            },
            {
              name: 'Other tool', type: 'python', request: 'launch',
              program: '${workspaceFolder}/tools/other.py',
            },
            {
              name: 'Environment override', type: 'debugpy', request: 'launch',
              program: '${file}', env: { TOKEN: 'secret' },
            },
            { name: 'Attach process', type: 'debugpy', request: 'attach' },
          ],
        }
        """,
        encoding="utf-8",
    )

    result = workspace.debug_configurations(engagement.id, "research/parser.py")
    profiles = {profile.name: profile for profile in result.configurations}
    assert profiles["Active parser"].supported is True
    assert profiles["Active parser"].path == "research/parser.py"
    assert profiles["Active parser"].arguments == [
        "--fixture",
        "/workspace/sample.bin",
    ]
    assert profiles["Other tool"].supported is False
    assert profiles["Other tool"].path == "tools/other.py"
    assert profiles["Other tool"].unsupported_reason == (
        "Open tools/other.py to use this profile."
    )
    assert profiles["Environment override"].supported is False
    assert "reviewed launch boundary" in (
        profiles["Environment override"].unsupported_reason or ""
    )
    assert profiles["Attach process"].supported is False

    response = client.get(
        f"/api/v1/engagements/{engagement.id}/workspace/debug-configurations",
        headers=AUTH,
        params={"path": "research/parser.py"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["active_path"] == "research/parser.py"
    assert len(payload["configurations"]) == 4


def test_workspace_search_is_recursive_bounded_and_never_follows_symlinks(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / "src").mkdir()
    (root / "src" / "scanner.py").write_text(
        "def scan_target(host):\n    return host\n", encoding="utf-8"
    )
    (root / "binary.bin").write_bytes(b"scan_target\x00private")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("scan_target must not escape", encoding="utf-8")
    os.symlink(outside, root / "linked-secret")

    with client:
        files = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/search",
            headers=AUTH,
            params={"query": "scanner", "mode": "files"},
        )
        assert files.status_code == 200
        assert [match["path"] for match in files.json()["matches"]] == [
            "src/scanner.py"
        ]

        text = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/search",
            headers=AUTH,
            params={"query": "SCAN_TARGET", "mode": "text"},
        )
        assert text.status_code == 200
        assert text.json()["matches"] == [
            {
                "path": "src/scanner.py",
                "kind": "content",
                "line": 1,
                "column": 5,
                "preview": "def scan_target(host):",
            }
        ]
        assert text.json()["scanned_files"] == 2
        assert text.json()["truncated"] is False

        escaped = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/search",
            headers=AUTH,
            params={"query": "secret", "path": "../"},
        )
        assert escaped.status_code == 422
        assert escaped.json()["code"] == "workspace_path_invalid"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
def test_workspace_source_control_reports_status_and_hardened_diffs(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    git("init", "-b", "main")
    git("config", "user.name", "Nebula Test")
    git("config", "user.email", "nebula@example.invalid")
    tracked = root / "scanner.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    git("add", "scanner.txt")
    git("commit", "-m", "baseline")
    tracked.write_text("baseline\nchanged target\n", encoding="utf-8")
    (root / "new-rule.yaml").write_text("id: test\n", encoding="utf-8")

    textconv_marker = tmp_path / "textconv-ran"
    textconv = tmp_path / "malicious-textconv.sh"
    textconv.write_text(
        f"#!/bin/sh\ntouch '{textconv_marker}'\ncat \"$1\"\n", encoding="utf-8"
    )
    textconv.chmod(0o700)
    (root / ".gitattributes").write_text("*.txt diff=unsafe\n", encoding="utf-8")
    git("config", "diff.unsafe.textconv", str(textconv))

    with client:
        status = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/source-control",
            headers=AUTH,
        )
        assert status.status_code == 200
        payload = status.json()
        assert payload["state"] == "ready"
        assert payload["branch"] == "main"
        assert len(payload["head"]) == 12
        changed = {item["path"]: item for item in payload["files"]}
        assert changed["scanner.txt"]["worktree_status"] == "modified"
        assert changed["new-rule.yaml"]["worktree_status"] == "untracked"

        diff = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/source-control/diff",
            headers=AUTH,
            params={"path": "scanner.txt"},
        )
        assert diff.status_code == 200
        assert "+changed target" in diff.json()["text"]
        assert diff.json()["head"] == payload["head"]
        assert not textconv_marker.exists()

        escaped = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/source-control/diff",
            headers=AUTH,
            params={"path": "../outside"},
        )
        assert escaped.status_code == 422
        assert escaped.json()["code"] == "workspace_path_invalid"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
def test_workspace_source_control_refuses_parent_repository_scope(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    parent = root.parent
    subprocess.run(
        ["git", "-C", str(parent), "init"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    with client:
        response = client.get(
            f"/api/v1/engagements/{engagement.id}/workspace/source-control",
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["state"] == "not_repository"
        assert "outside the selected project boundary" in response.json()["detail"]


def test_host_workspace_folder_browser_lists_only_directories(tmp_path):
    _store, _artifacts, _platform, _workspace, _engagement, client = _services(tmp_path)
    selectable = tmp_path / "selectable-project"
    selectable.mkdir()
    (tmp_path / "ordinary-file.txt").write_text("not a folder", encoding="utf-8")

    response = client.get(
        "/api/v1/workspace-folders",
        params={"path": str(tmp_path)},
        headers=AUTH,
    )

    assert response.status_code == 200
    listing = response.json()
    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.resolve().parent)
    assert listing["truncated"] is False
    assert {"name": "selectable-project", "path": str(selectable.resolve())} in listing[
        "directories"
    ]
    assert "ordinary-file.txt" not in {
        directory["name"] for directory in listing["directories"]
    }

    unavailable = client.get(
        "/api/v1/workspace-folders",
        params={"path": str(tmp_path / "missing")},
        headers=AUTH,
    )
    assert unavailable.status_code == 404
    assert unavailable.json()["detail"] == "folder is unavailable"


def test_host_workspace_folder_browser_creates_one_bounded_child(tmp_path):
    _store, _artifacts, _platform, _workspace, _engagement, client = _services(tmp_path)

    created = client.post(
        "/api/v1/workspace-folders",
        headers=AUTH,
        json={"parent_path": str(tmp_path), "name": "new-project"},
    )

    assert created.status_code == 201
    listing = created.json()
    expected = (tmp_path / "new-project").resolve()
    assert expected.is_dir()
    assert listing["path"] == str(expected)
    assert listing["parent"] == str(tmp_path.resolve())

    duplicate = client.post(
        "/api/v1/workspace-folders",
        headers=AUTH,
        json={"parent_path": str(tmp_path), "name": "new-project"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "folder already exists"

    for invalid_name in ("../outside", "nested/child", r"nested\child"):
        rejected = client.post(
            "/api/v1/workspace-folders",
            headers=AUTH,
            json={"parent_path": str(tmp_path), "name": invalid_name},
        )
        assert rejected.status_code == 422
    assert not (tmp_path.parent / "outside").exists()
    assert not (tmp_path / "nested").exists()


def test_promotion_survives_symlink_safe_workspace_reset(tmp_path):
    store, artifacts, platform, workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    payload = b"immutable promoted evidence\n"
    (root / "result.bin").write_bytes(payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    os.symlink(outside, root / "linked-directory")

    with client:
        promoted = client.post(
            f"/api/v1/engagements/{engagement.id}/workspace/promote",
            headers=AUTH,
            json={"path": "result.bin", "title": "Exact result"},
        )
        assert promoted.status_code == 201
        evidence = promoted.json()
        assert evidence["sha256"] == hashlib.sha256(payload).hexdigest()

        wrong = client.post(
            f"/api/v1/engagements/{engagement.id}/workspace/reset",
            headers=AUTH,
            json={"engagement_name": "wrong"},
        )
        assert wrong.status_code == 422

        reset = client.post(
            f"/api/v1/engagements/{engagement.id}/workspace/reset",
            headers=AUTH,
            json={"engagement_name": engagement.name},
        )
        assert reset.status_code == 200
        assert reset.json()["removed_entries"] == 2

    assert list(root.iterdir()) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    artifact = store.get(Artifact, evidence["artifact_id"])
    assert artifacts.verify(artifact)
    assert artifacts.read(artifact) == payload


def test_workspace_entry_rename_and_delete_are_bounded_and_audited(tmp_path):
    store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / "old.txt").write_text("content", encoding="utf-8")
    (root / "occupied.txt").write_text("keep", encoding="utf-8")
    (root / "nonempty").mkdir()
    (root / "nonempty" / "child.txt").write_text("keep", encoding="utf-8")

    with client:
        renamed = client.patch(
            f"/api/v1/engagements/{engagement.id}/workspace/entry",
            headers=AUTH,
            json={"path": "old.txt", "new_name": "renamed.txt"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["path"] == "renamed.txt"
        assert not (root / "old.txt").exists()
        assert (root / "renamed.txt").read_text(encoding="utf-8") == "content"

        collision = client.patch(
            f"/api/v1/engagements/{engagement.id}/workspace/entry",
            headers=AUTH,
            json={"path": "renamed.txt", "new_name": "occupied.txt"},
        )
        assert collision.status_code == 409

        nonempty = client.delete(
            f"/api/v1/engagements/{engagement.id}/workspace/entry",
            headers=AUTH,
            params={"path": "nonempty"},
        )
        assert nonempty.status_code == 409
        assert (root / "nonempty" / "child.txt").exists()

        deleted = client.delete(
            f"/api/v1/engagements/{engagement.id}/workspace/entry",
            headers=AUTH,
            params={"path": "renamed.txt"},
        )
        assert deleted.status_code == 200
        assert not (root / "renamed.txt").exists()

    events = store.list_operation_events(engagement.id, limit=100)
    assert {event.event_type for event in events} >= {
        "workspace.renamed",
        "workspace.deleted",
    }


def test_reset_refuses_a_queued_execution(tmp_path):
    store, _artifacts, _platform, workspace, engagement, _client = _services(tmp_path)
    store.create(
        OperatorExecution(
            engagement_id=engagement.id,
            operator_id="operator-1",
            origin=ExecutionOrigin(kind="rerun", execution_id="parent"),
            language="python",
            source_sha256="a" * 64,
            source_artifact_id="source",
            runtime=ExecutionRuntimeSnapshot(
                language="python",
                interpreter="/usr/bin/python3",
                arguments=["-I", "-B"],
                runtime_digest="sha256:" + "b" * 64,
                image="sha256:" + "c" * 64,
                runner_profile_id="runner",
                runner_profile_revision=1,
                runner_runtime=RunnerRuntime.PODMAN,
                runner_isolation=RunnerIsolation.ROOTLESS,
                runner_executable="/usr/bin/podman",
                runner_platform="linux/amd64",
            ),
            preview_fingerprint="d" * 64,
            request_fingerprint="e" * 64,
            client_idempotency_key="queued",
            status=OperatorExecutionStatus.QUEUED,
        )
    )

    try:
        workspace.reset(
            engagement.id, WorkspaceResetRequest(engagement_name=engagement.name)
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "workspace_busy"
    else:
        raise AssertionError("queued execution should block workspace reset")


def test_reset_status_reports_active_execution_and_stable_recovery_code(tmp_path):
    store, _artifacts, _platform, _workspace, engagement, client = _services(tmp_path)
    execution = OperatorExecution(
        engagement_id=engagement.id,
        operator_id="operator-1",
        origin=ExecutionOrigin(kind="rerun", execution_id="parent"),
        language="python",
        source_sha256="a" * 64,
        source_artifact_id="source",
        runtime=ExecutionRuntimeSnapshot(
            language="python",
            interpreter="/usr/bin/python3",
            arguments=["-I", "-B"],
            runtime_digest="sha256:" + "b" * 64,
            image="sha256:" + "c" * 64,
            runner_profile_id="runner",
            runner_profile_revision=1,
            runner_runtime=RunnerRuntime.PODMAN,
            runner_isolation=RunnerIsolation.ROOTLESS,
            runner_executable="/usr/bin/podman",
            runner_platform="linux/amd64",
        ),
        preview_fingerprint="d" * 64,
        request_fingerprint="e" * 64,
        client_idempotency_key="status",
        status=OperatorExecutionStatus.RUNNING,
    )
    stored = store.create(execution)

    blocked = client.get(
        f"/api/v1/engagements/{engagement.id}/workspace/reset-status", headers=AUTH
    )
    assert blocked.status_code == 200
    assert blocked.json()["can_reset"] is False
    assert blocked.json()["active_execution_count"] == 1
    assert blocked.json()["reason_code"] == "workspace_busy"

    store.update(
        OperatorExecution,
        stored.id,
        {"status": OperatorExecutionStatus.COMPLETED},
        expected_revision=stored.revision,
    )
    ready = client.get(
        f"/api/v1/engagements/{engagement.id}/workspace/reset-status", headers=AUTH
    )
    assert ready.json()["can_reset"] is True
    assert ready.json()["reason_code"] is None


def test_workspace_upload_is_atomic_bounded_and_requires_overwrite(tmp_path):
    _store, _artifacts, platform, workspace, engagement, _client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    (root / "notes").mkdir()

    async def chunks(*values: bytes):
        for value in values:
            yield value

    result = asyncio.run(
        workspace.upload(
            engagement.id,
            "notes/result.txt",
            chunks(b"hello ", b"world"),
        )
    )
    assert result.path == "notes/result.txt"
    assert result.size == 11
    assert result.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert (root / result.path).read_bytes() == b"hello world"

    try:
        asyncio.run(
            workspace.upload(
                engagement.id,
                "notes/result.txt",
                chunks(b"replacement"),
            )
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "workspace_file_exists"
    else:
        raise AssertionError("upload should require overwrite confirmation")
    assert (root / result.path).read_bytes() == b"hello world"

    replaced = asyncio.run(
        workspace.upload(
            engagement.id,
            "notes/result.txt",
            chunks(b"replacement"),
            overwrite=True,
        )
    )
    assert replaced.overwritten is True
    assert (root / result.path).read_bytes() == b"replacement"
    assert not list((root / "notes").glob(".nebula-upload-*.tmp"))


def test_workspace_upload_rejects_escape_and_symlink_destination(tmp_path):
    _store, _artifacts, platform, workspace, engagement, _client = _services(tmp_path)
    root = platform.workspace_for(engagement.id)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, root / "linked")

    async def chunks():
        yield b"unsafe"

    for path in ("../escape", "linked"):
        try:
            asyncio.run(workspace.upload(engagement.id, path, chunks(), overwrite=True))
        except Exception as exc:
            assert getattr(exc, "code", None) in {
                "workspace_path_invalid",
                "workspace_file_exists",
            }
        else:
            raise AssertionError(f"unsafe upload {path!r} should fail")
    assert outside.read_text(encoding="utf-8") == "outside"


def test_workspace_streaming_upload_api_requires_explicit_overwrite(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)

    with client:
        created = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            params={"path": "result.bin"},
            content=b"first payload",
        )
        assert created.status_code == 201
        assert created.json()["sha256"] == hashlib.sha256(b"first payload").hexdigest()

        conflict = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            params={"path": "result.bin"},
            content=b"second payload",
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "workspace_file_exists"

        replaced = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            params={"path": "result.bin", "overwrite": "true"},
            content=b"second payload",
        )
        assert replaced.status_code == 201
        assert replaced.json()["overwritten"] is True

    assert (
        platform.workspace_for(engagement.id) / "result.bin"
    ).read_bytes() == b"second payload"


def test_workspace_upload_if_match_prevents_lost_terminal_changes(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)
    path = platform.workspace_for(engagement.id) / "shared.py"
    original = b"print('editor opened this')\n"
    terminal_change = b"print('terminal changed this')\n"
    original_sha256 = hashlib.sha256(original).hexdigest()

    path.write_bytes(original)
    path.write_bytes(terminal_change)

    with client:
        stale = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={
                **AUTH,
                "Content-Type": "application/octet-stream",
                "If-Match": original_sha256,
            },
            params={"path": "shared.py", "overwrite": "true"},
            content=b"print('stale editor save')\n",
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "workspace_file_changed"
        assert path.read_bytes() == terminal_change

        matched = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={
                **AUTH,
                "Content-Type": "application/octet-stream",
                "If-Match": hashlib.sha256(terminal_change).hexdigest(),
            },
            params={"path": "shared.py", "overwrite": "true"},
            content=b"print('fresh editor save')\n",
        )
        assert matched.status_code == 201
        assert matched.json()["overwritten"] is True
        assert path.read_bytes() == b"print('fresh editor save')\n"


def test_workspace_upload_if_match_rejects_deleted_file(tmp_path):
    _store, _artifacts, platform, _workspace, engagement, client = _services(tmp_path)

    with client:
        response = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={
                **AUTH,
                "Content-Type": "application/octet-stream",
                "If-Match": "a" * 64,
            },
            params={"path": "deleted.py", "overwrite": "true"},
            content=b"print('replacement')\n",
        )

    assert response.status_code == 412
    assert response.json()["code"] == "workspace_file_changed"
    assert not (platform.workspace_for(engagement.id) / "deleted.py").exists()
