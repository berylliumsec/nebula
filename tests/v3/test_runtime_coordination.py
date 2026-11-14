"""Multi-connection coordination contracts using synthetic, inert projects."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import make_url

from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, Engagement
from nebula.v3.event_history import purge_event_history
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LEASE_MS,
    LeaseLost,
    RuntimeCoordination,
    STREAM_EPOCH,
)
from nebula.v3.storage import NebulaStore, StoreTransaction

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=["sqlite", "postgresql"])
def location(request, tmp_path):
    if request.param == "sqlite":
        yield tmp_path / "coordination.db"
        return
    configured = os.getenv("NEBULA_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("NEBULA_TEST_POSTGRES_URL is required for PostgreSQL coverage")
    url = make_url(Database._normalize_sync_url(configured))
    schema = "coordination_" + uuid4().hex
    engine = create_engine(url, pool_size=1, max_overflow=0)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    try:
        yield url.update_query_dict(
            {"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.fixture
async def cluster(location):
    databases = [
        Database(location, pool_settings=PoolSettings(5, 12, 2, 5)) for _ in range(2)
    ]
    stores = [NebulaStore(database) for database in databases]
    coordinators = [RuntimeCoordination(database) for database in databases]
    clock = [1_000_000]

    async def now(_session):
        return clock[0]

    for coordinator in coordinators:
        coordinator._clock_ms = now
    try:
        await stores[0].create(Engagement(id="project", name="Synthetic project"))
        await stores[0].create(Engagement(id="other", name="Other synthetic project"))
        await databases[1].initialize()
        for coordinator, name in zip(coordinators, ("old", "new")):
            await coordinator.register_runtime(name, f"release-{name}")
        yield coordinators, stores, clock
    finally:
        for store in stores:
            await store.event_history.close()
            await store.database.dispose()


async def submit(coordinator, key="request", **changes):
    values = dict(
        project_id="project",
        actor_id="user",
        idempotency_key=key,
        name="chat.submit",
        payload={"message": "inert fixture"},
        expected_revision=3,
    )
    values.update(changes)
    return await coordinator.submit(**values)


async def expire_and_claim(cluster):
    (first, second), _, clock = cluster
    clock[0] += LEASE_MS + 1
    await second.register_runtime("replacement", "release-new")
    return await second.claim_project("project", "replacement")


async def test_only_one_connection_can_claim_a_project(cluster):
    (first, second), _, _ = cluster
    outcomes = await asyncio.gather(
        first.claim_project("project", "old"),
        second.claim_project("project", "new"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CoordinationConflict) for result in outcomes) == 1
    owner = next(result for result in outcomes if not isinstance(result, Exception))
    assert owner.fencing_token == 1


async def test_recovery_waits_for_expiry_and_fences_every_old_write(cluster):
    (first, second), _, _ = cluster
    old = await first.claim_project("project", "old")
    with pytest.raises(CoordinationConflict, match="live execution owner"):
        await second.claim_project("project", "new")
    new = await expire_and_claim(cluster)
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(LeaseLost):
        async with first.fenced(old):
            pytest.fail("stale owner entered the transaction")
    async with second.fenced(new):
        pass


async def test_heartbeat_extends_only_live_leases_and_cannot_revive(cluster):
    (first, second), _, clock = cluster
    await first.claim_project("project", "old")
    clock[0] += 10_000
    await first.heartbeat("old")
    clock[0] += 20_001
    await second.register_runtime("candidate", "release-new")
    with pytest.raises(CoordinationConflict):
        await second.claim_project("project", "candidate")
    clock[0] += 10_000
    with pytest.raises(LeaseLost):
        await first.heartbeat("old")
    with pytest.raises(CoordinationConflict, match="revived"):
        await first.register_runtime("old", "release-old")
    assert (await second.claim_project("project", "candidate")).fencing_token == 2


async def test_api_process_cannot_be_an_execution_owner(cluster):
    (first, _), _, _ = cluster
    await first.register_runtime("api", "release-new", role="api")
    with pytest.raises(LeaseLost):
        await first.claim_project("project", "api")
    with pytest.raises(CoordinationConflict, match="does not exist"):
        await first.claim_project("missing", "old")


async def test_expiry_during_transaction_rolls_back_authoritative_state(cluster):
    (first, _), (store, _), clock = cluster
    lease = await first.claim_project("project", "old")
    before = await store.get(Engagement, "project")
    with pytest.raises(LeaseLost):
        async with first.fenced(lease) as session:
            await StoreTransaction(session).update(
                Engagement,
                "project",
                {"name": "Expired writer"},
                expected_revision=before.revision,
            )
            clock[0] += LEASE_MS
    after = await store.get(Engagement, "project")
    assert after.revision == before.revision
    assert after.name == before.name


async def test_lost_response_retry_returns_exact_original_receipt(cluster):
    (first, second), (store, _), _ = cluster
    receipts = await asyncio.gather(
        *(submit((first, second)[index % 2]) for index in range(12))
    )
    assert all(receipt == receipts[0] for receipt in receipts)
    async with store.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 1
        )
    lease = await first.claim_project("project", "old")
    command = await first.claim_next(lease)
    await first.finish_command(
        lease, command.receipt.command_id, result={"turn_id": "one-turn"}
    )
    assert await submit(second) == receipts[0]


@pytest.mark.parametrize(
    "changes",
    [
        {"payload": {"message": "different"}},
        {"project_id": "other"},
        {"name": "chat.stop"},
        {"expected_revision": 4},
    ],
)
async def test_reusing_a_key_with_changed_intent_is_a_conflict(cluster, changes):
    (first, second), _, _ = cluster
    await submit(first)
    with pytest.raises(CoordinationConflict, match="different payload"):
        await submit(second, **changes)


async def test_receipts_are_scoped_to_authenticated_actor(cluster):
    (first, second), _, _ = cluster
    one = await submit(first)
    two = await submit(second, actor_id="different-user")
    assert one.command_id != two.command_id
    assert one.sequence < two.sequence


async def test_accepted_receipt_survives_project_deletion(cluster):
    (first, second), (store, _), _ = cluster
    receipt = await submit(first)
    await store.delete(Engagement, "project")
    assert await submit(second) == receipt
    with pytest.raises(CoordinationConflict, match="different payload"):
        await submit(second, payload={"changed": True})


async def test_command_order_and_revision_survive_independent_readers(cluster):
    (first, second), _, _ = cluster
    one, two = await submit(first, "one"), await submit(second, "two")
    lease = await first.claim_project("project", "old")
    command = await second.claim_next(lease)
    assert command.receipt == one
    assert command.expected_revision == 3
    assert await first.claim_next(lease) is None
    await first.finish_command(lease, one.command_id, result={"turn_id": "first"})
    assert (await second.claim_next(lease)).receipt == two


async def test_expired_running_command_stays_uncertain_and_blocks_replay(cluster):
    (first, second), (store, _), _ = cluster
    receipt = await submit(first, "uncertain")
    queued = await submit(second, "later")
    old = await first.claim_project("project", "old")
    assert (await first.claim_next(old)).receipt == receipt
    new = await expire_and_claim(cluster)
    assert await second.claim_next(new) is None
    assert await second.claim_next(new) is None
    with pytest.raises(LeaseLost):
        await first.finish_command(
            old, receipt.command_id, result={"unsafe": "late result"}
        )
    async with store.database.session() as session:
        assert (
            await session.get(RuntimeCommandRow, receipt.command_id)
        ).state == "uncertain"
        assert (
            await session.get(RuntimeCommandRow, queued.command_id)
        ).state == "queued"


async def test_fenced_command_cannot_complete_in_another_project(cluster):
    (first, _), _, _ = cluster
    await first.claim_project("project", "old")
    other = await first.claim_project("other", "old")
    receipt = await submit(first)
    with pytest.raises(CoordinationConflict):
        await first.finish_command(other, receipt.command_id, result={})


@pytest.mark.parametrize("fail", [False, True])
async def test_command_outcome_and_authoritative_state_commit_together(cluster, fail):
    (first, _), (store, _), _ = cluster
    lease = await first.claim_project("project", "old")
    receipt = await submit(first)
    await first.claim_next(lease)

    async def apply(session):
        await StoreTransaction(session).update(
            Engagement,
            "project",
            {"name": "Committed project"},
            expected_revision=1,
        )
        if fail:
            raise ValueError("synthetic failed state write")

    if fail:
        with pytest.raises(ValueError, match="synthetic"):
            await first.finish_command(
                lease, receipt.command_id, result={}, apply=apply
            )
    else:
        await first.finish_command(lease, receipt.command_id, result={}, apply=apply)
    project = await store.get(Engagement, "project")
    assert project.revision == (1 if fail else 2)
    assert project.name == ("Synthetic project" if fail else "Committed project")
    async with store.database.session() as session:
        assert (await session.get(RuntimeCommandRow, receipt.command_id)).state == (
            "running" if fail else "succeeded"
        )


async def turn_fixture(cluster):
    (coordinator, _), (store, _), _ = cluster
    await store.create(
        ChatTurn(
            id="turn",
            engagement_id="project",
            session_id="chat",
            model="inert",
            provider_profile_id="inert-provider",
        )
    )
    return await coordinator.claim_project("project", "old")


async def test_stream_replays_committed_batches_on_another_connection(cluster):
    (first, second), (store, _), _ = cluster
    lease = await turn_fixture(cluster)
    head = await first.append_stream_batch(
        lease, "turn", batch_key="one", events=[("delta", {"text": "hello"})]
    )
    await store.append_event("turn", "unrelated.audit", {})
    tail = await first.append_stream_batch(
        lease,
        "turn",
        batch_key="two",
        events=[("delta", {"text": "world"}), ("done", {})],
    )
    assert await second.replay_stream("turn") == head + tail
    assert (
        await second.replay_stream("turn", after_sequence=head[-1].sequence, limit=1)
        == tail[:1]
    )
    assert tail[0].sequence == head[0].sequence + 2
    new = await expire_and_claim(cluster)
    final = await second.append_stream_batch(
        new, "turn", batch_key="three", events=[("resumed", {})]
    )
    assert (await first.replay_stream("turn")) == head + tail + final
    assert {frame.epoch for frame in head + tail + final} == {STREAM_EPOCH}


async def test_stream_batch_retry_is_atomic_and_conflicting_retry_fails(cluster):
    (first, second), _, _ = cluster
    lease = await turn_fixture(cluster)
    events = [("delta", {"text": "A"}), ("delta", {"text": "B"})]
    original = await first.append_stream_batch(
        lease, "turn", batch_key="batch", events=events
    )
    assert (
        await second.append_stream_batch(
            lease, "turn", batch_key="batch", events=events
        )
        == original
    )
    for changed in (
        events[:1],
        events + [("done", {})],
        [("delta", {"text": "changed"})] + events[1:],
    ):
        with pytest.raises(CoordinationConflict):
            await second.append_stream_batch(
                lease, "turn", batch_key="batch", events=changed
            )
    assert await second.replay_stream("turn") == original


async def test_stream_fences_stale_writers_and_cross_project_targets(cluster):
    (first, second), _, _ = cluster
    lease = await turn_fixture(cluster)
    other = await first.claim_project("other", "old")
    with pytest.raises(CoordinationConflict):
        await first.append_stream_batch(
            other, "turn", batch_key="wrong", events=[("delta", {})]
        )
    await expire_and_claim(cluster)
    with pytest.raises(LeaseLost):
        await first.append_stream_batch(
            lease, "turn", batch_key="stale", events=[("delta", {})]
        )
    assert await second.replay_stream("turn") == []


async def test_stream_uses_existing_owner_retention(cluster):
    (first, second), (store, _), _ = cluster
    lease = await turn_fixture(cluster)
    frames = await first.append_stream_batch(
        lease, "turn", batch_key="retained", events=[("done", {})]
    )
    await purge_event_history(store.database, owner_ids=["turn"])
    assert await second.replay_stream("turn") == frames
    await store.delete(ChatTurn, "turn")
    await store.event_history.wait_idle(timeout=10)
    assert await second.replay_stream("turn") == []


async def test_real_database_clock_is_used(cluster):
    (coordinator, _), _, _ = cluster
    del coordinator._clock_ms
    async with coordinator.database.session() as session:
        assert await coordinator._clock_ms(session) > 1_700_000_000_000


async def _lane_rows(coordinator, lane):
    async with coordinator.database.session() as session:
        return list(
            await session.execute(
                select(RuntimeCommandRow.id, RuntimeCommandRow.state)
                .where(RuntimeCommandRow.lane == lane)
                .order_by(RuntimeCommandRow.sequence)
            )
        )


async def _claim_acts(coordinator, lease, lane):
    before = await _lane_rows(coordinator, lane)
    claimed = await coordinator.claim_next(lease, lane=lane)
    return claimed is not None or before != await _lane_rows(coordinator, lane)


async def _dispatch_state(cluster, name):
    (first, second), _stores, _clock = cluster
    coordinator, lease = first, await first.claim_project("project", "old")

    async def add(row):
        async with coordinator.database.session() as session:
            session.add(row)

    async def handoff(phase, *, held=True, source_token=None):
        await add(
            ProjectHandoffRow(
                project_id="project",
                revision=1,
                source_runtime_id=lease.runtime_id,
                source_fencing_token=(
                    lease.fencing_token if source_token is None else source_token
                ),
                target_runtime_id="new",
                target_version="v2",
                phase=phase,
                admission_held=held,
                blockers=[],
                checkpoint={},
                admitted_turn_ids=[],
                updated_at_ms=1,
            )
        )

    if name in {"queued_work", "queued_both"} or name in {
        "handoff_hold",
        "handoff_verified",
        "shutdown",
        "retirement",
    }:
        await submit(first, "work")
    if name in {"queued_control", "queued_both"} or name in {
        "handoff_hold",
        "handoff_verified",
        "shutdown",
        "retirement",
    }:
        await submit(first, "control", lane="control")
    if name in {
        "control_own_running",
        "control_foreign_running",
        "control_uncertain",
    }:
        await submit(first, "control", lane="control")
        assert await first.claim_next(lease, lane="control") is not None
    if name in {"control_foreign_running", "control_uncertain"}:
        coordinator, lease = second, await expire_and_claim(cluster)
    if name == "control_uncertain":
        assert await second.claim_next(lease, lane="control") is None
        await submit(second, "control-behind", lane="control")
    if name in {"handoff_finishing", "handoff_transferable", "handoff_other_source"}:
        await submit(first, "work")
        await submit(first, "control", lane="control")
    if name == "handoff_finishing":
        await handoff("finishing", held=False)
    if name == "handoff_transferable":
        await handoff("transferable", held=False)
    if name == "handoff_other_source":
        await handoff("requested", source_token=lease.fencing_token + 7)
    if name == "own_running":
        await submit(first, "work")
        assert await first.claim_next(lease, lane="work") is not None
    if name in {"foreign_running", "uncertain_head"}:
        await submit(first, "work")
        assert await first.claim_next(lease, lane="work") is not None
        coordinator, lease = second, await expire_and_claim(cluster)
    if name == "uncertain_head":
        assert await second.claim_next(lease, lane="work") is None
        await submit(second, "behind")
    if name == "barrier_head":
        await submit(first, "barrier")
        async with coordinator.database.session() as session:
            await session.execute(
                update(RuntimeCommandRow).values(name="work.batch.barrier/v1")
            )
        await submit(first, "behind")
    if name == "handoff_hold":
        await handoff("requested")
    if name == "handoff_verified":
        await handoff("verified")
    if name == "shutdown":
        async with coordinator.database.session() as session:
            await session.execute(
                update(DeploymentHostRow)
                .where(DeploymentHostRow.key == "host")
                .values(shutdown_requested=True)
            )
    if name == "retirement":
        async with coordinator.database.session() as session:
            await session.execute(
                update(ProjectOwnershipRow).values(retirement_requested=True)
            )
    return coordinator, lease


@pytest.mark.parametrize(
    ("name", "expected", "handoff_active"),
    [
        ("empty", set(), False),
        ("queued_work", {"work"}, False),
        ("queued_control", {"control"}, False),
        ("queued_both", {"work", "control"}, False),
        ("own_running", set(), False),
        ("foreign_running", {"work"}, False),
        ("uncertain_head", set(), False),
        ("barrier_head", set(), False),
        ("handoff_hold", {"control"}, True),
        ("handoff_verified", {"work", "control"}, False),
        ("shutdown", {"control"}, False),
        ("retirement", set(), False),
        ("control_own_running", set(), False),
        ("control_foreign_running", {"control"}, False),
        ("control_uncertain", set(), False),
        ("handoff_finishing", {"work", "control"}, True),
        ("handoff_transferable", {"work", "control"}, True),
        ("handoff_other_source", {"control"}, False),
    ],
)
async def test_dispatch_snapshot_reports_exactly_the_lanes_a_claim_acts_on(
    cluster, name, expected, handoff_active
):
    coordinator, lease = await _dispatch_state(cluster, name)
    snapshot = await coordinator.dispatch_snapshot(lease)
    assert snapshot.owned
    assert snapshot.handoff_active is handoff_active
    acted = {
        lane
        for lane in ("work", "control")
        if await _claim_acts(coordinator, lease, lane)
    }
    assert set(snapshot.lanes) == acted == expected


async def test_dispatch_snapshot_takes_no_locks_and_reports_lost_ownership(cluster):
    (first, second), _stores, clock = cluster
    lease = await first.claim_project("project", "old")
    await submit(first, "work")
    statements = []

    def record(_connection, _cursor, statement, *_args):
        statements.append(statement.upper())

    engine = second.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    async with first.database.session() as session:
        # A held project-row lock would block a fenced claim but not the probe.
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "project")
            .values(command_sequence=ProjectOwnershipRow.command_sequence)
        )
        try:
            snapshot = await asyncio.wait_for(second.dispatch_snapshot(lease), 5)
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert snapshot.owned and snapshot.lanes == {"work"}
    assert statements and all(
        statement.lstrip().startswith("SELECT")
        and "FOR UPDATE" not in statement
        and "FOR SHARE" not in statement
        for statement in statements
    ), statements
    clock[0] += LEASE_MS + 1
    expired = await first.dispatch_snapshot(lease)
    assert not expired.owned


async def test_dispatch_snapshot_reports_a_superseded_owner(cluster):
    (first, second), _stores, _clock = cluster
    old = await first.claim_project("project", "old")
    await submit(first, "work")
    new = await expire_and_claim(cluster)
    assert not (await second.dispatch_snapshot(old)).owned
    assert (await second.dispatch_snapshot(new)).owned
