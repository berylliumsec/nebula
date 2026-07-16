from __future__ import annotations

import asyncio
import hashlib
import os

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
                tool_pack_installation_id="pack",
                manifest_digest="b" * 64,
                image="example.invalid/toolbox@sha256:" + "c" * 64,
                runner_profile_id="runner",
                runner_profile_revision=1,
                runner_runtime=RunnerRuntime.PODMAN,
                runner_isolation=RunnerIsolation.ROOTLESS,
                runner_executable="/usr/bin/podman",
                runner_platform="linux/amd64",
                trusted=True,
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
