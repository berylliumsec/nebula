"""A provider turn parked on a callback wait that stops moving is recovered.

A turn waiting on its subagents, on a reply, or on a background command sat
in ``waiting_callback`` with nothing to move it: only a settled process had a
reconciler. The recovery tick now interrupts a wait with no progress for the
stall threshold as an orphaned turn is (open effects stay outcome unknown),
resumes it once, and leaves a second stall for the operator.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from datetime import timedelta
import asyncio
import json
from pathlib import Path

import pytest

from nebula.v3.chat import ChatService, callback_stall_seconds
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatSubagentMessageDirection,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    Engagement,
    ProjectHold,
    ProviderProfile,
    ToolCall as DurableToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.recovery_markers import (
    CALLBACK_STALL_CAUSE,
    CALLBACK_STALL_REPEATED_CAUSE,
    GOAL_CALLBACK_STALL,
    TURN_CALLBACK_STALL,
)
from nebula.v3.session_state import session_state
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _drain,
    _finish,
    _history,
    _request,
    _response,
    _setup,
    _until,
)

pytestmark = pytest.mark.anyio

LATER = timedelta(hours=1)


def test_stall_threshold_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("NEBULA_CALLBACK_STALL_SECONDS", raising=False)
    assert callback_stall_seconds() == 20 * 60
    monkeypatch.setenv("NEBULA_CALLBACK_STALL_SECONDS", "5")
    assert callback_stall_seconds() == 60
    monkeypatch.setenv("NEBULA_CALLBACK_STALL_SECONDS", "not-a-number")
    assert callback_stall_seconds() == 20 * 60
    monkeypatch.setenv("NEBULA_CALLBACK_STALL_SECONDS", str(10**9))
    assert callback_stall_seconds() == 24 * 60 * 60


async def _parked_parent(tmp_path: Path, parent_script):
    """A parent parked on wait_subagents while its child is held mid-request."""

    provider = RoutedProvider(
        parent=[
            _call("p1", "start_subagent", task="Scan.", name="Scanner", context=None),
            _call("p2", "wait_subagents", subagent_ids=None, mode=None),
            *parent_script,
        ],
        child=[_response(text="Scanned.")],
    )
    provider.child_gate = asyncio.Event()
    store, project, _, chat = await _setup(tmp_path, provider)
    prepared = await chat.prepare_async(
        _request(project, content="Delegate the scan.", allow_subagents=True)
    )
    parent_id = await chat.start_provider_turn(prepared)
    assert "callback_required" in await _drain(chat, parent_id)
    await _until(lambda: bool(provider.child_requests))
    (record,) = await store.list_entities(ChatSubagent)
    return provider, store, chat, parent_id, record


async def _strand_child(store: NebulaStore, chat: ChatService, record) -> None:
    """The child's turn ended but its record still says running.

    This is the desync a stop of a parked child leaves: nothing settles the
    record, so the parent's wait is never satisfied.
    """

    child = await store.get(ChatTurn, record.child_turn_id)
    await store.update(
        ChatTurn,
        child.id,
        {
            "status": ChatTurnStatus.CANCELLED,
            "execution_claim_id": None,
            "execution_owner_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=child.revision,
    )
    active = chat.has_active_provider_turn
    chat.has_active_provider_turn = lambda turn_id: (  # type: ignore[method-assign]
        False if turn_id == child.id else active(turn_id)
    )


async def test_stalled_subagent_wait_is_interrupted_and_resumed_once(
    tmp_path: Path,
) -> None:
    provider, store, chat, parent_id, record = await _parked_parent(
        tmp_path, [_finish("p3"), _response(text="The scan stalled; reporting.")]
    )
    await _strand_child(store, chat, record)
    assert await chat.reconcile_stalled_callback_waits() == []

    acted = await chat.reconcile_stalled_callback_waits(utc_now() + LATER)
    assert acted == [parent_id]

    async def completed() -> bool:
        return (await store.get(ChatTurn, parent_id)).status == ChatTurnStatus.COMPLETE

    await _until(completed)
    parent = await store.get(ChatTurn, parent_id)
    assert parent.request_snapshot[TURN_CALLBACK_STALL]["wait_kind"] == "subagent"
    assert parent.request_snapshot[TURN_CALLBACK_STALL]["repeated"] is False
    wait_entry = next(
        item for item in await _history(store, parent) if item["step"] == 1
    )
    # The wait ends with the child's real state: still running, no report.
    assert wait_entry["status"] == "complete"
    result = json.loads(wait_entry["provider_result"])
    assert result["still_running"] == [record.id]
    assert "made no progress for 20 minutes" in result["wait_ended_by_core"]
    assert "wait ended after no progress" in wait_entry["result_summary"]
    # It resumed exactly once, as a continuation of the same turn.
    assert len(provider.parent_requests) == 4
    await chat.shutdown()


async def test_subagent_wait_with_working_children_is_untouched(
    tmp_path: Path,
) -> None:
    provider, store, chat, parent_id, record = await _parked_parent(
        tmp_path, [_finish("p3"), _response(text="Done.")]
    )
    # The child is still mid-request: a long legitimate wait.
    assert chat.has_active_provider_turn(record.child_turn_id)
    before = await store.get(ChatTurn, parent_id)
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == []
    after = await store.get(ChatTurn, parent_id)
    assert after.status == ChatTurnStatus.WAITING_CALLBACK
    assert after.revision == before.revision
    # A child parked on its own callback is bounded by its own watchdog.
    child = await store.get(ChatTurn, record.child_turn_id)
    assert chat._turn_working(child.model_copy(update={"status": "waiting_callback"}))
    provider.child_gate.set()

    async def completed() -> bool:
        return (await store.get(ChatTurn, parent_id)).status == ChatTurnStatus.COMPLETE

    await _until(completed)
    assert (
        TURN_CALLBACK_STALL
        not in (await store.get(ChatTurn, parent_id)).request_snapshot
    )
    await chat.shutdown()


async def test_second_stall_of_the_same_turn_waits_for_the_operator(
    tmp_path: Path,
) -> None:
    provider, store, chat, parent_id, record = await _parked_parent(
        tmp_path,
        [
            # After the first recovery the model waits on the child again.
            _call("p3", "wait_subagents", subagent_ids=None, mode=None),
        ],
    )
    await _strand_child(store, chat, record)
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == [parent_id]

    async def parked_again() -> bool:
        turn = await store.get(ChatTurn, parent_id)
        return (
            turn.status == ChatTurnStatus.WAITING_CALLBACK
            and not chat.has_active_provider_turn(parent_id)
            and len(provider.parent_requests) == 3
        )

    await _until(parked_again)
    assert await chat.reconcile_stalled_callback_waits(utc_now() + 2 * LATER) == [
        parent_id
    ]
    parent = await store.get(ChatTurn, parent_id)
    assert parent.status == ChatTurnStatus.INTERRUPTED
    assert parent.request_snapshot["recovery"]["cause"] == (
        CALLBACK_STALL_REPEATED_CAUSE
    )
    assert parent.request_snapshot[TURN_CALLBACK_STALL]["repeated"] is True
    assert "will not resume by itself" in (parent.error or "")
    # Not resumed by the recovery tick either.
    await chat.resume_turns_stopped_by_core()
    assert (await store.get(ChatTurn, parent_id)).status == ChatTurnStatus.INTERRUPTED
    assert len(provider.parent_requests) == 3
    await chat.shutdown()


async def _synthetic(tmp_path: Path):
    store = NebulaStore(tmp_path / "stall.db")
    await store.create(Engagement(id="project", name="Project"))
    await store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    await store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    chat = ChatService(store)
    resumed: list[list[str]] = []

    async def record_resume(candidates=None):
        resumed.append([turn.id for turn in candidates or []])
        return []

    chat.resume_turns_stopped_by_core = record_resume  # type: ignore[method-assign]
    return store, chat, resumed


def _record_wakes(chat: ChatService) -> list[str]:
    woken: list[str] = []

    async def wake(target):
        woken.append(target.id)

    chat.subagents._resume_waiting_turn = wake  # type: ignore[method-assign]
    return woken


async def _parked_turn(store, entry: dict, *, goal_id: str | None = None) -> ChatTurn:
    turn = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            goal_id=goal_id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.WAITING_CALLBACK,
        )
    )
    sequence = await ChatTurnLedger(store.database).append(
        turn.id,
        {"step": 1, "tool_call_id": "wait-call", "status": "waiting_callback", **entry},
    )
    return await _at_sequence(store, turn, sequence)


async def _at_sequence(store, turn: ChatTurn, sequence: int) -> ChatTurn:
    """The turn as a saved step leaves it: bound to its ledger and aged."""

    return await store.update(
        ChatTurn,
        turn.id,
        {"ledger_sequence": sequence, "next_step": 2},
        expected_revision=turn.revision,
    )


async def _question(store) -> ChatSubagentMessage:
    child = await store.create(
        ChatSubagent(
            engagement_id="project",
            parent_session_id="session",
            parent_turn_id="parent-turn",
            child_session_id="child-session",
            name="Child",
            task="Ask the parent",
            status=ChatSubagentStatus.RUNNING,
        )
    )
    return await store.create(
        ChatSubagentMessage(
            engagement_id="project",
            subagent_id=child.id,
            parent_session_id="session",
            direction=ChatSubagentMessageDirection.TO_PARENT,
            content="Which host first?",
            expects_reply=True,
            awaiting_reply=True,
        )
    )


async def test_stalled_reply_wait_is_ended_and_woken_once(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    woken = _record_wakes(chat)
    question = await _question(store)
    turn = await _parked_turn(
        store,
        {
            "name": "message_parent",
            "subagent_wait": {"reply_to": question.id},
            "result_summary": "Waiting for the delegating assistant to reply.",
        },
    )
    assert await chat.reconcile_stalled_callback_waits() == []
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == [turn.id]
    marked = await store.get(ChatTurn, turn.id)
    # A wait has no effect of its own: it is ended, not interrupted.
    assert marked.status == ChatTurnStatus.WAITING_CALLBACK
    stall = marked.request_snapshot[TURN_CALLBACK_STALL]
    assert stall["wait_kind"] == "reply" and stall["repeated"] is False
    assert "reply wait" in stall["note"]
    entry = (await chat._turn_history(marked))[-1]
    assert chat._callback_stall_wake_due(marked, entry)
    assert woken == [turn.id]
    assert resumed == []


async def test_answered_reply_wait_that_was_never_woken_resumes(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    question = await _question(store)
    await store.update(
        ChatSubagentMessage,
        question.id,
        {"awaiting_reply": False},
        expected_revision=question.revision,
    )
    turn = await _parked_turn(
        store, {"name": "message_parent", "subagent_wait": {"reply_to": question.id}}
    )
    woken = _record_wakes(chat)
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == [turn.id]
    assert woken == [turn.id]
    # A satisfied wait is resumed normally, never interrupted.
    assert (await store.get(ChatTurn, turn.id)).status == (
        ChatTurnStatus.WAITING_CALLBACK
    )
    assert resumed == []


async def _goal(store, **metadata) -> ChatGoal:
    return await store.create(
        ChatGoal(
            engagement_id="project",
            session_id="session",
            objective="Bounded work",
            completion_criteria=["Evidenced"],
            status=ChatGoalStatus.RUNNING,
            metadata=metadata,
        )
    )


async def test_second_stall_on_a_goal_pauses_it(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    question = await _question(store)
    goal = await _goal(
        store,
        **{GOAL_CALLBACK_STALL: {"turn_id": "earlier", "stalled_at": "then"}},
    )
    turn = await _parked_turn(
        store,
        {"name": "message_parent", "subagent_wait": {"reply_to": question.id}},
        goal_id=goal.id,
    )
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == [turn.id]
    interrupted = await store.get(ChatTurn, turn.id)
    assert interrupted.request_snapshot["recovery"]["cause"] == (
        CALLBACK_STALL_REPEATED_CAUSE
    )
    paused = await store.get(ChatGoal, goal.id)
    assert paused.status == ChatGoalStatus.PAUSED
    assert "stalled again" in (paused.blocked_reason or "")
    assert resumed == []


async def test_first_stall_on_a_goal_marks_it_and_completion_clears(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    woken = _record_wakes(chat)
    question = await _question(store)
    goal = await _goal(store)
    turn = await _parked_turn(
        store,
        {"name": "message_parent", "subagent_wait": {"reply_to": question.id}},
        goal_id=goal.id,
    )
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == [turn.id]
    marked = await store.get(ChatGoal, goal.id)
    assert marked.metadata[GOAL_CALLBACK_STALL]["turn_id"] == turn.id
    assert marked.status == ChatGoalStatus.RUNNING
    assert woken == [turn.id]
    latest = await store.get(ChatTurn, turn.id)
    await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE},
        expected_revision=latest.revision,
    )
    await chat._clear_goal_provider_retry(turn.id)
    assert GOAL_CALLBACK_STALL not in (await store.get(ChatGoal, goal.id)).metadata


async def test_held_project_is_skipped(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    question = await _question(store)
    woken = _record_wakes(chat)
    turn = await _parked_turn(
        store, {"name": "message_parent", "subagent_wait": {"reply_to": question.id}}
    )
    await store.create(
        ProjectHold(engagement_id="project", reason="Synthetic deployment hold")
    )
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == []
    assert (await store.get(ChatTurn, turn.id)).status == (
        ChatTurnStatus.WAITING_CALLBACK
    )
    assert resumed == [] and woken == []


async def _process_wait(store, *, timeout_ms: int) -> tuple[ChatTurn, str]:
    from nebula.v3.automation_runtime import AutomationRuntimeManager

    turn = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.WAITING_CALLBACK,
        )
    )
    call = await store.create(
        DurableToolCall(
            engagement_id="project",
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_turn_id=turn.id,
            tool_name="automation.bash",
            arguments={"command": "sleep 600", "background": True},
            status=ToolCallStatus.RUNNING,
            risk_class="persistent_change",
        )
    )
    await store.create(
        CommandExecution(
            id=AutomationRuntimeManager._execution_id("process-1"),
            engagement_id="project",
            session_id="automation-session",
            process_id="process-1",
            command="sleep 600",
            command_sha256="0" * 64,
            background=True,
            runtime_digest="sha256:" + "0" * 64,
            policy_revision=1,
            metadata={"chat_turn_id": turn.id, "timeout_ms": timeout_ms},
        )
    )
    sequence = await ChatTurnLedger(store.database).append(
        turn.id,
        {
            "step": 1,
            "tool_call_id": call.id,
            "name": "automation.bash",
            "status": "waiting_callback",
            "process_id": "process-1",
        },
    )
    return await _at_sequence(store, turn, sequence), call.id


async def test_process_wait_is_stalled_only_past_its_deadline(tmp_path: Path):
    store, chat, resumed = await _synthetic(tmp_path)
    turn, call_id = await _process_wait(store, timeout_ms=4 * 60 * 60 * 1_000)
    # A long command still within its own deadline is a legitimate wait.
    assert await chat.reconcile_stalled_callback_waits(utc_now() + LATER) == []
    assert await chat.reconcile_stalled_callback_waits(
        utc_now() + timedelta(hours=5)
    ) == [turn.id]
    interrupted = await store.get(ChatTurn, turn.id)
    recovery = interrupted.request_snapshot["recovery"]
    assert recovery["cause"] == CALLBACK_STALL_CAUSE
    # The command's effect is recorded as unknown, never as not having run.
    assert recovery["unknown_tool_call_ids"] == [call_id]
    assert "background command wait" in (interrupted.error or "")
    assert resumed == [[turn.id]]


async def test_provider_turn_reports_its_last_progress(tmp_path: Path):
    store, chat, _ = await _synthetic(tmp_path)
    question = await _question(store)
    turn = await _parked_turn(
        store, {"name": "message_parent", "subagent_wait": {"reply_to": question.id}}
    )
    state = await session_state(store, await store.get(ChatSession, "session"))
    assert state["turn_id"] == turn.id
    assert state["last_progress_at"] == turn.updated_at.isoformat()
