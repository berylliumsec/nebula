"""A read already inside a project worker when it quiesces is a reconnect.

Retirement releases the project's ownership while forwarded reads can still be
running in the worker. Their next SQL transaction is fenced with ``LeaseLost``.
That read is safe to repeat, so it answers the coded 503 the GUI and terminal
client already treat as "reconnecting", never a 500. A saved mutation keeps its
existing outcome: it is never answered as something to send again.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import secrets
from types import SimpleNamespace

from fastapi import WebSocket
import httpx
from keyring.backends.null import Keyring
from pydantic import SecretStr
import pytest
from sqlalchemy import update
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from nebula.v3 import execution_kernel
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database, ProjectOwnershipRow, RuntimeInstanceRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import execution_context
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_http import HttpUpstream, PrivateRuntimeHttp, RuntimeHttpProxy
from nebula.v3.runtime_ipc import RuntimeRpcServer
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_http import server

pytestmark = pytest.mark.anyio

TOKEN = "synthetic-operator"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
READ = "/api/v1/engagements/project"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def assert_reconnect(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "1"
    body = response.json()
    assert body["code"] == "project_runtime_unavailable", body
    assert body["retryable"] is True
    assert body["reason"] == "closing"


@pytest.fixture
async def worker(location, tmp_path, monkeypatch):
    settings = PoolSettings(5, 12, 3, 5)
    control = NebulaStore(Database(location, pool_settings=settings))
    runtime = NebulaStore(
        Database(
            location,
            bootstrap=False,
            execution_runtime_id="worker",
            pool_settings=settings,
        )
    )
    coordinator = DeploymentCoordinator(control.database)
    await control.create(Engagement(id="project", name="Synthetic project"))
    await coordinator.coordination.register_runtime("worker", "fixture")
    lease = await coordinator.coordination.claim_project("project", "worker")
    kernel = create_app(
        runtime,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        credential_store=CredentialStore(Keyring()),
        auth_token=TOKEN,
        execution_data_root=tmp_path,
        controller_root=tmp_path / "controllers",
        mission_checkpoint_path=tmp_path / "mission-checkpoints.db",
        enable_executable_missions=False,
    )
    # Hold one real route inside the worker, before its SQL runs.
    hold, entered, release, calls = [False], asyncio.Event(), asyncio.Event(), []
    original_get = runtime.get

    async def held_get(model, entity_id, *args, **kwargs):
        if model is Engagement and hold[0]:
            hold[0] = False
            calls.append(entity_id)
            entered.set()
            await release.wait()
        return await original_get(model, entity_id, *args, **kwargs)

    monkeypatch.setattr(runtime, "get", held_get)
    try:
        yield SimpleNamespace(
            control=control,
            runtime=runtime,
            coordinator=coordinator,
            lease=lease,
            kernel=kernel,
            hold=hold,
            entered=entered,
            release=release,
            calls=calls,
            tmp_path=tmp_path,
        )
    finally:
        release.set()
        for store in (runtime, control):
            await store.event_history.close()
            await store.database.dispose()


async def quiesce_ownership(coordinator, lease) -> None:
    """The SQL a retiring worker commits: hold admission, prove idle, release."""
    assert await coordinator.request_project_retirement(lease) is True
    checkpoint = await coordinator.quiesce_checkpoint(lease, local_blockers=[])
    await coordinator.release_quiesced_project(lease, checkpoint=checkpoint)


def kernel_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    )


async def test_kernel_read_fenced_by_quiescence_answers_the_reconnect_503(
    worker, monkeypatch
):
    recorded = []
    original = execution_kernel.record_diagnostic

    def record(level, feature, code, *args, **kwargs):
        recorded.append((level, code))
        return original(level, feature, code, *args, **kwargs)

    monkeypatch.setattr(execution_kernel, "record_diagnostic", record)
    async with kernel_client(worker.kernel) as client:
        assert (await client.get(READ, headers=AUTH)).status_code == 200
        worker.hold[0] = True
        # ProjectExecution.app binds every forwarded read to the worker's grant.
        with execution_context(worker.lease):
            reading = asyncio.create_task(client.get(READ, headers=AUTH))
        await asyncio.wait_for(worker.entered.wait(), 20)
        await quiesce_ownership(worker.coordinator, worker.lease)
        worker.release.set()
        response = await asyncio.wait_for(reading, 30)
    assert_reconnect(response)
    assert ("warning", "runtime.project.read_fenced") in recorded
    assert not [code for _, code in recorded if code.endswith("unhandled_exception")]


async def test_kernel_mutation_fenced_by_quiescence_is_never_offered_for_resending(
    worker,
):
    async with kernel_client(worker.kernel) as client:
        worker.hold[0] = True
        with execution_context(worker.lease):
            patching = asyncio.create_task(
                client.patch(
                    READ, headers=AUTH, json={"changes": {"name": "Must not apply"}}
                )
            )
        await asyncio.wait_for(worker.entered.wait(), 20)
        await quiesce_ownership(worker.coordinator, worker.lease)
        worker.release.set()
        response = await asyncio.wait_for(patching, 30)
    # Unchanged: a mutation is not a retryable read. DurableHttpRunner cannot
    # save this answer under a lost lease and leaves the command to reconcile.
    assert response.status_code == 500, response.text
    assert response.json()["retryable"] is False
    assert "retry-after" not in response.headers
    assert (await worker.control.get(Engagement, "project")).name == "Synthetic project"


def handoff_stub():
    async def tick():
        return None

    return SimpleNamespace(
        restored_digest=None,
        tick=tick,
        handles=lambda: {},
        chat=SimpleNamespace(
            _active_provider_turns={}, _naming_tasks=set(), _precompactions={}
        ),
    )


@pytest.fixture
async def execution(worker):
    context = SimpleNamespace(
        bootstrap=SimpleNamespace(
            identity=SimpleNamespace(runtime_id="worker"),
            data_root=str(worker.tmp_path),
            operator_token=SecretStr(TOKEN),
        ),
        coordinator=worker.coordinator,
        rpc=RuntimeRpcServer(b"k" * 32),
    )

    @asynccontextmanager
    async def application(context, _grant):
        worker.kernel.state.runtime_handoff = handoff_stub()
        try:
            yield worker.kernel
        finally:
            context.restore_cleanup_completed = True

    project = ProjectExecution(context, application_factory=application)
    try:
        await project.attach(asdict(worker.lease))
        await until(lambda: project.state == "ready")
        yield project
    finally:
        worker.release.set()
        await project.close()


async def test_read_in_flight_when_the_worker_quiesces_answers_the_reconnect_503(
    worker, execution
):
    async with kernel_client(execution.app) as client:
        assert (await client.get(READ, headers=AUTH)).status_code == 200
        worker.hold[0] = True
        reading = asyncio.create_task(client.get(READ, headers=AUTH))
        await asyncio.wait_for(worker.entered.wait(), 20)
        assert await worker.coordinator.request_project_retirement(worker.lease)
        status = await execution.quiesce({})
        assert status["state"] == "closed"
        worker.release.set()
        response = await asyncio.wait_for(reading, 30)
        assert_reconnect(response)
        # Later reads reaching this worker are refused at its gate the same way.
        assert_reconnect(await client.get(READ, headers=AUTH))


async def test_read_admitted_as_the_worker_stops_never_reaches_a_closed_app(
    worker, execution, monkeypatch
):
    coordination = worker.coordinator.coordination
    original_fenced = coordination.fenced
    armed, gate_entered, gate_release = [False], asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def fenced(lease):
        if armed[0]:
            armed[0] = False
            gate_entered.set()
            await gate_release.wait()
        async with original_fenced(lease):
            yield

    monkeypatch.setattr(coordination, "fenced", fenced)
    async with kernel_client(execution.app) as client:
        armed[0] = True
        reading = asyncio.create_task(client.get(READ, headers=AUTH))
        await asyncio.wait_for(gate_entered.wait(), 20)
        # The worker stops (close, quiescence or lease loss) while this read
        # is between its admission check and its dispatch.
        execution._stop.set()
        await until(lambda: execution.application is None)
        gate_release.set()
        response = await asyncio.wait_for(reading, 30)
    assert_reconnect(response)


async def test_proxied_read_in_flight_at_quiescence_reaches_the_viewer_as_503(
    worker, execution
):
    key = secrets.token_bytes(32)
    async with server(PrivateRuntimeHttp(execution.app, key)) as endpoint:

        async def select(_scope):
            return HttpUpstream(endpoint, key)

        proxy = RuntimeHttpProxy(select, project_reads=True)
        try:
            async with kernel_client(proxy) as viewer:
                assert (await viewer.get(READ, headers=AUTH)).status_code == 200
                worker.hold[0] = True
                reading = asyncio.create_task(viewer.get(READ, headers=AUTH))
                await asyncio.wait_for(worker.entered.wait(), 20)
                assert await worker.coordinator.request_project_retirement(worker.lease)
                assert (await execution.quiesce({}))["state"] == "closed"
                worker.release.set()
                assert_reconnect(await asyncio.wait_for(reading, 30))
        finally:
            await proxy.close()


async def test_saved_mutation_cut_off_by_lease_loss_is_reconciled_not_rerun(
    worker, execution
):
    coordination = worker.coordinator.coordination
    delivery = RuntimeDelivery(
        coordination, RuntimeVault(worker.tmp_path / "runtime" / "payloads")
    )
    worker.hold[0] = True
    receipt = await delivery.accept(
        HttpIntent.capture(
            "PATCH",
            READ,
            b"",
            [(b"content-type", b"application/json")],
            json.dumps({"changes": {"name": "Applied at most once"}}).encode(),
        ),
        project_id="project",
        actor_id="operator",
        idempotency_key="saved-mutation",
    )
    await asyncio.wait_for(worker.entered.wait(), 20)
    # Another owner takes the project over while the mutation runs here.
    async with worker.control.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "project")
            .values(lease_expires_at_ms=0)
        )
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == "worker")
            .values(lease_expires_at_ms=0)
        )
    await coordination.register_runtime("replacement", "fixture")
    replacement = await coordination.claim_project("project", "replacement")
    worker.release.set()
    await until(lambda: execution.state == "fenced")
    page = await delivery.page("operator", receipt.command_id)
    # The old owner could not save any answer, least of all "read again".
    assert page.state == "running" and not page.frames
    # The new owner never runs it again: it needs reconciliation.
    assert await coordination.claim_next(replacement, lane="work") is None
    assert (await delivery.page("operator", receipt.command_id)).state == "uncertain"
    assert worker.calls == ["project"]
    assert (await worker.control.get(Engagement, "project")).name == "Synthetic project"


async def test_fenced_kernel_websocket_is_refused_or_closed_for_reconnect(worker):
    @worker.kernel.websocket("/api/v1/fixture-fenced/{phase}")
    async def fenced_socket(websocket: WebSocket, phase: str):
        if phase == "accepted":
            await websocket.accept()
        raise LeaseLost("execution task lost its project fencing token")

    async with server(worker.kernel) as endpoint:
        with pytest.raises(InvalidStatus) as refused:
            async with unix_connect(
                endpoint[7:], uri="ws://worker/api/v1/fixture-fenced/handshake"
            ):
                raise AssertionError("a fenced handshake was accepted")
        assert refused.value.response.status_code == 503
        assert refused.value.response.headers["Retry-After"] == "1"
        body = json.loads(refused.value.response.body)
        assert body["code"] == "project_runtime_unavailable"
        assert body["retryable"] is True
        async with unix_connect(
            endpoint[7:], uri="ws://worker/api/v1/fixture-fenced/accepted"
        ) as socket:
            with pytest.raises(ConnectionClosed) as closed:
                await socket.recv()
        assert closed.value.rcvd.code == 1012
