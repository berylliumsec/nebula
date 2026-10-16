"""Pending-review events follow saved Core state and remain project-scoped."""

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


def _setup(tmp_path):
    store = NebulaStore(tmp_path / "attention.db")
    store.create(Engagement(id="project-a", name="A"))
    store.create(Engagement(id="project-b", name="B"))
    store.create(
        ProviderProfile(
            id="provider", name="Local", provider_type="ollama", is_local=True
        )
    )
    store.create(
        ChatSession(
            id="chat",
            engagement_id="project-a",
            title="Parent",
            provider_profile_id="provider",
            model="model",
        )
    )
    return store


def _events(store, project="project-a"):
    return store.replay_operation_events(project_stream_id(project))


def _incident(store, source):
    return store.find_entities(
        ButlerIncident, {"source": source}, engagement_id="project-a"
    )[0]


def test_approval_is_durable_project_attention_and_resolves_with_source(tmp_path):
    store = _setup(tmp_path)
    approval = store.create(
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
    incident = _incident(store, "pending_approval")
    assert incident.status == "open" and incident.session_ids == ["chat"]
    assert [event.event_type for event in _events(store)] == [
        "review.approval_requested"
    ]
    assert "private value" not in str(_events(store)[0].payload)
    assert _events(store, "project-b") == []

    store.update(
        Approval,
        approval.id,
        {"status": ApprovalStatus.APPROVED, "decided_at": utc_now()},
        expected_revision=approval.revision,
    )
    assert store.get(ButlerIncident, incident.id).status == "resolved"
    assert [event.event_type for event in _events(store)] == [
        "review.approval_requested",
        "review.resolved",
    ]
    reconcile_attention(store)
    assert len(_events(store)) == 2


def test_secret_question_is_visible_without_prompt_and_mission_is_linked(tmp_path):
    store = _setup(tmp_path)
    question = store.create(
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
    incident = _incident(store, "pending_question")
    assert incident.status == "open"
    assert {ref["kind"] for ref in incident.evidence_refs} == {
        "harness_interaction",
        "mission",
    }
    assert "secret password" not in str(_events(store)[0].payload)
    store.update(
        HarnessInteraction,
        question.id,
        {"status": HarnessInteractionStatus.ANSWERED, "resolved_at": utc_now()},
        expected_revision=question.revision,
    )
    assert store.get(ButlerIncident, incident.id).status == "resolved"


def test_queue_review_tracks_each_item_and_restart_reconciliation(
    tmp_path, monkeypatch
):
    store = _setup(tmp_path)
    queue = store.create(
        ChatQueue(
            id="chat-queue-chat", engagement_id="project-a", session_id="chat", items=[]
        )
    )
    changed = store.update(
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
    assert _incident(store, "pending_queue").status == "open"
    assert _events(store)[0].event_type == "review.queue_requested"
    store.update(
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
    assert _incident(store, "pending_queue").status == "resolved"

    import nebula.v3.butler_attention as attention

    monkeypatch.setattr(attention, "record_attention_transition", lambda *_: None)
    pending = store.update(
        ChatQueue,
        queue.id,
        {"items": [{"id": "item-c", "status": "needs_review"}]},
        expected_revision=changed.revision + 1,
    )
    assert pending.items[0]["status"] == "needs_review"
    monkeypatch.undo()
    reconcile_attention(store)
    assert (
        len(
            store.find_entities(
                ButlerIncident, {"source": "pending_queue"}, engagement_id="project-a"
            )
        )
        == 2
    )
    assert _events(store)[-1].event_type == "review.queue_requested"


def test_completed_goal_reply_is_reviewed_from_saved_message_not_event_text(tmp_path):
    store = _setup(tmp_path)
    goal = store.create(
        ChatGoal(
            id="goal-a",
            engagement_id="project-a",
            session_id="chat",
            objective="Research",
            completion_criteria=["Report"],
            status=ChatGoalStatus.RUNNING,
        )
    )
    turn = store.create(
        ChatTurn(
            id="turn-a",
            engagement_id="project-a",
            session_id="chat",
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model",
        )
    )
    message = store.create(
        ChatMessage(
            id="message-a",
            engagement_id="project-a",
            session_id="chat",
            sequence=1,
            role=ChatRole.ASSISTANT,
            content="I need you to review the evidence before I proceed.",
        )
    )
    store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.COMPLETE, "final_message_id": message.id},
        expected_revision=turn.revision,
    )
    event = _events(store)[0]
    assert event.event_type == "agent.turn_completed" and is_meaningful_event(event)
    assert "review the evidence" not in str(event.payload)
    packet = build_review_packet(store, "project-a", event)
    assert "review the evidence" in packet.event_summary
    assert {ref.kind for ref in packet.evidence_refs} == {"goal", "chat_turn"}


def test_act_investigates_pending_review_and_only_approves_exact_low_risk_request(
    tmp_path,
):
    store = _setup(tmp_path)
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id("project-a"), engagement_id="project-a", authority="act"
        )
    )
    approval = store.create(
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
    state = store.get(ButlerProjectState, project_state_id("project-a"))
    store.update(
        ButlerProjectState,
        state.id,
        {
            "lease_owner": "worker",
            "lease_expires_at": utc_now() + timedelta(minutes=2),
        },
        expected_revision=state.revision,
    )
    recovery = ButlerRecoveryService(store)
    recovery.capture_events("project-a", "worker")
    case = store.find_entities(ButlerRecoveryCase, {}, engagement_id="project-a")[0]
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
    capability._check_background_change_reason(
        case, "nebula.work.decide_approval", arguments
    )
    with pytest.raises(ValueError):
        capability._check_background_change_reason(
            case, "nebula.work.decide_approval", {**arguments, "action_id": "other"}
        )
    with pytest.raises(ValueError):
        capability._check_background_change_reason(
            case, "nebula.work.decide_approval", {**arguments, "expected_revision": 99}
        )
    store.update(
        Approval,
        approval.id,
        {"risk_class": RiskClass.WORKSPACE_WRITE},
        expected_revision=approval.revision,
    )
    with pytest.raises(ValueError):
        capability._check_background_change_reason(
            case,
            "nebula.work.decide_approval",
            {**arguments, "expected_revision": approval.revision + 1},
        )
