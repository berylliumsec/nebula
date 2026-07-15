from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
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
    Engagement,
    RiskClass,
    RunBudget,
    RunStatus,
    ToolCall,
    utc_now,
)
from nebula.v3.storage import (
    ConflictError,
    NebulaStore,
    NotFoundError,
    RunBudgetExceededError,
)


@pytest.fixture
def store(tmp_path):
    return NebulaStore(Database(tmp_path / "nebula.db"))


def test_sqlite_bootstrap_enables_wal_and_schema_version(store):
    health = store.database.health()
    assert health == {
        "database": "ok",
        "dialect": "sqlite",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "journal_mode": "wal",
    }


def test_sqlite_database_and_wal_files_are_private(tmp_path):
    path = tmp_path / "private" / "nebula.db"
    database = Database(path)

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(f"{path}-wal").stat().st_mode & 0o777 == 0o600
    assert Path(f"{path}-shm").stat().st_mode & 0o777 == 0o600

    database.dispose()


def test_schema_bootstrap_is_idempotent_and_refuses_a_newer_database(tmp_path):
    path = tmp_path / "versioned.db"
    first = Database(path)
    assert first.current_schema_version() == CURRENT_SCHEMA_VERSION
    with first.session() as session:
        marker_count = session.query(SchemaVersionRow).count()
    first.dispose()

    reopened = Database(path)
    with reopened.session() as session:
        assert session.query(SchemaVersionRow).count() == marker_count
    with reopened.engine.begin() as connection:
        connection.execute(
            SchemaVersionRow.__table__.insert().values(
                version=CURRENT_SCHEMA_VERSION + 1,
                applied_at=utc_now(),
            )
        )
    reopened.dispose()

    with pytest.raises(SchemaVersionError, match="newer than supported"):
        Database(path)


def test_typed_crud_and_optimistic_revision(store):
    engagement = store.create(Engagement(name="Acme"))
    assert store.get(Engagement, engagement.id) == engagement

    updated = store.update(
        Engagement,
        engagement.id,
        {"description": "External assessment"},
        expected_revision=1,
    )
    assert updated.description == "External assessment"
    assert updated.revision == 2
    with pytest.raises(ConflictError):
        store.update(
            Engagement,
            engagement.id,
            {"description": "stale"},
            expected_revision=1,
        )
    with pytest.raises(ValueError):
        store.update(Engagement, engagement.id, {"id": "different"})

    with pytest.raises(ConflictError, match="revision conflict"):
        store.delete(Engagement, engagement.id, expected_revision=1)

    store.delete(Engagement, engagement.id, expected_revision=updated.revision)
    with pytest.raises(NotFoundError):
        store.get(Engagement, engagement.id)


def test_transaction_rolls_back_every_entity(store):
    engagement = Engagement(name="Rollback")
    asset = Asset(engagement_id=engagement.id, name="10.0.0.1")
    with pytest.raises(RuntimeError):
        with store.transaction() as transaction:
            transaction.add(engagement)
            transaction.add(asset)
            raise RuntimeError("abort")
    assert store.count(Engagement) == 0
    assert store.count(Asset) == 0


def test_event_ledger_sequences_replays_and_deduplicates(store):
    first = store.append_event(
        "run-1", "run.created", {"objective": "test"}, idempotency_key="create"
    )
    retried = store.append_event(
        "run-1", "run.created", {"objective": "test"}, idempotency_key="create"
    )
    second = store.append_event("run-1", "task.created", {"task_id": "t-1"})
    assert retried == first
    assert second.sequence == 2
    assert [event.sequence for event in store.replay_events("run-1")] == [1, 2]
    assert [
        event.sequence for event in store.replay_events("run-1", after_sequence=1)
    ] == [2]
    with pytest.raises(ConflictError, match="idempotency key"):
        store.append_event(
            "run-1", "run.created", {"different": True}, idempotency_key="create"
        )


def test_event_sequence_is_atomic_between_threads(store):
    def append(number):
        return store.append_event("parallel", "tick", {"number": number}).sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(20)))
    assert sorted(sequences) == list(range(1, 21))


def test_execution_and_artifact_query_budgets_are_independent_and_atomic(store):
    engagement = store.create(Engagement(name="Independent budgets"))
    run = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="exercise both counters",
            budget=RunBudget(max_tool_calls=1, max_artifact_queries=1),
        )
    )

    def reserve(call_id: str, budget_class: str) -> str:
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
            return store.reserve_tool_call(call).id
        except RunBudgetExceededError:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(
            executor.map(
                lambda item: reserve(*item),
                [
                    ("action-a", "execution"),
                    ("action-b", "execution"),
                    ("query-a", "artifact_query"),
                    ("query-b", "artifact_query"),
                ],
            )
        )
    assert outcomes.count("exhausted") == 2
    assert len([item for item in outcomes if item.startswith("action-")]) == 1
    assert len([item for item in outcomes if item.startswith("query-")]) == 1
    with store.database.session() as session:
        counter = session.get(RunBudgetCounterRow, run.id)
        assert counter is not None
        assert counter.tool_calls == 1
        assert counter.artifact_queries == 1


def test_create_with_event_is_atomic_when_event_conflicts(store):
    store.append_event(
        "run-1",
        "run.started",
        {"objective": "first"},
        idempotency_key="run:started",
    )
    engagement = Engagement(name="Must roll back")

    with pytest.raises(ConflictError):
        store.create_with_event(
            engagement,
            run_id="run-1",
            event_type="run.started",
            event_payload={"objective": "second"},
            idempotency_key="run:started",
        )

    with pytest.raises(NotFoundError):
        store.get(Engagement, engagement.id)


def test_update_with_event_is_atomic_and_idempotent(store):
    engagement = store.create(Engagement(name="Atomic transitions"))
    run = store.create(
        AgentRun(
            engagement_id=engagement.id,
            objective="exercise transition retries",
        )
    )
    changes = {"status": RunStatus.RUNNING}
    event_payload = {"status": RunStatus.RUNNING.value}

    updated, event = store.update_with_event(
        AgentRun,
        run.id,
        changes,
        expected_revision=run.revision,
        run_id=run.id,
        event_type="run.running",
        event_payload=event_payload,
        idempotency_key="run:running",
    )
    retried, retried_event = store.update_with_event(
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
    assert store.replay_events(run.id) == [event]

    with pytest.raises(ConflictError, match="idempotency key"):
        store.update_with_event(
            AgentRun,
            run.id,
            {"status": RunStatus.FAILED},
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.failed",
            event_payload={"status": RunStatus.FAILED.value},
            idempotency_key="run:running",
        )
    assert store.get(AgentRun, run.id) == updated


def test_orm_rejects_event_updates_and_deletes(store):
    event = store.append_event("run-1", "immutable")
    with pytest.raises(RuntimeError, match="append-only"):
        with store.database.session() as session:
            row = session.get(RunEventRow, event.id)
            row.event_type = "rewritten"
            session.flush()
    with pytest.raises(RuntimeError, match="append-only"):
        with store.database.session() as session:
            row = session.get(RunEventRow, event.id)
            session.delete(row)
            session.flush()

    with pytest.raises(DBAPIError, match="append-only"):
        with store.database.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE run_events SET event_type='raw-rewrite' WHERE id=?",
                (event.id,),
            )


def test_orm_and_database_reject_operation_event_mutation(store):
    engagement = store.create(Engagement(name="Immutable operations"))
    event = store.append_operation_event(
        "execution-1",
        "operator_execution",
        engagement.id,
        "execution.queued",
    )
    with pytest.raises(RuntimeError, match="append-only"):
        with store.database.session() as session:
            row = session.get(OperationEventRow, event.id)
            row.event_type = "rewritten"
            session.flush()
    with pytest.raises(RuntimeError, match="append-only"):
        with store.database.session() as session:
            row = session.get(OperationEventRow, event.id)
            session.delete(row)
            session.flush()

    with pytest.raises(DBAPIError, match="immutable"):
        with store.database.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE operation_events SET event_type='raw-rewrite' WHERE id=?",
                (event.id,),
            )


def test_overview_counts_entities_by_engagement(store):
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    store.create(Asset(engagement_id=first.id, name="one"))
    store.create(Asset(engagement_id=second.id, name="two"))
    overview = store.overview(first.id)
    assert overview["counts"]["engagements"] == 1
    assert overview["counts"]["assets"] == 1
