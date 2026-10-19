"""A vendor load failure may retry one scheduled case without replaying writes."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_oversight import project_state_id
from nebula.v3.butler_recovery import ButlerRecoveryAgent, ButlerRecoveryService
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    Engagement,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    utc_now,
)
from nebula.v3.storage import NebulaStore


def _failed_case(tmp_path, error_code: str, *, changed: bool = False):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    policy = store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id), engagement_id=project.id, authority="act"
        )
    )
    worker_id = "worker-1"
    store.create(
        ButlerProjectState(
            id=project_state_id(project.id),
            engagement_id=project.id,
            lease_owner=worker_id,
            lease_expires_at=utc_now() + timedelta(minutes=10),
        )
    )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="event-1",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="needs_attention",
            summary="Scheduled check",
            outcome="Butler could not finish the investigation (HarnessUnavailableError).",
            finished_at=utc_now(),
            steps=(
                [
                    {
                        "operation": "nebula.global.create_recovery_goal",
                        "status": "failed",
                    }
                ]
                if changed
                else []
            ),
        )
    )
    session = store.create(
        HarnessSession(
            engagement_id=project.id, harness_profile_id="codex", model="gpt-6-sol"
        )
    )
    turn = store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            origin=HarnessTurnOrigin.ANALYSIS,
            prompt="Check Core",
            status=HarnessTurnStatus.INTERRUPTED,
            error="Selected model is at capacity",
            metadata={"butler_recovery_case_id": case.id},
        )
    )
    case = store.update(
        ButlerRecoveryCase,
        case.id,
        {"model_backend": ChatBackend.HARNESS, "model_turn_id": turn.id},
        expected_revision=case.revision,
    )
    store.append_operation_event(
        turn.id,
        "harness_turn",
        project.id,
        "harness.notice",
        {"type": "notice", "payload": {"error": {"codexErrorInfo": error_code}}},
    )
    return store, project, case, turn, worker_id


def test_server_load_reopens_same_case_once_and_keeps_both_turns(tmp_path):
    store, project, case, first_turn, worker_id = _failed_case(
        tmp_path, "serverOverloaded"
    )
    recorder = ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=None,
        memory=None,
    )

    class Agent:
        def _global_instructions(self, _case):
            return "Current mandate"

        async def run(self, active_case):
            recorder._record_model_turn(
                active_case.id, ChatBackend.HARNESS, "codex", "gpt-6-sol", "retry-turn"
            )
            return (
                "Core state inspected",
                ChatBackend.HARNESS,
                "codex",
                "gpt-6-sol",
                "retry-turn",
            )

    service = ButlerRecoveryService(store, Agent())
    asyncio.run(service.process_project(project.id, worker_id))
    saved = store.get(ButlerRecoveryCase, case.id)
    assert saved.capacity_retry_count == 1
    assert saved.model_turn_ids == [first_turn.id, "retry-turn"]
    assert saved.model_turn_id == "retry-turn"
    assert saved.status == "needs_attention"
    assert service.retry_overloaded_cases(project.id, worker_id) == 0


def test_quota_error_or_attempted_change_never_reopens(tmp_path):
    for code, changed in (("usageLimitExceeded", False), ("serverOverloaded", True)):
        branch = tmp_path / ("changed" if changed else "quota")
        branch.mkdir()
        store, project, case, _, worker_id = _failed_case(branch, code, changed=changed)
        agent = type("Agent", (), {"_global_instructions": lambda self, _: "mandate"})()
        service = ButlerRecoveryService(store, agent)
        assert service.retry_overloaded_cases(project.id, worker_id) == 0
        assert store.get(ButlerRecoveryCase, case.id).status == "needs_attention"
