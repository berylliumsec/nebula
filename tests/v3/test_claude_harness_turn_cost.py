"""A Claude harness turn records what it cost, not the CLI session's running total.

Claude Code reports ``total_cost_usd`` on every ``result`` as the running total
of its session. The CLI bundled with the pinned SDK (0.2.118, CLI 2.1.209) fills
it from its process-wide cost counter, restores that counter from its config
when it resumes a session, and zeroes it when the conversation is reset. Against
a mock Messages API, three identical turns reported 0.000105, 0.00021 and
0.000315. The ``usage`` token counts of the same results are per turn (10 in and
5 out every time), so only the cost is a running total.

The real ``claude_agent_sdk.ClaudeSDKClient`` runs over the scripted CLI
transports of the other Claude harness tests; only the subprocess is replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest

from nebula.v3.domain import (
    AgentRun,
    HarnessDetailedUsage,
    HarnessKind,
    RunBudget,
    RunStatus,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
)
from tests.v3.test_claude_harness_turns import (
    SESSION,
    TURN_DEADLINE_SECONDS,
    WAIT,
    Chat,
    ScriptedCli,
    _connection,
    _turn,
    _until,
    assistant,
    result,
    stream_text,
    user_text,
)
from tests.v3.test_harness_leftovers import (
    QueueingCli,
    _answer,
    _claude,
    _collect,
    _result,
    _steered_turn,
)


def costed(text: str, session_cost_usd: float) -> list[dict[str, Any]]:
    """A CLI turn answering ``text`` whose result reports the session's total."""

    return [
        *stream_text(text),
        assistant([{"type": "text", "text": text}]),
        result(text=text, total_cost_usd=session_cost_usd),
    ]


def queued_costed(
    text: str, session_cost_usd: float, *, pause: float = 0.0
) -> list[Any]:
    return [
        *_answer(text, pause=pause)[:-1],
        _result(text, total_cost_usd=session_cost_usd),
    ]


def final_usage(events: list[HarnessEvent]) -> HarnessEvent:
    usage = [event for event in events if event.type == "usage"][-1]
    assert usage.detailed_usage is not None
    return usage


def turn_usage(events: list[HarnessEvent]) -> HarnessDetailedUsage:
    detailed = final_usage(events).detailed_usage
    assert detailed is not None
    return detailed


def turn_cost(events: list[HarnessEvent]) -> float | None:
    return turn_usage(events).cost_usd


def test_claude_turn_cost_is_the_growth_of_the_session_total(tmp_path: Path) -> None:
    async def scenario() -> list[list[HarnessEvent]]:
        cli = ScriptedCli(
            [
                costed("first answer", 0.000105),
                costed("second answer", 0.00021),
                costed("third answer", 0.000315),
            ]
        )
        connection = await _connection(cli, tmp_path)
        turns = [
            await _turn(connection, prompt) for prompt in ("first", "second", "third")
        ]
        await connection.close()
        return turns

    turns = asyncio.run(scenario())

    assert [turn_cost(events) for events in turns] == pytest.approx([0.000105] * 3)
    # The running total stays visible next to the turn's own cost.
    assert [final_usage(events).payload["session_cost"] for events in turns] == [
        {"amount": 0.000105, "currency": "USD"},
        {"amount": 0.00021, "currency": "USD"},
        {"amount": 0.000315, "currency": "USD"},
    ]
    # Token counts are already per turn and stay as the CLI reports them.
    tokens = [turn_usage(events) for events in turns]
    assert [(usage.input_tokens, usage.output_tokens) for usage in tokens] == [
        (3, 2)
    ] * 3


def test_claude_mission_spend_is_the_cli_session_total(tmp_path: Path) -> None:
    async def scenario() -> tuple[AgentRun, int]:
        cli = ScriptedCli(
            [
                costed("Stage one done.", 0.000105),
                costed("Stage two done.", 0.00021),
                costed("Stage three done.", 0.000315),
            ]
        )
        chat = Chat(tmp_path, lambda: cli)
        run = await chat.service.start_mission(
            engagement_id=chat.engagement.id,
            name="Cost review",
            objective="Review the scope in three stages",
            profile_id=chat.profile.id,
            model="claude-test",
            budget=RunBudget(max_duration_seconds=10),
            stages=[
                {"title": f"Stage {number}", "objective": f"Do step {number}"}
                for number in (1, 2, 3)
            ],
        )
        await asyncio.wait_for(
            chat.service._mission_tasks[run.id], TURN_DEADLINE_SECONDS
        )
        finished = chat.store.get(AgentRun, run.id)
        opened = len(chat.adapter.opened)
        await chat.service.shutdown()
        return finished, opened

    finished, opened = asyncio.run(scenario())

    assert finished.status == RunStatus.COMPLETE
    assert opened == 1  # One CLI session answered every stage.
    assert finished.metadata["spent_usd"] == pytest.approx(0.000315)
    per_turn = [
        entry["cost_usd"] for entry in finished.metadata["harness_turn_usage"].values()
    ]
    assert per_turn == pytest.approx([0.000105] * 3)


class ResumingAdapter(HarnessAdapter):
    """Opens scripted CLIs the way the Claude adapter does, resuming the session."""

    kind = HarnessKind.CLAUDE_AGENT_SDK

    def __init__(self, clis: Callable[[], ScriptedCli]) -> None:
        self.clis = clis
        self.opened: list[ScriptedCli] = []
        self.resumed: list[str | None] = []

    async def probe(self, profile: Any, credential_store: Any) -> HarnessHealth:
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        cli = self.clis()
        self.opened.append(cli)
        self.resumed.append(request.session.external_session_id)
        client = claude_agent_sdk.ClaudeSDKClient(
            options=claude_agent_sdk.ClaudeAgentOptions(), transport=cli
        )
        await client.connect()
        return ClaudeAgentSdkConnection(
            client,
            permission_handler=request.permission_handler,
            sdk=claude_agent_sdk,
            external_session_id=request.session.external_session_id,
            workspace=request.workspace,
        )


def test_claude_resumed_session_does_not_bill_its_earlier_spend(
    tmp_path: Path,
) -> None:
    clis = [
        # The first CLI answers, then exits while idle.
        ScriptedCli([[*costed("first answer", 0.1), ("SLEEP", 0.05), ("EXIT", 1)]]),
        # The CLI that resumes the session restored its running total (0.1)
        # from its config, so its first total includes spend of the first CLI.
        ScriptedCli([costed("second answer", 0.16), costed("third answer", 0.19)]),
    ]

    async def scenario() -> tuple[list[list[HarnessEvent]], list[str | None]]:
        adapter = ResumingAdapter(lambda: clis.pop(0))
        chat = Chat(tmp_path, adapter=adapter)
        turns = []
        first, _ = await chat.send("first")
        turns.append(first)
        connection = chat.service._connections[first.harness_session_id]
        await _until(lambda: connection.connection_state == "disconnected")
        for prompt in ("second", "third"):
            turn, _ = await chat.send(prompt)
            turns.append(turn)
        events = [chat.service.activity_events(turn.id).events for turn in turns]
        await chat.service.shutdown()
        return events, adapter.resumed

    events, resumed = asyncio.run(scenario())

    assert resumed == [None, SESSION]
    first, second, third = (turn_cost(turn_events) for turn_events in events)
    assert first == pytest.approx(0.1)
    # Where the resumed CLI's total started is unknown, so no cost is invented.
    assert second is None
    assert final_usage(events[1]).payload["session_cost"] == {
        "amount": 0.16,
        "currency": "USD",
    }
    assert third == pytest.approx(0.03)


def test_claude_session_total_that_drops_starts_a_new_baseline(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[list[HarnessEvent]]:
        cli = ScriptedCli(
            [
                costed("first answer", 0.3),
                # The CLI reset its running totals during this turn (as a
                # conversation reset does) and spent 0.02 after the reset.
                costed("second answer", 0.02),
                costed("third answer", 0.05),
            ]
        )
        connection = await _connection(cli, tmp_path)
        turns = [
            await _turn(connection, prompt) for prompt in ("first", "second", "third")
        ]
        await connection.close()
        return turns

    turns = asyncio.run(scenario())

    assert [turn_cost(events) for events in turns] == pytest.approx([0.3, 0.02, 0.03])


def test_claude_steered_turn_cost_covers_both_cli_turns(tmp_path: Path) -> None:
    async def scenario() -> list[list[HarnessEvent]]:
        cli = QueueingCli(
            {
                "Open question": queued_costed("Nothing yet.", 0.1),
                "Summarize the scan": queued_costed(
                    "Two hosts are up.", 0.25, pause=0.3
                ),
                # The queued guidance runs as a CLI turn of its own.
                "Also list open ports": queued_costed(
                    "Ports 22 and 443 are open.", 0.4
                ),
                "Next question": queued_costed("Answer to the next question.", 0.45),
            }
        )
        connection = await _claude(cli, tmp_path)
        first = await asyncio.wait_for(
            _collect(connection.run_turn("Open question", model="m")),
            TURN_DEADLINE_SECONDS,
        )
        steered = await _steered_turn(
            connection, "Summarize the scan", "Also list open ports"
        )
        following = await asyncio.wait_for(
            _collect(connection.run_turn("Next question", model="m")),
            TURN_DEADLINE_SECONDS,
        )
        await connection.close()
        return [first, steered, following]

    first, steered, following = asyncio.run(scenario())

    assert steered[-1].message == "Two hosts are up.\n\nPorts 22 and 443 are open."
    # Both CLI turns that answered the steered turn, and nothing before it.
    assert [turn_cost(events) for events in (first, steered, following)] == (
        pytest.approx([0.1, 0.3, 0.05])
    )
    assert final_usage(steered).payload["session_cost"] == {
        "amount": 0.4,
        "currency": "USD",
    }
    steered_usage = turn_usage(steered)
    assert (steered_usage.input_tokens, steered_usage.output_tokens) == (6, 4)


BACKGROUND_TURN = [
    user_text(
        "<task-notification>scan done</task-notification>",
        origin={"kind": "task-notification"},
    ),
    *stream_text("Background scan finished."),
    result(
        text="Background scan finished.",
        origin={"kind": "task-notification"},
        total_cost_usd=0.15,
    ),
]


@pytest.mark.parametrize("out_of_band", ["stopped_turn", "background_turn"])
def test_claude_turn_is_not_billed_for_spend_before_its_prompt(
    tmp_path: Path, out_of_band: str
) -> None:
    async def scenario() -> list[HarnessEvent]:
        if out_of_band == "stopped_turn":
            cli = ScriptedCli(
                [
                    costed("first answer", 0.1),
                    [*stream_text("partial "), WAIT],
                    costed("next answer", 0.17),
                ],
                on_interrupt=[
                    user_text("[Request interrupted by user]"),
                    result(
                        "error_during_execution",
                        text=None,
                        terminal_reason="aborted_streaming",
                        total_cost_usd=0.15,
                    ),
                ],
            )
        else:
            cli = ScriptedCli(
                [
                    [*costed("first answer", 0.1), ("SLEEP", 0.05), *BACKGROUND_TURN],
                    costed("next answer", 0.17),
                ]
            )
        connection = await _connection(cli, tmp_path)
        await _turn(connection, "first")
        if out_of_band == "stopped_turn":
            stopped = connection.run_turn("second", model="m")
            async for event in stopped:
                if event.type == "message_delta":
                    break
            # Stopped: the next prompt interrupts and drains the CLI turn.
            await stopped.aclose()
        else:
            # The CLI ran a turn of its own for a finished background task.
            await _until(lambda: cli.delivered(10))
        following = await _turn(connection, "next")
        await connection.close()
        return following

    following = asyncio.run(scenario())

    assert following[-1].message == "next answer"
    assert turn_cost(following) == pytest.approx(0.02)
