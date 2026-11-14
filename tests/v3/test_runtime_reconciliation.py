"""Host reconstruction retires only positively absent, unowned workers."""

import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import update
from nebula.v3.database import (
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
)
from nebula.v3.process_identity import process_origin, same_process
from nebula.v3 import runtime_processes
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_orphan_recovery import reconcile_dead_project
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


EARLIER_BOOT = "00000000-1111-4222-8333-444444444444"


def _rewrite_origin(managed, *, boot=None, namespace=None, remove_sockets=True):
    """Make the recorded origin look like another boot or PID namespace."""
    marker = managed.data / "runtime" / "process-origin.json"
    document = json.loads(marker.read_text())
    kind, recorded_boot, label, inode = document["process_origin"].split(":")
    document["process_origin"] = ":".join(
        (kind, boot or recorded_boot, label, namespace or inode)
    )
    if remove_sockets:
        shutil.rmtree(document["socket_root"])
        managed.host.socket_root = None  # the fixture must not remove it again
    marker.write_text(json.dumps(document))
    return marker, document


async def _kill(managed, *identities):
    for identity in identities:
        child = managed.host.children[identity.runtime_id]
        child.kill()
        child.wait(timeout=5)


async def _expire(managed, *runtime_ids, project_id=None):
    async with managed.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id.in_(runtime_ids))
            .values(lease_expires_at_ms=0)
        )
        if project_id is not None:
            await session.execute(
                update(ProjectOwnershipRow)
                .where(ProjectOwnershipRow.project_id == project_id)
                .values(lease_expires_at_ms=0)
            )


async def _born_in_earlier_boot(managed, *identities):
    """A real reboot leaves every recorded birth on the previous kernel boot."""
    async with managed.database.session() as session:
        for identity in identities:
            await session.execute(
                update(RuntimeProcessRow)
                .where(RuntimeProcessRow.runtime_id == identity.runtime_id)
                .values(
                    process_birth="linux:"
                    + EARLIER_BOOT
                    + ":"
                    + identity.process_birth.rsplit(":", 1)[1]
                )
            )


async def test_rebooted_host_replaces_its_origin_and_recovers_dead_owners(managed):
    await managed.stage("v1")
    coordinator = managed.coordinator
    api = await managed.host.spawn("v1", "api")
    execution = await managed.host.spawn("v1", "execution")
    await coordinator.promote_api(
        api.runtime_id, expected_revision=(await coordinator.status()).revision
    )
    lease = await coordinator.coordination.claim_project(
        "synthetic-project", execution.runtime_id
    )
    await _kill(managed, api, execution)
    await managed.host.close()
    # A reboot empties /tmp, and every recorded origin and birth names the
    # previous kernel boot; the host was down longer than the leases.
    marker, stale = _rewrite_origin(managed, boot=EARLIER_BOOT)
    await _born_in_earlier_boot(managed, api, execution)
    await _expire(
        managed, api.runtime_id, execution.runtime_id, project_id=lease.project_id
    )

    replacement = managed.new_host()
    await replacement.start()
    managed.host.socket_root = replacement.socket_root  # fixture cleanup
    current = json.loads(marker.read_text())
    assert replacement.launch_origin == current["process_origin"] == process_origin()
    assert current["replaced"] == {
        "reason": "previous_boot",
        "process_origin": stale["process_origin"],
        "socket_root": stale["socket_root"],
    }
    assert str(replacement.socket_root) == current["socket_root"]
    assert current["socket_root"] != stale["socket_root"]
    sockets = os.lstat(replacement.socket_root)
    assert stat.S_ISDIR(sockets.st_mode) and stat.S_IMODE(sockets.st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    observed = {
        worker.runtime_id: worker.process_state
        for worker in await replacement.rediscover()
    }
    assert observed == {api.runtime_id: "gone", execution.runtime_id: "gone"}
    result = await replacement.reconcile_processes()
    # The dead API is released; the owned execution worker waits for recovery.
    assert result["retired_runtime_ids"] == [api.runtime_id]
    async with managed.database.session() as session:
        assert (await session.get(DeploymentHostRow, "host")).api_runtime_id is None
    receipt = await reconcile_dead_project(
        coordinator, lease, launch_origin=replacement.launch_origin
    )
    assert receipt["effects_replayed"] is False
    assert (receipt["project_id"], receipt["runtime_id"]) == (
        lease.project_id,
        execution.runtime_id,
    )
    worker = await replacement.spawn("v1", "api")
    assert worker.endpoint.startswith("unix://" + str(replacement.socket_root) + "/")
    assert same_process(worker.pid, worker.process_birth)


@pytest.mark.parametrize("sockets", ["present", "missing"])
async def test_same_boot_namespace_change_keeps_refusing_recovery(managed, sockets):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    lease = await managed.coordinator.coordination.claim_project(
        "synthetic-project", execution.runtime_id
    )
    await _kill(managed, execution)
    await managed.host.close()
    await _expire(managed, execution.runtime_id, project_id=lease.project_id)
    marker, stale = _rewrite_origin(
        managed, namespace="4026599999", remove_sockets=sockets == "missing"
    )
    recorded = marker.read_bytes()
    replacement = managed.new_host()
    if sockets == "missing":
        with pytest.raises(CoordinationConflict, match="another process namespace"):
            await replacement.start()
        assert marker.read_bytes() == recorded
        assert replacement.socket_root is None
        return
    await replacement.start()
    assert replacement.launch_origin == stale["process_origin"] != process_origin()
    observed = await replacement.rediscover()
    assert [worker.process_state for worker in observed] == ["unknown"]
    result = await replacement.reconcile_processes()
    assert not result["retired_runtime_ids"]
    with pytest.raises(CoordinationConflict, match="process namespace changed"):
        await replacement.spawn("v1", "execution")
    with pytest.raises(CoordinationConflict, match="namespace cannot be verified"):
        await reconcile_dead_project(
            managed.coordinator, lease, launch_origin=replacement.launch_origin
        )
    assert marker.read_bytes() == recorded


async def test_earlier_boot_worker_with_live_heartbeat_lease_is_retained(managed):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    await _kill(managed, execution)
    await _born_in_earlier_boot(managed, execution)
    async with managed.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == execution.runtime_id)
            .values(lease_expires_at_ms=2**62)
        )
    # Another host sharing this database can still be renewing that lease.
    [observed] = await managed.host.rediscover()
    assert observed.process_state == "unknown"
    assert not (await managed.host.reconcile_processes())["retired_runtime_ids"]
    await _expire(managed, execution.runtime_id)
    [observed] = await managed.host.rediscover()
    assert observed.process_state == "gone"
    result = await managed.host.reconcile_processes()
    assert result["retired_runtime_ids"] == [execution.runtime_id]


async def test_missing_socket_directory_on_the_same_boot_retains_live_workers(
    managed,
):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    await managed.host.close()
    stale = managed.host.socket_root
    shutil.rmtree(stale)  # e.g. /tmp ageing while Core was stopped
    managed.host.socket_root = None  # the fixture must not remove it again
    replacement = managed.new_host()
    await replacement.start()
    managed.host.socket_root = replacement.socket_root  # fixture cleanup
    current = json.loads((managed.data / "runtime" / "process-origin.json").read_text())
    assert replacement.launch_origin == current["process_origin"] == process_origin()
    assert current["replaced"]["reason"] == "socket_root_missing"
    assert replacement.socket_root != stale
    # The live worker lost its socket path but is not presumed dead.
    [observed] = await replacement.rediscover()
    assert (observed.reachable, observed.process_state) == (False, "alive")
    assert not (await replacement.reconcile_processes())["retired_runtime_ids"]
    assert same_process(execution.pid, execution.process_birth)
    worker = await replacement.spawn("v1", "execution")
    assert worker.endpoint.startswith("unix://" + str(replacement.socket_root) + "/")


async def _renewing(managed, *identities):
    """Leases another host could still be renewing for these rows."""
    async with managed.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id.in_([item.runtime_id for item in identities]))
            .values(lease_expires_at_ms=2**62)
        )


async def test_boot_change_is_not_adopted_while_recorded_leases_are_renewed(
    managed, monkeypatch
):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    await _kill(managed, execution)
    await managed.host.close()
    # e.g. this data root was copied to another host sharing the database.
    marker, stale = _rewrite_origin(managed, boot=EARLIER_BOOT)
    await _born_in_earlier_boot(managed, execution)
    await _renewing(managed, execution)
    recorded = marker.read_bytes()
    monkeypatch.setattr(runtime_processes, "PREVIOUS_BOOT_LEASE_WAIT_SECONDS", 0.2)
    replacement = managed.new_host()
    with pytest.raises(CoordinationConflict, match="still renew their leases"):
        await replacement.start()
    assert marker.read_bytes() == recorded
    assert replacement.socket_root is None and replacement.lock.descriptor is None
    await _expire(managed, execution.runtime_id)
    await replacement.start()
    managed.host.socket_root = replacement.socket_root  # fixture cleanup
    assert replacement.launch_origin == process_origin() != stale["process_origin"]


async def test_retiring_an_earlier_boot_worker_waits_for_its_lease(managed):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    await _kill(managed, execution)
    await _born_in_earlier_boot(managed, execution)
    await _renewing(managed, execution)
    # Absent from this kernel is not exited while another host may renew it.
    with pytest.raises(TimeoutError):
        await managed.host.retire(execution.runtime_id, timeout=1)
    async with managed.database.session() as session:
        assert (
            await session.get(RuntimeProcessRow, execution.runtime_id)
        ).state != "retired"
    await _expire(managed, execution.runtime_id)
    await managed.host.retire(execution.runtime_id, timeout=5)
    async with managed.database.session() as session:
        assert (
            await session.get(RuntimeProcessRow, execution.runtime_id)
        ).state == "retired"


async def test_unreadable_origin_only_replaces_a_missing_socket_directory(
    managed, monkeypatch
):
    await managed.stage("v1")
    await managed.host.close()
    marker = managed.data / "runtime" / "process-origin.json"
    document = json.loads(marker.read_text())
    document["process_origin"] = None
    shutil.rmtree(document["socket_root"])
    managed.host.socket_root = None  # the fixture must not remove it again
    marker.write_text(json.dumps(document))
    monkeypatch.setattr(runtime_processes, "process_origin", lambda: None)
    replacement = managed.new_host()
    await replacement.start()
    managed.host.socket_root = replacement.socket_root  # fixture cleanup
    current = json.loads(marker.read_text())
    assert replacement.launch_origin is None and current["process_origin"] is None
    assert current["replaced"]["reason"] == "socket_root_missing"


def _record_socket_root(managed, path, *, namespace=None):
    """Point the private marker at another socket directory (same boot)."""
    marker = managed.data / "runtime" / "process-origin.json"
    document = json.loads(marker.read_text())
    document["socket_root"] = str(path)
    if namespace is not None:
        kind, boot, label, _inode = document["process_origin"].split(":")
        document["process_origin"] = ":".join((kind, boot, label, namespace))
    marker.write_text(json.dumps(document))
    return marker


@pytest.mark.skipif(os.getuid() == 0, reason="root owns every directory")
@pytest.mark.parametrize("kind", ["foreign", "shared", "symlink"])
async def test_untrusted_socket_directory_on_the_same_boot_is_never_reused(
    managed, tmp_path, request, kind
):
    await managed.stage("v1")
    execution = await managed.host.spawn("v1", "execution")
    await managed.host.close()
    original = managed.host.socket_root
    if kind == "foreign":
        # e.g. another local user recreated the vanished public /tmp path.
        untrusted = Path("/")
    elif kind == "shared":
        untrusted = original
        untrusted.chmod(0o755)
    else:
        untrusted = tmp_path / "linked-sockets"
        untrusted.symlink_to(original)
    before = os.lstat(untrusted)
    marker = _record_socket_root(managed, untrusted)
    replacement = managed.new_host()
    await replacement.start()
    current = json.loads(marker.read_text())
    assert replacement.launch_origin == current["process_origin"] == process_origin()
    assert current["replaced"]["reason"] == "socket_root_untrusted"
    assert current["replaced"]["socket_root"] == str(untrusted)
    fresh = replacement.socket_root
    assert fresh not in {untrusted, original} and str(fresh) == current["socket_root"]
    details = os.lstat(fresh)
    assert stat.S_ISDIR(details.st_mode) and stat.S_IMODE(details.st_mode) == 0o700
    # The untrusted path is neither repaired, followed nor removed.
    after = os.lstat(untrusted)
    assert (after.st_mode, after.st_uid, after.st_ino) == (
        before.st_mode,
        before.st_uid,
        before.st_ino,
    )
    # A live worker recorded in the old directory is not presumed dead.
    [observed] = await replacement.rediscover()
    assert observed.process_state == "alive"
    assert not (await replacement.reconcile_processes())["retired_runtime_ids"]
    assert same_process(execution.pid, execution.process_birth)
    worker = await replacement.spawn("v1", "execution")
    assert worker.endpoint.startswith("unix://" + str(fresh) + "/")
    original.chmod(0o700)
    managed.host.socket_root = fresh  # the fixture removes the new root
    request.addfinalizer(lambda: shutil.rmtree(original, ignore_errors=True))


@pytest.mark.skipif(os.getuid() == 0, reason="root owns every directory")
async def test_untrusted_socket_directory_in_another_namespace_is_refused(managed):
    await managed.stage("v1")
    await managed.host.close()
    marker = _record_socket_root(managed, Path("/"), namespace="4026599999")
    recorded = marker.read_bytes()
    replacement = managed.new_host()
    with pytest.raises(CoordinationConflict, match="another process namespace"):
        await replacement.start()
    assert marker.read_bytes() == recorded
    assert replacement.socket_root is None
