"""Exact project grants and execution-owned delivery with isolated SQL only."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import update

from nebula.v3.database import Database, ProjectOwnershipRow, RuntimeInstanceRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import current_execution_lease, execution_context
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_ipc import RuntimeIpcError, RuntimeRpcServer
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def runtime(location, tmp_path):
    settings = PoolSettings(5, 12, 3, 5)
    database = Database(location, pool_settings=settings)
    store = NebulaStore(database)
    application_store = NebulaStore(
        Database(
            location,
            bootstrap=False,
            execution_runtime_id="old",
            pool_settings=settings,
        )
    )
    coordination = DeploymentCoordinator(database)
    await store.create(Engagement(id="project", name="Synthetic project"))
    await store.create(Engagement(id="other", name="Other project"))
    await coordination.coordination.register_runtime("old", "fixture")
    lease = await coordination.coordination.claim_project("project", "old")
    context = SimpleNamespace(
        bootstrap=SimpleNamespace(
            identity=SimpleNamespace(runtime_id="old"),
            data_root=str(tmp_path),
            operator_token=SecretStr("synthetic-operator"),
        ),
        coordinator=coordination,
        rpc=RuntimeRpcServer(b"k" * 32),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    cleanup_release.set()
    seen, mutations = [], []

    @asynccontextmanager
    async def application(context, grant):
        seen.append(("startup", current_execution_lease()))

        async def inherited():
            seen.append(("child", current_execution_lease()))
            await asyncio.Event().wait()

        child = asyncio.create_task(inherited())
        app = FastAPI()

        @app.get("/api/v1/fixture")
        async def read():
            seen.append(("read", current_execution_lease()))
            return [
                project.id
                for project in await application_store.list_entities(Engagement)
            ]

        @app.post("/api/v1/fixture")
        async def mutate(request: Request):
            body = await request.json()
            mutations.append((body, current_execution_lease()))
            if body.get("hold"):
                entered.set()
                await release.wait()
            project = await application_store.get(Engagement, grant.project_id)
            project.name = body.get("name", "Changed")
            await application_store.update(
                Engagement,
                project.id,
                {"name": project.name},
                expected_revision=project.revision,
            )
            return {"name": project.name}

        try:
            yield app
        finally:
            seen.append(("cleanup", current_execution_lease()))
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            cleanup_entered.set()
            await cleanup_release.wait()

    execution = ProjectExecution(context, application_factory=application)
    delivery = RuntimeDelivery(
        coordination.coordination, RuntimeVault(tmp_path / "runtime" / "payloads")
    )
    try:
        yield SimpleNamespace(
            execution=execution,
            context=context,
            lease=lease,
            store=store,
            application_store=application_store,
            coordination=coordination,
            delivery=delivery,
            seen=seen,
            mutations=mutations,
            entered=entered,
            release=release,
            cleanup_entered=cleanup_entered,
            cleanup_release=cleanup_release,
        )
    finally:
        release.set()
        cleanup_release.set()
        await execution.close()
        for item in (application_store, store):
            await item.event_history.close()
            await item.database.dispose()


async def attach(runtime):
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "ready")


async def accept(runtime, key, *, lane="work", **body):
    return await runtime.delivery.accept(
        HttpIntent.capture(
            "POST",
            "/api/v1/fixture",
            b"",
            [(b"content-type", b"application/json")],
            json.dumps(body).encode(),
        ),
        project_id="project",
        actor_id="operator",
        idempotency_key=key,
        lane=lane,
    )


async def terminal(runtime, receipt):
    async with asyncio.timeout(8):
        while True:
            page = await runtime.delivery.page("operator", receipt.command_id)
            if page.state not in {"queued", "running"}:
                return page
            await asyncio.sleep(0.02)


async def test_unassigned_worker_starts_no_controllers_and_holds_reads(runtime):
    assert (await runtime.execution.status({}))["state"] == "unassigned"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.execution.app),
        base_url="http://fixture",
    ) as client:
        assert (await client.get("/api/v1/fixture")).status_code == 503
    assert runtime.seen == []


async def test_attachment_is_idempotent_and_every_task_inherits_exact_grant(runtime):
    await asyncio.gather(
        *(runtime.execution.attach(asdict(runtime.lease)) for _ in range(5))
    )
    await until(lambda: runtime.execution.state == "ready")
    await until(lambda: len(runtime.seen) >= 2)
    assert runtime.seen == [("startup", runtime.lease), ("child", runtime.lease)]
    for changed in (
        replace(runtime.lease, project_id="other"),
        replace(runtime.lease, fencing_token=2),
    ):
        with pytest.raises(RuntimeIpcError, match="cannot change"):
            await runtime.execution.attach(asdict(changed))
    await runtime.execution.close()
    assert runtime.seen[-1] == ("cleanup", runtime.lease)


@pytest.mark.parametrize(
    "change",
    [
        {"runtime_id": "another"},
        {"fencing_token": 2},
        {"fencing_token": True},
        {"unknown": "rejected"},
    ],
)
async def test_invalid_grants_never_start_controllers(runtime, change):
    with pytest.raises((RuntimeIpcError, LeaseLost)):
        await runtime.execution.attach({**asdict(runtime.lease), **change})
    assert runtime.seen == []
    assert runtime.execution.lease is None


async def test_durable_retry_executes_one_mutation_without_any_api_viewer(runtime):
    receipt = await accept(runtime, "same", name="Acknowledged")
    assert await accept(runtime, "same", name="Acknowledged") == receipt
    await attach(runtime)
    assert (await terminal(runtime, receipt)).state == "succeeded"
    assert runtime.mutations == [({"name": "Acknowledged"}, runtime.lease)]
    assert (await runtime.store.get(Engagement, "project")).name == "Acknowledged"
    replacement = RuntimeDelivery(
        runtime.coordination.coordination, RuntimeVault(runtime.delivery.vault.root)
    )
    assert (await replacement.page("operator", receipt.command_id)).frames[
        -1
    ].kind == "end"


async def test_control_delivers_while_work_lane_is_occupied(runtime):
    await attach(runtime)
    work = await accept(runtime, "work", hold=True)
    await asyncio.wait_for(runtime.entered.wait(), 5)
    control = await accept(runtime, "control", lane="control", name="Control applied")
    assert (await terminal(runtime, control)).state == "succeeded"
    assert (await runtime.delivery.page("operator", work.command_id)).state == "running"
    runtime.release.set()
    assert (await terminal(runtime, work)).state == "succeeded"


async def test_forwarded_reads_are_scoped_and_direct_mutations_are_rejected(runtime):
    await attach(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.execution.app),
        base_url="http://fixture",
    ) as client:
        response = await client.get("/api/v1/fixture")
        assert response.json() == ["project"]
        assert (
            await client.post("/api/v1/fixture", json={"name": "Bypass"})
        ).status_code == 405
    assert runtime.seen[-1] == ("read", runtime.lease)
    assert runtime.mutations == []


async def test_lost_grant_fences_old_tasks_and_cannot_be_rebound(runtime):
    await attach(runtime)
    async with runtime.coordination.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "project")
            .values(lease_expires_at_ms=0)
        )
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == "old")
            .values(lease_expires_at_ms=0)
        )
    await runtime.coordination.coordination.register_runtime("new", "replacement")
    replacement = await runtime.coordination.coordination.claim_project(
        "project", "new"
    )
    with execution_context(runtime.lease), pytest.raises(LeaseLost):
        await runtime.application_store.get(Engagement, "project")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.execution.app),
        base_url="http://fixture",
    ) as client:
        assert (await client.get("/api/v1/fixture")).status_code == 503
    assert runtime.execution.state == "fenced"
    with pytest.raises(RuntimeIpcError):
        await runtime.execution.attach(asdict(replacement))
    assert runtime.mutations == []


async def test_failed_startup_is_reported_and_same_grant_does_not_retry(runtime):
    calls = []

    @asynccontextmanager
    async def unavailable(context, lease):
        calls.append(lease)
        raise RuntimeIpcError("Synthetic unavailable host dependency")
        yield

    runtime.execution.application_factory = unavailable
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "failed")
    assert (await runtime.execution.attach(asdict(runtime.lease)))["state"] == "failed"
    assert calls == [runtime.lease]


async def test_cancelled_close_waits_for_controller_cleanup(runtime):
    await attach(runtime)
    runtime.cleanup_release.clear()
    closing = asyncio.create_task(runtime.execution.close())
    await asyncio.wait_for(runtime.cleanup_entered.wait(), 5)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    assert not closing.done()
    runtime.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await runtime.execution.close()
    assert runtime.execution.task.done()
