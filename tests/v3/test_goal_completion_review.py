"""Goals that hold an agent's own completion for closure review."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio

import pytest
from fastapi import FastAPI
from tests.v3.async_test_support import AsyncTestClient as TestClient

import nebula.v3.chat as chat_module
from nebula.v3.butler_board import BoardGoal, _goal_status_line
from nebula.v3.butler_oversight import project_stream_id
from nebula.v3.butler_precedent import _QUERIES
from nebula.v3.butler_review import build_review_packet, is_meaningful_event
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goal_tools import GOAL_COMPLETE_TOOL_NAME
from nebula.v3.chat_goals import (
    GoalCreate,
    GoalUpdate,
    GoalWrite,
    goals_router,
)
from nebula.v3.control_assistant_work import (
    CREATE_GOAL_SCHEMA,
    START_GOAL_CONVERSATION_SCHEMA,
    UPDATE_GOAL_SCHEMA,
)
from nebula.v3.domain import (
    DEFAULT_GOAL_RESUME_INSTRUCTION,
    GOAL_COMPLETION_PROPOSAL_KEY,
    GOAL_COMPLETION_REVIEW_REASON,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ToolChoice
from nebula.v3.storage import ConflictError
from tests.v3.test_chat import FakeProvider, _admitted_stream
from tests.v3.test_chat_goals import setup_goal


SUMMARY = "Recon handoff is complete."
EVIDENCE = "The requested artifact is saved."


class CompletingProvider(FakeProvider):
    """Calls goal.complete on its routing step, then answers."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        if request.tool_choice != ToolChoice.NONE:
            return ModelResponse(
                provider_id=self.config.id,
                model="model",
                tool_calls=[
                    {
                        "id": "complete-1",
                        "name": GOAL_COMPLETE_TOOL_NAME,
                        "arguments": {
                            "completion_summary": SUMMARY,
                            "completion_evidence": [EVIDENCE],
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
        return ModelResponse(
            provider_id=self.config.id,
            model="model",
            text="The handoff is ready for review.",
            finish_reason="stop",
        )


async def _run_provider_goal(tmp_path, monkeypatch, *, requires_review: bool):
    store, goals = await setup_goal(tmp_path)
    draft = await goals.create(
        "session",
        GoalCreate(
            objective="Complete the recon handoff",
            completion_criteria=["Save the requested artifact"],
            completion_requires_review=requires_review,
        ),
    )
    assert draft.completion_requires_review is requires_review
    running = await goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    profile = await store.get(ProviderProfile, "provider")
    capabilities = profile.capabilities.model_dump(mode="python")
    capabilities["tool_calling"] = True
    (
        await store.update(
            ProviderProfile,
            profile.id,
            {
                "capabilities": capabilities,
                "capability_verifications": {
                    "model": {"model": "model", "status": "verified"}
                },
            },
        )
    )
    provider = CompletingProvider("provider", local=True)
    provider.config.default_model = "model"
    provider.config.model_allowlist = ["model"]
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chat = ChatService(store, workspace_resolver=lambda _: workspace)
    prepared = await chat.prepare_async(
        ChatCompletionRequest(
            provider_id="provider",
            engagement_id="project",
            session_id="session",
            goal_id=running.id,
            messages=[{"role": "user", "content": "Finish the recon handoff"}],
            include_knowledge=False,
            stream=True,
        )
    )
    results = []
    async for event in _admitted_stream(chat, prepared):
        if (
            event[0] == "tool_completed"
            and event[1]["capability"] == GOAL_COMPLETE_TOOL_NAME
        ):
            results.append(event[1]["receipt"])
    # A held goal must not continue on its own.
    await asyncio.sleep(0.05)
    turns = await store.list_entities(ChatTurn, engagement_id="project")
    await chat.shutdown()
    return store, goals, prepared.turn.id, turns, results


async def test_native_completion_without_review_still_completes(tmp_path, monkeypatch):
    _, goals, turn_id, turns, results = await _run_provider_goal(
        tmp_path, monkeypatch, requires_review=False
    )
    goal = await goals.get("session")
    assert goal.status == ChatGoalStatus.COMPLETED
    assert goal.completion_summary == SUMMARY
    assert GOAL_COMPLETION_PROPOSAL_KEY not in goal.metadata
    assert len(turns) == 1
    assert results and results[-1]["state"] == "pending_final_answer"


async def test_native_completion_is_held_for_review_and_dispatches_no_turn(
    tmp_path, monkeypatch
):
    store, goals, turn_id, turns, results = await _run_provider_goal(
        tmp_path, monkeypatch, requires_review=True
    )
    goal = await goals.get("session")
    assert goal.status == ChatGoalStatus.BLOCKED
    assert goal.awaiting_completion_review
    assert goal.blocked_reason == GOAL_COMPLETION_REVIEW_REASON
    assert goal.completion_summary is None and goal.completion_evidence == []
    assert goal.execution_claim_id is None and goal.active_since is None
    proposal = goal.metadata[GOAL_COMPLETION_PROPOSAL_KEY]
    assert proposal["completion_summary"] == SUMMARY
    assert proposal["completion_evidence"] == [
        {"kind": "agent_reported", "detail": EVIDENCE, "turn_id": turn_id}
    ]
    assert proposal["turn_id"] == turn_id
    # The turn ends cleanly with its answer, and nothing runs after it.
    assert len(turns) == 1 and turns[0].status == ChatTurnStatus.COMPLETE
    assert results and results[-1]["state"] == "pending_closure_review"
    assert "closure review" in results[-1]["next_action"]
    # Butler's semantic review sees the request, with the agent's claim.
    events = await store.replay_operation_events(project_stream_id("project"))
    requested = [
        event
        for event in events
        if event.event_type == "goal.completion_review_requested"
    ]
    assert len(requested) == 1
    assert not any(event.event_type == "goal.blocked" for event in events)
    assert is_meaningful_event(requested[0])
    assert SUMMARY in requested[0].payload["summary"]
    packet = await build_review_packet(store, "project", requested[0])
    assert packet.goal_status == "blocked"
    assert SUMMARY in packet.event_summary
    assert "goal.completion_review_requested" in _QUERIES


async def test_harness_completion_is_held_for_review_and_dispatches_no_turn(
    tmp_path,
):
    from tests.v3.test_harnesses import _runtime

    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    session = await runtime.create_session(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        mcp_server_ids=[],
    )
    chat = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Goal chat",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            model=session.model,
        )
    )
    goal = await store.create(
        ChatGoal(
            engagement_id=engagement.id,
            session_id=chat.id,
            objective="Finish the saved research task",
            completion_criteria=["Record a result"],
            completion_requires_review=True,
            status=ChatGoalStatus.RUNNING,
            started_at=utc_now(),
            active_since=utc_now(),
        )
    )
    _, owner, harness_turn = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Continue the goal",
        chat_session_id=chat.id,
        goal_id=goal.id,
        harness_session_id=session.id,
        mcp_server_ids=[],
    )
    # What the gateway records once goal.complete is accepted.
    await store.update(
        ChatTurn,
        owner.id,
        {
            "request_snapshot": {
                **owner.request_snapshot,
                "goal_completion_proposal": {
                    "completion_summary": "Synthetic work finished",
                    "completion_evidence": ["Inert fixture returned its result"],
                },
            }
        },
    )
    await runtime._complete_owner(
        harness_turn,
        "Synthetic work is ready for review.",
        ChatTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        usage_reported=True,
    )
    await runtime._settle_goal_turn(harness_turn.id)
    await asyncio.sleep(0.05)
    saved = await store.get(ChatGoal, goal.id)
    assert saved.status == ChatGoalStatus.BLOCKED
    assert saved.awaiting_completion_review
    assert saved.blocked_reason == GOAL_COMPLETION_REVIEW_REASON
    proposal = saved.metadata[GOAL_COMPLETION_PROPOSAL_KEY]
    assert proposal["completion_summary"] == "Synthetic work finished"
    assert proposal["completion_evidence"] == [
        {
            "kind": "agent_reported",
            "detail": "Inert fixture returned its result",
            "turn_id": owner.id,
        }
    ]
    assert saved.execution_claim_id is None and saved.active_since is None
    assert saved.usage.total_tokens == 2
    assert (await store.get(ChatTurn, owner.id)).status == ChatTurnStatus.COMPLETE
    turns = await store.list_session_entities(ChatTurn, chat.id)
    assert [item.id for item in turns] == [owner.id]


async def _held_goal(tmp_path) -> tuple:
    store, goals = await setup_goal(tmp_path)
    draft = await goals.create(
        "session",
        GoalCreate(
            objective="Hold for review",
            completion_criteria=["Done"],
            completion_requires_review=True,
        ),
    )
    running = await goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    held = await store.update(
        ChatGoal,
        running.id,
        {
            "status": ChatGoalStatus.BLOCKED,
            "blocked_reason": GOAL_COMPLETION_REVIEW_REASON,
            "active_since": None,
            "metadata": {
                GOAL_COMPLETION_PROPOSAL_KEY: {
                    "completion_summary": SUMMARY,
                    "completion_evidence": [
                        {"kind": "agent_reported", "detail": EVIDENCE, "turn_id": "t"}
                    ],
                    "turn_id": "t",
                    "proposed_at": "2026-11-23T00:00:00+00:00",
                }
            },
        },
        expected_revision=running.revision,
    )
    return store, goals, held


async def test_operator_approval_completes_with_the_stored_proposal(tmp_path):
    store, goals, held = await _held_goal(tmp_path)
    # The ordinary block action still needs a running goal.
    with pytest.raises(ConflictError):
        await goals.write(
            "session",
            GoalWrite(expected_revision=held.revision, action="block", reason="x"),
        )
    approved = await goals.write(
        "session", GoalWrite(expected_revision=held.revision, action="complete")
    )
    assert approved.status == ChatGoalStatus.COMPLETED
    assert approved.blocked_reason is None
    assert approved.completion_summary == SUMMARY
    assert approved.completion_evidence[0]["kind"] == "agent_reported"
    assert GOAL_COMPLETION_PROPOSAL_KEY not in approved.metadata
    assert approved.metadata["completion_review"]["outcome"] == "approved"
    events = await store.replay_operation_events(project_stream_id("project"))
    completed = [event for event in events if event.event_type == "goal.completed"]
    assert len(completed) == 1 and is_meaningful_event(completed[0])
    assert SUMMARY in completed[0].payload["summary"]
    assert _goal_status_line(BoardGoal.of(held)) == "Awaiting closure review"


async def test_operator_send_back_resumes_with_feedback(tmp_path):
    store, goals, held = await _held_goal(tmp_path)
    dispatched: list[tuple[str, str]] = []

    async def dispatcher(session_id: str, instruction: str) -> str | None:
        dispatched.append((session_id, instruction))
        return None

    app = FastAPI()
    app.include_router(goals_router(store, goal_dispatcher=dispatcher))
    async with TestClient(app) as client:
        response = await client.post(
            "/chat/sessions/session/goal/actions",
            json={
                "expected_revision": held.revision,
                "action": "resume",
                "reason": "Criterion 2 has no saved artifact.",
            },
        )
        assert response.status_code == 200, response.text
    resumed = await goals.get("session")
    assert resumed.status == ChatGoalStatus.RUNNING
    assert resumed.blocked_reason is None
    assert not resumed.awaiting_completion_review
    assert GOAL_COMPLETION_PROPOSAL_KEY not in resumed.metadata
    review = resumed.metadata["completion_review"]
    assert review["outcome"] == "sent_back"
    assert review["proposal"]["completion_summary"] == SUMMARY
    [(session_id, instruction)] = dispatched
    assert session_id == "session"
    assert "sent this goal back" in instruction
    assert "Criterion 2 has no saved artifact." in instruction
    assert instruction.endswith(DEFAULT_GOAL_RESUME_INSTRUCTION)


async def test_review_choice_is_saved_on_update_and_kept_when_omitted(tmp_path):
    _, goals = await setup_goal(tmp_path)
    draft = await goals.create(
        "session", GoalCreate(objective="Edit me", completion_criteria=["Done"])
    )
    assert draft.completion_requires_review is False
    enabled = await goals.update(
        "session",
        GoalUpdate(
            expected_revision=draft.revision,
            objective="Edit me",
            completion_criteria=["Done"],
            completion_requires_review=True,
        ),
    )
    assert enabled.completion_requires_review is True
    kept = await goals.update(
        "session",
        GoalUpdate(
            expected_revision=enabled.revision,
            objective="Edited",
            completion_criteria=["Done"],
        ),
    )
    assert kept.completion_requires_review is True
    for schema in (
        CREATE_GOAL_SCHEMA,
        UPDATE_GOAL_SCHEMA,
        START_GOAL_CONVERSATION_SCHEMA,
    ):
        assert schema["properties"]["completion_requires_review"]["type"] == "boolean"
