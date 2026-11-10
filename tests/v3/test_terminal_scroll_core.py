"""Scroll a saved transcript through the paired TUI against an isolated LAN Core."""

from __future__ import annotations

import os

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_scrolls_saved_history_and_reopens_without_mutation(lan_core):
    print(f"Paired LAN Core: {lan_core['origin']}")
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    text = "\n\n".join(f"Transcript fixture {index:03}" for index in range(100))
    os.write(terminal.input_fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")
    terminal.expect_screen("Transcript fixture 099")
    os.write(terminal.input_fd, b"\r")
    terminal.expect_screen("Synthetic provider answer.", "Ready for a message")
    admin = lan_core["admin"]
    session = next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
    )
    path = f"chat/sessions/{session['id']}/messages"
    saved = admin.get(path).json()
    assert next(row["content"] for row in saved if row["role"] == "user") == text

    os.write(terminal.input_fd, b"unsent draft")
    terminal.expect_screen("> unsent draft")
    os.write(terminal.input_fd, b"\x1b[<64;45;8M" * 20)
    terminal.expect_screen(
        "Transcript fixture 085", absent=("Synthetic provider answer.",)
    )
    os.write(terminal.input_fd, b"\x1b[5~\x1b[1;5H")
    terminal.expect_screen("Transcript fixture 000", "PgUp/PgDn")
    os.write(terminal.input_fd, b"\x1b[24~")
    terminal.expect_screen("F12 Scroll")
    os.write(terminal.input_fd, b"\x1b[24~\x1b[1;5F")
    terminal.expect_screen("Synthetic provider answer.", "F12 Copy")
    os.write(terminal.input_fd, b"\t")
    terminal.expect_screen("> unsent draft")
    os.write(terminal.input_fd, b"\x03")
    terminal.expect_screen("Draft cleared", absent=("> unsent draft",))
    terminal.send("/quit")
    terminal.close(send_quit=False)

    reopened = lan_core["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        session["id"],
        tty=True,
        columns=120,
        rows=30,
    )
    reopened.expect_screen("Synthetic provider answer.", "Ready for a message")
    os.write(reopened.input_fd, b"\x1b[1;5H")
    reopened.expect_screen("Transcript fixture 000")
    os.write(reopened.input_fd, b"\t")
    reopened.send("/quit")
    reopened.close(send_quit=False)
    assert admin.get(path).json() == saved
