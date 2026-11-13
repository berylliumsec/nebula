"""Work-batch SQL, queue and version boundaries; no host batch API claim."""

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    delete,
    event,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from nebula.v3.database import (
    Database,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeWorkBatchProjectRow,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_context_requirements import (
    WORK_BATCH_FEATURE,
    required_context_features,
)
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_routing import RuntimeRouting
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_shutdown_readmission import admission as admission

pytestmark = pytest.mark.anyio
STATES = ("waiting", "barrier", "complete", "failed")


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/api/v1/chat/goal-conversations",
            {"engagement_id": "project", "objective": "Start a goal chat"},
        ),
        (
            "/api/v1/chat/control-assistant/session",
            {"engagement_id": "project", "backend": "provider"},
        ),
        (
            "/api/v1/chat/temporary-sessions",
            {"engagement_id": "project", "backend": "provider"},
        ),
        (
            "/api/v1/chat/sessions/existing-chat/fork",
            {"through_message_id": "message"},
        ),
    ],
)
def test_session_creation_routes_are_explicit_chat_start_intents(path, body):
    import json

    from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery

    intent = HttpIntent.capture(
        "POST",
        path,
        b"",
        [(b"content-type", b"application/json")],
        json.dumps(body).encode(),
    )
    assert RuntimeDelivery.is_chat_start_intent(intent)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def migrate(engine, revision):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "src/nebula/v3/migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_additive_migration_keeps_prior_data_and_enforces_metadata(location):
    engine = create_engine(
        Database._normalize_sync_url(location), pool_size=1, max_overflow=0
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    try:
        migrate(engine, "0046_provider_tool_manifest")
        entities = Table("entities", MetaData(), autoload_with=engine)
        commands = Table("runtime_commands", MetaData(), autoload_with=engine)
        ownership = Table("project_ownership", MetaData(), autoload_with=engine)
        stamp = datetime(2026, 11, 9, tzinfo=timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                entities.insert().values(
                    id="prior-data",
                    kind="observations",
                    engagement_id="one",
                    revision=7,
                    payload={"content": "retained exactly"},
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            connection.execute(
                commands.insert().values(
                    id="prior-command",
                    project_id="one",
                    actor_id="synthetic",
                    idempotency_key="prior",
                    sequence=1,
                    name="chat.submit",
                    lane="work",
                    payload={"content": "keep"},
                    payload_hash="a" * 64,
                    accepted_at_ms=1234,
                    state="queued",
                )
            )
            connection.execute(
                ownership.insert().values(
                    project_id="prior-owner",
                    runtime_id=None,
                    fencing_token=9,
                    lease_expires_at_ms=123,
                    command_sequence=4,
                )
            )
            previous_data = connection.execute(select(entities)).mappings().all()
            previous_commands = connection.execute(select(commands)).mappings().all()
            previous_owner = dict(
                connection.execute(select(ownership)).mappings().one()
            )
        migrate(engine, "0047_runtime_work_batches")
        migrate(engine, "0048_chat_scoped_admission")
        inspector = inspect(engine)
        table = RuntimeWorkBatchProjectRow.__table__
        assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
            "batch_id",
            "project_id",
        ]
        assert [column.name for column in table.primary_key] == [
            "batch_id",
            "project_id",
        ]
        assert set(table.columns.keys()) == {
            column["name"] for column in inspector.get_columns(table.name)
        }
        assert any(
            index["name"] == "ix_runtime_work_batch_project_state"
            and index["column_names"] == ["project_id", "state"]
            for index in inspector.get_indexes(table.name)
        )
        (foreign_key,) = inspector.get_foreign_keys(table.name)
        assert (
            foreign_key["constrained_columns"],
            foreign_key["referred_table"],
            foreign_key["referred_columns"],
            foreign_key["options"]["ondelete"],
        ) == (["batch_id"], "runtime_commands", ["id"], "CASCADE")
        participants = Table(table.name, MetaData(), autoload_with=engine)
        migrated_ownership = Table(
            "project_ownership", MetaData(), autoload_with=engine
        )
        with engine.begin() as connection:
            assert (
                connection.execute(select(entities)).mappings().all() == previous_data
            )
            assert (
                connection.execute(select(commands)).mappings().all()
                == previous_commands
            )
            current_owner = (
                connection.execute(select(migrated_ownership)).mappings().one()
            )
            assert current_owner["admission_requested"] is False
            assert all(
                current_owner[key] == value for key, value in previous_owner.items()
            )
            assert migrated_ownership.c.admission_requested.nullable is False
            assert migrated_ownership.c.admission_requested.server_default is not None
            connection.execute(
                commands.insert().values(
                    id="cascade-parent",
                    project_id="one",
                    actor_id="synthetic",
                    idempotency_key="cascade",
                    sequence=2,
                    name="work.batch/v1",
                    lane="work",
                    payload={},
                    payload_hash="b" * 64,
                    accepted_at_ms=1235,
                    state="queued",
                )
            )
            connection.execute(
                participants.insert(),
                [
                    {
                        "batch_id": "cascade-parent",
                        "project_id": project,
                        "state": "waiting",
                    }
                    for project in ("one", "two")
                ],
            )
        for values in (
            {"batch_id": "cascade-parent", "project_id": "one", "state": "waiting"},
            {"batch_id": "missing-parent", "project_id": "other", "state": "waiting"},
        ):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(participants.insert().values(**values))
        with engine.begin() as connection:
            connection.execute(
                delete(commands).where(commands.c.id == "cascade-parent")
            )
            assert connection.execute(select(participants)).all() == []
            assert (
                connection.execute(select(entities)).mappings().all() == previous_data
            )
            assert (
                connection.execute(select(commands)).mappings().all()
                == previous_commands
            )
    finally:
        engine.dispose()


@pytest.fixture
async def versions(host):
    coordinator, _ = host
    for name, features in (
        ("batch-unaware", ["project.handoff/v1"]),
        ("batch-aware", ["project.handoff/v1", WORK_BATCH_FEATURE]),
    ):
        await coordinator.stage(
            manifest(name, features=features), Path("/isolated") / name, "d" * 64
        )
    unaware = await worker(host, "batch-unaware")
    aware = await worker(host, "batch-aware")
    assert unaware is not None and aware is not None
    return coordinator, unaware, aware


async def participant(coordinator, state, *, project_id="one"):
    # The parent is a host-owned batch intent; its participant project is
    # independent of the command envelope's project.
    parent = await coordinator.coordination.submit(
        project_id="two",
        actor_id="synthetic",
        idempotency_key="batch-parent",
        name="work.batch/v1",
        payload={"projects": [project_id]},
    )
    async with coordinator.database.session() as session:
        session.add(
            RuntimeWorkBatchProjectRow(
                batch_id=parent.command_id, project_id=project_id, state=state
            )
        )
    return parent.command_id


async def owner(coordinator):
    async with coordinator.database.session() as session:
        row = await session.get(ProjectOwnershipRow, "one")
        return row.runtime_id, row.fencing_token


async def complete_participant(coordinator, batch_id):
    async with coordinator.database.session() as session:
        await session.execute(
            update(RuntimeWorkBatchProjectRow)
            .where(
                RuntimeWorkBatchProjectRow.batch_id == batch_id,
                RuntimeWorkBatchProjectRow.project_id == "one",
            )
            .values(state="complete")
        )


async def test_work_barrier_stays_at_fifo_head_while_control_and_other_project_advance(
    versions,
):
    coordinator, unaware, _ = versions
    coordination = coordinator.coordination
    lease = await coordination.claim_project("one", unaware)
    other = await coordination.claim_project("two", unaware)

    async def submit(key, name, *, lane="work", project="one"):
        return await coordination.submit(
            project_id=project,
            actor_id="synthetic",
            idempotency_key=key,
            name=name,
            payload={},
            lane=lane,
        )

    earlier = await submit("earlier", "ordinary.work")
    barrier = await submit("barrier", "work.batch.barrier/v1")
    later = await submit("later", "ordinary.work")
    approval = await submit("approval", "approval.decision", lane="control")
    assert (await coordination.claim_next(lease, lane="work")).receipt == earlier
    await coordination.finish_command(lease, earlier.command_id, result={"done": True})
    for lane in (None, "work"):
        assert await coordination.claim_next(lease, lane=lane) is None
    assert (await coordination.claim_next(lease, lane="control")).receipt == approval
    await coordination.finish_command(
        lease, approval.command_id, result={"approved": True}
    )
    assert await coordination.claim_next(lease, lane="work") is None
    independent = await submit("independent", "ordinary.work", project="two")
    assert (await coordination.claim_next(other, lane="work")).receipt == independent
    async with coordinator.database.session() as session:
        for receipt in (barrier, later):
            row = await session.get(RuntimeCommandRow, receipt.command_id)
            assert (row.state, row.runtime_id, row.fencing_token) == (
                "queued",
                None,
                None,
            )


@pytest.mark.parametrize("state", STATES)
async def test_claim_pins_only_active_participant_project(versions, state):
    coordinator, unaware, aware = versions
    await participant(coordinator, state)
    assert (
        await coordinator.coordination.claim_project("two", unaware)
    ).runtime_id == unaware
    if state in {"waiting", "barrier"}:
        with pytest.raises(CoordinationConflict, match=WORK_BATCH_FEATURE):
            await coordinator.coordination.claim_project("one", unaware)
        assert (
            await coordinator.coordination.claim_project("one", aware)
        ).runtime_id == aware
    else:
        assert (
            await coordinator.coordination.claim_project("one", unaware)
        ).runtime_id == unaware


@pytest.mark.parametrize("state", STATES)
async def test_transfer_rechecks_late_batch_and_terminal_rows_unpin(versions, state):
    coordinator, unaware, aware = versions
    lease = await coordinator.coordination.claim_project("one", aware)
    await coordinator.request_handoff("one", unaware)
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    batch_id = await participant(coordinator, state)
    if state in {"waiting", "barrier"}:
        with pytest.raises(CoordinationConflict, match=WORK_BATCH_FEATURE):
            await coordinator.transfer("one")
        assert await owner(coordinator) == (aware, lease.fencing_token)
        await complete_participant(coordinator, batch_id)
    transferred = await coordinator.transfer("one")
    assert (transferred.runtime_id, transferred.fencing_token) == (
        unaware,
        lease.fencing_token + 1,
    )


@pytest.mark.parametrize("state", STATES)
async def test_expired_recovery_requires_active_batch_support_and_terminal_unpins(
    versions, state
):
    coordinator, unaware, aware = versions
    lease = await coordinator.coordination.claim_project("one", aware)
    batch_id = await participant(coordinator, state)
    async with coordinator.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "one")
            .values(lease_expires_at_ms=0)
        )

    async def recover():
        return await coordinator.begin_recovery(
            "one",
            unaware,
            expected_owner=lease,
            expected_handoff_revision=None,
            operation_id="batch-recovery",
            actor_id="synthetic",
        )

    if state in {"waiting", "barrier"}:
        with pytest.raises(CoordinationConflict, match=WORK_BATCH_FEATURE):
            await recover()
        assert await owner(coordinator) == (aware, lease.fencing_token)
        await complete_participant(coordinator, batch_id)
    recovered = await recover()
    assert (recovered.runtime_id, recovered.fencing_token) == (
        unaware,
        lease.fencing_token + 1,
    )


async def test_execution_scope_cannot_read_or_write_another_project_batch(
    execution,
):
    control, runtime, coordination, lease = execution
    parent = await coordination.submit(
        project_id="mine",
        actor_id="synthetic",
        idempotency_key="batch",
        name="work.batch/v1",
        payload={},
    )
    second = await coordination.submit(
        project_id="mine",
        actor_id="synthetic",
        idempotency_key="other-batch",
        name="work.batch/v1",
        payload={},
    )
    async with control.database.session() as session:
        session.add_all(
            [
                RuntimeWorkBatchProjectRow(
                    batch_id=parent.command_id, project_id=project, state="waiting"
                )
                for project in ("mine", "other")
            ]
        )
    with execution_context(lease):
        async with runtime.database.session() as session:
            assert list(
                await session.scalars(select(RuntimeWorkBatchProjectRow.project_id))
            ) == ["mine"]
            assert WORK_BATCH_FEATURE in await required_context_features(
                session, "mine"
            )
            assert WORK_BATCH_FEATURE not in await required_context_features(
                session, "other"
            )
            result = await session.execute(
                update(RuntimeWorkBatchProjectRow)
                .where(RuntimeWorkBatchProjectRow.project_id == "other")
                .values(state="failed")
            )
            assert result.rowcount == 0
        with pytest.raises(LeaseLost):
            async with runtime.database.session() as session:
                session.add(
                    RuntimeWorkBatchProjectRow(
                        batch_id=second.command_id, project_id="other", state="waiting"
                    )
                )
                await session.flush()
    async with control.database.session() as session:
        assert (
            await session.get(RuntimeWorkBatchProjectRow, (parent.command_id, "other"))
        ).state == "waiting"
        assert (
            await session.get(RuntimeWorkBatchProjectRow, (second.command_id, "other"))
            is None
        )


@pytest.mark.parametrize(
    "demand",
    ["read-routing", "legacy-normal-command", "chat-start", "legacy-typed-chat"],
)
async def test_project_admission_requires_an_exact_chat_start(
    admission, tmp_path, demand
):
    value = admission
    database = value.coordinator.database
    async with database.session() as session:
        session.add(
            ProjectOwnershipRow(
                project_id="one",
                runtime_id=None,
                fencing_token=7,
                lease_expires_at_ms=0,
                command_sequence=1,
                admission_requested=False,
            )
        )
        session.add(
            RuntimeCommandRow(
                id="metadata-barrier",
                project_id="one",
                actor_id="synthetic",
                idempotency_key="metadata-barrier",
                sequence=1,
                name="work.batch.barrier/v1",
                lane="work",
                payload={},
                payload_hash="a" * 64,
                accepted_at_ms=1,
                state="queued",
            )
        )
    await value.automatic.reconcile()
    assert value.calls == [] and value.leases == {}
    async with database.session() as session:
        counter = await session.get(ProjectOwnershipRow, "one")
        assert (
            counter.runtime_id,
            counter.fencing_token,
            counter.admission_requested,
        ) == (None, 7, False)
    if demand == "read-routing":
        routing = RuntimeRouting(
            value.coordinator.coordination,
            tmp_path / "credentials",
            request_admission=True,
        )
        with pytest.raises(RuntimeIpcError, match="no live execution owner"):
            await routing.upstream_for(
                {
                    "method": "GET",
                    "path": "/api/v1/engagements/one",
                    "query_string": b"",
                }
            )
        async with database.session() as session:
            assert (
                await session.get(ProjectOwnershipRow, "one")
            ).admission_requested is False
    elif demand in {"legacy-normal-command", "chat-start", "legacy-typed-chat"}:
        async with database.session() as session:
            session.add(
                RuntimeCommandRow(
                    id="ordinary-request",
                    project_id="one",
                    actor_id="synthetic",
                    idempotency_key="ordinary-request",
                    sequence=2,
                    name=(
                        "chat.accept/v1"
                        if demand == "legacy-typed-chat"
                        else "http.invoke/v1"
                    ),
                    lane="work",
                    payload={"format": 1, "request_reference": "synthetic-unexecuted"},
                    payload_hash="b" * 64,
                    accepted_at_ms=2,
                    state="queued",
                    chat_start=demand == "chat-start",
                )
            )
            (await session.get(ProjectOwnershipRow, "one")).command_sequence = 2
        async with database.session() as session:
            assert (
                await session.get(ProjectOwnershipRow, "one")
            ).admission_requested is False
    await value.automatic.reconcile()
    should_admit = demand in {"chat-start", "legacy-typed-chat"}
    assert len(value.calls) == int(should_admit)
    assert set(value.leases) == ({"one"} if should_admit else set())
    if should_admit:
        assert value.leases["one"].fencing_token == 8
    await value.automatic.reconcile()
    assert len(value.calls) == int(should_admit)
    async with database.session() as session:
        assert (
            await session.get(RuntimeCommandRow, "metadata-barrier")
        ).state == "queued"
        assert await session.get(ProjectOwnershipRow, "two") is None
