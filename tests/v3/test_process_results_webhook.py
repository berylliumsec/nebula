import asyncio
import json

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.automation_runtime import ProcessResultsRequest, RunCommandRequest
from nebula.v3.automation_tools import AutomationBroker, AutomationToolComponents
from nebula.v3.chat import ChatService, PreparedChat
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    AutomationApprovalPolicy,
    AutomationNetworkMode,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ProviderProfile,
    ScopePolicy,
    ToolCall as DurableToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.providers import ModelMessage, ModelRequest, ToolCall
from nebula.v3.tool_results import ToolOutputService
from tests.v3.test_automation_runtime import runtime
from tests.v3.test_chat import FakeProvider
from tests.v3.test_chat_tool_loop import ScriptedProvider, _response


def test_background_command_issues_lan_results_url_and_api_key(tmp_path):
    async def scenario():
        manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
        manager.callback_origin = "http://192.168.1.20:8000"
        policy = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=True,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=policy.revision,
        )
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id="session-1",
            request=RunCommandRequest(
                command="wait-forever",
                background=True,
                network=AutomationNetworkMode.PROJECT_SCOPE,
            ),
            tool_call_id="tool-1",
            chat_session_id="session-1",
            chat_turn_id="turn-1",
        )
        assert started.results_url == (
            f"http://192.168.1.20:8000/api/v1/automation-processes/{started.process_id}/results"
        )
        assert started.results_api_key
        app = create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            automation_runtime=manager,
        )
        client = TestClient(app)
        denied = client.post(
            f"/api/v1/automation-processes/{started.process_id}/results",
            json={"status": "complete", "summary": "done"},
        )
        assert denied.status_code == 401
        wrong = client.post(
            f"/api/v1/automation-processes/{started.process_id}/results",
            headers={"Authorization": "Bearer not-the-key"},
            json={"status": "complete", "summary": "done"},
        )
        assert wrong.status_code == 401
        accepted = client.post(
            f"/api/v1/automation-processes/{started.process_id}/results",
            headers={"X-Nebula-Api-Key": started.results_api_key},
            json={"status": "complete", "summary": "scan finished", "exit_code": 0},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "completed"
        replay = client.post(
            f"/api/v1/automation-processes/{started.process_id}/results",
            headers={"X-Nebula-Api-Key": started.results_api_key},
            json={"status": "complete", "summary": "again"},
        )
        assert replay.status_code == 409

    asyncio.run(scenario())


def test_results_webhook_resumes_waiting_provider_turn(tmp_path):
    async def scenario():
        manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
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
                id="provider",
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
            workspace_resolver=lambda _: tmp_path / "workspaces" / engagement.id,
        )
        session = store.create(
            ChatSession(
                id="session-callback",
                engagement_id=engagement.id,
                title="Callback",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        turn = store.create(
            ChatTurn(
                id="turn-callback",
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.WAITING_CALLBACK,
                request_snapshot={
                    "model_request": {"model": "model-a", "messages": []}
                },
            )
        )
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id=session.id,
            request=RunCommandRequest(command="wait-forever", background=True),
            tool_call_id="tool-scan",
            chat_session_id=session.id,
            chat_turn_id=turn.id,
        )
        store.update(
            ChatTurn,
            turn.id,
            {
                "status": ChatTurnStatus.WAITING_CALLBACK,
                "tool_history": [
                    {
                        "step": 0,
                        "model_call_id": "call-1",
                        "tool_call_id": "tool-scan",
                        "name": "run_command",
                        "status": "waiting_callback",
                        "process_id": started.process_id,
                        "results_url": started.results_url,
                        "arguments": {"command": "wait-forever", "background": True},
                    }
                ],
            },
            expected_revision=turn.revision,
        )
        waiting = store.get(ChatTurn, turn.id)
        manager.accept_results(
            started.process_id,
            started.results_api_key or "",
            ProcessResultsRequest(status="complete", summary="ports enumerated"),
        )
        prepared = type("Prepared", (), {})()
        prepared.turn = waiting
        events = []
        async for item in chat._resume_callback_result(prepared, waiting):
            events.append(item)
        assert events[0][0] == "tool_completed"
        assert events[0][1]["status"] == "complete"
        latest = store.get(ChatTurn, waiting.id)
        assert latest.status == ChatTurnStatus.ROUTING
        assert latest.tool_history[-1]["status"] == "complete"
        await chat.shutdown()

    asyncio.run(scenario())


def test_terminal_background_process_without_callback_becomes_unknown_failure(
    tmp_path,
):
    """A dead callback producer must not leave its tool and turn looking live."""

    async def scenario():
        manager, store, artifacts, engagement, sessions = runtime(tmp_path)
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
                id="provider-missing-callback",
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
            workspace_resolver=lambda _: tmp_path / "workspaces" / engagement.id,
        )
        session = store.create(
            ChatSession(
                id="session-missing-callback",
                engagement_id=engagement.id,
                title="Missing callback",
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        turn = store.create(
            ChatTurn(
                id="turn-missing-callback",
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.WAITING_CALLBACK,
                request_snapshot={
                    "model_request": {"model": "model-a", "messages": []}
                },
            )
        )
        call = store.create(
            DurableToolCall(
                id="tool-missing-callback",
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
        notified: list[str] = []
        manager.bind_process_terminal_observer(notified.append)
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id=session.id,
            request=RunCommandRequest(command="wait-forever", background=True),
            tool_call_id=call.id,
            chat_session_id=session.id,
            chat_turn_id=turn.id,
        )
        turn = store.update(
            ChatTurn,
            turn.id,
            {
                "tool_history": [
                    {
                        "step": 0,
                        "model_call_id": "call-1",
                        "tool_call_id": call.id,
                        "name": "run_command",
                        "status": "waiting_callback",
                        "process_id": started.process_id,
                        "results_url": started.results_url,
                        "arguments": {
                            "command": "wait-forever",
                            "background": True,
                        },
                    }
                ]
            },
            expected_revision=turn.revision,
        )

        await sessions[0].processes[0].terminate()
        managed = manager._processes[started.process_id]
        assert managed.final_task is not None
        execution = await managed.final_task
        assert execution.status.value == "failed"
        assert execution.metadata.get("results_received") is not True
        assert notified == [started.process_id]

        resumed: list[str] = []
        chat.prepare_resume = lambda turn_id: turn_id  # type: ignore[method-assign]
        chat.start_provider_turn = (  # type: ignore[method-assign]
            lambda prepared, **_kwargs: resumed.append(prepared) or prepared
        )
        assert chat.reconcile_waiting_callbacks() == [turn.id]
        assert resumed == [turn.id]
        latest = store.get(ChatTurn, turn.id)
        receipt = json.loads(latest.tool_history[-1]["provider_result"])
        assert receipt["schema"] == "nebula.tool-failure/v1"
        assert receipt["category"] == "missing_callback"
        assert receipt["side_effects"] == "unknown"
        assert receipt["retry_safe"] is False
        assert latest.status == ChatTurnStatus.ROUTING
        assert latest.tool_history[-1]["status"] == "failed"
        durable_call = store.get(DurableToolCall, call.id)
        assert durable_call.status == ToolCallStatus.FAILED
        assert durable_call.result == receipt

        # A competing wake may have settled the turn before startup recovery
        # revisits the old callback step. The durable tool row must still close.
        settled_call = store.create(
            DurableToolCall(
                id="tool-settled-missing-callback",
                engagement_id=engagement.id,
                run_id="turn-settled-missing-callback",
                origin=ToolCallOrigin.CHAT,
                chat_session_id=session.id,
                chat_turn_id="turn-settled-missing-callback",
                tool_name="run_command",
                status=ToolCallStatus.RUNNING,
                risk_class=RiskClass.ACTIVE_SCAN,
                arguments={"command": "wait-forever", "background": True},
                started_at=utc_now(),
            )
        )
        store.create(
            ChatTurn(
                id="turn-settled-missing-callback",
                engagement_id=engagement.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
                request_snapshot={
                    "model_request": {"model": "model-a", "messages": []}
                },
                tool_history=[
                    {
                        "step": 0,
                        "model_call_id": "call-settled",
                        "tool_call_id": settled_call.id,
                        "name": "run_command",
                        "status": "waiting_callback",
                        "process_id": started.process_id,
                        "arguments": {
                            "command": "wait-forever",
                            "background": True,
                        },
                    }
                ],
            )
        )
        chat.reconcile_waiting_callbacks()
        settled_call = store.get(DurableToolCall, settled_call.id)
        assert settled_call.status == ToolCallStatus.FAILED
        assert isinstance(settled_call.result, dict)
        assert settled_call.result["category"] == "missing_callback"
        assert settled_call.result["side_effects"] == "unknown"
        await chat.shutdown()

    asyncio.run(scenario())


async def _approved_background_command_chat(tmp_path):
    """A provider turn whose approved background command waits for its webhook."""

    manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
    manager.callback_origin = "http://10.0.0.8:8765"
    policy = manager.project_policy(engagement.id)
    manager.update_project_policy(
        engagement.id,
        approval_policy=AutomationApprovalPolicy.ALWAYS,
        network_enabled=True,
        runner_profile_id="runner",
        max_timeout_ms=30_000,
        expected_revision=policy.revision,
    )
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
            metadata={"default_model": "model-a"},
        )
    )
    session = store.create(
        ChatSession(
            id="session-approval",
            engagement_id=engagement.id,
            title="Approval",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={
                "message_count": 1,
                "last_sequence": 1,
                "initial_title_state": "generated",
            },
        )
    )
    user = store.create(
        ChatMessage(
            id="user-message",
            engagement_id=engagement.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Scan the host in the background.",
        )
    )
    turn = store.create(
        ChatTurn(
            id="turn-approval",
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            max_tool_calls=5,
        )
    )
    provider = ScriptedProvider(
        [
            _response(
                calls=[
                    ToolCall(
                        id="call-1",
                        name="run_command",
                        arguments={"command": "wait-forever", "background": True},
                    )
                ]
            ),
            # After the callback lands: one routing response with no call
            # moves the turn to synthesis, then the final answer.
            _response(),
            _response(text="The scan results are in."),
        ]
    )
    broker = AutomationBroker(
        manager=manager,
        store=store,
        output_service=ToolOutputService(store, artifacts),
    )
    chat = ChatService(store, provider_factory=lambda _: provider, worker_id="worker")
    assert engagement.scope_policy_id is not None
    scope = store.get(ScopePolicy, engagement.scope_policy_id)

    def prepared_for(current: ChatTurn) -> PreparedChat:
        return PreparedChat(
            provider=provider,
            provider_profile=profile,
            model_request=ModelRequest(
                model="model-a",
                messages=[ModelMessage(role="user", content=user.content)],
            ),
            resolved_model="model-a",
            citations=[],
            engagement_id=engagement.id,
            session=session,
            pending_session=None,
            stored_messages=[user],
            new_messages=[],
            tools_enabled=True,
            tool_components=AutomationToolComponents(
                broker=broker,
                scope=scope,
                workspace=tmp_path / "workspaces" / engagement.id,
                specs=dict(broker.specs),
                runtime_digest="test-runtime",
            ),
            turn=current,
            inputs_persisted=True,
        )

    return manager, store, chat, turn, provider, prepared_for


def _approve_pending(store, turn_id: str) -> ChatTurn:
    paused = store.get(ChatTurn, turn_id)
    assert paused.status == ChatTurnStatus.WAITING_APPROVAL
    assert paused.approval_id is not None
    approval = store.get(Approval, paused.approval_id)
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
    return paused


def test_approved_background_command_waits_for_its_callback(tmp_path):
    """An approval resume must park a background command exactly like a fresh run.

    The receipt of an approved background command carries results_url and
    results_api_key; the resumed turn has to wait in WAITING_CALLBACK for the
    LAN webhook instead of recording the call as complete with no output.
    """

    async def scenario():
        (
            manager,
            store,
            chat,
            turn,
            _,
            prepared_for,
        ) = await _approved_background_command_chat(tmp_path)
        events = [item async for item in chat.stream(prepared_for(turn))]
        assert events[-1][0] == "approval_required"
        paused = _approve_pending(store, turn.id)

        events = [item async for item in chat.stream(prepared_for(paused))]

        names = [name for name, _ in events]
        assert names == ["started", "callback_required"], names
        callback = events[-1][1]
        process_id = callback["process_id"]
        assert process_id
        assert callback["results_url"] == (
            f"http://10.0.0.8:8765/api/v1/automation-processes/{process_id}/results"
        )
        waiting = store.get(ChatTurn, turn.id)
        assert waiting.status == ChatTurnStatus.WAITING_CALLBACK
        assert waiting.approval_id is None
        assert waiting.execution_claim_id is None
        entry = waiting.tool_history[-1]
        assert entry["status"] == "waiting_callback"
        assert entry["process_id"] == process_id
        assert entry["results_url"] == callback["results_url"]
        assert entry["trusted_result"] is False
        results_api_key = json.loads(entry["provider_result"])["results_api_key"]
        assert results_api_key

        # The webhook now lands on a turn that is actually waiting for it.
        manager.accept_results(
            process_id,
            results_api_key,
            ProcessResultsRequest(status="complete", summary="ports enumerated"),
        )
        events = [item async for item in chat.stream(prepared_for(waiting))]

        assert [name for name, _ in events][:2] == ["started", "tool_completed"]
        assert events[1][1]["status"] == "complete"
        assert events[1][1]["summary"] == "ports enumerated"
        assert events[-1][0] == "done"
        finished = store.get(ChatTurn, turn.id)
        assert finished.status == ChatTurnStatus.COMPLETE
        assert finished.tool_history[-1]["status"] == "complete"
        await chat.shutdown()

    asyncio.run(scenario())


def test_approval_and_results_webhook_resume_through_provider_admission(tmp_path):
    """The operator's resume and the webhook wake both reach their parked step.

    Both go through ``start_provider_turn`` and provider admission, exactly as
    ``POST /chat/turns/{id}/resume`` and the results webhook do.
    """

    async def scenario():
        (
            manager,
            store,
            chat,
            turn,
            provider,
            prepared_for,
        ) = await _approved_background_command_chat(tmp_path)
        chat.prepare_resume = lambda turn_id: prepared_for(  # type: ignore[method-assign]
            store.get(ChatTurn, turn_id)
        )
        chat.start_provider_turn(prepared_for(turn))
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "approval_required"
        _approve_pending(store, turn.id)

        chat.start_provider_turn(chat.prepare_resume(turn.id))
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        # The approved command ran and now waits for its results callback.
        assert events == ["queued", "admitted", "started", "callback_required"]
        waiting = store.get(ChatTurn, turn.id)
        assert waiting.status == ChatTurnStatus.WAITING_CALLBACK
        entry = waiting.tool_history[-1]
        results_api_key = json.loads(entry["provider_result"])["results_api_key"]

        manager.accept_results(
            entry["process_id"],
            results_api_key,
            ProcessResultsRequest(status="complete", summary="ports enumerated"),
        )
        assert chat.continue_after_tool_callback(entry["process_id"]) == turn.id
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]

        assert events[:4] == ["queued", "admitted", "started", "tool_completed"]
        assert events[-1] == "done"
        finished = store.get(ChatTurn, turn.id)
        assert finished.status == ChatTurnStatus.COMPLETE
        (step,) = chat._turn_history(finished)
        assert step["status"] == "complete"
        assert step["result_summary"] == "ports enumerated"
        # The model routed on the callback's outcome, not the accepted receipt
        # that still carried the callback credentials.
        replayed = provider.requests[1].tool_results[-1].output
        assert isinstance(replayed, dict)
        assert replayed["status"] == "completed"
        assert replayed["results_api_key"] is None
        await chat.shutdown()

    asyncio.run(scenario())
