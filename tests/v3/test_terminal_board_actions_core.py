"""Work board actions through the installed terminal client on a TLS LAN Core."""

from __future__ import annotations

import json
import os

from tests.v3 import test_terminal_board_core as board_tests

lan_core = board_tests.lan_core
built_client = board_tests.built_client
board_core = board_tests.board_core

PATH = "engagements/terminal-project/work"


def keys(terminal, text):
    os.write(terminal.input_fd, text.encode())


def items(admin):
    return {row["title"]: row for row in admin.get(PATH).json()}


def test_paired_tui_board_creates_checks_in_tracks_links_and_toggles_tools(
    board_core,
):
    data = board_core
    admin = data["admin"]
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("Start a synthetic board chat")
    terminal.expect_screen("Response complete.")
    chat = next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )
    tools_before = admin.get("engagements/terminal-project").json()["work_enabled"]

    terminal.send("/board", expect="Project Board")
    terminal.expect_screen("0 Work items", "N new", "Press N to create one")

    keys(terminal, "a")
    terminal.expect_screen(
        "Agent Work tools are on" if not tools_before else "Agent Work tools are off"
    )
    assert (
        admin.get("engagements/terminal-project").json()["work_enabled"]
        is not tools_before
    )

    keys(terminal, "n")
    terminal.expect_screen("New Work item title")
    keys(terminal, "Board journey item\r")
    terminal.expect_screen("Description (optional)")
    keys(terminal, "Created from the terminal\r")
    terminal.expect_screen("Created Board journey item in Ready.", "1 Work items")
    created = items(admin)["Board journey item"]
    assert created["status"] == "ready"
    assert created["description"] == "Created from the terminal"

    keys(terminal, "u")
    terminal.expect_screen("Check-in summary")
    keys(terminal, "Started the work\r")
    terminal.expect_screen("Next step (optional)")
    keys(terminal, "Write the tests\r")
    terminal.expect_screen("Blocker (optional)")
    keys(terminal, "\r")
    terminal.expect_screen("Status after this check-in")
    keys(terminal, "\x15in_progress\r")  # Clear the default, choose a status.
    terminal.expect_screen("Check-in saved. Core status: In Progress.")
    updates = admin.get(f"{PATH}/{created['id']}/updates").json()
    assert [row["summary"] for row in updates] == ["Started the work"]
    assert items(admin)["Board journey item"]["status"] == "in_progress"

    # Core sets Blocked whenever a blocker is given; the board shows that.
    keys(terminal, "u")
    terminal.expect_screen("Check-in summary")
    keys(terminal, "Waiting on logs\r")
    terminal.expect_screen("Next step (optional)")
    keys(terminal, "\r")
    terminal.expect_screen("Blocker (optional)")
    keys(terminal, "Need the trace\r")
    terminal.expect_screen("Status after this check-in")
    keys(terminal, "\r")
    terminal.expect_screen("Core status: Blocked (you chose In Progress)")
    assert items(admin)["Board journey item"]["status"] == "blocked"

    keys(terminal, "t")
    terminal.expect_screen("Work item title for this conversation")
    keys(terminal, "\x15Tracked chat work\r")
    terminal.expect_screen("Tracking this conversation as Tracked chat work")
    tracked = items(admin)["Tracked chat work"]
    assert tracked["status"] == "in_progress"
    assert tracked["assignee_session_id"] == chat["id"]
    assert tracked["source_kind"] == "chat" and tracked["source_id"] == chat["id"]

    keys(terminal, "/Board journey\r")
    terminal.expect_screen("Board journey item", "Blocked (1)")
    keys(terminal, "l")
    terminal.expect_screen("Linked Board journey item to this conversation.")
    assert items(admin)["Board journey item"]["assignee_session_id"] == chat["id"]

    # Escape cancels a prompt without writing, then closes the board.
    before = admin.get(PATH).json()
    keys(terminal, "n")
    terminal.expect_screen("New Work item title")
    keys(terminal, "\x1b")
    terminal.expect_screen("Cancelled; nothing was saved.")
    assert admin.get(PATH).json() == before
    keys(terminal, "\x1b")
    terminal.expect_screen("Message or /command", absent=("Project Board",))

    terminal.send("/board tools")
    terminal.expect_screen(
        "Agent Work tools are off" if not tools_before else "Agent Work tools are on"
    )
    assert admin.get("engagements/terminal-project").json()["work_enabled"] is (
        tools_before
    )
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "installed terminal client wheel and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic project",
                "checks": [
                    "agent tools toggled with Core readback",
                    "new Work item created as Ready",
                    "check-in saved with chosen status",
                    "blocker check-in shows Core's Blocked status",
                    "conversation tracked as an in-progress item",
                    "existing item linked to the conversation",
                    "Escape cancels without a write",
                    "/board tools outside the panel",
                ],
                "result": "passed",
            }
        )
    )
