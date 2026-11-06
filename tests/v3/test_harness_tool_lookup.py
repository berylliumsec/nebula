"""Tool-event reads seek to the current turn's pending calls, regardless of history."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatTurn,
    HarnessSession,
    HarnessTurn,
    McpServerProfile,
    ToolCall,
    ToolCallStatus,
)
from nebula.v3.harnesses import HarnessEvent, HarnessRuntimeService
from nebula.v3.storage import NebulaStore


async def _fixture(tmp_path, origin):
    store = NebulaStore(tmp_path / "tools.db")
    session = await store.create(
        HarnessSession(
            id="harness",
            engagement_id="project",
            harness_profile_id="fixture",
            model="fixture-model",
        )
    )
    turn = await store.create(
        HarnessTurn(
            id="turn",
            engagement_id="project",
            harness_session_id=session.id,
            origin=origin,
            prompt="Synthetic tool history",
            **(
                {"chat_session_id": "chat", "chat_turn_id": "chat-turn"}
                if origin == "chat"
                else {"run_id": "run"}
            ),
        )
    )
    if origin == "chat":
        (
            await store.create(
                ChatTurn(
                    id="chat-turn",
                    engagement_id="project",
                    session_id="chat",
                    backend="harness",
                    model="fixture-model",
                    harness_turn_id=turn.id,
                )
            )
        )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    return store, session, turn, runtime


def _call(turn, key, *, status="running", owner=None, project=None):
    return ToolCall(
        id=key,
        engagement_id=project or turn.engagement_id,
        run_id=turn.chat_turn_id or turn.run_id,
        origin=turn.origin,
        chat_session_id=turn.chat_session_id,
        chat_turn_id=turn.chat_turn_id,
        tool_name="vendor:codex:synthetic.noop",
        vendor_tool_name="codex:synthetic.noop",
        status=status,
        risk_class="local_read",
        metadata={"harness_turn_id": owner or turn.id, "vendor_item_id": key},
        created_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("origin", ["chat", "mission"])
@pytest.mark.parametrize("history_owner", ["turn", "older-turn"])
async def test_pending_lookup_work_is_bounded_by_active_calls(
    tmp_path, origin, history_owner
):
    store, _session, turn, runtime = await _fixture(tmp_path, origin)
    wanted = (
        ToolCallStatus.PROPOSED,
        ToolCallStatus.WAITING_APPROVAL,
        ToolCallStatus.APPROVED,
        ToolCallStatus.RUNNING,
    )
    (
        await store.create_many(
            [
                _call(turn, f"active-{index}", status=status)
                for index, status in enumerate(wanted)
            ]
            + [
                _call(turn, "different-turn", owner="different-turn"),
                _call(turn, "different-project", project="foreign"),
            ]
        )
    )
    queries = []
    steps = []
    progress_connections = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM entities" in statement:
            queries.append(
                (
                    statement,
                    parameters,
                    cursor.connection.execute(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    ).fetchall(),
                )
            )
            cursor.connection.set_progress_handler(lambda: steps.append(1) or 0, 100)
            progress_connections.append(cursor.connection)

    try:
        baseline_steps = None
        for history in (0, 4500):
            if history:
                (
                    await store.create_many(
                        [
                            _call(
                                turn,
                                f"history-{index}",
                                status="complete",
                                owner=history_owner,
                            ).model_copy(update={"result": "x" * 4096})
                            for index in range(history)
                        ]
                    )
                )
            queries.clear()
            steps.clear()
            event.listen(store.database.engine, "before_cursor_execute", capture)
            try:
                assert [
                    call.id
                    for call in (await runtime._turn_tool_calls(turn, statuses=wanted))
                ] == [f"active-{index}" for index in range(4)]
            finally:
                for connection in progress_connections:
                    connection.set_progress_handler(None, 0)
                progress_connections.clear()
                event.remove(store.database.engine, "before_cursor_execute", capture)
            assert len(queries) == 1
            sql, _, plan = queries[0]
            assert "JSON_EXTRACT" not in sql
            detail = " ".join(row[-1] for row in plan)
            assert "ix_entities_tool_call_turn_status_created" in detail, detail
            assert "tool_call_harness_turn_id=?" in detail, detail
            assert "tool_call_status=?" in detail, detail
            if baseline_steps is None:
                baseline_steps = len(steps)
            else:
                assert len(steps) <= baseline_steps + 2, (baseline_steps, len(steps))
        assert (await runtime._turn_tool_calls(turn, statuses=())) == []
    finally:
        (await store.database.dispose())


@pytest.mark.parametrize("origin", ["chat", "mission"])
@pytest.mark.parametrize("vendor", ["codex", "claude", "mcp"])
async def test_tool_events_reuse_the_approved_call_and_settle_it(
    tmp_path, origin, vendor
):
    store, session, turn, runtime = await _fixture(tmp_path, origin)
    call = _call(turn, "item-1", status="approved")
    if vendor == "mcp":
        server = McpServerProfile(
            id="mcp", name="fixture", transport="stdio", command="/bin/true"
        )
        session = session.model_copy(
            update={"mcp_snapshot": [server.model_dump(mode="json")]}
        )
        call = call.model_copy(
            update={
                "mcp_server_id": server.id,
                "mcp_tool_name": "synthetic.noop",
            }
        )
    else:
        call = call.model_copy(update={"vendor_tool_name": f"{vendor}:synthetic.noop"})
    (await store.create(call))
    try:
        for kind in ("tool_started", "tool_completed"):
            recorded = await runtime._record_tool_event(
                turn,
                session,
                HarnessEvent(
                    type=kind,
                    server_id=vendor,
                    tool_name="synthetic.noop",
                    item_id=call.id,
                    payload={"result": "Synthetic retained result"},
                ),
            )
            assert recorded.tool_call_id == call.id
            latest = await store.get(ToolCall, call.id)
            assert latest.status == (
                ToolCallStatus.RUNNING
                if kind == "tool_started"
                else ToolCallStatus.COMPLETE
            )
        assert (
            await runtime._turn_tool_calls(turn, statuses=(ToolCallStatus.RUNNING,))
        ) == []
        assert (
            len(
                (
                    await store.find_entities(
                        ToolCall, {"metadata.harness_turn_id": turn.id}
                    )
                )
            )
            == 1
        )
        assert latest.result == "Synthetic retained result"
    finally:
        (await store.database.dispose())


async def test_harness_turn_projection_preserves_metadata_filter_types(tmp_path):
    store, _session, turn, _runtime = await _fixture(tmp_path, "mission")
    try:
        for key, value in [
            ("string", "12"),
            ("number", 12),
            ("empty", ""),
            ("null", None),
            ("array", ["12"]),
            ("boolean", True),
        ]:
            (
                await store.create(
                    _call(turn, key).model_copy(
                        update={"metadata": {"harness_turn_id": value}}
                    )
                )
            )
        (await store.create(_call(turn, "missing").model_copy(update={"metadata": {}})))
        assert [
            c.id
            for c in (
                await store.find_entities(ToolCall, {"metadata.harness_turn_id": "12"})
            )
        ] == ["string"]
        assert [
            c.id
            for c in (
                await store.find_entities(ToolCall, {"metadata.harness_turn_id": 12})
            )
        ] == ["number", "string"]  # Preserve the existing integer/text comparison.
        assert [
            c.id
            for c in (
                await store.find_entities(ToolCall, {"metadata.harness_turn_id": ""})
            )
        ] == ["empty"]
        assert {
            c.id
            for c in (
                await store.find_entities(ToolCall, {"metadata.harness_turn_id": None})
            )
        } == {"null", "missing"}
    finally:
        (await store.database.dispose())


pytestmark = pytest.mark.anyio
