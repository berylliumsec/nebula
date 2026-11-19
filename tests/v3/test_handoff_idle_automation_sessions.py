"""An idle Host automation session never pins its project at a handoff.

A source worker closes a project's idle Host sessions before its handoff
inventory; the owner's next command opens a fresh session on the new owner.
Anything still running, queued, held by a detached descendant, or in Docker
stays open and keeps pinning the project.
"""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.automation_runtime import (
    ProcessIORequest,
    RunCommandRequest,
    _ManagedSession,
)
from nebula.v3.domain import (
    AutomationProjectPolicy,
    AutomationSession,
    AutomationSessionStatus,
    CommandExecutionStatus,
)
from tests.v3.test_host_memory_runner import (
    _host_manager,
    _host_session,
    _limit,
    budgets as budgets,
    host_queue as host_queue,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def unmanaged_host(budgets, monkeypatch):
    """Host mode through the operator override: no protected runner."""
    from uuid import NAMESPACE_URL, uuid5

    store, controller = budgets
    await store.create(
        AutomationProjectPolicy(
            id=str(uuid5(NAMESPACE_URL, "nebula:automation-policy:project")),
            engagement_id="project",
            execution_mode="host",
            host_access_acknowledged=True,
        )
    )
    monkeypatch.setenv("NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS", "project")
    return store, controller


async def _run(manager, command, *, owner="chat-a", background=False):
    return await manager.run_command(
        engagement_id="project",
        owner_kind="chat",
        owner_id=owner,
        request=RunCommandRequest(command=command, background=background),
    )


async def test_idle_session_closes_and_the_next_command_opens_a_fresh_one(
    unmanaged_host, tmp_path
):
    store, controller = unmanaged_host
    manager = _host_manager(store, controller, tmp_path)
    first = await _run(manager, "echo handoff")
    assert first.status == CommandExecutionStatus.COMPLETED
    assert await manager.release_idle_sessions_for_handoff("other") == []
    assert await manager.release_idle_sessions_for_handoff("project") == [
        first.session_id
    ]
    assert manager._sessions == {} and manager._processes == {}
    closed = await store.get(AutomationSession, first.session_id)
    assert closed.status == AutomationSessionStatus.CLOSED
    # The finished command's result is durable after its session closed.
    kept = await manager.process_io(first.process_id, ProcessIORequest())
    assert kept.status == CommandExecutionStatus.COMPLETED
    assert kept.stdout_artifact_id == first.stdout_artifact_id
    second = await _run(manager, "true")
    assert second.session_id != first.session_id
    await manager.shutdown()


async def test_running_background_command_keeps_its_session(unmanaged_host, tmp_path):
    store, controller = unmanaged_host
    manager = _host_manager(store, controller, tmp_path)
    started = await _run(manager, "sleep 30", background=True)
    assert await manager.release_idle_sessions_for_handoff("project") == []
    assert started.session_id in manager._sessions
    await manager.process_io(started.process_id, ProcessIORequest(action="terminate"))
    assert await manager.release_idle_sessions_for_handoff("project") == [
        started.session_id
    ]
    await manager.shutdown()


async def test_protected_session_is_released_only_without_held_or_queued_commands(
    host_queue, tmp_path
):
    store, controller, runner = host_queue
    await _limit(controller, 1)
    manager = _host_manager(store, controller, tmp_path)
    # chat-a holds a command group: a detached descendant still runs in it.
    holder = await _host_session(manager, "chat-a")
    await holder.admit("a", execution_id="a", labels={})
    runner.groups["command-a"]["populated"] = True
    idle = await _host_session(manager, "chat-idle")
    # chat-b's command waits in the Host queue behind chat-a's slot: it is
    # in flight between session lookup and process registration.
    waiting = asyncio.create_task(_run(manager, "true", owner="chat-b"))
    try:
        await _assert_only_idle_released(manager, controller, idle, holder)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await manager.shutdown()


async def _assert_only_idle_released(manager, controller, idle, holder):
    for _ in range(100):
        await asyncio.sleep(0.02)
        queued = [
            job for job in await controller.service.list_jobs() if job.state == "queued"
        ]
        if queued:
            break
    assert queued, "chat-b never queued"
    sessions = {
        managed.entity.owner_id: session_id
        for session_id, managed in manager._sessions.items()
    }
    assert await manager.release_idle_sessions_for_handoff("project") == [
        sessions["chat-idle"]
    ]
    assert idle._closed and not holder._closed
    assert sessions["chat-a"] in manager._sessions
    assert sessions["chat-b"] in manager._sessions
    assert (await controller.service.get_job("command-a")).state == "running"


async def test_docker_session_is_never_released(unmanaged_host, tmp_path):
    from nebula.v3.automation_runtime import ContainerRuntimeSession

    store, controller = unmanaged_host
    manager = _host_manager(store, controller, tmp_path)
    finished = await _run(manager, "true")
    host = manager._sessions[finished.session_id]
    container = object.__new__(ContainerRuntimeSession)
    manager._sessions["container"] = _ManagedSession(
        entity=host.entity.model_copy(update={"id": "container"}),
        policy=host.policy,
        scope=host.scope,
        backend=container,
        workspace=host.workspace,
    )
    assert await manager.release_idle_sessions_for_handoff("project") == [
        finished.session_id
    ]
    assert "container" in manager._sessions
    manager._sessions.pop("container")
    await manager.shutdown()
