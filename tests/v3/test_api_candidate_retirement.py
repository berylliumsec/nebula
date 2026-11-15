"""API roll-forward admission uses real SQL; process/RPC boundaries are inert."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    DeploymentHostRow,
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
    RuntimeInstanceRow,
    RuntimeResourceRow,
    RuntimeCommandRow,
    RuntimeCapacityReservationRow,
)
from nebula.v3.deployment_protocol import WorkerIdentity
from nebula.v3.runtime_coordination import CoordinationConflict, OperationRejected
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def inert_supervisor(host):
    coordinator, _ = host
    spawned, retired, admitted = [], [], []

    async def identity(runtime_id):
        async with coordinator.database.session() as session:
            row = await session.get(RuntimeProcessRow, runtime_id)
            return WorkerIdentity(
                runtime_id=row.runtime_id,
                version=row.version,
                role="api",
                pid=row.pid,
                process_birth=row.process_birth,
                endpoint=row.endpoint,
            )

    async def spawn(version, role):
        runtime_id = await coordinator.reserve_worker(
            version,
            role=role,
            endpoint="unix:///isolated/api-" + str(len(spawned)) + ".sock",
            credential_reference="synthetic",
        )
        assert runtime_id is not None
        async with coordinator.coordination._write() as session:
            row = await session.get(RuntimeProcessRow, runtime_id)
            row.pid, row.process_birth, row.state = (
                999999999,
                "inert-dead-incarnation-" + runtime_id,
                "ready",
            )
            runtime = await session.get(RuntimeInstanceRow, runtime_id)
            runtime.lease_expires_at_ms = 9999999999999
        spawned.append(runtime_id)
        return await identity(runtime_id)

    async def retire(runtime_id):
        proof = await coordinator.begin_retirement(runtime_id)
        await coordinator.retired(proof)
        retired.append(runtime_id)
        return proof

    async def rpc(runtime_id):
        async def call(name, parameters, **kwargs):
            assert name == "worker.identity"
            return (await identity(runtime_id)).model_dump(mode="json")

        async def close():
            pass

        return SimpleNamespace(call=call, close=close)

    async def admission(manifest, role):
        admitted.append((manifest.version, role))

    async def discard(endpoint):
        pass

    root = RuntimeSupervisor.__new__(RuntimeSupervisor)
    root._operations = asyncio.Lock()
    root._active = lambda: None
    root.coordinator = coordinator
    root.host = SimpleNamespace(spawn=spawn, retire=retire, rpc=rpc)
    root.proxy = SimpleNamespace(discard_endpoint=discard)
    root.worker_admission = admission
    return SimpleNamespace(
        root=root, spawned=spawned, retired=retired, admitted=admitted
    )


async def promote(root, identity):
    status = await root.coordinator.status()
    await root.promote_api(identity.runtime_id, expected_revision=status.revision)


async def test_sql_upgrade_rollback_rollforward_retires_only_draining_incarnations(
    inert_supervisor,
):
    f = inert_supervisor
    first = await f.root.prepare_api("v1")
    await promote(f.root, first)
    second = await f.root.prepare_api("v2")
    await promote(f.root, second)
    restored = await f.root.prepare_api("v1")
    await promote(f.root, restored)
    rolled_forward = await f.root.prepare_api("v2")
    await promote(f.root, rolled_forward)
    assert restored.runtime_id != first.runtime_id
    assert rolled_forward.runtime_id != second.runtime_id
    assert f.retired == [first.runtime_id, second.runtime_id]
    assert f.admitted == [("v1", "api"), ("v2", "api"), ("v1", "api"), ("v2", "api")]
    assert await f.root.prepare_api("v2") == rolled_forward
    assert len(f.spawned) == 4 and len(f.admitted) == 4
    async with f.root.coordinator.database.session() as session:
        receipts = list(
            await session.scalars(
                select(DeploymentReceiptRow).where(
                    DeploymentReceiptRow.action == "worker.retired"
                )
            )
        )
        assert {row.result["runtime_id"] for row in receipts} == {
            first.runtime_id,
            second.runtime_id,
        }
        assert all(row.result["pid"] == 999999999 for row in receipts)
        current = await session.get(DeploymentHostRow, "host")
        assert current.api_runtime_id == rolled_forward.runtime_id


@pytest.mark.parametrize(
    "guard",
    ["admission", "active_api", "owner", "resource", "command", "capacity", "identity"],
)
async def test_draining_candidate_retains_original_safety_holds(
    inert_supervisor, guard
):
    f = inert_supervisor
    root = f.root
    coordinator = root.coordinator
    old = await root.prepare_api("v2")
    await promote(root, old)
    current = await root.prepare_api("v1")
    await promote(root, current)
    async with coordinator.coordination._write() as session:
        if guard == "active_api":
            row = await session.get(DeploymentHostRow, "host")
            row.api_runtime_id = old.runtime_id
        elif guard == "owner":
            session.add(
                ProjectOwnershipRow(
                    project_id="one",
                    runtime_id=old.runtime_id,
                    fencing_token=1,
                    lease_expires_at_ms=0,
                    command_sequence=0,
                )
            )
        elif guard == "resource":
            session.add(
                RuntimeResourceRow(
                    id="held-resource",
                    project_id="one",
                    runtime_id=old.runtime_id,
                    fencing_token=1,
                    kind="vendor_reference",
                    state="open",
                    restoration="restorable",
                    detail="",
                    checkpoint={},
                    updated_at_ms=1,
                )
            )
        elif guard == "command":
            session.add(
                RuntimeCommandRow(
                    id="uncertain-command",
                    project_id="one",
                    runtime_id=old.runtime_id,
                    fencing_token=1,
                    sequence=1,
                    name="test",
                    state="uncertain",
                    actor_id="operator",
                    idempotency_key="test",
                    payload={},
                    payload_hash="a" * 64,
                    accepted_at_ms=1,
                )
            )
        elif guard == "capacity":
            session.add(
                RuntimeCapacityReservationRow(
                    id="held-capacity",
                    runtime_id=old.runtime_id,
                    project_id="one",
                    fencing_token=1,
                    request_key="test",
                    demands={},
                    state="held",
                    created_at_ms=1,
                )
            )
    if guard == "admission":

        async def reject(*args):
            raise OperationRejected("synthetic admission hold")

        root.worker_admission = reject
    if guard == "identity":

        async def mismatch(runtime_id):
            proof = await coordinator.begin_retirement(runtime_id)
            await coordinator.retired(
                proof.model_copy(update={"endpoint": "unix:///wrong-incarnation.sock"})
            )

        root.host.retire = mismatch
    expected_active = old.runtime_id if guard == "active_api" else current.runtime_id
    with pytest.raises(CoordinationConflict):
        await root.prepare_api("v2")
    assert f.spawned == [old.runtime_id, current.runtime_id] and not f.retired
    async with coordinator.database.session() as session:
        assert (
            await session.get(DeploymentHostRow, "host")
        ).api_runtime_id == expected_active
        assert (
            await session.get(RuntimeProcessRow, old.runtime_id)
        ).state == "draining"


@pytest.mark.parametrize(
    "state", ["reserved", "starting", "aborting", "unreachable", "ready"]
)
async def test_unresolved_same_version_candidate_is_not_retired_or_spawned(
    inert_supervisor, state
):
    f = inert_supervisor
    root = f.root
    old = await root.prepare_api("v2")
    await promote(root, old)
    current = await root.prepare_api("v1")
    await promote(root, current)
    async with root.coordinator.coordination._write() as session:
        row = await session.get(RuntimeProcessRow, old.runtime_id)
        row.state = state
        runtime = await session.get(RuntimeInstanceRow, old.runtime_id)
        runtime.lease_expires_at_ms = 0
    admitted = list(f.admitted)
    with pytest.raises(OperationRejected, match="earlier candidate"):
        await root.prepare_api("v2")
    assert not f.retired and f.spawned == [old.runtime_id, current.runtime_id]
    assert f.admitted == admitted
