"""Retained results and harness activity through a paired TLS LAN Core.

The inert fixtures produce no tool output artifacts, native subagent tasks or
file checkpoints; those actions are covered by tests/test_terminal_inspect.py
against Core's documented shapes. This journey checks the real routes the new
/results browser and /activity harness step read.
"""

from __future__ import annotations

import json
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def settle():
    # Without type-ahead, Enter during a command's last Core read is dropped;
    # let the shell return to its prompt before typing the next command.
    time.sleep(2)


def test_paired_tui_results_browser_and_harness_activity(lan_core):
    data = lan_core
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Answer a synthetic results question")
    terminal.expect_screen("Response complete.")
    terminal.send("/results")
    terminal.expect_screen("No retained results in this conversation yet.")
    settle()

    # A harness conversation reads its turn's activity; with no stoppable task or
    # advertised checkpoint rewind, no action menu appears.
    terminal.send("/chats harness-chat")
    terminal.expect_screen("Harness chat", "Ready for a message")
    settle()
    terminal.send("/activity")
    # /activity prints state first, then reads the harness turn; wait for it to
    # return to the prompt so /quit is not typed while it is still running.
    settle()
    terminal.expect_screen("Message or /command", absent=("| Harness activity |",))
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic harness",
                "checks": [
                    "/results reads Core's retained results route",
                    "/activity reads harness turn events without offering actions",
                ],
                "result": "passed",
            }
        )
    )
