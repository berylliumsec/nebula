"""Real connection isolation of existing ORM and raw ledger write paths."""

import asyncio
import time

import pytest
from fastapi import FastAPI
from sqlalchemy import func, insert, literal, select, text, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from nebula.v3.database import (
    Database,
    EntityRow,
    ProjectOwnershipRow,
    ProviderTurnQueueRow,
    SearchDocumentRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import (
    ChatGoal,
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
)
from nebula.v3.chat_goals import ChatGoalService, GoalConversationCreate
from nebula.v3.chat_queue import ChatQueueService
from nebula.v3.execution_scope import ExecutionSQLScope, execution_context
from nebula.v3.provider_scheduler import ProviderScheduler
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LeaseLost,
    RuntimeCoordination,
)
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from nebula.v3.chat import ChatService
from nebula.v3.temporary_chat import temporary_chat_router
from tests.v3.async_test_support import AsyncTestClient
from tests.v3.test_runtime_coordination import location as location
from unittest.mock import Mock

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


async def test_execution_worker_can_start_chat_under_lease_and_retirement_hold(
    execution,
):
    control, runtime, coordinator, lease = execution
    retirement = DeploymentCoordinator(control.database)
    service = ChatGoalService(runtime)
    body = GoalConversationCreate(
        engagement_id="mine",
        provider_id="provider",
        model="inert",
        objective="Create a project goal chat",
        completion_criteria=["The chat and draft goal are saved."],
    )
    with execution_context(lease):
        created = await service.create_conversation(body)

    assert (await control.get(ChatSession, created.session.id)).engagement_id == "mine"
    assert (
        await control.get(ChatGoal, created.goal.id)
    ).session_id == created.session.id
    assert await retirement.request_project_retirement(lease) is False

    await control.create(Engagement(id="retiring", name="Retiring"))
    retiring_lease = await coordinator.claim_project("retiring", "execution")
    assert await retirement.request_project_retirement(retiring_lease) is True
    retiring_body = body.model_copy(update={"engagement_id": "retiring"})
    with execution_context(retiring_lease):
        with pytest.raises(ConflictError, match="worker is closing"):
            await service.create_conversation(retiring_body)

    with pytest.raises(ConflictError, match="worker is closing"):
        await control.create(
            ChatSession(
                id="host-chat-during-retirement",
                engagement_id="retiring",
                title="Rejected during retirement",
                model="inert",
                provider_profile_id="provider",
            )
        )
    assert await control.find_entities(ChatSession, {}, engagement_id="retiring") == []


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


async def test_filtered_count_retains_mapped_project_scope(execution):
    control, runtime, _, _ = execution
    assert await control.count_entities(ChatTurn, {"model": "inert"}) == 2
    assert await runtime.count_entities(ChatTurn, {"model": "inert"}) == 1
    assert (
        await runtime.count_entities(
            ChatTurn, {"model": "inert"}, engagement_id="other"
        )
        == 0
    )


@pytest.mark.parametrize("activity", ["idle", "provider", "harness", "recovery"])
async def test_chat_deletion_retains_execution_scope_and_activity_guards(
    execution, activity
):
    control, runtime, _, lease = execution
    for project in ("mine", "other"):
        await control.create(
            ChatSession(
                id=f"chat-{project}",
                engagement_id=project,
                title="Synthetic conversation",
                model="inert",
                provider_profile_id="provider",
                metadata={"side_chat": True},
            )
        )
    turn = await control.get(ChatTurn, "turn-mine")
    if activity == "provider":
        turn.status = ChatTurnStatus.ROUTING
    elif activity == "recovery":
        turn.status = ChatTurnStatus.INTERRUPTED
        turn.request_snapshot["recovery"] = {"required": True}
    else:
        turn.status = ChatTurnStatus.COMPLETE
    await control.update(
        ChatTurn,
        turn.id,
        {"status": turn.status, "request_snapshot": turn.request_snapshot},
    )
    if activity == "harness":
        await control.create(
            HarnessTurn(
                id="active-harness",
                engagement_id="mine",
                harness_session_id="vendor",
                origin=HarnessTurnOrigin.CHAT,
                chat_session_id="chat-mine",
                chat_turn_id=turn.id,
                prompt="Inert fixture",
                status=HarnessTurnStatus.RUNNING,
            )
        )
    with execution_context(lease):
        with pytest.raises(NotFoundError):
            await runtime.delete_chat_session("chat-other")
        if activity == "idle":
            await runtime.validate_chat_session_delete("chat-mine")
            await runtime.delete_chat_session("chat-mine")
            with pytest.raises(NotFoundError):
                await control.get(ChatSession, "chat-mine")
            with pytest.raises(NotFoundError):
                await control.get(ChatTurn, turn.id)
        else:
            message = (
                "harness turn is active"
                if activity == "harness"
                else "response is active"
            )
            for operation in (
                runtime.validate_chat_session_delete,
                runtime.delete_chat_session,
            ):
                with pytest.raises(ConflictError, match=message):
                    await operation("chat-mine")
            assert (await control.get(ChatSession, "chat-mine")).id == "chat-mine"
            assert (await control.get(ChatTurn, turn.id)).status == turn.status
    assert (await control.get(ChatSession, "chat-other")).id == "chat-other"
    assert (await control.get(ChatTurn, "turn-other")).id == "turn-other"


async def test_side_chat_discard_route_on_execution_worker_preserves_parent(execution):
    control, runtime, _, lease = execution
    await control.create(
        ChatSession(
            id="parent",
            engagement_id="mine",
            title="Parent",
            model="inert",
            provider_profile_id="provider",
        )
    )
    await control.create(
        ChatMessage(
            id="parent-message",
            engagement_id="mine",
            session_id="parent",
            sequence=1,
            role="user",
            content="Keep this history",
        )
    )
    side = await ChatService(control).fork_session(
        "parent", through_message_id="parent-message", side_chat=True
    )
    app = FastAPI()
    app.include_router(
        temporary_chat_router(runtime, lambda: ChatService(runtime), Mock())
    )
    client = AsyncTestClient(app)
    with execution_context(lease):
        response = await client.delete(f"/chat/temporary-sessions/{side.id}")
        assert response.status_code == 204, response.text
        assert (
            await client.delete(f"/chat/temporary-sessions/{side.id}")
        ).status_code == 204
        assert (await client.get("/chat/sessions/parent/side-chats")).json() == []
    with pytest.raises(NotFoundError):
        await control.get(ChatSession, side.id)
    assert (await control.get(ChatSession, "parent")).id == "parent"
    assert (
        await control.get(ChatMessage, "parent-message")
    ).content == "Keep this history"


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


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("one_turn", [False, True])
async def test_worker_settles_only_its_projects_ended_admissions(
    execution, scoped, one_turn
):
    """An execution worker closes admissions of turns that ended while waiting.

    Its recovery pass used to be refused by the execution scope on every run,
    leaving ended turns queued: they held queue positions and blocked project
    retirement and Core shutdown.
    """

    control, runtime, _, lease = execution
    await control.create(
        ChatTurn(
            id="turn-mine-waiting",
            engagement_id="mine",
            session_id="chat-mine",
            model="inert",
            provider_profile_id="provider",
        )
    )
    async with control.database.session() as session:
        for turn in ("turn-mine", "turn-other", "turn-mine-waiting"):
            await session.execute(
                insert(ProviderTurnQueueRow).values(
                    turn_id=turn, lane="direct", state="queued"
                )
            )
    for turn in ("turn-mine", "turn-other"):
        current = await control.get(ChatTurn, turn)
        await control.update(
            ChatTurn,
            turn,
            {"status": ChatTurnStatus.CANCELLED},
            expected_revision=current.revision,
        )
    retirement = DeploymentCoordinator(control.database)
    async with control.database.session() as session:
        assert "Provider turn turn-mine is admitted or queued" in (
            await retirement._shutdown_blockers(session, "mine")
        )
    scheduler = ProviderScheduler(runtime, worker_id="execution")

    async def settle(turn_id):
        if not scoped:
            return await scheduler.settle(turn_id)
        with execution_context(lease):
            return await scheduler.settle(turn_id)

    assert await settle("turn-other") == []
    assert await settle("turn-mine" if one_turn else None) == ["turn-mine"]
    assert await settle(None) == []
    async with control.database.session() as session:
        states = dict(
            (
                await session.execute(
                    select(ProviderTurnQueueRow.turn_id, ProviderTurnQueueRow.state)
                )
            ).all()
        )
        blockers = await retirement._shutdown_blockers(session, "mine")
    assert states == {
        "turn-mine": "cancelled",
        "turn-other": "queued",
        "turn-mine-waiting": "queued",
    }
    assert "Provider turn turn-mine is admitted or queued" not in blockers
    assert "Provider turn turn-mine-waiting is admitted or queued" in blockers


async def test_interrupted_worker_statements_keep_their_own_exception(execution):
    """Cancelling or timing out a worker query mid-statement stays what it was.

    SQLAlchemy invalidates a connection whose statement a cancellation
    interrupts. The scope's rollback listener read its info anyway and raised
    PendingRollbackError in place of the cancellation or timeout.
    """

    _, runtime, _, lease = execution
    if runtime.database.engine.dialect.name != "postgresql":
        pytest.skip("SQLite cannot hold a statement open to interrupt it")
    started = asyncio.Event()

    async def wait_in_sql():
        with execution_context(lease):
            async with runtime.database.session() as session:
                started.set()
                await session.execute(text("SELECT pg_sleep(30)"))

    task = asyncio.create_task(wait_in_sql())
    await started.wait()
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            async with runtime.database.session() as session:
                await session.execute(text("SELECT pg_sleep(30)"))
    # The pool reconnects and the next transaction is scoped again.
    assert [item.id for item in await runtime.list_entities(Engagement)] == ["mine"]


async def test_chat_follow_ups_stop_cleanly_while_their_poll_waits(execution):
    """Core shutdown cancels the follow-up poll even while it waits on SQL.

    The cancellation surfaced as PendingRollbackError, so the worker recorded
    its cleanup as failed, kept the project and refused every later shutdown.
    """

    control, runtime, _, _ = execution
    if runtime.database.engine.dialect.name != "postgresql":
        pytest.skip("SQLite cannot hold a statement open to interrupt it")
    service = ChatQueueService(runtime, None, None)
    async with control.database.engine.connect() as blocker:
        await blocker.execute(text("LOCK TABLE entities IN ACCESS EXCLUSIVE MODE"))
        service.task = asyncio.create_task(service.run())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = await blocker.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE NOT granted "
                    "AND relation = 'entities'::regclass"
                )
            )
            if waiting:
                break
            await asyncio.sleep(0.1)
        assert waiting, "the follow-up poll never reached its read"
        await service.shutdown()
        assert service.task.cancelled()
        await blocker.rollback()
    assert [item.id for item in await runtime.list_entities(Engagement)] == ["mine"]


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
