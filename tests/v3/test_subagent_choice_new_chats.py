"""A new conversation starts with the Subagents choice the operator made.

Checking Subagents in the composer before a conversation exists has nothing to
save the choice on. The conversation is created later, either with a goal
(``POST /chat/goal-conversations``) or by the first message. Both paths dropped
the choice, so the box unchecked as soon as the new conversation appeared:

- a goal conversation saved the tools, MCP servers and hooks but not the
  subagent choice or reasoning effort, so the goal's first Core-started turn
  ran with ``allow_subagents=False``;
- a harness chat's first message leaves out a subagent model that is still
  being verified, so Core never heard of the choice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatHistoryConflict, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
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
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_harness_provider_subagents import _setup as _setup_harness
from tests.v3.test_subagent_choice_sticks import ToolProvider

HEADERS = {"Authorization": "Bearer test-token"}


def _setup(tmp_path: Path):
    store = NebulaStore(tmp_path / "new-chats.db")
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
    provider = ToolProvider()
    chat = ChatService(store, provider_factory=lambda _: provider, worker_id="worker")
    # Goal tool turns can publish a dashboard.
    chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
    client = TestClient(create_app(store, auth_token="test-token"))
    return store, chat, client


def _create_goal_conversation(client: TestClient, **composer) -> dict:
    response = client.post(
        "/api/v1/chat/goal-conversations",
        headers=HEADERS,
        json={
            "engagement_id": "project",
            "provider_id": "provider",
            "model": "model-a",
            "objective": "Split the review",
            "completion_criteria": ["Every area is reviewed"],
            "step_budget": 1,
            **composer,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["session"]


def _start_goal(store: NebulaStore, session_id: str) -> None:
    goals = ChatGoalService(store)
    goal = goals.get(session_id)
    goals.write(session_id, GoalWrite(expected_revision=goal.revision, action="start"))


async def _settled_turn(store: NebulaStore, session_id: str) -> ChatTurn:
    for _ in range(500):
        turns = store.list_session_entities(ChatTurn, session_id)
        if (
            len(turns) == 1
            and turns[0].status == ChatTurnStatus.COMPLETE
            and ChatGoalService(store).get(session_id).execution_claim_id is None
        ):
            return turns[0]
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"expected one settled goal turn, got {store.list_session_entities(ChatTurn, session_id)}"
    )


def test_goal_conversation_starts_with_the_composer_subagent_choice(tmp_path):
    async def scenario() -> None:
        store, chat, _client = _setup(tmp_path)
        # Subagents checked (at most 2 at once) and effort High in the
        # composer, then Add goal before any message.
        session = _create_goal_conversation(
            _client,
            allow_subagents=True,
            max_active_subagents=2,
            reasoning_effort="high",
        )
        # The composer re-reads the box from the new conversation.
        assert session["metadata"]["allow_subagents"] is True
        assert session["metadata"]["max_active_subagents"] == 2
        assert session["metadata"]["reasoning_effort"] == "high"

        _start_goal(store, session["id"])
        await chat.dispatch_running_goal(
            session["id"], "Begin work on the active conversation goal now."
        )

        turn = await _settled_turn(store, session["id"])
        assert turn.goal_id is not None
        assert turn.request_snapshot.get("allow_subagents") is True
        assert turn.request_snapshot.get("max_active_subagents") == 2
        assert turn.request_snapshot["model_request"]["reasoning_effort"] == "high"
        # The turn recorded its settings back without unchecking the box.
        saved = store.get(ChatSession, session["id"]).metadata
        assert saved["allow_subagents"] is True
        assert saved["max_active_subagents"] == 2
        assert saved["reasoning_effort"] == "high"
        await chat.shutdown()

    asyncio.run(scenario())


def test_goal_first_turn_sends_every_composer_setting(tmp_path):
    async def scenario() -> None:
        store, chat, client = _setup(tmp_path)
        store.create(
            McpServerProfile(
                id="mcp-docs",
                name="Docs",
                transport=McpTransport.STREAMABLE_HTTP,
                url="http://127.0.0.1:1/mcp",
                enabled=True,
            )
        )
        session = _create_goal_conversation(
            client,
            tools_enabled=True,
            mcp_server_ids=["mcp-docs"],
            hook_ids=["pre-commit"],
            allow_subagents=True,
            max_active_subagents=3,
            reasoning_effort="medium",
        )
        _start_goal(store, session["id"])
        captured: list[ChatCompletionRequest] = []

        async def capture(request: ChatCompletionRequest):
            captured.append(request)
            # Dispatch treats a lost race as a quiet no-op.
            raise ChatHistoryConflict("captured before the provider")

        chat.prepare_async = capture  # type: ignore[method-assign]
        await chat.dispatch_running_goal(
            session["id"], "Begin work on the active conversation goal now."
        )

        (request,) = captured
        assert (
            request.tools_enabled,
            request.mcp_server_ids,
            request.hook_ids,
            request.allow_subagents,
            request.max_active_subagents,
            request.reasoning_effort,
        ) == (True, ["mcp-docs"], ["pre-commit"], True, 3, "medium")
        await chat.shutdown()

    asyncio.run(scenario())


def test_goal_conversation_refuses_an_out_of_range_subagent_limit(tmp_path):
    store, _, client = _setup(tmp_path)
    response = client.post(
        "/api/v1/chat/goal-conversations",
        headers=HEADERS,
        json={
            "engagement_id": "project",
            "provider_id": "provider",
            "model": "model-a",
            "objective": "Split the review",
            "completion_criteria": ["Every area is reviewed"],
            "allow_subagents": True,
            "max_active_subagents": 0,
        },
    )
    assert response.status_code == 422, response.text
    assert store.list_entities(ChatSession) == []


def _sse_frames(text: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]


def test_new_harness_chat_remembers_a_subagent_model_still_being_verified(
    tmp_path,
):
    store, project, harness, _, _, runtime = _setup_harness(tmp_path)
    client = TestClient(
        create_app(store, auth_token="test-token", harness_runtime_service=runtime)
    )
    choice = {
        "provider_profile_id": "provider",
        "model": "model-unverified",
        "max_active": 2,
    }

    # Subagents is checked on a model that has not passed the tool check yet:
    # this first message cannot use it, but carries the choice to remember.
    response = client.post(
        "/api/v1/chat/completions",
        headers=HEADERS,
        json={
            "backend": "harness",
            "engagement_id": project.id,
            "harness_profile_id": harness.id,
            "mcp_server_ids": [],
            "stream": True,
            "messages": [{"role": "user", "content": "Hello."}],
            "pending_provider_subagent": choice,
        },
    )

    assert response.status_code == 200, response.text
    frames = _sse_frames(response.text)
    assert frames[0]["type"] == "started"
    assert frames[-1]["type"] == "done", frames[-1]
    chat_session = store.get(ChatSession, frames[0]["session_id"])
    # The new conversation holds the choice, so the box stays checked ...
    assert chat_session.metadata["provider_subagent"] == choice
    listed = client.get(
        "/api/v1/chat-sessions",
        headers=HEADERS,
        params={"engagement_id": project.id},
    )
    assert listed.status_code == 200, listed.text
    (summary,) = listed.json()
    assert summary["metadata"]["provider_subagent"] == choice
    # ... while this turn ran without subagent tools, exactly as before.
    (turn,) = store.list_session_entities(ChatTurn, chat_session.id)
    assert turn.request_snapshot["provider_subagent"] is None
    vendor = store.get(HarnessSession, chat_session.harness_session_id or "")
    assert "provider_subagent" not in vendor.metadata
