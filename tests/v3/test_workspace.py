from __future__ import annotations

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
