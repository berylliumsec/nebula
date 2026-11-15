"""Checkpoint scans keep leases live and reject concurrently changed SQL state."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, update

from nebula.v3.database import EntityRow, ProviderToolManifestRow, ProviderTurnQueueRow
from nebula.v3.domain import utc_now
from nebula.v3.runtime_checkpoint_generation import (
    assert_checkpoint_generation,
    read_checkpoint_generation,
)
from nebula.v3.runtime_coordination import CoordinationConflict, LEASE_MS, LeaseLost
from nebula.v3 import runtime_project_handoff as handoff
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import cluster as cluster, location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def coordinator(cluster):
    coordination = cluster[0][0]
    return SimpleNamespace(database=coordination.database, coordination=coordination)


async def generation(database, project_id="project"):
    async with database.session() as session:
        return await read_checkpoint_generation(session, project_id)


async def seed_input(database, family):
    async with database.session() as session:
        # The queue resolves its project through the turn; manifests have an FK
        # to the session. Raw SQL deliberately bypasses StoreTransaction so the
        # test also covers writes by older retained execution releases.
        await session.execute(
            insert(EntityRow).values(
                id="input",
                kind="chat_turns",
                engagement_id="project",
                payload={},
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    if family == "entities":
        return EntityRow, EntityRow.id == "input", {"revision": 2}
    if family == "queue":
        return (
            ProviderTurnQueueRow,
            ProviderTurnQueueRow.turn_id == "input",
            {"state": "running", "lease_owner": "execution"},
        )
    return (
        ProviderToolManifestRow,
        ProviderToolManifestRow.session_id == "input",
        {"digest": "b" * 64},
    )


async def insert_input(session, family):
    if family == "entities":
        await session.execute(
            insert(EntityRow).values(
                id="new-input",
                kind="observations",
                engagement_id="project",
                payload={},
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    elif family == "queue":
        await session.execute(
            insert(ProviderTurnQueueRow).values(
                turn_id="input", lane="foreground", state="queued"
            )
        )
    else:
        await session.execute(
            insert(ProviderToolManifestRow).values(
                session_id="input",
                revision=1,
                project_id="project",
                definitions=[],
                digest="a" * 64,
            )
        )


async def test_slow_scan_allows_heartbeat_and_fenced_writer(cluster, monkeypatch):
    owner, writer = cluster[0]
    lease = await owner.claim_project("project", "old")
    entered, release = asyncio.Event(), asyncio.Event()
    original = handoff.project_sql_checkpoint

    async def slow_scan(session, project_id):
        checkpoint = await original(session, project_id)
        entered.set()
        await release.wait()
        return checkpoint

    monkeypatch.setattr(handoff, "project_sql_checkpoint", slow_scan)
    scan = asyncio.create_task(
        handoff.snapshot_project_checkpoint(
            coordinator(cluster), "project", lease=lease
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        cluster[2][0] += 10_000
        # Both operations must finish while the scan is still paused. A scan
        # inside fenced() blocks the heartbeat and this waiting writer.
        await asyncio.wait_for(owner.heartbeat("old"), timeout=5)

        async def change_project():
            async with writer.fenced(lease) as session:
                await session.execute(
                    update(EntityRow)
                    .where(EntityRow.id == "project")
                    .values(revision=EntityRow.revision + 1)
                )

        await asyncio.wait_for(change_project(), timeout=5)
        assert not scan.done()
        release.set()
        with pytest.raises(CoordinationConflict, match="changed"):
            _, scanned_generation = await asyncio.wait_for(scan, timeout=5)
            # A backend may retain the read snapshot until scan completion;
            # the final fenced comparison must still reject publication.
            async with owner.fenced(lease) as session:
                await assert_checkpoint_generation(
                    session, "project", scanned_generation
                )
    finally:
        release.set()
        if not scan.done():
            scan.cancel()
        await asyncio.gather(scan, return_exceptions=True)


async def test_unchanged_scan_can_publish_under_fence(cluster):
    owner = cluster[0][0]
    lease = await owner.claim_project("project", "old")
    checkpoint, scanned_generation = await handoff.snapshot_project_checkpoint(
        coordinator(cluster), "project", lease=lease
    )
    assert checkpoint["project_id"] == "project"
    assert checkpoint["entity_count"] == 1
    async with owner.fenced(lease) as session:
        await assert_checkpoint_generation(session, "project", scanned_generation)


async def test_write_after_scan_rejects_publication(cluster):
    owner = cluster[0][0]
    lease = await owner.claim_project("project", "old")
    _, scanned_generation = await handoff.snapshot_project_checkpoint(
        coordinator(cluster), "project", lease=lease
    )
    async with owner.fenced(lease) as session:
        await session.execute(
            update(EntityRow)
            .where(EntityRow.id == "project")
            .values(revision=EntityRow.revision + 1)
        )
    with pytest.raises(CoordinationConflict, match="changed"):
        async with owner.fenced(lease) as session:
            await assert_checkpoint_generation(session, "project", scanned_generation)


@pytest.mark.parametrize("loss", ["expired", "replaced"])
async def test_unchanged_generation_does_not_bypass_lost_lease(cluster, loss):
    (owner, replacement), _, clock = cluster
    lease = await owner.claim_project("project", "old")
    _, scanned_generation = await handoff.snapshot_project_checkpoint(
        coordinator(cluster), "project", lease=lease
    )
    clock[0] += LEASE_MS + 1
    if loss == "replaced":
        await replacement.register_runtime("replacement", "release-new")
        newer = await replacement.claim_project("project", "replacement")
        assert newer.fencing_token > lease.fencing_token
    assert await generation(owner.database) == scanned_generation
    with pytest.raises(LeaseLost):
        async with owner.fenced(lease) as session:
            await assert_checkpoint_generation(session, "project", scanned_generation)


@pytest.mark.parametrize("family", ["entities", "queue", "manifests"])
async def test_raw_insert_update_delete_advance_only_affected_project(cluster, family):
    database = cluster[0][0].database
    row, predicate, changes = await seed_input(database, family)
    untouched = await generation(database, "other")
    before = await generation(database)
    async with database.session() as session:
        await insert_input(session, family)
    inserted = await generation(database)
    assert inserted > before
    async with database.session() as session:
        await session.execute(update(row).where(predicate).values(**changes))
    updated = await generation(database)
    assert updated > inserted
    async with database.session() as session:
        await session.execute(delete(row).where(predicate))
    assert await generation(database) > updated
    assert await generation(database, "other") == untouched


@pytest.mark.parametrize("family", ["entities", "queue", "manifests"])
async def test_moving_input_invalidates_old_and_new_project(cluster, family):
    database = cluster[0][0].database
    row, predicate, _ = await seed_input(database, family)
    if family != "entities":
        async with database.session() as session:
            await insert_input(session, family)
    before = {key: await generation(database, key) for key in ("project", "other")}
    async with database.session() as session:
        if family == "queue":
            await session.execute(
                insert(EntityRow).values(
                    id="other-input",
                    kind="chat_turns",
                    engagement_id="other",
                    payload={},
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            # Capture the new project's baseline after its turn is inserted.
            before["other"] = await read_checkpoint_generation(session, "other")
            changes = {"turn_id": "other-input"}
        else:
            changes = {
                "engagement_id" if family == "entities" else "project_id": "other"
            }
        await session.execute(update(row).where(predicate).values(**changes))
    for key in before:
        assert await generation(database, key) > before[key]


async def test_rolled_back_input_preserves_generation(cluster):
    database = cluster[0][0].database
    before = await generation(database)
    with pytest.raises(RuntimeError, match="rollback fixture"):
        async with database.session() as session:
            await session.execute(
                update(EntityRow).where(EntityRow.id == "project").values(revision=99)
            )
            assert await read_checkpoint_generation(session, "project") > before
            raise RuntimeError("rollback fixture")
    assert await generation(database) == before


async def test_missing_project_generation_is_zero(cluster):
    assert await generation(cluster[0][0].database, "never-created") == 0


@pytest.mark.parametrize("phase", ["prepare", "verify"])
@pytest.mark.parametrize("change_input", [False, True])
async def test_recovery_scan_keeps_heartbeat_and_writer_live(
    host, monkeypatch, phase, change_input
):
    from tests.v3.test_runtime_recovery import begin, expire, pair

    deployment, target, old = await pair(host)
    await expire(deployment)
    lease = await begin(deployment, target, old)
    digest = None
    if phase == "verify":
        digest = await deployment.prepare_recovery(lease, operation_id="recovery-one")
    before = await deployment.recovery_status("one")
    entered, release = asyncio.Event(), asyncio.Event()
    original = handoff.project_sql_checkpoint

    async def slow_scan(session, project_id):
        checkpoint = await original(session, project_id)
        entered.set()
        await release.wait()
        return checkpoint

    monkeypatch.setattr(handoff, "project_sql_checkpoint", slow_scan)
    operation = (
        deployment.prepare_recovery(lease, operation_id="recovery-one")
        if phase == "prepare"
        else deployment.verify_recovery(
            lease, operation_id="recovery-one", restored_checkpoint_digest=digest
        )
    )
    pending = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.wait_for(deployment.coordination.heartbeat(target), timeout=5)

        async def waiting_writer():
            async with deployment.coordination.fenced(lease) as session:
                if change_input:
                    await session.execute(
                        update(EntityRow)
                        .where(EntityRow.id == "one")
                        .values(revision=EntityRow.revision + 1)
                    )

        await asyncio.wait_for(waiting_writer(), timeout=5)
        assert not pending.done()
        release.set()
        if change_input:
            with pytest.raises(CoordinationConflict, match="changed"):
                await asyncio.wait_for(pending, timeout=5)
            after = await deployment.recovery_status("one")
            assert after["phase"] == before["phase"]
            assert after["revision"] == before["revision"]
        else:
            await asyncio.wait_for(pending, timeout=5)
            after = await deployment.recovery_status("one")
            assert after["phase"] == ("prepared" if phase == "prepare" else "verified")
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("phase", ["prepare", "verify"])
async def test_recovery_rejects_input_changed_between_scan_and_publication(
    host, monkeypatch, phase
):
    from tests.v3.test_runtime_recovery import begin, expire, pair

    deployment, target, old = await pair(host)
    await expire(deployment)
    lease = await begin(deployment, target, old)
    digest = None
    if phase == "verify":
        digest = await deployment.prepare_recovery(lease, operation_id="recovery-one")
    before = await deployment.recovery_status("one")
    original = handoff.snapshot_project_checkpoint

    async def change_after_scan(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with deployment.coordination.fenced(lease) as session:
            await session.execute(
                update(EntityRow)
                .where(EntityRow.id == "one")
                .values(revision=EntityRow.revision + 1)
            )
        return result

    monkeypatch.setattr(handoff, "snapshot_project_checkpoint", change_after_scan)
    with pytest.raises(CoordinationConflict, match="changed"):
        if phase == "prepare":
            await deployment.prepare_recovery(lease, operation_id="recovery-one")
        else:
            await deployment.verify_recovery(
                lease, operation_id="recovery-one", restored_checkpoint_digest=digest
            )
    after = await deployment.recovery_status("one")
    assert after["phase"] == before["phase"]
    assert after["revision"] == before["revision"]
