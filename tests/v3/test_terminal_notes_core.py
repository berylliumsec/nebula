"""Project Notes through a paired full-screen terminal and disposable LAN Core."""

from __future__ import annotations

import os

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_notes_create_conflict_recovery_and_delete(lan_core):
    print(f"Paired LAN Core: {lan_core['origin']}")
    admin = lan_core["admin"]
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=100, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("/notes", expect="Project Notes")
    terminal.expect_screen("No notes yet · Ctrl-N creates one")

    os.write(terminal.input_fd, b"\x0e")  # Ctrl-N creates a draft.
    terminal.expect_screen("New note")
    os.write(terminal.input_fd, b"Terminal research\tFirst Markdown note.\x13")
    terminal.expect_screen("Saved in Core", "Terminal research")
    rows = admin.get("observations", params={"engagement_id": "terminal-project"})
    assert rows.status_code == 200, rows.text
    saved = next(row for row in rows.json() if row["title"] == "Terminal research")
    assert saved["body"] == "First Markdown note."
    assert saved["revision"] >= 1

    # A different editor saves a new revision after this TUI loaded the note.
    changed = admin.patch(
        f"observations/{saved['id']}",
        json={
            "changes": {"title": "Updated in browser"},
            "expected_revision": saved["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    os.write(terminal.input_fd, b"\x1b")
    terminal.expect_screen("Message or /command", absent=("Project Notes",))
    terminal.send("/notes", expect="Project Notes")
    terminal.expect_screen("Updated in browser")
    os.write(terminal.input_fd, b"\r")  # Enter focuses the selected title.
    os.write(terminal.input_fd, b"\x01Local draft")  # Ctrl-A, then add a prefix.
    terminal.expect_screen("Unsaved")

    changed_again = admin.patch(
        f"observations/{saved['id']}",
        json={
            "changes": {"title": "Second browser edit"},
            "expected_revision": changed.json()["revision"],
        },
    )
    assert changed_again.status_code == 200, changed_again.text
    os.write(terminal.input_fd, b"\x13")  # Ctrl-S must preserve a conflicting draft.
    terminal.expect_screen("Note changed in Core", "Local draft")
    os.write(terminal.input_fd, b"\x12")  # First Ctrl-R warns about draft loss.
    terminal.expect_screen("Ctrl-R again discards", "Local draft")
    os.write(terminal.input_fd, b"\x12")
    terminal.expect_screen("Second browser edit", absent=("Local draft",))

    os.write(terminal.input_fd, b"\t\t")  # Title -> body -> list.
    os.write(terminal.input_fd, b"d")
    terminal.expect_screen("Delete this note?")
    os.write(terminal.input_fd, b"y")
    terminal.expect_screen("Deleted from Core", "No notes yet")
    missing = admin.get(f"observations/{saved['id']}")
    assert missing.status_code == 404
    os.write(terminal.input_fd, b"\x0e")
    terminal.expect_screen("New note")
    os.write(terminal.input_fd, b"Unsent draft\x1b")
    terminal.expect_screen("Unsaved changes", "Unsent draft")
    os.write(terminal.input_fd, b"d")
    terminal.expect_screen("Message or /command", absent=("Project Notes",))
    terminal.send("/quit")
    terminal.close(send_quit=False)

    narrow = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=54, rows=20
    )
    narrow.expect("New conversation")
    narrow.send("New conversation", expect="/help for commands")
    narrow.send("/notes", expect="Project Notes")
    narrow.expect_screen("No notes yet · Ctrl-N creates one")
    os.write(narrow.input_fd, b"\x1b")
    narrow.expect_screen("Message or /command", absent=("Project Notes",))
    narrow.send("/quit")
    narrow.close(send_quit=False)
