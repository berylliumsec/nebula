"""Restart, shutdown, callback and goal recovery keep durable work and keep goals moving."""

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.automation_runtime import ProcessResultsRequest, RunCommandRequest
from nebula.v3.chat import ChatCompletionRequest, ChatHistoryConflict, ChatService
from nebula.v3.chat_schedules import ChatScheduleService, ScheduleCreate
from nebula.v3.domain import (
    AutomationApprovalPolicy,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    ChatTurnStatus,
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.providers import ModelRequest
from nebula.v3.providers import ToolCall as ModelToolCall
from nebula.v3.storage import NebulaStore
from tests.v3.test_automation_runtime import runtime
from tests.v3.test_chat import FakeProvider, _profile


def _model_request() -> dict:
    return ModelRequest(
        model="model-a", messages=[{"role": "user", "content": "Continue."}]
    ).model_dump(mode="json")


def _conversation(tmp_path, name: str = "recovery"):
    store = NebulaStore(tmp_path / f"{name}.db")
    engagement = store.create(Engagement(name="Recovery"))
    profile = store.create(_profile(local=True))
    session = store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Recovery",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    return store, engagement, profile, session


def _running_goal(store, engagement, session, **fields) -> ChatGoal:
    return store.create(
        ChatGoal(
            engagement_id=engagement.id,
            session_id=session.id,
            objective="Keep going",
            completion_criteria=["done"],
            status=ChatGoalStatus.RUNNING,
            **fields,
        )
    )


def _tool_call(store, turn, call_id, name, status, step, *, result=None, **fields):
    return store.create(
        ToolCall(
            id=call_id,
            engagement_id=turn.engagement_id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=turn.session_id,
            chat_turn_id=turn.id,
            tool_name=name,
            status=status,
            risk_class=RiskClass.LOCAL_READ,
            arguments={"artifact_id": f"artifact-{step}"},
            result=result,
            started_at=utc_now(),
            metadata={
                "provider_call_id": f"provider-{step}",
                "provider_step": step,
                "budget_class": "artifact_query",
            },
            **fields,
        )
    )


def _entry(call: ToolCall, status: str, **fields) -> dict:
    return {
        "step": call.metadata["provider_step"],
        "model_call_id": call.metadata["provider_call_id"],
        "tool_call_id": call.id,
        "name": call.tool_name,
        "arguments": call.arguments,
        "budget_class": "artifact_query",
        "status": status,
        **fields,
    }


def _turn_with_projected_reads(tmp_path):
    """A routing turn: two projected reads, one projected failure, and open calls."""

    store, engagement, profile, session = _conversation(tmp_path)
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            tools_enabled=True,
            next_step=3,
            queued_at=utc_now(),
            request_snapshot={"model_request": _model_request(), "context_usage": {}},
        )
    )
    service = ChatService(store)
    projected = []
    for step, (name, status, entry_status) in enumerate(
        (
            ("tool_output.read", ToolCallStatus.COMPLETE, "complete"),
            ("tool_output.read", ToolCallStatus.COMPLETE, "complete"),
            ("workspace.read", ToolCallStatus.FAILED, "failed"),
        )
    ):
        # Core reads store their plain output, not a v2 receipt, so read
        # repair cannot adopt them; the projected step is their only record.
        call = _tool_call(
            store,
            turn,
            f"read-{step}",
            name,
            status,
            step,
            result={"content": f"line {step} of the real output " * 40},
        )
        service.turn_ledger.append(turn.id, _entry(call, "running"))
        service.turn_ledger.append(
            turn.id,
            _entry(
                call,
                entry_status,
                provider_result=f"real output {step}",
                result_summary=f"read {step}",
            ),
        )
        projected.append(call)
    command = _tool_call(
        store, turn, "command-open", "run_command", ToolCallStatus.RUNNING, 3
    )
    service.turn_ledger.append(turn.id, _entry(command, "running"))
    read = _tool_call(
        store, turn, "read-open", "tool_output.read", ToolCallStatus.RUNNING, 4
    )
    service.turn_ledger.append(turn.id, _entry(read, "running"))
    return store, session, turn, projected, command, read


def test_restart_keeps_projected_results_and_marks_only_the_open_effect_unknown(
    tmp_path,
):
    store, session, turn, projected, command, read = _turn_with_projected_reads(
        tmp_path
    )
    service = ChatService(store)
    before = {item["tool_call_id"]: item for item in service._turn_history(turn)}

    asyncio.run(service.startup())

    interrupted = store.get(ChatTurn, turn.id)
    recovery = interrupted.request_snapshot["recovery"]
    assert interrupted.status == ChatTurnStatus.INTERRUPTED
    assert recovery["unknown_tool_call_ids"] == [command.id]
    assert recovery["rerunnable_tool_call_ids"] == [read.id]

    service.prepare_resume = lambda turn_id: turn_id  # type: ignore[method-assign]
    service.start_provider_turn = lambda prepared, **_: prepared  # type: ignore[method-assign]
    assert service.resume_turns_stopped_by_core() == [turn.id]

    recovered = store.get(ChatTurn, turn.id)
    history = {item["tool_call_id"]: item for item in service._turn_history(recovered)}
    for call in projected:
        # The results saved before the restart stay the model's record.
        assert history[call.id] == before[call.id]
    assert history[command.id]["recovered_from_restart_unknown"] is True
    assert history[read.id]["recovered_from_restart_rerunnable"] is True
    recovery = recovered.request_snapshot["recovery"]
    assert recovery["auto_continued_unknown_tool_call_ids"] == [command.id]
    assert recovery["auto_rerunnable_tool_call_ids"] == [read.id]
    assert all(call.id not in recovery["automatic_note"] for call in projected)
    assert recovered.next_step == 5
    # The unknown command is refused on replay; the interrupted read is not.
    assert chat_module._replays_restart_unknown(
        list(history.values()),
        ModelToolCall(id="again", name="run_command", arguments=command.arguments),
    )
    assert not chat_module._replays_restart_unknown(
        list(history.values()),
        ModelToolCall(id="again", name="tool_output.read", arguments=read.arguments),
    )


def test_recovery_of_an_old_snapshot_never_overwrites_a_projected_result(tmp_path):
    store, session, turn, projected, command, read = _turn_with_projected_reads(
        tmp_path
    )
    service = ChatService(store)
    before = {item["tool_call_id"]: item for item in service._turn_history(turn)}
    # Snapshots written by earlier releases listed every call of the turn.
    store.update(
        ChatTurn,
        turn.id,
        {
            "status": ChatTurnStatus.INTERRUPTED,
            "request_snapshot": {
                **turn.request_snapshot,
                "recovery": {
                    "required": True,
                    "cause": "core_restart",
                    "unknown_tool_call_ids": [
                        *(call.id for call in projected),
                        command.id,
                        read.id,
                    ],
                    "unknown_hook_execution_ids": [],
                },
            },
        },
        expected_revision=turn.revision,
    )

    recovered = service._auto_reconcile_restart_uncertainty(turn.id)

    history = {item["tool_call_id"]: item for item in service._turn_history(recovered)}
    for call in projected:
        assert history[call.id] == before[call.id]
    assert history[command.id]["recovered_from_restart_unknown"] is True
    assert history[read.id]["recovered_from_restart_rerunnable"] is True
    recovery = recovered.request_snapshot["recovery"]
    assert recovery["auto_continued_unknown_tool_call_ids"] == [command.id]
    assert recovery["auto_rerunnable_tool_call_ids"] == [read.id]


def test_recovered_goal_turn_continues_the_goal(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "goal")
    goal = _running_goal(store, engagement, session)
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.INTERRUPTED,
            error="Core stopped before this response completed. Core will resume it automatically.",
            request_snapshot={
                "model_request": _model_request(),
                "context_usage": {},
                "recovery": {
                    "required": True,
                    "cause": "core_shutdown",
                    "unknown_tool_call_ids": [],
                },
            },
        )
    )
    service = ChatService(
        store, provider_factory=lambda _: FakeProvider(profile.id, local=True)
    )
    continued: list = []

    async def scenario():
        await service.startup()
        original_start = service.start_provider_turn

        def start(prepared, **kwargs):
            if prepared.turn.id != turn.id:
                # The goal's next turn; recorded instead of run.
                continued.append(prepared)
                return prepared.turn.id
            return original_start(prepared, **kwargs)

        service.start_provider_turn = start  # type: ignore[method-assign]
        assert service.resume_turns_stopped_by_core() == [turn.id]
        for _ in range(200):
            await asyncio.sleep(0.02)
            if continued:
                break
        await service.shutdown()

    asyncio.run(scenario())

    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.COMPLETE
    assert len(continued) == 1
    assert continued[0].turn.goal_id == goal.id
    assert continued[0].turn.id != turn.id


def test_idle_running_goal_continues_after_two_idle_passes(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "idle-goal")
    goal = _running_goal(store, engagement, session)
    store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
            request_snapshot={"model_request": _model_request()},
        )
    )
    service = ChatService(
        store, provider_factory=lambda _: FakeProvider(profile.id, local=True)
    )
    started: list = []
    service.start_provider_turn = lambda prepared, **_: (
        started.append(prepared) or prepared.turn.id
    )  # type: ignore[method-assign]

    first = asyncio.run(service.reconcile_idle_running_goals())
    second = asyncio.run(service.reconcile_idle_running_goals())

    # One pass may race a continuation still being prepared; two agree.
    assert first == []
    assert second == [goal.id]
    assert len(started) == 1
    assert started[0].turn.goal_id == goal.id
    assert store.get(ChatGoal, goal.id).status == ChatGoalStatus.RUNNING


def test_idle_running_goal_after_a_failed_turn_pauses_with_the_reason(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "failed-goal")
    goal = _running_goal(store, engagement, session)
    store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.FAILED,
            request_snapshot={"model_request": _model_request()},
        )
    )
    service = ChatService(store)

    for _ in range(2):
        asyncio.run(service.reconcile_idle_running_goals())

    paused = store.get(ChatGoal, goal.id)
    assert paused.status == ChatGoalStatus.PAUSED
    assert "ended failed" in (paused.blocked_reason or "")


def test_running_goal_waiting_for_a_subagent_is_left_running(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "child-goal")
    goal = _running_goal(store, engagement, session)
    parent = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
            request_snapshot={"model_request": _model_request()},
        )
    )
    store.create(
        ChatSubagent(
            engagement_id=engagement.id,
            parent_session_id=session.id,
            parent_turn_id=parent.id,
            child_session_id="child-session",
            name="worker",
            task="work",
        )
    )
    service = ChatService(store)
    service.start_provider_turn = lambda prepared, **_: pytest.fail("dispatched")  # type: ignore[method-assign]

    for _ in range(3):
        assert asyncio.run(service.reconcile_idle_running_goals()) == []

    assert store.get(ChatGoal, goal.id).status == ChatGoalStatus.RUNNING


def test_failed_parent_resume_pauses_its_goal(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "parent-resume")
    goal = _running_goal(store, engagement, session)
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.WAITING_CALLBACK,
            request_snapshot={"model_request": _model_request()},
        )
    )
    service = ChatService(store)

    asyncio.run(
        service.subagents._fail_unresumable(
            turn,
            ChatHistoryConflict(
                "automation runtime or scope changed while the response was paused"
            ),
        )
    )

    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.FAILED
    paused = store.get(ChatGoal, goal.id)
    assert paused.status == ChatGoalStatus.PAUSED
    assert "could not resume" in (paused.blocked_reason or "")


def _callback_wait(tmp_path, manager, store, engagement):
    """A provider turn waiting for a running background command's callback."""

    manager.callback_origin = "http://10.0.0.8:8765"
    policy = manager.project_policy(engagement.id)
    manager.update_project_policy(
        engagement.id,
        approval_policy=AutomationApprovalPolicy.NEVER,
        network_enabled=True,
        runner_profile_id="runner",
        max_timeout_ms=30_000,
        expected_revision=policy.revision,
    )
    profile = store.create(
        ProviderProfile(
            id="p",
            name="Local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    chat = ChatService(
        store,
        provider_factory=lambda _: FakeProvider(profile.id, local=True),
        workspace_resolver=lambda _: tmp_path / "w" / engagement.id,
    )
    session = store.create(
        ChatSession(
            id="s",
            engagement_id=engagement.id,
            title="x",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    turn = store.create(
        ChatTurn(
            id="t",
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.WAITING_CALLBACK,
            request_snapshot={"model_request": {"model": "model-a", "messages": []}},
        )
    )
    call = store.create(
        ToolCall(
            id="c",
            engagement_id=engagement.id,
            run_id=turn.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=session.id,
            chat_turn_id=turn.id,
            tool_name="run_command",
            status=ToolCallStatus.RUNNING,
            risk_class=RiskClass.ACTIVE_SCAN,
            arguments={"command": "wait-forever", "background": True},
            started_at=utc_now(),
        )
    )
    return chat, turn, call


async def _start_background(manager, engagement, turn, call):
    started = await manager.run_command(
        engagement_id=engagement.id,
        owner_kind="chat",
        owner_id=turn.session_id,
        request=RunCommandRequest(command="wait-forever", background=True),
        tool_call_id=call.id,
        chat_session_id=turn.session_id,
        chat_turn_id=turn.id,
    )
    return started


def _park_on_callback(store, turn, call, started) -> None:
    store.update(
        ChatTurn,
        turn.id,
        {
            "tool_history": [
                {
                    "step": 0,
                    "model_call_id": "m",
                    "tool_call_id": call.id,
                    "name": "run_command",
                    "status": "waiting_callback",
                    "process_id": started.process_id,
                    "results_url": started.results_url,
                    "arguments": {"command": "wait-forever", "background": True},
                }
            ]
        },
        expected_revision=store.get(ChatTurn, turn.id).revision,
    )


def test_core_shutdown_interrupts_background_commands_without_waking_owners(
    tmp_path,
):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        manager.callback_origin = "http://10.0.0.8:8765"
        policy = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=True,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=policy.revision,
        )
        notified: list[str] = []
        manager.bind_process_terminal_observer(notified.append)
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id="s",
            request=RunCommandRequest(command="wait-forever", background=True),
            tool_call_id="tool-1",
            chat_session_id="s",
            chat_turn_id="t",
        )
        await manager.shutdown()
        execution = store.get(
            CommandExecution, manager._execution_id(started.process_id)
        )
        # The state a crash leaves after the next startup; nobody is woken
        # while Core tears down.
        assert execution.status == CommandExecutionStatus.INTERRUPTED
        assert notified == []

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["graceful", "crash"])
def test_interrupted_callback_producer_releases_the_wait_as_unknown(tmp_path, stop):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        chat, turn, call = _callback_wait(tmp_path, manager, store, engagement)
        started = await _start_background(manager, engagement, turn, call)
        _park_on_callback(store, turn, call, started)
        execution_id = manager._execution_id(started.process_id)
        if stop == "graceful":
            await manager.shutdown()
        else:
            # What AutomationRuntimeManager.startup() writes after a crash.
            execution = store.get(CommandExecution, execution_id)
            store.update(
                CommandExecution,
                execution.id,
                {
                    "status": "interrupted",
                    "completed_at": utc_now(),
                    "error": "Core restarted before the process completed",
                },
                expected_revision=execution.revision,
            )
        chat.prepare_resume = lambda turn_id: turn_id  # type: ignore[method-assign]
        chat.start_provider_turn = lambda prepared, **_: prepared  # type: ignore[method-assign]

        passes = [chat.reconcile_waiting_callbacks() for _ in range(3)]

        assert passes == [[turn.id], [], []]
        latest = store.get(ChatTurn, turn.id)
        assert latest.status == ChatTurnStatus.ROUTING
        settled = store.get(ToolCall, call.id)
        assert settled.status == ToolCallStatus.FAILED
        assert isinstance(settled.result, dict)
        assert settled.result["category"] == "missing_callback"
        assert settled.result["side_effects"] == "unknown"
        assert settled.result["retry_safe"] is False
        assert chat._turn_history(latest)[-1]["status"] == "failed"

        # A receipt that still arrives attaches to the settled call only.
        manager.accept_results(
            started.process_id,
            started.results_api_key,
            ProcessResultsRequest(status="complete", summary="late"),
        )
        assert chat.continue_after_tool_callback(started.process_id) is None
        late = store.get(ToolCall, call.id)
        assert late.status == ToolCallStatus.FAILED
        assert late.metadata["late_callback_receipt"]["summary"] == "late"
        assert store.get(ChatTurn, turn.id).revision == latest.revision
        await chat.shutdown()

    asyncio.run(scenario())


class _BlockingProvider(FakeProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()

    async def complete(self, request):
        if request.metadata.get("operation") == "conversation_naming":
            return await super().complete(request)
        self.entered.set()
        await asyncio.Event().wait()


def test_core_shutdown_parks_a_complete_driven_turn_for_recovery(tmp_path):
    async def scenario():
        store, engagement, profile, session = _conversation(tmp_path, "complete")
        provider = _BlockingProvider(profile.id, local=True)
        service = ChatService(store, provider_factory=lambda _: provider)
        prepared = await service.prepare_async(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=engagement.id,
                session_id=session.id,
                model="model-a",
                messages=[{"role": "user", "content": "hi"}],
                include_knowledge=False,
                stream=True,
            )
        )
        task = asyncio.create_task(service.complete(prepared))
        await asyncio.wait_for(provider.entered.wait(), 5)
        # Core's stop order cancels the caller before provider chat shuts down.
        task.cancel()
        await service.shutdown()
        await asyncio.gather(task, return_exceptions=True)
        turn = store.get(ChatTurn, prepared.turn.id)
        assert turn.status == ChatTurnStatus.INTERRUPTED
        assert turn.request_snapshot["recovery"]["cause"] == "core_shutdown"

    asyncio.run(scenario())


def test_operator_stop_terminates_the_background_command_holding_its_callback(
    tmp_path,
):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        chat, turn, call = _callback_wait(tmp_path, manager, store, engagement)
        chat.automation_tool_platform = SimpleNamespace(manager=manager)  # type: ignore[assignment]
        started = await _start_background(manager, engagement, turn, call)
        _park_on_callback(store, turn, call, started)

        stopped = await chat.stop_provider_turn(turn.id)

        assert stopped.status == ChatTurnStatus.CANCELLED
        assert store.get(ToolCall, call.id).status == ToolCallStatus.CANCELLED
        execution = store.get(
            CommandExecution, manager._execution_id(started.process_id)
        )
        assert execution.status == CommandExecutionStatus.CANCELLED
        await manager.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


def test_approved_call_whose_turn_ended_is_settled_without_running(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "approved")
    chat_turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            backend="harness",
            harness_turn_id="harness-turn",
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
        )
    )
    store.create(
        HarnessTurn(
            id="harness-turn",
            engagement_id=engagement.id,
            harness_session_id="harness-session",
            origin=HarnessTurnOrigin.CHAT,
            chat_session_id=session.id,
            chat_turn_id=chat_turn.id,
            status=HarnessTurnStatus.COMPLETE,
            prompt="work",
        )
    )
    live_turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.WAITING_APPROVAL,
        )
    )

    def approved(call_id: str, owner: ChatTurn, *, age: timedelta, **metadata):
        changed = utc_now() - age
        return store.create(
            ToolCall(
                id=call_id,
                created_at=changed,
                updated_at=changed,
                engagement_id=engagement.id,
                run_id=owner.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=session.id,
                chat_turn_id=owner.id,
                tool_name="session/request_permission",
                status=ToolCallStatus.APPROVED,
                risk_class=RiskClass.ACTIVE_SCAN,
                metadata=metadata,
            )
        )

    hours = timedelta(hours=7)
    stale = approved("stale", chat_turn, age=hours, harness_turn_id="harness-turn")
    fresh = approved(
        "fresh", chat_turn, age=timedelta(seconds=5), harness_turn_id="harness-turn"
    )
    live = approved("live", live_turn, age=hours)
    service = ChatService(store)

    assert service.reconcile_stale_approved_tool_calls() == [stale.id]
    assert service.reconcile_stale_approved_tool_calls() == []

    settled = store.get(ToolCall, stale.id)
    assert settled.status == ToolCallStatus.CANCELLED
    evidence = settled.metadata["settled_without_start"]
    assert evidence["effect"] == "unknown"
    assert evidence["owners"] == {
        f"chat_turn:{chat_turn.id}": "complete",
        "harness_turn:harness-turn": "complete",
    }
    assert store.get(ToolCall, fresh.id).status == ToolCallStatus.APPROVED
    assert store.get(ToolCall, live.id).status == ToolCallStatus.APPROVED


def test_recovery_tick_reads_only_turns_that_can_need_recovery(tmp_path, monkeypatch):
    store, engagement, profile, session = _conversation(tmp_path, "tick")
    service = ChatService(store)
    for index in range(12):
        settled = store.create(
            ChatTurn(
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
                tools_enabled=True,
                queued_at=utc_now(),
            )
        )
        service.turn_ledger.append(
            settled.id,
            {
                "step": 0,
                "model_call_id": f"m-{index}",
                "tool_call_id": f"done-{index}",
                "name": "tool_output.read",
                "status": "complete",
                "provider_result": "x" * 4_000,
            },
        )
    # A settled turn whose call Core never closed (a restart-unknown effect)
    # is read once, then not again until it changes.
    leftover = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
            tools_enabled=True,
            queued_at=utc_now(),
        )
    )
    _tool_call(store, leftover, "leftover", "run_command", ToolCallStatus.RUNNING, 0)
    waiting = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.WAITING_CALLBACK,
            tools_enabled=True,
            queued_at=utc_now(),
        )
    )
    service.turn_ledger.append(
        waiting.id,
        {
            "step": 0,
            "model_call_id": "m-wait",
            "tool_call_id": "wait",
            "name": "run_command",
            "status": "waiting_callback",
            "process_id": "process-still-running",
        },
    )
    histories: list[str] = []
    scan_threads: set[str] = set()
    read_history = service.turn_ledger.history
    find_entities = store.find_entities

    def history(turn):
        histories.append(turn.id)
        return read_history(turn)

    def find(model, filters, **kwargs):
        if model is ChatTurn:
            scan_threads.add(threading.current_thread().name)
        return find_entities(model, filters, **kwargs)

    def full_scan(model, *args, **kwargs):
        if model is ChatTurn:
            pytest.fail("the recovery tick paged every chat turn")
        return NebulaStore.list_entities(store, model, *args, **kwargs)

    monkeypatch.setattr(service.turn_ledger, "history", history)
    monkeypatch.setattr(store, "find_entities", find)
    monkeypatch.setattr(store, "list_entities", full_scan)

    async def ticks():
        await service.recovery_tick()
        await service.recovery_tick()

    asyncio.run(ticks())

    assert sorted(set(histories)) == sorted({waiting.id, leftover.id})
    assert histories.count(leftover.id) == 1
    assert scan_threads and threading.main_thread().name not in scan_threads


def test_due_schedule_is_dispatched_without_holding_the_recovery_tick(tmp_path):
    async def scenario():
        store, engagement, profile, session = _conversation(tmp_path, "schedule")
        _running_goal(store, engagement, session)
        schedule = ChatScheduleService(store).create(
            session.id, ScheduleCreate(interval_seconds=3_600)
        )
        store.update(
            type(schedule),
            schedule.id,
            {"next_run_at": utc_now() - timedelta(seconds=1)},
            expected_revision=schedule.revision,
        )
        provider = _BlockingProvider(profile.id, local=True)
        service = ChatService(store, provider_factory=lambda _: provider)

        await asyncio.wait_for(service.fire_due_schedules(), 5)
        await asyncio.wait_for(provider.entered.wait(), 5)

        latest = ChatScheduleService(store).get(session.id)
        assert latest.last_status == "started"
        assert latest.last_turn_id is not None
        assert service.has_active_provider_turn(latest.last_turn_id)
        # The tick is free for restart and callback recovery meanwhile.
        await asyncio.wait_for(service.recovery_tick(), 5)
        await service.stop_provider_turn(latest.last_turn_id)
        await asyncio.sleep(0)
        assert ChatScheduleService(store).get(session.id).last_status == "cancelled"
        await service.shutdown()

    asyncio.run(scenario())


def test_foreground_command_cancelled_while_core_stops_is_interrupted(tmp_path):
    """Owners cancel foreground commands before the runtime itself shuts down.

    A command cancelled once Core began stopping ends interrupted, the state
    a crash leaves; the same cancellation while Core runs stays an operator or
    caller decision.
    """

    async def scenario():
        manager, store, _, engagement, _ = runtime(tmp_path)
        manager.update_project_policy(
            engagement.id,
            execution_mode="host",
            host_access_acknowledged=True,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=False,
            runner_profile_id=None,
            max_timeout_ms=60_000,
        )

        async def cancelled_status(owner_id: str) -> CommandExecutionStatus:
            command = asyncio.create_task(
                manager.run_command(
                    engagement_id=engagement.id,
                    owner_kind="api",
                    owner_id=owner_id,
                    request=RunCommandRequest(command="sleep 5"),
                )
            )
            for _ in range(200):
                running = store.find_entities(
                    CommandExecution, {"metadata.owner_id": owner_id}
                )
                if running:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
            (execution,) = store.find_entities(
                CommandExecution, {"metadata.owner_id": owner_id}
            )
            for _ in range(200):
                execution = store.get(CommandExecution, execution.id)
                if execution.status != CommandExecutionStatus.RUNNING:
                    break
                await asyncio.sleep(0.01)
            return execution.status

        assert await cancelled_status("before-stop") == CommandExecutionStatus.CANCELLED
        manager.begin_stopping()
        assert (
            await cancelled_status("during-stop") == CommandExecutionStatus.INTERRUPTED
        )
        await manager.shutdown()

    asyncio.run(scenario())


def test_idle_goal_reconciler_does_not_count_idle_time_as_active(tmp_path):
    store, engagement, profile, session = _conversation(tmp_path, "idle-time")
    idle_since = utc_now() - timedelta(hours=2)
    goal = _running_goal(
        store, engagement, session, elapsed_seconds=30, active_since=None
    )
    store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            goal_id=goal.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.FAILED,
            created_at=idle_since,
            updated_at=idle_since,
            request_snapshot={"model_request": _model_request()},
        )
    )
    service = ChatService(store)

    for _ in range(2):
        asyncio.run(service.reconcile_idle_running_goals())

    paused = store.get(ChatGoal, goal.id)
    assert paused.status == ChatGoalStatus.PAUSED
    # Two idle hours with nothing running were not charged as active time.
    assert paused.elapsed_seconds == pytest.approx(30, abs=1)
