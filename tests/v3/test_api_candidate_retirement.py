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
from nebula.v3.deployments import API_DRAIN_BOUND_MS, API_DRAIN_GRACE_MS
from nebula.v3.runtime_coordination import CoordinationConflict, OperationRejected
from nebula.v3.runtime_worker import WORKER_GRACEFUL_SHUTDOWN_SECONDS
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


def started(root):
    root.closed, root.started, root._shutdown_committed = False, True, False
    return root


async def age_drain(coordinator, runtime_id, milliseconds):
    async with coordinator.coordination._write() as session:
        row = await session.get(RuntimeProcessRow, runtime_id)
        row.updated_at_ms -= milliseconds


async def test_replaced_api_with_open_stream_is_retired_after_drain_grace(
    inert_supervisor,
):
    f = inert_supervisor
    root = started(f.root)
    coordinator = root.coordinator
    streams: dict[str, str] = {}

    async def discard(endpoint):
        # The supervisor proxy closes relayed streams so clients reconnect.
        if endpoint in streams:
            streams[endpoint] = "closed"

    root.proxy = SimpleNamespace(discard_endpoint=discard)
    old = await root.prepare_api("v1")
    await promote(root, old)
    streams[old.endpoint] = "open"
    current = await root.prepare_api("v2")
    await promote(root, current)

    # Within its grace the replaced API keeps its stream and health is ready.
    assert await root.retire_drained_apis() == []
    assert streams[old.endpoint] == "open" and not f.retired
    status = await coordinator.status()
    assert status.health == "ready"
    assert {row.runtime_id: row.state for row in status.processes}[
        old.runtime_id
    ] == "draining"

    await age_drain(coordinator, old.runtime_id, API_DRAIN_GRACE_MS + 1_000)
    assert await root.retire_drained_apis() == [old.runtime_id]
    assert f.retired == [old.runtime_id]
    assert streams[old.endpoint] == "closed"
    async with coordinator.database.session() as session:
        assert (await session.get(RuntimeProcessRow, old.runtime_id)).state == (
            "retired"
        )
        assert (
            await session.get(DeploymentHostRow, "host")
        ).api_runtime_id == current.runtime_id
    assert (await coordinator.status()).health == "ready"
    # The promoted API is never a drain candidate, however old its row is.
    await age_drain(coordinator, current.runtime_id, API_DRAIN_GRACE_MS * 10)
    assert await root.retire_drained_apis() == []


async def test_draining_api_does_not_degrade_health_within_its_bound(
    inert_supervisor,
):
    f = inert_supervisor
    root = f.root
    coordinator = root.coordinator
    old = await root.prepare_api("v1")
    await promote(root, old)
    await promote(root, await root.prepare_api("v2"))
    # A replaced API winding down may stop renewing its lease.
    async with coordinator.coordination._write() as session:
        runtime = await session.get(RuntimeInstanceRow, old.runtime_id)
        runtime.lease_expires_at_ms = 0
    assert (await coordinator.status()).health == "ready"
    # Past the bound, a replaced API still resident means retirement failed.
    await age_drain(coordinator, old.runtime_id, API_DRAIN_BOUND_MS + 1_000)
    assert (await coordinator.status()).health == "degraded"


async def test_refused_drain_retirement_keeps_the_api_for_a_later_pass(
    inert_supervisor,
):
    f = inert_supervisor
    root = started(f.root)
    coordinator = root.coordinator
    old = await root.prepare_api("v1")
    await promote(root, old)
    await promote(root, await root.prepare_api("v2"))
    await age_drain(coordinator, old.runtime_id, API_DRAIN_GRACE_MS + 1_000)

    async def slow(runtime_id):
        raise TimeoutError

    root.host.retire = slow
    assert await root.retire_drained_apis() == []
    async with coordinator.database.session() as session:
        assert (await session.get(RuntimeProcessRow, old.runtime_id)).state == (
            "draining"
        )


async def test_worker_server_exits_with_an_open_event_stream(tmp_path):
    """A replaced API's open SSE stream cannot hold its process past shutdown."""
    import time

    import httpx
    import uvicorn

    opened = asyncio.Event()

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b": open\n\n", "more_body": True}
        )
        opened.set()
        while True:
            await asyncio.sleep(0.1)
            await send(
                {"type": "http.response.body", "body": b": ping\n\n", "more_body": True}
            )

    socket = str(tmp_path / "api.sock")
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            uds=socket,
            lifespan="off",
            access_log=False,
            log_level="error",
            timeout_graceful_shutdown=WORKER_GRACEFUL_SHUTDOWN_SECONDS,
        )
    )
    serving = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=socket), base_url="http://api"
    ) as client:
        while not server.started:
            await asyncio.sleep(0.01)
        async with client.stream("GET", "/events") as response:
            await opened.wait()
            stopping = time.monotonic()
            server.should_exit = True
            await asyncio.wait_for(
                serving, timeout=WORKER_GRACEFUL_SHUTDOWN_SECONDS + 5
            )
            assert time.monotonic() - stopping < WORKER_GRACEFUL_SHUTDOWN_SECONDS + 5
            assert response.status_code == 200
