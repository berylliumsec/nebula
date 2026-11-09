"""Host reconstruction retires only positively absent, unowned workers."""

from pathlib import Path
from uuid import uuid4
import pytest
from nebula.v3.database import RuntimeProcessRow
from nebula.v3.process_identity import same_process
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_reconstruction_aborts_only_a_never_spawned_reservation(managed):
    await managed.stage("v1")
    coordinator = managed.host.coordinator
    identity = await coordinator.reserve_worker(
        "v1",
        role="execution",
        endpoint="unix:///unspawned-fixture.sock",
        credential_reference=str(uuid4()),
    )
    result = await managed.host.reconcile_processes()
    assert result["retired_runtime_ids"] == [identity]
    async with coordinator.database.session() as session:
        assert (await session.get(RuntimeProcessRow, identity)).state == "retired"


@pytest.mark.parametrize("owned", [False, True])
async def test_reconstruction_retires_dead_worker_only_after_ownership_clears(
    managed, owned
):
    await managed.stage("v1")
    identity = await managed.host.spawn("v1", "execution")
    coordinator = managed.host.coordinator
    if owned:
        await coordinator.coordination.claim_project(
            "synthetic-project", identity.runtime_id
        )
    child = managed.host.children[identity.runtime_id]
    child.terminate()
    child.wait(timeout=5)
    result = await managed.host.reconcile_processes()
    if owned:
        assert not result["retired_runtime_ids"]
        assert any(
            "Project synthetic-project" in item["detail"] for item in result["retained"]
        )
    else:
        assert result["retired_runtime_ids"] == [identity.runtime_id]
    async with coordinator.database.session() as session:
        assert (
            (await session.get(RuntimeProcessRow, identity.runtime_id)).state
            == "retired"
        ) is (not owned)


async def test_missing_control_socket_does_not_retire_live_process(managed):
    await managed.stage("v1")
    identity = await managed.host.spawn("v1", "execution")
    Path(identity.endpoint[7:]).unlink()
    result = await managed.host.reconcile_processes()
    assert not result["retired_runtime_ids"]
    assert any(item["state"] == "alive" for item in result["retained"])
    assert same_process(identity.pid, identity.process_birth)
