"""Core-owned goals on durable harness chats, using an in-process vendor fixture."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    update_chat_assistant_config,
)
from nebula.v3.chat_goals import (
    ChatGoalService,
    GoalConversationCreate,
    GoalCreate,
    GoalUpdate,
    GoalWrite,
)
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_queue import ChatQueueService, QueueWrite
from nebula.v3.domain import (
    ChatBackend,
    ChatGoalStatus,
    ChatMessage,
    ChatQueue,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
    ToolCall,
    utc_now,
)
from nebula.v3.harnesses import (
    HarnessConfigurationError,
    HarnessEvent,
    HarnessGoalSnapshot,
)
from nebula.v3.storage import ConflictError
from tests.v3.test_harnesses import FakeConnection, _runtime


async def _wait_for_goal(
    goals: ChatGoalService, session_id: str, status: ChatGoalStatus
) -> None:
    for _ in range(300):
        if goals.get(session_id).status == status:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"goal did not reach {status.value}")


def _new_goal(
    goals: ChatGoalService,
    *,
    step_budget: int | None = 2,
    token_budget: int | None = None,
):
    return goals.create_conversation(
        GoalConversationCreate(
            engagement_id="eng-a",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-a",
            model="test-model",
            tools_enabled=False,
            objective="Investigate the project and retain evidence",
            completion_criteria=["The result has evidence"],
            step_budget=step_budget,
            token_budget=token_budget,
        )
    )


@pytest.mark.parametrize(
    "kind",
    [HarnessKind.CODEX_APP_SERVER, HarnessKind.GROK_ACP, HarnessKind.CLAUDE_AGENT_SDK],
)
def test_goal_conversation_is_atomic_and_supported_for_each_harness(tmp_path, kind):
    store, _engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
    if kind != profile.kind:
        profile = store.update(
            HarnessProfile,
            profile.id,
            {"kind": kind},
            expected_revision=profile.revision,
        )
    store.update(
        HarnessProfile,
        profile.id,
        {
            "capabilities": profile.capabilities.model_copy(
                update={"models": ["test-model"]}
            )
        },
        expected_revision=profile.revision,
    )
    goals = ChatGoalService(store, runtime)
    created = _new_goal(goals)
    session = created.session
    harness_session = store.get(HarnessSession, session.harness_session_id)
    assert session.backend == ChatBackend.HARNESS
    assert harness_session.harness_profile_id == profile.id
    assert harness_session.external_session_id is None
    assert goals.get(session.id).id == created.goal.id
    assert store.list_session_entities(ChatMessage, session.id) == []
    assert store.list_session_entities(ChatTurn, session.id) == []
    with pytest.raises(HarnessConfigurationError):
        goals.create_conversation(
            GoalConversationCreate(
                engagement_id="eng-a",
                backend=ChatBackend.HARNESS,
                harness_profile_id="harness-a",
                model="unadvertised",
                objective="Rejected",
                completion_criteria=["No partial records"],
            )
        )
    assert len(store.list_entities(ChatSession)) == 1
    assert len(store.list_entities(HarnessSession)) == 1


def test_existing_harness_chat_can_add_goal_and_child_gets_fresh_session(tmp_path):
    store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
    vendor_session = runtime.create_session(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model="test-model",
        tools_enabled=False,
    )
    chat = store.create(
        ChatSession(
            engagement_id=engagement.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=vendor_session.id,
            model="test-model",
            title="Existing project harness chat",
        )
    )
    goals = ChatGoalService(store, runtime)
    parent = goals.create(
        chat.id,
        GoalCreate(
            objective="Trace the audio path",
            completion_criteria=["Retain a trace"],
            child_budget=1,
        ),
    )
    parent = goals.write(
        chat.id, GoalWrite(expected_revision=parent.revision, action="start")
    )
    child = goals.start_child(
        chat.id,
        GoalCreate(
            objective="Inspect packet bounds",
            completion_criteria=["Record the bounds"],
        ),
    )
    child_chat = store.get(ChatSession, child.session_id)
    child_vendor = store.get(HarnessSession, child_chat.harness_session_id)
    assert child.parent_goal_id == parent.id
    assert child.status == ChatGoalStatus.DRAFT
    assert child_chat.parent_session_id == chat.id
    assert child_chat.engagement_id == chat.engagement_id
    assert child_vendor.id != vendor_session.id
    assert child_vendor.external_session_id is None
    assert child_vendor.metadata["forked_from_session_id"] == vendor_session.id
    assert store.list_session_entities(ChatMessage, child_chat.id) == []
    assert store.list_session_entities(ChatTurn, child_chat.id) == []
    assert goals.get(chat.id).children_started == 1


def test_harness_goal_continues_and_pauses_at_step_limit_without_self_completing(
    tmp_path,
):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals)
        started = goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        first = await runtime.dispatch_running_goal(
            created.session.id, "Begin the bounded goal"
        )
        assert first
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        goal = goals.get(created.session.id)
        turns = store.list_session_entities(ChatTurn, created.session.id)
        assert [turn.goal_id for turn in turns] == [started.id, started.id]
        assert all(turn.status == ChatTurnStatus.COMPLETE for turn in turns)
        assert goal.current_step == 2
        assert goal.usage.total_tokens == 18
        assert goal.blocked_reason == "Goal step budget is exhausted."
        assert goal.completion_summary is None
        assert len(adapter.connections) == 1
        assert "Core-owned Nebula goal" in adapter.connections[0].prompts[0]
        assert len(adapter.connections[0].prompts) == 2
        await runtime.shutdown()

    asyncio.run(scenario())


def test_harness_goal_agent_can_block_only_its_active_core_goal(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=None)
        other = _new_goal(goals, step_budget=None)
        for item in (created, other):
            goals.write(
                item.session.id,
                GoalWrite(expected_revision=item.goal.revision, action="start"),
            )
        observed: dict = {}

        class BlockingConnection(FakeConnection):
            async def run_turn(
                self, prompt: str, *, model: str, images=None
            ) -> AsyncIterator[HarnessEvent]:
                self.prompts.append(prompt)
                yield HarnessEvent(type="started", external_session_id="blocker")
                session = self.request.session
                observed["tools"] = {
                    tool["name"] for tool in runtime._gateway_catalog(session)["tools"]
                }
                observed["cross_goal"] = await runtime._gateway_call(
                    session,
                    "goal.block",
                    {"reason": "Missing worker route", "goal_id": other.goal.id},
                )
                observed["other_before"] = goals.get(other.session.id).status
                observed["blank"] = await runtime._gateway_call(
                    session, "goal.block", {"reason": "   "}
                )
                observed["blocked"] = await runtime._gateway_call(
                    session, "goal.block", {"reason": "Missing worker route"}
                )
                yield HarnessEvent(
                    type="completed", message="Waiting for the worker route"
                )

        async def open_blocking(request):
            connection = BlockingConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_blocking
        assert await runtime.dispatch_running_goal(created.session.id, "Investigate")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.BLOCKED)
        for _ in range(300):
            if not runtime._chat_turn_tasks:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the blocked turn did not settle")
        assert "goal.block" in observed["tools"]
        assert observed["cross_goal"]["isError"] is True
        assert observed["blank"]["isError"] is True
        assert observed["other_before"] == ChatGoalStatus.RUNNING
        assert observed["blocked"]["isError"] is False
        assert observed["blocked"]["structuredContent"]["status"] == "blocked"
        assert goals.get(created.session.id).blocked_reason == "Missing worker route"
        assert goals.get(other.session.id).status == ChatGoalStatus.RUNNING
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 1
        calls = store.list_session_entities(ToolCall, created.session.id)
        assert [call.tool_name for call in calls] == ["goal.block"]
        assert (
            await runtime.dispatch_running_goal(created.session.id, "Automatic") is None
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_tool_free_harness_goal_continuations_pause_for_review(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=None)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        assert await runtime.dispatch_running_goal(created.session.id, "Begin work")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        goal = goals.get(created.session.id)
        assert goal.current_step == 4
        assert "Three automatic goal responses" in (goal.blocked_reason or "")
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 4
        assert len(adapter.connections[0].prompts) == 4
        assert await runtime.reconcile_idle_running_goals() == []
        assert await runtime.reconcile_idle_running_goals() == []
        await runtime.shutdown()

    asyncio.run(scenario())


def test_native_tool_activity_resets_harness_goal_continuation_guard(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=None)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )

        class ToolConnection(FakeConnection):
            async def run_turn(
                self, prompt: str, *, model: str, images=None
            ) -> AsyncIterator[HarnessEvent]:
                self.prompts.append(prompt)
                yield HarnessEvent(type="started", external_session_id="native-tool")
                if len(self.prompts) == 3:
                    yield HarnessEvent(
                        type="item_upsert",
                        item_id="command-1",
                        item_kind="command",
                        item_status="completed",
                        title="Inspect inputs",
                    )
                yield HarnessEvent(type="completed", message="Saved analysis")

        async def open_tool(request):
            connection = ToolConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_tool
        assert await runtime.dispatch_running_goal(created.session.id, "Begin work")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert goals.get(created.session.id).current_step == 6
        assert len(adapter.connections[0].prompts) == 6
        await runtime.shutdown()

    asyncio.run(scenario())


def test_saved_harness_effort_applies_to_goal_resume_and_continuation(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        store.update(
            HarnessProfile,
            profile.id,
            {
                "capabilities": profile.capabilities.model_copy(
                    update={
                        "models": ["test-model"],
                        "model_options": [
                            HarnessModelOptions(
                                model="test-model",
                                reasoning_efforts=[
                                    HarnessRuntimeOption(id="medium", label="Medium"),
                                    HarnessRuntimeOption(id="max", label="Max"),
                                ],
                                default_reasoning_effort="medium",
                            )
                        ],
                    }
                )
            },
            expected_revision=profile.revision,
        )
        goals = ChatGoalService(store, runtime)
        created = goals.create_conversation(
            GoalConversationCreate(
                engagement_id=engagement.id,
                backend=ChatBackend.HARNESS,
                harness_profile_id=profile.id,
                model="test-model",
                harness_reasoning_effort="medium",
                objective="Review the bounded research result",
                completion_criteria=["The result has evidence"],
                step_budget=2,
            )
        )
        session_id = created.session.id
        old_harness_id = created.session.harness_session_id
        started = goals.write(
            session_id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        blocked = goals.write(
            session_id,
            GoalWrite(
                expected_revision=started.revision,
                action="block",
                reason="Waiting for a settings change",
            ),
        )
        chat = store.get(ChatSession, session_id)
        update_chat_assistant_config(
            store,
            chat,
            ChatAssistantConfigPatch(harness_reasoning_effort="max"),
            expected_revision=chat.revision,
        )
        assert goals.get(session_id).status == ChatGoalStatus.BLOCKED
        assert store.get(ChatSession, session_id).harness_session_id == old_harness_id
        assert (
            store.get(HarnessSession, old_harness_id).metadata["runtime_options"][
                "reasoning_effort"
            ]
            == "medium"
        )

        goals.write(
            session_id,
            GoalWrite(expected_revision=blocked.revision, action="resume"),
        )
        assert await runtime.dispatch_running_goal(session_id, "Resume this goal")
        await _wait_for_goal(goals, session_id, ChatGoalStatus.PAUSED)
        chat = store.get(ChatSession, session_id)
        assert chat.harness_session_id != old_harness_id
        assert (
            store.get(HarnessSession, chat.harness_session_id).metadata[
                "runtime_options"
            ]["reasoning_effort"]
            == "max"
        )
        turns = store.list_session_entities(ChatTurn, session_id)
        assert len(turns) == 2
        assert all(turn.goal_id == created.goal.id for turn in turns)
        assert [
            turn.request_snapshot["harness_runtime_options"]["reasoning_effort"]
            for turn in turns
        ] == ["max", "max"]
        await runtime.shutdown()

    asyncio.run(scenario())


def test_native_goal_status_survives_turn_end_beside_core_goal(tmp_path):
    class NativeGoalConnection(FakeConnection):
        async def run_turn(
            self, prompt: str, *, model: str, images=None
        ) -> AsyncIterator[HarnessEvent]:
            self.prompts.append(prompt)
            yield HarnessEvent(type="started", external_session_id="vendor-native-goal")
            yield HarnessEvent(
                type="item_upsert",
                item_id="native-goal",
                item_kind="goal",
                item_status="completed",
                title="Native goal",
                goal=HarnessGoalSnapshot(objective="Harness review", status="complete"),
            )
            yield HarnessEvent(type="completed", message="Native review finished")

    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)

        async def open_native(request):
            connection = NativeGoalConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_native
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=1)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        await runtime.dispatch_running_goal(
            created.session.id, "Complete a native review"
        )
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        native = runtime.session_activity(created.session.harness_session_id)
        assert native.goal is not None
        assert native.goal.status == "complete"
        assert native.goal.objective == "Harness review"
        assert goals.get(created.session.id).completion_summary is None
        await runtime.shutdown()

    asyncio.run(scenario())


def test_competing_dispatches_claim_only_one_harness_goal_turn(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=1)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        results = await asyncio.gather(
            *[
                runtime.dispatch_running_goal(created.session.id, "Begin once")
                for _ in range(2)
            ]
        )
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert len([result for result in results if result]) == 1
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 1
        assert len(store.list_session_entities(ChatMessage, created.session.id)) == 2
        await runtime.shutdown()

    asyncio.run(scenario())


def test_queued_operator_follow_up_takes_priority_over_goal_dispatch(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=1)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        queue = store.create(
            ChatQueue(
                id="chat-queue-" + created.session.id,
                engagement_id=created.session.engagement_id,
                session_id=created.session.id,
                items=[{"id": "follow-up", "status": "queued"}],
            )
        )
        assert (
            await runtime.dispatch_running_goal(created.session.id, "Automatic work")
            is None
        )
        assert await runtime.reconcile_idle_running_goals() == []
        assert await runtime.reconcile_idle_running_goals() == []
        assert store.list_session_entities(ChatTurn, created.session.id) == []
        store.update(
            ChatQueue, queue.id, {"items": []}, expected_revision=queue.revision
        )
        assert await runtime.reconcile_idle_running_goals() == []
        assert await runtime.reconcile_idle_running_goals() == [created.goal.id]
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_reviewed_follow_up_releases_completed_harness_goal_handoff(tmp_path):
    async def scenario() -> None:
        store, _engagement, profile, _mcp, adapter, runtime = _runtime(tmp_path)
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        class WaitingConnection(FakeConnection):
            async def run_turn(
                self, prompt: str, *, model: str, images=None
            ) -> AsyncIterator[HarnessEvent]:
                async for event in super().run_turn(prompt, model=model, images=images):
                    yield event
                    if event.type == "started" and len(self.prompts) == 1:
                        first_started.set()
                        await release_first.wait()

        async def open_waiting(request):
            connection = WaitingConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_waiting
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=2)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        assert await runtime.dispatch_running_goal(created.session.id, "First work")
        await asyncio.wait_for(first_started.wait(), timeout=5)

        queue_service = ChatQueueService(store, None, runtime)
        queued = queue_service.write(
            created.session.id,
            QueueWrite(
                action="enqueue",
                expected_revision=0,
                idempotency_key="while-first-turn-runs",
                request=ChatCompletionRequest(
                    backend="harness",
                    harness_profile_id=profile.id,
                    engagement_id=created.session.engagement_id,
                    session_id=created.session.id,
                    model=created.session.model,
                    messages=[{"role": "user", "content": "Queued operator follow-up"}],
                    include_knowledge=False,
                ),
            ),
        )
        release_first.set()
        for _ in range(300):
            turns = store.list_session_entities(ChatTurn, created.session.id)
            if (
                turns
                and turns[0].status == ChatTurnStatus.COMPLETE
                and not runtime._chat_turn_tasks
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("first harness goal turn did not settle")
        assert goals.get(created.session.id).status == ChatGoalStatus.RUNNING
        assert len(turns) == 1
        assert queue_service.get(created.session.id).items[0]["status"] == "queued"

        # A successful turn updates profile health evidence and its revision.
        # The queued operator request must be reviewed, while the goal resumes.
        assert (
            store.get(HarnessProfile, profile.id).revision
            > queued.items[0]["profile_revision"]
        )
        await queue_service.step(queue_service.get(created.session.id))
        reviewed = queue_service.get(created.session.id)
        assert reviewed.paused
        assert reviewed.items[0]["status"] == "needs_review"
        assert await runtime.reconcile_idle_running_goals() == []
        assert await runtime.reconcile_idle_running_goals() == [created.goal.id]
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 2
        assert len(adapter.connections[0].prompts) == 2
        assert "Queued operator follow-up" not in adapter.connections[0].prompts[1]
        assert (
            queue_service.get(created.session.id).items[0]["status"] == "needs_review"
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_late_subagent_report_resumes_parked_harness_goal_once(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, _mcp, adapter, runtime = _runtime(tmp_path)
        chat = ChatService(store)
        runtime.bind_provider_subagents(chat.subagents)
        chat.subagents.bind_harness_runtime(runtime)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        _session, parent_turn, harness_turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model="test-model",
            prompt="Delegate and wait for a report",
            chat_session_id=created.session.id,
            goal_id=created.goal.id,
            harness_session_id=None,
            mcp_server_ids=None,
            include_knowledge=False,
        )
        child_chat = store.create(
            ChatSession(
                engagement_id=engagement.id,
                backend=ChatBackend.PROVIDER,
                provider_profile_id="provider-fixture",
                model="fixture",
                parent_session_id=created.session.id,
                title="Child report",
            )
        )
        subagent = store.create(
            ChatSubagent(
                engagement_id=engagement.id,
                parent_session_id=created.session.id,
                parent_turn_id=parent_turn.id,
                parent_backend=ChatBackend.HARNESS,
                child_session_id=child_chat.id,
                name="Packet reviewer",
                task="Inspect bounds",
            )
        )
        runtime.start_chat_turn(harness_turn.id)
        for _ in range(300):
            if (
                store.get(ChatTurn, parent_turn.id).status == ChatTurnStatus.COMPLETE
                and not runtime._chat_turn_tasks
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("parent harness turn did not settle")
        assert goals.get(created.session.id).status == ChatGoalStatus.RUNNING
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 1
        subagent = store.update(
            ChatSubagent,
            subagent.id,
            {
                "status": ChatSubagentStatus.COMPLETED,
                "finished_at": utc_now(),
                "result": "Packet lengths were checked.",
            },
            expected_revision=subagent.revision,
        )
        await chat.subagents.deliver_pending(created.session.id)
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 2
        assert store.get(ChatSubagent, subagent.id).result_message_id is not None
        assert "Subagent reports are ready" in adapter.connections[0].prompts[-1]
        await chat.subagents.deliver_pending(created.session.id)
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 2
        await runtime.shutdown()
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("emit_zero_usage", [False, True])
def test_harness_token_limit_requires_reported_usage_and_pauses_when_missing(
    tmp_path, emit_zero_usage
):
    class NoUsageConnection(FakeConnection):
        async def run_turn(
            self, prompt: str, *, model: str, images=None
        ) -> AsyncIterator[HarnessEvent]:
            self.prompts.append(prompt)
            yield HarnessEvent(type="started", external_session_id="vendor-no-usage")
            if emit_zero_usage:
                yield HarnessEvent(type="usage", usage=ChatTokenUsage())
            yield HarnessEvent(type="completed", message="A saved answer without usage")

    async def scenario() -> None:
        store, _engagement, profile, _mcp, adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        with pytest.raises(ConflictError, match="does not report token usage"):
            _new_goal(goals, token_budget=20)
        profile = store.get(HarnessProfile, profile.id)
        store.update(
            HarnessProfile,
            profile.id,
            {
                "capabilities": profile.capabilities.model_copy(
                    update={"detailed_usage": True}
                )
            },
            expected_revision=profile.revision,
        )

        async def open_without_usage(request):
            connection = NoUsageConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_without_usage
        created = _new_goal(goals, token_budget=20)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        await runtime.dispatch_running_goal(
            created.session.id, "Work within the token limit"
        )
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        goal = goals.get(created.session.id)
        assert goal.metadata["usage_unknown"] is True
        assert "usage was unavailable" in (goal.blocked_reason or "")
        with pytest.raises(ConflictError, match="remove the token limit"):
            goals.write(
                created.session.id,
                GoalWrite(expected_revision=goal.revision, action="resume"),
            )
        updated = goals.update(
            created.session.id,
            GoalUpdate(
                expected_revision=goal.revision,
                objective=goal.objective,
                completion_criteria=goal.completion_criteria,
                plan=goal.plan,
                step_budget=goal.step_budget,
                token_budget=None,
            ),
        )
        assert updated.token_budget is None
        await runtime.shutdown()

    asyncio.run(scenario())


def test_failed_harness_turn_pauses_goal_until_explicit_resume(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, _adapter, runtime = _runtime(
            tmp_path, fail=True
        )
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        await runtime.dispatch_running_goal(created.session.id, "Try once")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 1
        assert (
            store.list_session_entities(ChatTurn, created.session.id)[0].status
            != ChatTurnStatus.COMPLETE
        )
        goal = goals.get(created.session.id)
        assert "stopped or failed" in (goal.blocked_reason or "")
        await runtime.shutdown()

    asyncio.run(scenario())


def test_operator_interruption_pauses_harness_goal(tmp_path):
    class BlockingConnection(FakeConnection):
        async def run_turn(
            self, prompt: str, *, model: str, images=None
        ) -> AsyncIterator[HarnessEvent]:
            self.prompts.append(prompt)
            yield HarnessEvent(type="started", external_session_id="vendor-blocked")
            await asyncio.Event().wait()

    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = _runtime(tmp_path)

        async def open_blocked(request):
            connection = BlockingConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_blocked
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        await runtime.dispatch_running_goal(
            created.session.id, "Start interruptible work"
        )
        for _ in range(300):
            if adapter.connections:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("harness connection did not start")
        turn = store.list_session_entities(ChatTurn, created.session.id)[0]
        await runtime.cancel_turn(turn.harness_turn_id, reason="Operator stop")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        assert store.get(HarnessTurn, turn.harness_turn_id).status in {
            HarnessTurnStatus.CANCELLED,
            HarnessTurnStatus.INTERRUPTED,
        }
        assert len(store.list_session_entities(ChatTurn, created.session.id)) == 1
        await runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("initial_kind", "next_kind"),
    [
        (HarnessKind.CODEX_APP_SERVER, HarnessKind.GROK_ACP),
        (HarnessKind.GROK_ACP, HarnessKind.CODEX_APP_SERVER),
    ],
)
def test_goal_resume_uses_saved_next_harness_in_same_chat(
    tmp_path, initial_kind, next_kind
):
    async def scenario() -> None:
        store, _engagement, profile, _mcp, adapter, runtime = _runtime(tmp_path)
        if profile.kind != initial_kind:
            profile = store.update(
                HarnessProfile,
                profile.id,
                {"kind": initial_kind},
                expected_revision=profile.revision,
            )
        next_profile = store.create(
            HarnessProfile(
                id="harness-next",
                name="Next harness fixture",
                kind=next_kind,
                executable="/bin/true",
                default_model="next-model",
                capabilities=profile.capabilities.model_copy(
                    update={"models": ["next-model"]}
                ),
                privacy=profile.privacy,
            )
        )
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=1)
        session_id = created.session.id
        old_harness_id = created.session.harness_session_id
        started = goals.write(
            session_id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        blocked = goals.write(
            session_id,
            GoalWrite(
                expected_revision=started.revision,
                action="block",
                reason="Switch to an available harness",
            ),
        )
        chat = store.get(ChatSession, session_id)
        saved = update_chat_assistant_config(
            store,
            chat,
            ChatAssistantConfigPatch(
                backend=ChatBackend.HARNESS,
                profile_id=next_profile.id,
                model="next-model",
            ),
            expected_revision=chat.revision,
        )
        assert saved.harness_session_id == old_harness_id
        assert saved.harness_profile_id == profile.id

        goals.write(
            session_id,
            GoalWrite(expected_revision=blocked.revision, action="resume"),
        )
        turn_id = await runtime.dispatch_running_goal(session_id, "Resume this goal")
        assert turn_id
        await _wait_for_goal(goals, session_id, ChatGoalStatus.PAUSED)
        chat = store.get(ChatSession, session_id)
        turn = store.get(ChatTurn, turn_id)
        harness_session = store.get(HarnessSession, chat.harness_session_id)
        assert chat.id == session_id
        assert chat.harness_session_id != old_harness_id
        assert chat.harness_profile_id == next_profile.id
        assert chat.model == "next-model"
        assert harness_session.harness_profile_id == next_profile.id
        assert turn.goal_id == created.goal.id
        assert turn.harness_turn_id
        assert adapter.connections[0].request.profile.id == next_profile.id
        await runtime.shutdown()

    asyncio.run(scenario())


def test_goal_survives_harness_session_rollover_with_one_transcript(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals, step_budget=1)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        await runtime.dispatch_running_goal(created.session.id, "First model")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        first_session = store.get(ChatSession, created.session.id).harness_session_id
        paused = goals.get(created.session.id)
        expanded = goals.update(
            created.session.id,
            GoalUpdate(
                expected_revision=paused.revision,
                objective=paused.objective,
                completion_criteria=paused.completion_criteria,
                plan=paused.plan,
                step_budget=2,
            ),
        )
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=expanded.revision, action="resume"),
        )
        _chat, second, harness_turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model="alternate-model",
            prompt="Continue after model change",
            chat_session_id=created.session.id,
            goal_id=created.goal.id,
            harness_session_id=None,
            mcp_server_ids=None,
            include_knowledge=False,
        )
        runtime.start_chat_turn(harness_turn.id)
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.PAUSED)
        latest_session = store.get(ChatSession, created.session.id)
        assert latest_session.harness_session_id != first_session
        assert latest_session.model == "alternate-model"
        assert second.goal_id == created.goal.id
        assert [
            turn.goal_id
            for turn in store.list_session_entities(ChatTurn, created.session.id)
        ] == [created.goal.id, created.goal.id]
        assert len(store.list_session_entities(ChatMessage, created.session.id)) == 4
        await runtime.shutdown()

    asyncio.run(scenario())


def test_restart_interrupts_queued_turn_and_pauses_goal(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = _new_goal(goals)
        goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        _chat, turn, harness_turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model="test-model",
            prompt="Accepted before restart",
            chat_session_id=created.session.id,
            goal_id=created.goal.id,
            harness_session_id=None,
            mcp_server_ids=None,
            include_knowledge=False,
        )
        assert goals.get(created.session.id).execution_claim_id == turn.id
        await runtime.startup()
        assert (
            store.get(HarnessTurn, harness_turn.id).status
            == HarnessTurnStatus.INTERRUPTED
        )
        assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.INTERRUPTED
        goal = goals.get(created.session.id)
        assert goal.status == ChatGoalStatus.PAUSED
        assert goal.execution_claim_id is None
        assert "restarted" in (goal.blocked_reason or "")
        await runtime.shutdown()

    asyncio.run(scenario())


def test_harness_goal_time_limit_is_enforced_before_dispatch(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, _adapter, runtime = _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = goals.create_conversation(
            GoalConversationCreate(
                engagement_id="eng-a",
                backend=ChatBackend.HARNESS,
                harness_profile_id="harness-a",
                model="test-model",
                tools_enabled=False,
                objective="Bounded work",
                completion_criteria=["Evidence retained"],
                time_budget_seconds=1800,
            )
        )
        running = goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        store.update(
            type(running),
            running.id,
            {"active_since": utc_now() - timedelta(seconds=1801)},
            expected_revision=running.revision,
        )
        assert (
            await runtime.dispatch_running_goal(created.session.id, "Too late") is None
        )
        assert (
            goals.get(created.session.id).blocked_reason
            == "Goal active-time budget is exhausted."
        )
        assert store.list_session_entities(ChatTurn, created.session.id) == []
        await runtime.shutdown()

    asyncio.run(scenario())
