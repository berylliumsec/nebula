"""Goal completion and child goals through a paired terminal on a TLS LAN Core."""

from __future__ import annotations

import asyncio
import json
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def hold_goal_turns(provider) -> None:
    """Keep goal turns running so the goal stays running while it is managed."""
    stream_original, complete_original = provider.stream, provider.complete

    def goal_turn(request) -> bool:
        return any(
            item.role == "user" and "active conversation goal" in str(item.content)
            for item in request.messages[-2:]
        )

    async def complete(request):
        if goal_turn(request):
            await asyncio.Event().wait()
        return await complete_original(request)

    async def stream(request):
        if goal_turn(request):
            await asyncio.Event().wait()
        async for event in stream_original(request):
            yield event

    provider.stream = stream
    provider.complete = complete


def wait_for(read, check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if check(value):
            return value
        time.sleep(0.25)
    raise AssertionError(f"Core did not reach the expected state: {value!r}")


def test_paired_tui_starts_child_goal_and_completes_goal_with_evidence(lan_core):
    data = lan_core
    admin = data["admin"]
    hold_goal_turns(data["provider"])
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Start a synthetic goal chat")
    terminal.expect_screen("Response complete.")
    session = next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )
    goal_path = f"chat/sessions/{session['id']}/goal"

    terminal.send("/goal create")
    for prompt, answer in (
        ("Goal objective", "Prepare the synthetic release notes"),
        ("Completion criteria", "Notes reviewed"),
        ("Plan steps", ""),
        ("Resume instruction", ""),
        ("Token budget", ""),
        ("Active-time budget", ""),
        ("Step budget", ""),
        ("Child-goal budget", "2"),
    ):
        terminal.expect_screen(prompt)
        terminal.send(answer)
    terminal.expect_screen("Goal saved in Core")
    terminal.send("/goal start")
    terminal.expect_screen("Core goal status: running")

    terminal.send("/goal child")
    terminal.expect_screen("Child goal objective")
    terminal.send("Collect the synthetic changelog")
    terminal.expect_screen("Open the child goal's conversation?")
    terminal.send("yes")
    children = wait_for(
        lambda: admin.get(goal_path + "/children").json(), lambda rows: len(rows) == 1
    )
    child_session = children[0]["session_id"]
    terminal.expect_screen(
        "(child)", "Ready for a message", absent=("Child goal objective",)
    )
    state = json.loads(
        (data["config"]).read_text()
    )  # The client remembers the opened child chat as its selection.
    selection = next(iter(state["cores"].values()))["selection"]
    assert selection["session_id"] == child_session

    terminal.send(f"/chats {session['id']}")
    terminal.expect_screen(
        "Terminal Project / ", "Message or /command", absent=("(child)",)
    )
    terminal.send("/goal complete")
    terminal.expect_screen("Complete this goal?")
    terminal.send("yes")
    terminal.expect_screen("Completion summary")
    terminal.send("Release notes prepared")
    terminal.expect_screen("Completion evidence")
    terminal.send("Notes reviewed by the operator")
    terminal.expect_screen("More evidence")
    terminal.send("")
    terminal.expect_screen("Core goal status: completed")
    goal = admin.get(goal_path).json()
    assert goal["status"] == "completed"
    assert goal["completion_evidence"] == [
        {"kind": "operator_confirmation", "detail": "Notes reviewed by the operator"}
    ]
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic project",
                "checks": [
                    "goal created with a child budget and started",
                    "child goal started and its conversation opened",
                    "goal completed with operator evidence",
                ],
                "result": "passed",
            }
        )
    )
