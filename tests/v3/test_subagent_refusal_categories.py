"""Subagent and agent-messaging refusals reach the model classified by cause.

The failure contract (docs/TOOL_FAILURE_CONTRACT.md) carries no Core error
text, so the category, its guidance and Core's own limit numbers are all a
model has to decide what to do next. A provider chat and a harness chat must
get the same answer for the same refusal:

- the operator's running-at-once limit is ``capacity_reached``: wait for a
  subagent to finish, then retry;
- a goal whose token budget is used up is ``budget_exhausted``: do not retry;
- a rule (turned off, a subagent delegating, no provider model, messaging
  from outside a subagent) is ``permission_denied``: do not repeat;
- a bad argument is ``invalid_arguments``: correct it.

Every one of them is refused before anything runs, so it has no side effects.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat_subagents import GOAL_BUDGET_SUBAGENT_NOTE
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatSubagentStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.tool_failures import FAILURE_SCHEMA, tool_failure
from nebula.v3.tools import (
    BudgetExhausted,
    PolicyDenied,
    ToolCallOrigin,
    ToolInvocation,
    ToolNotPermitted,
)
from tests.v3.test_chat_subagent_lifecycle import _child, _running_goal, _session
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _drain,
    _finish,
    _history,
    _request,
    _response,
    _setup,
    _until,
)
from tests.v3.test_harness_provider_subagents import (
    SETTING,
    ScriptedConnection,
    _payload,
    _prepare,
)
from tests.v3.test_harness_provider_subagents import _setup as _harness_setup

_WAIT_AND_RETRY = "Wait for running work to finish, then retry this call."


def _envelope(entry: dict) -> dict:
    failure = json.loads(entry["provider_result"])
    assert failure["schema"] == FAILURE_SCHEMA
    return failure


def _refusal(response: dict) -> dict:
    assert response["isError"] is True
    failure = response["structuredContent"]
    assert failure["schema"] == FAILURE_SCHEMA
    # The text block the vendor model reads is the same envelope.
    assert json.loads(response["content"][0]["text"]) == failure
    return failure


def _provider_envelope(spec, arguments: dict, error: BaseException) -> dict:
    """Classify a refusal as the provider chat's tool loop does.

    For refusals a provider turn cannot reach through its offered tools (a
    child never gets start_subagent), the service is called directly and its
    exception classified with the chat loop's own phase rule.
    """

    before = isinstance(error, PolicyDenied) or getattr(
        error, "_nebula_before_execution", False
    )
    return tool_failure(
        spec,
        arguments,
        error,
        phase="before_execution" if before else "after_execution",
    )


# -- provider chat --------------------------------------------------------


def test_provider_chat_waits_at_the_running_limit_then_retries(tmp_path: Path):
    async def scenario() -> None:
        provider = RoutedProvider(parent=[], child=[])

        async def wait_after_refusal(request):
            # The refusal told the model to wait for running work, then retry.
            provider.child_gate.set()
            return _call("p3", "wait_subagents", subagent_ids=None, mode="any")

        provider.parent = [
            _call("p1", "start_subagent", task="One.", name=None, context=None),
            _call("p2", "start_subagent", task="Two.", name=None, context=None),
            wait_after_refusal,
            _call("p4", "start_subagent", task="Two.", name=None, context=None),
            _call("p5", "wait_subagents", subagent_ids=None, mode=None),
            _finish("p6"),
            _response(text="Both reports are in."),
        ]
        provider.child = [_response(text="First."), _response(text="Second.")]
        store, project, _, chat = _setup(tmp_path, provider)
        provider.child_gate = asyncio.Event()
        prepared = await chat.prepare_async(
            _request(
                project,
                content="Fan out.",
                allow_subagents=True,
                max_active_subagents=1,
            )
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        await _until(
            lambda: (
                store.get(ChatTurn, parent_turn_id).status == ChatTurnStatus.COMPLETE
            ),
            timeout=10,
        )

        history = _history(store, store.get(ChatTurn, parent_turn_id))
        assert [(item["name"], item["status"]) for item in history] == [
            ("start_subagent", "complete"),
            ("start_subagent", "failed"),
            ("wait_subagents", "complete"),
            ("start_subagent", "complete"),
            ("wait_subagents", "complete"),
        ]
        refused = _envelope(history[1])
        assert refused["category"] == "capacity_reached"
        assert refused["side_effects"] == "none"
        assert refused["retry_safe"] is True
        assert refused["next_action"] == _WAIT_AND_RETRY
        assert refused["limit"] == {
            "resource": "running_subagents",
            "maximum": 1,
            "current": 1,
        }
        assert "operator allows" not in history[1]["provider_result"]
        # The retry after the wait started the second subagent; both reported.
        records = store.list_entities(ChatSubagent)
        assert sorted(item.task for item in records) == ["One.", "Two."]
        assert {item.status for item in records} == {ChatSubagentStatus.COMPLETED}
        await chat.shutdown()

    asyncio.run(scenario())


def test_provider_chat_does_not_delegate_past_an_exhausted_goal_budget(
    tmp_path: Path,
):
    """A goal paused on its token budget refuses more subagents, for good.

    A provider goal turn checks its goal before running a batch of tool
    calls, so the refusal meets a later call of the same batch after a child
    spent the rest of the budget. Waiting cannot refill it.
    """

    async def scenario() -> None:
        budget = 500_000
        store, project, _, chat = _setup(tmp_path, RoutedProvider([], []))
        chat.artifact_store = ArtifactStore(tmp_path / "artifacts")
        session = _session(store)
        _, goal = _running_goal(store, session, token_budget=budget)
        prepared = await chat.prepare_async(
            _request(
                project,
                content="Keep delegating.",
                session_id=session.id,
                goal_id=goal.id,
                allow_subagents=True,
            )
        )
        finished = _child(
            store,
            session,
            prepared.turn,
            id="finished-record",
            status=ChatSubagentStatus.COMPLETED,
            finished_at=utc_now(),
            child_status=ChatTurnStatus.COMPLETE,
        )
        # A child's usage paused the goal at its budget (#570).
        goal = store.get(ChatGoal, goal.id)
        store.update(
            ChatGoal,
            goal.id,
            {
                "status": ChatGoalStatus.PAUSED,
                "usage": ChatTokenUsage(total_tokens=budget + 40),
                "blocked_reason": GOAL_BUDGET_SUBAGENT_NOTE,
            },
            expected_revision=goal.revision,
        )
        components = prepared.tool_components
        calls = {
            "start_subagent": {"task": "More.", "name": None, "context": None},
            # Another round of a finished child is another running subagent.
            "message_subagent": {"subagent_id": finished.id, "message": "Again."},
        }
        for step, (name, arguments) in enumerate(calls.items(), 1):
            invocation = ToolInvocation(
                engagement_id="project",
                run_id=prepared.turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=session.id,
                chat_turn_id=prepared.turn.id,
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
                idempotency_key=f"chat:{prepared.turn.id}:step:{step}",
            )
            with pytest.raises(BudgetExhausted) as caught:
                await components.broker.execute(invocation, components.scope)
            refused = _provider_envelope(
                components.specs[name], arguments, caught.value
            )
            assert refused["category"] == "budget_exhausted", name
            assert refused["side_effects"] == "none"
            assert refused["retry_safe"] is False
            assert refused["next_action"].startswith("Do not retry this call")
            assert refused["limit"] == {
                "resource": "goal_tokens",
                "maximum": budget,
                "current": budget + 40,
            }
            assert "token budget" not in json.dumps(refused)
        # Nothing started and the message was never stored.
        assert [item.id for item in store.list_entities(ChatSubagent)] == [finished.id]
        assert store.list_entities(ChatSubagentMessage) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_provider_chat_reports_bad_subagent_arguments_as_invalid_input(
    tmp_path: Path,
):
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="   ", name=None, context=None),
                _call("p2", "stop_subagent", subagent_id="not-a-subagent"),
                _call("p3", "message_subagent", subagent_id="nope", message="Hi"),
                _finish("p4"),
                _response(text="Nothing to delegate."),
            ],
            child=[],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Delegate.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)

        history = _history(store, store.get(ChatTurn, parent_turn_id))
        assert [item["status"] for item in history] == ["failed"] * 3
        for entry in history:
            refused = _envelope(entry)
            assert refused["category"] == "invalid_arguments", entry["name"]
            # Refused before anything ran, so the model corrects and reissues.
            assert refused["side_effects"] == "none"
            assert refused["retry_safe"] is True
            assert refused["next_action"].startswith("Correct the indicated argument")
            assert "limit" not in refused
        assert store.list_entities(ChatSubagent) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_provider_subagent_rules_are_not_permitted(tmp_path: Path):
    async def scenario() -> None:
        store, project, _, chat = _setup(tmp_path, RoutedProvider([], []))
        prepared = await chat.prepare_async(
            _request(project, content="Delegate.", allow_subagents=True)
        )
        spec = prepared.tool_components.specs["start_subagent"]
        arguments = {"task": "Nested.", "name": None, "context": None}
        parent_session = store.get(ChatSession, prepared.session.id)
        child_session = _session(
            store,
            title="Subagent",
            parent_session_id=parent_session.id,
            metadata={"subagent_id": "child-record"},
        )
        child_turn = store.create(
            ChatTurn(
                engagement_id="project",
                session_id=child_session.id,
                provider_profile_id="provider",
                model="model-a",
                request_snapshot={"subagent_child": True},
            )
        )
        cases = {
            "subagents.depth": ToolInvocation(
                engagement_id="project",
                run_id=child_turn.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=child_session.id,
                chat_turn_id=child_turn.id,
                tool_name="start_subagent",
                arguments=arguments,
                workspace=tmp_path,
                idempotency_key="nested",
            ),
            "subagents.chat_turn": ToolInvocation(
                engagement_id="project",
                run_id="run",
                origin=ToolCallOrigin.CHAT,
                chat_session_id=parent_session.id,
                tool_name="start_subagent",
                arguments=arguments,
                workspace=tmp_path,
                idempotency_key="no-turn",
            ),
        }
        for rule, invocation in cases.items():
            with pytest.raises(ToolNotPermitted) as caught:
                await chat.subagents.start(
                    invocation, task="Nested.", name=None, context=None
                )
            assert caught.value.decision.rule == rule
            refused = _provider_envelope(spec, arguments, caught.value)
            assert refused["category"] == "permission_denied", rule
            assert refused["side_effects"] == "none"
            assert refused["retry_safe"] is False
            assert "do not repeat" in refused["next_action"]
        # A child's message to its parent after its round ended is refused
        # the same way.
        with pytest.raises(ToolNotPermitted) as caught:
            await chat.subagents.send_to_parent(
                cases["subagents.depth"].model_copy(
                    update={"tool_name": "message_parent"}
                ),
                "Late news.",
                wait_for_reply=False,
            )
        assert caught.value.decision.rule == "subagents.parent"
        assert store.list_entities(ChatSubagent) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_provider_agent_messaging_refusals_are_classified(tmp_path: Path):
    async def scenario() -> None:
        provider = RoutedProvider(parent=[], child=[])
        store, project, _, chat = _setup(tmp_path, provider)

        async def turned_off(request):
            session = store.get(ChatSession, prepared.session.id)
            store.update(
                ChatSession,
                session.id,
                {"metadata": {**session.metadata, "allow_agent_messaging": False}},
                expected_revision=session.revision,
            )
            return _call("a3", "send_agent_message", session_id="peer", message="Hi")

        provider.parent = [
            _call("a1", "send_agent_message", session_id="peer", message="   "),
            _call("a2", "send_agent_message", session_id="no-such", message="Hi"),
            turned_off,
            _finish("a4"),
            _response(text="Could not reach a peer."),
        ]
        prepared = await chat.prepare_async(
            _request(project, content="Tell the peer.", allow_agent_messaging=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)

        history = _history(store, store.get(ChatTurn, parent_turn_id))
        categories = [_envelope(item)["category"] for item in history]
        assert categories == [
            "invalid_arguments",
            "invalid_arguments",
            "permission_denied",
        ]
        assert [item["status"] for item in history] == ["failed", "failed", "denied"]
        assert {_envelope(item)["side_effects"] for item in history} == {"none"}
        await chat.shutdown()

    asyncio.run(scenario())


# -- harness gateway ------------------------------------------------------


def test_harness_waits_at_the_running_limit_then_retries(tmp_path: Path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["First.", "Second."]
        child.gate = asyncio.Event()
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            _payload(await connection.call("subagent.start", task="One."))
            seen["refused"] = _refusal(
                await connection.call("subagent.start", task="Two.")
            )
            child.gate.set()
            seen["waited"] = _payload(
                await connection.call("subagent.wait", mode="any", timeout_seconds=5)
            )
            seen["retried"] = _payload(
                await connection.call("subagent.start", task="Two.")
            )
            return "Both started."

        adapter.script = script
        _, _, turn = _prepare(
            runtime, project, harness, "Fan out.", setting={**SETTING, "max_active": 1}
        )
        await runtime.start_chat_turn(turn.id)

        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
        refused = seen["refused"]
        assert refused["tool"] == "subagent.start"
        assert refused["category"] == "capacity_reached"
        assert refused["side_effects"] == "none"
        assert refused["retry_safe"] is True
        assert refused["next_action"] == _WAIT_AND_RETRY
        assert refused["limit"] == {
            "resource": "running_subagents",
            "maximum": 1,
            "current": 1,
        }
        assert seen["waited"]["subagents"][0]["status"] == "completed"
        assert seen["retried"]["status"] == "running"
        records = store.list_entities(ChatSubagent)
        assert sorted(item.task for item in records) == ["One.", "Two."]
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_subagent_refusals_match_the_provider_categories(tmp_path: Path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            # Invalid input: Core strips a blank task; an id it does not own.
            seen["blank"] = _refusal(
                await connection.call("subagent.start", task="   ")
            )
            seen["unknown"] = _refusal(
                await connection.call("subagent.stop", subagent_id="nope")
            )
            # Not permitted: the conversation no longer has a provider model.
            chat_turn = store.get(ChatTurn, chat_turn_id)
            store.update(
                ChatTurn,
                chat_turn.id,
                {
                    "request_snapshot": {
                        **chat_turn.request_snapshot,
                        "provider_subagent": {**SETTING, "model": ""},
                    },
                },
                expected_revision=chat_turn.revision,
            )
            seen["no_model"] = _refusal(
                await connection.call("subagent.start", task="Elsewhere.")
            )
            # Not permitted: subagents were turned off for this turn.
            harness_turn = store.get(HarnessTurn, turn.id)
            store.update(
                HarnessTurn,
                harness_turn.id,
                {
                    "metadata": {
                        key: value
                        for key, value in harness_turn.metadata.items()
                        if key != "provider_subagent"
                    }
                },
                expected_revision=harness_turn.revision,
            )
            seen["off"] = _refusal(
                await connection.call("subagent.start", task="Anyway.")
            )
            return "Refused."

        adapter.script = script
        _, chat_turn, turn = _prepare(runtime, project, harness, "Delegate.")
        chat_turn_id = chat_turn.id
        await runtime.start_chat_turn(turn.id)

        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
        # A harness conversation has no Nebula goal (its goals are the
        # vendor's), so a goal budget refusal cannot reach it.
        expected = {
            "blank": "invalid_arguments",
            "unknown": "invalid_arguments",
            "no_model": "permission_denied",
            "off": "permission_denied",
        }
        assert {key: seen[key]["category"] for key in expected} == expected
        assert {seen[key]["side_effects"] for key in expected} == {"none"}
        assert seen["blank"]["retry_safe"] is True
        for key in ("no_model", "off"):
            assert seen[key]["retry_safe"] is False, key
        for key in expected:
            assert "limit" not in seen[key], key
        # No Core error text reaches the vendor model.
        text = json.dumps(seen)
        for detail in ("turned off", "provider model", "describe"):
            assert detail not in text
        assert store.list_entities(ChatSubagent) == []
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_agent_messaging_refusals_match_the_provider_categories(
    tmp_path: Path,
):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        runtime.bind_agent_messages(chat.agent_messages)
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            seen["blank"] = _refusal(
                await connection.call("agent.send", session_id="peer", message="  ")
            )
            seen["unknown"] = _refusal(
                await connection.call("agent.send", session_id="nope", message="Hi")
            )
            harness_turn = store.get(HarnessTurn, turn.id)
            store.update(
                HarnessTurn,
                harness_turn.id,
                {
                    "metadata": {
                        **harness_turn.metadata,
                        "allow_agent_messaging": False,
                    }
                },
                expected_revision=harness_turn.revision,
            )
            seen["off"] = _refusal(
                await connection.call("agent.send", session_id="peer", message="Hi")
            )
            return "Refused."

        adapter.script = script
        _, _, turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=harness.id,
            model=None,
            prompt="Coordinate.",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allow_agent_messaging=True,
        )
        await runtime.start_chat_turn(turn.id)

        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
        assert {key: item["category"] for key, item in seen.items()} == {
            "blank": "invalid_arguments",
            "unknown": "invalid_arguments",
            "off": "permission_denied",
        }
        assert {item["side_effects"] for item in seen.values()} == {"none"}
        assert "turned off" not in json.dumps(seen)
        await chat.shutdown()

    asyncio.run(scenario())
