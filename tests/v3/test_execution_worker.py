"""Exact project grants and execution-owned delivery with isolated SQL only."""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import event, update

from nebula.v3 import execution_worker
from nebula.v3.database import Database, ProjectOwnershipRow, RuntimeInstanceRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import Engagement
from nebula.v3.execution_scope import current_execution_lease, execution_context
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.runtime_coordination import DispatchSnapshot, LeaseLost
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


async def test_idle_worker_makes_no_fenced_claims_and_still_picks_up_work(
    runtime, monkeypatch
):
    # Long lease and safety intervals: only the probe can act or deliver here.
    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 60.0)
    monkeypatch.setattr(execution_worker, "HEARTBEAT_SECONDS", 60.0)
    coordination = runtime.coordination.coordination
    original, claims, statements = coordination.claim_next, [], []

    async def counted(lease, *, lane=None):
        claims.append(lane)
        return await original(lease, lane=lane)

    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)

    monkeypatch.setattr(coordination, "claim_next", counted)
    engine = coordination.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await attach(runtime)
        await asyncio.sleep(0.3)
        claims.clear()
        statements.clear()
        await asyncio.sleep(2)
        idle_claims, idle_statements = len(claims), list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # Polling claimed every lane about every 0.1 s: ~30 fenced claims and
    # ~400 statements in two idle seconds. The probe reads ~6 rows twice a second.
    assert idle_claims == 0
    assert len(idle_statements) <= 60
    # The probe only reads: no row locks, writes or immediate transactions.
    for statement in idle_statements:
        assert not re.search(
            r"\b(UPDATE|INSERT|DELETE|IMMEDIATE)\b|FOR (UPDATE|SHARE)",
            statement.upper(),
        ), statement
    receipt = await accept(runtime, "after-idle", name="Picked up after idle")
    assert (await terminal(runtime, receipt)).state == "succeeded"


async def test_control_pickup_stays_fast_while_work_is_running(runtime, monkeypatch):
    # Idle probing and safety claims are pushed past terminal()'s budget, so the
    # control command can only be delivered by the active (running-work) cadence.
    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "IDLE_PROBE_SECONDS", 60.0)
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 60.0)
    work = await accept(runtime, "work", hold=True)
    await attach(runtime)
    await asyncio.wait_for(runtime.entered.wait(), 5)
    control = await accept(runtime, "control", lane="control", name="Control applied")
    assert (await terminal(runtime, control)).state == "succeeded"
    runtime.release.set()
    assert (await terminal(runtime, work)).state == "succeeded"


async def test_failing_probe_fails_open_with_one_diagnostic_and_backs_off(
    runtime, monkeypatch
):
    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 60.0)
    calls, recorded = [], []
    real_record = execution_worker.record_caught_exception

    async def broken(_lease):
        calls.append(1)
        raise RuntimeError("synthetic probe failure")

    def record(feature, code, *args, **kwargs):
        recorded.append(code)
        return real_record(feature, code, *args, **kwargs)

    monkeypatch.setattr(runtime.coordination.coordination, "dispatch_snapshot", broken)
    monkeypatch.setattr(execution_worker, "record_caught_exception", record)
    await attach(runtime)
    receipt = await accept(runtime, "despite-probe", name="Delivered without probe")
    assert (await terminal(runtime, receipt)).state == "succeeded"
    await asyncio.sleep(1.5)
    assert recorded.count("runtime.dispatch_probe.unavailable") == 1
    # Backoff (0.2, 0.4, 0.8, 1.6 s...) rather than ten failing probes a second.
    assert len(calls) <= 6


async def test_handoff_watcher_ticks_only_while_a_source_handoff_applies(
    runtime, monkeypatch
):
    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 60.0)
    ticks, active, phase = [], [False], [None]

    async def tick():
        ticks.append(1)
        return phase[0]

    @asynccontextmanager
    async def application(context, _grant):
        app = FastAPI()
        app.state.runtime_handoff = SimpleNamespace(restored_digest=None, tick=tick)
        try:
            yield app
        finally:
            context.restore_cleanup_completed = True

    coordination = runtime.coordination.coordination
    original = coordination.dispatch_snapshot

    async def snapshot(lease):
        return replace(await original(lease), handoff_active=active[0])

    monkeypatch.setattr(coordination, "dispatch_snapshot", snapshot)
    runtime.execution.application_factory = application
    await attach(runtime)
    await asyncio.sleep(1.5)
    # Only the startup tick: no source handoff applies, so no fenced tick per second.
    assert len(ticks) == 1
    active[0], phase[0] = True, "requested"
    await until(lambda: len(ticks) >= 2)
    active[0] = False
    # A returned phase keeps the one-second cadence without further hints.
    await until(lambda: len(ticks) >= 4)


async def test_stop_raised_with_a_probe_hint_never_starts_a_claim(runtime, monkeypatch):
    # Regression: on Python 3.11, wait_for could swallow the stop's cancellation
    # if a hint and the stop landed together, so a command ran after stop.
    coordination = runtime.coordination.coordination
    original = coordination.dispatch_snapshot
    armed, stopped = [False], []

    async def snapshot(lease):
        result = await original(lease)
        if armed[0] and "work" in result.lanes:
            armed[0] = False
            # close(), quiesce() or lease loss stopping the worker in the same
            # loop turn as the probe's hint.
            runtime.execution._stop.set()
            stopped.append(True)
        return result

    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "IDLE_PROBE_SECONDS", 0.05)
    monkeypatch.setattr(coordination, "dispatch_snapshot", snapshot)
    await attach(runtime)
    await asyncio.sleep(0.3)
    armed[0] = True
    await accept(runtime, "after-stop", hold=True)
    await until(lambda: stopped)
    await asyncio.wait_for(asyncio.shield(runtime.execution.task), 5)
    assert not runtime.entered.is_set()
    assert runtime.mutations == []


async def test_background_chat_turn_keeps_control_pickup_active(runtime, monkeypatch):
    # A chat turn runs after its start command returns. Its Stop and approvals
    # must keep the active cadence, not the idle one.
    monkeypatch.setattr(execution_worker, "ACTIVE_WINDOW_SECONDS", 0.0)
    monkeypatch.setattr(execution_worker, "IDLE_PROBE_SECONDS", 60.0)
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 60.0)
    turn = SimpleNamespace(done=False)

    @asynccontextmanager
    async def application(context, _grant):
        app = FastAPI()
        app.state.control_assistant_work = SimpleNamespace(
            chat=SimpleNamespace(_active_provider_turns={"turn": turn})
        )

        @app.post("/api/v1/fixture")
        async def mutate(request: Request):
            return {"name": (await request.json()).get("name")}

        try:
            yield app
        finally:
            context.restore_cleanup_completed = True

    runtime.execution.application_factory = application
    await attach(runtime)
    await asyncio.sleep(0.3)
    control = await accept(runtime, "stop", lane="control", name="Stop the turn")
    assert (await terminal(runtime, control)).state == "succeeded"


async def test_safety_claims_deliver_without_any_probe_hint(runtime, monkeypatch):
    monkeypatch.setattr(execution_worker, "SAFETY_CLAIM_SECONDS", 0.3)

    async def silent(_lease):
        return DispatchSnapshot(owned=True, lanes=frozenset(), handoff_active=False)

    monkeypatch.setattr(runtime.coordination.coordination, "dispatch_snapshot", silent)
    await attach(runtime)
    await asyncio.sleep(0.5)
    # One at a time: both fixture handlers rename the same project revision.
    work = await accept(runtime, "work-safety", name="Work by safety claim")
    assert (await terminal(runtime, work)).state == "succeeded"
    control = await accept(runtime, "control-safety", lane="control", name="Control")
    assert (await terminal(runtime, control)).state == "succeeded"
