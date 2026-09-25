"""Subagent lifecycle: who stops a child, what it inherits, what it costs.

Each test pins one operator-visible rule: a child never outlives a parent
response that ended without finishing, never waits forever on a decision
nobody can make, never reaches a host its parent was not given, never spends
past its goal's token budget, and never reports into a conversation the
operator edited. A goal's active time counts only the time Nebula works.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
import nebula.v3.chat_goals as chat_goals_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatGoalUsageCharge,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessageDirection,
    ChatSubagentMessageStatus,
    ChatSubagentStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    SshEnvironment,
)
from nebula.v3.providers import ModelRequest, ModelResponse, ModelUsage, ToolCall
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments, ToolCallOrigin, ToolInvocation
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _drain,
    _messages,
    _request,
    _response,
    _setup,
    _until,
)


def _session(store: NebulaStore, *, title: str = "Supervisor", **fields) -> ChatSession:
    return store.create(
        ChatSession(
            engagement_id="project",
            title=title,
            provider_profile_id="provider",
            model="model-a",
            **fields,
        )
    )


def _turn(store: NebulaStore, session: ChatSession, **fields) -> ChatTurn:
    return store.create(
        ChatTurn(
            engagement_id="project",
            session_id=session.id,
            provider_profile_id="provider",
            model="model-a",
            **fields,
        )
    )


def _child(
    store: NebulaStore,
    parent_session: ChatSession,
    parent_turn: ChatTurn,
    *,
    child_status: ChatTurnStatus = ChatTurnStatus.ROUTING,
    **fields,
) -> ChatSubagent:
    """A running subagent whose child turn has no live task."""

    record_id = fields.pop("id", "child-record")
    child_session = _session(
        store,
        title="Subagent",
        parent_session_id=parent_session.id,
        metadata={"subagent_id": record_id},
    )
    child_turn = _turn(
        store,
        child_session,
        status=child_status,
        request_snapshot={"subagent_child": True},
        **fields.pop("child_turn_fields", {}),
    )
    return store.create(
        ChatSubagent(
            id=record_id,
            engagement_id="project",
            parent_session_id=parent_session.id,
            parent_turn_id=parent_turn.id,
            child_session_id=child_session.id,
            child_turn_id=child_turn.id,
            provider_profile_id="provider",
            model="model-a",
            name="Worker",
            task="Do the delegated work.",
            **fields,
        )
    )


def _usage(total: int) -> ModelUsage:
    return ModelUsage(input_tokens=total - 1, output_tokens=1, total_tokens=total)


def _spending_call(call_id: str, tool: str, total: int, **arguments) -> ModelResponse:
    return ModelResponse(
        provider_id="provider",
        model="model-a",
        tool_calls=[ToolCall(id=call_id, name=tool, arguments=arguments)],
        usage=_usage(total),
        finish_reason="tool_calls",
    )


def _running_goal(
    store: NebulaStore, session: ChatSession, **budgets
) -> tuple[ChatGoalService, ChatGoal]:
    goals = ChatGoalService(store)
    draft = goals.create(
        session.id,
        GoalCreate(
            objective="Finish the audit", completion_criteria=["Done"], **budgets
        ),
    )
    return goals, goals.write(
        session.id, GoalWrite(expected_revision=draft.revision, action="start")
    )


# -- LIVE-3: a parent that ends without finishing stops its children --------


def test_failed_parent_stops_its_running_subagents_and_says_why(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def provider_error(request: ModelRequest) -> ModelResponse:
            await _until(lambda: bool(provider.child_requests))
            raise RuntimeError("upstream provider returned 500")

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Map the routes.",
                    name="Routes",
                    context=None,
                ),
                provider_error,
            ],
            child=[_response(text="Never delivered.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        # The child is mid-task when its parent fails.
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Split the work.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _until(lambda: bool(store.list_entities(ChatSubagent)))
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status != ChatSubagentStatus.RUNNING
            )
        )

        parent = store.get(ChatTurn, parent_turn_id)
        assert parent.status == ChatTurnStatus.FAILED
        record = store.get(ChatSubagent, record.id)
        assert record.status == ChatSubagentStatus.STOPPED
        assert record.error is not None
        assert "delegating response failed" in record.error
        assert "upstream provider returned 500" in record.error
        child_turn = store.get(ChatTurn, record.child_turn_id)
        assert child_turn.status == ChatTurnStatus.CANCELLED
        assert not chat.has_active_provider_turn(child_turn.id)
        # The conversation (and a goal resumed later) sees why it stopped.
        posted = [
            item
            for item in _messages(store, parent.session_id)
            if item.metadata.get("kind") == "subagent_result"
        ]
        assert [item.metadata["subagent_status"] for item in posted] == ["stopped"]
        assert "delegating response failed" in posted[0].content
        provider.child_gate.set()
        await asyncio.sleep(0.05)
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.STOPPED
        await chat.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("parent_status", "snapshot", "stopped"),
    [
        (ChatTurnStatus.FAILED, {}, True),
        (ChatTurnStatus.CANCELLED, {}, True),
        # The operator can still retry the missing answer; the turn is not over.
        (
            ChatTurnStatus.FAILED,
            {"final_answer_recovery": {"attempts": 2}},
            False,
        ),
        # A completed parent's children report into the conversation.
        (ChatTurnStatus.COMPLETE, {}, False),
        # Restart recovery resumes this parent; its children stay live.
        (
            ChatTurnStatus.INTERRUPTED,
            {"recovery": {"required": True, "cause": "core_restart"}},
            False,
        ),
    ],
)
def test_only_a_parent_that_ended_for_good_stops_its_subagents(
    tmp_path: Path,
    parent_status: ChatTurnStatus,
    snapshot: dict,
    stopped: bool,
) -> None:
    async def scenario() -> None:
        store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
        parent_session = _session(store)
        parent = _turn(
            store,
            parent_session,
            status=parent_status,
            error="provider failed" if parent_status == ChatTurnStatus.FAILED else None,
            request_snapshot=snapshot,
        )
        record = _child(store, parent_session, parent)

        await chat.subagents.turn_settled(parent.id)

        latest = store.get(ChatSubagent, record.id)
        if stopped:
            assert latest.status == ChatSubagentStatus.STOPPED
            assert latest.error in {
                "The delegating response was stopped, so this subagent was "
                "stopped with it.",
                "The delegating response failed, so this subagent was stopped: "
                "provider failed",
            }
        else:
            assert latest.status == ChatSubagentStatus.RUNNING
        await chat.shutdown()

    asyncio.run(scenario())


def test_failed_harness_turn_stops_its_provider_subagents(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
        parent_session = _session(store)
        harness_turn = store.create(
            ChatTurn(
                engagement_id="project",
                session_id=parent_session.id,
                backend=ChatBackend.HARNESS,
                model="gpt-5.5",
                status=ChatTurnStatus.FAILED,
            )
        )
        record = _child(
            store, parent_session, harness_turn, parent_backend=ChatBackend.HARNESS
        )

        await chat.subagents.harness_turn_settled(
            parent_session.id, harness_turn.id, stopped=False, failed=True
        )

        latest = store.get(ChatSubagent, record.id)
        assert latest.status == ChatSubagentStatus.STOPPED
        assert "delegating response failed" in (latest.error or "")
        await chat.shutdown()

    asyncio.run(scenario())


# -- SUB-5: Stop during the next-round start leaves nothing running ----------


def test_stop_while_core_starts_the_next_round_leaves_no_round_running(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        answer_gate = asyncio.Event()
        parent_gate = asyncio.Event()
        round_gate = asyncio.Event()

        async def child_answer(request: ModelRequest) -> ModelResponse:
            await answer_gate.wait()
            return _response(text="Report for round 1.")

        async def parent_hold(request: ModelRequest) -> ModelResponse:
            await parent_gate.wait()
            return _call("p3", "finish_response")

        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Do the thing.",
                    name="Worker",
                    context=None,
                ),
                parent_hold,
                _response(text="done"),
            ],
            child=[child_answer, _response(text="Report for round 2.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        service = chat.subagents
        real_prepare = chat.prepare_async
        state = {"blocked": False}

        async def slow_prepare(request, *args, **kwargs):
            # Preparing a round awaits; the operator presses Stop meanwhile.
            content = str(request.messages[0].content)
            if "after your last report" in content and not state["blocked"]:
                state["blocked"] = True
                await round_gate.wait()
            return await real_prepare(request, *args, **kwargs)

        chat.prepare_async = slow_prepare  # type: ignore[method-assign]
        prepared = await real_prepare(
            _request(project, content="go", allow_subagents=True)
        )
        chat.start_provider_turn(prepared)
        await _until(lambda: bool(provider.child_requests))
        (record,) = store.list_entities(ChatSubagent)
        # The parent writes while the child produces its final answer.
        service._add_message(
            store.get(ChatSubagent, record.id),
            ChatSubagentMessageDirection.TO_CHILD,
            "Also check X.",
        )
        answer_gate.set()
        await _until(lambda: state["blocked"])

        stopped = await service.stop(record.id)

        child_turns = [
            item
            for item in store.list_entities(ChatTurn)
            if item.session_id == stopped.child_session_id
        ]
        assert not any(chat.has_active_provider_turn(item.id) for item in child_turns)
        assert stopped.status in {
            ChatSubagentStatus.COMPLETED,
            ChatSubagentStatus.STOPPED,
        }
        # Round 1 finished before the stop, so its report is kept, not dropped.
        assert stopped.status == ChatSubagentStatus.COMPLETED
        assert stopped.result == "Report for round 1."
        assert stopped.rounds == 1
        (message,) = service.messages_for(
            record.id, ChatSubagentMessageDirection.TO_CHILD
        )
        assert message.status == ChatSubagentMessageStatus.UNDELIVERED
        assert message.note == "The subagent stopped before reading it."

        round_gate.set()
        parent_gate.set()
        await asyncio.sleep(0.3)
        latest = store.get(ChatSubagent, record.id)
        assert latest.rounds == 1
        assert latest.child_turn_id == record.child_turn_id
        assert len(provider.child_requests) == 1
        assert not [
            item
            for item in store.list_entities(ChatTurn)
            if item.session_id == latest.child_session_id
            and item.status not in {ChatTurnStatus.COMPLETE, ChatTurnStatus.CANCELLED}
        ]
        await chat.shutdown()

    asyncio.run(scenario())


# -- SUB-10: an approval-blocked child is released when its supervisor ends --


def test_child_blocked_on_approval_stops_when_its_supervisor_ends(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
        parent_session = _session(store)
        parent = _turn(store, parent_session, status=ChatTurnStatus.ROUTING)
        approval = store.create(
            Approval(
                engagement_id="project",
                run_id="child-turn",
                risk_class=RiskClass.LOCAL_READ,
                exact_request={
                    "tool_name": "safe_read",
                    "arguments": {"path": "/etc/hosts"},
                },
                policy_rationale="approval boundary",
                requested_by="chat-assistant",
            )
        )
        record = _child(
            store,
            parent_session,
            parent,
            child_status=ChatTurnStatus.WAITING_APPROVAL,
            child_turn_fields={
                "id": "child-turn",
                "approval_id": approval.id,
                "tool_history": [
                    {
                        "name": "safe_read",
                        "arguments": {"path": "/etc/hosts"},
                        "status": "waiting_approval",
                    }
                ],
            },
        )
        # It blocks while its supervisor works: the supervisor owns it.
        await chat.subagents.turn_settled("child-turn")
        assert store.get(ChatSubagent, record.id).status == ChatSubagentStatus.RUNNING

        # The supervisor's response ends without acting on it.
        latest = store.get(ChatTurn, parent.id)
        store.update(
            ChatTurn,
            parent.id,
            {"status": ChatTurnStatus.COMPLETE},
            expected_revision=latest.revision,
        )
        await chat.subagents.turn_settled(parent.id)

        released = store.get(ChatSubagent, record.id)
        assert released.status == ChatSubagentStatus.STOPPED
        assert "blocked on approval for safe_read (/etc/hosts)" in (
            released.error or ""
        )
        assert store.get(ChatTurn, "child-turn").status == ChatTurnStatus.CANCELLED
        assert store.get(Approval, approval.id).status == ApprovalStatus.CANCELLED
        posted = _messages(store, parent_session.id)
        assert any(
            item.metadata.get("subagent_status") == "stopped"
            and "blocked on approval" in item.content
            for item in posted
        )
        await chat.shutdown()

    asyncio.run(scenario())


# -- SUB-12: editing a message retracts its subagents ------------------------


def test_editing_a_message_stops_its_subagents_and_posts_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Audit the host.",
                    name="Audit",
                    context=None,
                ),
                _call("p2", "finish_response"),
                _response(text="Started an audit; it reports back."),
            ],
            child=[_response(text="The audit found nothing.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Audit it.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.RUNNING
        session_id = prepared.session.id
        (question,) = [
            item for item in _messages(store, session_id) if item.role == ChatRole.USER
        ]

        # As POST /chat/sessions/{id}/rewind does.
        chat.rewind_session(session_id, before_message_id=question.id)
        await chat.subagents.stop_retracted(session_id)

        stopped = store.get(ChatSubagent, record.id)
        assert stopped.status == ChatSubagentStatus.STOPPED
        assert "edited the message" in (stopped.error or "")
        assert not chat.has_active_provider_turn(stopped.child_turn_id)
        provider.child_gate.set()
        await asyncio.sleep(0.1)
        visible = [
            item
            for item in _messages(store, session_id)
            if not item.metadata.get("retracted_at")
        ]
        assert visible == []
        assert chat.subagents.pending_update(session_id).records == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_rewind_route_stops_the_edited_exchanges_subagents(tmp_path: Path) -> None:
    store = NebulaStore(tmp_path / "rewind-subagents.db")
    from nebula.v3.domain import Engagement, ProviderProfile

    store.create(Engagement(id="project", name="Rewind"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    parent_session = _session(store)
    parent = _turn(store, parent_session, status=ChatTurnStatus.COMPLETE)
    question = store.create(
        ChatMessage(
            engagement_id="project",
            session_id=parent_session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Audit it.",
        )
    )
    store.create(
        ChatMessage(
            engagement_id="project",
            session_id=parent_session.id,
            sequence=2,
            role=ChatRole.ASSISTANT,
            content="Started an audit.",
            metadata={"chat_turn_id": parent.id},
        )
    )
    record = _child(store, parent_session, parent)
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.post(
        f"/api/v1/chat/sessions/{parent_session.id}/rewind",
        headers={"Authorization": "Bearer test-token"},
        json={"before_message_id": question.id},
    )

    assert response.status_code == 200, response.text
    stopped = store.get(ChatSubagent, record.id)
    assert stopped.status == ChatSubagentStatus.STOPPED
    assert not [
        item
        for item in store.list_entities(ChatMessage)
        if item.session_id == parent_session.id
        and item.metadata.get("kind") == "subagent_result"
    ]


# -- SUB-6: a child gets exactly its parent's SSH hosts ----------------------


@pytest.mark.parametrize(
    ("parent_hosts", "expected"),
    [([], []), ([{"id": "ssh-a", "alias": "lab-a"}], ["ssh-a"])],
)
def test_subagent_gets_only_the_ssh_hosts_its_parent_was_given(
    tmp_path: Path, parent_hosts: list[dict], expected: list[str]
) -> None:
    async def scenario() -> None:
        store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
        for host_id, alias in (("ssh-a", "lab-a"), ("ssh-b", "lab-b")):
            store.create(SshEnvironment(id=host_id, alias=alias, enabled=True))
        parent_session = _session(store)
        parent = _turn(
            store,
            parent_session,
            status=ChatTurnStatus.ROUTING,
            request_snapshot={
                "include_oci_tools": True,
                "ssh_environment_snapshot": parent_hosts,
                "allow_subagents": True,
            },
        )
        captured: list = []

        async def capture(request):
            captured.append(request)
            raise RuntimeError("stop before the provider")

        chat.prepare_async = capture  # type: ignore[method-assign]
        invocation = ToolInvocation(
            engagement_id="project",
            run_id=parent.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent_session.id,
            chat_turn_id=parent.id,
            tool_name="start_subagent",
            workspace=tmp_path,
            idempotency_key="start-1",
        )
        with pytest.raises(InvalidToolArguments):
            await chat.subagents.start(
                invocation, task="Check the hosts.", name=None, context=None
            )
        assert captured[0].ssh_environment_ids == expected

        # Another round, started by the parent's message, gets the same hosts.
        record = _child(
            store,
            parent_session,
            parent,
            id="finished-record",
            status=ChatSubagentStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc),
            child_status=ChatTurnStatus.COMPLETE,
        )
        with pytest.raises(InvalidToolArguments):
            await chat.subagents.send_to_child(
                parent_session.id,
                record.id,
                "Check the other host too.",
                parent_turn_id=parent.id,
            )
        assert captured[1].ssh_environment_ids == expected
        await chat.shutdown()

    asyncio.run(scenario())


# -- SUB-9: the goal continuation keeps the operator's turn settings ---------


def test_goal_continuation_after_reports_keeps_the_operators_turn_settings(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Review auth.",
                    name="Auth",
                    context=None,
                ),
                _call("p2", "finish_response"),
                _response(text="Started a review."),
            ],
            child=[_response(text="Cookies lack SameSite.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(project, content="Review auth.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        session_id = prepared.session.id
        goals, goal = _running_goal(store, store.get(ChatSession, session_id))
        parent = store.get(ChatTurn, parent_turn_id)
        store.update(
            ChatTurn, parent.id, {"goal_id": goal.id}, expected_revision=parent.revision
        )
        # The operator's current choices, saved on the conversation.
        session = store.get(ChatSession, session_id)
        store.update(
            ChatSession,
            session_id,
            {
                "metadata": {
                    **session.metadata,
                    "hook_ids": ["completion-gate"],
                    "allow_agent_messaging": True,
                    "reasoning_effort": "high",
                }
            },
            expected_revision=session.revision,
        )
        captured: list = []
        real_prepare = chat.prepare_async

        async def continuation(request, *args, **kwargs):
            if "Subagent reports are ready" in str(request.messages[-1].content):
                captured.append(request)
                # An operator message won the idle conversation meanwhile.
                raise chat_module.ChatHistoryConflict(
                    "chat session already has an active response"
                )
            return await real_prepare(request, *args, **kwargs)

        chat.prepare_async = continuation  # type: ignore[method-assign]
        provider.child_gate.set()
        await _until(lambda: bool(captured))

        (request,) = captured
        assert request.hook_ids == ["completion-gate"]
        assert request.allow_agent_messaging is True
        assert request.allow_subagents is True
        assert request.reasoning_effort == "high"
        # The newest turn's hosts, not every enabled host.
        assert request.ssh_environment_ids == []
        # Losing that race is not a reason to pause the goal.
        latest = goals.get(session_id)
        assert latest.status == ChatGoalStatus.RUNNING
        assert latest.blocked_reason is None
        await chat.shutdown()

    asyncio.run(scenario())


# -- SUB-8: the goal's token budget bounds its subagents as they spend -------


def test_goal_token_budget_stops_subagents_while_they_spend(tmp_path: Path) -> None:
    async def scenario() -> None:
        steps = 6
        provider = RoutedProvider(
            parent=[
                _call(
                    "p1",
                    "start_subagent",
                    task="Scan everything.",
                    name="Scan",
                    context=None,
                ),
                _call("p2", "wait_subagents", subagent_ids=None, mode=None),
            ],
            child=[
                _spending_call(f"c{index}", "read_parent_messages", 8_000)
                for index in range(steps)
            ],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
        session = _session(store)
        goals, goal = _running_goal(store, session, token_budget=20_000)
        prepared = await chat.prepare_async(
            _request(
                project,
                content="Scan it.",
                session_id=session.id,
                goal_id=goal.id,
                allow_subagents=True,
            )
        )
        chat.start_provider_turn(prepared)
        await _until(lambda: bool(store.list_entities(ChatSubagent)))
        (record,) = store.list_entities(ChatSubagent)
        await _until(
            lambda: (
                store.get(ChatSubagent, record.id).status
                in {ChatSubagentStatus.STOPPED, ChatSubagentStatus.FAILED}
            )
        )

        stopped = store.get(ChatSubagent, record.id)
        assert "budget" in (stopped.error or "")
        # It stopped near the budget, not after spending its whole script.
        assert len(provider.child_requests) < steps
        paused = store.get(ChatGoal, goal.id)
        assert paused.status == ChatGoalStatus.PAUSED
        assert "budget" in (paused.blocked_reason or "")
        assert paused.usage.total_tokens < 20_000 + 8_000 + 100
        # One debit record per child turn holds exactly what the turn spent,
        # and the goal counts the parent's and the child's tokens once each.
        child_turn = store.get(ChatTurn, stopped.child_turn_id)
        (charge,) = store.list_entities(ChatGoalUsageCharge)
        assert charge.usage == child_turn.usage
        await _until(lambda: not chat.has_active_provider_turn(prepared.turn.id))
        parent = store.get(ChatTurn, prepared.turn.id)
        assert (
            store.get(ChatGoal, goal.id).usage.total_tokens
            == parent.usage.total_tokens + child_turn.usage.total_tokens
        )
        await chat.subagents.reconcile_after_restart()
        assert (
            store.get(ChatGoal, goal.id).usage.total_tokens
            == parent.usage.total_tokens + child_turn.usage.total_tokens
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_settle_charges_only_what_accrual_left_uncharged(tmp_path: Path) -> None:
    store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
    session = _session(store)
    goals, goal = _running_goal(store, session)
    parent = _turn(store, session, status=ChatTurnStatus.COMPLETE, goal_id=goal.id)
    record = _child(store, session, parent)
    child_turn = store.get(ChatTurn, record.child_turn_id)
    service = chat.subagents

    child_turn = store.update(
        ChatTurn,
        child_turn.id,
        {"usage": ChatTokenUsage(input_tokens=90, output_tokens=10, total_tokens=100)},
        expected_revision=child_turn.revision,
    )
    service.charge_child_usage(child_turn)
    assert store.get(ChatGoal, goal.id).usage.total_tokens == 100
    child_turn = store.update(
        ChatTurn,
        child_turn.id,
        {"usage": ChatTokenUsage(input_tokens=130, output_tokens=20, total_tokens=150)},
        expected_revision=child_turn.revision,
    )
    service._charge_parent_goal(store.get(ChatSubagent, record.id), child_turn)
    service._charge_parent_goal(store.get(ChatSubagent, record.id), child_turn)
    service.charge_child_usage(child_turn)

    assert store.get(ChatGoal, goal.id).usage.total_tokens == 150
    (charge,) = store.list_entities(ChatGoalUsageCharge)
    assert charge.usage.total_tokens == 150


# -- GOAL-5: active time is time Nebula works on the goal --------------------


def test_goal_active_time_excludes_idle_gaps_between_turns(
    tmp_path: Path, monkeypatch
) -> None:
    store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
    session = _session(store)
    started = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    clock = {"now": started}
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(chat_module, "utc_now", lambda: clock["now"])
    goals, goal = _running_goal(store, session, time_budget_seconds=60)

    def work(seconds_from_start: float, seconds: float) -> None:
        turn = _turn(store, session, status=ChatTurnStatus.ROUTING, goal_id=goal.id)
        prepared = SimpleNamespace(turn=turn, execution_claim_id=None)
        clock["now"] = started + timedelta(seconds=seconds_from_start)
        chat._claim_execution(prepared)
        clock["now"] += timedelta(seconds=seconds)
        chat._release_execution(prepared)

    work(0, 10)
    idle = goals.get(session.id)
    assert idle.status == ChatGoalStatus.RUNNING
    assert idle.active_since is None
    assert idle.elapsed_seconds == pytest.approx(10)
    # Ninety minutes pass with no turn in flight (a failed turn, a queue).
    work(5_400, 5)
    after = goals.get(session.id)
    assert after.elapsed_seconds == pytest.approx(15)
    assert after.active_elapsed_seconds(started + timedelta(hours=5)) == pytest.approx(
        15
    )


def test_goal_active_time_runs_while_a_subagent_works(
    tmp_path: Path, monkeypatch
) -> None:
    store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
    session = _session(store)
    started = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    clock = {"now": started}
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: clock["now"])
    monkeypatch.setattr(chat_module, "utc_now", lambda: clock["now"])
    goals, goal = _running_goal(store, session)
    parent = _turn(store, session, status=ChatTurnStatus.ROUTING, goal_id=goal.id)
    prepared = SimpleNamespace(turn=parent, execution_claim_id=None)
    chat._claim_execution(prepared)
    record = _child(store, session, parent)
    clock["now"] = started + timedelta(seconds=20)
    # The parent parks in wait_subagents; its child keeps working.
    chat._release_execution(prepared)
    assert goals.get(session.id).active_since == started

    clock["now"] = started + timedelta(seconds=50)
    latest = store.get(ChatSubagent, record.id)
    store.update(
        ChatSubagent,
        record.id,
        {"status": ChatSubagentStatus.COMPLETED, "finished_at": clock["now"]},
        expected_revision=latest.revision,
    )
    goals.settle_active_time(session.id)

    settled = goals.get(session.id)
    assert settled.active_since is None
    assert settled.elapsed_seconds == pytest.approx(50)


def test_goal_read_reports_the_open_stretch_once(tmp_path: Path, monkeypatch) -> None:
    store, _, _, _ = _setup(tmp_path, RoutedProvider([], []))
    session = _session(store)
    started = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: started)
    goals, goal = _running_goal(store, session)
    now = started + timedelta(seconds=40)
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: now)

    snapshot = goals.read(session.id)

    # A client adds the time since active_since to elapsed_seconds.
    assert snapshot.elapsed_seconds == pytest.approx(40)
    assert snapshot.active_since == now
    assert snapshot.active_elapsed_seconds(now) == pytest.approx(40)
