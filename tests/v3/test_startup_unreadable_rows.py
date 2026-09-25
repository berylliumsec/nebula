"""One unreadable stored record must not stop Core from starting or recovering.

Startup and the periodic recovery pass read every in-flight record of many
kinds. A row that no longer validates (a torn write, a payload from a newer
Core, a manual edit) raised ``CorruptRecordError`` for its whole page, which
aborted the lifespan, so the service manager restarted Core into the same
failure. Each case stores one unreadable record of a kind the startup path
reads, in the state that read selects, beside a readable record in the same
state: Core must start, recover the readable record, and report the
unreadable one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nebula.v3.api as api_module
import nebula.v3.storage as storage_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalContinuation,
    AutomationSession,
    AutomationSessionStatus,
    ChatGoal,
    ChatGoalStatus,
    ChatQueue,
    ChatSchedule,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    ExecutionOrigin,
    ExecutionOriginKind,
    ExecutionRuntimeSnapshot,
    GeneratedDraft,
    GeneratedDraftStatus,
    HarnessInteraction,
    HarnessInteractionKind,
    HarnessInteractionStatus,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    KnowledgeSource,
    LibraryItem,
    NativeHookExecution,
    OperatorExecution,
    OperatorExecutionStatus,
    ProviderProfile,
    ReportRender,
    ReportRenderStatus,
    RiskClass,
    RunBackend,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    RunStatus,
    ScopeImport,
    ScopeImportStatus,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.knowledge import migrate_inline_knowledge_indexes
from nebula.v3.runtime_platform import default_runtime_platform
from nebula.v3.storage import NebulaStore, NotFoundError

AUTH = {"Authorization": "Bearer test-token"}
DIGEST = "sha256:" + "0" * 64
HEX = "0" * 64


@dataclass
class Case:
    """Records to store, which of them to make unreadable, and what recovery must do."""

    records: Callable[[], list]
    unreadable: list[str]
    recovered: Callable[[NebulaStore], None] = lambda _store: None
    # A scan reports each row it skips; a record read by id fails only the
    # recovery of the item that needed it.
    scanned: bool = True
    # State outside the entity table that makes startup read the records.
    seed: Callable[[NebulaStore], None] = lambda _store: None


def _chat(session_id: str, **changes) -> ChatSession:
    return ChatSession(
        id=session_id,
        engagement_id="project",
        title=session_id,
        provider_profile_id="provider",
        model="model",
        **changes,
    )


def _turn(turn_id: str, session_id: str, status: ChatTurnStatus, **changes):
    return ChatTurn(
        id=turn_id,
        engagement_id="project",
        session_id=session_id,
        provider_profile_id="provider",
        model="model",
        status=status,
        **changes,
    )


def _status(model, entity_id: str, expected) -> Callable[[NebulaStore], None]:
    def check(store: NebulaStore) -> None:
        assert store.get(model, entity_id).status == expected

    return check


def _interrupted_terminals(store: NebulaStore) -> None:
    for project in ("project", "unreadable"):
        store.append_operation_event(
            f"terminal-{project}",
            "container_terminal",
            project,
            "container_terminal.running",
            {"status": "running"},
            actor_id="operator",
        )


def _terminal_settled(store: NebulaStore) -> None:
    [*_, settled] = store.replay_operation_events("terminal-project")
    assert settled.event_type == "container_terminal.terminal"
    assert settled.payload["status"] == "interrupted"
    [left] = store.replay_operation_events("terminal-unreadable")
    assert left.event_type == "container_terminal.running"


def _automation_session(session_id: str) -> AutomationSession:
    return AutomationSession(
        id=session_id,
        engagement_id="project",
        owner_kind="chat",
        owner_id="chat-a",
        runtime_image="image",
        runtime_digest=DIGEST,
        runner_profile_id="removed-runner",
        runner_profile_revision=1,
        policy_id="policy",
        policy_revision=1,
        status=AutomationSessionStatus.READY,
    )


def _command(execution_id: str) -> CommandExecution:
    return CommandExecution(
        id=execution_id,
        engagement_id="project",
        session_id="runtime-session",
        process_id=f"process-{execution_id}",
        command="sleep 60",
        command_sha256=HEX,
        runtime_digest=DIGEST,
        policy_revision=1,
        status=CommandExecutionStatus.RUNNING,
    )


def _runner(profile_id: str) -> RunnerProfile:
    return RunnerProfile(
        id=profile_id,
        name=profile_id,
        runtime=RunnerRuntime.PODMAN,
        executable="/nonexistent/podman",
        platform="linux/amd64",
        isolation=RunnerIsolation.ROOTLESS,
    )


def _operator_execution(execution_id: str) -> OperatorExecution:
    return OperatorExecution(
        id=execution_id,
        engagement_id="project",
        operator_id="operator",
        origin=ExecutionOrigin(kind=ExecutionOriginKind.RERUN, execution_id="earlier"),
        language="bash",
        source_sha256=HEX,
        source_artifact_id="source",
        runtime=ExecutionRuntimeSnapshot(
            language="bash",
            interpreter="/bin/bash",
            runtime_digest=DIGEST,
            image="image",
            runner_profile_id="removed-runner",
            runner_profile_revision=1,
            runner_runtime=RunnerRuntime.PODMAN,
            runner_isolation=RunnerIsolation.ROOTLESS,
            runner_executable="/nonexistent/podman",
            runner_platform="linux/amd64",
        ),
        preview_fingerprint=HEX,
        request_fingerprint=HEX,
        client_idempotency_key=execution_id,
        status=OperatorExecutionStatus.RUNNING,
    )


def _render(render_id: str) -> ReportRender:
    return ReportRender(
        id=render_id,
        engagement_id="project",
        report_id="report",
        report_revision=1,
        input_fingerprint=HEX,
        template_version="1",
        renderer_version="1",
        status=ReportRenderStatus.RENDERING,
    )


def _scope_import(import_id: str) -> ScopeImport:
    return ScopeImport(
        id=import_id,
        engagement_id="project",
        artifact_id="artifact",
        filename="scope.txt",
        source_type="text",
        source_sha256=HEX,
        status=ScopeImportStatus.GENERATING,
    )


def _draft(draft_id: str) -> GeneratedDraft:
    return GeneratedDraft(
        id=draft_id,
        engagement_id="project",
        execution_id="execution",
        provider_profile_id="provider",
        model="model",
        prompt_version="1",
        context_fingerprint=HEX,
        status=GeneratedDraftStatus.GENERATING,
    )


def _goal(goal_id: str, session_id: str) -> ChatGoal:
    return ChatGoal(
        id=goal_id,
        engagement_id="project",
        session_id=session_id,
        objective="Keep going",
        completion_criteria=["Done"],
        status=ChatGoalStatus.RUNNING,
        active_since=utc_now(),
        execution_owner_id="previous-core",
        execution_claim_id="claim",
        execution_claimed_at=utc_now(),
    )


def _subagent(record_id: str, child_session_id: str) -> ChatSubagent:
    return ChatSubagent(
        id=record_id,
        engagement_id="project",
        parent_session_id="chat-a",
        parent_turn_id="parent-turn",
        child_session_id=child_session_id,
        name=record_id,
        task="Look around",
        status=ChatSubagentStatus.RUNNING,
    )


def _tool_call(call_id: str, turn_id: str, status: ToolCallStatus, **changes):
    return ToolCall(
        id=call_id,
        engagement_id="project",
        run_id=f"chat:{turn_id}",
        chat_session_id="chat-a",
        chat_turn_id=turn_id,
        origin=ToolCallOrigin.CHAT,
        tool_name="workspace.read",
        risk_class=RiskClass.LOCAL_READ,
        status=status,
        **changes,
    )


def _hook(hook_id: str, turn_id: str) -> NativeHookExecution:
    return NativeHookExecution(
        id=hook_id,
        engagement_id="project",
        chat_session_id="chat-a",
        chat_turn_id=turn_id,
        hook_id="hook",
        hook_snapshot={},
        event_name="tool.before",
        started_at=utc_now(),
    )


def _harness_session(session_id: str) -> HarnessSession:
    return HarnessSession(
        id=session_id,
        engagement_id="project",
        harness_profile_id="harness",
        model="model",
    )


def _harness_turn(turn_id: str, session_id: str) -> HarnessTurn:
    return HarnessTurn(
        id=turn_id,
        engagement_id="project",
        harness_session_id=session_id,
        origin=HarnessTurnOrigin.ANALYSIS,
        prompt="Look around",
        status=HarnessTurnStatus.RUNNING,
    )


def _scheduled_harness_run(run_id: str) -> AgentRun:
    return AgentRun(
        id=run_id,
        engagement_id="project",
        objective="Scheduled",
        backend=RunBackend.HARNESS,
        harness_profile_id="harness",
        status=RunStatus.QUEUED,
        metadata={"scheduled_for": (utc_now() + timedelta(hours=1)).isoformat()},
    )


def _interaction(interaction_id: str) -> HarnessInteraction:
    return HarnessInteraction(
        id=interaction_id,
        engagement_id="project",
        harness_turn_id="harness-turn",
        harness_session_id="harness-session",
        origin=HarnessTurnOrigin.CHAT,
        chat_session_id="chat-a",
        kind=HarnessInteractionKind.USER_INPUT,
        vendor_request_id=interaction_id,
        status=HarnessInteractionStatus.PENDING,
    )


def _approval(approval_id: str) -> Approval:
    return Approval(
        id=approval_id,
        engagement_id="project",
        run_id="run",
        risk_class=RiskClass.ACTIVE_SCAN,
        exact_request={"tool_name": "scan", "arguments": {}},
        policy_rationale="active operation",
        requested_by="agent",
        continuation=ApprovalContinuation(harness_turn_id="harness-turn"),
    )


def _schedule(schedule_id: str, session_id: str) -> ChatSchedule:
    return ChatSchedule(
        id=schedule_id,
        engagement_id="project",
        session_id=session_id,
        provider_profile_id="provider",
        model="model",
        interval_seconds=3_600,
        next_run_at=utc_now() - timedelta(seconds=1),
    )


def _removed(model, entity_id: str) -> Callable[[NebulaStore], None]:
    def check(store: NebulaStore) -> None:
        with pytest.raises(NotFoundError):
            store.get(model, entity_id)

    return check


def _approval_failed(store: NebulaStore) -> None:
    continuation = store.get(Approval, "readable").continuation
    assert continuation is not None and continuation.status == "failed"


def _schedule_skipped(store: NebulaStore) -> None:
    assert store.get(ChatSchedule, "readable").last_status == "skipped"


def _harness_run_settled(store: NebulaStore) -> None:
    assert store.get(AgentRun, "readable").status != RunStatus.QUEUED


EXPIRED = utc_now() - timedelta(days=2)
STALE = utc_now() - timedelta(hours=1)
CASES: dict[str, Case] = {
    "automation_sessions": Case(
        lambda: [_automation_session("readable"), _automation_session("unreadable")],
        ["unreadable"],
        _status(AutomationSession, "readable", AutomationSessionStatus.INTERRUPTED),
    ),
    "command_executions": Case(
        lambda: [_command("readable"), _command("unreadable")],
        ["unreadable"],
        _status(CommandExecution, "readable", CommandExecutionStatus.INTERRUPTED),
    ),
    "providers": Case(
        lambda: [
            ProviderProfile(id="unreadable", name="Broken", provider_type="openrouter")
        ],
        ["unreadable"],
    ),
    "runner_profiles": Case(
        lambda: [_runner("readable"), _runner("unreadable")], ["unreadable"]
    ),
    # Terminal recovery reads projects only to settle interrupted sessions.
    "engagements": Case(
        lambda: [Engagement(id="unreadable", name="Broken")],
        ["unreadable"],
        _terminal_settled,
        seed=_interrupted_terminals,
    ),
    "operator_executions": Case(
        lambda: [_operator_execution("readable"), _operator_execution("unreadable")],
        ["unreadable"],
        _status(OperatorExecution, "readable", OperatorExecutionStatus.INTERRUPTED),
    ),
    "report_renders": Case(
        lambda: [_render("readable"), _render("unreadable")],
        ["unreadable"],
        _status(ReportRender, "readable", ReportRenderStatus.INTERRUPTED),
    ),
    "scope_imports": Case(
        lambda: [_scope_import("readable"), _scope_import("unreadable")],
        ["unreadable"],
        _status(ScopeImport, "readable", ScopeImportStatus.FAILED),
    ),
    "generated_drafts": Case(
        lambda: [_draft("readable"), _draft("unreadable")],
        ["unreadable"],
        _status(GeneratedDraft, "readable", GeneratedDraftStatus.FAILED),
    ),
    "chat_turns-routing": Case(
        lambda: [
            _turn("readable", "chat-a", ChatTurnStatus.ROUTING),
            _turn("unreadable", "chat-b", ChatTurnStatus.ROUTING),
        ],
        ["unreadable"],
        _status(ChatTurn, "readable", ChatTurnStatus.INTERRUPTED),
    ),
    "chat_turns-waiting_callback": Case(
        lambda: [
            _turn("readable", "chat-a", ChatTurnStatus.WAITING_CALLBACK),
            _turn("unreadable", "chat-b", ChatTurnStatus.WAITING_CALLBACK),
        ],
        ["unreadable"],
    ),
    "chat_turns-interrupted": Case(
        lambda: [
            _turn(
                "readable",
                "chat-a",
                ChatTurnStatus.INTERRUPTED,
                request_snapshot={"recovery": {"required": True}},
            ),
            _turn(
                "unreadable",
                "chat-b",
                ChatTurnStatus.INTERRUPTED,
                request_snapshot={"recovery": {"required": True}},
            ),
        ],
        ["unreadable"],
    ),
    "chat_goals": Case(
        lambda: [_goal("readable", "chat-a"), _goal("unreadable", "chat-b")],
        ["unreadable"],
        _status(ChatGoal, "readable", ChatGoalStatus.PAUSED),
    ),
    "chat_subagents": Case(
        lambda: [
            _subagent("readable", "child-a"),
            _subagent("unreadable", "child-b"),
        ],
        ["unreadable"],
        _status(ChatSubagent, "readable", ChatSubagentStatus.INTERRUPTED),
    ),
    "tool_calls-running": Case(
        # One of the routing turn's own calls, which startup reads to settle it.
        lambda: [
            _turn("readable", "chat-a", ChatTurnStatus.ROUTING),
            _tool_call("readable-call", "readable", ToolCallStatus.RUNNING),
            _tool_call("unreadable", "readable", ToolCallStatus.RUNNING),
        ],
        ["unreadable"],
        _status(ChatTurn, "readable", ChatTurnStatus.INTERRUPTED),
    ),
    "tool_calls-approved": Case(
        # Approved for a turn that ended before the call started.
        lambda: [
            _turn("readable", "chat-a", ChatTurnStatus.COMPLETE),
            _tool_call(
                "readable-call",
                "readable",
                ToolCallStatus.APPROVED,
                created_at=STALE,
                updated_at=STALE,
            ),
            _tool_call(
                "unreadable",
                "readable",
                ToolCallStatus.APPROVED,
                created_at=STALE,
                updated_at=STALE,
            ),
        ],
        ["unreadable"],
        _status(ToolCall, "readable-call", ToolCallStatus.CANCELLED),
    ),
    "native_hook_executions": Case(
        lambda: [
            _turn("readable", "chat-a", ChatTurnStatus.ROUTING),
            _hook("unreadable", "readable"),
        ],
        ["unreadable"],
        _status(ChatTurn, "readable", ChatTurnStatus.INTERRUPTED),
    ),
    "harness_turns": Case(
        lambda: [
            _harness_session("harness-session"),
            _harness_turn("readable", "harness-session"),
            _harness_turn("unreadable", "harness-session"),
        ],
        ["unreadable"],
        _status(HarnessTurn, "readable", HarnessTurnStatus.INTERRUPTED),
    ),
    "harness_sessions": Case(
        # Read for each in-flight harness turn, not scanned.
        lambda: [
            _harness_session("unreadable"),
            _harness_turn("broken-session-turn", "unreadable"),
            _harness_session("harness-session"),
            _harness_turn("readable", "harness-session"),
        ],
        ["unreadable"],
        _status(HarnessTurn, "readable", HarnessTurnStatus.INTERRUPTED),
        scanned=False,
    ),
    "runs": Case(
        # A stalled series makes startup look for its other active runs.
        lambda: [
            AgentRun(
                id="stalled-series",
                engagement_id="project",
                objective="Recurring",
                status=RunStatus.COMPLETE,
                metadata={
                    "origin": "api",
                    "recurrence_error": "capacity",
                    "series_id": "series",
                },
            ),
            _scheduled_harness_run("readable"),
            _scheduled_harness_run("unreadable"),
        ],
        ["unreadable"],
        _harness_run_settled,
    ),
    "harness_interactions": Case(
        lambda: [_interaction("readable"), _interaction("unreadable")],
        ["unreadable"],
        _status(HarnessInteraction, "readable", HarnessInteractionStatus.CANCELLED),
    ),
    "approvals": Case(
        lambda: [_approval("readable"), _approval("unreadable")],
        ["unreadable"],
        _approval_failed,
    ),
    "chat_sessions": Case(
        lambda: [
            _chat(
                "readable",
                metadata={"temporary_assistant": True},
                created_at=EXPIRED,
                updated_at=EXPIRED,
            ),
            _chat(
                "unreadable",
                metadata={"temporary_assistant": True},
                created_at=EXPIRED,
                updated_at=EXPIRED,
            ),
        ],
        ["unreadable"],
        _removed(ChatSession, "readable"),
    ),
    "chat_schedules": Case(
        lambda: [_schedule("readable", "chat-a"), _schedule("unreadable", "chat-b")],
        ["unreadable"],
        _schedule_skipped,
    ),
    "chat_queues": Case(
        lambda: [
            ChatQueue(id="readable", engagement_id="project", session_id="chat-a"),
            ChatQueue(id="unreadable", engagement_id="project", session_id="chat-b"),
        ],
        ["unreadable"],
    ),
}


def _make_unreadable(store: NebulaStore, entity_id: str) -> None:
    # A revision below 1 fails validation while leaving every field a scan
    # filters on intact, so the scan still selects the row.
    with store.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE entities SET payload = json_set(payload, '$.revision', 0) "
            "WHERE id = ?",
            (entity_id,),
        )


@pytest.mark.parametrize("name", sorted(CASES))
def test_core_starts_and_recovers_beside_an_unreadable_record(
    tmp_path: Path, monkeypatch, name: str
):
    case = CASES[name]
    store = NebulaStore(tmp_path / "nebula.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    store.create(
        HarnessProfile(
            id="harness",
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
        )
    )
    for session_id in ("chat-a", "chat-b", "child-a", "child-b"):
        store.create(_chat(session_id))
    for record in case.records():
        store.create(record)
    case.seed(store)
    for entity_id in case.unreadable:
        _make_unreadable(store, entity_id)

    skipped: list[str] = []
    record = storage_module.record_caught_exception

    def recording(feature, event_code, message, exception, **kwargs):
        if event_code == "storage.scan.skipped_unreadable_record":
            skipped.append(kwargs["metadata"]["entity_id"])
        return record(feature, event_code, message, exception, **kwargs)

    monkeypatch.setattr(storage_module, "record_caught_exception", recording)
    services: list[ChatService] = []

    class RecordedChatService(ChatService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            services.append(self)

    monkeypatch.setattr(api_module, "ChatService", RecordedChatService)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="test-token",
        tool_platform=default_runtime_platform(
            store=store, artifact_store=artifacts, data_root=tmp_path
        ),
        execution_data_root=tmp_path,
    )

    with TestClient(app) as client:
        # The periodic pass reads the same kinds again every 15 seconds.
        client.portal.call(services[0].fire_due_schedules)
        client.portal.call(services[0].recovery_tick)
        # The temporary-conversation sweep runs beside the lifespan.
        client.portal.call(asyncio.sleep, 0.2)
        assert client.get("/api/v1/setup/status", headers=AUTH).status_code == 200
        case.recovered(store)

    if case.scanned:
        assert set(case.unreadable) <= set(skipped)


class _RecordingIndex:
    descriptor = {"knowledge_index": "test"}
    library_descriptor = {"knowledge_index": "test-library"}

    def __init__(self) -> None:
        self.indexed: list[str] = []

    def upsert_source(self, source, chunks) -> None:
        self.indexed.append(source.id)

    def upsert_library_item(self, item, chunks) -> None:
        self.indexed.append(item.id)


def test_startup_index_migration_moves_readable_records_beside_unreadable_ones(
    tmp_path: Path, monkeypatch
):
    """Core migrates legacy inline chunks as it starts, before any component."""

    store = NebulaStore(tmp_path / "nebula.db")
    store.create(Engagement(id="project", name="Project"))
    chunks = {"chunks": [{"text": "Scope notes"}]}
    for suffix in ("readable", "unreadable"):
        store.create(
            KnowledgeSource(
                id=f"source-{suffix}",
                engagement_id="project",
                name="Notes",
                source_type="text",
                metadata=chunks,
            )
        )
        store.create(
            LibraryItem(
                id=f"library-{suffix}",
                name="Notes",
                source_type="text",
                metadata=chunks,
            )
        )
        if suffix == "unreadable":
            _make_unreadable(store, f"source-{suffix}")
            _make_unreadable(store, f"library-{suffix}")
    skipped: list[str] = []
    record = storage_module.record_caught_exception

    def recording(feature, event_code, message, exception, **kwargs):
        if event_code == "storage.scan.skipped_unreadable_record":
            skipped.append(kwargs["metadata"]["entity_id"])
        return record(feature, event_code, message, exception, **kwargs)

    monkeypatch.setattr(storage_module, "record_caught_exception", recording)
    index = _RecordingIndex()

    migrated = migrate_inline_knowledge_indexes(store=store, knowledge_index=index)  # type: ignore[arg-type]

    assert migrated == 2
    assert index.indexed == ["source-readable", "library-readable"]
    assert "chunks" not in store.get(KnowledgeSource, "source-readable").metadata
    assert sorted(skipped) == ["library-unreadable", "source-unreadable"]


def test_a_readable_scan_reaches_every_match_while_its_caller_settles_them(
    tmp_path: Path,
):
    """A recovery pass moves each record out of its filter as it goes.

    Offset pages would then skip records: after the first page is settled, the
    second page's offset points past records that moved up. The scan reads
    the matching ids first, so every record that matched is reached once.
    """

    store = NebulaStore(tmp_path / "nebula.db")
    store.create(Engagement(id="project", name="Project"))
    for index in range(7):
        store.create(_render(f"render-{index}"))
    _make_unreadable(store, "render-3")
    settled: list[str] = []

    for render in store.iter_readable_entities(
        ReportRender, {"status": ReportRenderStatus.RENDERING.value}, page_size=2
    ):
        store.update(
            ReportRender,
            render.id,
            {"status": ReportRenderStatus.INTERRUPTED},
            expected_revision=render.revision,
        )
        settled.append(render.id)
        if render.id == "render-4":
            # A record removed before its page is read is left out.
            store.delete(ReportRender, "render-6")

    assert settled == ["render-0", "render-1", "render-2", "render-4", "render-5"]
    assert (
        list(
            store.iter_readable_entities(
                ReportRender, {"status": ReportRenderStatus.RENDERING.value}
            )
        )
        == []
    )
