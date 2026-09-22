"""The operator's Subagents choice survives the turns Core starts on its own.

Checking Subagents saves the choice on the conversation at once. Goal
dispatch, goal continuation and scheduled occurrences build their own turns,
and every turn records its settings back on the conversation, so a Core-built
turn that copied an older turn's settings unchecked the box again. A harness
turn sent while the subagent model is still being verified likewise erased the
saved choice.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatHistoryConflict, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_schedules import ChatScheduleService, ScheduleCreate
from nebula.v3.domain import (
    ChatSchedule,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessSession,
    McpServerProfile,
    McpTransport,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    utc_now,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
    ToolCall,
    ToolChoice,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_harness_provider_subagents import (
    _prepare as _prepare_harness_turn,
    _setup as _setup_harness,
)

SESSION = "session"


class ToolProvider(ModelProvider):
    """A tool-calling model that finishes routing, then answers."""

    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                id="provider",
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(
                    streaming=True, tools=True, strict_tools=True
                ),
            )
        )
        self.during_first_turn = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        usage = ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3)
        if not request.metadata.get("operation") and self.during_first_turn:
            action, self.during_first_turn = self.during_first_turn, None
            action()
        if request.tools and request.tool_choice != ToolChoice.NONE:
            return ModelResponse(
                provider_id="provider",
                model="model-a",
                tool_calls=[
                    ToolCall(id="finish", name="finish_response", arguments={})
                ],
                usage=usage,
                finish_reason="tool_calls",
            )
        return ModelResponse(
            provider_id="provider",
            model="model-a",
            text="Done.",
            usage=usage,
            finish_reason="stop",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id="provider", healthy=True, models=["model-a"])


def _setup(tmp_path: Path):
    store = NebulaStore(tmp_path / "choice.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
            capability_verifications={
                "model-a": ProviderCapabilityVerification(
                    model="model-a", status=ProviderVerificationStatus.VERIFIED
                )
            },
        )
    )
    store.create(
        ChatSession(
            id=SESSION,
            engagement_id="project",
            title="Delegating chat",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    provider = ToolProvider()
    chat = ChatService(store, provider_factory=lambda _: provider, worker_id="worker")
    # Goal tool turns can publish a dashboard.
    chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
    client = TestClient(create_app(store, auth_token="test-token"))
    return store, provider, chat, client


def _patch(client: TestClient, session_id: str, **changes) -> dict:
    response = client.patch(
        f"/api/v1/chat-sessions/{session_id}",
        headers={"Authorization": "Bearer test-token"},
        json=changes,
    )
    assert response.status_code == 200, response.text
    return response.json()["metadata"]


def _manual_turn(**flags) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        provider_id="provider",
        engagement_id="project",
        session_id=SESSION,
        model="model-a",
        messages=[{"role": "user", "content": "Look around first."}],
        include_knowledge=False,
        stream=True,
        **flags,
    )


def _goal(store: NebulaStore, *, step_budget: int | None = None) -> ChatGoalService:
    goals = ChatGoalService(store)
    goals.create(
        SESSION,
        GoalCreate(
            objective="Split the review",
            completion_criteria=["Every area is reviewed"],
            step_budget=step_budget,
        ),
    )
    return goals


def _start(goals: ChatGoalService) -> None:
    goal = goals.get(SESSION)
    goals.write(SESSION, GoalWrite(expected_revision=goal.revision, action="start"))


def _schedule(store: NebulaStore) -> None:
    schedule = ChatScheduleService(store).create(
        SESSION, ScheduleCreate(interval_seconds=3600)
    )
    store.update(
        ChatSchedule,
        schedule.id,
        {"next_run_at": utc_now() - timedelta(seconds=1)},
        expected_revision=schedule.revision,
    )


def _turns(store: NebulaStore) -> list[ChatTurn]:
    return sorted(
        store.list_session_entities(ChatTurn, SESSION), key=lambda turn: turn.created_at
    )


async def _settled(store: NebulaStore, count: int) -> list[ChatTurn]:
    for _ in range(500):
        turns = _turns(store)
        if len(turns) == count and all(
            turn.status == ChatTurnStatus.COMPLETE for turn in turns
        ):
            goal = ChatGoalService(store).get(SESSION)
            if goal.execution_claim_id is None:
                return turns
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} settled turns, got {_turns(store)}")


def test_goal_turns_keep_subagents_checked_after_the_last_send(tmp_path):
    async def scenario() -> None:
        store, _, chat, client = _setup(tmp_path)
        goals = _goal(store, step_budget=2)
        # The last message went out with Subagents off ...
        await chat.complete(await chat.prepare_async(_manual_turn()))
        # ... then the operator checked it and started the goal.
        saved = _patch(
            client,
            SESSION,
            allow_subagents=True,
            max_active_subagents=2,
            reasoning_effort="high",
        )
        assert saved["allow_subagents"] is True
        _start(goals)
        await chat.dispatch_running_goal(SESSION, "Begin work on the goal now.")

        manual, dispatched, continued = await _settled(store, 3)
        assert not manual.request_snapshot.get("allow_subagents")
        for turn in (dispatched, continued):
            assert turn.goal_id is not None
            assert turn.request_snapshot.get("allow_subagents") is True
            assert turn.request_snapshot.get("max_active_subagents") == 2
            assert turn.request_snapshot["model_request"]["reasoning_effort"] == "high"
        metadata = store.get(ChatSession, SESSION).metadata
        assert metadata["allow_subagents"] is True
        assert metadata["max_active_subagents"] == 2
        assert metadata["reasoning_effort"] == "high"
        await chat.shutdown()

    asyncio.run(scenario())


def test_goal_continuation_follows_subagents_checked_mid_turn(tmp_path):
    """A running goal is always mid-response, so the box is checked mid-turn."""

    async def scenario() -> None:
        store, provider, chat, client = _setup(tmp_path)
        goals = _goal(store, step_budget=2)
        _start(goals)
        provider.during_first_turn = lambda: _patch(
            client, SESSION, allow_subagents=True, max_active_subagents=4
        )
        await chat.dispatch_running_goal(SESSION, "Begin work on the goal now.")

        first, continued = await _settled(store, 2)
        # The running turn kept what it started with; the next one delegates.
        assert not first.request_snapshot.get("allow_subagents")
        assert continued.request_snapshot.get("allow_subagents") is True
        assert continued.request_snapshot.get("max_active_subagents") == 4
        metadata = store.get(ChatSession, SESSION).metadata
        assert metadata["allow_subagents"] is True
        assert metadata["max_active_subagents"] == 4
        await chat.shutdown()

    asyncio.run(scenario())


def test_scheduled_run_keeps_subagents_checked_after_the_last_send(tmp_path):
    async def scenario() -> None:
        store, _, chat, client = _setup(tmp_path)
        goals = _goal(store)
        await chat.complete(await chat.prepare_async(_manual_turn()))
        _patch(
            client,
            SESSION,
            allow_subagents=True,
            max_active_subagents=2,
            reasoning_effort="low",
        )
        _start(goals)
        _schedule(store)

        await chat.fire_due_schedules()

        assert ChatScheduleService(store).get(SESSION).last_status == "complete"
        _, scheduled = _turns(store)
        assert scheduled.request_snapshot.get("allow_subagents") is True
        assert scheduled.request_snapshot.get("max_active_subagents") == 2
        assert scheduled.request_snapshot["model_request"]["reasoning_effort"] == "low"
        metadata = store.get(ChatSession, SESSION).metadata
        assert metadata["allow_subagents"] is True
        assert metadata["max_active_subagents"] == 2
        assert metadata["reasoning_effort"] == "low"
        await chat.shutdown()

    asyncio.run(scenario())


def test_first_scheduled_run_uses_the_saved_choice(tmp_path):
    async def scenario() -> None:
        store, _, chat, client = _setup(tmp_path)
        goals = _goal(store)
        # Checked before the conversation's first turn: no snapshot to copy.
        _patch(client, SESSION, allow_subagents=True)
        _start(goals)
        _schedule(store)

        await chat.fire_due_schedules()

        assert ChatScheduleService(store).get(SESSION).last_status == "complete"
        (scheduled,) = _turns(store)
        assert scheduled.request_snapshot.get("allow_subagents") is True
        assert store.get(ChatSession, SESSION).metadata["allow_subagents"] is True
        await chat.shutdown()

    asyncio.run(scenario())


def test_core_built_turns_send_the_saved_servers_hooks_and_effort(tmp_path):
    async def scenario() -> None:
        store, _, chat, client = _setup(tmp_path)
        for server_id in ("mcp-old", "mcp-new", "mcp-later-off"):
            store.create(
                McpServerProfile(
                    id=server_id,
                    name=server_id,
                    transport=McpTransport.STREAMABLE_HTTP,
                    url="http://127.0.0.1:1/mcp",
                    enabled=True,
                )
            )
        goals = _goal(store)
        _start(goals)
        goal = goals.get(SESSION)
        # A goal turn settled with the old selection, which its send recorded
        # on the conversation.
        old = _manual_turn(
            goal_id=goal.id,
            mcp_server_ids=["mcp-old"],
            hook_ids=["old-hook"],
            reasoning_effort="low",
        )
        old_turn = store.create(
            ChatTurn(
                engagement_id="project",
                session_id=SESSION,
                goal_id=goal.id,
                provider_profile_id="provider",
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
                tools_enabled=True,
                request_snapshot={
                    "include_oci_tools": False,
                    "mcp_server_ids": ["mcp-old"],
                    "allow_subagents": False,
                    "max_active_subagents": None,
                    "context_usage": {},
                },
            )
        )
        session = store.get(ChatSession, SESSION)
        store.update(
            ChatSession,
            SESSION,
            {
                "metadata": {
                    **session.metadata,
                    "mcp_server_ids": ["mcp-old"],
                    "hook_ids": ["old-hook"],
                    "reasoning_effort": "low",
                    "allow_subagents": False,
                    "max_active_subagents": None,
                }
            },
            expected_revision=session.revision,
        )
        _patch(
            client,
            SESSION,
            mcp_server_ids=["mcp-new", "mcp-later-off"],
            hook_ids=["pre-commit"],
            reasoning_effort="medium",
            allow_subagents=True,
            max_active_subagents=3,
        )
        # Disabled after it was saved: dropped, not fatal.
        server = store.get(McpServerProfile, "mcp-later-off")
        store.update(
            McpServerProfile,
            server.id,
            {"enabled": False},
            expected_revision=server.revision,
        )
        captured: list[ChatCompletionRequest] = []

        async def capture(request: ChatCompletionRequest):
            captured.append(request)
            # Both goal paths treat a lost race as a quiet no-op.
            raise ChatHistoryConflict("captured before the provider")

        chat.prepare_async = capture  # type: ignore[method-assign]
        await chat._continue_running_goal_after_turn(
            SimpleNamespace(turn=old_turn, source_request=old)  # type: ignore[arg-type]
        )
        await chat.dispatch_running_goal(SESSION, "Resume work on the goal now.")
        _schedule(store)
        await chat.fire_due_schedules()

        # Continuation, dispatch and the scheduled occurrence, in that order.
        assert [request.goal_id for request in captured] == [goal.id] * 3
        assert [
            (
                request.mcp_server_ids,
                request.hook_ids,
                request.reasoning_effort,
                request.allow_subagents,
                request.max_active_subagents,
            )
            for request in captured
        ] == [(["mcp-new"], ["pre-commit"], "medium", True, 3)] * 3
        await chat.shutdown()

    asyncio.run(scenario())


def test_unverified_harness_send_keeps_the_saved_subagent_choice(tmp_path):
    async def scenario() -> None:
        store, project, harness, chat, _, runtime = _setup_harness(tmp_path)
        chat_session, _, first = _prepare_harness_turn(
            runtime, project, harness, "Hello.", setting=None
        )
        await runtime.start_chat_turn(first.id)
        client = TestClient(create_app(store, auth_token="test-token"))
        choice = {
            "provider_profile_id": "provider",
            "model": "model-unverified",
            "max_active": 2,
        }
        saved = _patch(
            client,
            chat_session.id,
            allow_subagents=True,
            subagent_provider_id="provider",
            subagent_model="model-unverified",
            max_active_subagents=2,
        )
        assert saved["provider_subagent"] == choice

        # Verification is still running, so the composer sends no subagent
        # model and the turn runs without subagent tools.
        _, chat_turn, unverified = _prepare_harness_turn(
            runtime,
            project,
            harness,
            "Keep going.",
            chat_id=chat_session.id,
            setting=None,
        )
        assert chat_turn.request_snapshot["provider_subagent"] is None
        vendor = store.get(HarnessSession, unverified.harness_session_id)
        assert "provider_subagent" not in vendor.metadata
        await runtime.start_chat_turn(unverified.id)
        assert (
            store.get(ChatSession, chat_session.id).metadata["provider_subagent"]
            == choice
        )

        # Unchecking still sticks through the next send.
        cleared = _patch(client, chat_session.id, allow_subagents=False)
        assert "provider_subagent" not in cleared
        _, _, off = _prepare_harness_turn(
            runtime,
            project,
            harness,
            "Alone now.",
            chat_id=chat_session.id,
            setting=None,
        )
        await runtime.start_chat_turn(off.id)
        assert not store.get(ChatSession, chat_session.id).metadata.get(
            "provider_subagent"
        )
        await chat.shutdown()

    asyncio.run(scenario())
