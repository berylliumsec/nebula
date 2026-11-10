"""Browse durable Work through a paired terminal on a disposable TLS LAN Core."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests import test_terminal_package
from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core
built_client = test_terminal_package.built_client


@pytest.fixture
def board_core(lan_core, built_client, tmp_path):
    installed = tmp_path / "installed-client"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-compile",
            "--target",
            str(installed),
            str(next(built_client.glob("*.whl"))),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lan_core["env"]["PYTHONPATH"] = str(installed)
    processes = []
    start = lan_core["start"]

    def tracked_start(*args, **kwargs):
        terminal = start(*args, **kwargs)
        processes.append(terminal)
        return terminal

    yield {**lan_core, "start": tracked_start}
    for terminal in processes:
        if terminal.process.poll() is None:
            os.write(terminal.input_fd, b"\x1b")
            terminal.close(send_quit=False, send_eof=True)


def test_paired_tui_board_browse_refresh_search_and_reconnect(board_core):
    lan_core = board_core
    print(f"Paired LAN Core: {lan_core['origin']}")
    admin = lan_core["admin"]
    path = "engagements/terminal-project/work"
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=100, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("/board", expect="Project Board")
    terminal.expect_screen("No Work items yet", "0 Work items")

    items = []
    for status in ("backlog", "ready", "in_progress", "blocked", "review", "done"):
        response = admin.post(
            path,
            json={
                "title": f"Board task {status}",
                "description": "Complete description visible in details. " * 20,
                "status": status,
                "priority": "high",
            },
        )
        assert response.status_code == 200, response.text
        items.append(response.json())
    # Agent posting can be off without hiding the operator's saved Work records.
    disabled = admin.patch(path + "/setting", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    first_page = admin.get(path, params={"offset": 0, "limit": 1})
    second_page = admin.get(path, params={"offset": 1, "limit": 1})
    assert len(first_page.json()) == len(second_page.json()) == 1
    assert first_page.json()[0]["id"] != second_page.json()[0]["id"]
    assert admin.get(path, params={"offset": -1}).status_code == 422
    update = admin.post(
        path + f"/{items[0]['id']}/updates",
        json={
            "summary": "Reviewed source",
            "next_step": "Read trace",
            "blocker": "Needs review",
        },
    )
    assert update.status_code == 200, update.text

    os.write(terminal.input_fd, b"r")
    terminal.expect_screen("6 Work items", "Board task backlog")
    os.write(terminal.input_fd, b"/backlog\r")
    terminal.expect_screen("Blocked (1)")
    terminal.expect_screen("Complete description visible")
    os.write(terminal.input_fd, b"\t\x1b[1;5F")  # Details, then Ctrl-End.
    terminal.expect_screen(
        "Reviewed source", "Next: Read trace", "Blocked: Needs review"
    )
    os.write(terminal.input_fd, b"/\x01\x0bblocked\r")
    terminal.expect_screen("Board task blocked", "Blocked (1)")
    # The selected item disappears in Core while the board is open.
    # Work has no delete route; move it out of the current search instead.
    changed = admin.patch(
        path + f"/{items[3]['id']}",
        json={
            "title": "Renamed externally",
        },
    )
    assert changed.status_code == 200, changed.text
    os.write(terminal.input_fd, b"r")
    terminal.expect_screen("No matching Work items")
    os.write(terminal.input_fd, b"/\x01\x0b\r")  # Clear search.
    terminal.expect_screen("Board task backlog")
    os.write(terminal.input_fd, b"\x1b")
    terminal.expect_screen("Message or /command", absent=("Project Board",))
    # Enter through discovery while keeping an existing unsent draft.
    os.write(terminal.input_fd, b"Unsent board draft\x1bOS")
    terminal.expect_screen("Command")
    terminal.send("board", expect="Project Board")
    os.write(terminal.input_fd, b"\x1b")
    terminal.expect_screen("Unsent board draft", absent=("Project Board",))
    os.write(terminal.input_fd, b"\x03")  # Clear draft, not stop Core work.
    terminal.send("/quit")
    terminal.close(send_quit=False)

    narrow = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=54, rows=20
    )
    narrow.expect("New conversation")
    narrow.send("New conversation", expect="/help for commands")
    narrow.send("/board", expect="Project Board")
    narrow.expect_screen("6 Work items", "Board task backlog")
    os.write(narrow.input_fd, b"/Renamed\r")
    narrow.expect_screen("Renamed externally")
    os.write(narrow.input_fd, b"\t")
    narrow.expect_screen("Complete description visible")
    os.write(narrow.input_fd, b"\x1b")
    narrow.expect_screen("Message or /command", absent=("Project Board",))
    narrow.send("/quit")
    narrow.close(send_quit=False)
