"""A dead process can be reconciled without replaying its interrupted effects."""

from uuid import uuid4

import pytest
from sqlalchemy import select, update

from nebula.v3.chat_subagents import recoverable_after_core_restart
from nebula.v3.database import (
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    ProviderTurnQueueRow,
    RuntimeCapacityReservationRow,
    RuntimeInstanceRow,
    RuntimeResourceRow,
)
from nebula.v3.domain import (
    ChatGoal,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.process_identity import process_origin
from nebula.v3.runtime_chat_stream import RuntimeChatJournal, SEGMENT_END
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_orphan_recovery import reconcile_dead_project
from tests.v3.test_chat import _profile
from tests.v3.test_deployment_lifecycle import (
    host as host,
    stop_worker,
    worker,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def orphan(host):
    coordinator, store = host
    runtime = await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", runtime)
    profile = await store.create(_profile(local=True))
    session = await store.create(
        ChatSession(
            engagement_id="one",
            title="Synthetic runtime recovery",
            provider_profile_id=profile.id,
            model="synthetic",
        )
    )
    claim = str(uuid4())
    goal = await store.create(
        ChatGoal(
            engagement_id="one",
            session_id=session.id,
            objective="Synthetic recovery",
            completion_criteria=["No repeated effect"],
            status="running",
            execution_claim_id=claim,
            execution_owner_id="synthetic-chat-owner",
            execution_claimed_at=utc_now(),
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id="one",
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="synthetic",
            status="routing",
            execution_claim_id=claim,
            execution_owner_id="synthetic-chat-owner",
            execution_claimed_at=utc_now(),
        )
    )
    journal = RuntimeChatJournal(coordinator.coordination, store)
    with execution_context(lease):
        await journal.begin(turn.id, "synthetic-segment")
    async with coordinator.database.session() as sql:
        sql.add(
            ProviderTurnQueueRow(
                turn_id=turn.id,
                lane="interactive",
                state="running",
                lease_owner="synthetic-admission",
                lease_expires_at=utc_now(),
            )
        )
        sql.add(
            RuntimeCapacityReservationRow(
                id="provider-capacity",
                runtime_id=runtime,
                project_id="one",
                fencing_token=lease.fencing_token,
                request_key="provider:synthetic-admission",
                demands={"provider": 1},
                state="held",
                created_at_ms=0,
            )
        )

    async def expire(*, stop=True):
        if stop:
            stop_worker(host, runtime)
        async with coordinator.database.session() as sql:
            await sql.execute(
                update(ProjectOwnershipRow)
                .where(ProjectOwnershipRow.project_id == "one")
                .values(lease_expires_at_ms=0)
            )
            await sql.execute(
                update(RuntimeInstanceRow)
                .where(RuntimeInstanceRow.id == runtime)
                .values(lease_expires_at_ms=0)
            )

    async def reconcile(**kwargs):
        return await reconcile_dead_project(
            coordinator, lease, launch_origin=kwargs.get("origin", process_origin())
        )

    from types import SimpleNamespace

    yield SimpleNamespace(
        coordinator=coordinator,
        store=store,
        lease=lease,
        turn=turn,
        goal=goal,
        session=session,
        journal=journal,
        expire=expire,
        reconcile=reconcile,
    )
    await store.event_history.close()


async def test_dead_owner_reconciles_claims_admission_and_stream_once(orphan):
    value = orphan
    await value.expire()
    result = await value.reconcile()
    assert result["interrupted_turn_ids"] == [value.turn.id]
    assert result["effects_replayed"] is False
    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert turn.status == ChatTurnStatus.INTERRUPTED
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "paused"
    assert recoverable_after_core_restart(turn)
    frames = await value.coordinator.coordination.replay_stream(turn.id)
    assert [frame.event_type for frame in frames] == ["error", SEGMENT_END]
    assert await value.reconcile() == result
    assert await value.coordinator.coordination.replay_stream(turn.id) == frames
    async with value.coordinator.database.session() as sql:
        admission = await sql.get(ProviderTurnQueueRow, turn.id)
        assert admission.state == "parked" and admission.lease_owner is None
        capacity = await sql.get(RuntimeCapacityReservationRow, "provider-capacity")
        assert capacity.state == "released"
        assert result["released_capacity_ids"] == ["provider-capacity"]
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )


async def test_unknown_tool_effect_remains_recorded_and_requires_review(orphan):
    value = orphan
    call = await value.store.create(
        ToolCall(
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            tool_name="workspace.write",
            arguments={"path": "synthetic.txt"},
            risk_class=RiskClass.WORKSPACE_WRITE,
            status=ToolCallStatus.RUNNING,
            started_at=utc_now(),
        )
    )
    await value.expire()
    await value.reconcile()
    turn = await value.store.get(ChatTurn, value.turn.id)
    assert turn.request_snapshot["recovery"]["unknown_tool_call_ids"] == [call.id]
    assert turn.request_snapshot["recovery"]["cause"] == "runtime_loss"
    assert not recoverable_after_core_restart(turn)
    saved = await value.store.get(ToolCall, call.id)
    assert saved.status == ToolCallStatus.RUNNING and saved.revision == call.revision


@pytest.mark.parametrize("state", ["paused", "blocked"])
async def test_reconciliation_preserves_preexisting_goal_holds(orphan, state):
    value = orphan
    await value.store.update(
        ChatGoal,
        value.goal.id,
        {"status": state, "blocked_reason": "Existing operator hold"},
        expected_revision=value.goal.revision,
    )
    await value.expire()
    await value.reconcile()
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert goal.status.value == state
    assert goal.blocked_reason == "Existing operator hold"
    assert goal.execution_claim_id is None
    turn = await value.store.get(ChatTurn, value.turn.id)
    assert turn.request_snapshot["recovery"]["cause"] == "runtime_loss"
    assert not recoverable_after_core_restart(turn)


@pytest.mark.parametrize("proof", ["live", "unexpired", "namespace"])
async def test_missing_death_or_expiry_proof_never_mutates_work(orphan, proof):
    value = orphan
    if proof == "live":
        await value.expire(stop=False)
    elif proof == "namespace":
        await value.expire()
    with pytest.raises(CoordinationConflict):
        await value.reconcile(
            origin="different-namespace" if proof == "namespace" else process_origin()
        )
    assert (
        await value.store.get(ChatTurn, value.turn.id)
    ).status == ChatTurnStatus.ROUTING
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, value.turn.id)).state == "running"


async def test_invalid_stream_identity_rolls_back_interruption_and_receipt(orphan):
    value = orphan
    await value.expire()
    async with value.coordinator.database.session() as sql:
        row = await sql.scalar(select(RuntimeResourceRow))
        row.checkpoint = {"turn_id": value.turn.id, "segment": "wrong"}
    with pytest.raises(CoordinationConflict, match="invalid durable identity"):
        await value.reconcile()
    assert (
        await value.store.get(ChatTurn, value.turn.id)
    ).status == ChatTurnStatus.ROUTING
    assert (
        await value.store.get(ChatGoal, value.goal.id)
    ).execution_claim_id is not None
    async with value.coordinator.database.session() as sql:
        assert not await sql.scalar(
            select(DeploymentReceiptRow.id).where(
                DeploymentReceiptRow.action == "project.orphans_reconciled"
            )
        )


@pytest.mark.parametrize("kind", ["tool", "wrong-admission", "wrong-fence"])
async def test_unrelated_capacity_is_not_released_by_process_death(orphan, kind):
    value = orphan
    async with value.coordinator.database.session() as sql:
        sql.add(
            RuntimeCapacityReservationRow(
                id="unconfirmed-capacity",
                runtime_id=value.lease.runtime_id,
                project_id="one",
                fencing_token=value.lease.fencing_token
                + (1 if kind == "wrong-fence" else 0),
                request_key="tool:synthetic-admission"
                if kind == "tool"
                else "provider:other"
                if kind == "wrong-admission"
                else "provider:synthetic-admission",
                demands={"tool": 1} if kind == "tool" else {"provider": 1},
                state="held",
                created_at_ms=0,
            )
        )
    await value.expire()
    await value.reconcile()
    async with value.coordinator.database.session() as sql:
        assert (
            await sql.get(RuntimeCapacityReservationRow, "unconfirmed-capacity")
        ).state == "held"
