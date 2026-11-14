"""Allow a reported model from the terminal /model picker on a TLS LAN Core."""

from __future__ import annotations

import json
import time

from nebula.v3 import execution_kernel
from nebula.v3.providers import ProviderHealth
from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def test_paired_tui_allows_a_reported_model_and_switches_to_it(lan_core, monkeypatch):
    data = lan_core
    admin = data["admin"]
    provider = data["provider"]

    async def health():
        return ProviderHealth(
            provider_id="provider-a", healthy=True, models=["model-a", "model-b"]
        )

    provider.health = health
    # Core's provider health route builds its provider from the saved profile;
    # route it to the fixture's inert provider, which reports one more model.
    monkeypatch.setattr(
        execution_kernel, "provider_from_profile", lambda *_args: provider
    )
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")
    terminal.send("Start a synthetic model chat")
    terminal.expect_screen("Response complete.")
    time.sleep(1.5)  # Let the shell return to its prompt before the command.
    terminal.send("/model")
    terminal.expect_screen("| Model runtime |", "adds to allowed")
    terminal.send("adds to allowed")
    terminal.expect_screen(
        "| More Synthetic provider models · adds to allowed |", "model-b"
    )
    terminal.send("model-b")
    terminal.expect_screen("Added model-b to Synthetic provider's allowed models.")
    terminal.expect_screen("Runtime switch saved in Core")
    profile = admin.get("providers/provider-a").json()
    assert profile["model_allowlist"] == ["model-a", "model-b"]
    session = next(
        row
        for row in admin.get(
            "chat-sessions", params={"engagement_id": "terminal-project"}
        ).json()
        if row["backend"] == "provider"
        and not (row.get("metadata") or {}).get("control_assistant")
    )
    config = admin.get(f"chat-sessions/{session['id']}/assistant-config").json()
    assert config["settings"]["model"] == "model-b"
    time.sleep(1.5)  # The switch's readback finishes before /quit is typed.
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider reporting one unlisted model",
                "checks": [
                    "reported model offered under More models",
                    "allowlist PATCH with the health revision and readback",
                    "conversation switched to the newly allowed model",
                ],
                "result": "passed",
            }
        )
    )
