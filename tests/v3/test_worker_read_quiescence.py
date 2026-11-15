"""A read already inside a project worker when it quiesces is a reconnect.

Retirement releases the project's ownership while forwarded reads can still be
running in the worker. Their next SQL transaction is fenced with ``LeaseLost``.
That read is safe to repeat, so it answers the coded 503 the GUI and terminal
client already treat as "reconnecting", never a 500. A saved mutation keeps its
existing outcome: it is never answered as something to send again.
"""

import asyncio
from types import SimpleNamespace

import httpx
from keyring.backends.null import Keyring
import pytest

from nebula.v3 import execution_kernel
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import execution_context
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

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
