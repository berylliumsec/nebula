"""Read a synthetic project's Snapshot through the paired full-screen client."""

from __future__ import annotations

import os

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_snapshot_refresh_and_return_to_chat(lan_core):
    admin = lan_core["admin"]
    first = admin.post(
        "assets",
        json={
            "engagement_id": "terminal-project",
            "asset_type": "domain",
            "name": "example.test",
            "criticality": "medium",
        },
    )
    assert first.status_code == 201, first.text
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=100, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("/snapshot", expect="PROJECT SNAPSHOT")
    terminal.expect_screen("Assets       1", "No Work check-in")

    second = admin.post(
        "assets",
        json={
            "engagement_id": "terminal-project",
            "asset_type": "domain",
            "name": "second.example.test",
            "criticality": "medium",
        },
    )
    assert second.status_code == 201, second.text
    os.write(terminal.input_fd, b"r")
    terminal.expect_screen("Assets       2")
    os.write(terminal.input_fd, b"\x1b")
    terminal.expect_screen("Message or /command", absent=("PROJECT SNAPSHOT",))
    os.write(terminal.input_fd, b"\x1b[23~")  # F11 on xterm-compatible terminals.
    terminal.expect_screen("Assets       2")
    os.write(terminal.input_fd, b"\x1b")
    terminal.expect_screen("Message or /command", absent=("PROJECT SNAPSHOT",))
    terminal.send("/quit")
    terminal.close(send_quit=False)

    narrow = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=54, rows=20
    )
    narrow.expect("New conversation")
    narrow.send("New conversation", expect="/help for commands")
    narrow.send("/snapshot", expect="PROJECT SNAPSHOT")
    narrow.expect_screen("Project Snapshot", "Assets")
    os.write(narrow.input_fd, b"\x1b")
    narrow.expect_screen("Message or /command", absent=("PROJECT SNAPSHOT",))
    narrow.send("/quit")
    narrow.close(send_quit=False)
