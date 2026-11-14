"""Follow-ups, mid-chat runtime switching and MCP delivery on a paired TLS LAN Core."""

from __future__ import annotations

import json
import os
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def provider_chat(admin):
    return next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )


def wait_for(read, check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if check(value):
            return value
        time.sleep(0.25)
    raise AssertionError(f"Core did not reach the expected state: {value!r}")


def user_messages(admin, session_id):
    return [
        row["content"]
        for row in admin.get(f"chat/sessions/{session_id}/messages").json()
        if row["role"] == "user"
    ]


def test_paired_tui_queue_move_remove_and_stop_and_send(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Please hold response while I queue follow-ups")
    terminal.expect_screen("WORKING")
    assert data["provider"].holding.wait(30)
    session = provider_chat(admin)
    queue_path = f"chat/sessions/{session['id']}/queue"

    terminal.send("Follow-up alpha")
    terminal.expect_screen("Saved in Core's follow-up queue.")
    terminal.position = len(terminal.output)
    terminal.send("Follow-up bravo")
    # Wait for the shell's own Core readback, not just Core's write, so the
    # next command is typed at a ready prompt.
    terminal.expect("Saved in Core's follow-up queue.")
    wait_for(lambda: admin.get(queue_path).json(), lambda q: len(q["items"]) == 2)

    terminal.send("/queue move")
    terminal.expect_screen("Search:", "↑↓ Select", "2. Queued · Follow-up bravo")
    terminal.send("bravo")
    terminal.expect_screen("Move follow-up", "Send first")
    terminal.send("Send first")
    terminal.expect_screen("1. Queued · Follow-up bravo", "2. Queued · Follow-up alpha")
    queue = admin.get(queue_path).json()
    assert [row["request"]["messages"][-1]["content"] for row in queue["items"]] == [
        "Follow-up bravo",
        "Follow-up alpha",
    ]

    terminal.send("/queue remove")
    terminal.expect_screen("Search:", "↑↓ Select", "Follow-up alpha")
    terminal.send("alpha")
    terminal.expect_screen("1 waiting")
    waiting = [
        row
        for row in admin.get(queue_path).json()["items"]
        if row["status"] == "queued"
    ]
    assert [row["request"]["messages"][-1]["content"] for row in waiting] == [
        "Follow-up bravo"
    ]

    # Stop and send: the new message goes first, the running turn is cancelled,
    # and Core sends the queue in its new order without any client resend.
    terminal.send("/now Switch to the fallback plan")
    terminal.expect_screen("your message sends next", timeout=60)
    wait_for(
        lambda: user_messages(admin, session["id"]),
        lambda rows: (
            rows[:3]
            == [
                "Please hold response while I queue follow-ups",
                "Switch to the fallback plan",
                "Follow-up bravo",
            ]
        ),
        timeout=90,
    )
    final = wait_for(
        lambda: admin.get(queue_path).json(),
        lambda q: (
            not q["paused"]
            and all(row["status"] in {"complete", "cancelled"} for row in q["items"])
        ),
        timeout=90,
    )
    assert final["paused"] is False
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider holding a response",
                "checks": [
                    "busy text queued in Core",
                    "move a follow-up first with Core order readback",
                    "remove a follow-up",
                    "/now cancels the running turn and sends the message first",
                    "queue resumes and drains in order",
                ],
                "result": "passed",
            }
        )
    )


def test_paired_tui_switches_harness_chat_to_provider_and_mcp_schema_mode(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"](
        "shell",
        "--project",
        "Terminal Project",
        "--chat",
        "Harness chat",
        tty=True,
        columns=120,
        rows=36,
    )
    terminal.expect_screen("Harness chat", "/help for commands")
    terminal.send("/model")
    terminal.expect_screen("Model runtime", "Synthetic provider / model-a")
    terminal.send("Synthetic provider / model-a")
    terminal.expect_screen("Runtime switch saved in Core")
    config = admin.get("chat-sessions/harness-chat/assistant-config").json()
    assert config["settings"]["backend"] == "provider"
    assert config["settings"]["profile_id"] == "provider-a"
    defaults = admin.get("engagements/terminal-project/assistant-defaults").json()
    assert defaults["backend"] == "provider" and defaults["provider_id"] == "provider-a"

    before = admin.get("chat-sessions/harness-chat/assistant-config").json()
    mode = before["settings"].get("mcp_context_mode") or "on_demand"
    flipped = "on_demand" if mode == "always_loaded" else "always_loaded"
    terminal.send("/mcp")
    terminal.expect_screen("Search:", "Always load selected tool schemas")
    terminal.send("Always")
    terminal.expect_screen(
        ("[x]" if flipped == "always_loaded" else "[ ]")
        + " Always load selected tool schemas"
    )
    os.write(terminal.input_fd, b"\x13")  # Ctrl-S saves.
    terminal.expect_screen(
        "tool schemas "
        + ("always loaded" if flipped == "always_loaded" else "loaded on demand")
    )
    config = admin.get("chat-sessions/harness-chat/assistant-config").json()
    assert config["settings"]["mcp_context_mode"] == flipped
    defaults = admin.get("engagements/terminal-project/assistant-defaults").json()
    assert defaults["mcp_context_mode"] == flipped

    terminal.send("Answer on the provider now")
    terminal.expect_screen("Response complete.")
    assert user_messages(admin, "harness-chat")[-1] == "Answer on the provider now"
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "synthetic harness chat and verified inert provider",
                "checks": [
                    "harness chat switched to a provider through runtime-switch",
                    "choice remembered as the project default",
                    "MCP tool schema mode saved and remembered",
                    "next turn answered by the provider",
                ],
                "result": "passed",
            }
        )
    )
