"""Interactive Butler Notebook actions keep Core's project and grant boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    McpServerProfile,
    NotebookRun,
)
from nebula.v3.notebook_mcp import NotebookMcpTools
from nebula.v3.notebooks import NotebookService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, GuidedToolError


def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    root = tmp_path / "selected"
    root.mkdir()
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    selected = store.create(Engagement(name="Selected", workspace_path=str(root)))
    foreign = store.create(Engagement(name="Foreign", workspace_path=str(foreign_root)))
    notebooks = NotebookService(
        store=store,
        workspace_resolver=lambda project_id: (
            root if project_id == selected.id else foreign_root
        ),
        automation_runtime=None,
        data_root=tmp_path / "runtime",
    )
    profile = NotebookMcpTools(store, notebooks).register_profile(selected.id)
    capabilities = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        selected.id,
        notebooks=notebooks,
    )
    return store, selected, foreign, notebooks, profile, capabilities


def test_butler_notebook_pages_exact_cells_and_rejects_stale_edits(tmp_path):
    store, selected, foreign, notebooks, _, capabilities = _fixture(tmp_path)

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        found = await capabilities.search({"query": "notebook cell"})
        assert any(
            row["operation"] == "nebula.notebook.edit_cell" for row in found["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.notebook.read"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (await capabilities.describe({"operation": "nebula.notebook.create"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        created = await invoke("create", {"path": "research.ipynb"}, change=True)
        assert created["path"] == "research.ipynb"
        assert created["total_cells"] == 0
        with pytest.raises(Exception, match="already exists"):
            await invoke("create", {"path": "research.ipynb"}, change=True)
        revision = created["revision"]
        for index in range(22):
            saved = await invoke(
                "edit_cell",
                {
                    "path": "research.ipynb",
                    "source": "x" * 12_100 if index == 0 else f"print({index})",
                    "cell_type": "code",
                    "expected_revision": revision,
                },
                change=True,
            )
            revision = saved["revision"]
        listed = await invoke("list")
        assert [row["path"] for row in listed["notebooks"]] == ["research.ipynb"]
        await notebooks.create(foreign.id, "foreign.ipynb")
        assert "foreign.ipynb" not in [
            row["path"] for row in (await invoke("list"))["notebooks"]
        ]
        first = await invoke("read", {"path": "research.ipynb"})
        assert first["total_cells"] == 22
        assert len(first["cells"]) == 20
        assert first["next_offset"] == 20
        last = await invoke("read", {"path": "research.ipynb", "offset": 20})
        assert len(last["cells"]) == 2
        assert last["next_offset"] is None
        cell_id = first["cells"][0]["id"]
        source = await invoke("source", {"path": "research.ipynb", "cell_id": cell_id})
        assert len(source["source"]) == 12_000
        assert source["next_offset"] == 12_000
        assert (
            await invoke(
                "source",
                {"path": "research.ipynb", "cell_id": cell_id, "offset": 12_000},
            )
        )["source"] == "x" * 100
        with pytest.raises(Exception, match="changed"):
            await invoke(
                "edit_cell",
                {
                    "path": "research.ipynb",
                    "cell_id": cell_id,
                    "source": "overwritten",
                    "cell_type": "code",
                    "expected_source_hash": "0" * 64,
                },
                change=True,
            )
        assert (await invoke("source", {"path": "research.ipynb", "cell_id": cell_id}))[
            "source"
        ] == "x" * 12_000
        with pytest.raises(CapabilityInputError, match="invalid arguments"):
            await invoke("read", {"path": "../foreign/foreign.ipynb", "extra": True})
        with pytest.raises(Exception, match="project-relative|escapes"):
            await invoke("read", {"path": "../foreign/foreign.ipynb"})

    asyncio.run(scenario())


def test_butler_notebook_grant_revocation_blocks_next_action(tmp_path):
    store, selected, _, _, profile, capabilities = _fixture(tmp_path)

    async def scenario():
        assert (
            await capabilities.invoke(
                {"operation": "nebula.notebook.list", "arguments": {}}, change=False
            )
        )["notebooks"] == []
        store.update(
            McpServerProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
        with pytest.raises(GuidedToolError, match="revoked"):
            await capabilities.invoke(
                {
                    "operation": "nebula.notebook.create",
                    "arguments": {"path": "blocked.ipynb"},
                },
                change=True,
            )
        assert not (tmp_path / "selected" / "blocked.ipynb").exists()

    asyncio.run(scenario())


def test_butler_notebook_package_lookup_requires_policy_and_excludes_unverified_changes(
    tmp_path,
):
    _, selected, _, notebooks, _, capabilities = _fixture(tmp_path)
    runtime = SimpleNamespace(
        policy=AutomationProjectPolicy(
            engagement_id=selected.id,
            execution_mode="host",
            host_access_acknowledged=False,
            network_enabled=False,
        )
    )
    runtime.project_policy = lambda _: runtime.policy
    notebooks.automation_runtime = runtime

    async def invoke(name, arguments, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments},
            change=change,
        )

    async def scenario():
        created = await invoke("create", {"path": "policy.ipynb"}, change=True)
        saved = await invoke(
            "edit_cell",
            {
                "path": "policy.ipynb",
                "source": "print(42)",
                "cell_type": "code",
                "expected_revision": created["revision"],
            },
            change=True,
        )
        assert (await invoke("status", {}))["consent_required"] is True
        for operation in ("execute", "interrupt"):
            assert (
                await capabilities.describe(
                    {"operation": f"nebula.notebook.{operation}"}
                )
            )["invoke_with"] == "nebula.capabilities.change"
        for operation in ("restart", "install_package"):
            assert (
                await capabilities.describe(
                    {"operation": f"nebula.notebook.{operation}"}
                )
            )["invoke_with"] == "nebula.capabilities.change"
        with pytest.raises(GuidedToolError) as execution_consent:
            await invoke(
                "execute",
                {
                    "path": "policy.ipynb",
                    "cell_id": saved["cell_id"],
                    "expected_source_hash": saved["cells"][0]["source_hash"],
                },
                change=True,
            )
        assert execution_consent.value.code == "notebook.permission_required"
        assert (await invoke("recent_runs", {"path": "policy.ipynb"}))["runs"] == []
        assert (
            await capabilities.describe({"operation": "nebula.notebook.search_package"})
        )["invoke_with"] == "nebula.capabilities.read"
        with pytest.raises(GuidedToolError) as consent:
            await invoke("search_package", {"path": "policy.ipynb", "name": "colorama"})
        assert consent.value.code == "notebook.permission_required"
        runtime.policy = runtime.policy.model_copy(
            update={"host_access_acknowledged": True}
        )
        with pytest.raises(GuidedToolError) as network:
            await invoke("search_package", {"path": "policy.ipynb", "name": "colorama"})
        assert network.value.code == "notebook.permission_required"
        assert "network" in str(network.value).lower()

    asyncio.run(scenario())


def test_butler_notebook_restarts_only_the_current_idle_kernel(tmp_path):
    _, selected, _, notebooks, _, capabilities = _fixture(tmp_path)
    notebooks.automation_runtime = SimpleNamespace(
        project_policy=lambda _: AutomationProjectPolicy(
            engagement_id=selected.id,
            execution_mode="host",
            host_access_acknowledged=True,
            network_enabled=False,
        )
    )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        file = await invoke("create", {"path": "restart.ipynb"}, change=True)
        absent = await invoke("kernel", {"path": "restart.ipynb"})
        assert absent["state"] == "absent"
        assert (await notebooks.restart(selected.id, "restart.ipynb"))[
            "restarted"
        ] is False
        commands = []

        async def command(operation):
            commands.append(operation)

        worker = SimpleNamespace(
            kernel_id="a" * 32,
            restart_count=0,
            active_run=None,
            process=SimpleNamespace(returncode=None),
            command=command,
        )
        notebooks.workers[(selected.id, "restart.ipynb")] = worker
        before = await invoke("kernel", {"path": "restart.ipynb"})
        assert before["notebook_revision"] == file["revision"]
        arguments = {
            "path": "restart.ipynb",
            "expected_notebook_revision": file["revision"],
            "expected_kernel_id": worker.kernel_id,
            "expected_restart_count": 0,
        }
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "restart", {**arguments, "expected_kernel_id": "b" * 32}, change=True
            )
        assert commands == []
        result = await invoke("restart", arguments, change=True)
        assert result["restarted"] is True
        assert result["kernel"]["restart_count"] == 1
        assert commands == ["restart"]
        with pytest.raises(ValueError, match="changed|already restarted"):
            await invoke("restart", arguments, change=True)
        assert commands == ["restart"]
        worker.active_run = "active-run"
        with pytest.raises(GuidedToolError, match="running"):
            await invoke(
                "restart", {**arguments, "expected_restart_count": 1}, change=True
            )
        assert commands == ["restart"]

    asyncio.run(scenario())


def test_butler_notebook_file_index_uses_browser_choice_and_current_project_revision(
    tmp_path,
):
    store, selected, _, notebooks, profile, capabilities = _fixture(tmp_path)
    index = tmp_path / "inventory.db"
    with sqlite3.connect(index) as connection:
        connection.executescript(
            "CREATE TABLE repositories (id INTEGER PRIMARY KEY, root TEXT UNIQUE, complete INTEGER, pending_files INTEGER, last_reconciled_at TEXT);"
            "CREATE TABLE files (repository_id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER);"
        )
        connection.execute(
            "INSERT INTO repositories VALUES (1, ?, 1, 0, ?)",
            (str(tmp_path / "selected"), datetime.now(timezone.utc).isoformat()),
        )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        before = await invoke("file_index")
        assert before["state"] == "none"
        revision = before["project_revision"]
        requested = await invoke(
            "choose_file_index",
            {"expected_project_revision": revision},
            change=True,
        )
        assert requested["client_action"] == {
            "kind": "notebook_choose_file_index",
            "project_id": selected.id,
            "expected_project_revision": revision,
        }
        assert "path" not in requested["client_action"]
        assert (await invoke("file_index"))["path"] is None
        await notebooks.set_file_index(
            selected.id, str(index), expected_project_revision=revision
        )
        connected = await invoke("file_index")
        assert connected["path"] == str(index)
        with pytest.raises(ValueError, match="project changed"):
            await invoke(
                "remove_file_index",
                {"expected_project_revision": revision},
                change=True,
            )
        assert (await invoke("file_index"))["path"] == str(index)
        removed = await invoke(
            "remove_file_index",
            {"expected_project_revision": connected["project_revision"]},
            change=True,
        )
        assert removed["file_index"]["path"] is None
        assert (
            removed["file_index"]["project_revision"]
            == connected["project_revision"] + 1
        )
        store.update(
            McpServerProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
        with pytest.raises(GuidedToolError, match="revoked"):
            await invoke("file_index")

    asyncio.run(scenario())


def test_butler_notebook_install_reads_selected_environment_and_rejects_stale_file(
    tmp_path,
):
    _, selected, _, notebooks, _, capabilities = _fixture(tmp_path)
    policy = AutomationProjectPolicy(
        engagement_id=selected.id,
        execution_mode="host",
        host_access_acknowledged=True,
        network_enabled=False,
    )
    notebooks.automation_runtime = SimpleNamespace(project_policy=lambda _: policy)
    installed = False
    commands = []

    async def environment(_project):
        return Path(sys.executable), Path("/worker.py"), Path("/fixture/uv")

    async def capture(command, timeout=120):
        nonlocal installed
        commands.append((command, timeout))
        if command[0] == "/fixture/uv":
            assert "--offline" in command
            installed = True
            return "Audited one distribution"
        return json.dumps(
            {
                "installed": installed,
                "name": "pip",
                "version": "25.0" if installed else None,
            }
        )

    notebooks._host_environment = environment
    notebooks._capture = capture

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        file = await invoke("create", {"path": "packages.ipynb"}, change=True)
        status = await invoke(
            "package_status", {"path": "packages.ipynb", "name": "pip"}
        )
        assert status["installed"] is False
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "install_package",
                {
                    "path": "packages.ipynb",
                    "requirement": "pip==25.0",
                    "expected_notebook_revision": "0" * 64,
                },
                change=True,
            )
        assert not installed
        changed = await invoke(
            "install_package",
            {
                "path": "packages.ipynb",
                "requirement": "pip==25.0",
                "expected_notebook_revision": file["revision"],
            },
            change=True,
        )
        assert changed["package"]["name"] == "pip"
        assert changed["package"]["version"] == "25.0"
        assert changed["restart_recommended"] is True
        assert sum(command[0][0] == "/fixture/uv" for command in commands) == 1

    asyncio.run(scenario())


def test_butler_notebook_runs_exact_source_and_reads_back_interruption(tmp_path):
    store, selected, foreign, notebooks, _, capabilities = _fixture(tmp_path)
    runtime = SimpleNamespace(
        policy=AutomationProjectPolicy(
            engagement_id=selected.id,
            execution_mode="host",
            host_access_acknowledged=True,
            network_enabled=False,
        )
    )
    runtime.project_policy = lambda _: runtime.policy
    notebooks.automation_runtime = runtime

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notebook.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        created = await invoke("create", {"path": "exact.ipynb"}, change=True)
        saved = await invoke(
            "edit_cell",
            {
                "path": "exact.ipynb",
                "source": "print(42)",
                "cell_type": "code",
                "expected_revision": created["revision"],
            },
            change=True,
        )
        cell_id = saved["cell_id"]
        source_hash = saved["cells"][0]["source_hash"]
        assert (await capabilities.describe({"operation": "nebula.notebook.execute"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        with pytest.raises(CapabilityInputError, match="invalid arguments"):
            await invoke(
                "execute", {"path": "exact.ipynb", "cell_id": cell_id}, change=True
            )
        with pytest.raises(GuidedToolError) as stale:
            await invoke(
                "execute",
                {
                    "path": "exact.ipynb",
                    "cell_id": cell_id,
                    "expected_source_hash": "0" * 64,
                },
                change=True,
            )
        assert stale.value.code == "notebook.conflict"
        assert (await invoke("recent_runs", {"path": "exact.ipynb"}))["runs"] == []

        preparing = asyncio.Event()
        release = asyncio.Event()

        async def slow_worker(*_args):
            preparing.set()
            await release.wait()
            return object()

        notebooks._worker = slow_worker
        queued = await invoke(
            "execute",
            {
                "path": "exact.ipynb",
                "cell_id": cell_id,
                "expected_source_hash": source_hash,
            },
            change=True,
        )
        run_id = queued["run"]["id"]
        assert queued["run"]["source_hash"] == source_hash
        assert queued["run"]["status"] == "queued"
        assert (await invoke("recent_runs", {"path": "exact.ipynb"}))["runs"][0][
            "id"
        ] == run_id
        await asyncio.wait_for(preparing.wait(), timeout=5)
        interrupted = await invoke("interrupt", {"run_id": run_id}, change=True)
        assert interrupted["run"]["status"] == "interrupted"
        release.set()
        observed = await invoke("wait", {"run_id": run_id, "timeout": 5})
        assert observed["run"]["status"] == "interrupted"
        events = notebooks.run(run_id, engagement_id=selected.id)["events"]
        assert [event["event_type"] for event in events].count(
            "notebook.completed"
        ) == 1
        foreign_run = store.create(
            NotebookRun(
                engagement_id=foreign.id,
                notebook_path="foreign.ipynb",
                cell_id="other",
                source_hash="a" * 64,
                execution_mode="host",
            )
        )
        with pytest.raises(GuidedToolError):
            await invoke("run", {"run_id": foreign_run.id})

    asyncio.run(scenario())
