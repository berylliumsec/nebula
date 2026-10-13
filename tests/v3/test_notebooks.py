import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    McpServerProfile,
    NotebookRun,
)
from nebula.v3.mcp import McpProbeError, McpProbeService, resolve_mcp_profiles
from nebula.v3.notebook_mcp import NotebookMcpTools
from nebula.v3.notebooks import (
    NotebookConflict,
    NotebookPermissionRequired,
    NotebookService,
    NotebookWorkerConnection,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.credentials import CredentialStore


def _project(tmp_path):
    store = NebulaStore(tmp_path / "store.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = store.create(
        Engagement(name="Notebook project", workspace_path=str(workspace))
    )
    notebooks = NotebookService(
        store=store,
        workspace_resolver=lambda _: workspace,
        automation_runtime=None,
        data_root=tmp_path / "data",
    )
    return store, project, notebooks


def test_notebook_documents_are_atomic_and_reject_stale_cell_edits(tmp_path):
    store, project, notebooks = _project(tmp_path)

    async def scenario():
        created = await notebooks.create(project.id, "research/quick.ipynb")
        assert created["document"]["cells"] == []
        first = await notebooks.upsert_cell(
            project.id,
            "research/quick.ipynb",
            cell_id=None,
            source="print(1)",
            expected_revision=created["revision"],
            expected_source_hash=None,
        )
        cell_id = first["cell_id"]
        with pytest.raises(NotebookConflict, match="changed"):
            await notebooks.upsert_cell(
                project.id,
                "research/quick.ipynb",
                cell_id=cell_id,
                source="print(2)",
                expected_revision=None,
                expected_source_hash="0" * 64,
            )
        previous = notebooks.read(project.id, "research/quick.ipynb")["document"]
        previous["cells"][0]["outputs"] = [
            {"output_type": "stream", "name": "stdout", "text": "old result"}
        ]
        notebooks.documents._atomic_write(
            notebooks.documents.path(project.id, "research/quick.ipynb"), previous
        )
        saved = await notebooks.upsert_cell(
            project.id,
            "research/quick.ipynb",
            cell_id=cell_id,
            source="print(3)",
            expected_revision=None,
            expected_source_hash=first["cell_revisions"][cell_id],
        )
        assert saved["document"]["cells"][0]["source"] == "print(3)"
        assert saved["document"]["cells"][0]["outputs"] == []
        with pytest.raises(Exception, match="escapes|project-relative"):
            notebooks.read(project.id, "../outside.ipynb")

    asyncio.run(scenario())


def test_notebook_mcp_is_project_scoped_and_revocation_is_immediate(tmp_path):
    store, project, notebooks = _project(tmp_path)
    other = store.create(
        Engagement(name="Other project", workspace_path=str(tmp_path / "workspace"))
    )
    tools = NotebookMcpTools(store, notebooks)
    profile = tools.register_profile(project.id)
    probe = McpProbeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path / "workspace",
    )
    probe.bind_notebooks(notebooks)
    assert (
        resolve_mcp_profiles(store, [profile.id], engagement_id=project.id)[0].id
        == profile.id
    )
    with pytest.raises(McpProbeError, match="another project"):
        resolve_mcp_profiles(store, [profile.id], engagement_id=other.id)

    async def scenario():
        result = await probe.call_tool(
            profile, engagement_id=project.id, tool_name="list", arguments={}
        )
        assert result["structuredContent"] == []
        narrowed = store.update(
            McpServerProfile,
            profile.id,
            {"disabled_tools": ["list"]},
            expected_revision=profile.revision,
        )
        with pytest.raises(McpProbeError, match="tool grant was revoked"):
            await probe.call_tool(
                profile, engagement_id=project.id, tool_name="list", arguments={}
            )
        store.update(
            McpServerProfile,
            profile.id,
            {"enabled": False},
            expected_revision=narrowed.revision,
        )
        with pytest.raises(McpProbeError, match="revoked|disabled"):
            await probe.call_tool(
                profile, engagement_id=project.id, tool_name="list", arguments={}
            )

    asyncio.run(scenario())


def test_package_search_respects_project_network_policy(tmp_path):
    _, project, notebooks = _project(tmp_path)
    policy = AutomationProjectPolicy(
        engagement_id=project.id,
        execution_mode="host",
        host_access_acknowledged=True,
        network_enabled=False,
    )
    notebooks.automation_runtime = type(
        "Runtime", (), {"project_policy": lambda self, _: policy}
    )()

    async def scenario():
        await notebooks.create(project.id, "packages.ipynb")
        with pytest.raises(
            NotebookPermissionRequired, match="Enable project network access"
        ):
            await notebooks.search_package(project.id, "packages.ipynb", "numpy")

    asyncio.run(scenario())


def test_package_install_requires_host_consent(tmp_path):
    _, project, notebooks = _project(tmp_path)
    policy = AutomationProjectPolicy(
        engagement_id=project.id,
        execution_mode="host",
        host_access_acknowledged=False,
        network_enabled=True,
    )
    notebooks.automation_runtime = type(
        "Runtime", (), {"project_policy": lambda self, _: policy}
    )()

    async def scenario():
        await notebooks.create(project.id, "packages.ipynb")
        with pytest.raises(NotebookPermissionRequired, match="Acknowledge Host access"):
            await notebooks.install_package(project.id, "packages.ipynb", "numpy")

    asyncio.run(scenario())


def test_run_replay_uses_an_ordered_sequence_cursor(tmp_path):
    store, project, notebooks = _project(tmp_path)
    run = store.create(
        NotebookRun(
            engagement_id=project.id,
            notebook_path="replay.ipynb",
            cell_id="cell",
            source_hash="a" * 64,
            execution_mode="host",
        )
    )
    for number in range(3):
        store.append_event(run.id, "notebook.output", {"text": str(number)})
    first = notebooks.run(run.id, engagement_id=project.id)
    assert [item["sequence"] for item in first["events"]] == [1, 2, 3]
    assert first["next_sequence"] == 3
    next_page = notebooks.run(run.id, engagement_id=project.id, after_sequence=2)
    assert [item["payload"]["text"] for item in next_page["events"]] == ["2"]
    assert (
        notebooks.run(run.id, engagement_id=project.id, after_sequence=3)["events"]
        == []
    )


def test_notebook_event_stream_authenticates_and_replays_in_order(tmp_path):
    store, project, _ = _project(tmp_path)
    run = store.create(
        NotebookRun(
            engagement_id=project.id,
            notebook_path="stream.ipynb",
            cell_id="cell",
            source_hash="a" * 64,
            execution_mode="host",
        )
    )
    store.append_event(run.id, "notebook.output", {"text": "first"})
    store.append_event(run.id, "notebook.completed", {"status": "succeeded"})
    store.update(
        NotebookRun, run.id, {"status": "succeeded"}, expected_revision=run.revision
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    url = f"/api/v1/engagements/{project.id}/notebooks/runs/{run.id}/events"
    assert client.get(url).status_code == 401
    with client.stream(
        "GET", url, headers={"Authorization": "Bearer test-token"}
    ) as response:
        assert response.status_code == 200
        frames = "".join(response.iter_text())
    assert frames.index("id: 1") < frames.index("id: 2")
    assert "event: notebook" in frames
    assert '"text": "first"' in frames


def test_mode_change_exposes_active_runs_and_waits_for_an_explicit_interrupt(tmp_path):
    store, project, notebooks = _project(tmp_path)
    policy = AutomationProjectPolicy(engagement_id=project.id, execution_mode="docker")
    notebooks.automation_runtime = type(
        "Runtime", (), {"project_policy": lambda self, _: policy}
    )()
    run = store.create(
        NotebookRun(
            engagement_id=project.id,
            notebook_path="queued.ipynb",
            cell_id="cell",
            source_hash="a" * 64,
            execution_mode="docker",
        )
    )

    async def scenario():
        assert [item.id for item in notebooks.active_runs(project.id)] == [run.id]
        with pytest.raises(NotebookConflict, match="interrupt it"):
            await notebooks.prepare_policy_change(project.id, "host")
        await notebooks.interrupt(run.id, engagement_id=project.id)
        assert notebooks.active_runs(project.id) == []
        assert await notebooks.prepare_policy_change(project.id, "host") is True
        with pytest.raises(NotebookConflict, match="mode is changing"):
            await notebooks.start_run(project.id, "queued.ipynb", "cell")
        notebooks.finish_policy_change(project.id)

    asyncio.run(scenario())


def test_queued_interrupt_during_kernel_start_remains_interrupted(tmp_path):
    store, project, notebooks = _project(tmp_path)
    policy = AutomationProjectPolicy(
        engagement_id=project.id,
        execution_mode="host",
        host_access_acknowledged=True,
    )
    notebooks.automation_runtime = type(
        "Runtime", (), {"project_policy": lambda self, _: policy}
    )()

    async def scenario():
        created = await notebooks.create(project.id, "queued.ipynb")
        saved = await notebooks.upsert_cell(
            project.id,
            "queued.ipynb",
            cell_id=None,
            source="print(1)",
            expected_revision=created["revision"],
            expected_source_hash=None,
        )
        preparing = asyncio.Event()
        release = asyncio.Event()

        async def slow_worker(*_args):
            preparing.set()
            await release.wait()
            return object()

        notebooks._worker = slow_worker
        run = await notebooks.start_run(project.id, "queued.ipynb", saved["cell_id"])
        await asyncio.wait_for(preparing.wait(), timeout=5)
        await notebooks.interrupt(run.id, engagement_id=project.id)
        release.set()
        result = await notebooks.wait_run(run.id, engagement_id=project.id, timeout=5)
        assert result["run"]["status"] == "interrupted"
        assert store.get(NotebookRun, run.id).status == "interrupted"
        assert [event["event_type"] for event in result["events"]].count(
            "notebook.completed"
        ) == 1

    asyncio.run(scenario())


def test_bundled_runtime_restages_when_only_the_worker_changes(tmp_path):
    _, _, notebooks = _project(tmp_path)
    resource = tmp_path / "bundled-runtime"
    python = resource / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"portable python")
    (resource / "uv").write_bytes(b"bundled uv")
    worker = resource / "worker.py"

    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def write_manifest(worker_source: bytes) -> None:
        worker.write_bytes(worker_source)
        (resource / "nebula-notebook-runtime.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "python_archive_sha256": "a" * 64,
                    "requirements_sha256": "b" * 64,
                    "executables": {
                        "python/bin/python3": digest(python.read_bytes()),
                        "uv": digest((resource / "uv").read_bytes()),
                    },
                    "worker_sha256": digest(worker_source),
                }
            )
        )

    async def scenario():
        write_manifest(b"worker v1")
        first = await notebooks._staged_runtime(resource)
        write_manifest(b"worker v2")
        second = await notebooks._staged_runtime(resource)
        assert first != second
        assert (first / "worker.py").read_bytes() == b"worker v1"
        assert (second / "worker.py").read_bytes() == b"worker v2"

    asyncio.run(scenario())


@pytest.mark.skipif(
    importlib.util.find_spec("jupyter_client") is None,
    reason="Jupyter worker dependencies are staged only in the packaged runtime",
)
def test_jupyter_worker_streams_and_reuses_a_kernel(tmp_path):
    worker_script = Path(__file__).resolve().parents[2] / "packaging/notebook/worker.py"

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker_script),
            cwd=str(tmp_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        events = []
        worker = NotebookWorkerConnection(
            process,
            lambda run_id, event: events.append((run_id, event)),
            mode="host",
            policy_revision=1,
        )
        try:
            await asyncio.wait_for(worker.ready, timeout=30)
            assert (
                await asyncio.wait_for(
                    worker.execute("one", "value = 7\nprint(value)"), timeout=30
                )
                == "succeeded"
            )
            assert (
                await asyncio.wait_for(
                    worker.execute("two", "print(value + 1)"), timeout=30
                )
                == "succeeded"
            )
            assert any(
                item[0] == "one"
                and item[1]["kind"] == "stream"
                and "7" in item[1]["content"]["text"]
                for item in events
            )
            assert any(
                item[0] == "two"
                and item[1]["kind"] == "stream"
                and "8" in item[1]["content"]["text"]
                for item in events
            )
        finally:
            await worker.close()

    asyncio.run(scenario())
