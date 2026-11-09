"""Real connection isolation of existing ORM and raw ledger write paths."""

import time

import pytest
from sqlalchemy import func, insert, literal, select, text, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from nebula.v3.database import (
    Database,
    EntityRow,
    ProjectOwnershipRow,
    SearchDocumentRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import ChatTurn, Engagement, ProviderProfile
from nebula.v3.execution_scope import ExecutionSQLScope, execution_context
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LeaseLost,
    RuntimeCoordination,
)
from nebula.v3.storage import NebulaStore, NotFoundError
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def execution(location):
    control = NebulaStore(Database(location, pool_settings=PoolSettings(5, 12, 2, 5)))
    runtime = None
    try:
        for project in ("mine", "other"):
            await control.create(Engagement(id=project, name=project))
            await control.create(
                ChatTurn(
                    id=f"turn-{project}",
                    engagement_id=project,
                    session_id=f"chat-{project}",
                    model="inert",
                    provider_profile_id="provider",
                )
            )
        await control.create(
            ProviderProfile(
                id="provider", name="Inert provider", provider_type="openrouter"
            )
        )
        coordinator = RuntimeCoordination(control.database)
        await coordinator.register_runtime("execution", "fixture")
        lease = await coordinator.claim_project("mine", "execution")
        runtime = NebulaStore(
            Database(
                location,
                bootstrap=False,
                execution_runtime_id="execution",
                pool_settings=PoolSettings(5, 12, 2, 5),
            )
        )
        yield control, runtime, coordinator, lease
    finally:
        if runtime:
            await runtime.event_history.close()
            await runtime.database.dispose()
        await control.event_history.close()
        await control.database.dispose()


async def test_existing_read_helpers_only_see_owned_projects(execution):
    _, runtime, _, _ = execution
    assert [item.id for item in await runtime.list_entities(Engagement)] == ["mine"]
    assert [item.id for item in await runtime.list_entities(ChatTurn)] == ["turn-mine"]
    with pytest.raises(NotFoundError):
        await runtime.get(ChatTurn, "turn-other")
    assert (await runtime.get(ProviderProfile, "provider")).name == "Inert provider"


async def test_column_only_reads_and_raw_connections_are_scoped(execution):
    _, runtime, _, _ = execution
    async with runtime.database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EntityRow)
                .where(EntityRow.kind == "chat_turns")
            )
            == 1
        )
    async with runtime.database.connection() as connection:
        assert list(
            await connection.scalars(
                select(EntityRow.id).where(EntityRow.kind == "chat_turns")
            )
        ) == ["turn-mine"]


async def test_first_raw_statement_loads_current_transaction_authority(execution):
    _, runtime, _, _ = execution
    await runtime.database.initialize()
    async with runtime.database.connection() as connection:
        assert list(
            await connection.scalars(
                select(EntityRow.id).where(EntityRow.kind == "chat_turns")
            )
        ) == ["turn-mine"]


async def test_task_context_narrows_an_existing_transaction(execution):
    _, runtime, coordinator, lease = execution
    await coordinator.claim_project("other", "execution")
    async with runtime.database.session() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(EntityRow.id).where(EntityRow.kind == "chat_turns")
                    )
                )
            )
            == 2
        )
        with execution_context(lease):
            assert list(
                await session.scalars(
                    select(EntityRow.id).where(EntityRow.kind == "chat_turns")
                )
            ) == ["turn-mine"]
            changed = await session.execute(
                update(EntityRow)
                .where(EntityRow.id == "turn-other")
                .values(revision=77)
            )
            assert changed.rowcount == 0


@pytest.mark.parametrize(
    "kind",
    ["table", "text", "driver", "compound", "mixed", "unmapped", "alias", "subquery"],
)
async def test_unscoped_query_forms_fail_closed(execution, kind):
    _, runtime, _, _ = execution
    with pytest.raises(CoordinationConflict):
        async with runtime.database.connection() as connection:
            if kind == "table":
                await connection.execute(select(EntityRow.__table__))
            elif kind == "text":
                await connection.execute(text("SELECT * FROM entities"))
            elif kind == "mixed":
                await connection.execute(
                    select(EntityRow.id, SearchDocumentRow.__table__.c.id)
                )
            elif kind == "unmapped":
                from nebula.v3.application_model.persistence import graphs

                await connection.execute(select(graphs))
            elif kind == "compound":
                await connection.execute(
                    union_all(select(EntityRow.id), select(EntityRow.id))
                )
            elif kind == "alias":
                await connection.execute(select(EntityRow.__table__.alias()))
            elif kind == "subquery":
                await connection.execute(select(select(EntityRow.__table__).subquery()))
            else:
                await connection.exec_driver_sql("SELECT * FROM entities")


@pytest.mark.parametrize("kind", ["multi", "from_select", "upsert"])
async def test_unreviewed_insert_shapes_fail_closed(execution, kind):
    control, runtime, _, _ = execution
    turn = await control.get(ChatTurn, "turn-mine")
    values = dict(
        id="unscoped",
        kind="chat_turns",
        engagement_id="other",
        revision=1,
        payload=turn.model_copy(
            update={"id": "unscoped", "engagement_id": "other"}
        ).model_dump(mode="json"),
        created_at=turn.created_at,
        updated_at=turn.updated_at,
    )
    if kind == "multi":
        statement = insert(EntityRow).values([values])
    elif kind == "from_select":
        statement = insert(EntityRow).from_select(["id"], select(literal("unscoped")))
    else:
        factory = (
            pg_insert
            if runtime.database.engine.dialect.name == "postgresql"
            else sqlite_insert
        )
        statement = (
            factory(EntityRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["id"], set_={"engagement_id": "other"}
            )
        )
    with pytest.raises(CoordinationConflict):
        async with runtime.database.connection() as connection:
            await connection.execute(statement)
    with pytest.raises(NotFoundError):
        await control.get(ChatTurn, "unscoped")


@pytest.mark.parametrize("kind", ["column", "payload", "ordered"])
async def test_updates_cannot_reparent_owned_records(execution, kind):
    control, runtime, _, _ = execution
    turn = await control.get(ChatTurn, "turn-mine")
    values = (
        {"engagement_id": "other"}
        if kind in {"column", "ordered"}
        else {
            "payload": turn.model_copy(update={"engagement_id": "other"}).model_dump(
                mode="json"
            )
        }
    )
    with pytest.raises(CoordinationConflict):
        async with runtime.database.connection() as connection:
            statement = update(EntityRow).where(EntityRow.id == turn.id)
            statement = (
                statement.ordered_values((EntityRow.engagement_id, "other"))
                if kind == "ordered"
                else statement.values(**values)
            )
            await connection.execute(statement)
            await connection.commit()
    assert (await control.get(ChatTurn, turn.id)).engagement_id == "mine"


async def test_first_raw_write_still_checks_expiry_at_commit(execution, monkeypatch):
    control, runtime, _, _ = execution
    clock = [int(time.time() * 1000)]
    monkeypatch.setattr(
        ExecutionSQLScope, "_clock_sql", staticmethod(lambda _: str(clock[0]))
    )
    with pytest.raises(LeaseLost):
        async with runtime.database.connection() as connection:
            await connection.execute(
                update(EntityRow).where(EntityRow.id == "turn-mine").values(revision=77)
            )
            clock[0] += 60_000
            await connection.commit()
    async with control.database.session() as session:
        assert (
            await session.scalar(
                select(EntityRow.revision).where(EntityRow.id == "turn-mine")
            )
            == 1
        )


async def test_existing_entity_updates_keep_project_fencing(execution):
    control, runtime, _, lease = execution
    with execution_context(lease):
        await runtime.update(
            Engagement, "mine", {"name": "Changed"}, expected_revision=1
        )
    assert (await control.get(Engagement, "mine")).name == "Changed"
    with pytest.raises(NotFoundError):
        await runtime.update(
            Engagement, "other", {"name": "wrong"}, expected_revision=1
        )
    assert (await control.get(Engagement, "other")).name == "other"


async def test_existing_raw_event_writer_is_fenced(execution):
    control, runtime, _, lease = execution
    with execution_context(lease):
        event = await runtime.append_event("turn-mine", "fixture", {"ok": True})
    assert event.sequence == 1
    assert len(await control.replay_events("turn-mine")) == 1
    with pytest.raises(LeaseLost):
        await runtime.append_event("turn-other", "wrong", {})
    assert await control.replay_events("turn-other") == []


async def test_recovery_query_handles_derived_columns_and_keeps_project_scope(
    execution,
):
    control, runtime, _, lease = execution
    for project in ("mine", "other"):
        await control.append_operation_event(
            f"live-{project}", "container_terminal", project, "started", {}
        )
        await control.append_operation_event(
            f"done-{project}", "container_terminal", project, "started", {}
        )
        await control.append_operation_event(
            f"done-{project}", "container_terminal", project, "settled", {}
        )
    with execution_context(lease):
        pending = await runtime.unsettled_operation_events(
            "container_terminal", "settled"
        )
    assert [(item.operation_id, item.engagement_id) for item in pending] == [
        ("live-mine", "mine")
    ]


async def test_insert_cannot_escape_to_another_project(execution):
    control, runtime, _, _ = execution
    with pytest.raises(LeaseLost):
        await runtime.create(
            ChatTurn(
                id="wrong",
                engagement_id="other",
                session_id="chat-other",
                model="inert",
                provider_profile_id="provider",
            )
        )
    with pytest.raises(NotFoundError):
        await control.get(ChatTurn, "wrong")


async def test_stale_task_token_is_not_replaced_by_current_ownership(execution):
    control, runtime, _, lease = execution
    async with control.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "mine")
            .values(fencing_token=2)
        )
    with pytest.raises(LeaseLost):
        with execution_context(lease):
            await runtime.update(
                Engagement, "mine", {"name": "stale"}, expected_revision=1
            )
    assert (await control.get(Engagement, "mine")).name == "mine"


async def test_expired_worker_cannot_write_anything(execution):
    control, runtime, _, _ = execution
    async with control.database.session() as session:
        await session.execute(
            update(ProjectOwnershipRow)
            .where(ProjectOwnershipRow.project_id == "mine")
            .values(lease_expires_at_ms=0)
        )
    with pytest.raises((LeaseLost, NotFoundError)):
        await runtime.update(
            Engagement, "mine", {"name": "expired"}, expected_revision=1
        )
    assert (await control.get(Engagement, "mine")).name == "mine"


def test_execution_worker_cannot_bootstrap_schema(tmp_path):
    with pytest.raises(ValueError, match="without migrating"):
        Database(tmp_path / "forbidden.db", execution_runtime_id="worker")
