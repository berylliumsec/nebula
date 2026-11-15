"""Concurrent tool bookkeeping on one running turn never fails the turn.

A harness turn's tool calls are recorded on its HarnessTurn and on the ChatTurn
it mirrors. A vendor-native command (recorded by the event stream) and a Core
tool called through Nebula's gateway (recorded by the gateway request) save
that bookkeeping concurrently. Each write used the revision its own earlier read
returned, so whichever saved second failed with ``revision conflict: expected
N, found N+1``; on the event stream that interrupted the turn and paused its
goal, after the tools had already run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from nebula.v3.chat import HarnessKnowledgeMatch
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ChatCitation,
    ChatGoalStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    ChatWorkingNotes,
    HarnessProfile,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTurn,
    HarnessTurnStatus,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallStatus,
)
from nebula.v3.harness_core_tools import call_core_tool
from nebula.v3.harnesses import HarnessEvent, HarnessPermissionRequest
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import (
    ConflictError,
    NebulaStore,
    RevisionConflict,
    revise_latest,
)
from nebula.v3.tools import ToolExecutionResult, ToolSpec
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_harness_chat_goals import _new_goal
from tests.v3.test_harnesses import FakeConnection, _runtime

pytestmark = pytest.mark.anyio

_TERMINAL = {
    HarnessTurnStatus.COMPLETE,
    HarnessTurnStatus.CANCELLED,
    HarnessTurnStatus.FAILED,
    HarnessTurnStatus.INTERRUPTED,
}


def _land_after_read(
    store: NebulaStore,
    model: type,
    competing: Callable[[Any], Awaitable[None]],
    *,
    armed: Callable[[], bool],
) -> None:
    """Let ``competing`` save between a bookkeeping read and its write.

    The first read of ``model`` while ``armed()`` returns the saved record,
    then runs ``competing`` to completion before the reader resumes. The
    reader's next write is therefore planned on a revision that is no longer
    the newest, exactly as when two coroutines interleave on a busy worker.
    """

    original_get = store.get
    fired = False

    async def get(requested, entity_id):
        nonlocal fired
        value = await original_get(requested, entity_id)
        if requested is model and not fired and armed():
            fired = True
            await competing(value)
        return value

    store.get = get  # type: ignore[method-assign]


async def _settled_turn(store: NebulaStore, chat_turn_id: str) -> HarnessTurn:
    """The settled harness turn behind a dispatched goal's chat turn."""

    for _ in range(1_000):
        turns = await store.find_entities(HarnessTurn, {"chat_turn_id": chat_turn_id})
        if turns and turns[0].status in _TERMINAL:
            return turns[0]
        await asyncio.sleep(0.01)
    pytest.fail("the harness turn did not settle")


async def _settled_goal(goals: ChatGoalService, session_id: str):
    for _ in range(1_000):
        goal = await goals.get(session_id)
        if goal.status != ChatGoalStatus.RUNNING:
            return goal
        await asyncio.sleep(0.01)
    pytest.fail("the goal did not settle")


class _NativeCommandConnection(FakeConnection):
    """A vendor turn that runs one native command, then answers."""

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.external_session_id = self.external_session_id or "vendor-native"
        yield HarnessEvent(type="started", external_session_id=self.external_session_id)
        for kind in ("tool_started", "tool_completed"):
            yield HarnessEvent(
                type=kind,
                server_id="codex",
                tool_name="commandExecution",
                item_id="native-command-1",
                payload={"arguments": {"command": "ls"}, "result": "ok"},
            )
        yield HarnessEvent(type="completed", message="Listed the workspace.")


async def _goal_turn_with_native_command(tmp_path):
    store, _engagement, _profile, _mcp, adapter, runtime = await _runtime(tmp_path)

    async def open_native(request):
        connection = _NativeCommandConnection(request)
        adapter.connections.append(connection)
        return connection

    adapter.open = open_native
    goals = ChatGoalService(store, runtime)
    created = await _new_goal(goals, step_budget=1)
    (
        await goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
    )
    return store, runtime, goals, created


def _arm_on_native_call(store: NebulaStore) -> Callable[[], bool]:
    """Arm once the event stream has saved the native command's ledger row."""

    original_create = store.create
    native = False

    async def create(entity):
        nonlocal native
        saved = await original_create(entity)
        if isinstance(entity, ToolCall) and entity.tool_name.startswith("vendor:"):
            native = True
        return saved

    store.create = create  # type: ignore[method-assign]
    return lambda: native


async def test_native_command_and_core_tool_bookkeeping_overlap_in_a_goal_turn(
    tmp_path,
):
    """The operator's case: both tools ran; the turn and its goal carry on."""

    store, runtime, goals, created = await _goal_turn_with_native_command(tmp_path)
    core_results: list[dict[str, Any]] = []

    async def core_tool_call(turn: HarnessTurn) -> None:
        # Nebula's MCP gateway serves the vendor's Core tool request while the
        # event stream is between reading and saving the native command.
        session = await store.get(HarnessSession, turn.harness_session_id)
        core_results.append(
            await runtime._gateway_call(
                session, "notes.write", {"content": "Workspace listing reviewed."}
            )
        )

    _land_after_read(
        store, HarnessTurn, core_tool_call, armed=_arm_on_native_call(store)
    )
    chat_turn_id = await runtime.dispatch_running_goal(created.session.id, "Begin work")
    assert chat_turn_id is not None
    turn = await _settled_turn(store, chat_turn_id)
    goal = await _settled_goal(goals, created.session.id)

    assert len(core_results) == 1
    assert core_results[0]["isError"] is False, core_results[0]
    # Neither the turn nor its goal stops on the other tool's bookkeeping.
    assert (turn.status, turn.error, goal.status, goal.blocked_reason) == (
        HarnessTurnStatus.COMPLETE,
        None,
        ChatGoalStatus.PAUSED,
        "Goal step budget is exhausted.",
    ), (
        f"turn {turn.status.value}: {turn.error!r}; goal {goal.status.value}: {goal.blocked_reason!r}"
    )

    calls = await store.find_entities(ToolCall, {"metadata.harness_turn_id": turn.id})
    by_name = {call.tool_name: call for call in calls}
    assert set(by_name) == {"vendor:codex:commandExecution", "notes.write"}
    assert all(call.status == ToolCallStatus.COMPLETE for call in calls)
    # Each tool's bookkeeping is kept once, whichever writer saved first.
    assert sorted(turn.tool_call_ids) == sorted(call.id for call in calls)
    chat_turn = await store.get(ChatTurn, turn.chat_turn_id)
    assert sorted(chat_turn.tool_call_ids) == sorted(call.id for call in calls)
    assert chat_turn.execution_tool_calls + chat_turn.artifact_queries == 2
    assert chat_turn.next_step == 2
    # The Core tool's effect happened once.
    notes = await store.list_session_entities(ChatWorkingNotes, created.session.id)
    assert [item.content for item in notes] == ["Workspace listing reviewed."]
    await runtime.shutdown()


async def test_native_command_saved_while_a_core_tool_records_its_completion(
    tmp_path,
):
    """A Core tool's post-effect bookkeeping is retried, never its effect.

    ``goal.complete`` has run (its proposal was accepted) when its record on
    the chat turn conflicts with the native command's record. Only that
    record is planned again on the newest turn; the tool does not run twice
    and the native command's counters and id stay.
    """

    store, engagement, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    chat, chat_turn, turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Finish the goal",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    turn = await store.update(
        HarnessTurn,
        turn.id,
        {"status": HarnessTurnStatus.RUNNING},
        expected_revision=turn.revision,
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    effects: list[str] = []

    class Broker:
        async def execute(self, invocation, _scope, *, approval=None):
            effects.append(invocation.tool_name)
            return ToolExecutionResult(output={"accepted": True})

    spec = ToolSpec(
        name="goal.complete",
        description="Synthetic completion proposal",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
    )
    components = RuntimeToolComponents(
        broker=Broker(),
        scope=ScopePolicy(engagement_id=engagement.id),
        workspace=tmp_path,
        specs={spec.name: spec},
    )
    native: list[ToolCall] = []

    async def native_command(_chat_turn: ChatTurn) -> None:
        call = await store.create(
            ToolCall(
                engagement_id=engagement.id,
                run_id=chat_turn.id,
                chat_session_id=chat.id,
                chat_turn_id=chat_turn.id,
                tool_name="vendor:codex:commandExecution",
                vendor_tool_name="codex:commandExecution",
                status=ToolCallStatus.RUNNING,
                risk_class=RiskClass.LOCAL_READ,
                metadata={"harness_turn_id": turn.id, "budget_class": "execution"},
            )
        )
        native.append(await runtime._attach_gateway_tool_call(turn, call.id))

    _land_after_read(store, ChatTurn, native_command, armed=lambda: bool(effects))
    result = await call_core_tool(
        runtime,
        session,
        turn,
        components,
        "goal.complete",
        {"summary": "Done", "evidence": ["notes"]},
    )

    assert result["structuredContent"] == {"accepted": True}
    assert effects == ["goal.complete"]
    saved = await store.get(ChatTurn, chat_turn.id)
    assert saved.request_snapshot["goal_completion_proposal"] == {
        "summary": "Done",
        "evidence": ["notes"],
    }
    assert native and native[0].id in saved.tool_call_ids
    assert len(saved.tool_call_ids) == 2
    assert (saved.execution_tool_calls, saved.artifact_queries) == (1, 1)
    assert saved.next_step == 2
    await runtime.shutdown()


async def test_native_command_bookkeeping_that_never_saves_reconciles_from_ledger(
    tmp_path,
):
    """Bookkeeping that keeps losing is merged from the ledger, not fatal.

    Another writer saves the harness turn before every attempt to record the
    native command. The command's ledger row is saved, so the turn still
    completes, its record lists the command, and the goal is not paused.
    """

    store, runtime, goals, created = await _goal_turn_with_native_command(tmp_path)
    armed = _arm_on_native_call(store)
    contending = True
    original_get = store.get
    competing_writes = 0

    async def get(requested, entity_id):
        nonlocal competing_writes
        value = await original_get(requested, entity_id)
        if requested is HarnessTurn and contending and armed():
            # A writer that always lands between this read and its write.
            await store.update(
                HarnessTurn,
                value.id,
                {"metadata": {**value.metadata, "contender": competing_writes}},
                expected_revision=value.revision,
            )
            competing_writes += 1
        return value

    store.get = get  # type: ignore[method-assign]
    original_persist = runtime._persist_activity

    async def persist(turn, session, event):
        nonlocal contending
        if event.type == "tool_completed":
            # The stream has moved past the command's bookkeeping.
            contending = False
        return await original_persist(turn, session, event)

    runtime._persist_activity = persist
    original_fail = runtime._fail_turn

    async def fail(*args, **kwargs):
        nonlocal contending
        # The failure itself is not contended, so a failed turn reports why.
        contending = False
        return await original_fail(*args, **kwargs)

    runtime._fail_turn = fail
    chat_turn_id = await runtime.dispatch_running_goal(created.session.id, "Begin work")
    assert chat_turn_id is not None
    turn = await _settled_turn(store, chat_turn_id)
    goal = await _settled_goal(goals, created.session.id)

    assert (turn.status, turn.error, goal.status, goal.blocked_reason) == (
        HarnessTurnStatus.COMPLETE,
        None,
        ChatGoalStatus.PAUSED,
        "Goal step budget is exhausted.",
    ), (
        f"turn {turn.status.value}: {turn.error!r}; goal {goal.status.value}: {goal.blocked_reason!r}"
    )
    # Every bounded attempt lost to the other writer.
    assert competing_writes > 1
    calls = await store.find_entities(ToolCall, {"metadata.harness_turn_id": turn.id})
    assert [call.tool_name for call in calls] == ["vendor:codex:commandExecution"]
    assert calls[0].status == ToolCallStatus.COMPLETE
    assert turn.tool_call_ids == [calls[0].id]
    chat_turn = await store.get(ChatTurn, turn.chat_turn_id)
    assert chat_turn.tool_call_ids == [calls[0].id]
    assert chat_turn.execution_tool_calls == 1
    await runtime.shutdown()


async def _running_chat_turn(tmp_path, *, mcp_server_ids=None):
    store, engagement, profile, mcp, _adapter, runtime = await _runtime(tmp_path)
    chat, chat_turn, turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Inspect the workspace",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[mcp.id] if mcp_server_ids is None else mcp_server_ids,
    )
    turn = await store.update(
        HarnessTurn,
        turn.id,
        {"status": HarnessTurnStatus.RUNNING},
        expected_revision=turn.revision,
    )
    return store, engagement, runtime, chat, chat_turn, turn


async def test_reconcile_tool_calls_uses_saved_ledger_without_in_memory_retry(tmp_path):
    """A crash after saving an effect loses no calls or accounting on recovery."""

    store, engagement, runtime, chat, chat_turn, turn = await _running_chat_turn(
        tmp_path
    )
    call = await store.create(
        ToolCall(
            engagement_id=engagement.id,
            run_id=chat_turn.id,
            chat_session_id=chat.id,
            chat_turn_id=chat_turn.id,
            tool_name="vendor:codex:commandExecution",
            status=ToolCallStatus.COMPLETE,
            risk_class=RiskClass.LOCAL_READ,
            metadata={"harness_turn_id": turn.id, "budget_class": "execution"},
        )
    )
    # No attachment was attempted in this process: only the durable effect
    # record remains, just as after restart before the projection was saved.
    await runtime._reconcile_turn_tool_calls(turn.id)
    await runtime._reconcile_turn_tool_calls(turn.id)
    saved = await store.get(HarnessTurn, turn.id)
    owner = await store.get(ChatTurn, chat_turn.id)
    assert saved.tool_call_ids == [call.id]
    assert owner.tool_call_ids == [call.id]
    assert owner.next_step == owner.execution_tool_calls == 1
    assert owner.artifact_queries == 0
    await runtime.shutdown()


async def test_terminal_owner_bookkeeping_uses_locked_fallback_once(tmp_path):
    store, _engagement, runtime, _chat, chat_turn, turn = await _running_chat_turn(
        tmp_path
    )
    original = runtime._complete_owner_once
    attempts = 0

    async def contended(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if kwargs.get("transaction") is None:
            raise RevisionConflict("another writer saved the owner")
        return await original(*args, **kwargs)

    runtime._complete_owner_once = contended
    await runtime._complete_owner(
        turn, "Saved once", ChatTokenUsage(), usage_reported=True
    )
    await runtime._complete_owner(
        turn, "Saved once", ChatTokenUsage(), usage_reported=True
    )
    saved = await store.get(ChatTurn, chat_turn.id)
    assert saved.status == ChatTurnStatus.COMPLETE
    from nebula.v3.domain import ChatMessage

    messages = await store.list_session_entities(ChatMessage, saved.session_id)
    assert [item.content for item in messages if item.role.value == "assistant"] == [
        "Saved once"
    ]
    assert attempts == 12
    await runtime.shutdown()


async def test_late_completion_keeps_stop_and_closed_session(tmp_path):
    store, runtime, goals, created = await _goal_turn_with_native_command(tmp_path)
    original_update = store.update
    stopped = False

    async def update(model, entity_id, changes, **kwargs):
        nonlocal stopped
        if (
            model is HarnessTurn
            and changes.get("status") == HarnessTurnStatus.COMPLETE
            and not stopped
        ):
            stopped = True
            current = await store.get(HarnessTurn, entity_id)
            await original_update(
                HarnessTurn,
                entity_id,
                {"status": HarnessTurnStatus.INTERRUPTED, "error": "Operator stopped"},
                expected_revision=current.revision,
            )
            session = await store.get(HarnessSession, current.harness_session_id)
            await original_update(
                HarnessSession,
                session.id,
                {"status": HarnessSessionStatus.CLOSED},
                expected_revision=session.revision,
            )
        return await original_update(model, entity_id, changes, **kwargs)

    store.update = update
    chat_turn_id = await runtime.dispatch_running_goal(created.session.id, "Begin work")
    assert chat_turn_id is not None
    turn = await _settled_turn(store, chat_turn_id)
    await _settled_goal(goals, created.session.id)
    assert stopped
    assert turn.status == HarnessTurnStatus.INTERRUPTED
    assert turn.error == "Operator stopped"
    assert (
        await store.get(HarnessSession, turn.harness_session_id)
    ).status == HarnessSessionStatus.CLOSED
    await runtime.shutdown()


async def test_completed_owner_does_not_reopen_cancelled_chat(tmp_path):
    store, _engagement, runtime, _chat, chat_turn, turn = await _running_chat_turn(
        tmp_path
    )
    await store.update(
        ChatTurn,
        chat_turn.id,
        {"status": ChatTurnStatus.CANCELLED},
        expected_revision=chat_turn.revision,
    )
    await runtime._complete_owner(
        turn, "Late answer", ChatTokenUsage(), usage_reported=True
    )
    assert (await store.get(ChatTurn, chat_turn.id)).status == ChatTurnStatus.CANCELLED
    await runtime.shutdown()


async def test_late_failure_keeps_successful_owner_session_and_profile(tmp_path):
    store, _engagement, runtime, _chat, chat_turn, turn = await _running_chat_turn(
        tmp_path
    )
    session = await store.get(HarnessSession, turn.harness_session_id)
    profile = await store.get(HarnessProfile, session.harness_profile_id)
    await store.update(
        HarnessTurn,
        turn.id,
        {"status": HarnessTurnStatus.COMPLETE},
        expected_revision=turn.revision,
    )
    await runtime._fail_turn(turn.id, HarnessTurnStatus.INTERRUPTED, "late failure")
    assert (await store.get(HarnessTurn, turn.id)).status == HarnessTurnStatus.COMPLETE
    assert (await store.get(ChatTurn, chat_turn.id)).status == chat_turn.status
    assert (await store.get(HarnessSession, session.id)).status == session.status
    assert (await store.get(HarnessProfile, profile.id)).revision == profile.revision
    await runtime.shutdown()


def _native_attacher(store, runtime, engagement, chat, chat_turn, turn):
    """A native command recorded by the event stream, as a competing writer."""

    attached: list[ToolCall] = []

    async def attach(_record) -> None:
        call = await store.create(
            ToolCall(
                engagement_id=engagement.id,
                run_id=chat_turn.id,
                chat_session_id=chat.id,
                chat_turn_id=chat_turn.id,
                tool_name="vendor:codex:commandExecution",
                vendor_tool_name="codex:commandExecution",
                status=ToolCallStatus.RUNNING,
                risk_class=RiskClass.LOCAL_READ,
                metadata={"harness_turn_id": turn.id, "budget_class": "execution"},
            )
        )
        attached.append(await runtime._attach_gateway_tool_call(turn, call.id))

    return attach, attached


async def test_revise_latest_plans_each_attempt_on_the_newest_record(tmp_path):
    store, _engagement, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    seen: list[int] = []

    async def other_writer(record) -> None:
        await store.update(
            HarnessProfile,
            record.id,
            {"default_model": "model-from-another-writer"},
            expected_revision=record.revision,
        )

    _land_after_read(store, HarnessProfile, other_writer, armed=lambda: True)

    def plan(latest: HarnessProfile) -> dict[str, Any]:
        seen.append(latest.revision)
        return {"name": f"{latest.name} (renamed)"}

    saved = await revise_latest(store, HarnessProfile, profile.id, plan)
    # The first plan lost to the other writer; the second saw its write.
    assert seen == [profile.revision, profile.revision + 1]
    assert (saved.name, saved.default_model) == (
        f"{profile.name} (renamed)",
        "model-from-another-writer",
    )
    assert saved.revision == profile.revision + 2
    # Nothing to save leaves the record alone.
    assert (await revise_latest(store, HarnessProfile, profile.id, lambda _: None)) == (
        saved
    )
    await runtime.shutdown()


async def test_revise_latest_is_bounded_and_keeps_other_conflicts(tmp_path):
    store, _engagement, profile, _mcp, _adapter, runtime = await _runtime(tmp_path)
    original_get = store.get

    async def always_moved(model, entity_id):
        value = await original_get(model, entity_id)
        if model is HarnessProfile:
            await store.update(
                HarnessProfile,
                value.id,
                {"default_model": f"model-{value.revision}"},
                expected_revision=value.revision,
            )
        return value

    store.get = always_moved  # type: ignore[method-assign]
    attempts = 0

    def plan(latest: HarnessProfile) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {"name": "never saved"}

    saved = await revise_latest(store, HarnessProfile, profile.id, plan, attempts=3)
    assert saved.name == "never saved"
    assert attempts == 4
    store.get = original_get  # type: ignore[method-assign]

    class Guarded:
        calls = 0

        async def get(self, model, entity_id):
            return await store.get(model, entity_id)

        async def update(self, model, entity_id, changes, *, expected_revision):
            self.calls += 1
            raise ConflictError("this project worker no longer owns the record")

    guarded = Guarded()
    with pytest.raises(ConflictError, match="no longer owns"):
        await revise_latest(guarded, HarnessProfile, profile.id, plan)
    # Only a stale revision is planned again; any other refusal is final.
    assert guarded.calls == 1
    await runtime.shutdown()


async def test_native_permission_request_waits_while_a_core_tool_records(tmp_path):
    """A native command's approval wait keeps a Core tool recorded meanwhile."""

    store, engagement, runtime, chat, chat_turn, turn = await _running_chat_turn(
        tmp_path
    )
    attach, attached = _native_attacher(
        store, runtime, engagement, chat, chat_turn, turn
    )
    _land_after_read(
        store, HarnessTurn, attach, armed=lambda: bool(runtime._approval_futures)
    )
    ticket = await runtime._request_permission(
        turn.id,
        HarnessPermissionRequest(
            vendor_request_id="delete-1",
            category="mcp",
            vendor_name="mcp__workspace__delete_file",
            server_name="workspace",
            tool_name="delete_file",
            arguments={"path": "evidence.txt"},
        ),
    )
    approval = await store.get(Approval, ticket.approval_id or "")
    assert approval.status == ApprovalStatus.PENDING
    saved = await store.get(HarnessTurn, turn.id)
    owner = await store.get(ChatTurn, chat_turn.id)
    assert saved.status == HarnessTurnStatus.WAITING_APPROVAL
    assert (owner.status, owner.approval_id) == (
        ChatTurnStatus.WAITING_APPROVAL,
        approval.id,
    )
    assert attached and attached[0].id in saved.tool_call_ids
    assert set(saved.tool_call_ids) == {ticket.tool_call_id, attached[0].id}
    assert set(owner.tool_call_ids) == set(saved.tool_call_ids)
    assert owner.execution_tool_calls == 2
    ticket.decision.cancel()
    await runtime.shutdown()


async def test_gateway_citations_and_a_native_command_are_both_kept(tmp_path):
    store, engagement, runtime, chat, chat_turn, turn = await _running_chat_turn(
        tmp_path, mcp_server_ids=[]
    )
    attach, attached = _native_attacher(
        store, runtime, engagement, chat, chat_turn, turn
    )
    _land_after_read(store, HarnessTurn, attach, armed=lambda: True)
    citation = ChatCitation(
        source_id="knowledge-a",
        name="runbook.md",
        chunk_id="chunk-a",
        excerpt="Page above 2%.",
    )
    (
        await runtime._record_searched_citations(
            turn.id,
            [
                HarnessKnowledgeMatch(
                    text="Page above 2%.",
                    citation=citation,
                    local_only=False,
                    relevance="strong",
                )
            ],
        )
    )
    saved = await store.get(HarnessTurn, turn.id)
    assert [item["chunk_id"] for item in saved.metadata["citations"]] == ["chunk-a"]
    assert saved.tool_call_ids == [attached[0].id]
    await runtime.shutdown()


async def test_a_failing_turn_ends_even_when_a_tool_records_meanwhile(tmp_path):
    """The failure is saved on the newest turn instead of leaving it running."""

    store, engagement, runtime, chat, chat_turn, turn = await _running_chat_turn(
        tmp_path, mcp_server_ids=[]
    )
    attach, attached = _native_attacher(
        store, runtime, engagement, chat, chat_turn, turn
    )
    _land_after_read(store, HarnessTurn, attach, armed=lambda: True)
    (
        await runtime._fail_turn(
            turn.id, HarnessTurnStatus.INTERRUPTED, "The harness transport closed."
        )
    )
    saved = await store.get(HarnessTurn, turn.id)
    owner = await store.get(ChatTurn, chat_turn.id)
    assert (saved.status, saved.error) == (
        HarnessTurnStatus.INTERRUPTED,
        "The harness transport closed.",
    )
    assert saved.tool_call_ids == [attached[0].id]
    assert owner.status == ChatTurnStatus.INTERRUPTED
    await runtime.shutdown()
