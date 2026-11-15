"""Pending-review events follow saved Core state and remain project-scoped."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

import pytest

from nebula.v3.butler_attention import reconcile_attention
from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_oversight import project_state_id, project_stream_id
from nebula.v3.butler_recovery import ButlerRecoveryService
from nebula.v3.butler_review import build_review_packet, is_meaningful_event
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ButlerAuthorityPolicy,
    ButlerIncident,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatQueue,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessInteraction,
    HarnessInteractionKind,
    HarnessInteractionStatus,
    HarnessTurnOrigin,
    ProviderProfile,
    RiskClass,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.storage import NebulaStore


async def _setup(tmp_path):
    store = NebulaStore(tmp_path / "attention.db")
    (await store.create(Engagement(id="project-a", name="A")))
    (await store.create(Engagement(id="project-b", name="B")))
    (
        await store.create(
            ProviderProfile(
                id="provider", name="Local", provider_type="ollama", is_local=True
            )
        )
    )
    (
        await store.create(
            ChatSession(
                id="chat",
                engagement_id="project-a",
                title="Parent",
                provider_profile_id="provider",
                model="model",
            )
        )
    )
    return store


async def _events(store, project="project-a"):
    return await store.replay_operation_events(project_stream_id(project))


async def _incident(store, source):
    return (
        await store.find_entities(
            ButlerIncident, {"source": source}, engagement_id="project-a"
        )
    )[0]


async def test_approval_is_durable_project_attention_and_resolves_with_source(tmp_path):
    store = await _setup(tmp_path)
    approval = await store.create(
        Approval(
            id="approval-a",
            engagement_id="project-a",
            run_id="run-a",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="chat",
            risk_class=RiskClass.LOCAL_READ,
            exact_request={
                "tool_name": "workspace.read",
                "arguments": {"secret": "private value"},
            },
            policy_rationale="Check exact request",
            requested_by="agent",
        )
    )
    incident = await _incident(store, "pending_approval")
    assert incident.status == "open" and incident.session_ids == ["chat"]
    assert [event.event_type for event in (await _events(store))] == [
        "review.approval_requested"
    ]
    assert "private value" not in str((await _events(store))[0].payload)
    assert (await _events(store, "project-b")) == []

    (
        await store.update(
            Approval,
            approval.id,
            {"status": ApprovalStatus.APPROVED, "decided_at": utc_now()},
            expected_revision=approval.revision,
        )
    )
    assert (await store.get(ButlerIncident, incident.id)).status == "resolved"
    assert [event.event_type for event in (await _events(store))] == [
        "review.approval_requested",
        "review.resolved",
    ]
    (await reconcile_attention(store))
    assert len((await _events(store))) == 2


async def test_secret_question_is_visible_without_prompt_and_mission_is_linked(
    tmp_path,
):
    store = await _setup(tmp_path)
    question = await store.create(
        HarnessInteraction(
            id="question-a",
            engagement_id="project-a",
            harness_turn_id="turn-a",
            harness_session_id="harness-a",
            origin=HarnessTurnOrigin.MISSION,
            kind=HarnessInteractionKind.USER_INPUT,
            run_id="mission-a",
            vendor_request_id="vendor-a",
            prompt="Enter secret password",
            contains_secret=True,
        )
    )
    incident = await _incident(store, "pending_question")
    assert incident.status == "open"
    assert {ref["kind"] for ref in incident.evidence_refs} == {
        "harness_interaction",
        "mission",
    }
    assert "secret password" not in str((await _events(store))[0].payload)
    (
        await store.update(
            HarnessInteraction,
            question.id,
            {"status": HarnessInteractionStatus.ANSWERED, "resolved_at": utc_now()},
            expected_revision=question.revision,
        )
    )
    assert (await store.get(ButlerIncident, incident.id)).status == "resolved"


async def test_queue_review_tracks_each_item_and_restart_reconciliation(
    tmp_path, monkeypatch
):
    store = await _setup(tmp_path)
    queue = await store.create(
        ChatQueue(
            id="chat-queue-chat", engagement_id="project-a", session_id="chat", items=[]
        )
    )
    changed = await store.update(
        ChatQueue,
        queue.id,
        {
            "items": [
                {"id": "item-a", "status": "needs_review"},
                {"id": "item-b", "status": "queued"},
            ]
        },
        expected_revision=queue.revision,
    )
    assert (await _incident(store, "pending_queue")).status == "open"
    assert (await _events(store))[0].event_type == "review.queue_requested"
    (
        await store.update(
            ChatQueue,
            queue.id,
            {
                "items": [
                    {"id": "item-a", "status": "queued"},
                    {"id": "item-b", "status": "queued"},
                ]
            },
            expected_revision=changed.revision,
        )
    )
    assert (await _incident(store, "pending_queue")).status == "resolved"

    import nebula.v3.butler_attention as attention

    async def skip_attention_transition(*_):
        return None

    monkeypatch.setattr(
        attention, "record_attention_transition", skip_attention_transition
    )
    pending = await store.update(
        ChatQueue,
        queue.id,
        {"items": [{"id": "item-c", "status": "needs_review"}]},
        expected_revision=changed.revision + 1,
    )
    assert pending.items[0]["status"] == "needs_review"
    monkeypatch.undo()
    (await reconcile_attention(store))
    assert (
        len(
            (
                await store.find_entities(
                    ButlerIncident,
                    {"source": "pending_queue"},
                    engagement_id="project-a",
                )
            )
        )
        == 2
    )
    assert (await _events(store))[-1].event_type == "review.queue_requested"


async def test_completed_goal_reply_is_reviewed_from_saved_message_not_event_text(
    tmp_path,
):
    store = await _setup(tmp_path)
    goal = await store.create(
        ChatGoal(
            id="goal-a",
            engagement_id="project-a",
            session_id="chat",
            objective="Research",
            completion_criteria=["Report"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    turn = await store.create(
        ChatTurn(
            id="turn-a",
            engagement_id="project-a",
            session_id="chat",
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model",
        )
    )
    message = await store.create(
        ChatMessage(
            id="message-a",
            engagement_id="project-a",
            session_id="chat",
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="I need you to review the evidence before I proceed.",
        )
    )
    (
        await store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.COMPLETE, "final_message_id": message.id},
            expected_revision=turn.revision,
        )
    )
    event = (await _events(store))[0]
    assert event.event_type == "agent.turn_completed" and is_meaningful_event(event)
    assert "review the evidence" not in str(event.payload)
    packet = await build_review_packet(store, "project-a", event)
    assert "review the evidence" in packet.event_summary
    assert {ref.kind for ref in packet.evidence_refs} == {"goal", "chat_turn"}


async def test_act_investigates_pending_review_and_only_approves_exact_low_risk_request(
    tmp_path,
):
    store = await _setup(tmp_path)
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id("project-a"), engagement_id="project-a", authority="act"
        )
    )
    approval = await store.create(
        Approval(
            id="approval-a",
            engagement_id="project-a",
            run_id="run-a",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="chat",
            risk_class=RiskClass.LOCAL_READ,
            exact_request={"tool_name": "workspace.read"},
            policy_rationale="Read",
            requested_by="agent",
        )
    )
    state = await store.get(ButlerProjectState, project_state_id("project-a"))
    (
        await store.update(
            ButlerProjectState,
            state.id,
            {
                "lease_owner": "worker",
                "lease_expires_at": utc_now() + timedelta(minutes=2),
            },
            expected_revision=state.revision,
        )
    )
    recovery = ButlerRecoveryService(store)
    (await recovery.capture_events("project-a", "worker"))
    case = (
        await store.find_entities(ButlerRecoveryCase, {}, engagement_id="project-a")
    )[0]
    assert (
        case.signal == "review.approval_requested"
        and case.authority_revision == policy.revision
    )
    capability = ButlerCapabilities.__new__(ButlerCapabilities)
    capability._store = store
    capability._project_id = "project-a"
    arguments = {
        "action_id": approval.id,
        "decision": "approve",
        "expected_revision": approval.revision,
    }
    await capability._check_background_change_reason(
        case, "nebula.work.decide_approval", arguments
    )
    with pytest.raises(ValueError):
        await capability._check_background_change_reason(
            case, "nebula.work.decide_approval", {**arguments, "action_id": "other"}
        )
    with pytest.raises(ValueError):
        await capability._check_background_change_reason(
            case, "nebula.work.decide_approval", {**arguments, "expected_revision": 99}
        )
    (
        await store.update(
            Approval,
            approval.id,
            {"risk_class": RiskClass.WORKSPACE_WRITE},
            expected_revision=approval.revision,
        )
    )
    with pytest.raises(ValueError):
        await capability._check_background_change_reason(
            case,
            "nebula.work.decide_approval",
            {**arguments, "expected_revision": approval.revision + 1},
        )


pytestmark = pytest.mark.anyio
