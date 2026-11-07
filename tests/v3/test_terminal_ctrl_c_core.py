"""Exercise two-stage Ctrl-C through the paired full-screen terminal client."""

from __future__ import annotations

import os

import pytest

from nebula.v3.domain import ChatTurn
from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


@pytest.mark.asyncio
async def test_paired_tui_ctrl_c_clears_draft_then_stops_selected_turn(lan_core):
    print(f"Paired LAN Core: {lan_core['origin']}")
    terminal = lan_core["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=100, rows=30
    )
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")

    os.write(terminal.input_fd, b"unsent draft")
    terminal.expect_screen("unsent draft")
    os.write(terminal.input_fd, b"\x03")
    terminal.expect_screen("^C again to stop turn", absent=("unsent draft",))

    terminal.send("hold response")
    assert lan_core["provider"].holding.wait(15), (
        "Inert provider did not reach its hold"
    )
    sessions = (
        lan_core["admin"]
        .get("chat-sessions", params={"engagement_id": "terminal-project"})
        .json()
    )
    provider = next(row for row in sessions if row["backend"] == "provider")
    path = f"chat/sessions/{provider['id']}"
    active = lan_core["admin"].get(path + "/state").json()
    assert active["busy"], active

    os.write(terminal.input_fd, b"another unsent draft")
    terminal.expect_screen("another unsent draft")
    os.write(terminal.input_fd, b"\x03")
    terminal.expect_screen("^C again to stop turn", absent=("another unsent draft",))
    assert lan_core["admin"].get(path + "/state").json()["busy"]
    os.write(terminal.input_fd, b"\x03")
    terminal.expect("Turn cancelled.")
    assert (
        await lan_core["store"].get(ChatTurn, active["turn_id"])
    ).status.value == "cancelled"
    terminal.close()
