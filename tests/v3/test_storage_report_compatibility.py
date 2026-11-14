"""A storage report only runs on, moves to, or is viewed by a release that knows it."""

from pathlib import Path

import pytest

from nebula.v3.database import DeploymentHostRow
from nebula.v3.runtime_coordination import CoordinationConflict, STORAGE_REPORT_COMMAND
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_routing import RuntimeRouting
from nebula.v3.runtime_vault import RuntimeVault
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
ACTOR = "storage-source:synthetic-source:" + "a" * 64


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def stage_storage(host, version="storage"):
    await host[0].stage(
        manifest(version, features=[STORAGE_REPORT_COMMAND]),
        Path("/isolated/releases") / version,
        "d" * 64,
    )


async def save_report(coordinator, tmp_path, project="one"):
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(tmp_path / "payloads")
    )
    return await delivery.accept(
        HttpIntent.capture(
            "POST",
            "/api/v1/storage/events",
            b"",
            [(b"content-type", b"application/json")],
            b'{"schema_version":1}',
        ),
        project_id=project,
        actor_id=ACTOR,
        idempotency_key="storage-event-synthetic",
    )


async def test_routing_names_the_release_that_runs_a_projects_next_report(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)
    routing = RuntimeRouting(coordinator.coordination, tmp_path / "credentials")
    # An owner from a release without the feature cannot run a report.
    old = await worker(host, "v1")
    await coordinator.coordination.claim_project("one", old)
    assert not await routing.supports_storage_reports("one", default=True)
    # An unowned project starts from the host target, or this build without one.
    assert await routing.supports_storage_reports("two", default=True)
    assert not await routing.supports_storage_reports("two", default=False)
    for target, expected in (("storage", True), ("v1", False)):
        async with coordinator.coordination.database.session() as session:
            row = await session.get(DeploymentHostRow, "host")
            if row is None:
                session.add(
                    DeploymentHostRow(
                        key="host",
                        revision=0,
                        api_runtime_id=None,
                        target_version=target,
                        rollback_version=None,
                        shutdown_requested=False,
                        detail="synthetic",
                    )
                )
            else:
                row.target_version = target
            await session.commit()
        assert (
            await routing.supports_storage_reports("two", default=not expected)
            is expected
        )


async def test_saved_storage_reports_cannot_transfer_to_an_older_execution_worker(
    host, tmp_path
):
    coordinator, _ = host
    await stage_storage(host)
    source, target = await worker(host, "storage"), await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", source)
    await coordinator.request_handoff("one", target)
    receipt = await save_report(coordinator, tmp_path)
    assert receipt.project_id == "one"
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    with pytest.raises(CoordinationConflict, match="storage reports"):
        await coordinator.transfer("one")
    async with coordinator.coordination.fenced(lease):
        pass


async def test_saved_storage_reports_protect_an_api_rollback(host, tmp_path):
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
