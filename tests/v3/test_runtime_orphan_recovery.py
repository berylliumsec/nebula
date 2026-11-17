"""A dead process can be reconciled without replaying its interrupted effects."""

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
async def test_failed_provider_claim_is_retained_while_effect_is_unconfirmed(
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

    with pytest.raises(CoordinationConflict, match="unconfirmed|unfinished"):
        await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id is not None
    assert goal.status.value == "running"
    assert goal.execution_claim_id == turn.execution_claim_id
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, turn.id)).state == "running"
        assert not await sql.scalar(
            select(DeploymentReceiptRow.id).where(
                DeploymentReceiptRow.action == "project.orphans_reconciled"
            )
        )


async def test_failed_provider_claim_requires_exact_paired_goal_claim(orphan):
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

    with pytest.raises(CoordinationConflict, match="exact running goal claim"):
        await value.reconcile()

    assert (
        await value.store.get(ChatTurn, value.turn.id)
    ).execution_claim_id == value.turn.execution_claim_id
    assert (
        await value.store.get(ChatGoal, value.goal.id)
    ).execution_claim_id == "different-claim"


@pytest.mark.parametrize("reference", ["missing", "wrong_turn", "wrong_project"])
async def test_failed_provider_claim_rejects_dangling_or_mismatched_approval(
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
        initial_resource_state = (await sql.get(RuntimeResourceRow, resource_id)).state
    await value.expire()

    with pytest.raises(
        CoordinationConflict, match="references (a missing approval|an approval)"
    ):
        await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id == value.turn.execution_claim_id
    assert goal.status.value == "running"
    assert goal.execution_claim_id == value.goal.execution_claim_id
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, turn.id)).state == "running"
        assert (
            await sql.get(RuntimeResourceRow, resource_id)
        ).state == initial_resource_state
        assert not await sql.scalar(
            select(DeploymentReceiptRow.id).where(
                DeploymentReceiptRow.action == "project.orphans_reconciled"
            )
        )


@pytest.mark.parametrize("reference", ["missing", "wrong_project", "wrong_turn"])
async def test_failed_provider_claim_rejects_dangling_tool_call_approval(
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
        initial_resource_state = (await sql.get(RuntimeResourceRow, resource_id)).state
    await value.expire()

    with pytest.raises(
        CoordinationConflict,
        match="references (a missing approval|an approval for another project, turn, or call)",
    ):
        await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id == value.turn.execution_claim_id
    assert goal.status.value == "running"
    assert goal.execution_claim_id == value.goal.execution_claim_id
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, turn.id)).state == "running"
        assert (
            await sql.get(RuntimeResourceRow, resource_id)
        ).state == initial_resource_state
        assert not await sql.scalar(
            select(DeploymentReceiptRow.id).where(
                DeploymentReceiptRow.action == "project.orphans_reconciled"
            )
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
async def test_failed_provider_claim_rejects_unsettled_or_mismatched_approval_links(
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
        initial_resource_state = (await sql.get(RuntimeResourceRow, resource_id)).state
    await value.expire()

    with pytest.raises(
        CoordinationConflict,
        match="references (a missing tool call|another project, turn, or tool call)|no exact settled tool call|unconfirmed approval effect",
    ):
        await value.reconcile()

    turn = await value.store.get(ChatTurn, value.turn.id)
    goal = await value.store.get(ChatGoal, value.goal.id)
    assert turn.status == ChatTurnStatus.FAILED
    assert turn.execution_claim_id == value.turn.execution_claim_id
    assert goal.status.value == "running"
    assert goal.execution_claim_id == value.goal.execution_claim_id
    async with value.coordinator.database.session() as sql:
        assert (await sql.get(ProviderTurnQueueRow, turn.id)).state == "running"
        assert (
            await sql.get(RuntimeResourceRow, resource_id)
        ).state == initial_resource_state
        assert not await sql.scalar(
            select(DeploymentReceiptRow.id).where(
                DeploymentReceiptRow.action == "project.orphans_reconciled"
            )
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


async def test_dead_owner_retains_unmatched_command_callback(orphan):
    value = orphan
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
            "tool_call_id": "missing-tool-call",
            "name": "run_command",
            "status": "waiting_callback",
            "process_id": "synthetic-process",
        },
    )
    await value.expire()
    with pytest.raises(CoordinationConflict, match="command callback"):
        await value.reconcile()
    turn = await value.store.get(ChatTurn, value.turn.id)
    assert turn.status == ChatTurnStatus.WAITING_CALLBACK
    assert turn.execution_claim_id is not None


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
