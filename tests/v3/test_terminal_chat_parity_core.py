"""Chat lifecycle parity through a paired full-screen terminal on a TLS LAN Core."""

from __future__ import annotations

import json
import os

from nebula.v3.providers import (
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    StreamEventType,
)
from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core

THOUGHT = "Weighing the synthetic options before answering."


def think_aloud(provider) -> None:
    """Make the inert provider reason when asked to think aloud."""
    stream_original, complete_original = provider.stream, provider.complete

    def asked(request) -> bool:
        return any(
            item.role == "user" and "think aloud" in str(item.content)
            for item in request.messages[-3:]
        )

    def response(request) -> ModelResponse:
        return ModelResponse(
            provider_id=provider.config.id,
            model=request.model or "model-a",
            text="Synthetic considered answer.",
            reasoning=THOUGHT,
            usage=ModelUsage(input_tokens=5, output_tokens=4, total_tokens=9),
            finish_reason="stop",
        )

    async def complete(request):
        return response(request) if asked(request) else await complete_original(request)

    async def stream(request):
        if not asked(request):
            async for event in stream_original(request):
                yield event
            return
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        yield ModelStreamEvent(type=StreamEventType.REASONING_DELTA, delta=THOUGHT)
        yield ModelStreamEvent(
            type=StreamEventType.TEXT_DELTA, delta="Synthetic considered answer."
        )
        yield ModelStreamEvent(
            type=StreamEventType.COMPLETED, response=response(request)
        )

    provider.stream = stream
    provider.complete = complete


def main_chats(admin):
    return [
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if not (row.get("metadata") or {}).get("control_assistant")
        and not (row.get("metadata") or {}).get("side_chat")
    ]


def provider_chat(admin):
    return next(row for row in main_chats(admin) if row["backend"] == "provider")


def open_new_chat(data, columns=120, rows=36):
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=columns, rows=rows
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    return terminal


def test_paired_tui_thinking_turn_details_and_edit_in_place(lan_core):
    data = lan_core
    admin = data["admin"]
    think_aloud(data["provider"])
    terminal = open_new_chat(data)
    terminal.send("Please think aloud about the synthetic plan")
    display = terminal.expect_screen(
        "Response complete.",
        "Thinking · ",
        "/thinking shows it",
        "9 tokens",
        absent=(THOUGHT,),
    )
    assert "ASSISTANT · " in display
    terminal.send("/thinking")
    terminal.expect_screen("THINKING", THOUGHT, "Thinking is shown")
    terminal.send("/thinking")
    terminal.expect_screen("Thinking is collapsed", absent=(THOUGHT,))

    session = provider_chat(admin)
    terminal.send("/chat edit")
    terminal.expect_screen("Saved message")
    terminal.send("user: Please think")
    terminal.expect_screen("Editing saved message", "Enter resends from here")
    # Nothing is rewound until the edit is sent.
    assert [
        row["content"]
        for row in admin.get(f"chat/sessions/{session['id']}/messages").json()
        if row["role"] == "user"
    ] == ["Please think aloud about the synthetic plan"]
    os.write(terminal.input_fd, b"\x05")  # End of line.
    terminal.send(" (edited)")
    terminal.expect_screen("Response complete.", "(edited)")
    saved = admin.get(f"chat/sessions/{session['id']}/messages").json()
    assert [row["content"] for row in saved if row["role"] == "user"] == [
        "Please think aloud about the synthetic plan (edited)"
    ]
    assert [row["role"] for row in saved if row["role"] in {"user", "assistant"}] == [
        "user",
        "assistant",
    ]
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider streaming synthetic reasoning",
                "checks": [
                    "thinking collapsed by default and shown with /thinking",
                    "answer heading shows elapsed time and tokens",
                    "edit restores the message without rewinding",
                    "send rewinds and saves only the edited request",
                ],
                "result": "passed",
            }
        )
    )


def test_paired_tui_bookmark_search_side_chat_and_bulk_archive(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = open_new_chat(data)
    terminal.send("Remember the zebra checklist")
    terminal.expect_screen("Response complete.")
    session = provider_chat(admin)

    terminal.send("/bookmark")
    terminal.expect_screen("Saved message")
    terminal.send("zebra")
    terminal.expect_screen("Bookmarked user message")
    marks = admin.get(f"chat/sessions/{session['id']}/bookmarks").json()
    assert [row["active"] for row in marks] == [True]

    terminal.send("/search zebra")
    terminal.expect_screen("messages in this conversation", "Remember the zebra")
    terminal.send("Bookmarked messages only")
    terminal.expect_screen("Bookmarked messages in this conversation")
    terminal.send("Remember the zebra")
    terminal.expect_screen("PgUp/PgDn Read")
    os.write(terminal.input_fd, b"\t")  # Back to the composer.

    terminal.send("/side new")
    terminal.expect_screen("Side chat opened", "Side chat #1")
    terminal.send("A quick side question")
    terminal.expect_screen("Response complete.")
    sides = admin.get(f"chat/sessions/{session['id']}/side-chats").json()
    assert len(sides) == 1 and sides[0]["metadata"]["side_chat"] is True
    side_messages = admin.get(f"chat/sessions/{sides[0]['id']}/messages").json()
    assert "A quick side question" in [row["content"] for row in side_messages]
    main_messages = admin.get(f"chat/sessions/{session['id']}/messages").json()
    assert "A quick side question" not in [row["content"] for row in main_messages]

    terminal.send("/side close")
    terminal.expect_screen("Close and discard this side chat?")
    terminal.send("yes")
    terminal.expect_screen("Side chat closed")
    assert admin.get(f"chat/sessions/{session['id']}/side-chats").json() == []

    count = len(main_chats(admin))
    terminal.send("/chat bulk")
    terminal.expect_screen("Select conversations")
    # Space toggles in a multi-select picker, so search with one word.
    terminal.send("all")
    terminal.expect_screen("1 selected", "[x] Select all")
    os.write(terminal.input_fd, b"\x13")  # Ctrl-S saves the selection.
    terminal.expect_screen(f"Action for {count} conversations")
    terminal.send("Archive")
    terminal.expect_screen(f"Archive: {count} of {count} conversations confirmed")
    assert all(row["metadata"].get("archived_at") for row in main_chats(admin))
    terminal.send("/chats")
    terminal.expect_screen("Conversation", "· archived")
    os.write(terminal.input_fd, b"\x07")  # Ctrl-G closes the picker.
    terminal.expect_screen("Message or /command", absent=("· archived",))
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider and synthetic project",
                "checks": [
                    "bookmark saved with Core readback",
                    "search scoped to conversation and bookmarks",
                    "side chat created, used and discarded; main chat unchanged",
                    "bulk archive verified and marked in /chats",
                ],
                "result": "passed",
            }
        )
    )
