"""A storage report only runs on, moves to, or is viewed by a release that knows it."""

from pathlib import Path

import pytest
from sqlalchemy import select, update

from nebula.v3.database import (
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
)
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    REPORT_LANE,
    STORAGE_REPORT_COMMAND,
)
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage_report_admission import StorageReportUnsupported
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
ACTOR = "storage-source:synthetic-source:" + "a" * 64
FEATURES = [STORAGE_REPORT_COMMAND, "project.handoff/v1"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def stage_storage(host, version="storage"):
    await host[0].stage(
        manifest(version, features=FEATURES),
        Path("/isolated/releases") / version,
        "d" * 64,
    )


async def save_report(
    coordinator, tmp_path, project="one", *, key="synthetic", release_default=None
):
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(tmp_path / "payloads")
    )
    return await delivery.accept(
        HttpIntent.capture(
            "POST",
            "/api/v1/storage/events",
            b"",
            [(b"content-type", b"application/json")],
            b'{"schema_version":1,"key":"' + key.encode() + b'"}',
        ),
        project_id=project,
        actor_id=ACTOR,
        idempotency_key="storage-event-" + key,
        storage_release_default=release_default,
    )


async def set_target(coordinator, version):
    async with coordinator.coordination.database.session() as session:
        row = await session.get(DeploymentHostRow, "host")
        if row is None:
            session.add(
                DeploymentHostRow(
                    key="host",
                    revision=0,
                    api_runtime_id=None,
                    target_version=version,
                    rollback_version=None,
                    shutdown_requested=False,
                    detail="synthetic",
                )
            )
        else:
            row.target_version = version
        await session.commit()


async def expire(coordinator, project):
    async with coordinator.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == project)
            .values(lease_expires_at_ms=0)
        )
        await session.commit()


async def saved(coordinator, project=None):
    async with coordinator.coordination.database.session() as session:
        return list(
            await session.scalars(
                select(RuntimeCommandRow).where(
                    RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
                    *(
                        [RuntimeCommandRow.project_id == project]
                        if project is not None
                        else []
                    ),
                )
            )
        )


async def finish(coordinator, lease):
    claimed = await coordinator.coordination.claim_next(lease, lane=REPORT_LANE)
    await coordinator.coordination.finish_command(
        lease, claimed.receipt.command_id, result={"http_status": 202}
    )


async def test_a_report_is_saved_only_where_every_next_worker_can_run_it(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)

    def files():
        root = tmp_path / "payloads"
        return {path.name for path in root.iterdir()} if root.exists() else set()

    async def refused(project="one", **options):
        before = files()
        with pytest.raises(StorageReportUnsupported):
            await save_report(coordinator, tmp_path, project, **options)
        assert await saved(coordinator, project) == []
        # The refused report's payload did not stay in the vault.
        assert files() - before <= {"key"}

    # A live owner from a release without the feature would run it.
    old = await worker(host, "v1")
    await coordinator.coordination.claim_project("one", old)
    await refused(release_default=True)
    # The check reads only a live owner: an expired one runs nothing more and
    # its project's next owner starts from the host target.
    await expire(coordinator, "one")
    await set_target(coordinator, "v1")
    await refused(release_default=True)
    await set_target(coordinator, "storage")
    assert (await save_report(coordinator, tmp_path, release_default=False)).project_id
    # An unowned project starts from the target, or this API's build without one.
    await set_target(coordinator, None)
    await refused("two", release_default=False, key="two")
    await save_report(coordinator, tmp_path, "two", release_default=True, key="two")
    assert len(await saved(coordinator)) == 2


async def test_no_report_is_saved_for_a_project_moving_to_an_older_release(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)
    await set_target(coordinator, "storage")
    source, target = await worker(host, "storage"), await worker(host, "v1")
    await coordinator.coordination.claim_project("one", source)
    await coordinator.request_handoff("one", target)
    # The source owner could run it, but the handoff's target could not.
    with pytest.raises(StorageReportUnsupported):
        await save_report(coordinator, tmp_path, release_default=True)
    assert await saved(coordinator) == []


async def test_saved_storage_reports_cannot_transfer_to_an_older_execution_worker(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)
    source, target = await worker(host, "storage"), await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", source)
    receipt = await save_report(coordinator, tmp_path)
    assert receipt.project_id == "one"
    # A pending report keeps its project off a release that cannot run it.
    with pytest.raises(CoordinationConflict, match="storage.report/v1"):
        await coordinator.request_handoff("one", target)
    # A report saved without the release check (an older API) is still fenced.
    async with coordinator.coordination.database.session() as session:
        row = await session.get(RuntimeCommandRow, receipt.command_id)
        row.state = "succeeded"
        await session.commit()
    await coordinator.request_handoff("one", target)
    async with coordinator.coordination.database.session() as session:
        row = await session.get(RuntimeCommandRow, receipt.command_id)
        row.state = "queued"
        await session.commit()
    with pytest.raises(CoordinationConflict, match="storage"):
        await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    with pytest.raises(CoordinationConflict):
        await coordinator.transfer("one")
    async with coordinator.coordination.fenced(lease):
        pass


async def test_no_worker_without_the_feature_takes_a_project_with_pending_reports(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)
    old = await worker(host, "v1")
    await save_report(coordinator, tmp_path)
    # Plain admission (and expired-owner recovery) need the feature now.
    with pytest.raises(CoordinationConflict, match="storage.report/v1"):
        await coordinator.coordination.claim_project("one", old)
    current = await worker(host, "storage")
    lease = await coordinator.coordination.claim_project("one", current)
    await finish(coordinator, lease)
    # Once nothing is pending, the older release may own the project again.
    assert not await saved(coordinator) or all(
        row.state == "succeeded" for row in await saved(coordinator)
    )


async def test_a_target_change_waits_for_pending_reports(host, tmp_path):
    coordinator, _ = host
    await stage_storage(host)
    await coordinator.target_execution("storage")
    owner = await worker(host, "storage")
    lease = await coordinator.coordination.claim_project("one", owner)
    await save_report(coordinator, tmp_path)
    revision = (await coordinator.status()).revision
    for change in (
        coordinator.rollback_code("v1", expected_revision=revision),
        coordinator.target_execution("v1"),
        coordinator.reserve_worker(
            "v1",
            role="execution",
            endpoint="unix:///isolated/v1.sock",
            credential_reference="v1",
        ),
    ):
        with pytest.raises(CoordinationConflict, match="storage reports waiting"):
            await change
    assert (await coordinator.status()).target_execution_version == "storage"
    # A release that knows reports is never held back by them.
    await stage_storage(host, "storage-next")
    await coordinator.target_execution("storage-next")
    await finish(coordinator, lease)
    await coordinator.rollback_code(
        "v1", expected_revision=(await coordinator.status()).revision
    )
    assert (await coordinator.status()).target_execution_version == "v1"


async def test_saved_storage_reports_protect_an_api_rollback_only_until_they_run(
    host, tmp_path
):
    coordinator, _ = host
    old_api = await worker(host, "v1", "api")
    await save_report(coordinator, tmp_path)
    with pytest.raises(CoordinationConflict, match=STORAGE_REPORT_COMMAND):
        await coordinator.promote_api(
            old_api, expected_revision=(await coordinator.status()).revision
        )
    await stage_storage(host)
    new_api = await worker(host, "storage", "api")
    await coordinator.promote_api(
        new_api, expected_revision=(await coordinator.status()).revision
    )
    assert (await coordinator.status()).api_version == "storage"
    # A finished report needs nothing from the API: rollback is possible again.
    owner = await worker(host, "storage")
    await finish(
        coordinator, await coordinator.coordination.claim_project("one", owner)
    )
    old_api = await worker(host, "v1", "api")
    await coordinator.promote_api(
        old_api, expected_revision=(await coordinator.status()).revision
    )
    assert (await coordinator.status()).api_version == "v1"


async def test_recovery_runs_an_interrupted_report_again(host, tmp_path):
    coordinator, _ = host
    await stage_storage(host)
    first, target = await worker(host, "storage"), await worker(host, "storage")
    old = await coordinator.coordination.claim_project("one", first)
    report = await save_report(coordinator, tmp_path)
    operator = await coordinator.coordination.submit(
        actor_id="operator",
        idempotency_key="operator-work",
        project_id="one",
        name="http.invoke/v1",
        payload={},
    )
    claimed = await coordinator.coordination.claim_next(old, lane=REPORT_LANE)
    assert claimed.receipt.command_id == report.command_id
    await expire(coordinator, "one")
    current = await coordinator.begin_recovery(
        "one",
        target,
        expected_owner=old,
        expected_handoff_revision=None,
        operation_id="recovery-one",
        actor_id="operator",
    )
    async with coordinator.database.session() as session:
        row = await session.get(RuntimeCommandRow, report.command_id)
    # Re-running a report is safe, so recovery queues it again instead of
    # leaving it uncertain, and it is no recovery blocker.
    assert (row.state, row.runtime_id, row.result) == (
        "queued",
        None,
        {"interrupted_runs": 1},
    )
    status = await coordinator.recovery_status("one")
    assert status["blockers"] == []
    digest = await coordinator.prepare_recovery(current, operation_id="recovery-one")
    await coordinator.verify_recovery(
        current, operation_id="recovery-one", restored_checkpoint_digest=digest
    )
    again = await coordinator.coordination.claim_next(current, lane=REPORT_LANE)
    assert again.receipt.command_id == report.command_id
    work = await coordinator.coordination.claim_next(current, lane="work")
    assert work.receipt.command_id == operator.command_id
