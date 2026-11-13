"""Durable handoff and bounded-version decisions on SQLite and PostgreSQL."""

from pathlib import Path
import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from nebula.v3.database import (
    Database,
    RuntimeResourceRow,
    EntityRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
    RuntimeInstanceRow,
)
from sqlalchemy import update
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator, checkpoint_digest
from nebula.v3.deployment_protocol import (
    HandoffPhase,
    ReleaseManifest,
    ResourceSnapshot,
    WorkerIdentity,
)
from nebula.v3.domain import ChatSession, Engagement, Observation
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.storage import NebulaStore
from nebula.v3.process_identity import process_birth
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def manifest(version, **changes):
    values = dict(
        version=version,
        schema_family=5,
        schema_min=5,
        schema_max=5,
        additive_migrations=True,
        migration_head="0033_deployment_lifecycle",
        core_executable="nebula-core",
        ui_directory="ui",
        files={"nebula-core": "a" * 64, "ui/index.html": "b" * 64},
        restorable_resources=["vendor_reference", "approval_checkpoint"],
    )
    return ReleaseManifest(**{**values, **changes})


@pytest.fixture
async def host(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    store = NebulaStore(database)
    for identity in ("one", "two"):
        await store.create(Engagement(id=identity, name="Synthetic " + identity))
    coordinator = DeploymentCoordinator(database)
    coordinator.test_children = {}
    for version in ("v1", "v2", "v3"):
        await coordinator.stage(
            manifest(version), Path("/isolated/releases") / version, "a" * 64
        )
    try:
        yield coordinator, store
    finally:
        for child in coordinator.test_children.values():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        await database.dispose()


async def worker(host, version, role="execution"):
    coordinator, _store = host
    endpoint = "unix:///isolated/" + uuid4().hex + ".sock"
    identity = await coordinator.reserve_worker(
        version, role=role, endpoint=endpoint, credential_reference=str(uuid4())
    )
    if identity is None:
        return None
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    coordinator.test_children[identity] = child
    proof = WorkerIdentity(
        runtime_id=identity,
        version=version,
        role=role,
        pid=child.pid,
        process_birth=process_birth(child.pid),
        endpoint=endpoint,
    )
    await coordinator.bind_spawn(proof)
    await coordinator.worker_ready(proof)
    return identity


def stop_worker(host, runtime_id):
    child = host[0].test_children[runtime_id]
    child.terminate()
    child.wait(timeout=5)


async def pair(host):
    first, second = await worker(host, "v1"), await worker(host, "v2")
    lease = await host[0].coordination.claim_project("one", first)
    return first, second, lease


async def test_open_parent_chat_does_not_keep_child_project_worker(host):
    coordinator, store = host
    await store.create(
        Engagement(
            id="child",
            name="Synthetic child",
            parent_engagement_id="one",
        )
    )
    runtime_id = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("child", runtime_id)
    await store.create(
        ChatSession(
            id="parent-chat",
            engagement_id="one",
            title="Parent chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )

    assert await coordinator.request_project_retirement(lease) is True
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "child")
        assert owner is not None and owner.retirement_requested
        assert not owner.admission_requested


async def test_open_child_chat_keeps_its_exact_project_worker(host):
    coordinator, store = host
    await store.create(
        Engagement(
            id="child",
            name="Synthetic child",
            parent_engagement_id="one",
        )
    )
    runtime_id = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("child", runtime_id)
    await store.create(
        ChatSession(
            id="child-chat",
            engagement_id="child",
            title="Child chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )

    assert await coordinator.request_project_retirement(lease) is False
    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "child")
        assert owner is not None and not owner.retirement_requested


async def test_project_retirement_uses_its_own_hold_and_releases_exact_owner(host):
    coordinator, store = host
    runtime_id = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", runtime_id)
    await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="queued-non-chat-work",
        name="project.inspect",
        payload={},
    )

    assert await coordinator.request_project_retirement(lease) is True
    checkpoint = await coordinator.quiesce_checkpoint(lease, local_blockers=[])
    assert checkpoint["project_id"] == "one"
    await coordinator.release_quiesced_project(lease, checkpoint=checkpoint)

    async with coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner is not None
        assert owner.runtime_id is None
        assert owner.fencing_token == lease.fencing_token
        assert not owner.retirement_requested
        assert not owner.admission_requested


async def test_third_execution_version_stages_while_compatible_api_can_promote(host):
    coordinator, _store = host
    await worker(host, "v1")
    await worker(host, "v2")
    assert await worker(host, "v3") is None
    api = await worker(host, "v3", "api")
    current = await coordinator.status()
    await coordinator.promote_api(api, expected_revision=current.revision)
    result = await coordinator.status()
    assert result.resident_versions == ["v1", "v2"] and result.api_version == "v3"
    assert "v3" in result.staged_versions


async def test_pinned_project_remains_usable_while_another_transfers(host):
    coordinator, _store = host
    first, second, lease = await pair(host)
    other = await coordinator.coordination.claim_project("two", first)
    await coordinator.resource(
        lease,
        ResourceSnapshot(
            id="terminal",
            project_id="one",
            runtime_id=first,
            fencing_token=lease.fencing_token,
            kind="terminal",
            state="active",
            restoration="pinned",
            detail="An open terminal cannot be restored",
        ),
    )
    for project in ("one", "two"):
        await coordinator.request_handoff(project, second)
    assert (
        await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
        == HandoffPhase.FINISHING
    )
    receipt = await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="message",
        name="chat.submit",
        payload={"text": "continue on the old runtime"},
    )
    assert (
        await coordinator.coordination.claim_next(lease, lane="work")
    ).receipt == receipt
    assert (
        await coordinator.checkpoint(
            other, checkpoint={"conversation_ids": ["chat-two"]}, blockers=[]
        )
        == HandoffPhase.TRANSFERABLE
    )
    transferred = await coordinator.transfer("two")
    await coordinator.verified(
        transferred,
        restored_checkpoint_digest=checkpoint_digest(
            {"conversation_ids": ["chat-two"]}
        ),
    )
    statuses = {
        project.project_id: project for project in (await coordinator.status()).projects
    }
    assert statuses["one"].runtime_id == first and not statuses["one"].admission_held
    assert (
        statuses["two"].runtime_id == second
        and statuses["two"].phase == HandoffPhase.VERIFIED
    )
    assert any("one" in item for item in await coordinator.retirement_blockers(first))


async def test_control_lane_bypasses_uncertain_work_and_handoff_drain(host):
    coordinator, _store = host
    _first, second, lease = await pair(host)
    work = await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="external",
        name="effect",
        payload={"kind": "unknown"},
    )
    await coordinator.coordination.claim_next(lease)
    await coordinator.coordination.finish_command(
        lease, work.command_id, result={"outcome": "unknown"}, state="uncertain"
    )
    await coordinator.request_handoff("one", second)
    control = await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="stop",
        name="chat.stop",
        payload={"turn_id": "existing"},
        lane="control",
    )
    assert (
        await coordinator.coordination.claim_next(lease, lane="control")
    ).receipt == control
    await coordinator.coordination.finish_command(
        lease, control.command_id, result={"stopped": True}
    )
    assert await coordinator.coordination.claim_next(lease, lane="work") is None
    assert (
        await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
        == HandoffPhase.FINISHING
    )
    await coordinator.resolve_uncertain(
        lease,
        work.command_id,
        actor_id="operator",
        result={"inspected": True},
        succeeded=False,
    )
    assert (
        await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
        == HandoffPhase.TRANSFERABLE
    )


async def test_transfer_fences_source_and_retries_do_not_increment_owner_twice(host):
    coordinator, _store = host
    _first, second, lease = await pair(host)
    await coordinator.request_handoff("one", second)
    await coordinator.checkpoint(
        lease, checkpoint={"queued_messages": ["message"]}, blockers=[]
    )
    transferred = await coordinator.transfer("one")
    assert transferred.fencing_token == lease.fencing_token + 1
    assert await coordinator.transfer("one") == transferred
    with pytest.raises(LeaseLost):
        async with coordinator.coordination.fenced(lease):
            pass
    queued = await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="next",
        name="chat.submit",
        payload={},
    )
    assert await coordinator.coordination.claim_next(transferred, lane="work") is None
    await coordinator.verified(
        transferred,
        restored_checkpoint_digest=checkpoint_digest({"queued_messages": ["message"]}),
    )
    assert (
        await coordinator.coordination.claim_next(transferred, lane="work")
    ).receipt == queued


async def test_new_resource_invalidates_an_old_transferable_checkpoint(host):
    coordinator, _store = host
    first, second, lease = await pair(host)
    await coordinator.request_handoff("one", second)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    await coordinator.resource(
        lease,
        ResourceSnapshot(
            id="late",
            project_id="one",
            runtime_id=first,
            fencing_token=lease.fencing_token,
            kind="terminal",
            state="active",
            restoration="pinned",
            detail="New terminal",
        ),
    )
    with pytest.raises(CoordinationConflict, match="transferable"):
        await coordinator.transfer("one")


async def test_restorable_references_keep_identity_and_checkpoint_credentials_are_rejected(
    host,
):
    coordinator, _store = host
    first, second, lease = await pair(host)
    await coordinator.resource(
        lease,
        ResourceSnapshot(
            id="vendor",
            project_id="one",
            runtime_id=first,
            fencing_token=lease.fencing_token,
            kind="vendor_reference",
            state="restorable",
            restoration="reference",
            detail="Idle restorable vendor session",
            checkpoint={"session_ref": "vendor-session"},
        ),
    )
    await coordinator.request_handoff("one", second)
    with pytest.raises(ValueError, match="credentials"):
        await coordinator.checkpoint(
            lease, checkpoint={"auth_token": "not-recorded"}, blockers=[]
        )
    checkpoint = {
        "conversation_id": "same-chat",
        "approvals": ["same-approval"],
        "schedules": ["same-schedule"],
        "parent_child_refs": ["same-child"],
    }
    await coordinator.checkpoint(lease, checkpoint=checkpoint, blockers=[])
    target = await coordinator.transfer("one")
    with pytest.raises(CoordinationConflict, match="restore"):
        await coordinator.verified(target, restored_checkpoint_digest="wrong")
    await coordinator.verified(
        target, restored_checkpoint_digest=checkpoint_digest(checkpoint)
    )
    async with coordinator.database.session() as session:
        resource = await session.get(RuntimeResourceRow, "vendor")
        assert resource.runtime_id == second and resource.checkpoint == {
            "session_ref": "vendor-session"
        }


@pytest.mark.parametrize("phase", list(HandoffPhase))
async def test_supervisor_reconstruction_preserves_every_handoff_phase(host, phase):
    coordinator, _store = host
    _first, second, lease = await pair(host)
    await coordinator.request_handoff("one", second)
    if phase != HandoffPhase.REQUESTED:
        await coordinator.checkpoint(
            lease,
            checkpoint={"marker": "retained"},
            blockers=["finishing current turn"]
            if phase == HandoffPhase.FINISHING
            else [],
        )
    if phase in {HandoffPhase.TRANSFERRED, HandoffPhase.VERIFIED}:
        target = await coordinator.transfer("one")
        if phase == HandoffPhase.VERIFIED:
            await coordinator.verified(
                target,
                restored_checkpoint_digest=checkpoint_digest({"marker": "retained"}),
            )
    reconstructed = DeploymentCoordinator(coordinator.database)
    project = next(
        item
        for item in (await reconstructed.status()).projects
        if item.project_id == "one"
    )
    assert project.phase == phase


async def test_incompatible_protocol_and_destructive_overlap_are_rejected(host):
    coordinator, _store = host
    await worker(host, "v1")
    for item in (
        manifest("incompatible", execution_protocol_min=2, execution_protocol_max=2),
        manifest("destructive", additive_migrations=False),
    ):
        await coordinator.stage(
            item, Path("/isolated/releases") / item.version, "b" * 64
        )
        with pytest.raises(CoordinationConflict, match="incompatible"):
            await worker(host, item.version)


async def test_code_target_rollback_keeps_work_committed_after_promotion(host):
    coordinator, store = host
    await worker(host, "v1")
    await worker(host, "v2")
    await coordinator.target_execution("v1")
    await coordinator.target_execution("v2")
    await store.create(
        Observation(
            id="continuing-work",
            engagement_id="one",
            observation_type="note",
            title="Committed after promotion",
            body="keep this work",
        )
    )
    await coordinator.target_execution("v1")
    assert (await store.get(Observation, "continuing-work")).body == "keep this work"
    assert (await coordinator.status()).target_execution_version == "v1"


async def test_worker_retirement_requires_no_projects_resources_or_commands(host):
    coordinator, _store = host
    first, second, lease = await pair(host)
    with pytest.raises(CoordinationConflict, match="Project"):
        await coordinator.begin_retirement(first)
    await coordinator.request_handoff("one", second)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    transferred = await coordinator.transfer("one")
    await coordinator.verified(
        transferred, restored_checkpoint_digest=checkpoint_digest({})
    )
    proof = await coordinator.begin_retirement(first)
    stop_worker(host, first)
    await coordinator.retired(proof)
    assert await worker(host, "v3") is not None


async def test_retirement_never_frees_a_resident_slot_while_its_exact_process_is_alive(
    host,
):
    coordinator, _store = host
    endpoint = "unix:///isolated/" + uuid4().hex + ".sock"
    identity = await coordinator.reserve_worker(
        "v1", role="execution", endpoint=endpoint, credential_reference=str(uuid4())
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        proof = WorkerIdentity(
            runtime_id=identity,
            version="v1",
            role="execution",
            pid=child.pid,
            process_birth=process_birth(child.pid),
            endpoint=endpoint,
        )
        await coordinator.bind_spawn(proof)
        await coordinator.worker_ready(proof)
        await coordinator.begin_retirement(identity)
        with pytest.raises(CoordinationConflict, match="still alive"):
            await coordinator.retired(proof)
        child.terminate()
        child.wait(timeout=5)
        await coordinator.retired(proof)
        assert (await coordinator.status()).resident_versions == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


async def test_controls_after_checkpoint_require_a_fresh_boundary(host):
    coordinator, _store = host
    _first, second, lease = await pair(host)
    await coordinator.request_handoff("one", second)
    await coordinator.checkpoint(lease, checkpoint={"name": "before"}, blockers=[])
    control = await coordinator.coordination.submit(
        project_id="one",
        actor_id="operator",
        idempotency_key="control-after",
        name="chat.stop",
        payload={},
        lane="control",
    )
    await coordinator.coordination.claim_next(lease, lane="control")

    async def change(session):
        row = await session.get(EntityRow, "one")
        row.payload = {**row.payload, "name": "after"}

    await coordinator.coordination.finish_command(
        lease, control.command_id, result={"stopped": True}, apply=change
    )
    with pytest.raises(CoordinationConflict, match="transferable"):
        await coordinator.transfer("one")
    assert (
        await coordinator.checkpoint(lease, checkpoint={"name": "after"}, blockers=[])
        == HandoffPhase.TRANSFERABLE
    )


async def test_failed_starts_free_slots_and_late_ready_is_fenced(host):
    coordinator, _store = host
    first = await coordinator.reserve_worker(
        "v1",
        role="execution",
        endpoint="unix:///isolated/unstarted.sock",
        credential_reference=str(uuid4()),
    )
    second = await coordinator.reserve_worker(
        "v2",
        role="execution",
        endpoint="unix:///isolated/unstarted2.sock",
        credential_reference=str(uuid4()),
    )
    assert await worker(host, "v3") is None
    await coordinator.abort_start(first)
    await coordinator.abort_start(second)
    assert await worker(host, "v3") is not None
    proof = WorkerIdentity(
        runtime_id=first,
        version="v1",
        role="execution",
        endpoint="unix:///isolated/unstarted.sock",
        pid=os.getpid(),
        process_birth=process_birth(os.getpid()),
    )
    with pytest.raises(CoordinationConflict, match="cancelled"):
        await coordinator.bind_spawn(proof)
    with pytest.raises(CoordinationConflict):
        await coordinator.worker_ready(proof)


async def test_startup_abort_holds_its_slot_and_fences_readiness_before_cleanup(host):
    coordinator, _store = host
    first = await coordinator.reserve_worker(
        "v1",
        role="execution",
        endpoint="unix:///isolated/abort.sock",
        credential_reference=str(uuid4()),
    )
    await coordinator.reserve_worker(
        "v2",
        role="execution",
        endpoint="unix:///isolated/resident.sock",
        credential_reference=str(uuid4()),
    )
    assert await coordinator.begin_start_abort(first)
    assert (await coordinator.status()).resident_versions == ["v1", "v2"]
    assert (
        await coordinator.reserve_worker(
            "v3",
            role="execution",
            endpoint="unix:///isolated/third.sock",
            credential_reference=str(uuid4()),
        )
        is None
    )
    proof = WorkerIdentity(
        runtime_id=first,
        version="v1",
        role="execution",
        endpoint="unix:///isolated/abort.sock",
        pid=os.getpid(),
        process_birth=process_birth(os.getpid()),
    )
    with pytest.raises(CoordinationConflict):
        await coordinator.bind_spawn(proof)
    with pytest.raises(CoordinationConflict):
        await coordinator.worker_ready(proof)
    await coordinator.abort_start(first)
    assert (
        await coordinator.reserve_worker(
            "v3",
            role="execution",
            endpoint="unix:///isolated/third.sock",
            credential_reference=str(uuid4()),
        )
        is not None
    )


async def test_forged_birth_cannot_bind_or_retire_a_live_process(host):
    coordinator, _store = host
    endpoint = "unix:///isolated/birth.sock"
    identity = await coordinator.reserve_worker(
        "v1", role="execution", endpoint=endpoint, credential_reference=str(uuid4())
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    coordinator.test_children[identity] = child
    forged = WorkerIdentity(
        runtime_id=identity,
        version="v1",
        role="execution",
        endpoint=endpoint,
        pid=child.pid,
        process_birth="incorrect-birth",
    )
    with pytest.raises(CoordinationConflict, match="birth"):
        await coordinator.bind_spawn(forged)
    actual = forged.model_copy(update={"process_birth": process_birth(child.pid)})
    await coordinator.bind_spawn(actual)
    with pytest.raises(CoordinationConflict, match="exit"):
        await coordinator.abort_start(identity)
    with pytest.raises(CoordinationConflict, match="birth"):
        await coordinator.worker_ready(forged)
    await coordinator.worker_ready(actual)
    assert child.poll() is None


async def test_pinned_handoff_can_retarget_a_retired_candidate(host):
    coordinator, _store = host
    first, second, lease = await pair(host)
    await coordinator.resource(
        lease,
        ResourceSnapshot(
            id="pinned",
            project_id="one",
            runtime_id=first,
            fencing_token=lease.fencing_token,
            kind="terminal",
            state="active",
            restoration="pinned",
            detail="Open terminal",
        ),
    )
    await coordinator.request_handoff("one", second)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    proof = await coordinator.begin_retirement(second)
    stop_worker(host, second)
    await coordinator.retired(proof)
    third = await worker(host, "v3")
    await coordinator.request_handoff("one", third)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    project = next(
        item
        for item in (await coordinator.status()).projects
        if item.project_id == "one"
    )
    assert project.target_version == "v3" and not project.admission_held


async def test_retirement_serializes_with_admitted_project_claim(host, monkeypatch):
    coordinator, _store = host
    identity = await worker(host, "v1")
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.coordination._live_execution

    async def barrier(session, runtime_id, now):
        checked = await original(session, runtime_id, now)
        entered.set()
        await release.wait()
        return checked

    monkeypatch.setattr(coordinator.coordination, "_live_execution", barrier)
    claim = asyncio.create_task(coordinator.coordination.claim_project("one", identity))
    retirement = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        retirement = asyncio.create_task(coordinator.begin_retirement(identity))
        async with coordinator.database.session() as session:
            await session.get(EntityRow, "one")
        assert not retirement.done()
        release.set()
        assert (await asyncio.wait_for(claim, timeout=5)).runtime_id == identity
        with pytest.raises(CoordinationConflict, match="Project"):
            await asyncio.wait_for(retirement, timeout=5)
    finally:
        release.set()
        await asyncio.gather(
            claim, *([retirement] if retirement else []), return_exceptions=True
        )


async def test_waiting_claim_rechecks_runtime_expiry_after_process_lock(
    host, monkeypatch
):
    coordinator, _store = host
    if coordinator.database.engine.dialect.name != "postgresql":
        pytest.skip(
            "PostgreSQL row-lock wait; SQLite serializes writers before this boundary"
        )
    identity = await worker(host, "v1")
    entered = asyncio.Event()
    original = coordinator.coordination._live_execution

    async def entering(session, runtime_id, now):
        entered.set()
        return await original(session, runtime_id, now)

    monkeypatch.setattr(coordinator.coordination, "_live_execution", entering)
    async with coordinator.database.session() as lock:
        await lock.get(RuntimeProcessRow, identity, with_for_update=True)
        claim = asyncio.create_task(
            coordinator.coordination.claim_project("one", identity)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        await lock.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == identity)
            .values(lease_expires_at_ms=0)
        )
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(claim, timeout=5)
