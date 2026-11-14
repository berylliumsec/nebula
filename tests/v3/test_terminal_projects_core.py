"""Project management through a paired full-screen terminal on a TLS LAN Core."""

from __future__ import annotations

import json
import os
import time

from tests.v3 import test_terminal_core as terminal_core

lan_core = terminal_core.lan_core


def settle():
    """Let the shell finish refreshing navigation before the next command."""
    time.sleep(1.5)


def project_named(admin, name):
    return next(
        (row for row in admin.get("engagements").json() if row["name"] == name), None
    )


def test_paired_tui_creates_renames_configures_archives_restores_and_deletes(
    lan_core,
):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"](
        "shell", "--project", "Terminal Project", tty=True, columns=120, rows=36
    )
    terminal.expect_screen("New conversation")
    terminal.send("New conversation")
    terminal.expect_screen("Message or /command", "/help for commands")

    terminal.send("/projects")
    terminal.expect_screen("Search:", "Create project", "Archived projects")
    terminal.send("Create project")
    terminal.expect_screen("Project name")
    terminal.send("TUI Created Project")
    terminal.expect_screen("Client name")
    terminal.send("Synthetic client")
    terminal.expect_screen("Search:", "No folder")
    terminal.send("No folder")
    terminal.expect_screen("Search:", "Docker mode")
    terminal.send("Docker mode")
    terminal.expect_screen("Search:", "Butler")
    terminal.send("New conversation")
    terminal.expect_screen("TUI Created Project", "Ready for a message")
    settle()
    created = project_named(admin, "TUI Created Project")
    assert created["client_name"] == "Synthetic client"
    project_id = created["id"]

    terminal.send("/project rename")
    terminal.expect_screen("Project name")
    os.write(terminal.input_fd, b"\x05\x15")  # End, then clear the default name.
    terminal.send("TUI Renamed Project")
    terminal.expect_screen("Renamed the project to TUI Renamed Project")
    assert (
        admin.get(f"engagements/{project_id}").json()["name"] == "TUI Renamed Project"
    )
    settle()

    # This fixture Core has no command runtime: the policy action explains that
    # and the rest of /project keeps working (the scratch supervisor Core covers
    # editing the policy).
    terminal.send("/project policy")
    terminal.expect_screen("automation runtime is not configured")
    settle()

    scope = admin.get(f"engagements/{project_id}/scope").json()
    terminal.send("/project scope")
    terminal.expect_screen("Search:", "Web search:")
    terminal.send("Web search:")
    # The menu reopens on Core's saved scope, showing the flipped value.
    terminal.expect_screen(
        "Search:", f"Web search: {'off' if scope['web_search'] else 'on'}", "Done"
    )
    terminal.send("Done")
    updated = admin.get(f"engagements/{project_id}/scope").json()
    assert updated["web_search"] is (not scope["web_search"])
    assert updated["allowed_domains"] == scope["allowed_domains"]
    settle()

    terminal.send("/project archive")
    terminal.expect_screen("Archive TUI Renamed Project?")
    terminal.send("yes")
    terminal.expect_screen("Search:", "Archived projects")
    assert admin.get(f"engagements/{project_id}").json()["status"] == "archived"
    display = terminal.expect_screen("Search:", "Create project", "Terminal Project")
    project_rows = display.split("Search:", 1)[1]
    assert "TUI Renamed Project" not in project_rows
    terminal.send("Archived projects")
    terminal.expect_screen("| Archived projects |", "TUI Renamed Project")
    terminal.send("TUI Renamed Project")
    terminal.expect_screen("Search:", "Restore project")
    terminal.send("Restore project")
    terminal.expect_screen("Search:", "Butler")
    terminal.send("New conversation")
    terminal.expect_screen("Ready for a message")
    assert admin.get(f"engagements/{project_id}").json()["status"] == "active"
    settle()

    terminal.send("/project archive")
    terminal.expect_screen("Archive TUI Renamed Project?")
    terminal.send("yes")
    terminal.expect_screen("Search:", "Archived projects")
    terminal.send("Archived projects")
    terminal.expect_screen("| Archived projects |", "TUI Renamed Project")
    terminal.send("TUI Renamed Project")
    terminal.expect_screen("Search:", "Delete project permanently")
    terminal.send("Delete project permanently")
    terminal.expect_screen("Permanently delete TUI Renamed Project?")
    terminal.send("yes")
    terminal.expect_screen("Search:", "Create project")
    assert admin.get(f"engagements/{project_id}").status_code == 404
    terminal.send("Terminal Project")
    terminal.expect_screen("Search:", "Butler")
    terminal.send("New conversation")
    terminal.expect_screen("Ready for a message")
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "synthetic project",
                "checks": [
                    "create with client name and Docker mode",
                    "rename with revision readback",
                    "policy unavailability explained; network scope saved",
                    "archive hides the project; restore returns it",
                    "delete an archived project permanently",
                ],
                "result": "passed",
            }
        )
    )
