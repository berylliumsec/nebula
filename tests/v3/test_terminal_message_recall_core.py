"""Recall sent messages through the paired full-screen client and real Core history."""

from __future__ import annotations

import os

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_up_recalls_sent_messages_and_down_restores_draft(lan_core):
    print(f"Paired LAN Core: {lan_core['origin']}")
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=100, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("Recall alpha", expect="Response complete.")
    terminal.send("Recall beta", expect="Response complete.")
    terminal.expect_screen("Ready for a message")

    os.write(terminal.input_fd, b"\x1b[A")
    terminal.expect_screen("> Recall beta", "Recalled message 2/2")
    os.write(terminal.input_fd, b"\x1b[A")
    terminal.expect_screen("> Recall alpha", "Recalled message 1/2")
    os.write(terminal.input_fd, b"\x1b[B")
    terminal.expect_screen("> Recall beta", "Recalled message 2/2")
    os.write(terminal.input_fd, b"\x1b[B")
    terminal.expect_screen("↑ Recall", absent=("Recalled message", "> Recall beta"))

    os.write(terminal.input_fd, b"current draft")
    terminal.expect_screen("> current draft")
    os.write(terminal.input_fd, b"\x1b[A")
    terminal.expect_screen("> Recall beta")
    os.write(terminal.input_fd, b"\x1b[B")
    terminal.expect_screen("> current draft")

    admin = lan_core["admin"]
    sessions = admin.get(
        "chat-sessions", params={"engagement_id": "terminal-project"}
    ).json()
    session = next(
        row
        for row in sessions
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )
    path = f"chat/sessions/{session['id']}/messages"
    saved = admin.get(path).json()
    assert [row["content"] for row in saved if row["role"] == "user"] == [
        "Recall alpha",
        "Recall beta",
    ]

    os.write(terminal.input_fd, b"\x03")  # Clear the unsent draft before quitting.
    terminal.expect_screen("Draft cleared", absent=("> current draft",))
    terminal.send("/quit")
    terminal.close(send_quit=False)

    reopened = lan_core["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        session["id"],
        tty=True,
        columns=100,
        rows=30,
    )
    reopened.expect("/help for commands")
    reopened.expect_screen("Ready for a message")
    os.write(reopened.input_fd, b"\x1b[A")
    reopened.expect_screen("> Recall beta")
    os.write(reopened.input_fd, b"\x1b[B")
    reopened.expect_screen("↑ Recall", absent=("Recalled message", "> Recall beta"))
    reopened.send("/quit")
    reopened.close(send_quit=False)
    assert admin.get(path).json() == saved
