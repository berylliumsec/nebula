"""Operator pause and stop run while project recovery is held for unresolved effects.

Built on the stepped recovery fixture: real SQLite/PostgreSQL coordination, a
real ProjectExecution behind signed RPC, the real durable delivery vault and
the real recovery-control runner with its own fenced execution pool.
"""

import asyncio
import json
import time

import pytest
from sqlalchemy import update

from nebula.v3.database import (
    ProviderTurnQueueRow,
    RuntimeCommandRow,
    RuntimeInstanceRow,
)
from nebula.v3.domain import ChatGoal, ChatSession, ChatTurn, utc_now
from nebula.v3.recovery_markers import OPERATOR_RECOVERY_PAUSE, OPERATOR_RECOVERY_STOP
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_routes import command_lane, forwarding_lane, recovery_control
from nebula.v3.runtime_vault import RuntimeVault
from tests.v3.test_recovery_reconciliation import recovery as recovery, until
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_work_batches import batch as batch

pytestmark = pytest.mark.anyio

GOAL = "/api/v1/chat/sessions/{}/goal/actions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def capture(path, body=None):
    return HttpIntent.capture(
        "POST",
        path,
        b"",
        [(b"content-type", b"application/json")],
        json.dumps(body).encode() if body is not None else b"",
    )


async def submit(value, key, path, body=None):
    intent = capture(path, body)
    delivery = RuntimeDelivery(
        value.coordination, RuntimeVault(value.host.root / "payloads")
    )
    return await delivery.accept(
        intent,
        project_id="one",
        actor_id="operator",
        idempotency_key=key,
        lane=command_lane(intent.path, method=intent.method, body=intent.body()),
    )


async def command(value, receipt):
    async with value.database.session() as session:
        return await session.get(RuntimeCommandRow, receipt.command_id)


async def settled(value, *receipts):
    async def done():
        rows = [await command(value, receipt) for receipt in receipts]
        return all(row.state in {"succeeded", "failed"} for row in rows)

    try:
        async with asyncio.timeout(10):
            while not await done():
                await asyncio.sleep(0.05)
    except TimeoutError:
        rows = [await command(value, receipt) for receipt in receipts]
        raise AssertionError(
            [(row.state, row.result) for row in rows]
            + [value.execution.state, value.execution.failure]
        )


async def held_project(value):
    """A running goal whose claim names the dead owner's unresolved step."""
    # The recovery runner's execution pool checks leases against SQL's own
    # clock, so this fixture's coordination clock follows real time.
    value.clock[0] = int(time.time() * 1000)
    async with value.database.session() as session:
        await session.execute(
            update(RuntimeInstanceRow)
            .where(RuntimeInstanceRow.id == "target")
            .values(lease_expires_at_ms=value.clock[0] + 600_000)
        )
    for session_id in ("held-session", "queued-session", "synthetic-session"):
        await value.store.create(
            ChatSession(
                id=session_id,
                engagement_id="one",
                title="Synthetic " + session_id,
                provider_profile_id="synthetic-provider",
                model="inert",
            )
        )
    goal = await value.store.create(
        ChatGoal(
            engagement_id="one",
            session_id="held-session",
            objective="Synthetic long-running objective",
            completion_criteria=["Inert"],
            status="running",
            started_at=utc_now(),
            active_since=utc_now(),
            execution_owner_id="source",
            execution_claim_id="claim-from-dead-owner",
            execution_claimed_at=utc_now(),
        )
    )
    queued = await value.store.create(
        ChatTurn(
            engagement_id="one",
            session_id="queued-session",
            provider_profile_id="synthetic-provider",
            model="inert",
            status="queued",
            queued_at=utc_now(),
        )
    )
    async with value.database.session() as session:
        session.add(
            ProviderTurnQueueRow(
                turn_id=queued.id,
                lane="direct",
                state="queued",
            )
        )
    claimed = await value.store.create(
        ChatTurn(
            engagement_id="one",
            session_id="held-session",
            goal_id=goal.id,
            provider_profile_id="synthetic-provider",
            model="inert",
            status="routing",
            execution_owner_id="source",
            execution_claim_id="claim-from-dead-owner",
            execution_claimed_at=utc_now(),
        )
    )
    return goal, queued, claimed


def test_goal_pause_is_control_and_start_resume_are_work():
    path = GOAL.format("s1")
    for action, lane in [
        ("pause", "control"),
        ("start", "work"),
        ("resume", "work"),
        ("cancel", "work"),
        ("complete", "work"),
    ]:
        body = json.dumps({"expected_revision": 1, "action": action}).encode()
        assert command_lane(path, method="POST", body=body) == lane
        assert recovery_control("POST", path, body) is (action == "pause")
    # A malformed or absent body is ordinary work, never a control.
    assert command_lane(path, method="POST", body=b"{") == "work"
    assert command_lane(path) == "work"
    # Every request admission may classify as control is forwarded on control
    # capacity; forwarding never sees the body and authorizes nothing.
    for candidate in [
        path,
        "/api/v1/chat/turns/t/cancel",
        "/api/v1/runs/r/stop",
        "/api/v1/chat/sessions/s/subagents/stop",
    ]:
        assert forwarding_lane({"method": "POST", "path": candidate}) == "control"
    assert forwarding_lane({"method": "POST", "path": "/api/v1/chat/completions"}) == (
        "work"
    )
    assert forwarding_lane({"method": "GET", "path": path}) == "read"
    assert recovery_control("POST", "/api/v1/chat/turns/t/cancel", b"") is True
    assert recovery_control("POST", "/api/v1/runs/r/stop", b"") is False
    assert recovery_control("PUT", "/api/v1/chat/turns/t/cancel", b"") is False


async def test_pause_and_stop_run_while_recovery_is_blocked(recovery):  # noqa: F811 (fixture)
    value = recovery
    goal, queued, claimed = await held_project(value)
    lease = await value.begin()
    pause = await submit(
        value,
        "pause",
        GOAL.format("held-session"),
        {"expected_revision": goal.revision, "action": "pause"},
    )
    stop = await submit(value, "stop", f"/api/v1/chat/turns/{queued.id}/cancel")
    stop_claimed = await submit(
        value, "stop-claimed", f"/api/v1/chat/turns/{claimed.id}/cancel"
    )
    resume = await submit(
        value,
        "resume",
        GOAL.format("synthetic-session"),
        {"expected_revision": value.goal.revision, "action": "resume"},
    )
    subagents = await submit(
        value, "subagents", "/api/v1/chat/sessions/held-session/subagents/stop"
    )
    assert (await command(value, pause)).lane == "control"
    assert (await command(value, resume)).lane == "work"

    await value.new_supervisor().advance_project_handoffs()
    assert value.execution.state == "recovering"
    await settled(value, pause, stop, stop_claimed)

    # The pause applied and kept the dead owner's claim for reconciliation.
    paused = await value.store.get(ChatGoal, goal.id)
    assert paused.status == "paused"
    assert paused.execution_claim_id == "claim-from-dead-owner"
    assert "unresolved" in paused.blocked_reason
    assert OPERATOR_RECOVERY_PAUSE in paused.metadata
    assert (await command(value, pause)).state == "succeeded"
    # A queued response that never reached an owner stops cleanly.
    assert (await value.store.get(ChatTurn, queued.id)).status == "cancelled"
    async with value.database.session() as session:
        admission = await session.get(ProviderTurnQueueRow, queued.id)
        assert admission.state == "cancelled"
    assert (await command(value, stop)).state == "succeeded"
    # A response the dead owner started is not declared stopped: its effect
    # is unknown, so status and claim stay. The stop itself is saved so
    # restart recovery will not continue it.
    unresolved = await value.store.get(ChatTurn, claimed.id)
    assert unresolved.status == "routing"
    assert unresolved.execution_claim_id == "claim-from-dead-owner"
    assert unresolved.request_snapshot[OPERATOR_RECOVERY_STOP]
    saved = await command(value, stop_claimed)
    assert saved.state == "succeeded" and saved.result == {"http_status": 202}

    # Ordinary work and controls that need the application stay held.
    assert (await command(value, resume)).state == "queued"
    assert (await command(value, subagents)).state == "queued"
    assert (await value.store.get(ChatGoal, value.goal.id)).status == "paused"
    assert value.constructions == [] and not value.activations
    blockers = (await value.coordinator.recovery_status("one"))["blockers"]
    assert any(goal.id in blocker for blocker in blockers)
    assert any(claimed.id in blocker for blocker in blockers)
    assert value.execution.recovery_blockers

    # A repeated request returns its saved receipt; nothing runs twice.
    again = await submit(
        value,
        "pause",
        GOAL.format("held-session"),
        {"expected_revision": goal.revision, "action": "pause"},
    )
    assert again.command_id == pause.command_id
    revision = (await value.store.get(ChatGoal, goal.id)).revision
    await asyncio.sleep(0.6)
    assert (await value.store.get(ChatGoal, goal.id)).revision == revision

    # Shutdown reports the retained claims rather than calling them paused.
    async with value.database.session() as session:
        shutdown = await value.coordinator._shutdown_blockers(session, "one")
    assert any(goal.id in item and "execution claim" in item for item in shutdown)
    assert lease == value.execution.lease


async def test_stale_owner_cannot_claim_a_control(recovery):  # noqa: F811 (fixture)
    value = recovery
    goal, _, _ = await held_project(value)
    lease = await value.begin()
    pause = await submit(
        value,
        "pause",
        GOAL.format("held-session"),
        {"expected_revision": goal.revision, "action": "pause"},
    )
    with pytest.raises(LeaseLost):
        await value.coordination.claim_control(value.old, pause.command_id)
    row = await command(value, pause)
    assert row.state == "queued" and row.runtime_id is None
    # The current owner claims it exactly once.
    first = await value.coordination.claim_control(lease, pause.command_id)
    assert first is not None
    assert await value.coordination.claim_control(lease, pause.command_id) is None
    # A claim interrupted before its outcome is never replayed: it stays
    # running under its owner and blocks recovery as an explicit outcome.
    assert (await command(value, pause)).state == "running"
    blockers = (await value.coordinator.recovery_status("one"))["blockers"]
    assert any(pause.command_id in blocker for blocker in blockers)
    assert (await value.store.get(ChatGoal, goal.id)).status == "running"


async def test_held_worker_activates_only_after_blockers_resolve(recovery):  # noqa: F811 (fixture)
    value = recovery
    goal, queued, claimed = await held_project(value)
    await value.begin()
    pause = await submit(
        value,
        "pause",
        GOAL.format("held-session"),
        {"expected_revision": goal.revision, "action": "pause"},
    )
    supervisor = value.new_supervisor()
    await supervisor.advance_project_handoffs()
    await settled(value, pause)
    await supervisor.advance_project_handoffs()
    assert value.execution.state == "recovering" and not value.activations

    # Synthetic stand-in for the operator's reconciliation of the dead
    # owner's step (outside this change): the claims are reviewed and closed.
    current = await value.store.get(ChatGoal, goal.id)
    await value.store.update(
        ChatGoal,
        goal.id,
        {
            "execution_owner_id": None,
            "execution_claim_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=current.revision,
    )
    turn = await value.store.get(ChatTurn, claimed.id)
    await value.store.update(
        ChatTurn,
        claimed.id,
        {
            "status": "failed",
            "execution_owner_id": None,
            "execution_claim_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=turn.revision,
    )
    await until(lambda: value.execution.state == "prepared")
    await supervisor.advance_project_handoffs()
    assert value.execution.state == "ready" and len(value.activations) == 1
    # Activation did not resume the goal the operator paused.
    assert (await value.store.get(ChatGoal, goal.id)).status == "paused"
    # Shutdown now names only work that is genuinely still open: the queued
    # response nobody stopped, never the reconciled claims.
    async with value.database.session() as session:
        remaining = await value.coordinator._shutdown_blockers(session, "one")
    assert remaining and all(queued.id in item for item in remaining)


async def test_restart_recovery_keeps_operator_recovery_pause_and_stop(tmp_path):
    from nebula.v3.chat import ChatService
    from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
    from nebula.v3.domain import ChatTurnStatus
    from nebula.v3.recovery_markers import OPERATOR_RECOVERY_PAUSE
    from nebula.v3.storage import ConflictError
    from tests.v3.test_chat_stream_follow import _conversation, _interrupted_turn

    store = await _conversation(tmp_path)
    goals = ChatGoalService(store)
    goal = await goals.create(
        "chat", GoalCreate(objective="Continue", completion_criteria=["Done"])
    )
    goal = await goals.write(
        "chat", GoalWrite(expected_revision=goal.revision, action="start")
    )
    # Restart recovery paused it first; the operator's recovery pause then
    # marks it, without changing anything else.
    goal = await goals.write(
        "chat", GoalWrite(expected_revision=goal.revision, action="pause")
    )
    marked = await goals.write(
        "chat",
        GoalWrite(expected_revision=goal.revision, action="pause"),
        retain_execution_claim=True,
    )
    assert marked.status == "paused" and OPERATOR_RECOVERY_PAUSE in marked.metadata
    await _interrupted_turn(
        store, {"required": False, "cause": "core_restart"}, goal.id
    )

    service = ChatService(store)
    assert await service.resume_turns_stopped_by_core() == []
    assert (await store.get(ChatTurn, "turn")).status == ChatTurnStatus.INTERRUPTED
    assert (await store.get(ChatGoal, goal.id)).status == "paused"
    with pytest.raises(ConflictError, match="operator paused"):
        await goals.write_latest("chat", "resume", allow_pending_recovery=True)
    # Only the operator's own resume continues it, and it clears the mark.
    current = await store.get(ChatGoal, goal.id)
    resumed = await goals.write(
        "chat", GoalWrite(expected_revision=current.revision, action="resume")
    )
    assert resumed.status == "running"
    assert OPERATOR_RECOVERY_PAUSE not in resumed.metadata
    await store.event_history.close()
    await store.database.dispose()


async def test_restart_recovery_keeps_an_operator_recovery_stop(tmp_path):
    from nebula.v3.chat import ChatService
    from nebula.v3.domain import ChatTurnStatus
    from nebula.v3.recovery_markers import OPERATOR_RECOVERY_STOP
    from tests.v3.test_chat_stream_follow import _conversation, _interrupted_turn

    store = await _conversation(tmp_path)
    turn = await _interrupted_turn(store, {"required": False, "cause": "core_restart"})
    await store.update(
        ChatTurn,
        turn.id,
        {"request_snapshot": {**turn.request_snapshot, OPERATOR_RECOVERY_STOP: "t"}},
        expected_revision=turn.revision,
    )
    assert await ChatService(store).resume_turns_stopped_by_core() == []
    assert (await store.get(ChatTurn, turn.id)).status == ChatTurnStatus.INTERRUPTED
    await store.event_history.close()
    await store.database.dispose()
