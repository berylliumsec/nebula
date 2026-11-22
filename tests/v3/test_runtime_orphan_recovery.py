"""A dead process can be reconciled without replaying its interrupted effects."""

import re
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from nebula.v3.chat_subagents import recoverable_after_core_restart
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import (
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    ProviderTurnQueueRow,
    RuntimeCapacityReservationRow,
    RuntimeChatBindingRow,
    RuntimeCommandRow,
    RuntimeInstanceRow,
    RuntimeResourceRow,
)
from nebula.v3.domain import (
    Approval,
    ApprovalContinuation,
    ApprovalStatus,
    ChatGoal,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    NativeHookExecution,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.process_identity import process_origin
from nebula.v3.runtime_chat_stream import RuntimeChatJournal, SEGMENT_END
from nebula.v3.runtime_coordination import (
    STORAGE_REPORT_COMMAND,
    CoordinationConflict,
)
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


@pytest.mark.parametrize(
    "status", [ChatTurnStatus.ROUTING, ChatTurnStatus.WAITING_CALLBACK]
)
async def test_dead_owner_reconciles_claims_admission_and_stream_once(orphan, status):
    value = orphan
    child_ids = []
    if status != ChatTurnStatus.ROUTING:
        await value.store.update(
            ChatTurn,
            value.turn.id,
            {"status": status},
            expected_revision=value.turn.revision,
        )
        for name, state in (
            ("Completed child", ChatSubagentStatus.COMPLETED),
            ("Failed child", ChatSubagentStatus.FAILED),
        ):
            child = await value.store.create(
                ChatSubagent(
                    engagement_id="one",
                    parent_session_id=value.session.id,
                    parent_turn_id=value.turn.id,
                    child_session_id=str(uuid4()),
                    name=name,
                    task="Synthetic delegated work",
                    status=state,
                    finished_at=utc_now(),
                    result="Synthetic result"
                    if state == ChatSubagentStatus.COMPLETED
                    else "",
                    error="Synthetic failure"
                    if state == ChatSubagentStatus.FAILED
                    else None,
                )
            )
            child_ids.append(child.id)
        await ChatTurnLedger(value.coordinator.database).append(
            value.turn.id,
            {
                "step": 1,
                "tool_call_id": "synthetic-subagent-wait",
                "name": "wait_subagents",
                "status": "waiting_callback",
                "subagent_wait": {"ids": child_ids, "mode": "all"},
                "result_summary": "Waiting for delegated work.",
            },
        )
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
    if child_ids:
        from nebula.v3.chat import ChatService

        chat = ChatService(value.store)
        pending = (await chat._turn_history(turn))[-1]
        wait = pending["subagent_wait"]
        assert wait["ids"] == child_ids
        assert await chat.subagents.wait_ready(wait)
        output, _summary = await chat.subagents.wait_result(wait)
        assert len(output["subagents"]) == 2
        assert (
            await value.store.get(ChatSubagent, child_ids[0])
        ).status == ChatSubagentStatus.COMPLETED
        assert (
            await value.store.get(ChatSubagent, child_ids[1])
        ).status == ChatSubagentStatus.FAILED
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


async def test_failed_provider_turn_releases_only_paired_claims_and_keeps_history(
    orphan,
):
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {
            "status": ChatTurnStatus.FAILED,
            "content": "Saved partial response",
            "error": "Provider stream ended",
        },
        expected_revision=value.turn.revision,
    )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["interrupted_turn_ids"] == []
    assert result["released_failed_provider_claim_ids"] == [value.turn.id]
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.content == "Saved partial response"
    assert turn.error == "Provider stream ended"
    assert turn.execution_claim_id is None and turn.execution_owner_id is None
    assert goal.status.value == "running"
    assert goal.execution_claim_id is None and goal.execution_owner_id is None
    frames = await value.coordinator.coordination.replay_stream(turn.id)
    assert [frame.event_type for frame in frames] == ["error", SEGMENT_END]
    assert frames[-1].event_type == SEGMENT_END
    async with value.coordinator.database.session() as sql:
        admission = await sql.get(ProviderTurnQueueRow, turn.id)
        assert admission.state == "complete" and admission.lease_owner is None
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"


@pytest.mark.parametrize(
    "unsafe_effect",
    [
        "running_tool",
        "failed_write",
        "running_hook",
        "running_child",
        "pending_approval",
    ],
)
async def test_failed_provider_claim_is_released_and_goal_held_while_effect_is_unconfirmed(
    orphan, unsafe_effect
):
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.FAILED},
        expected_revision=value.turn.revision,
    )
    if unsafe_effect in {"running_tool", "failed_write"}:
        await value.store.create(
            ToolCall(
                engagement_id="one",
                run_id=value.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=value.session.id,
                chat_turn_id=value.turn.id,
                tool_name=(
                    "workspace.write"
                    if unsafe_effect == "failed_write"
                    else "local.read"
                ),
                arguments={},
                risk_class=(
                    RiskClass.WORKSPACE_WRITE
                    if unsafe_effect == "failed_write"
                    else RiskClass.LOCAL_READ
                ),
                status=(
                    ToolCallStatus.FAILED
                    if unsafe_effect == "failed_write"
                    else ToolCallStatus.RUNNING
                ),
                started_at=utc_now(),
            )
        )
    elif unsafe_effect == "running_hook":
        await value.store.create(
            NativeHookExecution(
                engagement_id="one",
                chat_session_id=value.session.id,
                chat_turn_id=value.turn.id,
                hook_id="synthetic-hook",
                hook_snapshot={},
                event_name="turn.complete",
                status="running",
                side_effects="none",
                started_at=utc_now(),
            )
        )
    elif unsafe_effect == "running_child":
        await value.store.create(
            ChatSubagent(
                engagement_id="one",
                parent_session_id=value.session.id,
                parent_turn_id=value.turn.id,
                child_session_id="synthetic-child-session",
                name="Synthetic child",
                task="Finish a bounded task",
                status=ChatSubagentStatus.RUNNING,
            )
        )
    else:
        await value.store.create(
            Approval(
                engagement_id="one",
                run_id=value.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=value.session.id,
                chat_turn_id=value.turn.id,
                status=ApprovalStatus.PENDING,
                risk_class=RiskClass.WORKSPACE_WRITE,
                exact_request={"synthetic": True},
                policy_rationale="Synthetic pending approval",
                requested_by="test",
            )
        )
    await value.expire()

    await _assert_held_for_review(value, "unconfirmed|unfinished")


async def test_dead_owner_releases_unpaired_turn_and_goal_claims(orphan):
    """A claim held only by the dead owner can never be released by anyone else."""
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.FAILED},
        expected_revision=value.turn.revision,
    )
    await value.store.update(
        ChatGoal,
        value.goal.id,
        {"execution_claim_id": "different-claim"},
        expected_revision=value.goal.revision,
    )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert set(result["released_failed_provider_claim_ids"]) == {turn.id, goal.id}
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "running"
    async with value.coordinator.database.session() as sql:
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )


async def _assert_held_for_review(value, match):
    """The dead owner's claim is released; the effect record stays for review."""
    result = await value.reconcile()
    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["effects_replayed"] is False
    assert result["goal_review_turn_ids"] == [turn.id]
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "paused"
    assert re.search(match, goal.blocked_reason)
    assert "no effect has been retried" in goal.blocked_reason
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, turn.id)).state != "running"
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


@pytest.mark.parametrize(
    "status",
    [ChatTurnStatus.COMPLETE, ChatTurnStatus.CANCELLED, ChatTurnStatus.INTERRUPTED],
)
async def test_dead_owner_releases_ended_turn_claim_and_stream_without_replay(
    orphan, status
):
    """A worker that died after saving an ended turn, before releasing its claim.

    Before this, recovery refused forever with "The orphaned stream still
    requires execution reconciliation" and the project never served again.
    """
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": status, "content": "Saved answer"},
        expected_revision=value.turn.revision,
    )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["effects_replayed"] is False
    assert result["interrupted_turn_ids"] == []
    assert result["goal_review_turn_ids"] == []
    assert turn.id in result["released_failed_provider_claim_ids"]
    assert turn.status == status and turn.content == "Saved answer"
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "running"
    frames = await value.coordinator.coordination.replay_stream(turn.id)
    assert [frame.event_type for frame in frames] == ["error", SEGMENT_END]
    async with value.coordinator.database.session() as sql:
        admission = await sql.get(ProviderTurnQueueRow, turn.id)
        assert admission.state != "running" and admission.lease_owner is None
        assert (
            await sql.get(RuntimeCapacityReservationRow, "provider-capacity")
        ).state == "released"
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


@pytest.mark.parametrize("reference", ["missing", "wrong_turn", "wrong_project"])
async def test_failed_provider_claim_holds_goal_for_dangling_or_mismatched_approval(
    orphan, reference
):
    value = orphan
    approval_id = (
        "missing-approval" if reference == "missing" else "mismatched-approval"
    )
    if reference != "missing":
        await value.store.create(
            Approval(
                id=approval_id,
                engagement_id="two" if reference == "wrong_project" else "one",
                run_id=value.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=value.session.id,
                chat_turn_id=(
                    "different-turn" if reference == "wrong_turn" else value.turn.id
                ),
                status=ApprovalStatus.REJECTED,
                risk_class=RiskClass.WORKSPACE_WRITE,
                exact_request={"synthetic": True},
                policy_rationale="Synthetic settled approval",
                requested_by="test",
            )
        )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.FAILED, "approval_id": approval_id},
        expected_revision=value.turn.revision,
    )
    resource_id = value.journal.resource_id(value.turn.id, "synthetic-segment")
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(RuntimeResourceRow, resource_id)).state != "closed"
    await value.expire()

    await _assert_held_for_review(value, "references (a missing approval|an approval)")


@pytest.mark.parametrize("reference", ["missing", "wrong_project", "wrong_turn"])
async def test_failed_provider_claim_holds_goal_for_dangling_tool_call_approval(
    orphan, reference
):
    value = orphan
    approval_id = (
        "missing-tool-call-approval"
        if reference == "missing"
        else "mismatched-tool-call-approval"
    )
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
            status=ToolCallStatus.COMPLETE,
            approval_id=approval_id,
            started_at=utc_now(),
            completed_at=utc_now(),
        )
    )
    if reference != "missing":
        await value.store.create(
            Approval(
                id=approval_id,
                engagement_id="two" if reference == "wrong_project" else "one",
                run_id=value.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=value.session.id,
                chat_turn_id=(
                    "different-turn" if reference == "wrong_turn" else value.turn.id
                ),
                tool_call_id=call.id,
                status=ApprovalStatus.REJECTED,
                risk_class=RiskClass.WORKSPACE_WRITE,
                exact_request={"synthetic": True},
                policy_rationale="Synthetic settled approval",
                requested_by="test",
            )
        )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.FAILED},
        expected_revision=value.turn.revision,
    )
    resource_id = value.journal.resource_id(value.turn.id, "synthetic-segment")
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(RuntimeResourceRow, resource_id)).state != "closed"
    await value.expire()

    await _assert_held_for_review(
        value,
        "references (a missing approval|an approval for another project, turn, or call)",
    )


@pytest.mark.parametrize(
    "reference",
    [
        "approved_no_call",
        "edited_no_call",
        "missing_call",
        "wrong_project_call",
        "wrong_turn_call",
        "call_missing_backref",
        "failed_continuation",
        "failed_adapter",
        "unknown_adapter",
    ],
)
async def test_failed_provider_claim_holds_goal_for_unsettled_or_mismatched_approval_links(
    orphan, reference
):
    value = orphan
    approval_id = f"approval-{reference}"
    call = None
    if reference in {
        "wrong_project_call",
        "wrong_turn_call",
        "call_missing_backref",
    }:
        call = await value.store.create(
            ToolCall(
                engagement_id=("two" if reference == "wrong_project_call" else "one"),
                run_id=value.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=value.session.id,
                chat_turn_id=(
                    "different-turn"
                    if reference == "wrong_turn_call"
                    else value.turn.id
                ),
                tool_name="workspace.write",
                arguments={"path": "synthetic.txt"},
                risk_class=RiskClass.WORKSPACE_WRITE,
                status=ToolCallStatus.COMPLETE,
                started_at=utc_now(),
                completed_at=utc_now(),
            )
        )
    tool_call_id = (
        call.id
        if call is not None
        else "missing-tool-call"
        if reference == "missing_call"
        else None
    )
    approval_status = (
        ApprovalStatus.APPROVED
        if reference
        in {
            "approved_no_call",
            "missing_call",
            "wrong_project_call",
            "wrong_turn_call",
            "call_missing_backref",
        }
        else ApprovalStatus.EDITED
        if reference == "edited_no_call"
        else ApprovalStatus.REJECTED
    )
    continuation = None
    if reference == "failed_continuation":
        continuation = ApprovalContinuation(
            harness_turn_id=value.turn.id, status="failed"
        )
    elif reference == "failed_adapter":
        continuation = ApprovalContinuation(
            harness_turn_id=value.turn.id,
            status="delivered",
            adapter_handoff="transport_write",
            adapter_status="failed",
        )
    elif reference == "unknown_adapter":
        continuation = ApprovalContinuation(
            harness_turn_id=value.turn.id,
            status="delivered",
            adapter_handoff="transport_write",
            adapter_status="unknown",
        )
    await value.store.create(
        Approval(
            id=approval_id,
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            tool_call_id=tool_call_id,
            status=approval_status,
            risk_class=RiskClass.WORKSPACE_WRITE,
            exact_request={"synthetic": True},
            policy_rationale="Synthetic approval-link boundary",
            requested_by="test",
            continuation=continuation,
        )
    )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.FAILED, "approval_id": approval_id},
        expected_revision=value.turn.revision,
    )
    resource_id = value.journal.resource_id(value.turn.id, "synthetic-segment")
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(RuntimeResourceRow, resource_id)).state != "closed"
    await value.expire()

    await _assert_held_for_review(
        value,
        "references (a missing tool call|another project, turn, or tool call)|no exact settled tool call|unconfirmed approval effect",
    )


async def test_dead_owner_keeps_unclaimed_callback_wait(orphan):
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {
            "status": ChatTurnStatus.WAITING_CALLBACK,
            "execution_claim_id": None,
            "execution_owner_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=value.turn.revision,
    )
    await value.store.update(
        ChatGoal,
        value.goal.id,
        {
            "status": "paused",
            "execution_claim_id": None,
            "execution_owner_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=value.goal.revision,
    )
    await value.expire()
    result = await value.reconcile()
    assert result["interrupted_turn_ids"] == []
    turn = await value.store.get(ChatTurn, value.turn.id)
    assert turn.status == ChatTurnStatus.WAITING_CALLBACK
    async with value.coordinator.database.session() as sql:
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )


async def test_dead_owner_interrupts_unknown_command_callback_without_replay(orphan):
    value = orphan
    call = await value.store.create(
        ToolCall(
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            tool_name="run_command",
            arguments={"command": "synthetic"},
            risk_class=RiskClass.WORKSPACE_WRITE,
            status=ToolCallStatus.RUNNING,
            started_at=utc_now(),
        )
    )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.WAITING_CALLBACK},
        expected_revision=value.turn.revision,
    )
    await ChatTurnLedger(value.coordinator.database).append(
        value.turn.id,
        {
            "step": 1,
            "tool_call_id": call.id,
            "name": "run_command",
            "status": "waiting_callback",
            "process_id": "synthetic-process",
            "result_summary": "Waiting for command result.",
        },
    )
    await value.expire()
    result = await value.reconcile()
    turn = await value.store.get(ChatTurn, value.turn.id)
    assert result["interrupted_turn_ids"] == [turn.id]
    assert turn.status == ChatTurnStatus.INTERRUPTED
    assert turn.execution_claim_id is None
    assert turn.request_snapshot["recovery"]["unknown_tool_call_ids"] == [call.id]
    assert (await value.store.get(ChatGoal, value.goal.id)).status.value == "paused"
    assert (await value.store.get(ToolCall, call.id)).status == ToolCallStatus.RUNNING
    async with value.coordinator.database.session() as sql:
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"


@pytest.mark.parametrize("last_step", ["missing_call", "resumed_result", "no_history"])
async def test_dead_owner_interrupts_unmatched_callback_for_review(orphan, last_step):
    """A claimed callback turn nobody can finish no longer holds the project.

    Before, reconciliation refused forever with "An orphaned command callback
    still requires effect reconciliation".
    """
    value = orphan
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.WAITING_CALLBACK},
        expected_revision=value.turn.revision,
    )
    if last_step != "no_history":
        await ChatTurnLedger(value.coordinator.database).append(
            value.turn.id,
            {
                "step": 1,
                "tool_call_id": "missing-tool-call",
                "name": "run_command",
                # A resuming worker records the callback result before the
                # turn leaves WAITING_CALLBACK.
                "status": "waiting_callback"
                if last_step == "missing_call"
                else "completed",
                "process_id": "synthetic-process",
            },
        )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["interrupted_turn_ids"] == [turn.id]
    assert result["effects_replayed"] is False
    assert turn.status == ChatTurnStatus.INTERRUPTED
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "paused"
    recovery = turn.request_snapshot["recovery"]
    assert recovery["cause"] == "runtime_loss"
    assert "unmatched_callback" in recovery
    assert "no effect has been retried" in turn.error
    # Core never resumes it by itself; the operator reviews it.
    assert not recoverable_after_core_restart(turn)
    async with value.coordinator.database.session() as sql:
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


async def test_dead_owner_releases_claim_of_turn_parked_for_approval(orphan):
    """Saved WAITING_APPROVAL before the claim was released, then died."""
    value = orphan
    approval = await value.store.create(
        Approval(
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            status=ApprovalStatus.PENDING,
            risk_class=RiskClass.WORKSPACE_WRITE,
            exact_request={"synthetic": True},
            policy_rationale="Synthetic pending approval",
            requested_by="test",
        )
    )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.WAITING_APPROVAL, "approval_id": approval.id},
        expected_revision=value.turn.revision,
    )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["effects_replayed"] is False
    assert result["interrupted_turn_ids"] == []
    assert result["goal_review_turn_ids"] == []
    assert turn.status == ChatTurnStatus.WAITING_APPROVAL
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "running"
    assert (
        await value.store.get(Approval, approval.id)
    ).status == ApprovalStatus.PENDING
    async with value.coordinator.database.session() as sql:
        admission = await sql.get(ProviderTurnQueueRow, turn.id)
        assert admission.state == "parked" and admission.lease_owner is None
        resource = await sql.get(
            RuntimeResourceRow, value.journal.resource_id(turn.id, "synthetic-segment")
        )
        assert resource.state == "closed"
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )


async def test_dead_owner_interrupts_approved_call_it_was_resuming(orphan):
    """The worker claims a WAITING_APPROVAL turn and runs the approved call
    before the turn changes status; its effect is unknown, never replayed."""
    value = orphan
    approval = await value.store.create(
        Approval(
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            status=ApprovalStatus.APPROVED,
            risk_class=RiskClass.WORKSPACE_WRITE,
            exact_request={"synthetic": True},
            policy_rationale="Synthetic approved write",
            requested_by="test",
        )
    )
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
            approval_id=approval.id,
            started_at=utc_now(),
        )
    )
    await value.store.update(
        ChatTurn,
        value.turn.id,
        {"status": ChatTurnStatus.WAITING_APPROVAL, "approval_id": approval.id},
        expected_revision=value.turn.revision,
    )
    await value.expire()

    result = await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert result["interrupted_turn_ids"] == [turn.id]
    assert turn.status == ChatTurnStatus.INTERRUPTED
    assert turn.request_snapshot["recovery"]["unknown_tool_call_ids"] == [call.id]
    assert not recoverable_after_core_restart(turn)
    assert turn.execution_claim_id is None and goal.execution_claim_id is None
    assert goal.status.value == "paused"
    saved = await value.store.get(ToolCall, call.id)
    assert saved.status == ToolCallStatus.RUNNING and saved.revision == call.revision
    async with value.coordinator.database.session() as sql:
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )


async def test_dead_owner_marks_unacknowledged_adapter_delivery_without_holding(
    orphan,
):
    """A pending adapter handoff held recovery; only the next worker settled it."""
    value = orphan
    approval = await value.store.create(
        Approval(
            engagement_id="one",
            run_id=value.turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=value.session.id,
            chat_turn_id=value.turn.id,
            status=ApprovalStatus.APPROVED,
            risk_class=RiskClass.WORKSPACE_WRITE,
            exact_request={"synthetic": True},
            policy_rationale="Synthetic decided approval",
            requested_by="test",
            continuation=ApprovalContinuation(
                harness_turn_id="synthetic-harness-turn",
                status="delivered",
                adapter_handoff="transport_write",
                adapter_status="pending",
            ),
        )
    )
    await value.expire()

    result = await value.reconcile()

    saved = await value.store.get(Approval, approval.id)
    assert result["unconfirmed_delivery_approval_ids"] == [approval.id]
    assert saved.status == ApprovalStatus.APPROVED
    assert saved.continuation.adapter_status == "unknown"
    assert "No work was replayed" in saved.continuation.adapter_detail
    async with value.coordinator.database.session() as sql:
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


async def test_dead_owner_closes_its_handoff_inventory_handles(orphan):
    """A worker that died mid-handoff left pinned handles holding recovery."""
    value = orphan
    async with value.coordinator.database.session() as sql:
        for identity, state in (("live", "active"), ("contract", "uncertain")):
            sql.add(
                RuntimeResourceRow(
                    id="handoff-inventory:" + identity,
                    project_id="one",
                    runtime_id=value.lease.runtime_id,
                    fencing_token=value.lease.fencing_token,
                    kind="resident_handle",
                    state=state,
                    restoration="pinned",
                    detail="A live handle stays with its runtime",
                    checkpoint={},
                    updated_at_ms=0,
                )
            )
        sql.add(
            RuntimeResourceRow(
                id="handoff-inventory:newer-owner",
                project_id="one",
                runtime_id=value.lease.runtime_id,
                fencing_token=value.lease.fencing_token + 1,
                kind="resident_handle",
                state="active",
                restoration="pinned",
                detail="Another incarnation's handle",
                checkpoint={},
                updated_at_ms=0,
            )
        )
    await value.expire()

    result = await value.reconcile()

    assert {"handoff-inventory:live", "handoff-inventory:contract"} <= set(
        result["closed_resource_ids"]
    )
    async with value.coordinator.database.session() as sql:
        for identity in ("live", "contract"):
            assert (
                await sql.get(RuntimeResourceRow, "handoff-inventory:" + identity)
            ).state == "closed"
        assert (
            await sql.get(RuntimeResourceRow, "handoff-inventory:newer-owner")
        ).state == "active"


async def test_unknown_tool_effect_is_recorded_and_resumes_like_a_restart(orphan):
    """A stopped worker's unknown effect is carried forward, never replayed.

    The operator prefers a project that keeps working (possibly repeating a
    step) over an interrupted turn waiting for review after every deploy.
    """
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
    recovery = turn.request_snapshot["recovery"]
    assert recovery["unknown_tool_call_ids"] == [call.id]
    assert recovery["cause"] == "core_restart"
    assert "resume automatically without replaying the effect" in turn.error
    # Unknown until restart reconciliation carries it forward as outcome-unknown;
    # then Core resumes the turn by itself.
    assert not recoverable_after_core_restart(turn)
    reconciled = turn.model_copy(
        update={
            "request_snapshot": {
                **turn.request_snapshot,
                "recovery": {
                    **recovery,
                    "unknown_tool_call_ids": [],
                    "auto_continued_unknown_tool_call_ids": [call.id],
                    "required": False,
                },
            }
        }
    )
    assert recoverable_after_core_restart(reconciled)
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


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        # Died between acquiring capacity and committing the admission, or
        # between completing the admission and releasing the capacity.
        ("provider-without-admission", "dead-owner-effect-ended"),
        ("tool-unknown", "dead-owner-outcome-unknown"),
        ("tool-ended", "dead-owner-effect-ended"),
        ("earlier-dead-owner", "dead-owner-effect-ended"),
    ],
)
async def test_dead_owner_capacity_is_settled_without_holding_recovery(
    orphan, kind, reason
):
    """Recovery stayed held on "Capacity reservation ... completion evidence"."""
    value = orphan
    request_key = "provider:abandoned-admission"
    if kind.startswith("tool"):
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
                status=ToolCallStatus.COMPLETE
                if kind == "tool-ended"
                else ToolCallStatus.RUNNING,
                started_at=utc_now(),
            )
        )
        request_key = "tool:" + call.id
    async with value.coordinator.database.session() as sql:
        sql.add(
            RuntimeCapacityReservationRow(
                id="leftover-capacity",
                runtime_id="gone-runtime"
                if kind == "earlier-dead-owner"
                else value.lease.runtime_id,
                project_id="one",
                fencing_token=value.lease.fencing_token
                - (1 if kind == "earlier-dead-owner" else 0),
                request_key=request_key,
                demands={"tool": 1} if kind.startswith("tool") else {"provider": 1},
                state="held",
                created_at_ms=0,
            )
        )
    await value.expire()
    result = await value.reconcile()
    async with value.coordinator.database.session() as sql:
        row = await sql.get(RuntimeCapacityReservationRow, "leftover-capacity")
        assert (row.state, row.release_reason) == ("released", reason)
        assert "leftover-capacity" in result["released_capacity_ids"]
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    if kind.startswith("tool"):
        # The effect's own record is kept for review; nothing is replayed.
        saved = await value.store.get(ToolCall, call.id)
        assert saved.status == call.status and saved.revision == call.revision


async def test_capacity_of_a_live_or_newer_owner_is_not_released(orphan, host):
    value = orphan
    live = await worker(host, "v1")
    async with value.coordinator.database.session() as sql:
        for identity, runtime, fence in (
            ("newer-fence", value.lease.runtime_id, value.lease.fencing_token + 1),
            ("live-earlier-owner", live, value.lease.fencing_token - 1),
            ("other-project", value.lease.runtime_id, value.lease.fencing_token),
        ):
            sql.add(
                RuntimeCapacityReservationRow(
                    id=identity,
                    runtime_id=runtime,
                    project_id="two" if identity == "other-project" else "one",
                    fencing_token=fence,
                    request_key="provider:" + identity,
                    demands={"provider": 1},
                    state="held",
                    created_at_ms=0,
                )
            )
    await value.expire()
    await value.reconcile()
    async with value.coordinator.database.session() as sql:
        for identity in ("newer-fence", "live-earlier-owner", "other-project"):
            assert (
                await sql.get(RuntimeCapacityReservationRow, identity)
            ).state == "held"


async def test_reconciled_owner_settles_capacity_left_by_an_earlier_pass(orphan):
    """An owner reconciled before this fix keeps its receipt and is unblocked."""
    value = orphan
    await value.expire()
    result = await value.reconcile()
    async with value.coordinator.database.session() as sql:
        sql.add(
            RuntimeCapacityReservationRow(
                id="left-by-earlier-pass",
                runtime_id=value.lease.runtime_id,
                project_id="one",
                fencing_token=value.lease.fencing_token,
                request_key="provider:abandoned-admission",
                demands={"provider": 1},
                state="held",
                created_at_ms=0,
            )
        )
    assert await value.reconcile() == result
    async with value.coordinator.database.session() as sql:
        assert (
            await sql.get(RuntimeCapacityReservationRow, "left-by-earlier-pass")
        ).state == "released"


async def test_dead_owner_releases_harness_goal_claims_and_fails_its_requests(orphan):
    """A crash during goal-driven harness work must not hold recovery forever."""
    value = orphan
    session = await value.store.create(
        ChatSession(
            engagement_id="one",
            title="Synthetic harness goal",
            provider_profile_id=value.session.provider_profile_id,
            model="synthetic",
        )
    )
    now = utc_now()
    goal = await value.store.create(
        ChatGoal(
            engagement_id="one",
            session_id=session.id,
            objective="Synthetic harness recovery",
            completion_criteria=["Recovery verifies"],
            status="running",
            execution_claim_id="harness-turn",
            execution_owner_id="harness-runtime",
            execution_claimed_at=now,
        )
    )
    claimed = {}
    # A running goal turn, and an earlier one whose interruption kept its claim.
    for name, status in (("harness-turn", "routing"), ("ended", "interrupted")):
        claimed[name] = await value.store.create(
            ChatTurn(
                id=name,
                engagement_id="one",
                session_id=session.id,
                goal_id=goal.id,
                backend="harness",
                model="synthetic",
                status=status,
                execution_claim_id=name,
                execution_owner_id="harness-runtime",
                execution_claimed_at=now,
            )
        )

    def command(identity, sequence, state, *, name="http.invoke/v1", owned=True):
        return RuntimeCommandRow(
            id=identity,
            project_id="one",
            actor_id="operator",
            idempotency_key=identity,
            sequence=sequence,
            name=name,
            lane="work",
            payload={},
            payload_hash="0" * 64,
            accepted_at_ms=0,
            state=state,
            runtime_id=value.lease.runtime_id if owned else None,
            fencing_token=value.lease.fencing_token if owned else None,
        )

    async with value.coordinator.database.session() as sql:
        sql.add(command("running-request", 1, "running"))
        sql.add(command("earlier-uncertain", 2, "uncertain", owned=False))
        sql.add(command("storage-report", 3, "running", name=STORAGE_REPORT_COMMAND))
        sql.add(command("queued-request", 4, "queued", owned=False))
    await value.expire()
    result = await value.reconcile()
    assert sorted(result["released_claim_ids"]) == sorted([goal.id, *claimed])
    assert result["settled_command_ids"] == ["running-request", "earlier-uncertain"]
    assert result["effects_replayed"] is False
    for name, turn in claimed.items():
        saved = await value.store.get(ChatTurn, name)
        assert saved.execution_claim_id is None and saved.execution_owner_id is None
        # The harness runtime's own startup settles the unfinished turn.
        assert saved.status == turn.status
    saved_goal = await value.store.get(ChatGoal, goal.id)
    assert saved_goal.execution_claim_id is None
    async with value.coordinator.database.session() as sql:
        for identity, state in (
            ("running-request", "failed"),
            ("earlier-uncertain", "failed"),
            ("storage-report", "running"),
            ("queued-request", "queued"),
        ):
            row = await sql.get(RuntimeCommandRow, identity)
            assert row.state == state
        failed = await sql.get(RuntimeCommandRow, "running-request")
        assert failed.result["interrupted_by_runtime_stop"] is True
        assert failed.result["previous_state"] == "running"
        await sql.delete(await sql.get(RuntimeCommandRow, "storage-report"))
        await sql.flush()
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


async def test_missing_death_proof_keeps_harness_claims_and_requests(orphan):
    value = orphan
    turn = await value.store.create(
        ChatTurn(
            engagement_id="one",
            session_id=value.session.id,
            backend="harness",
            model="synthetic",
            status="routing",
            execution_claim_id="live-harness-turn",
            execution_owner_id="harness-runtime",
            execution_claimed_at=utc_now(),
        )
    )
    await value.expire(stop=False)
    with pytest.raises(CoordinationConflict):
        await value.reconcile()
    saved = await value.store.get(ChatTurn, turn.id)
    assert saved.execution_claim_id == "live-harness-turn"


def _chat_command(value, identity, sequence, *, name="chat.accept/v1"):
    return RuntimeCommandRow(
        id=identity,
        project_id="one",
        actor_id="operator",
        idempotency_key=identity,
        sequence=sequence,
        name=name,
        lane="work",
        payload={},
        payload_hash="0" * 64,
        chat_start=True,
        accepted_at_ms=0,
        state="running",
        runtime_id=value.lease.runtime_id,
        fencing_token=value.lease.fencing_token,
    )


async def test_dead_owner_requeues_chat_start_that_saved_nothing(orphan):
    """A message accepted as a saved request was silently lost when its worker
    died before saving the turn: the request failed "may have partly applied"
    while nothing of it existed. It now runs once more on the next owner."""
    value = orphan
    async with value.coordinator.database.session() as sql:
        sql.add(_chat_command(value, "unsaved-accept", 1))
        sql.add(_chat_command(value, "unsaved-resume", 2, name="chat.resume/v1"))
        sql.add(_chat_command(value, "saved-accept", 3))
        sql.add(
            RuntimeChatBindingRow(
                command_id="saved-accept",
                project_id="one",
                turn_id=value.turn.id,
                session_id=value.session.id,
                runtime_id=value.lease.runtime_id,
                fencing_token=value.lease.fencing_token,
            )
        )
        sql.add(_chat_command(value, "plain-request", 4, name="http.invoke/v1"))
    await value.expire()

    result = await value.reconcile()

    assert result["requeued_command_ids"] == ["unsaved-accept", "unsaved-resume"]
    assert result["settled_command_ids"] == ["saved-accept", "plain-request"]
    async with value.coordinator.database.session() as sql:
        for identity in ("unsaved-accept", "unsaved-resume"):
            row = await sql.get(RuntimeCommandRow, identity)
            assert (row.state, row.runtime_id, row.fencing_token) == (
                "queued",
                None,
                None,
            )
            assert row.result == {"interrupted_runs": 1}
        saved = await sql.get(RuntimeCommandRow, "saved-accept")
        assert saved.state == "failed"
        assert saved.result["saved_turn"] is True
        assert saved.result["turn_id"] == value.turn.id
        assert "may have partly applied" not in saved.result["detail"]
        plain = await sql.get(RuntimeCommandRow, "plain-request")
        assert plain.state == "failed"
        assert plain.result["interrupted_by_runtime_stop"] is True
        assert not await value.coordinator._recovery_blockers(
            sql, "one", value.lease.runtime_id
        )
    assert await value.reconcile() == result


async def test_repeatedly_interrupted_chat_start_stops_being_requeued(orphan):
    value = orphan
    async with value.coordinator.database.session() as sql:
        row = _chat_command(value, "unlucky-accept", 1)
        row.result = {"interrupted_runs": 2}
        sql.add(row)
    await value.expire()
    result = await value.reconcile()
    assert result["requeued_command_ids"] == []
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(RuntimeCommandRow, "unlucky-accept")).state == "failed"


async def test_chat_start_that_published_a_response_is_not_requeued(orphan):
    from nebula.v3.database import RuntimeDeliveryRow

    value = orphan
    async with value.coordinator.database.session() as sql:
        sql.add(_chat_command(value, "answered-accept", 1))
        sql.add(
            RuntimeDeliveryRow(
                id="frame-1",
                project_id="one",
                command_id="answered-accept",
                sequence=1,
                kind="head",
                content_reference="0" * 64,
            )
        )
    await value.expire()
    result = await value.reconcile()
    assert result["requeued_command_ids"] == []
    assert result["settled_command_ids"] == ["answered-accept"]


async def test_dead_owner_requeues_tracked_request_that_saved_nothing(orphan):
    """A non-streamed request (stream:false chat, goal action, unarchive) whose
    worker died before saving anything failed "may have partly applied". Its
    write record now proves nothing was saved, so it runs once more; one that
    saved changes is settled from them and never run again."""
    from nebula.v3.database import RuntimeCommandWriteRow
    from nebula.v3.runtime_orphan_recovery import (
        SAVED_REQUEST_RESULT,
        UNSAVED_REQUEST_RESULT,
    )

    value = orphan

    def tracked(identity, *, wrote, fencing_token=None):
        return RuntimeCommandWriteRow(
            command_id=identity,
            project_id="one",
            runtime_id=value.lease.runtime_id,
            fencing_token=value.lease.fencing_token
            if fencing_token is None
            else fencing_token,
            wrote=wrote,
        )

    async with value.coordinator.database.session() as sql:
        for sequence, identity in enumerate(
            ("unsaved", "saved", "untracked", "stale-record", "exhausted"), 1
        ):
            row = _chat_command(value, identity, sequence, name="http.invoke/v1")
            if identity == "exhausted":
                row.result = {"interrupted_runs": 2}
            sql.add(row)
        sql.add(tracked("unsaved", wrote=False))
        sql.add(tracked("saved", wrote=True))
        # A record from an earlier owner says nothing about this run.
        sql.add(
            tracked(
                "stale-record", wrote=False, fencing_token=value.lease.fencing_token - 1
            )
        )
        sql.add(tracked("exhausted", wrote=False))
    await value.expire()

    result = await value.reconcile()

    assert result["requeued_command_ids"] == ["unsaved"]
    assert result["settled_command_ids"] == [
        "saved",
        "untracked",
        "stale-record",
        "exhausted",
    ]
    async with value.coordinator.database.session() as sql:
        unsaved = await sql.get(RuntimeCommandRow, "unsaved")
        assert (unsaved.state, unsaved.runtime_id, unsaved.result) == (
            "queued",
            None,
            {"interrupted_runs": 1},
        )
        # The next owner records its own run.
        assert await sql.get(RuntimeCommandWriteRow, "unsaved") is None
        saved = await sql.get(RuntimeCommandRow, "saved")
        assert saved.state == "failed"
        assert saved.result == {**SAVED_REQUEST_RESULT, "previous_state": "running"}
        # Settled results carry what the records proved; the records go.
        assert await sql.get(RuntimeCommandWriteRow, "saved") is None
        for identity in ("untracked", "stale-record"):
            row = await sql.get(RuntimeCommandRow, identity)
            assert row.state == "failed"
            assert "may have partly applied" in row.result["detail"]
        exhausted = await sql.get(RuntimeCommandRow, "exhausted")
        assert exhausted.result == {
            **UNSAVED_REQUEST_RESULT,
            "previous_state": "running",
        }
    assert await value.reconcile() == result
