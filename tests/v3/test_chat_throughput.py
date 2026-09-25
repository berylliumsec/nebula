import asyncio
import json

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from nebula.v3.chat import ChatError, ChatService
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import EntityRow, ProviderTurnQueueRow
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    utc_now,
)
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.providers import ProviderResponseError, ToolCall, ToolChoice
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.tools import ApprovalRequired, ToolExecutionResult
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _drain,
    _finish,
    _request,
    _setup,
)
from tests.v3.test_chat_subagents import _response as _subagent_response
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response


def _store(tmp_path):
    store = NebulaStore(tmp_path / "throughput.db")
    project = store.create(Engagement(id="project", name="Throughput"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
        )
    )
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Throughput",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    return store, project, profile, session


def test_long_turn_uses_checkpointed_ledger_without_growing_turn_json(tmp_path):
    store, project, profile, session = _store(tmp_path)
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            queued_at=utc_now(),
        )
    )
    ledger = ChatTurnLedger(store.database)
    baseline_replay_bytes = 0
    compact_replay_bytes = 0
    for step in range(120):
        entry = {
            "step": step,
            "response_group": f"group-{step}",
            "model_call_id": f"call-{step}",
            "tool_call_id": f"tool-{step}",
            "name": "workspace.read",
            "arguments": {"path": f"file-{step}.txt"},
            "status": "complete",
            "provider_result": {"status": "complete", "preview": "x" * 400},
            "result_summary": f"Read file {step}",
        }
        ledger.append(turn.id, entry)
        history = ledger.history(turn)
        checkpoint, replay = ledger.compacted_history(turn)
        baseline_replay_bytes += len(json.dumps(history))
        compact_replay_bytes += len(json.dumps(replay)) + (
            len(json.dumps(checkpoint.summary)) if checkpoint else 0
        )

    checkpoint, replay = ledger.compacted_history(turn)
    assert checkpoint is not None
    assert checkpoint.through_step >= 111
    # The checkpoint advances every 16 folded steps, so the replay is the
    # latest eight groups plus at most 15 steps not folded yet.
    assert len(replay) <= 8 + 15
    assert compact_replay_bytes < baseline_replay_bytes * 0.35
    with store.database.session() as session_db:
        row = session_db.scalar(select(EntityRow).where(EntityRow.id == turn.id))
        assert row is not None
        assert len(json.dumps(row.payload)) < 64 * 1024
        assert row.payload["tool_history"] == []


def test_scheduler_caps_total_and_background_provider_work(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        scheduler = ProviderScheduler(
            store,
            worker_id="worker",
            config=ProviderSchedulerConfig(
                provider_limit=6,
                background_limit=4,
                per_turn_tool_limit=4,
                global_tool_limit=8,
            ),
        )
        turns = []
        for index in range(20):
            turn = store.create(
                ChatTurn(
                    id=f"turn-{index:02d}",
                    engagement_id=project.id,
                    session_id=session.id,
                    provider_profile_id=profile.id,
                    model="model-a",
                    status=ChatTurnStatus.QUEUED,
                    queued_at=utc_now(),
                    capacity_lane="background" if index < 10 else "direct",
                )
            )
            scheduler.enqueue(turn)
            turns.append(turn)

        release = asyncio.Event()
        active = 0
        active_background = 0
        peak = 0
        peak_background = 0
        all_six = asyncio.Event()

        async def run(turn):
            nonlocal active, active_background, peak, peak_background
            admission = await scheduler.admit(turn.id)
            active += 1
            active_background += turn.capacity_lane == "background"
            peak = max(peak, active)
            peak_background = max(peak_background, active_background)
            if active == 6:
                all_six.set()
            await release.wait()
            active -= 1
            active_background -= turn.capacity_lane == "background"
            await admission.release(turn.id)

        tasks = [asyncio.create_task(run(turn)) for turn in turns]
        await asyncio.wait_for(all_six.wait(), 2)
        assert scheduler.metrics()["active"] == 6
        assert scheduler.metrics()["active_background"] == 4
        release.set()
        await asyncio.gather(*tasks)
        assert peak == 6
        assert peak_background == 4
        assert scheduler.metrics()["queued"] == 0

    asyncio.run(scenario())


def test_queued_cancellation_prevents_provider_admission(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        scheduler = ProviderScheduler(
            store,
            worker_id="worker",
            config=ProviderSchedulerConfig(provider_limit=1, background_limit=1),
        )
        first = store.create(
            ChatTurn(
                id="first",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        second = store.create(
            ChatTurn(
                id="second",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        scheduler.enqueue(first)
        scheduler.enqueue(second)
        first_admission = await scheduler.admit(first.id)
        waiting = asyncio.create_task(scheduler.admit(second.id))
        await asyncio.sleep(0)
        scheduler.cancel(second.id)
        latest = store.get(ChatTurn, second.id)
        store.update(
            ChatTurn,
            second.id,
            {"status": ChatTurnStatus.CANCELLED},
            expected_revision=latest.revision,
        )
        await first_admission.release(first.id)
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert scheduler.metrics()["active"] == 0

    asyncio.run(scenario())


def test_recovery_clears_an_expired_queued_lease(tmp_path):
    store, project, profile, session = _store(tmp_path)
    turn = store.create(
        ChatTurn(
            id="leased",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.QUEUED,
            queued_at=utc_now(),
        )
    )
    scheduler = ProviderScheduler(store, worker_id="worker")
    scheduler.enqueue(turn)
    with store.database.session() as database_session:
        row = database_session.get(ProviderTurnQueueRow, turn.id)
        assert row is not None
        row.lease_owner = "dead-worker"
        row.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert scheduler.recover() == [turn.id]
    with store.database.session() as database_session:
        recovered = database_session.get(ProviderTurnQueueRow, turn.id)
        assert recovered is not None
        assert recovered.lease_owner is None
        assert recovered.lease_expires_at is None


def _queue_state(store: NebulaStore, turn_id: str) -> str | None:
    with store.database.session() as database_session:
        row = database_session.get(ProviderTurnQueueRow, turn_id)
        return row.state if row is not None else None


@pytest.mark.parametrize(
    ("parked", "admitted"),
    [
        (ChatTurnStatus.QUEUED, ChatTurnStatus.ROUTING),
        (ChatTurnStatus.ROUTING, ChatTurnStatus.ROUTING),
        (ChatTurnStatus.WAITING_APPROVAL, ChatTurnStatus.WAITING_APPROVAL),
        (ChatTurnStatus.WAITING_CALLBACK, ChatTurnStatus.WAITING_CALLBACK),
        (ChatTurnStatus.FINALIZING, ChatTurnStatus.FINALIZING),
    ],
)
def test_admission_starts_only_a_new_turn_routing(tmp_path, parked, admitted):
    """A resumed turn keeps the parked state that selects how it resumes."""

    async def scenario():
        store, project, profile, session = _store(tmp_path)
        turn = store.create(
            ChatTurn(
                id="parked",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=parked,
                queued_at=utc_now(),
            )
        )
        scheduler = ProviderScheduler(store, worker_id="worker")
        scheduler.enqueue(turn)
        admission = await scheduler.admit(turn.id)
        latest = store.get(ChatTurn, turn.id)
        assert latest.status == admitted
        assert latest.admitted_at is not None
        await admission.release(turn.id)
        # Releasing twice (early, then on task exit) completes it only once.
        scheduler.enqueue(latest)
        second = await scheduler.admit(turn.id)
        await admission.release(turn.id)
        assert _queue_state(store, turn.id) == "running"
        assert scheduler.metrics()["active"] == 1
        await second.release(turn.id)
        assert scheduler.metrics()["active"] == 0

    asyncio.run(scenario())


class _ApprovingBroker(RecordingBroker):
    def __init__(self) -> None:
        super().__init__()
        self.approval: Approval | None = None
        self.approved_runs = 0

    async def execute(self, invocation, scope, *, approval=None):
        del scope
        self.calls.append(invocation)
        if approval is None:
            assert self.approval is not None
            raise ApprovalRequired(self.approval)
        self.approved_runs += 1
        return ToolExecutionResult(output={"value": invocation.arguments["value"]})


def _approval_turn(tmp_path, responses):
    broker = _ApprovingBroker()
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)
    broker.approval = store.create(
        Approval(
            id="approval-1",
            engagement_id="project",
            run_id="turn",
            risk_class=RiskClass.LOCAL_READ,
            exact_request={"tool_name": "safe_read", "arguments": {"value": "b"}},
            policy_rationale="the operator approves this read",
            requested_by="chat-assistant",
        )
    )
    return store, service, prepared, provider, broker


def _approve(store: NebulaStore) -> None:
    approval = store.get(Approval, "approval-1")
    store.update(
        Approval,
        approval.id,
        {
            "status": ApprovalStatus.APPROVED,
            "decided_by": "operator",
            "decided_at": utc_now(),
        },
        expected_revision=approval.revision,
    )


def _approval_script():
    return [
        _response(
            calls=[ToolCall(id="call-1", name="safe_read", arguments={"value": "b"})]
        ),
        _response(
            calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
        ),
        _response(text="The approved read returned b."),
    ]


def test_approved_call_runs_when_its_turn_resumes_through_admission(tmp_path):
    """The operator's approval resume (POST /chat/turns/{id}/resume) runs the call."""

    async def scenario():
        store, service, prepared, provider, broker = _approval_turn(
            tmp_path, _approval_script()
        )
        turn_id = service.start_provider_turn(prepared)
        first = [event async for event, _ in service.follow_provider_turn(turn_id)]
        assert first[-1] == "approval_required"
        paused = store.get(ChatTurn, turn_id)
        assert paused.status == ChatTurnStatus.WAITING_APPROVAL
        assert paused.execution_claim_id is None
        _approve(store)

        prepared.turn = paused
        prepared.execution_claim_id = None
        service.start_provider_turn(prepared)
        resumed = [event async for event, _ in service.follow_provider_turn(turn_id)]

        assert resumed[:3] == ["queued", "admitted", "started"]
        assert "tool_completed" in resumed
        assert resumed[-1] == "done"
        assert broker.approved_runs == 1
        finished = store.get(ChatTurn, turn_id)
        assert finished.status == ChatTurnStatus.COMPLETE
        (step,) = service._turn_history(finished)
        assert step["status"] == "complete"
        assert json.loads(step["provider_result"])["value"] == "b"
        # The model answered from the approved call's output.
        after_approval = provider.requests[1]
        assert [result.call_id for result in after_approval.tool_results] == ["call-1"]
        assert "b" in json.dumps(after_approval.tool_results[0].output)
        await service.shutdown()

    asyncio.run(scenario())


def test_non_streaming_approval_pause_frees_the_turn_for_its_resume(tmp_path):
    """complete() lets the stream park the turn, so the resume can claim it."""

    async def scenario():
        store, service, prepared, provider, broker = _approval_turn(
            tmp_path, _approval_script()
        )
        with pytest.raises(ChatError, match="waiting for operator approval"):
            await service.complete(prepared)
        paused = store.get(ChatTurn, "turn")
        assert paused.status == ChatTurnStatus.WAITING_APPROVAL
        assert paused.execution_claim_id is None
        assert paused.execution_owner_id is None
        _approve(store)

        prepared.turn = paused
        prepared.execution_claim_id = None
        service.start_provider_turn(prepared)
        resumed = [event async for event, _ in service.follow_provider_turn("turn")]
        assert resumed[-1] == "done"
        assert broker.approved_runs == 1
        assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
        await service.shutdown()

    asyncio.run(scenario())


def test_turn_resumed_while_it_settles_is_admitted_again(tmp_path):
    """A settle that resumes the same turn never queues behind the finished run."""

    async def scenario():
        store, service, prepared, provider, broker = _approval_turn(
            tmp_path, _approval_script()
        )
        settled = service.subagents.turn_settled
        resumes: list[str] = []

        async def resume_while_settling(turn_id: str) -> None:
            # What a satisfied subagent wait or a closed child question does
            # from inside the settle of the run that parked the turn.
            if not resumes:
                _approve(store)
                prepared.turn = store.get(ChatTurn, turn_id)
                prepared.execution_claim_id = None
                resumes.append(service.start_provider_turn(prepared))
            await settled(turn_id)

        service.subagents.turn_settled = resume_while_settling  # type: ignore[method-assign]
        turn_id = service.start_provider_turn(prepared)
        first = service._active_provider_turns[turn_id]
        await _drain(service, turn_id)
        assert first.task is not None
        await first.task
        assert resumes == [turn_id]
        second = service._active_provider_turns[turn_id]
        assert second is not first
        assert second.task is not None
        await second.task
        assert second.error is None
        assert broker.approved_runs == 1
        assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.COMPLETE
        assert _queue_state(store, turn_id) == "complete"
        assert service.provider_scheduler.metrics()["active"] == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_final_answer_retry_resumes_synthesis_through_admission(tmp_path):
    """An operator's "Finish the answer" retry goes straight to synthesis."""

    async def scenario():
        async def reasoning_only(request):
            del request
            return _subagent_response(text="")

        provider = RoutedProvider(
            parent=[
                _finish("p1"),
                reasoning_only,
                reasoning_only,
                _subagent_response(text="Recovered answer."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Answer me.", allow_subagents=True)
        )
        turn_id = chat.start_provider_turn(prepared)
        with pytest.raises(ProviderResponseError, match="no operator-facing answer"):
            await _drain(chat, turn_id)
        failed = store.get(ChatTurn, turn_id)
        assert failed.status == ChatTurnStatus.FAILED
        assert failed.request_snapshot["final_answer_recovery"]["attempts"] >= 2
        before = len(provider.parent_requests)

        chat.start_provider_turn(chat.prepare_resume(turn_id))
        events = await _drain(chat, turn_id)

        assert events[-1] == "done"
        retried = provider.parent_requests[before:]
        assert len(retried) == 1
        assert retried[0].tool_choice == ToolChoice.NONE
        completed = store.get(ChatTurn, turn_id)
        assert completed.status == ChatTurnStatus.COMPLETE
        assert completed.final_message_id is not None
        await chat.shutdown()

    asyncio.run(scenario())


def test_startup_closes_the_admission_of_a_turn_that_no_longer_exists(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        missing = ChatTurn(
            id="deleted-turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.QUEUED,
            queued_at=utc_now(),
            capacity_lane="background",
        )
        ProviderScheduler(store, worker_id="worker").enqueue(missing)

        service = ChatService(store, worker_id="restarted")
        await service.startup()

        assert _queue_state(store, missing.id) == "cancelled"
        assert service.provider_scheduler.recover() == []
        await service.shutdown()

    asyncio.run(scenario())


def test_queued_turn_restore_skips_an_unreadable_turn(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        corrupt = store.create(
            ChatTurn(
                id="corrupt-turn",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        missing = ChatTurn(
            id="deleted-turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.QUEUED,
            queued_at=utc_now(),
        )
        scheduler = ProviderScheduler(store, worker_id="worker")
        scheduler.enqueue(corrupt)
        scheduler.enqueue(missing)
        with store.database.session() as database_session:
            database_session.execute(
                update(EntityRow)
                .where(EntityRow.id == corrupt.id)
                .values(payload={"status": "not-a-status"})
            )

        service = ChatService(store, worker_id="restarted")
        service._restore_queued_turns()

        # The unreadable turn stays queued for a repaired record; the one
        # that is gone is closed; neither stops the other.
        assert _queue_state(store, corrupt.id) == "queued"
        assert _queue_state(store, missing.id) == "cancelled"
        await service.shutdown()

    asyncio.run(scenario())


def test_startup_restores_a_resume_the_previous_core_never_admitted(tmp_path):
    """An approval resume still waiting for capacity survives a Core restart."""

    async def scenario():
        store, service, prepared, provider, broker = _approval_turn(
            tmp_path, _approval_script()
        )
        turn_id = service.start_provider_turn(prepared)
        await _drain(service, turn_id)
        paused = store.get(ChatTurn, turn_id)
        assert paused.status == ChatTurnStatus.WAITING_APPROVAL
        _approve(store)
        # The resume was accepted, then Core stopped before admitting it.
        service.provider_scheduler.enqueue(paused)
        await service.shutdown()

        restarted = ChatService(store, worker_id="restarted")
        restarted.prepare_resume = lambda _turn_id: prepared  # type: ignore[method-assign]
        prepared.turn = store.get(ChatTurn, turn_id)
        prepared.execution_claim_id = None
        await restarted.startup()
        assert restarted.has_active_provider_turn(turn_id)
        events = await _drain(restarted, turn_id)
        assert events[-1] == "done"
        assert broker.approved_runs == 1
        assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.COMPLETE
        await restarted.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "recovery", "refused"),
    [
        (ChatTurnStatus.QUEUED, None, True),
        (ChatTurnStatus.INTERRUPTED, {"required": True}, True),
        (ChatTurnStatus.INTERRUPTED, {"automatic_retry_pending": True}, True),
        # Reviewed or settled recovery: nothing in the UI could stop it.
        (ChatTurnStatus.INTERRUPTED, {"required": False}, False),
    ],
)
def test_conversation_delete_waits_for_a_queued_or_recovering_turn(
    tmp_path, status, recovery, refused
):
    store, project, profile, session = _store(tmp_path)
    turn = store.create(
        ChatTurn(
            id="unfinished",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=status,
            queued_at=utc_now(),
            request_snapshot={"recovery": recovery} if recovery else {},
        )
    )
    ProviderScheduler(store, worker_id="worker").enqueue(turn)

    if refused:
        with pytest.raises(ConflictError, match="response is active"):
            store.delete_chat_session(session.id)
        assert store.get(ChatTurn, turn.id).id == turn.id
    else:
        store.delete_chat_session(session.id)
        assert _queue_state(store, turn.id) is None


def test_conversation_delete_removes_its_turns_admission_rows(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        turn = store.create(
            ChatTurn(
                id="finished",
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        scheduler = ProviderScheduler(store, worker_id="worker")
        scheduler.enqueue(turn)
        admission = await scheduler.admit(turn.id)
        latest = store.get(ChatTurn, turn.id)
        store.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=latest.revision,
        )
        await admission.release(turn.id)
        assert _queue_state(store, turn.id) == "complete"

        store.delete_chat_session(session.id)

        assert _queue_state(store, turn.id) is None
        restarted = ChatService(store, worker_id="restarted")
        await restarted.startup()
        await restarted.shutdown()

    asyncio.run(scenario())


def _admission(
    store: NebulaStore,
    project,
    profile,
    session,
    turn_id: str,
    turn_status: ChatTurnStatus | None,
    state: str,
) -> None:
    """A turn in ``turn_status`` (None: deleted) whose admission is ``state``."""

    if turn_status is not None:
        store.create(
            ChatTurn(
                id=turn_id,
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=turn_status,
                queued_at=utc_now(),
                capacity_lane="background",
            )
        )
    with store.database.session() as database_session:
        database_session.add(
            ProviderTurnQueueRow(
                turn_id=turn_id,
                lane="background",
                state=state,
                accepted_at=utc_now(),
                admitted_at=None if state == "queued" else utc_now(),
                completed_at=utc_now() if state == "parked" else None,
                lease_owner="dead-worker" if state == "running" else None,
                lease_expires_at=(
                    utc_now() + timedelta(hours=1) if state == "running" else None
                ),
            )
        )


# (turn status, admission state) -> the admission state after it settles.
_SETTLED_ADMISSIONS = {
    # The live case: the operator stopped a turn parked on a subagent wait.
    (ChatTurnStatus.CANCELLED, "parked"): "cancelled",
    (ChatTurnStatus.FAILED, "parked"): "complete",
    (ChatTurnStatus.COMPLETE, "parked"): "complete",
    # Ended before admission; the queue position counted it until a restart.
    (ChatTurnStatus.CANCELLED, "queued"): "cancelled",
    (ChatTurnStatus.FAILED, "queued"): "complete",
    (None, "parked"): "cancelled",
    (None, "queued"): "cancelled",
    # Turns that can still resume keep their admission.
    (ChatTurnStatus.WAITING_APPROVAL, "parked"): "parked",
    (ChatTurnStatus.WAITING_CALLBACK, "parked"): "parked",
    (ChatTurnStatus.INTERRUPTED, "parked"): "parked",
    (ChatTurnStatus.QUEUED, "queued"): "queued",
    # Admission release owns a running admission, and a closed one stays so.
    (ChatTurnStatus.CANCELLED, "running"): "running",
    (ChatTurnStatus.CANCELLED, "complete"): "complete",
}


def test_settle_closes_only_the_admissions_of_ended_turns(tmp_path):
    store, project, profile, session = _store(tmp_path)
    ids = {}
    for index, (status, state) in enumerate(_SETTLED_ADMISSIONS):
        ids[status, state] = f"turn-{index:02d}"
        _admission(store, project, profile, session, ids[status, state], status, state)
    _admission(
        store,
        project,
        profile,
        session,
        "unreadable",
        ChatTurnStatus.CANCELLED,
        "parked",
    )
    with store.database.session() as database_session:
        database_session.execute(
            update(EntityRow)
            .where(EntityRow.id == "unreadable")
            .values(payload={"status": "not-a-status"})
        )
    scheduler = ProviderScheduler(store, worker_id="worker")

    settled = scheduler.settle()

    assert sorted(settled) == sorted(
        ids[key] for key, expected in _SETTLED_ADMISSIONS.items() if expected != key[1]
    )
    for key, expected in _SETTLED_ADMISSIONS.items():
        assert _queue_state(store, ids[key]) == expected, key
    # A repaired record may still resume; its admission waits for it.
    assert _queue_state(store, "unreadable") == "parked"
    with store.database.session() as database_session:
        closed = database_session.get(
            ProviderTurnQueueRow, ids[ChatTurnStatus.CANCELLED, "queued"]
        )
        assert closed is not None and closed.completed_at is not None
    assert scheduler.metrics()["queued"] == 1
    assert scheduler.settle() == []
    assert scheduler.settle(ids[ChatTurnStatus.WAITING_CALLBACK, "parked"]) == []


def test_restart_keeps_the_admission_of_a_resumable_turn_parked(tmp_path):
    """A lease the stopped Core held ends as its turn now stands."""

    store, project, profile, session = _store(tmp_path)
    for turn_id, status in (
        ("waiting", ChatTurnStatus.WAITING_CALLBACK),
        ("interrupted", ChatTurnStatus.INTERRUPTED),
        ("finished", ChatTurnStatus.COMPLETE),
        ("stopped", ChatTurnStatus.CANCELLED),
    ):
        _admission(store, project, profile, session, turn_id, status, "running")
    _admission(store, project, profile, session, "deleted", None, "running")

    assert ProviderScheduler(store, worker_id="restarted").recover() == []

    assert _queue_state(store, "waiting") == "parked"
    assert _queue_state(store, "interrupted") == "parked"
    assert _queue_state(store, "finished") == "complete"
    assert _queue_state(store, "stopped") == "cancelled"
    assert _queue_state(store, "deleted") == "cancelled"


@pytest.mark.parametrize("stop", ["operator", "approval_decision"])
def test_stopping_a_parked_turn_closes_its_admission(tmp_path, stop):
    """Stopping a waiting turn ends it without admission release."""

    async def scenario():
        store, service, prepared, provider, broker = _approval_turn(
            tmp_path, _approval_script()
        )
        turn_id = service.start_provider_turn(prepared)
        await _drain(service, turn_id)
        assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.WAITING_APPROVAL
        assert _queue_state(store, turn_id) == "parked"

        if stop == "operator":
            await service.stop_provider_turn(turn_id)
        else:
            # POST /approvals/{id}/decision with "stop" cancels the turn.
            service.cancel_turn(turn_id)

        assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.CANCELLED
        assert _queue_state(store, turn_id) == "cancelled"
        assert broker.approved_runs == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_a_parked_wait_that_cannot_resume_closes_its_admission(tmp_path):
    async def scenario():
        store, project, profile, session = _store(tmp_path)
        _admission(
            store,
            project,
            profile,
            session,
            "waiting",
            ChatTurnStatus.WAITING_CALLBACK,
            "parked",
        )
        service = ChatService(store, worker_id="worker")

        await service.subagents._fail_unresumable(
            store.get(ChatTurn, "waiting"), RuntimeError("resume failed")
        )

        assert store.get(ChatTurn, "waiting").status == ChatTurnStatus.FAILED
        assert _queue_state(store, "waiting") == "complete"
        await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["startup", "recovery_tick"])
def test_recovery_closes_admissions_that_ended_turns_left_open(
    tmp_path, monkeypatch, entry
):
    off_loop: list[str] = []
    to_thread = asyncio.to_thread

    async def recording_to_thread(function, *args, **kwargs):
        off_loop.append(getattr(function, "__name__", ""))
        return await to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

    async def scenario():
        store, project, profile, session = _store(tmp_path)
        _admission(
            store,
            project,
            profile,
            session,
            "stopped",
            ChatTurnStatus.CANCELLED,
            "parked",
        )
        _admission(
            store,
            project,
            profile,
            session,
            "never-admitted",
            ChatTurnStatus.CANCELLED,
            "queued",
        )
        _admission(
            store,
            project,
            profile,
            session,
            "awaiting-approval",
            ChatTurnStatus.WAITING_APPROVAL,
            "parked",
        )
        service = ChatService(store, worker_id="restarted")

        if entry == "startup":
            await service.startup()
        else:
            await service.recovery_tick()

        assert _queue_state(store, "stopped") == "cancelled"
        assert _queue_state(store, "never-admitted") == "cancelled"
        assert _queue_state(store, "awaiting-approval") == "parked"
        assert service.provider_scheduler.metrics()["queued"] == 0
        assert service.provider_scheduler.recover() == []
        # The scan ran off the event loop.
        assert "settle" in off_loop
        await service.shutdown()

    asyncio.run(scenario())
