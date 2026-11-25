"""Goal agents open operator action requests (``goal.request_operator_action``).

Covers the provider goal tool, the harness gateway tool, guardrails,
de-duplication, the auto-check and verification refusals, notification on
open, blocking and the sweep's resume, and Claude's AskUserQuestion in a
headless turn (today's default and the Core-routed behaviour).
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ButlerIncident,
    ChatGoalStatus,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    ToolCall,
    WorkItem,
)
from nebula.v3.goal_operator_requests import (
    ASK_USER_TOOL,
    GOAL_REQUEST_TOOL,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkAdapter,
    HarnessEvent,
    HarnessPermissionDecision,
    PermissionTicket,
)
from nebula.v3.operator_action_requests import (
    GOAL_WAIT_MARKER,
    list_requests,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_harness_adapters import (
    FakeClaudeClient,
    FakeClaudeOptions,
    FakeHookMatcher,
)
from tests.v3.test_harness_chat_goals import _new_goal, _wait_for_goal
from tests.v3.test_harnesses import FakeConnection, _runtime

pytestmark = pytest.mark.anyio


class FakeWork:
    """Butler's Work gateway goal action, without the HTTP hop."""

    def __init__(self, store: NebulaStore) -> None:
        self.goals = ChatGoalService(store)

    async def goal_action(self, project_id: str, arguments: dict[str, Any]) -> None:
        await self.goals.write(
            arguments["session_id"],
            GoalWrite.model_validate(
                {k: v for k, v in arguments.items() if k != "session_id"}
            ),
        )


def _request(**overrides: Any) -> dict[str, Any]:
    return {
        "action": "power_cycle_device",
        "device_or_host": "lab-phone-07",
        "reason": "Power-cycle the lab phone; it stopped answering on USB.",
        "evidence": ["usb enumeration lists no device"],
        "verification": ["Remote reboot over the debug bridge failed"],
        "urgency": "high",
        "check": {"kind": "time_elapsed", "params": {"seconds": 3600}},
        **overrides,
    }


# Harness gateway tool ------------------------------------------------------------


async def test_harness_goal_opens_request_through_gateway_and_blocks(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = await _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = await _new_goal(goals, step_budget=None)
        await goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        observed: dict = {}

        class RequestingConnection(FakeConnection):
            async def run_turn(
                self, prompt: str, *, model: str, images=None
            ) -> AsyncIterator[HarnessEvent]:
                self.prompts.append(prompt)
                yield HarnessEvent(type="started", external_session_id="requester")
                session = self.request.session
                observed["tools"] = {
                    tool["name"]
                    for tool in (await runtime._gateway_catalog(session))["tools"]
                }
                observed["instructions"] = session.metadata
                observed["research"] = await runtime._gateway_call(
                    session,
                    GOAL_REQUEST_TOOL,
                    _request(reason="Should I pursue the decoder lane first?"),
                )
                observed["requested"] = await runtime._gateway_call(
                    session, GOAL_REQUEST_TOOL, _request(block_goal=True)
                )
                yield HarnessEvent(type="completed", message="Waiting on the lab")

        async def open_requesting(request):
            connection = RequestingConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_requesting
        assert await runtime.dispatch_running_goal(created.session.id, "Investigate")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.BLOCKED)
        for _ in range(300):
            if not runtime._chat_turn_tasks:
                break
            await asyncio.sleep(0.01)
        assert GOAL_REQUEST_TOOL in observed["tools"]
        assert observed["research"]["isError"] is False
        assert observed["research"]["structuredContent"]["refusal"] == (
            "research_question"
        )
        result = observed["requested"]["structuredContent"]
        assert observed["requested"]["isError"] is False, result
        assert result["created"] is True and result["goal_block"] == "blocked"
        request_id = result["request"]["id"]
        goal = await goals.get(created.session.id)
        assert f"{GOAL_WAIT_MARKER}{request_id}" in (goal.blocked_reason or "")
        views = await list_requests(store)
        assert [view.id for view in views] == [request_id]
        assert views[0].record.created_by.startswith("goal-turn:")
        assert len(await store.find_entities(ButlerIncident, {}, engagement_id="eng-a"))
        calls = await store.list_session_entities(ToolCall, created.session.id)
        assert [call.tool_name for call in calls] == [
            GOAL_REQUEST_TOOL,
            GOAL_REQUEST_TOOL,
        ]
        await runtime.shutdown()

    await scenario()


async def test_harness_ask_user_question_routes_through_core(tmp_path):
    async def scenario() -> None:
        store, _engagement, _profile, _mcp, adapter, runtime = await _runtime(tmp_path)
        goals = ChatGoalService(store, runtime)
        created = await _new_goal(goals, step_budget=None)
        await goals.write(
            created.session.id,
            GoalWrite(expected_revision=created.goal.revision, action="start"),
        )
        observed: dict = {}

        class AskingConnection(FakeConnection):
            async def run_turn(
                self, prompt: str, *, model: str, images=None
            ) -> AsyncIterator[HarnessEvent]:
                self.prompts.append(prompt)
                yield HarnessEvent(type="started", external_session_id="asker")
                ask = self.request.ask_user_handler
                observed["research"] = await ask(
                    {
                        "questions": [
                            {
                                "question": "Which lane should I pursue first?",
                                "options": [{"label": "decoder"}, {"label": "parser"}],
                            }
                        ]
                    }
                )
                observed["after_research"] = len(await list_requests(store))
                observed["operator"] = await ask(
                    {
                        "questions": [
                            {"question": "Please power-cycle lab-phone-07; it hung."}
                        ]
                    }
                )
                yield HarnessEvent(type="completed", message="Waiting")

        async def open_asking(request):
            connection = AskingConnection(request)
            adapter.connections.append(connection)
            return connection

        adapter.open = open_asking
        assert await runtime.dispatch_running_goal(created.session.id, "Investigate")
        await _wait_for_goal(goals, created.session.id, ChatGoalStatus.BLOCKED)
        for _ in range(300):
            if not runtime._chat_turn_tasks:
                break
            await asyncio.sleep(0.01)
        assert "decide this yourself" in observed["research"]
        assert observed["after_research"] == 0
        views = await list_requests(store)
        assert len(views) == 1 and views[0].record.action == "answer_question"
        assert "power-cycle lab-phone-07" in views[0].record.reason
        assert f"{GOAL_WAIT_MARKER}{views[0].id}" in observed["operator"]
        assert "blocked the goal" in observed["operator"]
        goal = await goals.get(created.session.id)
        assert f"{GOAL_WAIT_MARKER}{views[0].id}" in (goal.blocked_reason or "")
        await runtime.shutdown()

    await scenario()


# Claude adapter: AskUserQuestion ----------------------------------------------


class _Deny:
    behavior = "deny"

    def __init__(self, message: str) -> None:
        self.message = message


class _Allow:
    behavior = "allow"

    def __init__(self, updated_input: dict[str, Any]) -> None:
        self.updated_input = updated_input


def _claude_request(tmp_path, permission, **extra: Any) -> AdapterOpenRequest:
    from nebula.v3.credentials import CredentialStore

    profile = HarnessProfile(
        id="claude-a",
        name="Claude",
        kind=HarnessKind.CLAUDE_AGENT_SDK,
        default_model="claude-test",
    )
    session = HarnessSession(
        id="session-a",
        engagement_id="eng-a",
        harness_profile_id=profile.id,
        model="claude-test",
    )
    return AdapterOpenRequest(
        profile=profile,
        session=session,
        workspace=tmp_path,
        mcp_profiles=(),
        credential_store=CredentialStore(),
        permission_handler=permission,
        **extra,
    )


@pytest.mark.parametrize("with_hooks", [True, False])
async def test_claude_ask_user_question_today_and_core_routed(
    tmp_path, monkeypatch, with_hooks
):
    sdk = SimpleNamespace(
        ClaudeAgentOptions=FakeClaudeOptions,
        ClaudeSDKClient=FakeClaudeClient,
        PermissionResultAllow=_Allow,
        PermissionResultDeny=_Deny,
        **({"HookMatcher": FakeHookMatcher} if with_hooks else {}),
    )
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
    permissions: list[Any] = []

    async def permission(request):
        permissions.append(request)
        future: asyncio.Future[HarnessPermissionDecision] = (
            asyncio.get_running_loop().create_future()
        )
        future.set_result(HarnessPermissionDecision(allowed=True))
        return PermissionTicket(None, "call-1", future)

    question = {"questions": [{"question": "Which lane first?"}]}

    # No Core handler (today's behaviour with the hook): the tool is not
    # offered (the SDK's ``tools`` list restricts built-ins) and the hook
    # denies it as a disabled native capability. Without the hook it used to
    # become an operator approval that could not carry an answer; it is now
    # denied with a message instead.
    connection = await ClaudeAgentSdkAdapter().open(
        _claude_request(tmp_path, permission)
    )
    options = FakeClaudeClient.latest.options.kwargs
    assert ASK_USER_TOOL not in options["tools"]
    if with_hooks:
        hook = options["hooks"]["PreToolUse"][0].hooks[0]
        denied = await hook(
            {"tool_name": ASK_USER_TOOL, "tool_input": question}, None, None
        )
        output = denied["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "is not enabled" in output["permissionDecisionReason"]
    else:
        denied = await options["can_use_tool"](ASK_USER_TOOL, question, None)
        assert denied.behavior == "deny"
        assert "does not relay AskUserQuestion" in denied.message
        assert permissions == []
    await connection.close()

    # With Core's handler: offered, and answered by Core, never by a prompt.
    asked: list[dict[str, Any]] = []

    async def ask_user(tool_input: dict[str, Any]) -> str:
        asked.append(tool_input)
        return "Core answer: decide this yourself."

    permissions.clear()
    connection = await ClaudeAgentSdkAdapter().open(
        _claude_request(tmp_path, permission, ask_user_handler=ask_user)
    )
    options = FakeClaudeClient.latest.options.kwargs
    assert ASK_USER_TOOL in options["tools"]
    assert ASK_USER_TOOL not in options["disallowed_tools"]
    if with_hooks:
        hook = options["hooks"]["PreToolUse"][0].hooks[0]
        denied = await hook(
            {"tool_name": ASK_USER_TOOL, "tool_input": question}, None, None
        )
        output = denied["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert (
            output["permissionDecisionReason"] == "Core answer: decide this yourself."
        )
    else:
        denied = await options["can_use_tool"](ASK_USER_TOOL, question, None)
        assert denied.behavior == "deny"
        assert denied.message == "Core answer: decide this yourself."
    assert asked == [question]
    assert permissions == []
    await connection.close()


async def test_ask_user_outside_a_goal_is_told_to_ask_in_its_reply(tmp_path):
    from nebula.v3.domain import HarnessTurn, HarnessTurnOrigin
    from nebula.v3.goal_operator_requests import handle_headless_question

    store = NebulaStore(tmp_path / "core.db")
    turn = HarnessTurn.model_construct(
        id="turn-a",
        engagement_id="eng-a",
        harness_session_id="session-a",
        origin=HarnessTurnOrigin.CHAT,
        chat_turn_id=None,
        chat_session_id=None,
    )
    message = await handle_headless_question(
        store, turn, {"questions": [{"question": "Please replug the board?"}]}
    )
    assert "ask in your reply" in message
    assert await store.find_entities(WorkItem, {}) == []
