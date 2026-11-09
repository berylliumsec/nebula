"""Host reconstruction retires only positively absent, unowned workers."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from nebula.v3.database import DeploymentHostRow, ProjectOwnershipRow, RuntimeProcessRow
from nebula.v3.process_identity import same_process
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
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


async def test_dead_promoted_api_restarts_last_api_version_without_moving_project(
    managed,
):
    await managed.stage("v1")
    await managed.stage("v2")
    execution = await managed.host.spawn("v1", "execution")
    coordinator = managed.coordinator
    lease = await coordinator.coordination.claim_project(
        "synthetic-project", execution.runtime_id
    )
    api = await managed.host.spawn("v2", "api")
    await coordinator.promote_api(
        api.runtime_id, expected_revision=(await coordinator.status()).revision
    )
    child = managed.host.children[api.runtime_id]
    child.kill()
    child.wait(timeout=5)
    result = await managed.host.reconcile_processes()
    assert result["retired_runtime_ids"] == [api.runtime_id]
    async with managed.database.session() as session:
        assert (await session.get(DeploymentHostRow, "host")).api_runtime_id is None
        owner = await session.get(ProjectOwnershipRow, lease.project_id)
        assert (owner.runtime_id, owner.fencing_token) == (
            lease.runtime_id,
            lease.fencing_token,
        )

    async def prepare(version):
        assert version == "v2"  # an API restart must not revert to the old desktop
        return await managed.host.spawn(version, "api")

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=coordinator,
            store=managed.store,
            host=managed.host,
            prepare_api=prepare,
            promote_api=coordinator.promote_api,
        ),
        default_version="v1",
    )
    await automatic.reconcile()
    assert (await coordinator.status()).api_version == "v2"
    assert same_process(execution.pid, execution.process_birth)


@pytest.mark.parametrize("uncertainty", ["live", "namespace"])
async def test_api_recovery_retains_unreachable_or_uncomparable_process(
    managed, uncertainty
):
    await managed.stage("v1")
    api = await managed.host.spawn("v1", "api")
    await managed.coordinator.promote_api(
        api.runtime_id, expected_revision=(await managed.coordinator.status()).revision
    )
    if uncertainty == "live":
        Path(api.endpoint[7:]).unlink()
    else:
        managed.host.children[api.runtime_id].kill()
        managed.host.children[api.runtime_id].wait(timeout=5)
        managed.host.launch_origin = "uncomparable-origin"
    result = await managed.host.reconcile_processes()
    assert not result["retired_runtime_ids"]
    async with managed.database.session() as session:
        assert (
            await session.get(DeploymentHostRow, "host")
        ).api_runtime_id == api.runtime_id
