"""Hot entity reads must seek to their exact keys, regardless of history size."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from nebula.v3.domain import (
    AgentRun,
    Artifact,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.storage import NebulaStore


def _artifact(key, call_id="needle", project="project"):
    return Artifact(
        id=key,
        engagement_id=project,
        sha256="0" * 64,
        size=1,
        storage_path="synthetic/blob",
        metadata={"tool_call_id": call_id},
    )


def _call(key="call", status="approved", origin="chat", project="project"):
    return ToolCall(
        id=key,
        engagement_id=project,
        run_id="run",
        tool_name="synthetic.noop",
        risk_class=RiskClass.LOCAL_READ,
        status=status,
        origin=origin,
        metadata={"harness_turn_id": "harness-turn"},
    )


@pytest.mark.parametrize(
    "create_path",
    ["create", "create_many", "transaction", "run_event", "operation_event"],
)
@pytest.mark.parametrize("model", [Artifact, ToolCall])
async def test_all_entity_create_paths_populate_hot_projections(
    tmp_path, create_path, model
):
    store = NebulaStore(tmp_path / "create.db")
    entity = _artifact("artifact") if model is Artifact else _call()
    if create_path == "create":
        (await store.create(entity))
    elif create_path == "create_many":
        (await store.create_many([entity]))
    elif create_path == "transaction":
        async with store.transaction() as transaction:
            (await transaction.add(entity))
    elif create_path == "run_event":
        (
            await store.create_with_event(
                entity, run_id="run", event_type="synthetic.created"
            )
        )
    else:
        (
            await store.create_with_operation_event(
                entity,
                operation_id="operation",
                operation_kind="synthetic",
                engagement_id="project",
                event_type="synthetic.created",
                event_payload={},
            )
        )
    filters = (
        {"metadata.tool_call_id": "needle"}
        if model is Artifact
        else {
            "status": "approved",
            "origin": "chat",
            "metadata.harness_turn_id": "harness-turn",
        }
    )
    assert [item.id for item in (await store.find_entities(model, filters))] == [
        entity.id
    ]
    (await store.database.dispose())


async def test_reserved_tool_call_populates_projections_and_is_idempotent(tmp_path):
    store = NebulaStore(tmp_path / "reserve.db")
    (
        await store.create(
            AgentRun(id="run", engagement_id="project", objective="Synthetic")
        )
    )
    call = _call(origin="mission")
    (await store.reserve_tool_call(call))
    assert (await store.reserve_tool_call(call)) == call
    assert [
        item.id
        for item in (
            await store.find_entities(
                ToolCall,
                {
                    "status": "approved",
                    "origin": "mission",
                    "metadata.harness_turn_id": "harness-turn",
                },
            )
        )
    ] == [call.id]
    (await store.database.dispose())


@pytest.mark.parametrize(
    "update_path", ["update", "transaction", "replace", "run_event", "operation_event"]
)
@pytest.mark.parametrize("model", [Artifact, ToolCall])
async def test_all_update_paths_replace_and_clear_hot_projections(
    tmp_path, update_path, model
):
    store = NebulaStore(tmp_path / "update.db")
    entity = await store.create(_artifact("artifact") if model is Artifact else _call())
    for changes in (
        [{"metadata": {"tool_call_id": "changed"}}, {"metadata": {}}]
        if model is Artifact
        else [
            {
                "status": ToolCallStatus.RUNNING,
                "origin": ToolCallOrigin.MISSION,
                "metadata": {"harness_turn_id": "changed-turn"},
            },
            {
                "status": ToolCallStatus.COMPLETE,
                "origin": ToolCallOrigin.CHAT,
                "metadata": {},
            },
        ]
    ):
        old_filters = (
            {"metadata.tool_call_id": entity.metadata.get("tool_call_id")}
            if model is Artifact
            else {
                "status": entity.status.value,
                "origin": entity.origin.value,
                "metadata.harness_turn_id": entity.metadata.get("harness_turn_id"),
            }
        )
        if update_path == "update":
            entity = await store.update(
                model, entity.id, changes, expected_revision=entity.revision
            )
        elif update_path == "transaction":
            async with store.transaction() as transaction:
                entity = await transaction.update(
                    model, entity.id, changes, expected_revision=entity.revision
                )
        elif update_path == "replace":
            entity = await store.replace(
                model,
                entity.id,
                entity.model_copy(update=changes),
                expected_revision=entity.revision,
            )
        elif update_path == "run_event":
            entity, _ = await store.update_with_event(
                model,
                entity.id,
                changes,
                expected_revision=entity.revision,
                run_id="run",
                event_type="synthetic.changed",
                event_payload={},
            )
        else:
            entity, _ = await store.update_with_operation_event(
                model,
                entity.id,
                changes,
                expected_revision=entity.revision,
                operation_id="operation",
                operation_kind="synthetic",
                engagement_id="project",
                event_type="synthetic.changed",
                event_payload={},
            )
        assert (await store.find_entities(model, old_filters)) == []
        filters = (
            {"metadata.tool_call_id": entity.metadata.get("tool_call_id")}
            if model is Artifact
            else {
                "status": entity.status.value,
                "origin": entity.origin.value,
                "metadata.harness_turn_id": entity.metadata.get("harness_turn_id"),
            }
        )
        assert [item.id for item in (await store.find_entities(model, filters))] == [
            entity.id
        ]
    (await store.database.dispose())


async def test_artifact_lookup_preserves_project_order_and_metadata_types(tmp_path):
    store = NebulaStore(tmp_path / "artifacts.db")
    stamp = datetime(2026, 11, 2, tzinfo=timezone.utc)
    (
        await store.create_many(
            [
                _artifact(key, call_id, project).model_copy(
                    update={"created_at": stamp}
                )
                for key, call_id, project in [
                    ("z", "needle", "project"),
                    ("a", "needle", "project"),
                    ("foreign", "needle", "other"),
                    ("number", 12, "project"),
                    ("empty", "", "project"),
                    ("null", None, "project"),
                ]
            ]
        )
    )
    assert [
        a.id for a in (await store.list_tool_call_artifacts("project", "needle"))
    ] == [
        "a",
        "z",
    ]
    assert (await store.list_tool_call_artifacts("project", "12")) == []
    assert [
        a.id
        for a in (await store.find_entities(Artifact, {"metadata.tool_call_id": 12}))
    ] == ["number"]
    assert [
        a.id
        for a in (await store.find_entities(Artifact, {"metadata.tool_call_id": None}))
    ] == ["null"]
    assert [a.id for a in (await store.list_tool_call_artifacts("project", ""))] == [
        "empty"
    ]
    (await store.database.dispose())


async def test_recovery_filters_keep_scope_paging_and_readable_iteration(tmp_path):
    store = NebulaStore(tmp_path / "recovery.db")
    (
        await store.create_many(
            [_call(f"settled-{i}", status="complete") for i in range(1_001)]
        )
    )
    (
        await store.create_many(
            [
                _call("a"),
                _call("b", project="other"),
                _call("c", status="running"),
                _call("mission", origin="mission"),
            ]
        )
    )
    filters = {"status": ["approved", "running"], "origin": "chat"}
    assert [c.id for c in (await store.find_entities(ToolCall, filters))] == [
        "a",
        "b",
        "c",
    ]
    assert [
        c.id
        for c in (await store.find_entities(ToolCall, filters, engagement_id="project"))
    ] == ["a", "c"]
    assert [
        c.id for c in (await store.find_entities(ToolCall, filters, offset=1, limit=1))
    ] == ["b"]
    assert (await store.find_entities(ToolCall, {"status": []})) == []
    seen = []
    async for call in store.iter_readable_entities(ToolCall, filters, page_size=1):
        seen.append(call.id)
        (
            await store.update(
                ToolCall,
                call.id,
                {"status": "complete"},
                expected_revision=call.revision,
            )
        )
    assert seen == ["a", "b", "c"]
    (await store.database.dispose())


@pytest.mark.parametrize(
    "operation,index,keys",
    [
        (
            "artifact_hit",
            "ix_entities_artifact_tool_call_created",
            "kind=? AND engagement_id=? AND artifact_tool_call_id=?",
        ),
        (
            "artifact_miss",
            "ix_entities_artifact_tool_call_created",
            "kind=? AND engagement_id=? AND artifact_tool_call_id=?",
        ),
        (
            "artifact_filter",
            "ix_entities_artifact_tool_call_created",
            "kind=? AND engagement_id=? AND artifact_tool_call_id=?",
        ),
        (
            "recovery",
            "ix_entities_tool_call_recovery_created",
            "kind=? AND tool_call_status=? AND tool_call_origin=?",
        ),
        (
            "listing",
            "ix_entities_kind_engagement_created_id",
            "kind=? AND engagement_id=?",
        ),
        (
            "newest",
            "ix_entities_kind_engagement_created_id",
            "kind=? AND engagement_id=?",
        ),
    ],
)
async def test_actual_hot_queries_seek_full_keys_without_a_temporary_sort(
    tmp_path, operation, index, keys
):
    store = NebulaStore(tmp_path / "plans.db")
    (
        await store.create_many(
            [_artifact(f"old-{i}", f"old-call-{i}") for i in range(1_001)]
        )
    )
    (
        await store.create_many(
            [_call(f"settled-{i}", status="complete") for i in range(1_001)]
        )
    )
    (await store.create(_artifact("target")))
    (await store.create(_call()))
    plans = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM entities" in statement:
            plans.append(
                " ".join(
                    str(row[-1])
                    for row in conn.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    )
                )
            )

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        if operation.startswith("artifact_"):
            if operation == "artifact_filter":
                (
                    await store.find_entities(
                        Artifact,
                        {"metadata.tool_call_id": "needle"},
                        engagement_id="project",
                    )
                )
            else:
                (
                    await store.list_tool_call_artifacts(
                        "project",
                        "missing" if operation == "artifact_miss" else "needle",
                    )
                )
        elif operation == "recovery":
            assert (
                len(
                    list(
                        [
                            item
                            async for item in store.iter_readable_entities(
                                ToolCall, {"status": "approved", "origin": "chat"}
                            )
                        ]
                    )
                )
                == 1
            )
        else:
            (
                await store.list_entities(
                    Artifact,
                    engagement_id="project",
                    limit=1000,
                    newest_first=operation == "newest",
                )
            )
        assert plans
        for position, plan in enumerate(plans):
            # A one-id recovery page can use the primary key even more cheaply.
            if operation == "recovery" and position > 0 and "(id=?)" in plan:
                assert "sqlite_autoindex_entities_1" in plan, plan
                continue
            assert index in plan, plan
            assert keys in plan, plan
            assert "SCAN entities" not in plan, plan
            assert "TEMP B-TREE" not in plan, plan
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", capture
        )
        (await store.database.dispose())


pytestmark = pytest.mark.anyio
