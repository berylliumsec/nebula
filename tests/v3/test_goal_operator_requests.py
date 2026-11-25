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

from typing import Any

import pytest

from nebula.v3 import chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_goal_tools import GOAL_TOOL_NAMES, goal_completion_components
from nebula.v3.chat_goals import ChatGoalService, GoalWrite
from nebula.v3.domain import (
    ButlerIncident,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    ProviderProfile,
    ToolCallOrigin,
    WorkItem,
    utc_now,
)
from nebula.v3.goal_operator_requests import (
    GOAL_ACTIONS,
    GOAL_REQUEST_TOOL,
    MAX_OPEN_PER_GOAL,
    guardrail_refusal,
    operator_only_question,
    question_text,
)
from nebula.v3.operator_action_requests import (
    GOAL_WAIT_MARKER,
    board_requests,
    create_request,
    sweep_operator_actions,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments, ToolInvocation, ToolNotPermitted
from nebula.v3.work import WorkCheckIn, WorkService
from tests.v3.test_butler_oversight import FakeProvider, _setup, _start

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


# Guardrails -----------------------------------------------------------------


def test_guardrails_refuse_research_questions_and_vague_devices():
    assert guardrail_refusal(_request()) is None
    assert "review_model_refusal" not in GOAL_ACTIONS
    assert "answer_question" not in GOAL_ACTIONS
    assert guardrail_refusal(_request(action="answer_question"))[0] == (
        "action_not_allowed"
    )
    for vague in ("device", "the host", "iPhone", "?", "n/a"):
        assert guardrail_refusal(_request(device_or_host=vague))[0] == (
            "device_not_concrete"
        ), vague
    # A non-physical action is not about a machine name, but needs a subject.
    assert (
        guardrail_refusal(
            _request(action="attach_credential", device_or_host="build account")
        )
        is None
    )
    decision = guardrail_refusal(
        _request(reason="Should I fuzz the parser or the decoder first?")
    )
    assert decision is not None and decision[0] == "research_question"
    assert "decide this yourself" in decision[1]
    question = guardrail_refusal(
        _request(
            action="other",
            device_or_host="lab notebook",
            reason="Is the second lane still worth pursuing?",
        )
    )
    assert question is not None and question[0] == "research_question"
    assert (
        guardrail_refusal(
            _request(
                action="other",
                device_or_host="lab badge reader",
                reason="Swap the badge reader cable; the port is loose.",
            )
        )
        is None
    )


def test_operator_only_question_is_narrow():
    assert operator_only_question("Please power-cycle lab-phone-07, it hung.")
    assert operator_only_question("Can you replug the USB cable on the test board?")
    assert operator_only_question("Enter the passcode on the phone so I can pair.")
    assert not operator_only_question("Should I restart the host before lane 2?")
    assert not operator_only_question("Which hypothesis should we pursue first?")
    assert not operator_only_question("Do you want me to fuzz the decoder?")
    assert question_text(
        {
            "questions": [
                {
                    "question": "Which lane?",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
    ) == ["Which lane? Options: A, B"]


# Provider goal tool ------------------------------------------------------------


async def _provider_goal(tmp_path, monkeypatch):
    _, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    profile = await store.get(ProviderProfile, "provider-project")
    capabilities = profile.capabilities.model_dump(mode="python")
    capabilities["tool_calling"] = True
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
    provider = FakeProvider(profile.id, local=True)
    provider.config.default_model = "model"
    provider.config.model_allowlist = ["model"]
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "check" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("Check the lab.", encoding="utf-8")
    chat = ChatService(store, workspace_resolver=lambda _: workspace)
    prepared = await chat.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id="project",
            session_id=goal.session_id,
            goal_id=goal.id,
            skill={"name": "check", "path": str(skill_path.resolve())},
            messages=[{"role": "user", "content": "Check the lab phone"}],
            include_knowledge=False,
            stream=True,
        )
    )
    assert prepared.turn is not None
    # The scheduler moves the turn to routing and the goal's claim to it
    # before any tool call runs.
    await store.update(ChatTurn, prepared.turn.id, {"status": ChatTurnStatus.ROUTING})
    await store.update(
        ChatGoal,
        goal.id,
        {
            "execution_claim_id": prepared.turn.id,
            "execution_owner_id": "worker-a",
            "execution_claimed_at": utc_now(),
        },
    )
    return store, goals, goal, prepared, workspace


def _invocation(prepared, goal, workspace, arguments, key: str) -> ToolInvocation:
    return ToolInvocation(
        engagement_id="project",
        run_id=prepared.turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=goal.session_id,
        chat_turn_id=prepared.turn.id,
        tool_name=GOAL_REQUEST_TOOL,
        arguments=arguments,
        workspace=workspace,
        idempotency_key=key,
        requested_by="chat-assistant",
    )


async def test_provider_goal_opens_dedupes_blocks_notifies_and_sweep_resumes(
    tmp_path, monkeypatch
):
    store, goals, goal, prepared, workspace = await _provider_goal(
        tmp_path, monkeypatch
    )
    assert GOAL_REQUEST_TOOL in GOAL_TOOL_NAMES
    assert GOAL_REQUEST_TOOL in prepared.tool_components.specs
    assert GOAL_REQUEST_TOOL in prepared.turn.request_snapshot["available_tool_names"]
    # The always-loaded tool description carries the guidance (chat.py's goal
    # instructions are unchanged to keep the commit within its size cap).
    assert "Never use it for research decisions" in (
        prepared.tool_components.specs[GOAL_REQUEST_TOOL].description
    )
    broker, scope = prepared.tool_components.broker, prepared.tool_components.scope

    first = await broker.execute(
        _invocation(prepared, goal, workspace, _request(), "k1"), scope
    )
    assert first.output["created"] is True
    assert first.output["notified"] is True
    assert first.output["goal_block"] is None
    request_id = first.output["request"]["id"]
    assert first.output["request"]["goal_session_id"] == goal.session_id
    item = await store.get(WorkItem, request_id)
    assert item.title.startswith("Needs you: Power-cycle device on lab-phone-07")
    # Notified on open: a Butler incident in the project feed.
    incidents = await store.find_entities(ButlerIncident, {}, engagement_id="project")
    assert [incident.source for incident in incidents] == ["operator_action"]
    assert incidents[0].goal_ids == [goal.id]
    # Shown on the Butler board.
    board = await board_requests(store)
    assert [entry["id"] for entry in board] == [request_id]

    again = await broker.execute(
        _invocation(prepared, goal, workspace, _request(), "k2"), scope
    )
    assert again.output["created"] is False
    assert again.output["refusal"] == "duplicate"
    assert again.output["request"]["id"] == request_id

    blocked = await broker.execute(
        _invocation(prepared, goal, workspace, _request(block_goal=True), "k3"),
        scope,
    )
    assert blocked.output["refusal"] == "duplicate"
    assert blocked.output["goal_block"] == "blocked"
    assert "End this turn" in blocked.output["next_action"]
    saved = await goals.get(goal.session_id)
    assert saved.status == ChatGoalStatus.BLOCKED
    assert f"{GOAL_WAIT_MARKER}{request_id}" in (saved.blocked_reason or "")
    assert len(await store.find_entities(WorkItem, {})) == 1

    # The operator marks it done on the board; the sweep closes and resumes.
    await WorkService(store).check_in(
        "project",
        request_id,
        WorkCheckIn(summary="Power-cycled it.", status="done", request_id="op-1"),
        actor_kind="operator",
        actor_id="operator",
    )
    result = await sweep_operator_actions(store, FakeWork(store))
    assert [entry["action"] for entry in result["actions"]] == ["finish"]
    assert result["actions"][0]["resume"] == "resumed"
    assert (await goals.get(goal.session_id)).status == ChatGoalStatus.RUNNING
    resolved = await store.find_entities(ButlerIncident, {}, engagement_id="project")
    assert resolved[0].status == "resolved"


async def test_provider_goal_refusals(tmp_path, monkeypatch):
    store, _goals, goal, prepared, workspace = await _provider_goal(
        tmp_path, monkeypatch
    )
    broker, scope = prepared.tool_components.broker, prepared.tool_components.scope

    async def call(arguments: dict[str, Any], key: str):
        return await broker.execute(
            _invocation(prepared, goal, workspace, arguments, key), scope
        )

    # Verification is required by the schema.
    with pytest.raises(InvalidToolArguments):
        await call(_request(verification=[]), "v")
    # Core-only actions are not in the goal tool's enum.
    with pytest.raises(InvalidToolArguments):
        await call(_request(action="review_model_refusal"), "c")
    # A host action without an auto-check goes back to the model.
    no_check = _request()
    del no_check["check"]
    with pytest.raises(InvalidToolArguments, match="auto-check"):
        await call(no_check, "n")
    # The auto-check already passes: nothing for the operator to do.
    present = tmp_path / "token.txt"
    present.write_text("x")
    passes = await call(
        _request(
            action="attach_credential",
            device_or_host="build account",
            check={"kind": "path_exists", "params": {"path": str(present)}},
        ),
        "p",
    )
    assert passes.output["refusal"] == "check_already_passes"
    research = await call(_request(reason="Which lane should we pursue next?"), "r")
    assert research.output["refusal"] == "research_question"
    vague = await call(_request(device_or_host="the device"), "d")
    assert vague.output["refusal"] == "device_not_concrete"
    assert await store.find_entities(WorkItem, {}) == []
    assert await store.find_entities(ButlerIncident, {}, engagement_id="project") == []
    for index in range(MAX_OPEN_PER_GOAL):
        opened = await call(_request(device_or_host=f"lab-phone-{index}"), f"o{index}")
        assert opened.output["created"] is True
    limited = await call(_request(device_or_host="lab-phone-99"), "o99")
    assert limited.output["refusal"] == "too_many_open"
    repeat = await call(_request(device_or_host="lab-phone-0"), "o0-again")
    assert repeat.output["refusal"] == "duplicate"
    # Another turn (or no goal claim) may not raise requests for this goal.
    await store.update(ChatGoal, goal.id, {"execution_claim_id": "other-turn"})
    with pytest.raises(ToolNotPermitted):
        await call(_request(device_or_host="lab-phone-50"), "x")
    with pytest.raises(ToolNotPermitted):
        await broker.execute(
            _invocation(prepared, goal, workspace, _request(), "y").model_copy(
                update={"requested_by": "operator"}
            ),
            scope,
        )


async def test_core_and_butler_paths_notify_on_open_by_default(tmp_path):
    _, store, goals = await _setup(tmp_path)
    goal = await _start(goals)
    created = await create_request(
        store,
        {
            "action": "approve_account_change",
            "device_or_host": "lab account",
            "reason": "Approve the plan change for the lab account.",
            "goal_session_id": goal.session_id,
            "verification": ["The change needs the account owner"],
            "created_by": "core",
        },
    )
    assert created["notified"] is True
    assert len(await store.find_entities(ButlerIncident, {}, engagement_id="project"))
    silent = await create_request(
        store,
        {
            "action": "approve_account_change",
            "device_or_host": "other account",
            "reason": "Approve it.",
            "project_id": "project",
            "verification": ["Owner only"],
        },
        notifier=None,
    )
    assert silent["notified"] is False


def test_goal_components_offer_the_tool():
    from pathlib import Path

    components = goal_completion_components(
        None,  # type: ignore[arg-type]
        engagement_id="project",
        workspace=Path("."),
    )
    spec = components.specs[GOAL_REQUEST_TOOL]
    assert "operator-only physical or account action" in spec.description
    assert "Never use it for research decisions" in spec.description
    assert spec.input_schema["required"] == [
        "action",
        "device_or_host",
        "reason",
        "verification",
    ]
