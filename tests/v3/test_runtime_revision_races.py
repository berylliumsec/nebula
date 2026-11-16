"""#1100: operator Cancel/Interrupt and runtime bookkeeping in each other's window.

Executions: Cancel lands while a finished program's output is persisted,
the output is persisted while Cancel is requested, and Cancel lands while
the execution starts. Automation: a LAN results post and the process's own
finalization. Notebooks: Interrupt on a queued cell while the run queue
reads the policy.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import inspect
from contextlib import asynccontextmanager

import pytest

from nebula.v3.automation_runtime import (
    AutomationProjectPolicy,
    ProcessIORequest,
    ProcessResultsRequest,
    RunCommandRequest,
)
from nebula.v3.domain import (
    AutomationApprovalPolicy,
    CommandExecution,
    CommandExecutionStatus,
    NotebookRun,
    OperatorExecution,
    OperatorExecutionStatus,
    utc_now,
)
from nebula.v3.executions import ExecutionStartRequest
from nebula.v3.sandbox import SandboxResult
from nebula.v3.storage import ConflictError
from tests.v3.test_automation_runtime import runtime
from tests.v3.test_executions import _fixture as _sync_fixture
from tests.v3.test_notebooks import _project

pytestmark = pytest.mark.anyio


async def _fixture(tmp_path):
    # The execution fixture's stub resolver is already async and the service
    # awaits it.
    return await _sync_fixture(tmp_path)


def _in_frame(name: str) -> bool:
    return any(frame.function == name for frame in inspect.stack(context=0))


async def _start(service, request, key):
    preview = await service.preflight(request)
    assert preview.allowed
    return await service.start(
        ExecutionStartRequest(
            **request.model_dump(),
            preview_token=preview.preview_token,
            preview_fingerprint=preview.preview_fingerprint,
            client_idempotency_key=key,
        )
    )


async def _settle(service, execution_id):
    task = service._tasks.get(execution_id)
    if task is not None:
        await asyncio.wait({task}, timeout=10)
    return await service.store.get(OperatorExecution, execution_id)


class GatedRunner:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.requests = []

    async def run_stream(self, request, *, input_bytes, on_chunk, container_name):
        self.requests.append(request)
        await on_chunk("stdout", b"result line\n")
        await self.release.wait()
        now = utc_now()
        return SandboxResult(
            command=request.command,
            image=request.image,
            runtime="stub",
            started_at=now,
            completed_at=now,
            duration_seconds=0,
            exit_code=0,
            stdout="",
            stderr="",
        )


async def test_cancel_while_output_is_persisted_keeps_the_output(tmp_path):
    store, _a, _e, _p, runner, service, request = await _fixture(tmp_path)
    await service.startup()
    reached = asyncio.Event()
    resume = asyncio.Event()
    original_tx = store.transaction

    @asynccontextmanager
    async def gated_transaction(*args, **kwargs):
        if not reached.is_set() and _in_frame("_persist_terminal"):
            reached.set()
            await resume.wait()
        async with original_tx(*args, **kwargs) as transaction:
            yield transaction

    store.transaction = gated_transaction
    try:
        execution = await _start(service, request, "race-a")
        await asyncio.wait_for(reached.wait(), 5)
        await service.cancel(execution.id)
        resume.set()
        final = await _settle(service, execution.id)
        assert final.manifest_artifact_id
        assert final.evidence_id
        assert final.status in {
            OperatorExecutionStatus.COMPLETED,
            OperatorExecutionStatus.FAILED,
        }
        assert len(runner.requests) == 1
    finally:
        await service.shutdown()


async def test_cancel_after_the_output_is_persisted_returns_the_outcome(tmp_path):
    store, _a, _e, _p, _r, service, request = await _fixture(tmp_path)
    release = asyncio.Event()
    service.tool_platform.runner = GatedRunner(release)
    await service.startup()
    in_cancel = asyncio.Event()
    resume = asyncio.Event()
    original_update = store.update

    async def gated_update(model, entity_id, changes, **kwargs):
        if changes.get("status") == OperatorExecutionStatus.CANCELLING:
            in_cancel.set()
            await resume.wait()
        return await original_update(model, entity_id, changes, **kwargs)

    store.update = gated_update
    try:
        execution = await _start(service, request, "race-b")
        for _ in range(200):
            if (
                await store.get(OperatorExecution, execution.id)
            ).status == OperatorExecutionStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        cancel_task = asyncio.create_task(service.cancel(execution.id))
        await asyncio.wait_for(in_cancel.wait(), 5)
        release.set()
        terminal = await _settle(service, execution.id)
        resume.set()
        cancelled = await cancel_task
        assert terminal.status == OperatorExecutionStatus.COMPLETED
        assert cancelled.status == OperatorExecutionStatus.COMPLETED
        assert terminal.evidence_id
    finally:
        await service.shutdown()


async def test_cancel_while_the_execution_starts_cancels_it(tmp_path):
    store, _a, _e, _p, runner, service, request = await _fixture(tmp_path)
    await service.startup()
    in_resolve = asyncio.Event()
    resume_resolve = asyncio.Event()
    original_resolve = service._resolve

    async def gated_resolve(*args, **kwargs):
        if _in_frame("_run_isolated") and not in_resolve.is_set():
            in_resolve.set()
            await resume_resolve.wait()
        return await original_resolve(*args, **kwargs)

    service._resolve = gated_resolve
    original_event = service._event
    cancelling_saved = asyncio.Event()
    resume_cancel = asyncio.Event()

    async def gated_event(execution, event_type, payload, **kwargs):
        if event_type == "execution.cancelling":
            cancelling_saved.set()
            await resume_cancel.wait()
        return await original_event(execution, event_type, payload, **kwargs)

    service._event = gated_event
    try:
        execution = await _start(service, request, "race-c")
        await asyncio.wait_for(in_resolve.wait(), 5)
        cancel_task = asyncio.create_task(service.cancel(execution.id))
        await asyncio.wait_for(cancelling_saved.wait(), 5)
        resume_resolve.set()
        for _ in range(200):
            current = await store.get(OperatorExecution, execution.id)
            if current.status not in {
                OperatorExecutionStatus.QUEUED,
                OperatorExecutionStatus.CANCELLING,
            }:
                break
            await asyncio.sleep(0.01)
        resume_cancel.set()
        await cancel_task
        final = await _settle(service, execution.id)
        assert final.status == OperatorExecutionStatus.CANCELLED
        assert "revision conflict" not in (final.error_detail or "")
        assert runner.requests == []
    finally:
        await service.shutdown()


async def _background_command(tmp_path):
    manager, store, _artifacts, engagement, _sessions = await runtime(tmp_path)
    manager.callback_origin = "http://10.0.0.8:8765"
    policy = await manager.project_policy(engagement.id)
    await manager.update_project_policy(
        engagement.id,
        approval_policy=AutomationApprovalPolicy.NEVER,
        network_enabled=True,
        runner_profile_id="runner",
        max_timeout_ms=30_000,
        expected_revision=policy.revision,
    )
    notified: list[str] = []
    manager.bind_process_terminal_observer(notified.append)
    started = await manager.run_command(
        engagement_id=engagement.id,
        owner_kind="chat",
        owner_id="session-1",
        request=RunCommandRequest(command="wait-forever", background=True),
        tool_call_id="tool-race",
        chat_session_id="session-1",
        chat_turn_id="turn-1",
    )
    return manager, store, engagement, started, notified


def _gate_get(store, is_target):
    real_get = store.get
    read_done = asyncio.Event()
    release = asyncio.Event()

    async def gated_get(model, entity_id):
        result = await real_get(model, entity_id)
        if model is CommandExecution and is_target() and not read_done.is_set():
            read_done.set()
            await release.wait()
        return result

    store.get = gated_get
    return read_done, release


async def test_results_after_terminate_are_refused_by_outcome(tmp_path):
    manager, store, engagement, started, _notified = await _background_command(tmp_path)
    holder: dict[str, asyncio.Task] = {}
    read_done, release = _gate_get(
        store, lambda: asyncio.current_task() is holder.get("accept")
    )
    results = ProcessResultsRequest(status="complete", summary="posted", exit_code=0)
    holder["accept"] = asyncio.create_task(
        manager.accept_results(
            started.process_id, started.results_api_key or "", results
        )
    )
    await read_done.wait()
    terminated = await manager.process_io(
        started.process_id,
        ProcessIORequest(action="terminate"),
        engagement_id=engagement.id,
    )
    release.set()
    (outcome,) = await asyncio.gather(holder["accept"], return_exceptions=True)
    assert isinstance(outcome, ConflictError)
    assert "revision conflict" not in str(outcome)
    assert "cannot accept results" in str(outcome)
    row = await store.get(CommandExecution, terminated.execution_id)
    assert row.status == terminated.status


async def test_finalization_after_posted_results_keeps_both(tmp_path):
    manager, store, engagement, started, notified = await _background_command(tmp_path)
    managed = manager._processes[started.process_id]
    read_done, release = _gate_get(
        store, lambda: asyncio.current_task() is managed.final_task
    )
    await managed.backend.terminate()
    await read_done.wait()
    accepted = await manager.accept_results(
        started.process_id,
        started.results_api_key or "",
        ProcessResultsRequest(status="complete", summary="posted", exit_code=0),
    )
    release.set()
    (outcome,) = await asyncio.gather(managed.final_task, return_exceptions=True)
    assert not isinstance(outcome, BaseException), outcome
    row = await store.get(CommandExecution, accepted.id)
    assert row.status == CommandExecutionStatus.COMPLETED
    assert row.metadata["results_summary"] == "posted"
    assert row.stdout_artifact_id
    assert row.stderr_artifact_id
    # process_io reads the settled process without a revision error.
    await manager.process_io(
        started.process_id, ProcessIORequest(), engagement_id=engagement.id
    )


async def test_interrupt_of_a_queued_cell_survives_the_run_queue(tmp_path):
    store, project, notebooks = await _project(tmp_path)
    policy = AutomationProjectPolicy(
        engagement_id=project.id, execution_mode="host", host_access_acknowledged=True
    )
    holder: dict = {}

    class Runtime:
        async def project_policy(self, _):
            run_id = holder.get("run_id")
            if run_id and not holder.get("fired"):
                holder["fired"] = True
                # The operator interrupts the still-queued cell now.
                holder["interrupted"] = await notebooks.interrupt(
                    run_id, engagement_id=project.id
                )
            return policy

    notebooks.automation_runtime = Runtime()
    created = await notebooks.create(project.id, "queued.ipynb")
    saved = await notebooks.upsert_cell(
        project.id,
        "queued.ipynb",
        cell_id=None,
        source="print(1)",
        expected_revision=created["revision"],
        expected_source_hash=None,
    )
    release = asyncio.Event()
    executed: list[str] = []

    class Worker:
        async def execute(self, run_id, source):
            executed.append(run_id)
            return "complete"

    async def slow_worker(*_args):
        await release.wait()
        return Worker()

    notebooks._worker = slow_worker
    run = await notebooks.start_run(project.id, "queued.ipynb", saved["cell_id"])
    holder["run_id"] = run.id
    release.set()
    result = await notebooks.wait_run(run.id, engagement_id=project.id, timeout=5)
    saved_run = await store.get(NotebookRun, run.id)
    assert holder.get("fired")
    assert holder["interrupted"].status == "interrupted"
    assert saved_run.status == "interrupted"
    assert executed == []
    completed = [
        event
        for event in result["events"]
        if event["event_type"] == "notebook.completed"
    ]
    assert len(completed) == 1
