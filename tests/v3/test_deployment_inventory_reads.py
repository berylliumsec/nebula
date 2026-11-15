"""Deployment inventory reads for projects without an execution worker.

Since reads stopped admitting workers, a quiet project has no execution owner
on a supervisor Core. The deploy gate and the Butler pause/resume inventory
still read every project: these run the real gate and the public API worker
with real routing, which answers 503 for anything routed to a missing owner.
"""

from functools import partial
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import anyio
import httpx
import pytest
from sqlalchemy import func, or_, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import Database, ProjectOwnershipRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    AgentRun,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    ExecutionOrigin,
    ExecutionOriginKind,
    ExecutionRuntimeSnapshot,
    NotebookRun,
    OperatorExecution,
    OperatorExecutionStatus,
    RunnerIsolation,
    RunnerRuntime,
    RunStatus,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import RuntimeDelivery
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
TOKEN = "isolated-deployment-inventory-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
HEX = "0" * 64
GATE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "simulation"
    / "nebula_deploy_gate.py"
)
_spec = importlib.util.spec_from_file_location("deploy_gate_under_test", GATE_SOURCE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def core(location, tmp_path):
    database = Database(location, pool_settings=PoolSettings(5, 12, 5, 2))
    await database.initialize()
    database._bootstrap = False
    store = NebulaStore(database)
    coordination = RuntimeCoordination(database)
    routing = RuntimeRouting(
        coordination, tmp_path / "credentials", request_admission=True
    )
    delivery = RuntimeDelivery(coordination, RuntimeVault(tmp_path / "payloads"))
    global_api = RuntimeGlobalApi(database)
    # Real routing: a read for a project without a live owner raises the
    # runtime error the API worker answers with 503.
    app = create_api_worker(
        store,
        delivery,
        operator_token=TOKEN,
        project_for=routing.project_for,
        upstream_for=routing.upstream_for,
        global_api=global_api,
        command_start_timeout=0.03,
        poll_interval=0.01,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
    )
    try:
        yield SimpleNamespace(client=client, store=store, database=database)
    finally:
        await client.aclose()
        await app.state.viewer_proxy.close()
        await global_api.close()
        await store.event_history.close()
        await database.dispose()


class InProcessCore(gate.CoreClient):
    """The gate's own client, reading the in-process API worker over ASGI."""

    def __init__(self, client):
        self.origin, self.token, self.client = "https://synthetic.test", TOKEN, client
        self.paths = []

    def get(self, path, **parameters):
        self.paths.append(path)
        response = anyio.from_thread.run(
            partial(
                self.client.get,
                "/api/v1/" + path.lstrip("/"),
                params=parameters,
                headers=AUTH,
            )
        )
        if response.status_code != 200:
            raise HTTPError(path, response.status_code, response.text, {}, None)
        return response.json()


async def owner_rows(value):
    """Projects given a worker or asked to admit one; fencing rows are not."""
    async with value.database.session() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ProjectOwnershipRow)
            .where(
                or_(
                    ProjectOwnershipRow.runtime_id.is_not(None),
                    ProjectOwnershipRow.admission_requested.is_(True),
                )
            )
        )


async def busy_project(store):
    await store.create(Engagement(id="busy", name="Synthetic busy project"))
    butler = await store.create(
        ChatSession(
            engagement_id="busy",
            title="Butler",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
            metadata={"control_assistant": True},
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id="busy",
            session_id=butler.id,
            objective="Synthetic paused goal",
            completion_criteria=["Synthetic criterion"],
            status=ChatGoalStatus.PAUSED,
        )
    )
    run = await store.create(
        AgentRun(
            engagement_id="busy",
            objective="Synthetic queued mission",
            status=RunStatus.QUEUED,
        )
    )
    execution = await store.create(
        OperatorExecution(
            engagement_id="busy",
            operator_id="operator",
            origin=ExecutionOrigin(
                kind=ExecutionOriginKind.RERUN, execution_id="earlier"
            ),
            language="bash",
            source_sha256=HEX,
            source_artifact_id="source",
            runtime=ExecutionRuntimeSnapshot(
                language="bash",
                interpreter="/bin/bash",
                runtime_digest="sha256:" + HEX,
                image="image",
                runner_profile_id="synthetic-runner",
                runner_profile_revision=1,
                runner_runtime=RunnerRuntime.PODMAN,
                runner_isolation=RunnerIsolation.ROOTLESS,
                runner_executable="/nonexistent/podman",
                runner_platform="linux/amd64",
            ),
            preview_fingerprint=HEX,
            request_fingerprint=HEX,
            client_idempotency_key="synthetic-execution",
            status=OperatorExecutionStatus.RUNNING,
        )
    )
    notebook = await store.create(
        NotebookRun(
            engagement_id="busy",
            notebook_path="synthetic.ipynb",
            cell_id="cell",
            source_hash=HEX,
            execution_mode="docker",
            status="running",
        )
    )
    return SimpleNamespace(
        butler=butler, goal=goal, run=run, execution=execution, notebook=notebook
    )


async def test_inventory_reads_answer_from_sql_without_admitting_a_worker(core):
    saved = await busy_project(core.store)
    client = core.client

    async def read(path, **params):
        response = await client.get(path, params=params, headers=AUTH)
        assert response.status_code == 200, (path, response.text)
        return response.json()

    session = await read("/api/v1/chat/control-assistant/session", engagement_id="busy")
    assert session["id"] == saved.butler.id
    goal = await read(f"/api/v1/chat/sessions/{saved.butler.id}/goal")
    assert (goal["id"], goal["status"]) == (saved.goal.id, "paused")
    runs = await read("/api/v1/runs", engagement_id="busy", limit=1000)
    assert [(item["id"], item["status"]) for item in runs] == [(saved.run.id, "queued")]
    assert (await read(f"/api/v1/runs/{saved.run.id}"))["objective"] == (
        "Synthetic queued mission"
    )
    running = await read(
        "/api/v1/engagements/busy/executions", status="running", limit=1
    )
    assert [item["id"] for item in running] == [saved.execution.id]
    assert (
        await read("/api/v1/engagements/busy/executions", status="queued", limit=1)
        == []
    )
    notebooks = await read("/api/v1/engagements/busy/notebooks/runs/active")
    assert [item["id"] for item in notebooks] == [saved.notebook.id]
    # A read records no worker demand.
    assert await owner_rows(core) == 0

    # Run actions still belong to the project's execution owner.
    stop = await client.post(
        f"/api/v1/runs/{saved.run.id}/stop",
        headers={**AUTH, "Idempotency-Key": "stop-run"},
        json={},
    )
    assert stop.status_code == 202, stop.text
    bad_date = await client.get(
        "/api/v1/engagements/busy/executions",
        params={"date_from": "2026-11-14T00:00:00"},
        headers=AUTH,
    )
    assert bad_date.status_code == 422


async def test_deploy_gate_reads_projects_that_have_no_execution_worker(core):
    await core.store.create(Engagement(id="quiet", name="Synthetic quiet project"))
    reader = InProcessCore(core.client)

    assert await anyio.to_thread.run_sync(gate.blockers, reader) == []

    await busy_project(core.store)
    reasons = await anyio.to_thread.run_sync(gate.blockers, reader)
    assert sorted(reasons) == [
        "busy: 1 active notebook runs",
        "busy: active or resumable mission",
        "busy: running operator execution",
    ]
    assert "deployment/status" in reader.paths
    assert await owner_rows(core) == 0


async def test_deploy_gate_fails_closed_while_a_live_owner_does_not_answer(
    core, monkeypatch
):
    await core.store.create(Engagement(id="held", name="Synthetic held project"))
    # The project's owner keeps its grant, but its worker does not answer
    # (still retiring, or failed): its in-memory terminals are unknown.
    await RuntimeCoordination(core.database).register_runtime(
        "unreachable-worker", "synthetic"
    )
    async with core.database.session() as session:
        session.add(
            ProjectOwnershipRow(
                project_id="held",
                runtime_id="unreachable-worker",
                fencing_token=1,
                lease_expires_at_ms=2**62,
                command_sequence=0,
                admission_requested=False,
            )
        )
    monkeypatch.setattr(gate, "OWNER_READ_DELAY_SECONDS", 0)
    reader = InProcessCore(core.client)

    with pytest.raises(HTTPError) as failure:
        await anyio.to_thread.run_sync(gate.blockers, reader)
    assert failure.value.code == 503
    assert (
        reader.paths.count("engagements/held/workspace/reset-status")
        == gate.OWNER_READ_ATTEMPTS
    )
