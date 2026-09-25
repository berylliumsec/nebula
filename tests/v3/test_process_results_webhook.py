import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.automation_runtime import (
    MAX_RESULTS_OUTPUT_BYTES,
    ProcessIORequest,
    ProcessResultsRequest,
    RunCommandRequest,
)
from nebula.v3.automation_tools import AutomationBroker, AutomationToolComponents
from nebula.v3.chat import ChatService, PreparedChat
from nebula.v3.chat_turn_ledger import ChatTurnLedger
from nebula.v3.database import ChatTurnStepEventRow
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
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    RiskClass,
    ProviderProfile,
    ScopePolicy,
    ToolCall as DurableToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.providers import ModelMessage, ModelRequest, ModelResponse, ToolCall
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import (
    MAX_EXCERPT_BYTES,
    ToolArtifactRef,
    ToolOutputService,
    ToolResultReceipt,
    sanitize_model_history_result,
    serialize_model_result,
)
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
        # A small acknowledgement, not the process record: a command that
        # prints the response keeps only this in its own stdout.
        assert accepted.json() == {
            "accepted": True,
            "process_id": started.process_id,
            "status": "completed",
            "exit_code": 0,
        }
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


async def _approved_background_command_chat(
    tmp_path,
    *,
    approval_policy: AutomationApprovalPolicy = AutomationApprovalPolicy.ALWAYS,
    provider: ScriptedProvider | None = None,
):
    """A provider turn whose approved background command waits for its webhook."""

    manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
    manager.callback_origin = "http://10.0.0.8:8765"
    policy = manager.project_policy(engagement.id)
    manager.update_project_policy(
        engagement.id,
        approval_policy=approval_policy,
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
    provider = provider or ScriptedProvider(
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


def _results_key(manager, process_id: str) -> str:
    """The results key, which only the process receives, in its environment."""

    return manager._processes[process_id].backend.extra_env["NEBULA_RESULTS_KEY"]


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

    The receipt of an approved background command carries its results_url;
    the resumed turn has to wait in WAITING_CALLBACK for the LAN webhook
    instead of recording the call as complete with no output.
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
        # Only the process receives the key; the durable receipt never does.
        assert json.loads(entry["provider_result"])["results_api_key"] is None
        results_api_key = _results_key(manager, process_id)

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
        results_api_key = _results_key(manager, entry["process_id"])

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
        assert replayed["summary"] == "ports enumerated"
        assert replayed["results_api_key"] is None
        await chat.shutdown()

    asyncio.run(scenario())


class _ReactiveProvider(ScriptedProvider):
    """Answers each routing step from the request that reaches it.

    A step is a ready response or a function of the request, so a later step
    can use an ID that only exists once an earlier tool result came back.
    """

    def __init__(self, steps: list[Any]) -> None:
        super().__init__([])
        self.steps = list(steps)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.metadata.get("operation") == "conversation_naming":
            return _response(text="Background command")
        if not self.steps:
            raise AssertionError("provider script was exhausted")
        step = self.steps.pop(0)
        return step(request) if callable(step) else step

    def routed(self) -> list[ModelRequest]:
        return [
            request
            for request in self.requests
            if request.metadata.get("operation") != "conversation_naming"
        ]


_RUN_IN_BACKGROUND = _response(
    calls=[
        ToolCall(
            id="call-1",
            name="run_command",
            arguments={"command": "wait-forever", "background": True},
        )
    ]
)


def _read_posted_stdout(request: ModelRequest) -> ModelResponse:
    receipt = request.tool_results[-1].output
    assert isinstance(receipt, dict)
    (posted,) = [
        item
        for item in receipt.get("artifacts") or []
        if item["filename"].endswith(".results-stdout.txt")
    ]
    return _response(
        calls=[
            ToolCall(
                id="call-2",
                name="tool_output.read",
                arguments={"artifact_id": posted["artifact_id"]},
            )
        ]
    )


def _assert_bounded_history_result(output: object) -> dict[str, Any]:
    assert isinstance(output, dict)
    rendered = json.dumps(output, ensure_ascii=False, sort_keys=True)
    assert len(rendered.encode("utf-8")) <= MAX_EXCERPT_BYTES
    assert "Historical pre-v2 action output was omitted" not in rendered
    assert "results_api_key" not in output or output["results_api_key"] is None
    return output


def test_callback_result_reaches_the_model_through_the_results_webhook(
    tmp_path, monkeypatch
):
    """The posted summary and output reach the next provider request.

    The command POSTs to the real webhook route; the wake it triggers resumes
    through ``start_provider_turn`` and provider admission. The next routing
    request must carry a valid ``nebula.tool-result/v2`` receipt with the
    callback's summary and references to its posted output, not the pre-v2
    omission notice, and the posted output must be readable through
    ``tool_output.read``.
    """

    nonce = "nebula-nonce-7f3a91"

    async def scenario():
        provider = _ReactiveProvider(
            [
                _RUN_IN_BACKGROUND,
                _read_posted_stdout,
                _response(),
                _response(text=f"The command printed {nonce}."),
            ]
        )
        (
            manager,
            store,
            chat,
            turn,
            _,
            prepared_for,
        ) = await _approved_background_command_chat(
            tmp_path,
            approval_policy=AutomationApprovalPolicy.NEVER,
            provider=provider,
        )
        chat.prepare_resume = lambda turn_id: prepared_for(  # type: ignore[method-assign]
            store.get(ChatTurn, turn_id)
        )
        chat.start_provider_turn(prepared_for(turn))
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "callback_required", events
        waiting = store.get(ChatTurn, turn.id)
        assert waiting.status == ChatTurnStatus.WAITING_CALLBACK
        entry = waiting.tool_history[-1]
        results_api_key = _results_key(manager, entry["process_id"])

        # The app's wake is the one this test's chat serves, so the webhook
        # route itself drives accept_results and the resume.
        wake = ChatService.continue_after_tool_callback
        monkeypatch.setattr(
            ChatService,
            "continue_after_tool_callback",
            lambda _self, process_id: wake(chat, process_id),
        )
        app = create_app(
            store,
            artifact_store=manager.artifact_store,
            auth_token="test-token",
            automation_runtime=manager,
        )
        big_output = {
            "nonce": nonce,
            "rows": [f"row-{index}" for index in range(4_000)],
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            posted = await client.post(
                f"/api/v1/automation-processes/{entry['process_id']}/results",
                headers={"X-Nebula-Api-Key": results_api_key},
                json={
                    "status": "complete",
                    "summary": (
                        f"Printed {nonce}. Authorization: Bearer "
                        "abcdefghijklmnopqrstuvwxyz0123456789"
                    ),
                    "exit_code": 0,
                    "output": big_output,
                    "stdout": f"started\n{nonce}\n" + "x" * 60_000 + "\n",
                },
            )
        assert posted.status_code == 200, posted.text

        events = [item async for item in chat.follow_provider_turn(turn.id)]
        names = [name for name, _ in events]
        assert names[:4] == ["queued", "admitted", "started", "tool_completed"]
        assert names[-1] == "done"
        completed = events[3][1]
        assert completed["status"] == "complete"
        assert nonce in completed["summary"]
        finished = store.get(ChatTurn, turn.id)
        assert finished.status == ChatTurnStatus.COMPLETE

        routed = provider.routed()
        replayed = _assert_bounded_history_result(routed[1].tool_results[-1].output)
        receipt = ToolResultReceipt.model_validate(replayed)
        assert receipt.status.value == "completed"
        assert receipt.exit_code == 0
        assert receipt.incomplete is False
        assert receipt.results_url is None
        assert receipt.summary is not None
        assert nonce in receipt.summary
        # The posted summary is redacted before it enters model context.
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in receipt.summary
        assert routed[1].tool_results[-1].is_error is False
        assert {item.kind for item in receipt.artifacts} == {"stdout", "parsed"}
        assert all(item.searchable for item in receipt.artifacts)
        assert completed["artifacts"] == [
            item.model_dump(mode="json") for item in receipt.artifacts
        ]
        # The durable tool row records the same valid receipt.
        call = store.get(DurableToolCall, receipt.tool_call_id)
        assert call.status == ToolCallStatus.COMPLETE
        assert ToolResultReceipt.model_validate(call.result) == receipt

        # The posted output is evidence the model can read on demand.
        read = routed[2].tool_results[-1].output
        assert isinstance(read, dict)
        assert read["schema"] == "nebula.tool-output.read/v1"
        assert [line["text"] for line in read["lines"][:2]] == ["started", nonce]
        # Replayed again on the synthesis request, the receipt is unchanged.
        assert routed[-1].tool_results[0].output == replayed
        await chat.shutdown()

    asyncio.run(scenario())


def test_failed_callback_reaches_the_model_as_a_failed_receipt(tmp_path):
    """A command that reports failure gives the model its own account of it."""

    async def scenario():
        (
            manager,
            store,
            chat,
            turn,
            provider,
            prepared_for,
        ) = await _approved_background_command_chat(
            tmp_path,
            approval_policy=AutomationApprovalPolicy.NEVER,
            provider=_ReactiveProvider(
                [
                    _RUN_IN_BACKGROUND,
                    _response(),
                    _response(text="The scan failed."),
                ]
            ),
        )
        chat.prepare_resume = lambda turn_id: prepared_for(  # type: ignore[method-assign]
            store.get(ChatTurn, turn_id)
        )
        chat.start_provider_turn(prepared_for(turn))
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "callback_required", events
        entry = store.get(ChatTurn, turn.id).tool_history[-1]
        manager.accept_results(
            entry["process_id"],
            _results_key(manager, entry["process_id"]),
            ProcessResultsRequest(
                status="failed",
                summary="target 203.0.113.7 refused every probe",
                exit_code=2,
                stdout="probe 1 refused\nprobe 2 refused\n",
            ),
        )
        assert chat.continue_after_tool_callback(entry["process_id"]) == turn.id
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "done", events

        assert isinstance(provider, _ReactiveProvider)
        result = provider.routed()[1].tool_results[-1]
        assert result.is_error is True
        receipt = ToolResultReceipt.model_validate(
            _assert_bounded_history_result(result.output)
        )
        assert receipt.status.value == "failed"
        assert receipt.exit_code == 2
        assert receipt.summary == "target 203.0.113.7 refused every probe"
        assert [item.kind for item in receipt.artifacts] == ["stdout"]
        (step,) = chat._turn_history(store.get(ChatTurn, turn.id))
        assert step["status"] == "failed"
        call = store.get(DurableToolCall, receipt.tool_call_id)
        assert call.status == ToolCallStatus.FAILED
        assert ToolResultReceipt.model_validate(call.result) == receipt
        await chat.shutdown()

    asyncio.run(scenario())


def test_producer_that_ends_without_its_callback_reaches_the_model_as_unknown(
    tmp_path,
):
    """A process that exits without posting gives the model an unknown effect.

    The failure envelope stays bounded and never retry-safe, and it names the
    call and the output the process did record, so the advice to inspect that
    output can be followed.
    """

    async def scenario():
        (
            manager,
            store,
            chat,
            turn,
            provider,
            prepared_for,
        ) = await _approved_background_command_chat(
            tmp_path,
            approval_policy=AutomationApprovalPolicy.NEVER,
            provider=_ReactiveProvider(
                [
                    _RUN_IN_BACKGROUND,
                    _response(),
                    _response(text="The command ended without results."),
                ]
            ),
        )
        chat.prepare_resume = lambda turn_id: prepared_for(  # type: ignore[method-assign]
            store.get(ChatTurn, turn_id)
        )
        manager.bind_process_terminal_observer(chat.continue_after_tool_callback)
        chat.start_provider_turn(prepared_for(turn))
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "callback_required", events
        entry = store.get(ChatTurn, turn.id).tool_history[-1]

        managed = manager._processes[entry["process_id"]]
        await managed.backend.terminate()
        assert managed.final_task is not None
        execution = await managed.final_task
        assert execution.metadata.get("results_received") is not True
        events = [name async for name, _ in chat.follow_provider_turn(turn.id)]
        assert events[-1] == "done", events

        assert isinstance(provider, _ReactiveProvider)
        result = provider.routed()[1].tool_results[-1]
        assert result.is_error is True
        failure = _assert_bounded_history_result(result.output)
        assert failure["schema"] == "nebula.tool-failure/v1"
        assert failure["category"] == "missing_callback"
        assert failure["side_effects"] == "unknown"
        assert failure["retry_safe"] is False
        assert failure["tool_call_id"] == entry["tool_call_id"]
        refs = [ToolArtifactRef.model_validate(item) for item in failure["artifacts"]]
        assert {item.kind for item in refs} == {"stdout", "stderr"}
        assert {item.artifact_id for item in refs} == {
            execution.stdout_artifact_id,
            execution.stderr_artifact_id,
        }
        call = store.get(DurableToolCall, entry["tool_call_id"])
        assert call.status == ToolCallStatus.FAILED
        assert call.result == failure
        await chat.shutdown()

    asyncio.run(scenario())


def _rows_holding(database: Path, needle: str) -> list[str]:
    """The tables of Core's database with a row whose text holds ``needle``."""

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        tables = [
            str(name)
            for (name,) in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        ]
        return [
            table
            for table in tables
            if any(
                needle in repr(row)
                for row in connection.execute(f'select * from "{table}"')
            )
        ]


def test_results_key_reaches_only_the_process(tmp_path, monkeypatch):
    """The results key authorizes one POST; nothing durable or visible holds it.

    The process gets it as NEBULA_RESULTS_KEY. No database row, stream event
    or provider request carries it, and Core keeps only its digest to check
    the POST.
    """

    async def scenario():
        provider = _ReactiveProvider(
            [_RUN_IN_BACKGROUND, _response(), _response(text="Done.")]
        )
        (
            manager,
            store,
            chat,
            turn,
            _,
            prepared_for,
        ) = await _approved_background_command_chat(
            tmp_path,
            approval_policy=AutomationApprovalPolicy.NEVER,
            provider=provider,
        )
        chat.prepare_resume = lambda turn_id: prepared_for(  # type: ignore[method-assign]
            store.get(ChatTurn, turn_id)
        )
        chat.start_provider_turn(prepared_for(turn))
        parked = [item async for item in chat.follow_provider_turn(turn.id)]
        assert parked[-1][0] == "callback_required", parked
        (step,) = chat._turn_history(store.get(ChatTurn, turn.id))
        process_id = step["process_id"]
        key = _results_key(manager, process_id)
        assert key
        assert manager._processes[process_id].backend.extra_env[
            "NEBULA_RESULTS_URL"
        ] == (f"http://10.0.0.8:8765/api/v1/automation-processes/{process_id}/results")
        execution = store.get(CommandExecution, manager._execution_id(process_id))
        assert (
            execution.metadata["results_key_sha256"]
            == hashlib.sha256(key.encode("utf-8")).hexdigest()
        )
        waiting = json.loads(step["provider_result"])
        assert waiting["results_api_key"] is None
        assert waiting["results_url"] == step["results_url"]
        assert store.get(DurableToolCall, step["tool_call_id"]).result == waiting
        assert key not in json.dumps(parked, default=str)
        assert _rows_holding(tmp_path / "nebula.db", key) == []

        wake = ChatService.continue_after_tool_callback
        monkeypatch.setattr(
            ChatService,
            "continue_after_tool_callback",
            lambda _self, process_id: wake(chat, process_id),
        )
        app = create_app(
            store,
            artifact_store=manager.artifact_store,
            auth_token="test-token",
            automation_runtime=manager,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            posted = await client.post(
                f"/api/v1/automation-processes/{process_id}/results",
                headers={"X-Nebula-Api-Key": key},
                json={"status": "complete", "summary": "done", "exit_code": 0},
            )
        assert posted.status_code == 200, posted.text
        assert posted.json() == {
            "accepted": True,
            "process_id": process_id,
            "status": "completed",
            "exit_code": 0,
        }
        finished = [item async for item in chat.follow_provider_turn(turn.id)]
        assert finished[-1][0] == "done", finished
        assert key not in json.dumps(finished, default=str)
        assert all(
            key not in request.model_dump_json() for request in provider.requests
        )
        assert _rows_holding(tmp_path / "nebula.db", key) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_callback_key_recorded_before_it_left_the_receipt_is_never_read_back(
    tmp_path,
):
    """Rows written while waiting receipts carried the key keep it at rest only.

    The ledger, a turn from before the ledger, the tool call API and model
    replay all return the receipt without it, so neither the operator's tool
    card nor the model ever sees it again.
    """

    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Legacy callback"))
    legacy_key = "legacy-results-key-5d0c1e9a7b"
    results_url = "http://10.0.0.8:8765/api/v1/automation-processes/proc/results"
    receipt = {
        "schema": "nebula.tool-result/v2",
        "tool_call_id": "tool-legacy",
        "tool_name": "run_command",
        "tool_version": "1",
        "status": "completed",
        "process_id": "proc",
        "summary": "Process is running with id proc",
        "incomplete": True,
        "next_actions": ["process_io"],
        "results_url": results_url,
        "results_api_key": legacy_key,
    }
    entry = {
        "step": 0,
        "model_call_id": "call-1",
        "tool_call_id": "tool-legacy",
        "name": "run_command",
        "status": "waiting_callback",
        "process_id": "proc",
        "results_url": results_url,
        "provider_result": serialize_model_result(receipt),
        "arguments": {"command": "wait-forever", "background": True},
    }
    turns = {}
    for turn_id in ("turn-before-ledger", "turn-in-ledger"):
        turns[turn_id] = store.create(
            ChatTurn(
                id=turn_id,
                engagement_id=engagement.id,
                session_id="session-legacy",
                provider_profile_id="provider",
                model="model-a",
                status=ChatTurnStatus.WAITING_CALLBACK,
                # A turn from before the ledger kept its steps on the row.
                tool_history=[entry] if turn_id == "turn-before-ledger" else [],
            )
        )
    with store.database.session() as session:
        # A ledger row written while the waiting receipt carried the key.
        session.add(
            ChatTurnStepEventRow(
                id="legacy-row",
                turn_id="turn-in-ledger",
                sequence=1,
                step=0,
                event_type="waiting_callback",
                tool_call_id="tool-legacy",
                payload=entry,
                occurred_at=utc_now(),
                idempotency_key="legacy-waiting",
            )
        )
    store.create(
        DurableToolCall(
            id="tool-legacy",
            engagement_id=engagement.id,
            run_id="turn-in-ledger",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="session-legacy",
            chat_turn_id="turn-in-ledger",
            tool_name="run_command",
            status=ToolCallStatus.RUNNING,
            risk_class=RiskClass.ACTIVE_SCAN,
            arguments=entry["arguments"],
            result=receipt,
            started_at=utc_now(),
        )
    )
    assert _rows_holding(tmp_path / "nebula.db", legacy_key)

    ledger = ChatTurnLedger(store.database)
    for turn in turns.values():
        (read,) = ledger.history(turn)
        assert legacy_key not in json.dumps(read)
        assert json.loads(read["provider_result"])["results_url"] == results_url
    # The folded history is cached per turn and extended with new rows only;
    # neither the cached fold nor an extended one brings the key back.
    in_ledger = turns["turn-in-ledger"]
    assert legacy_key not in json.dumps(ledger.history(in_ledger))
    ledger.append(
        in_ledger.id,
        {**entry, "step": 1, "model_call_id": "call-2", "status": "complete"},
    )
    extended = ledger.history(in_ledger)
    assert [item["step"] for item in extended] == [0, 1]
    assert legacy_key not in json.dumps(extended)
    tail = ledger.tail("turn-in-ledger", 5)
    event = ledger.event("turn-in-ledger", "legacy-waiting")
    assert tail is not None and event is not None
    assert legacy_key not in json.dumps([tail, event])
    # Importing the pre-ledger turn does not copy the key into a new row.
    ledger.import_legacy(turns["turn-before-ledger"])
    assert ledger.has_events("turn-before-ledger")
    with sqlite3.connect(f"file:{tmp_path / 'nebula.db'}?mode=ro", uri=True) as db:
        imported = db.execute(
            "select payload from chat_turn_step_events where turn_id = ?",
            ("turn-before-ledger",),
        ).fetchall()
    assert imported and legacy_key not in repr(imported)

    # Replayed to a model, the old receipt is still a valid v2 receipt.
    replayed = sanitize_model_history_result(
        entry["provider_result"], tool_call_id="tool-legacy", tool_name="run_command"
    )
    assert replayed["schema"] == "nebula.tool-result/v2"
    assert replayed["tool_version"] == "1"
    assert replayed["results_api_key"] is None
    assert ToolResultReceipt.model_validate(receipt).results_api_key is None

    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    single = client.get("/api/v1/tool-calls/tool-legacy", headers=headers)
    listed = client.get(
        "/api/v1/tool-calls", params={"engagement_id": engagement.id}, headers=headers
    )
    assert single.status_code == 200, single.text
    assert listed.status_code == 200, listed.text
    assert single.json()["result"]["results_url"] == results_url
    assert legacy_key not in single.text
    assert legacy_key not in listed.text


def test_process_output_that_ends_after_its_callback_joins_the_record(tmp_path):
    """Captured stdout and stderr are linked even when results arrived first.

    The callback settled the outcome, so status and exit code stay as posted,
    and process_io finds the process's own output like any command's.
    """

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
            owner_id="session-1",
            request=RunCommandRequest(command="wait-forever", background=True),
            tool_call_id="tool-late-output",
            chat_session_id="session-1",
            chat_turn_id="turn-1",
        )
        accepted = manager.accept_results(
            started.process_id,
            started.results_api_key or "",
            ProcessResultsRequest(status="complete", summary="posted", exit_code=0),
        )
        assert accepted.stdout_artifact_id is None
        managed = manager._processes[started.process_id]
        await managed.backend.terminate()
        assert managed.final_task is not None
        finished = await managed.final_task

        assert finished.status == CommandExecutionStatus.COMPLETED
        assert finished.exit_code == 0
        assert finished.metadata["results_summary"] == "posted"
        assert finished.stdout_artifact_id is not None
        assert finished.stderr_artifact_id is not None
        assert finished.redacted_stdout_artifact_id is not None
        assert finished.observed_stdout_bytes > 0
        # The callback already woke the owner; the exit does not wake it again.
        assert notified == []
        assert store.get(CommandExecution, finished.id) == finished
        io = await manager.process_io(
            started.process_id, ProcessIORequest(), engagement_id=engagement.id
        )
        assert io.stdout_artifact_id == finished.stdout_artifact_id
        assert io.stderr_artifact_id == finished.stderr_artifact_id

    asyncio.run(scenario())


def test_results_webhook_bounds_what_a_command_posts(tmp_path):
    """Oversized results are refused with the limit; the process can still post."""

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
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id="session-1",
            request=RunCommandRequest(command="wait-forever", background=True),
            tool_call_id="tool-bounded",
            chat_session_id="session-1",
            chat_turn_id="turn-1",
        )
        url = f"/api/v1/automation-processes/{started.process_id}/results"
        headers = {"X-Nebula-Api-Key": started.results_api_key or ""}
        app = create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            automation_runtime=manager,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            oversized_output = await client.post(
                url,
                headers=headers,
                json={"output": {"rows": ["x" * 100] * 1_000}},
            )
            assert oversized_output.status_code == 422, oversized_output.text
            assert f"the limit is {MAX_RESULTS_OUTPUT_BYTES}" in oversized_output.text
            oversized_body = await client.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                content=b'{"stdout": "' + b"y" * (1024 * 1024) + b'"}',
            )
            assert oversized_body.status_code == 413, oversized_body.text
            assert "1 MiB" in oversized_body.text
            execution = store.get(
                CommandExecution, manager._execution_id(started.process_id)
            )
            assert execution.status == CommandExecutionStatus.RUNNING
            assert not execution.metadata.get("results_received")

            within = await client.post(
                url,
                headers=headers,
                json={
                    "summary": "fits",
                    "output": {"rows": ["x" * 100] * 500},
                    "stdout": "line\n" * 100,
                },
            )
            assert within.status_code == 200, within.text
            assert within.json()["status"] == "completed"
        recorded = store.get(
            CommandExecution, manager._execution_id(started.process_id)
        )
        # The posted output lives in its artifacts, not on the record that the
        # process list and the webhook would otherwise repeat.
        assert "results_output" not in recorded.metadata
        assert "results_stdout" not in recorded.metadata
        assert recorded.metadata["results_output_artifact_id"]
        assert recorded.metadata["results_stdout_artifact_id"]
        with pytest.raises(ValueError, match="the limit is"):
            ProcessResultsRequest(output={"rows": ["x" * 100] * 1_000})

    asyncio.run(scenario())
