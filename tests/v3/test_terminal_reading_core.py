"""Catch-up, quoting, evidence and turn details through a paired TLS LAN Core."""

from __future__ import annotations

import json
import os
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def settle():
    # Without type-ahead, Enter during a command's last Core read is dropped;
    # let the shell return to its prompt before typing the next command.
    time.sleep(1.5)


def provider_chat(admin):
    return next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )


def test_paired_tui_catch_up_quote_evidence_and_turn_details(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Please review the synthetic deployment checklist in detail")
    terminal.expect_screen("Response complete.")
    session = provider_chat(admin)
    settle()
    terminal.close()

    # Reopening the chat starts this device's read cursor without a notice.
    terminal = data["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        session["id"],
        tty=True,
        columns=120,
        rows=36,
    )
    terminal.expect_screen("/help for commands", absent=("While you were away",))
    settle()
    terminal.close()

    # Work continues elsewhere: another client adds a turn to the same chat.
    reply = admin.post(
        "chat/completions",
        json={
            "engagement_id": "terminal-project",
            "session_id": session["id"],
            "backend": "provider",
            "provider_id": "provider-a",
            "model": "model-a",
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": "Summarize the remaining synthetic checklist items now",
                }
            ],
        },
    )
    assert reply.is_success, reply.text

    terminal = data["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        session["id"],
        tty=True,
        columns=120,
        rows=36,
    )
    terminal.expect_screen("While you were away: 1 update", "/catchup reviews them")
    settle()
    terminal.send("/catchup read")
    terminal.expect_screen("Marked this conversation as read on this device.")
    settle()
    terminal.send("/catchup")
    terminal.expect_screen("Nothing new since this device last read")
    settle()

    terminal.send("/turn")
    terminal.expect_screen("| Saved message |")
    terminal.send("assistant: Synthetic")
    terminal.expect_screen("Response complete · model-a")
    # /turn opens the source message in the transcript, which takes focus for
    # reading (footer "Tab Write"); Tab returns to the composer.
    terminal.expect_screen("Tab Write")
    os.write(terminal.input_fd, b"\t")
    terminal.expect_screen("Message or /command", absent=("Tab Write",))
    settle()

    terminal.send("/evidence")
    terminal.expect_screen("| Saved message |")
    terminal.send("assistant: Synthetic")
    terminal.expect_screen("Supporting evidence for: Synthetic provider answer.")
    settle()

    terminal.send("/quote")
    terminal.expect_screen("| Saved message |")
    terminal.send("assistant: Synthetic")
    # As in the GUI, the quote ends with a blank line and the cursor waits below
    # it, so the reply typed next follows the quote.
    terminal.expect_screen("Quoted into your draft")
    settle()
    terminal.send("Agreed, ship it.")
    # "Response complete." is already on screen, so read Core until the quoted
    # message and its answer are saved.
    deadline = time.monotonic() + 30
    while True:
        rows = admin.get(f"chat/sessions/{session['id']}/messages").json()
        users = [row["content"] for row in rows if row["role"] == "user"]
        answers = [row for row in rows if row["role"] == "assistant"]
        if len(users) == 3 and len(answers) == 3:
            break
        assert time.monotonic() < deadline, rows
        time.sleep(0.5)
    assert users[-1].startswith("> Synthetic provider answer.\n\n")
    assert users[-1].rstrip().endswith("Agreed, ship it.")
    settle()
    terminal.close()

    # The answer was watched live after the catch-up was reviewed, so it is read:
    # reopening shows no catch-up, as the GUI marks what it renders at the bottom.
    terminal = data["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        session["id"],
        tty=True,
        columns=120,
        rows=36,
    )
    terminal.expect_screen("/help for commands", absent=("While you were away",))
    settle()
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic project",
                "checks": [
                    "first visit starts the read cursor silently",
                    "catch-up summary after another client's turn; mark read",
                    "turn details for an answer",
                    "supporting evidence for an answer",
                    "quote an answer into the draft and send it",
                    "an answer watched live is read on reopen",
                ],
                "result": "passed",
            }
        )
    )
