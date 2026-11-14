"""Typed-ahead commands and multi-word picker search on a paired TLS LAN Core."""

from __future__ import annotations

import json
import os
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_runs_typed_ahead_command_and_searches_with_spaces(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Start a synthetic input chat")
    terminal.expect_screen("Response complete.")

    # The second command is entered while /history is still reading from Core.
    os.write(terminal.input_fd, b"/history\r/thinking\r")
    terminal.expect_screen(
        "Thinking is shown in the transcript.", absent=("> /thinking",)
    )

    # A multi-word search in a multi-select picker types spaces into the search.
    terminal.send("/chat bulk")
    terminal.expect_screen("| Select conversations |")
    terminal.send("Select all")
    terminal.expect_screen("[x] Select all", "Search: Select all")
    os.write(terminal.input_fd, b"\x13")  # Ctrl-S saves the selection.
    terminal.expect_screen("Action for")
    os.write(terminal.input_fd, b"\x07")  # Ctrl-G leaves without changing anything.
    terminal.expect_screen("Message or /command", absent=("Action for",))
    chats = admin.get(
        "chat-sessions", params={"engagement_id": "terminal-project"}
    ).json()
    assert not any((row.get("metadata") or {}).get("archived_at") for row in chats)
    time.sleep(1.5)  # Let the cancelled bulk action return to the prompt.
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic project",
                "checks": [
                    "command typed while another runs is executed next",
                    "multi-word search in a multi-select picker",
                ],
                "result": "passed",
            }
        )
    )
