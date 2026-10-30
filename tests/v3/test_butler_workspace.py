"""Butler's workspace file catalog uses the selected Core workspace."""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import ButlerRecoveryCase, Engagement
from nebula.v3.runtime_platform import RuntimePlatform
from nebula.v3.storage import NebulaStore


def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    selected = store.create(Engagement(name="Selected workspace"))
    foreign = store.create(Engagement(name="Other workspace"))
    workspace = app.state.workspace_service
    assert runtime.workspace_service is workspace
    assert app.state.harness_runtime_service.workspace_service is workspace
    capabilities = ButlerCapabilities(
        store, artifacts, selected.id, workspace=workspace
    )
    return store, artifacts, workspace, runtime, selected, foreign, capabilities


def test_butler_workspace_file_lifecycle_uses_hash_and_project_boundary(tmp_path):
    _store, _artifacts, _workspace, runtime, selected, foreign, capabilities = _fixture(
        tmp_path
    )
    root = runtime.workspace_for(selected.id)
    (root / "notes").mkdir()
    foreign_root = runtime.workspace_for(foreign.id)
    (foreign_root / "private.txt").write_text("foreign secret")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")
    os.symlink(outside, root / "escape.txt")

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.workspace.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        found = await capabilities.search({"query": "workspace file delete download"})
        assert any(
            item["operation"] == "nebula.workspace.delete_file"
            for item in found["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.workspace.read"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (
            await capabilities.describe({"operation": "nebula.workspace.download_file"})
        )["invoke_with"] == "nebula.capabilities.change"
        listed = await invoke("list")
        assert [(item["name"], item["kind"]) for item in listed["entries"]] == [
            ("notes", "directory"),
            ("escape.txt", "symlink"),
        ]
        assert not any(item["name"] == "private.txt" for item in listed["entries"])
        with pytest.raises(Exception):
            await invoke("read", {"path": "escape.txt"})
        with pytest.raises(Exception):
            await invoke("read", {"path": "../outside.txt"})

        source = "A" * 12_100 + " λ\n"
        created = await invoke(
            "create_text",
            {"path": "notes/research.txt", "content": source},
            change=True,
        )
        assert created["sha256"] == hashlib.sha256(source.encode()).hexdigest()
        assert created["overwritten"] is False
        assert (root / "notes/research.txt").read_text() == source
        with pytest.raises(Exception, match="already exists"):
            await invoke(
                "create_text",
                {"path": "notes/research.txt", "content": "duplicate"},
                change=True,
            )
        first = await invoke("read", {"path": "notes/research.txt"})
        assert len(first["text"]) == 12_000
        assert first["next_offset"] == 12_000
        assert (await invoke("read", {"path": "notes/research.txt", "offset": 12_000}))[
            "text"
        ] == source[12_000:]
        with pytest.raises(Exception, match="changed"):
            await invoke(
                "replace_text",
                {
                    "path": "notes/research.txt",
                    "content": "stale",
                    "expected_sha256": "a" * 64,
                },
                change=True,
            )
        assert (root / "notes/research.txt").read_text() == source
        replaced = await invoke(
            "replace_text",
            {
                "path": "notes/research.txt",
                "content": "Current notes\n",
                "expected_sha256": first["sha256"],
            },
            change=True,
        )
        assert replaced["overwritten"] is True
        assert (root / "notes/research.txt").read_text() == "Current notes\n"
        with pytest.raises(Exception, match="changed"):
            await invoke(
                "rename_file",
                {
                    "path": "notes/research.txt",
                    "new_name": "renamed.txt",
                    "expected_sha256": first["sha256"],
                },
                change=True,
            )
        renamed = await invoke(
            "rename_file",
            {
                "path": "notes/research.txt",
                "new_name": "renamed.txt",
                "expected_sha256": replaced["sha256"],
            },
            change=True,
        )
        assert renamed["path"] == "notes/renamed.txt"
        assert (root / "notes/renamed.txt").read_text() == "Current notes\n"
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "delete_file",
                {"path": "notes/renamed.txt", "expected_sha256": first["sha256"]},
                change=True,
            )
        pending = await invoke(
            "delete_file",
            {"path": "notes/renamed.txt", "expected_sha256": renamed["sha256"]},
            change=True,
        )
        assert pending["client_action"] == {
            "kind": "workspace_delete",
            "project_id": selected.id,
            "path": "notes/renamed.txt",
            "expected_sha256": renamed["sha256"],
        }
        download = await invoke(
            "download_file", {"path": "notes/renamed.txt"}, change=True
        )
        assert download["client_action"]["expected_sha256"] == renamed["sha256"]
        assert "Current notes" not in str(download)
        assert not (foreign_root / "notes/renamed.txt").exists()

    asyncio.run(scenario())


def test_scheduled_butler_workspace_actions_are_unavailable(tmp_path):
    store, artifacts, workspace, _runtime, selected, _foreign, interactive = _fixture(
        tmp_path
    )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="immediate",
            signal="workspace.test",
            source_event_id="source-event",
            source_event_sequence=1,
            authority_revision=1,
            summary="Synthetic background case",
        )
    )
    scheduled = ButlerCapabilities(
        store,
        artifacts,
        selected.id,
        workspace=workspace,
        background_case_id=case.id,
    )
    assert "nebula.workspace.delete_file" in interactive._actions
    assert not any(name.startswith("nebula.workspace.") for name in scheduled._actions)


def test_butler_workspace_directory_actions_and_device_handoff(tmp_path):
    _store, _artifacts, workspace, runtime, selected, _foreign, capabilities = _fixture(
        tmp_path
    )

    async def invoke(name, arguments=None):
        return await capabilities.invoke(
            {"operation": f"nebula.workspace.{name}", "arguments": arguments or {}},
            change=name != "list",
        )

    async def scenario():
        root = await invoke("list")
        assert len(root["directory_identity"]) == 64
        created = await invoke(
            "create_directory",
            {
                "path": "captures",
                "expected_parent_identity": root["directory_identity"],
            },
        )
        assert created["kind"] == "directory"
        assert (runtime.workspace_for(selected.id) / "captures").is_dir()
        with pytest.raises(Exception, match="changed"):
            await invoke(
                "create_directory",
                {
                    "path": "other",
                    "expected_parent_identity": root["directory_identity"],
                },
            )
        folder = await invoke("list", {"path": "captures"})
        assert folder["directory_identity"] == created["entry_identity"]
        pending = await invoke(
            "upload_from_device",
            {
                "directory": "captures",
                "expected_directory_identity": folder["directory_identity"],
            },
        )
        assert pending["client_action"] == {
            "kind": "workspace_upload",
            "project_id": selected.id,
            "directory": "captures",
            "expected_directory_identity": folder["directory_identity"],
        }
        assert "file bytes" not in str(pending)
        renamed = await invoke(
            "rename_directory",
            {
                "path": "captures",
                "new_name": "archive",
                "expected_entry_identity": folder["directory_identity"],
            },
        )
        assert renamed["path"] == "archive"
        with pytest.raises(Exception, match="changed"):
            await invoke(
                "delete_directory",
                {
                    "path": "archive",
                    "expected_entry_identity": folder["directory_identity"],
                },
            )
        current = await invoke("list", {"path": "archive"})
        deletion = await invoke(
            "delete_directory",
            {
                "path": "archive",
                "expected_entry_identity": current["directory_identity"],
            },
        )
        assert deletion["client_action"]["kind"] == "workspace_delete_directory"
        assert (runtime.workspace_for(selected.id) / "archive").exists()
        workspace.delete(
            selected.id,
            "archive",
            expected_entry_identity=current["directory_identity"],
        )
        assert not (runtime.workspace_for(selected.id) / "archive").exists()

    asyncio.run(scenario())
