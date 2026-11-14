"""Project admission recovers from a transient worker-start failure on its own."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3 import runtime_auto_admission
from nebula.v3.database import (
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
)
from nebula.v3.domain import ChatSession
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import CoordinationConflict, ProjectLease
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_deployment_lifecycle import host as host, manifest
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _ready_worker(coordinator, runtime_id):
    await coordinator.coordination.register_runtime(runtime_id, "retry")
    async with coordinator.database.session() as session:
        session.add(
            RuntimeProcessRow(
                runtime_id=runtime_id,
                version="retry",
                role="execution",
                pid=0,
                process_birth="synthetic-no-process",
                endpoint="unix:///synthetic/" + runtime_id,
                credential_reference="synthetic-" + runtime_id,
                state="ready",
                updated_at_ms=0,
            )
        )


@pytest.fixture
async def admission(host, monkeypatch):
    coordinator, store = host
    await coordinator.stage(
        manifest("retry", features=["project.quiesce/v1"]),
        Path("/isolated/retry"),
        "e" * 64,
    )
    await coordinator.coordination.register_runtime("api", "retry", role="api")
    await coordinator.target_execution("retry")
    async with coordinator.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "api"
    value = SimpleNamespace(
        coordinator=coordinator,
        store=store,
        prepared=[],
        attached=[],
        prepare_failures=[0],
        attach_timeouts=set(),
        diagnostics=[],
    )

    async def prepare(_version):
        if value.prepare_failures[0]:
            value.prepare_failures[0] -= 1
            raise CoordinationConflict("synthetic transient start failure")
        runtime_id = f"retry-worker-{len(value.prepared)}"
        value.prepared.append(runtime_id)
        await _ready_worker(coordinator, runtime_id)
        return SimpleNamespace(runtime_id=runtime_id)

    async def attach(project_id, runtime_id):
        value.attached.append(runtime_id)
        async with coordinator.database.session() as session:
            process = await session.get(RuntimeProcessRow, runtime_id)
        if process is None or process.state == "retired":
            # The process has exited; its socket refuses the connection.
            raise httpx.ConnectError("synthetic connection refused")
        lease = await coordinator.coordination.claim_project(project_id, runtime_id)
        if runtime_id in value.attach_timeouts:
            # The grant committed, but the slow worker missed the status read.
            value.attach_timeouts.discard(runtime_id)
            raise httpx.ReadTimeout("synthetic slow status read")
        return lease

    def record(*args, **kwargs):
        value.diagnostics.append((args, kwargs))

    monkeypatch.setattr(runtime_auto_admission, "record_caught_exception", record)
    value.automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=coordinator,
            store=store,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="retry",
    )
    return value


async def _owner(value, project_id):
    async with value.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, project_id)
        return owner.runtime_id, owner.fencing_token


async def test_timed_out_attach_does_not_strand_the_next_readmission(admission):
    value = admission
    chat = await value.store.create(
        ChatSession(
            engagement_id="one",
            title="Only chat",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    value.attach_timeouts.add("retry-worker-0")
    await value.automatic.reconcile()
    # The attach timed out after its grant committed: the worker owns it.
    assert value.prepared == ["retry-worker-0"]
    assert await _owner(value, "one") == ("retry-worker-0", 1)
    await value.automatic.reconcile()
    assert value.prepared == ["retry-worker-0"]

    # Archiving the only chat retires that worker.
    chat = await value.store.update(
        ChatSession,
        chat.id,
        {"metadata": {"archived_at": "2026-11-14T00:00:00+00:00"}},
        expected_revision=chat.revision,
    )
    lease = ProjectLease("one", "retry-worker-0", 1)
    assert await value.coordinator.request_project_retirement(lease) is True
    checkpoint = await value.coordinator.quiesce_checkpoint(lease, local_blockers=[])
    await value.coordinator.release_quiesced_project(lease, checkpoint=checkpoint)
    async with value.coordinator.database.session() as session:
        (await session.get(RuntimeProcessRow, "retry-worker-0")).state = "retired"

    # Reopening the chat starts a fresh worker instead of retrying the dead one.
    await value.store.update(
        ChatSession,
        chat.id,
        {"metadata": {}},
        expected_revision=chat.revision,
    )
    await value.automatic.reconcile()
    assert value.prepared == ["retry-worker-0", "retry-worker-1"]
    assert "retry-worker-0" not in value.attached[1:]
    assert await _owner(value, "one") == ("retry-worker-1", 2)


async def test_transient_start_failure_retries_with_backoff_without_a_revision_change(
    admission, monkeypatch
):
    value = admission
    monkeypatch.setattr(
        runtime_auto_admission,
        "_ADMISSION_BACKOFF_SECONDS",
        (0.2, 0.2, 0.2),
        raising=False,
    )
    await value.store.create(
        ChatSession(
            engagement_id="one",
            title="Waiting chat",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    value.prepare_failures[0] = 1
    await value.automatic.reconcile()
    assert value.prepared == [] and await _owner(value, "one") == (None, 0)
    # Within the delay the project is not retried, and it does not hold up
    # another project that can be admitted now.
    await value.store.create(
        ChatSession(
            engagement_id="two",
            title="Other chat",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    await value.automatic.reconcile()
    assert value.prepared == ["retry-worker-0"]
    assert await _owner(value, "two") == ("retry-worker-0", 1)
    # Nothing changed the host or memory policy, yet the delay elapses and
    # the failed project is admitted.
    await asyncio.sleep(0.25)
    await value.automatic.reconcile()
    assert value.prepared == ["retry-worker-0", "retry-worker-1"]
    assert await _owner(value, "one") == ("retry-worker-1", 1)
    [(args, kwargs)] = value.diagnostics
    assert args[1] == "runtime.project_admission.waiting"
    assert "retries" in args[2]
    assert kwargs["metadata"] == {"entity_id": "one", "kind": "project", "attempt": 1}


async def test_persistent_start_failure_names_its_project_and_keeps_retrying(
    admission, monkeypatch
):
    value = admission
    monkeypatch.setattr(
        runtime_auto_admission, "_ADMISSION_BACKOFF_SECONDS", (0.01,), raising=False
    )
    persistent = getattr(runtime_auto_admission, "_ADMISSION_PERSISTENT", 5)
    await value.store.create(
        ChatSession(
            engagement_id="one",
            title="Stuck chat",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    value.prepare_failures[0] = persistent
    for _attempt in range(persistent):
        await value.automatic.reconcile()
        await asyncio.sleep(0.02)
    messages = [args[2] for args, _kwargs in value.diagnostics]
    attempts = [kwargs["metadata"]["attempt"] for _args, kwargs in value.diagnostics]
    assert attempts == list(range(1, persistent + 1))
    assert all("retries" in message for message in messages[:-1])
    assert "keeps failing" in messages[-1] and "memory budget" in messages[-1]
    # Once the cause clears, the next retry admits the project.
    await value.automatic.reconcile()
    assert await _owner(value, "one") == ("retry-worker-0", 1)


async def test_attach_keeps_polling_through_a_slow_status_read(admission):
    value = admission
    await _ready_worker(value.coordinator, "slow-worker")
    calls = []

    class Client:
        async def call(self, operation, payload, *, timeout):
            calls.append(operation)
            if operation == "execution.attach/v1":
                self.lease = payload
                return {}
            if not hasattr(self, "lease"):
                return {
                    "runtime_id": "slow-worker",
                    "lease": None,
                    "state": "unassigned",
                }
            if calls.count("execution.status/v1") == 2:
                raise httpx.ReadTimeout("synthetic slow status read")
            return {"runtime_id": "slow-worker", "lease": self.lease, "state": "ready"}

        async def close(self):
            return None

    async def rpc(_runtime_id):
        return Client()

    supervisor = RuntimeSupervisor.__new__(RuntimeSupervisor)
    supervisor._operations = asyncio.Lock()
    supervisor._active = lambda: None
    supervisor.host = SimpleNamespace(rpc=rpc)
    supervisor.coordinator = value.coordinator
    lease = await supervisor.attach_execution("one", "slow-worker", timeout=5)
    assert lease == ProjectLease("one", "slow-worker", 1)
    assert calls.count("execution.status/v1") == 3
    assert asdict(lease) == {
        "project_id": "one",
        "runtime_id": "slow-worker",
        "fencing_token": 1,
    }
