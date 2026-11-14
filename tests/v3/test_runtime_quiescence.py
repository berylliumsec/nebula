"""Used idle projects release ownership only after verified controller cleanup."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.exc import PendingRollbackError

from nebula.v3.async_utils import ServiceCleanupError
from nebula.v3.database import DeploymentHostRow, ProjectOwnershipRow
from nebula.v3.domain import ChatGoal, ChatSchedule, ChatTurn, utc_now
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_ipc import RuntimeIpcError, RuntimeRpcClient
from nebula.v3.runtime_operations import DeploymentOperations
from nebula.v3.runtime_processes import RuntimeProcessHost
from tests.v3.test_execution_worker import runtime as runtime, until
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


PRIVATE = "synthetic-private-statement"
LABEL = 'Project "Synthetic project" (project)'
SERVICE_FAILURE = (
    "1 Nebula Core service cleanup operation(s) failed: "
    "chat.follow-ups (PendingRollbackError)"
)


async def ready(
    runtime, *, fail_cleanup=False, change_pause=False, fail_component=False
):
    @asynccontextmanager
    async def application(context, lease):
        app = FastAPI()
        chat = SimpleNamespace(
            _active_provider_turns={}, _naming_tasks=set(), _precompactions={}
        )

        async def tick():
            pass

        app.state.runtime_handoff = SimpleNamespace(
            chat=chat, handles=lambda: {}, restored_digest=None, tick=tick
        )
        try:
            yield app
        finally:
            if fail_cleanup:
                raise RuntimeError("Inert cleanup failure")
            if change_pause:
                await runtime.application_store.update(
                    ChatGoal, "paused-goal", {"status": "cancelled"}
                )
            context.restore_cleanup_completed = True
            if fail_component:
                # The kernel attempted every service stop and the worker's own
                # pools and clients closed; one service's stop still raised.
                raise ServiceCleanupError(
                    [("chat.follow-ups", PendingRollbackError(PRIVATE))]
                )

    runtime.execution.application_factory = application
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "ready")


async def prepare(runtime):
    status = await runtime.coordination.status()
    return await runtime.coordination.prepare_shutdown(
        expected_revision=status.revision
    )


async def host_shutdown(runtime, operation_id):
    """Run the host's real shutdown operation against the worker's IPC server."""

    async def rpc(runtime_id):
        assert runtime_id == runtime.lease.runtime_id
        return RuntimeRpcClient(
            "unix:///isolated/worker.sock",
            b"k" * 32,
            transport=httpx.ASGITransport(
                app=runtime.context.rpc.app, raise_app_exceptions=False
            ),
        )

    async def retire(runtime_id):
        runtime.retired.append(runtime_id)

    runtime.retired = getattr(runtime, "retired", [])
    host = SimpleNamespace(
        _launch=asyncio.Lock(),
        closed=False,
        lock=SimpleNamespace(descriptor=1),
        coordinator=runtime.coordination,
        rpc=rpc,
        retire=retire,
    )
    status = await runtime.coordination.status()
    return await DeploymentOperations(runtime.coordination).execute(
        operation_id=operation_id,
        actor_id="operator",
        action="shutdown",
        parameters={"expected_revision": status.revision},
        dispatch=lambda _action, values: RuntimeProcessHost.shutdown(host, **values),
    )


async def project_status(runtime):
    status = await runtime.coordination.status()
    return status, next(
        item for item in status.projects if item.project_id == "project"
    )


async def test_used_idle_project_quiesces_preserving_paused_work_and_fence(runtime):
    await runtime.store.create(
        ChatTurn(
            id="completed",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            status="complete",
            request_snapshot={"recovery": {"required": True}},
        )
    )
    goal = await runtime.store.create(
        ChatGoal(
            id="paused-goal",
            engagement_id="project",
            session_id="session",
            objective="Preserve",
            completion_criteria=["Bounded"],
            status="paused",
            elapsed_seconds=12,
        )
    )
    schedule = await runtime.store.create(
        ChatSchedule(
            id="disabled",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            interval_seconds=3600,
            next_run_at=utc_now(),
            enabled=False,
        )
    )
    await ready(runtime)
    assert await prepare(runtime) == [runtime.lease]
    result = await runtime.execution.quiesce({})
    assert result["state"] == "closed" and result["cleanup_complete"]
    assert (await runtime.execution.quiesce({}))["state"] == "closed"
    async with runtime.coordination.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "project")
        assert (
            owner.runtime_id is None
            and owner.fencing_token == runtime.lease.fencing_token
        )
    assert (
        await runtime.store.get(ChatGoal, goal.id)
    ).model_dump() == goal.model_dump()
    assert (
        await runtime.store.get(ChatSchedule, schedule.id)
    ).model_dump() == schedule.model_dump()
    with pytest.raises(LeaseLost):
        async with runtime.coordination.coordination.fenced(runtime.lease):
            pass


@pytest.mark.parametrize("active", ["goal", "schedule"])
async def test_active_work_refuses_shutdown_before_controller_stop(runtime, active):
    if active == "goal":
        await runtime.store.create(
            ChatGoal(
                id="running-goal",
                engagement_id="project",
                session_id="session",
                objective="Continue",
                completion_criteria=["Bounded"],
                status="running",
            )
        )
    else:
        await runtime.store.create(
            ChatSchedule(
                id="enabled",
                engagement_id="project",
                session_id="session",
                provider_profile_id="provider",
                model="model",
                interval_seconds=3600,
                next_run_at=utc_now(),
                enabled=True,
            )
        )
    await ready(runtime)
    with pytest.raises(CoordinationConflict):
        await prepare(runtime)
    assert runtime.execution.state == "ready"
    async with runtime.coordination.database.session() as session:
        assert not (await session.get(DeploymentHostRow, "host")).shutdown_requested


async def test_failed_cleanup_keeps_owner_and_shutdown_hold(runtime):
    await ready(runtime, fail_cleanup=True)
    await prepare(runtime)
    with pytest.raises(RuntimeIpcError, match="cleanup is unverified"):
        await runtime.execution.quiesce({})
    async with runtime.coordination.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "project")).runtime_id == "old"
        assert (await session.get(DeploymentHostRow, "host")).shutdown_requested


async def test_changed_paused_work_refuses_quiesced_release(runtime):
    await runtime.store.create(
        ChatGoal(
            id="paused-goal",
            engagement_id="project",
            session_id="session",
            objective="Preserve",
            completion_criteria=["Bounded"],
            status="paused",
        )
    )
    await ready(runtime, change_pause=True)
    await prepare(runtime)
    with pytest.raises(CoordinationConflict, match="paused work changed"):
        await runtime.execution.quiesce({})
    async with runtime.coordination.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "project")).runtime_id == "old"


async def test_local_handle_refusal_keeps_controller_ready_for_resolution(runtime):
    await ready(runtime)
    runtime.execution.application.state.runtime_handoff.handles = lambda: {
        "terminal": "An open terminal still needs this worker"
    }
    await prepare(runtime)
    result = await runtime.execution.quiesce({})
    assert result["state"] == "ready" and result["quiesce_blockers"]
    assert not result["cleanup_complete"]
    await runtime.coordination.abort_shutdown()
    async with runtime.coordination.coordination.fenced(runtime.lease):
        pass


async def test_unverified_cleanup_reason_reaches_receipt_and_status(runtime):
    await ready(runtime, fail_cleanup=True)
    receipt = await host_shutdown(runtime, "stop")
    assert receipt["state"] == "uncertain"
    assert receipt["result"]["reason"] == (
        f"{LABEL}: Project cleanup is unverified (RuntimeError stopped the "
        "project application); its ownership is retained"
    )
    status, project = await project_status(runtime)
    assert status.health == "degraded"
    assert project.runtime_id == "old"
    assert project.blockers == [
        "Execution worker old failed: RuntimeError stopped the project "
        "application; its cleanup is unverified, so it keeps the project's ownership"
    ]
    async with runtime.coordination.database.session() as session:
        assert (await session.get(DeploymentHostRow, "host")).shutdown_requested


async def test_failed_service_stop_is_named_in_receipt_and_status(runtime):
    await ready(runtime, fail_component=True)
    first = await host_shutdown(runtime, "stop-1")
    assert first["state"] == "uncertain"
    assert first["result"]["reason"] == (
        f"{LABEL}: The project worker stopped in state failed "
        f"({SERVICE_FAILURE}); its ownership is retained"
    )
    assert PRIVATE not in str(first)
    status, project = await project_status(runtime)
    assert status.health == "degraded"
    assert project.blockers == [
        f"Execution worker old failed: {SERVICE_FAILURE}; its own cleanup "
        "finished, but it still owns the project"
    ]
    retry = await host_shutdown(runtime, "stop-2")
    assert retry["result"]["reason"] == (
        f"{LABEL}: The project worker failed ({SERVICE_FAILURE}) and cannot "
        "quiesce; its ownership is retained"
    )
    async with runtime.coordination.database.session() as session:
        host = await session.get(DeploymentHostRow, "host")
        assert host.detail == "Shutdown is blocked: " + retry["result"]["reason"]
        assert (await session.get(ProjectOwnershipRow, "project")).runtime_id == "old"
