"""Recovery resumes saved grants, never effects or a new ownership claim.

SQL cases step fresh supervisor views over real SQLite/PostgreSQL, signed ASGI
RPC and ProjectExecution with inert deferred controllers. The process case
additionally archives candidate source and retains a real execution worker.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
import httpx
from pydantic import SecretStr
import pytest

from nebula.v3.database import (
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeRecoveryRow,
)
from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.domain import ChatGoal, ChatSchedule, utc_now
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.process_identity import same_process
from nebula.v3.release_store import MANIFEST_NAME, ReleaseStore
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_host_services import HostSharedServices
from nebula.v3.runtime_ipc import RuntimeRpcClient, RuntimeRpcServer
from nebula.v3.runtime_project_handoff import restored_checkpoint
from nebula.v3.shared_memory_import import CompatibilityMemoryImport
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_deployment_lifecycle import manifest
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed, _TOKEN
from tests.v3.test_runtime_work_batches import batch as batch

pytestmark = pytest.mark.anyio
FEATURES = ["project.handoff/v1", "project.participants/v1"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def stepped_supervisor(coordinator, rpc):
    # These unit views deliberately omit unrelated shared services/background
    # loops. All recovery/attach/verify/activate methods themselves are real.
    root = RuntimeSupervisor.__new__(RuntimeSupervisor)
    root.coordinator = coordinator
    root.host = SimpleNamespace(rpc=rpc)
    root.started, root.closed, root._shutdown_committed = True, False, False
    root._handoff_serial, root._operations = asyncio.Lock(), asyncio.Lock()
    root._activated_handoffs = set()
    return root


@pytest.fixture
async def recovery(batch):
    coordinator = batch.coordinator
    await coordinator.stage(
        manifest("restore", features=FEATURES), Path("/synthetic/restore"), "a" * 64
    )
    for identity in ("source", "target"):
        await batch.coordination.register_runtime(identity, "restore")
        async with batch.database.session() as session:
            session.add(
                RuntimeProcessRow(
                    runtime_id=identity,
                    version="restore",
                    role="execution",
                    pid=0,
                    process_birth="synthetic-no-process",
                    endpoint="unix:///synthetic/" + identity,
                    credential_reference=uuid4().hex,
                    state="ready",
                    updated_at_ms=batch.clock[0],
                )
            )
    goal = await batch.store.create(
        ChatGoal(
            engagement_id="one",
            session_id="synthetic-session",
            objective="Preserve prior pause",
            completion_criteria=["Never resume automatically"],
            status="paused",
        )
    )
    schedule = await batch.store.create(
        ChatSchedule(
            engagement_id="one",
            session_id="synthetic-session",
            provider_profile_id="synthetic-provider",
            model="inert",
            interval_seconds=3600,
            next_run_at=utc_now(),
            enabled=False,
        )
    )
    old = await batch.coordination.claim_project("one", "source")
    secret = b"r" * 32
    rpc_server = RuntimeRpcServer(secret)
    context = SimpleNamespace(
        bootstrap=SimpleNamespace(
            identity=SimpleNamespace(runtime_id="target"),
            data_root=str(batch.host.data_root),
            operator_token=SecretStr("synthetic-recovery-token"),
        ),
        coordinator=coordinator,
        rpc=rpc_server,
    )
    activations, constructions, activation_requests = [], [], []

    @asynccontextmanager
    async def application(ctx, grant):
        constructions.append(grant)
        digest = await restored_checkpoint(coordinator, grant)
        saved = await coordinator.recovery_status(grant.project_id)
        app = FastAPI()

        async def tick():
            return "verified"

        async def activate():
            activations.append((grant, digest))

        app.state.runtime_handoff = SimpleNamespace(restored_digest=digest, tick=tick)
        app.state.runtime_recovery_operation_id = saved["operation_id"]
        app.state.runtime_activate = activate
        try:
            yield app
        finally:
            ctx.restore_cleanup_completed = True

    execution = ProjectExecution(context, application_factory=application)
    changes = {}
    status_operation = rpc_server.operations["execution.status/v1"]
    activate_operation = rpc_server.operations["execution.activate/v1"]

    async def status(payload):
        result = await status_operation(payload)
        result.update(changes)
        return result

    async def activate(payload):
        activation_requests.append(payload)
        return await activate_operation(payload)

    rpc_server.operations["execution.status/v1"] = status
    rpc_server.operations["execution.activate/v1"] = activate

    async def rpc(runtime_id):
        assert runtime_id == "target"
        return RuntimeRpcClient(
            "unix:///synthetic/target",
            secret,
            transport=httpx.ASGITransport(app=rpc_server.app),
        )

    async def begin():
        async with batch.database.session() as session:
            (await session.get(ProjectOwnershipRow, "one")).lease_expires_at_ms = 0
        return await coordinator.begin_recovery(
            "one",
            "target",
            expected_owner=old,
            expected_handoff_revision=None,
            operation_id="saved-recovery",
            actor_id="operator",
        )

    async def prepared(lease):
        await execution.attach(asdict(lease))
        await until(lambda: execution.state == "prepared")

    value = SimpleNamespace(
        **vars(batch),
        old=old,
        execution=execution,
        begin=begin,
        prepared=prepared,
        rpc=rpc,
        changes=changes,
        activations=activations,
        activation_requests=activation_requests,
        constructions=constructions,
        goal=goal,
        schedule=schedule,
    )
    value.new_supervisor = lambda: stepped_supervisor(coordinator, rpc)
    try:
        yield value
    finally:
        await execution.close()


@pytest.mark.parametrize("phase", ["begin", "prepared", "verified"])
async def test_restart_resumes_original_recovery_once_without_changing_paused_work(
    recovery, phase, monkeypatch
):
    value = recovery
    lease = await value.begin()
    if phase != "begin":
        await value.prepared(lease)
    if phase == "verified":
        await value.coordinator.verify_recovery(
            lease,
            operation_id="saved-recovery",
            restored_checkpoint_digest=value.execution.restored_checkpoint_digest,
        )
    claim = AsyncMock(
        side_effect=AssertionError("Recovery must not claim another grant")
    )
    monkeypatch.setattr(value.coordination, "claim_project", claim)
    replacement = value.new_supervisor()
    await replacement.advance_project_handoffs()
    assert value.execution.state == "ready"
    assert (
        value.constructions == [lease]
        and len(value.activations) == len(value.activation_requests) == 1
    )
    await replacement.advance_project_handoffs()
    await value.new_supervisor().advance_project_handoffs()
    assert len(value.activations) == len(value.activation_requests) == 1
    claim.assert_not_awaited()
    saved = await value.coordinator.recovery_status("one")
    assert (
        saved["operation_id"] == "saved-recovery"
        and saved["phase"] == "verified"
        and saved["lease"] == asdict(lease)
    )
    async with value.database.session() as session:
        handoff = await session.get(ProjectHandoffRow, "one")
        assert handoff.phase == "verified" and not handoff.admission_held
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.fencing_token == lease.fencing_token == value.old.fencing_token + 1
    assert await value.store.get(ChatGoal, value.goal.id) == value.goal
    assert await value.store.get(ChatSchedule, value.schedule.id) == value.schedule
    with pytest.raises(LeaseLost):
        async with value.coordination.fenced(value.old):
            pytest.fail("Expired source regained authority")


@pytest.mark.parametrize(
    "mismatch",
    [
        "saved-operation",
        "saved-target",
        "saved-fence",
        "peer-operation",
        "peer-runtime",
        "peer-fence",
        "peer-digest",
    ],
)
async def test_recovery_mismatch_stays_held_without_activation_or_claim(
    recovery, mismatch, monkeypatch
):
    value = recovery
    lease = await value.begin()
    await value.prepared(lease)
    async with value.database.session() as session:
        row = await session.get(RuntimeRecoveryRow, "saved-recovery")
        if mismatch == "saved-operation":
            handoff = await session.get(ProjectHandoffRow, "one")
            handoff.checkpoint = {
                **handoff.checkpoint,
                "operation_id": "other-operation",
            }
        elif mismatch == "saved-target":
            row.target_runtime_id = "source"
        elif mismatch == "saved-fence":
            row.target_fencing_token += 1
    if mismatch == "peer-operation":
        value.changes["recovery_operation_id"] = "other-operation"
    elif mismatch == "peer-runtime":
        value.changes["runtime_id"] = "source"
    elif mismatch == "peer-fence":
        value.changes["lease"] = {
            **asdict(lease),
            "fencing_token": lease.fencing_token + 1,
        }
    elif mismatch == "peer-digest":
        value.changes["restored_checkpoint_digest"] = "0" * 64
    claim = AsyncMock(side_effect=AssertionError("A mismatch must never claim"))
    monkeypatch.setattr(value.coordination, "claim_project", claim)
    await value.new_supervisor().advance_project_handoffs()
    assert (
        value.execution.state == "prepared"
        and not value.activations
        and not value.activation_requests
    )
    claim.assert_not_awaited()
    async with value.database.session() as session:
        handoff = await session.get(ProjectHandoffRow, "one")
        assert handoff.phase == "transferred" and handoff.admission_held
        assert (
            await session.get(RuntimeRecoveryRow, "saved-recovery")
        ).phase == "prepared"
        assert (
            await session.get(ProjectOwnershipRow, "one")
        ).fencing_token == lease.fencing_token


@pytest.mark.parametrize("phase", ["held", "verified"])
async def test_uncertain_effect_blocks_resume_until_explicit_outcome_without_replay(
    recovery, phase
):
    value = recovery
    if phase == "held":
        receipt = await value.coordination.submit(
            project_id="one",
            actor_id="operator",
            idempotency_key="unconfirmed",
            name="inert-effect",
            payload={},
        )
        await value.coordination.claim_next(value.old)
        lease = await value.begin()
    else:
        lease = await value.begin()
        await value.prepared(lease)
        await value.coordinator.verify_recovery(
            lease,
            operation_id="saved-recovery",
            restored_checkpoint_digest=value.execution.restored_checkpoint_digest,
        )
        receipt = await value.coordination.submit(
            project_id="one",
            actor_id="operator",
            idempotency_key="unconfirmed",
            name="inert-effect",
            payload={},
        )
        await value.coordination.claim_next(lease)
        await value.coordination.finish_command(
            lease,
            receipt.command_id,
            state="uncertain",
            result={"detail": "Synthetic lost effect response"},
        )
    root = value.new_supervisor()
    await root.advance_project_handoffs()
    assert not value.activation_requests and not value.activations
    if phase == "held":
        assert value.execution.state == "unassigned" and value.constructions == []
    async with value.database.session() as session:
        assert (
            await session.get(RuntimeCommandRow, receipt.command_id)
        ).state == "uncertain"
    await value.coordinator.resolve_uncertain(
        lease,
        receipt.command_id,
        actor_id="operator",
        result={"receipt": "Confirmed externally; never replay"},
        succeeded=True,
    )
    await root.advance_project_handoffs()
    assert value.execution.state == "ready" and len(value.activations) == 1
    async with value.database.session() as session:
        row = await session.get(RuntimeCommandRow, receipt.command_id)
        assert row.state == "succeeded" and row.result == {
            "receipt": "Confirmed externally; never replay"
        }


async def test_recovery_attach_expected_grant_cannot_fall_back_to_new_claim(
    recovery, monkeypatch
):
    lease = await recovery.begin()
    async with recovery.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        owner.runtime_id, owner.lease_expires_at_ms = None, 0
    claim = AsyncMock(side_effect=AssertionError("No replacement claim is allowed"))
    monkeypatch.setattr(recovery.coordination, "claim_project", claim)
    with pytest.raises(CoordinationConflict, match="original recovery grant"):
        await recovery.new_supervisor().attach_execution(
            "one", "target", allow_prepared=True, expected_lease=lease
        )
    claim.assert_not_awaited()
    assert recovery.execution.state == "unassigned" and not recovery.constructions


async def test_recover_management_and_host_progress_share_one_activation(recovery):
    lease = await recovery.begin()
    root = recovery.new_supervisor()
    request = dict(
        project_id="one",
        target_runtime_id="target",
        expected_owner=recovery.old,
        expected_handoff_revision=None,
        operation_id="saved-recovery",
        actor_id="operator",
    )
    first, second, _ = await asyncio.gather(
        root.recover_project(**request),
        root.recover_project(**request),
        root.advance_project_handoffs(),
    )
    assert first == second == lease
    assert recovery.constructions == [lease]
    assert len(recovery.activations) == len(recovery.activation_requests) == 1


_RETAINED_WORKER = r"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'python'))
import asyncio, json, os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from dataclasses import asdict
from fastapi import FastAPI
from nebula.v3.execution_worker import ProjectExecution
from nebula.v3.runtime_project_handoff import restored_checkpoint
from nebula.v3.runtime_processes import read_worker_bootstrap
from nebula.v3.runtime_worker import run_worker

@asynccontextmanager
async def project(context, lease):
    digest=await restored_checkpoint(context.coordinator,lease)
    saved=await context.coordinator.recovery_status(lease.project_id)
    app=FastAPI()
    async def tick():
        return 'verified'
    async def activate():
        root=Path(context.bootstrap.data_root)
        with (root/'recovery-activations.jsonl').open('a') as output:
            output.write(json.dumps({'pid':os.getpid(),'lease':asdict(lease),
                'operation_id':saved['operation_id'],'digest':digest})+'\n')
    app.state.runtime_handoff=SimpleNamespace(restored_digest=digest,tick=tick)
    app.state.runtime_recovery_operation_id=saved['operation_id']
    app.state.runtime_activate=activate
    try:
        yield app
    finally:
        context.restore_cleanup_completed=True

@asynccontextmanager
async def worker(context):
    execution=ProjectExecution(context,application_factory=project)
    try:
        yield execution
    finally:
        await execution.close()

bootstrap=read_worker_bootstrap(sys.stdin.buffer)
asyncio.run(run_worker(bootstrap,execution_factory=worker))
"""


async def archived_worker(managed, tmp_path):
    source = tmp_path / "recovery-source"
    package = Path(__file__).resolve().parents[2] / "src" / "nebula"
    shutil.copytree(
        package,
        source / "python" / "nebula",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (source / "nebula-core").write_text("#!" + sys.executable + "\n" + _RETAINED_WORKER)
    (source / "nebula-core").chmod(0o755)
    (source / "ui").mkdir()
    (source / "ui" / "index.html").write_text("<html>inert recovery worker</html>")
    metadata = json.loads((package / "v3" / "runtime-compatibility.json").read_text())[
        "manifest"
    ]
    candidate = ReleaseManifest(
        version="retained-recovery",
        schema_family=5,
        schema_min=5,
        schema_max=5,
        additive_migrations=True,
        migration_head=metadata["migration_head"],
        core_executable="nebula-core",
        ui_directory="ui",
        features=FEATURES,
        files={
            str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source.rglob("*"))
            if path.is_file()
        },
    )
    raw = candidate.model_dump_json().encode()
    (source / MANIFEST_NAME).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    retained = ReleaseStore(tmp_path / "recovery-retained")
    observed, installed = retained.install(source, expected_manifest_sha256=digest)
    await managed.coordinator.stage(observed, installed, digest)
    assert (
        candidate.files["python/nebula/v3/supervisor.py"]
        == hashlib.sha256((package / "v3" / "supervisor.py").read_bytes()).hexdigest()
    )
    shutil.rmtree(source)
    return retained, installed, digest


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Owned retained Linux process fixture"
)
async def test_retained_prepared_worker_survives_host_detach_and_new_host_activates_saved_recovery(
    managed, tmp_path
):
    retained, installed, manifest_digest = await archived_worker(managed, tmp_path)
    # The process-only managed fixture has not bootstrapped shared memory.
    # Complete the real initial import before any retained worker admission.
    await CompatibilityMemoryImport(managed.host).run()
    target = await managed.host.spawn("retained-recovery", "execution")
    coordinator = managed.coordinator
    await coordinator.coordination.register_runtime("expired-source", "lost-version")
    old = await coordinator.coordination.claim_project(
        "admitted-project", "expired-source"
    )
    async with managed.database.session() as session:
        (
            await session.get(ProjectOwnershipRow, "admitted-project")
        ).lease_expires_at_ms = 0
    lease = await coordinator.begin_recovery(
        "admitted-project",
        target.runtime_id,
        expected_owner=old,
        expected_handoff_revision=None,
        operation_id="retained-original-operation",
        actor_id="operator",
    )
    first = RuntimeSupervisor(
        managed.host,
        operator_token=_TOKEN,
        shared_services=HostSharedServices(managed.host),
    )
    # Deliberately stop before starting the host loop to represent the original
    # process dying after durable verification but before its activation RPC.
    first.started = True
    await first.attach_execution(
        lease.project_id, lease.runtime_id, allow_prepared=True, expected_lease=lease
    )
    peer = await managed.host.rpc(target.runtime_id)
    try:
        before = await peer.call("execution.status/v1", {}, timeout=5)
    finally:
        await peer.close()
    assert before["state"] == "prepared" and not before["activation_started"]
    await coordinator.verify_recovery(
        lease,
        operation_id="retained-original-operation",
        restored_checkpoint_digest=before["restored_checkpoint_digest"],
    )
    await first.close()
    assert same_process(target.pid, target.process_birth)
    replacement_host = managed.new_host()
    replacement = RuntimeSupervisor(
        replacement_host,
        operator_token=_TOKEN,
        shared_services=HostSharedServices(replacement_host),
    )
    try:
        await replacement.start()
        async with asyncio.timeout(15):
            while True:
                peer = await replacement_host.rpc(target.runtime_id)
                try:
                    status = await peer.call("execution.status/v1", {}, timeout=5)
                finally:
                    await peer.close()
                if status["state"] == "ready":
                    break
                await asyncio.sleep(0.02)
        assert (
            status["lease"] == asdict(lease)
            and status["recovery_operation_id"] == "retained-original-operation"
        )
        await replacement.advance_project_handoffs()
        records = [
            json.loads(line)
            for line in (managed.data / "recovery-activations.jsonl")
            .read_text()
            .splitlines()
        ]
        assert records == [
            {
                "pid": target.pid,
                "lease": asdict(lease),
                "operation_id": "retained-original-operation",
                "digest": before["restored_checkpoint_digest"],
            }
        ]
        assert same_process(target.pid, target.process_birth)
        assert (await coordinator.recovery_status(lease.project_id))[
            "phase"
        ] == "verified"
        assert (
            retained.verify(installed, expected_manifest_sha256=manifest_digest).version
            == "retained-recovery"
        )
    finally:
        await replacement.close()
        for path in retained.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)


async def test_recovery_refused_before_its_commit_is_a_rejection(recovery):
    from nebula.v3.runtime_coordination import OperationRejected

    value = recovery
    root = stepped_supervisor(value.coordinator, value.rpc)
    # The source owner is still live, so begin_recovery refuses in its
    # transaction and nothing changes.
    with pytest.raises(OperationRejected, match="still live"):
        await root.recover_project(
            "one",
            "target",
            expected_owner=value.old,
            expected_handoff_revision=None,
            operation_id="refused-recovery",
            actor_id="operator",
        )
    async with value.database.session() as session:
        assert await session.get(ProjectHandoffRow, "one") is None
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.runtime_id == "source"
