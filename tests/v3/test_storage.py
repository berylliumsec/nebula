from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from nebula.v3.database import (
    CURRENT_SCHEMA_VERSION,
    Database,
    OperationEventRow,
    RunEventRow,
    RunBudgetCounterRow,
    SchemaVersionError,
    SchemaVersionRow,
)
from nebula.v3.domain import (
    AgentRun,
    Asset,
    BrowserAutomationLease,
    BrowserCommand,
    BrowserProxyRule,
    ChatBackend,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    Engagement,
    EngagementStatus,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    NativeCheckpoint,
    NativeHookExecution,
    ProviderProfile,
    RiskClass,
    RunBackend,
    RunBudget,
    RunStatus,
    ToolCall,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.storage import (
    ConflictError,
    NebulaStore,
    NotFoundError,
    RunBudgetExceededError,
)


@pytest.fixture
async def store(tmp_path):
    store = NebulaStore(Database(tmp_path / "nebula.db"))
    await store.database.initialize()
    try:
        yield store
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def test_sqlite_bootstrap_enables_wal_and_schema_version(store):
    health = await store.database.health()
    assert health == {
        "database": "ok",
        "dialect": "sqlite",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "journal_mode": "wal",
    }


async def test_sqlite_database_and_wal_files_are_private(tmp_path):
    path = tmp_path / "private" / "nebula.db"
    database = Database(path)
    await database.initialize()

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(f"{path}-wal").stat().st_mode & 0o777 == 0o600
    assert Path(f"{path}-shm").stat().st_mode & 0o777 == 0o600

    (await database.dispose())


async def test_schema_bootstrap_is_idempotent_and_refuses_a_newer_database(tmp_path):
    path = tmp_path / "versioned.db"
    first = Database(path)
    assert (await first.current_schema_version()) == CURRENT_SCHEMA_VERSION
    async with first.session() as session:
        marker_count = await session.scalar(
            select(func.count()).select_from(SchemaVersionRow)
        )
    (await first.dispose())

    reopened = Database(path)
    async with reopened.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SchemaVersionRow))
            == marker_count
        )
    async with reopened.engine.begin() as connection:
        (
            await connection.execute(
                SchemaVersionRow.__table__.insert().values(
                    version=CURRENT_SCHEMA_VERSION + 1,
                    applied_at=utc_now(),
                )
            )
        )
    (await reopened.dispose())

    with pytest.raises(SchemaVersionError, match="newer than supported"):
        await Database(path).initialize()


async def test_typed_crud_and_optimistic_revision(store):
    engagement = await store.create(Engagement(name="Acme"))
    assert (await store.get(Engagement, engagement.id)) == engagement

    updated = await store.update(
        Engagement,
        engagement.id,
        {"description": "External assessment"},
        expected_revision=1,
    )
    assert updated.description == "External assessment"
    assert updated.revision == 2
    with pytest.raises(ConflictError):
        (
            await store.update(
                Engagement,
                engagement.id,
                {"description": "stale"},
                expected_revision=1,
            )
        )
    with pytest.raises(ValueError):
        (await store.update(Engagement, engagement.id, {"id": "different"}))

    with pytest.raises(ConflictError, match="revision conflict"):
        (await store.delete(Engagement, engagement.id, expected_revision=1))

    (await store.delete(Engagement, engagement.id, expected_revision=updated.revision))
    with pytest.raises(NotFoundError):
        (await store.get(Engagement, engagement.id))


async def test_transaction_rolls_back_every_entity(store):
    engagement = Engagement(name="Rollback")
    asset = Asset(engagement_id=engagement.id, name="10.0.0.1")
    with pytest.raises(RuntimeError):
        async with store.transaction() as transaction:
            (await transaction.add(engagement))
            (await transaction.add(asset))
            raise RuntimeError("abort")
    assert (await store.count(Engagement)) == 0
    assert (await store.count(Asset)) == 0


async def test_event_ledger_sequences_replays_and_deduplicates(store):
    first = await store.append_event(
        "run-1", "run.created", {"objective": "test"}, idempotency_key="create"
    )
    retried = await store.append_event(
        "run-1", "run.created", {"objective": "test"}, idempotency_key="create"
    )
    second = await store.append_event("run-1", "task.created", {"task_id": "t-1"})
    assert retried == first
    assert second.sequence == 2
    assert [event.sequence for event in (await store.replay_events("run-1"))] == [1, 2]
    assert [
        event.sequence
        for event in (await store.replay_events("run-1", after_sequence=1))
    ] == [2]
    with pytest.raises(ConflictError, match="idempotency key"):
        (
            await store.append_event(
                "run-1", "run.created", {"different": True}, idempotency_key="create"
            )
        )


async def test_event_sequence_is_atomic_between_tasks(store):
    async def append(number):
        return (
            await store.append_event("parallel", "tick", {"number": number})
        ).sequence

    sequences = await asyncio.gather(*(append(number) for number in range(20)))
    assert sorted(sequences) == list(range(1, 21))


async def test_execution_and_artifact_query_budgets_are_independent_and_atomic(store):
    engagement = await store.create(Engagement(name="Independent budgets"))
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="exercise both counters",
            budget=RunBudget(max_tool_calls=1, max_artifact_queries=1),
        )
    )

    async def reserve(call_id: str, budget_class: str) -> str:
        call = ToolCall(
            id=call_id,
            engagement_id=engagement.id,
            run_id=run.id,
            tool_name=(
                "tool_output.search"
                if budget_class == "artifact_query"
                else "nmap.scan"
            ),
            risk_class=RiskClass.LOCAL_READ,
            metadata={"budget_class": budget_class},
        )
        try:
            return (await store.reserve_tool_call(call)).id
        except RunBudgetExceededError:
            return "exhausted"

    outcomes = await asyncio.gather(
        *(
            reserve(*item)
            for item in [
                ("action-a", "execution"),
                ("action-b", "execution"),
                ("query-a", "artifact_query"),
                ("query-b", "artifact_query"),
            ]
        )
    )
    assert outcomes.count("exhausted") == 2
    assert len([item for item in outcomes if item.startswith("action-")]) == 1
    assert len([item for item in outcomes if item.startswith("query-")]) == 1
    async with store.database.session() as session:
        counter = await session.get(RunBudgetCounterRow, run.id)
        assert counter is not None
        assert counter.tool_calls == 1
        assert counter.artifact_queries == 1


async def test_unlimited_mission_budget_accepts_execution_and_artifact_calls(store):
    engagement = await store.create(Engagement(name="Unlimited mission budget"))
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="exercise unlimited counters",
            budget=RunBudget(),
        )
    )

    for index in range(3):
        (
            await store.reserve_tool_call(
                ToolCall(
                    id=f"unlimited-action-{index}",
                    engagement_id=engagement.id,
                    run_id=run.id,
                    tool_name="nmap.scan",
                    risk_class=RiskClass.LOCAL_READ,
                )
            )
        )
        (
            await store.reserve_tool_call(
                ToolCall(
                    id=f"unlimited-query-{index}",
                    engagement_id=engagement.id,
                    run_id=run.id,
                    tool_name="tool_output.search",
                    risk_class=RiskClass.LOCAL_READ,
                    metadata={"budget_class": "artifact_query"},
                )
            )
        )

    async with store.database.session() as session:
        counter = await session.get(RunBudgetCounterRow, run.id)
        assert counter is not None
        assert counter.tool_calls == 3
        assert counter.artifact_queries == 3


async def test_create_with_event_is_atomic_when_event_conflicts(store):
    (
        await store.append_event(
            "run-1",
            "run.started",
            {"objective": "first"},
            idempotency_key="run:started",
        )
    )
    engagement = Engagement(name="Must roll back")

    with pytest.raises(ConflictError):
        (
            await store.create_with_event(
                engagement,
                run_id="run-1",
                event_type="run.started",
                event_payload={"objective": "second"},
                idempotency_key="run:started",
            )
        )

    with pytest.raises(NotFoundError):
        (await store.get(Engagement, engagement.id))


async def test_update_with_event_is_atomic_and_idempotent(store):
    engagement = await store.create(Engagement(name="Atomic transitions"))
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="exercise transition retries",
        )
    )
    changes = {"status": RunStatus.RUNNING}
    event_payload = {"status": RunStatus.RUNNING.value}

    updated, event = await store.update_with_event(
        AgentRun,
        run.id,
        changes,
        expected_revision=run.revision,
        run_id=run.id,
        event_type="run.running",
        event_payload=event_payload,
        idempotency_key="run:running",
    )
    retried, retried_event = await store.update_with_event(
        AgentRun,
        run.id,
        changes,
        expected_revision=updated.revision,
        run_id=run.id,
        event_type="run.running",
        event_payload=event_payload,
        idempotency_key="run:running",
    )

    assert retried == updated
    assert retried_event == event
    assert retried.revision == 2
    assert (await store.replay_events(run.id)) == [event]

    with pytest.raises(ConflictError, match="idempotency key"):
        (
            await store.update_with_event(
                AgentRun,
                run.id,
                {"status": RunStatus.FAILED},
                expected_revision=run.revision,
                run_id=run.id,
                event_type="run.failed",
                event_payload={"status": RunStatus.FAILED.value},
                idempotency_key="run:running",
            )
        )
    assert (await store.get(AgentRun, run.id)) == updated


async def test_orm_rejects_event_updates_and_deletes(store):
    event = await store.append_event("run-1", "immutable")
    with pytest.raises(RuntimeError, match="append-only"):
        async with store.database.session() as session:
            row = await session.get(RunEventRow, event.id)
            row.event_type = "rewritten"
            (await session.flush())
    with pytest.raises(RuntimeError, match="append-only"):
        async with store.database.session() as session:
            row = await session.get(RunEventRow, event.id)
            (await session.delete(row))
            (await session.flush())

    with pytest.raises(DBAPIError, match="append-only"):
        async with store.database.engine.begin() as connection:
            (
                await connection.exec_driver_sql(
                    "UPDATE run_events SET event_type='raw-rewrite' WHERE id=?",
                    (event.id,),
                )
            )


async def test_orm_and_database_reject_operation_event_mutation(store):
    engagement = await store.create(Engagement(name="Immutable operations"))
    event = await store.append_operation_event(
        "execution-1",
        "operator_execution",
        engagement.id,
        "execution.queued",
    )
    with pytest.raises(RuntimeError, match="append-only"):
        async with store.database.session() as session:
            row = await session.get(OperationEventRow, event.id)
            row.event_type = "rewritten"
            (await session.flush())
    with pytest.raises(RuntimeError, match="append-only"):
        async with store.database.session() as session:
            row = await session.get(OperationEventRow, event.id)
            (await session.delete(row))
            (await session.flush())

    with pytest.raises(DBAPIError, match="immutable"):
        async with store.database.engine.begin() as connection:
            (
                await connection.exec_driver_sql(
                    "UPDATE operation_events SET event_type='raw-rewrite' WHERE id=?",
                    (event.id,),
                )
            )


async def test_delete_harness_chat_removes_its_operation_events(store):
    engagement = await store.create(Engagement(name="Harness chat deletion"))
    harness_session = await store.create(
        HarnessSession(
            engagement_id=engagement.id,
            harness_profile_id="harness-1",
            model="test-model",
        )
    )
    chat = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Delete me",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-1",
            harness_session_id=harness_session.id,
            model="test-model",
        )
    )
    chat_turn = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=chat.id,
            backend=ChatBackend.HARNESS,
            model="test-model",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    harness_turn = await store.create(
        HarnessTurn(
            engagement_id=engagement.id,
            harness_session_id=harness_session.id,
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=chat.id,
            chat_turn_id=chat_turn.id,
            status=HarnessTurnStatus.COMPLETE,
            prompt="test",
        )
    )
    (
        await store.append_operation_event(
            harness_turn.id,
            "harness_turn",
            engagement.id,
            "harness.completed",
        )
    )

    (await store.delete_chat_session(chat.id))

    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, chat.id))
    with pytest.raises(NotFoundError):
        (await store.get(ChatTurn, chat_turn.id))
    with pytest.raises(NotFoundError):
        (await store.get(HarnessTurn, harness_turn.id))
    # The turn's history follows it rather than outliving the conversation.
    assert await store.event_history.wait_idle(10)
    assert (await store.replay_operation_events(harness_turn.id)) == []


async def test_overview_counts_entities_by_engagement(store):
    first = await store.create(Engagement(name="First"))
    second = await store.create(Engagement(name="Second"))
    (await store.create(Asset(engagement_id=first.id, name="one")))
    (await store.create(Asset(engagement_id=second.id, name="two")))
    overview = await store.overview(first.id)
    assert overview["counts"]["engagements"] == 1
    assert overview["counts"]["assets"] == 1


async def test_count_and_overview_agree_with_list_entities_about_temporary_chats(store):
    engagement = await store.create(Engagement(name="Popup"))
    provider = await store.create(
        ProviderProfile(name="Local", provider_type="vllm", is_local=True)
    )
    durable = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Durable",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    popup = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Ask Nebula",
            provider_profile_id=provider.id,
            model="model-a",
            metadata={"temporary_assistant": True},
        )
    )

    assert [item.id for item in (await store.list_entities(ChatSession))] == [
        durable.id
    ]
    assert (await store.count(ChatSession)) == 1
    assert (await store.count(ChatSession, engagement_id=engagement.id)) == 1
    assert (await store.overview())["counts"]["chat_sessions"] == 1
    assert (await store.overview(engagement.id))["counts"]["chat_sessions"] == 1

    listed = await store.list_entities(ChatSession, include_temporary=True)
    assert [item.id for item in listed] == [durable.id, popup.id]
    assert (await store.count(ChatSession, include_temporary=True)) == 2
    # Other kinds are unaffected by the conversation filter.
    assert (await store.overview())["counts"]["engagements"] == 1
    assert (await store.overview())["counts"]["providers"] == 1


@pytest.mark.parametrize("limit", [None, 2])
async def test_chat_budgets_persist_and_count_without_legacy_ceilings(store, limit):
    engagement = await store.create(Engagement(name="Chat budget regression"))
    turn = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id="chat-budget-session",
            provider_profile_id="fixture-provider",
            model="fixture",
            max_tool_calls=limit,
            max_artifact_queries=limit,
        )
    )
    for budget_class in ("execution", "artifact_query"):
        count = 210 if limit is None else limit
        for index in range(count):
            call = ToolCall(
                id=f"{turn.id}-{budget_class}-{index}",
                engagement_id=engagement.id,
                run_id=turn.id,
                origin=ToolCallOrigin.CHAT,
                tool_name="fixture.read",
                risk_class=RiskClass.LOCAL_READ,
                metadata={"budget_class": budget_class},
            )
            (await store.reserve_tool_call(call))
            (await store.reserve_tool_call(call))  # Retry cannot consume a second slot.
        if limit is not None:
            with pytest.raises(RunBudgetExceededError):
                (
                    await store.reserve_tool_call(
                        call.model_copy(update={"id": f"{call.id}-extra"})
                    )
                )
    reloaded = await store.get(ChatTurn, turn.id)
    assert reloaded.max_tool_calls == limit
    assert reloaded.max_artifact_queries == limit
    async with store.database.session() as session:
        counter = await session.get(RunBudgetCounterRow, turn.id)
        assert counter.tool_calls == count
        assert counter.artifact_queries == count
    restored = ChatTurn.model_validate(
        {
            **reloaded.model_dump(),
            "next_step": 420,
            "execution_tool_calls": 210,
            "artifact_queries": 210,
        }
    )
    assert restored.next_step == 420


def test_new_chat_turn_budgets_are_unlimited():
    turn = ChatTurn(
        engagement_id="project",
        session_id="session",
        model="fixture",
        provider_profile_id="fixture-provider",
    )
    assert turn.max_tool_calls is None
    assert turn.max_artifact_queries is None


async def test_delete_chat_session_removes_its_harness_session_unless_a_mission_shares_it(
    store,
):
    engagement = await store.create(Engagement(name="Harness chat cleanup"))
    vendor = await store.create(
        HarnessSession(
            engagement_id=engagement.id,
            harness_profile_id="harness-1",
            model="test-model",
        )
    )
    chat = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Delete me",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-1",
            harness_session_id=vendor.id,
            model="test-model",
        )
    )

    (await store.delete_chat_session(chat.id))

    with pytest.raises(NotFoundError):
        (await store.get(HarnessSession, vendor.id))
    # The project has nothing left that would block deleting it.
    assert (await store.engagement_has_dependents(engagement.id)) is False

    shared_engagement = await store.create(Engagement(name="Continued as a mission"))
    shared_vendor = await store.create(
        HarnessSession(
            engagement_id=shared_engagement.id,
            harness_profile_id="harness-1",
            model="test-model",
        )
    )
    shared_chat = await store.create(
        ChatSession(
            engagement_id=shared_engagement.id,
            title="Continued",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-1",
            harness_session_id=shared_vendor.id,
            model="test-model",
        )
    )
    mission = await store.create(
        AgentRun(
            engagement_id=shared_engagement.id,
            objective="Keep using the vendor session",
            backend=RunBackend.HARNESS,
            harness_profile_id="harness-1",
            harness_session_id=shared_vendor.id,
        )
    )

    (await store.delete_chat_session(shared_chat.id))

    assert (await store.get(HarnessSession, shared_vendor.id)).id == shared_vendor.id
    assert (
        await store.get(AgentRun, mission.id)
    ).harness_session_id == shared_vendor.id


async def test_delete_chat_session_removes_native_checkpoints_and_hook_executions(
    store,
):
    engagement = await store.create(Engagement(name="Native chat cleanup"))
    chat = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Edited workspace files",
            provider_profile_id="provider-1",
            model="test-model",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=chat.id,
            provider_profile_id="provider-1",
            model="test-model",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    checkpoint = await store.create(
        NativeCheckpoint(
            engagement_id=engagement.id,
            chat_session_id=chat.id,
            label="before edit",
        )
    )
    started = utc_now()
    hook = await store.create(
        NativeHookExecution(
            engagement_id=engagement.id,
            chat_session_id=chat.id,
            chat_turn_id=turn.id,
            hook_id="hook-1",
            hook_snapshot={"command": "lint"},
            event_name="after_tool",
            status="complete",
            started_at=started,
            completed_at=started,
        )
    )

    (await store.delete_chat_session(chat.id))

    with pytest.raises(NotFoundError):
        (await store.get(NativeCheckpoint, checkpoint.id))
    with pytest.raises(NotFoundError):
        (await store.get(NativeHookExecution, hook.id))
    # Nothing invisible is left to block deleting the project directly.
    assert (await store.engagement_has_dependents(engagement.id)) is False


async def test_delete_run_removes_browser_automation_records_and_context_snapshots(
    store,
):
    engagement = await store.create(Engagement(name="Browser mission cleanup"))
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Browse the target",
            status=RunStatus.COMPLETE,
        )
    )
    other_run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Keep browsing",
            status=RunStatus.RUNNING,
        )
    )
    expires_at = utc_now() + timedelta(hours=1)

    def lease_for(run_id: str) -> BrowserAutomationLease:
        return BrowserAutomationLease(
            engagement_id=engagement.id,
            run_id=run_id,
            session_id="browser-session-1",
            identity_id="identity-1",
            scope_policy_id="scope-1",
            scope_policy_revision=1,
            target_urls=["https://target.example.test/"],
            allowed_risk_classes=[RiskClass.LOCAL_READ],
            expires_at=expires_at,
        )

    lease = await store.create(lease_for(run.id))
    other_lease = await store.create(lease_for(other_run.id))
    command = await store.create(
        BrowserCommand(
            engagement_id=engagement.id,
            run_id=run.id,
            lease_id=lease.id,
            session_id="browser-session-1",
            tab_id="tab-1",
            kind="navigate",
            expires_at=expires_at,
        )
    )
    rule = await store.create(
        BrowserProxyRule(
            engagement_id=engagement.id,
            run_id=run.id,
            lease_id=lease.id,
            session_id="browser-session-1",
            expires_at=expires_at,
        )
    )
    snapshot = await store.create(
        ContextSnapshot(
            engagement_id=engagement.id,
            owner_type=ContextOwnerType.AGENT_RUN,
            owner_id=run.id,
            status=ContextSnapshotStatus.FAILED,
            provider_profile_id="provider-1",
            model="test-model",
            prompt_version="v1",
            source_sha256="0" * 64,
            error="provider unavailable",
        )
    )

    (await store.delete_run(run.id))

    for model, entity in (
        (BrowserAutomationLease, lease),
        (BrowserCommand, command),
        (BrowserProxyRule, rule),
        (ContextSnapshot, snapshot),
    ):
        with pytest.raises(NotFoundError):
            (await store.get(model, entity.id))
    # Another mission's lease on the same browser session is untouched.
    assert (
        await store.get(BrowserAutomationLease, other_lease.id)
    ).run_id == other_run.id
    assert (
        await store.engagement_has_dependents(
            engagement.id, exclude_entity_ids=[other_run.id, other_lease.id]
        )
    ) is False


async def test_deleting_a_chat_run_or_archive_removes_budget_counters(store):
    engagement = await store.create(Engagement(name="Budget counter cleanup"))

    async def chat_with_tool_call(title: str) -> tuple[ChatSession, ChatTurn]:
        chat = await store.create(
            ChatSession(
                engagement_id=engagement.id,
                title=title,
                provider_profile_id="provider-1",
                model="test-model",
            )
        )
        turn = await store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=chat.id,
                provider_profile_id="provider-1",
                model="test-model",
                status=ChatTurnStatus.COMPLETE,
                tools_enabled=True,
            )
        )
        (
            await store.reserve_tool_call(
                ToolCall(
                    id=f"{title}-call",
                    engagement_id=engagement.id,
                    run_id=turn.id,
                    origin=ToolCallOrigin.CHAT,
                    chat_session_id=chat.id,
                    chat_turn_id=turn.id,
                    tool_name="nmap.scan",
                    risk_class=RiskClass.LOCAL_READ,
                )
            )
        )
        return chat, turn

    chat, turn = await chat_with_tool_call("deleted-chat")
    kept_chat, kept_turn = await chat_with_tool_call("archived-chat")
    run = await store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="Scan the target",
            status=RunStatus.COMPLETE,
        )
    )
    (
        await store.reserve_tool_call(
            ToolCall(
                id="run-call",
                engagement_id=engagement.id,
                run_id=run.id,
                tool_name="nmap.scan",
                risk_class=RiskClass.LOCAL_READ,
            )
        )
    )
    async with store.database.session() as session:
        for owner_id in (turn.id, kept_turn.id, run.id):
            assert (await session.get(RunBudgetCounterRow, owner_id)) is not None

    (await store.delete_chat_session(chat.id))
    (await store.delete_run(run.id))

    async with store.database.session() as session:
        assert (await session.get(RunBudgetCounterRow, turn.id)) is None
        assert (await session.get(RunBudgetCounterRow, run.id)) is None
        assert (await session.get(RunBudgetCounterRow, kept_turn.id)) is not None

    archived = await store.update(
        Engagement,
        engagement.id,
        {"status": EngagementStatus.ARCHIVED},
        expected_revision=engagement.revision,
    )
    (
        await store.delete_archived_engagement(
            engagement.id, expected_revision=archived.revision
        )
    )

    async with store.database.session() as session:
        assert (await session.get(RunBudgetCounterRow, kept_turn.id)) is None
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, kept_chat.id))


async def test_find_entities_filters_payload_fields_in_sql_and_pages_in_order(store):
    engagement = await store.create(Engagement(name="Filtered"))
    other = await store.create(Engagement(name="Other"))
    base = utc_now()
    (
        await store.create_many(
            [
                Asset(
                    id=f"asset-{index}",
                    engagement_id=engagement.id,
                    name=f"host-{index % 2}",
                    hostname=None if index == 2 else f"h{index}.test",
                    metadata={"tool_call_id": f"call-{index % 2}"},
                    created_at=base + timedelta(seconds=index),
                    updated_at=base + timedelta(seconds=index),
                )
                for index in range(4)
            ]
            + [
                Asset(
                    id="asset-elsewhere",
                    engagement_id=other.id,
                    name="host-0",
                    hostname="elsewhere.test",
                    metadata={"tool_call_id": "call-0"},
                    created_at=base + timedelta(seconds=10),
                    updated_at=base + timedelta(seconds=10),
                )
            ]
        )
    )

    def ids(items):
        return [item.id for item in items]

    assert ids((await store.find_entities(Asset, {"name": "host-0"}))) == [
        "asset-0",
        "asset-2",
        "asset-elsewhere",
    ]
    assert ids(
        (
            await store.find_entities(
                Asset, {"name": "host-0"}, engagement_id=engagement.id
            )
        )
    ) == ["asset-0", "asset-2"]
    assert ids((await store.find_entities(Asset, {"hostname": None}))) == ["asset-2"]
    assert ids(
        (await store.find_entities(Asset, {"metadata.tool_call_id": "call-1"}))
    ) == [
        "asset-1",
        "asset-3",
    ]
    assert ids(
        (await store.find_entities(Asset, {"name": "host-0", "hostname": None}))
    ) == ["asset-2"]
    newest = await store.find_entities(
        Asset,
        {"name": ["host-0", "host-1"]},
        engagement_id=engagement.id,
        newest_first=True,
        limit=3,
    )
    assert ids(newest) == ["asset-3", "asset-2", "asset-1"]
    rest = await store.find_entities(
        Asset,
        {"name": ["host-0", "host-1"]},
        engagement_id=engagement.id,
        newest_first=True,
        offset=3,
        limit=3,
    )
    assert ids(rest) == ["asset-0"]
    assert (await store.find_entities(Asset, {"name": []})) == []
    with pytest.raises(ValueError):
        (await store.find_entities(Asset, {"name": "host-0"}, offset=-1))
    with pytest.raises(ValueError):
        (await store.find_entities(Asset, {"name": "host-0"}, limit=0))


async def test_list_entities_can_page_newest_first(store):
    engagement = await store.create(Engagement(name="Paged"))
    base = utc_now()
    (
        await store.create_many(
            [
                Asset(
                    id=f"asset-{index}",
                    engagement_id=engagement.id,
                    name=f"host {index}",
                    created_at=base + timedelta(seconds=index),
                    updated_at=base + timedelta(seconds=index),
                )
                for index in range(3)
            ]
        )
    )

    newest = await store.list_entities(
        Asset, engagement_id=engagement.id, newest_first=True, limit=2
    )
    assert [item.id for item in newest] == ["asset-2", "asset-1"]
    rest = await store.list_entities(
        Asset, engagement_id=engagement.id, newest_first=True, offset=2, limit=2
    )
    assert [item.id for item in rest] == ["asset-0"]


pytestmark = pytest.mark.anyio
