import asyncio

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.automation_runtime import ProcessResultsRequest, RunCommandRequest
from nebula.v3.chat import ChatService
from nebula.v3.domain import (
    AutomationApprovalPolicy,
    AutomationNetworkMode,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ProviderProfile,
)
from tests.v3.test_automation_runtime import runtime
from tests.v3.test_chat import FakeProvider


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
