"""Tool calls, chat turns and harness turns store each record once.

Every command stored its streams twice (raw and a redacted copy with the same
bytes), tools without a process stored empty stdout and stderr placeholders,
MCP results stored their structured content twice, a deleted conversation left
its turn ledgers behind, Grok turns stored each ignored vendor notification as
an activity row, and a Mission harness turn's activity replayed its whole run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.automation_tools import AutomationBroker
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import ChatTurnCheckpointRow, ChatTurnStepEventRow
from nebula.v3.domain import (
    Artifact,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    Engagement,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    RiskClass,
    ScopePolicy,
    ToolCall,
    utc_now,
)
from nebula.v3.harnesses import HarnessEvent, HarnessKind
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import ToolOutputService
from nebula.v3.tools import (
    StoreToolEvidenceRecorder,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)
from tests.v3.test_automation_runtime import (
    create_tool_chat,
    runtime,
    tool_invocation,
)
from tests.v3.test_grok_acp_compliance import (
    MemoryGrokAdapter,
    MemoryRpc,
    _chat_turn,
    _chunk,
    _runtime as _grok_runtime,
)
from tests.v3.test_harnesses import _runtime as _harness_runtime


def _command_artifacts(
    store: NebulaStore, execution: CommandExecution
) -> list[Artifact]:
    return [
        artifact
        for artifact in store.list_entities(Artifact, limit=100)
        if artifact.metadata.get("command_execution_id") == execution.id
    ]


def _run_command(tmp_path: Path, command: str):
    async def scenario():
        manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
        create_tool_chat(store, engagement)
        broker = AutomationBroker(
            manager=manager,
            store=store,
            output_service=ToolOutputService(store, artifacts),
        )
        result = await broker.execute(
            tool_invocation(
                engagement,
                tmp_path / "workspaces" / engagement.id,
                "run_command",
                {"command": command},
                1,
            ),
            store.get(ScopePolicy, f"scope:{engagement.id}"),
        )
        [call] = store.list_entities(ToolCall)
        [execution] = store.list_entities(CommandExecution)
        return store, artifacts, engagement, call, execution, result

    return asyncio.run(scenario())


def test_a_command_stream_redaction_leaves_unchanged_is_stored_once(tmp_path):
    store, artifacts, engagement, call, execution, result = _run_command(
        tmp_path, "echo storage-waste-marker"
    )

    # The fake runtime echoes the command on stdout and writes no stderr:
    # neither needs redacting, so each stream is one artifact that also
    # serves as its redacted reference.
    recorded = _command_artifacts(store, execution)
    assert sorted(item.metadata["kind"] for item in recorded) == ["stderr", "stdout"]
    assert execution.redacted_stdout_artifact_id == execution.stdout_artifact_id
    assert execution.redacted_stderr_artifact_id == execution.stderr_artifact_id
    assert [item.kind for item in result.receipt.artifacts] == ["stdout", "stderr"]

    # Search reads the stream once, so each match comes back once.
    found = ToolOutputService(store, artifacts).search(
        engagement_id=engagement.id,
        owner_id="turn-1",
        tool_call_id=call.id,
        query="storage-waste-marker",
    )
    assert [item["artifact_id"] for item in found["matches"]] == [
        execution.stdout_artifact_id
    ]


def test_a_command_stream_with_a_secret_keeps_a_separate_redacted_copy(tmp_path):
    store, artifacts, engagement, call, execution, _result = _run_command(
        tmp_path, "echo password=hunter2hunter2"
    )

    raw = store.get(Artifact, execution.stdout_artifact_id)
    redacted = store.get(Artifact, execution.redacted_stdout_artifact_id)
    assert redacted.id != raw.id
    assert redacted.redacted is True and redacted.parent_artifact_id == raw.id
    assert b"hunter2hunter2" in artifacts.read(raw)
    assert b"hunter2hunter2" not in artifacts.read(redacted)
    # The unchanged stderr still needs no copy.
    assert execution.redacted_stderr_artifact_id == execution.stderr_artifact_id

    # The copy is not searched a second time, and the stream's own matches
    # are redacted as they are returned.
    found = ToolOutputService(store, artifacts).search(
        engagement_id=engagement.id,
        owner_id="turn-1",
        tool_call_id=call.id,
        query="password",
    )
    assert [item["artifact_id"] for item in found["matches"]] == [raw.id]
    assert "hunter2hunter2" not in json.dumps(found)


def test_search_skips_the_redacted_twin_an_older_command_recorded(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Older command rows"))
    call = store.create(
        ToolCall(
            engagement_id=engagement.id,
            run_id="mission-owner",
            tool_name="run_command",
            risk_class=RiskClass.WORKSPACE_WRITE,
        )
    )
    body = b"alpha\nneedle line\nomega\n"
    metadata = {"tool_call_id": call.id, "searchable": True}
    raw = store.create(
        artifacts.put_bytes(
            body,
            engagement_id=engagement.id,
            filename="command.stdout",
            media_type="text/plain",
            source="automation-runtime",
            metadata={**metadata, "kind": "stdout"},
        )
    )
    # Before this change every stream also had a byte-identical redacted row.
    twin = artifacts.put_bytes(
        body,
        engagement_id=engagement.id,
        filename="command.stdout.redacted.txt",
        media_type="text/plain",
        source="automation-runtime-redaction",
        parent_artifact_id=raw.id,
        metadata={**metadata, "kind": "redacted_stdout"},
    )
    twin = store.create(twin.model_copy(update={"redacted": True}))

    service = ToolOutputService(store, artifacts)
    found = service.search(
        engagement_id=engagement.id,
        owner_id="mission-owner",
        tool_call_id=call.id,
        query="needle",
    )

    assert [(item["artifact_id"], item["line"]) for item in found["matches"]] == [
        (raw.id, 2)
    ]
    # The twin is still readable by the id an older receipt might name.
    read = service.read(
        engagement_id=engagement.id, owner_id="mission-owner", artifact_id=twin.id
    )
    assert [line["text"] for line in read["lines"]] == ["alpha", "needle line", "omega"]


def _record(tmp_path: Path, result: ToolExecutionResult, *, name: str):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Recorded tools"))
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    call = store.create(
        ToolCall(
            engagement_id=engagement.id,
            run_id="mission-owner",
            tool_name=name,
            risk_class=RiskClass.LOCAL_READ,
        )
    )
    spec = ToolSpec(
        name=name,
        description="fixture tool",
        input_schema={"type": "object"},
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    invocation = ToolInvocation(
        id=call.id,
        engagement_id=engagement.id,
        run_id="mission-owner",
        tool_name=name,
        workspace=workspace,
    )
    recorded = asyncio.run(
        StoreToolEvidenceRecorder(store, artifacts).record(
            call, invocation, spec, result
        )
    )
    kinds = sorted(
        item.metadata["kind"]
        for item in store.list_tool_call_artifacts(engagement.id, call.id)
    )
    return recorded, kinds


def test_an_mcp_result_is_stored_once_without_stream_placeholders(tmp_path):
    structured = {"hosts": [{"address": "203.0.113.7", "open": [22, 443]}]}
    recorded, kinds = _record(
        tmp_path,
        ToolExecutionResult(
            output={},
            exit_code=0,
            mcp_content_blocks=[
                # MCP servers serialize structured content into a text block too.
                {"type": "text", "text": json.dumps(structured, indent=2)},
                {"type": "structured_content", "value": structured},
            ],
            execution={"runtime": "mcp"},
        ),
        name="mcp.fixture.scan",
    )

    assert kinds == ["mcp_content", "receipt"]
    assert [item.kind for item in recorded.receipt.artifacts] == ["mcp_content"]
    assert len(recorded.evidence_ids) == 1
    assert recorded.result_artifact_id is not None


def test_structured_content_the_text_does_not_carry_is_kept(tmp_path):
    _recorded, kinds = _record(
        tmp_path,
        ToolExecutionResult(
            output={},
            exit_code=0,
            mcp_content_blocks=[
                {"type": "text", "text": "Scan finished: 2 open ports."},
                {"type": "text", "text": '{"hosts": []}'},
                {"type": "structured_content", "value": {"hosts": ["203.0.113.7"]}},
            ],
        ),
        name="mcp.fixture.scan",
    )

    assert kinds == ["mcp_content", "mcp_content", "mcp_content", "receipt"]


def test_a_process_tool_keeps_both_streams_even_when_one_is_empty(tmp_path):
    recorded, kinds = _record(
        tmp_path,
        ToolExecutionResult(output={}, stdout="uid=0(root)\n", exit_code=0),
        name="ssh.fixture.run_command",
    )

    assert kinds == ["receipt", "stderr", "stdout"]
    assert [item.kind for item in recorded.receipt.artifacts] == ["stdout", "stderr"]


def _ledger_rows(store: NebulaStore, turn_id: str) -> tuple[int, int]:
    with store.database.session() as session:
        return tuple(  # type: ignore[return-value]
            int(
                session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.turn_id == turn_id)
                )
                or 0
            )
            for table in (ChatTurnStepEventRow, ChatTurnCheckpointRow)
        )


def _conversation_with_ledger(
    store: NebulaStore, engagement: Engagement, name: str
) -> tuple[ChatSession, ChatTurn]:
    chat = store.create(
        ChatSession(
            engagement_id=engagement.id,
            title=name,
            provider_profile_id="provider-1",
            model="test-model",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=chat.id,
            provider_profile_id="provider-1",
            model="test-model",
            status=ChatTurnStatus.COMPLETE,
            tools_enabled=True,
        )
    )
    ledger = ChatTurnLedger(store.database)
    for step in range(3):
        ledger.append(
            turn.id,
            {"step": step, "name": "run_command", "status": "complete"},
        )
    with store.database.session() as session:
        session.add(
            ChatTurnCheckpointRow(
                id=f"checkpoint-{turn.id}",
                turn_id=turn.id,
                through_step=2,
                summary={"schema": "fixture"},
                digest="0" * 64,
                token_estimate=10,
                created_at=utc_now(),
            )
        )
    assert _ledger_rows(store, turn.id) == (3, 1)
    return chat, turn


def test_deleting_a_conversation_removes_its_turn_ledgers(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Ledger retention"))
    deleted, deleted_turn = _conversation_with_ledger(store, engagement, "Deleted")
    _kept, kept_turn = _conversation_with_ledger(store, engagement, "Kept")

    store.delete_chat_session(deleted.id)

    assert _ledger_rows(store, deleted_turn.id) == (0, 0)
    assert _ledger_rows(store, kept_turn.id) == (3, 1)


def test_deleting_an_archived_project_removes_its_turn_ledgers(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Archived project"))
    other = store.create(Engagement(name="Other project"))
    _chat, turn = _conversation_with_ledger(store, engagement, "Archived chat")
    _other_chat, other_turn = _conversation_with_ledger(store, other, "Other chat")
    engagement = store.update(
        Engagement,
        engagement.id,
        {"status": "archived"},
        expected_revision=engagement.revision,
    )

    store.delete_archived_engagement(
        engagement.id, expected_revision=engagement.revision
    )

    assert _ledger_rows(store, turn.id) == (0, 0)
    assert _ledger_rows(store, other_turn.id) == (3, 1)


class _VendorFrameRpc(MemoryRpc):
    """Queues raw ACP frames (any method) before answering the prompt."""

    def __init__(self, frames: list[dict[str, Any]], result: dict[str, Any]) -> None:
        super().__init__()
        self.frames = frames
        self.result = result
        self.errors: list[tuple[Any, str]] = []

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method != "session/prompt":
            return {}
        for frame in self.frames:
            await self.events.put(frame)
        await asyncio.sleep(0.01)
        return self.result

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self.errors.append((request_id, message))


def _grok_turn_frames() -> list[dict[str, Any]]:
    def notification(method: str, **params: Any) -> dict[str, Any]:
        return {"method": method, "params": {"sessionId": "sess-1", **params}}

    def update(kind: str) -> dict[str, Any]:
        return notification("session/update", update={"sessionUpdate": kind})

    return [
        notification("_x.ai/mcp/init_progress", progress=0.5),
        notification("_x.ai/mcp/server_status", status="ready"),
        notification("_x.ai/mcp/server_status", status="ready"),
        notification("_x.ai/queue/changed", queue=[]),
        notification("_x.ai/sessions/changed"),
        notification("_x.ai/models/update", models=["grok-test"] * 40),
        update("tool_call_delta_chunk"),
        update("response_completed"),
        update("turn_completed"),
        {
            "method": "session/update",
            "params": {"sessionId": "sess-1", "update": _chunk("The answer.")},
        },
        notification("_x.ai/session/prompt_complete"),
        # A request Grok sends Nebula is still answered and still recorded.
        {"id": 77, **notification("_x.ai/consent/request")},
    ]


def test_grok_turn_counts_ignored_notifications_instead_of_storing_each(tmp_path):
    async def scenario() -> None:
        rpc = _VendorFrameRpc(_grok_turn_frames(), {"stopReason": "end_turn"})
        store, engagement, profile, service = _grok_runtime(
            tmp_path, MemoryGrokAdapter(rpc)
        )
        _chat, _owner, turn = _chat_turn(service, engagement, profile, "question")
        await service.start_chat_turn(turn.id)

        events = service.activity_events(turn.id).events
        notices = [event.summary for event in events if event.type == "notice"]
        assert notices == ["Unhandled Grok ACP event: _x.ai/consent/request"]
        assert rpc.errors and rpc.errors[0][0] == 77
        completed = next(event for event in events if event.type == "completed")
        assert completed.message == "The answer."
        assert completed.payload["ignored_notifications"] == {
            "_x.ai/mcp/init_progress": 1,
            "_x.ai/mcp/server_status": 2,
            "_x.ai/models/update": 1,
            "_x.ai/queue/changed": 1,
            "_x.ai/session/prompt_complete": 1,
            "_x.ai/sessions/changed": 1,
            "response_completed": 1,
            "tool_call_delta_chunk": 1,
            "turn_completed": 1,
        }
        # Three status pairs, start, the answered request, the answer and its
        # completion. The ten ignored notifications were ten more rows.
        rows = store.replay_operation_events(turn.id, limit=1_000)
        assert len(rows) == len(events) == 10
        assert [row.event_type for row in rows].count("harness.notice") == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_ignored_notification_names_are_bounded(tmp_path):
    frames = [
        {"method": f"_x.ai/vendor/event-{index}", "params": {"sessionId": "sess-1"}}
        for index in range(50)
    ]

    async def scenario() -> dict[str, int]:
        rpc = _VendorFrameRpc(frames, {"stopReason": "end_turn"})
        store, engagement, profile, service = _grok_runtime(
            tmp_path, MemoryGrokAdapter(rpc)
        )
        _chat, _owner, turn = _chat_turn(service, engagement, profile, "question")
        await service.start_chat_turn(turn.id)
        completed = next(
            event
            for event in service.activity_events(turn.id).events
            if event.type == "completed"
        )
        await service.shutdown()
        return completed.payload["ignored_notifications"]

    counts = asyncio.run(scenario())
    assert len(counts) == 33
    assert counts["other"] == 18
    assert sum(counts.values()) == 50


def test_mission_turn_activity_replays_only_that_turns_rows(tmp_path):
    store, engagement, profile, _mcp, _adapter, runtime_service = _harness_runtime(
        tmp_path
    )
    session = store.create(
        HarnessSession(
            engagement_id=engagement.id,
            harness_profile_id=profile.id,
            model="test-model",
        )
    )
    first, second = (
        store.create(
            HarnessTurn(
                engagement_id=engagement.id,
                harness_session_id=session.id,
                origin=HarnessTurnOrigin.MISSION,
                run_id="mission-run",
                prompt=prompt,
            )
        )
        for prompt in ("first", "second")
    )
    for index in range(3):
        for turn in (first, second):
            runtime_service._persist_activity(
                turn,
                session,
                HarnessEvent(
                    type="output_delta",
                    vendor=HarnessKind.CODEX_APP_SERVER,
                    item_id=f"{turn.prompt}-{index}",
                    item_kind="reasoning",
                    stream="commentary",
                    delta=f"{turn.prompt} {index}",
                ),
            )
        # The mission's own events share the run ledger.
        store.append_event("mission-run", "task.progress", {"index": index})

    page = runtime_service.activity_events(first.id)
    assert [event.delta for event in page.events] == ["first 0", "first 1", "first 2"]
    assert {event.harness_turn_id for event in page.events} == {first.id}

    # Pages follow the turn's own rows and end on its last one.
    head = runtime_service.activity_events(second.id, limit=2)
    assert [event.delta for event in head.events] == ["second 0", "second 1"]
    rest = runtime_service.activity_events(
        second.id, after_sequence=head.next_sequence, limit=2
    )
    assert [event.delta for event in rest.events] == ["second 2"]
    done = runtime_service.activity_events(second.id, after_sequence=rest.next_sequence)
    assert done.events == [] and done.next_sequence == rest.next_sequence
