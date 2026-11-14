"""Project tool follow-up settings through a paired terminal on a TLS LAN Core."""

from __future__ import annotations

import json
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_saves_tool_followup_runtime_and_toggles(lan_core):
    data = lan_core
    admin = data["admin"]
    path = "engagements/terminal-project/post-tool-assistant"
    before = admin.get(path).json()
    assert before["take_notes"] is False
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")

    terminal.send("/project followup")
    terminal.expect_screen("| Tool follow-up |", "Analysis runtime: not chosen")
    terminal.send("Analysis runtime")
    terminal.expect_screen("| Tool follow-up runtime |", "Synthetic provider")
    terminal.send("Synthetic provider")
    terminal.expect_screen("| Tool follow-up model |", "model-a")
    terminal.send("model-a")
    terminal.expect_screen(
        "| Tool follow-up |", "Analysis runtime: provider provider-a"
    )
    saved = admin.get(path).json()
    assert saved["provider_id"] == "provider-a" and saved["model"] == "model-a"
    assert saved["cloud_confirmed"] is False  # The fixture provider is local.

    terminal.send("Take notes")
    terminal.expect_screen("| Tool follow-up |", "Take notes: on")
    after = admin.get(path).json()
    assert after["take_notes"] is True
    assert after["revision"] > saved["revision"]
    terminal.send("Done")
    terminal.expect_screen("Message or /command", absent=("| Tool follow-up |",))
    time.sleep(1.5)
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert local provider and synthetic project",
                "checks": [
                    "tool follow-up runtime chosen from enabled providers",
                    "model limited to the provider allowlist",
                    "whole config saved with the engagement revision and read back",
                    "take notes toggled with Core readback",
                ],
                "result": "passed",
            }
        )
    )
