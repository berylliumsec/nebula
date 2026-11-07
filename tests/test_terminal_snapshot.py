"""Project Snapshot reads the selected Core project only when opened or refreshed."""

from __future__ import annotations

import asyncio

import httpx

from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, Settings
from nebula.terminal.tui import TuiUI


class SnapshotUI:
    def __init__(self) -> None:
        self.status = ""
        self.content = ""
        self.refresh = None
        self.loading = ""

    def begin_loading(self, value: str) -> None:
        self.loading = value

    def clear_loading(self) -> None:
        self.loading = ""

    async def show_snapshot(self, content, *, refresh) -> None:
        self.content = content
        self.refresh = refresh


def _shell(tmp_path, handler, *, session=True):
    settings = Settings(
        tmp_path / "chat.json",
        {
            "cores": {
                "https://core.example": {
                    "device_token": "synthetic",
                    "csrf_token": "synthetic-csrf",
                    "device_id": "synthetic-device",
                }
            }
        },
        "https://core.example",
    )
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    ui = SnapshotUI()
    shell = Shell(settings, ui=ui, api=api)
    shell.view = View(
        {"id": "project-a", "name": "Project A"},
        {"id": "chat-a", "title": "Conversation A"} if session else None,
    )
    return shell, ui


def test_snapshot_reads_project_totals_and_linked_work_only_on_demand(tmp_path):
    paths = []
    assets = [{"id": "asset-a"}]

    def handler(request):
        path = request.url.path.removeprefix("/api/v1/")
        paths.append((path, dict(request.url.params)))
        if path == "work/updates":
            return httpx.Response(
                200, json=[{"engagement_id": "project-a", "item_id": "work-a"}]
            )
        if path == "engagements/project-a/work/work-a":
            return httpx.Response(
                200,
                json={
                    "id": "work-a",
                    "title": "Review parser",
                    "status": "in_progress",
                    "priority": "high",
                },
            )
        if path == "engagements/project-a/work/work-a/updates":
            return httpx.Response(
                200,
                json=[
                    {
                        "summary": "Reviewed source",
                        "next_step": "Check trace",
                        "blocker": "Waiting for build",
                    }
                ],
            )
        values = {
            "assets": assets,
            "runs": [
                {
                    "status": "running",
                    "objective": "Review memory",
                    "metadata": {
                        "completed_tasks": 2,
                        "total_tasks": 4,
                        "spent_usd": 0.005,
                    },
                }
            ],
            "findings": [
                {"status": "validated", "severity": "high"},
                {"status": "candidate", "severity": "medium"},
            ],
            "approvals": [{"status": "pending"}, {"status": "approved"}],
        }
        assert path in values, path
        assert request.url.params["engagement_id"] == "project-a"
        return httpx.Response(200, json=values[path])

    shell, ui = _shell(tmp_path, handler)
    assert "snapshot" in shell.available_commands()
    assert paths == []
    asyncio.run(shell.command("/snapshot"))
    assert ui.loading == ""
    assert shell.current().session_id == "chat-a"
    assert "PROJECT SNAPSHOT  ·  Project A" in ui.content
    assert "Review parser" in ui.content
    assert "Blocked: Waiting for build" in ui.content
    assert "Assets       1" in ui.content
    assert "Running · Review memory" in ui.content
    assert "Progress     2 of 4 tasks" in ui.content
    assert "Model cost   $0.0050" in ui.content
    assert "Findings     1 validated · 2 total · 1 priority" in ui.content
    assert "Approvals    1 waiting for review" in ui.content
    assert len(paths) == 7

    assets.append({"id": "asset-b"})
    assert "Assets       2" in asyncio.run(ui.refresh())
    assert len(paths) == 14


def test_snapshot_shows_unavailable_metrics_then_recovers_on_refresh(tmp_path):
    failed = True
    paths = []

    def handler(request):
        nonlocal failed
        path = request.url.path.removeprefix("/api/v1/")
        paths.append(path)
        if path == "engagements/project-a/work":
            return httpx.Response(200, json=[])
        if path == "findings" and failed:
            return httpx.Response(503, json={"detail": "Synthetic outage"})
        return httpx.Response(200, json=[])

    shell, ui = _shell(tmp_path, handler, session=False)
    assert "snapshot" in shell.available_commands()
    asyncio.run(shell.command("/snapshot"))
    assert "No Work check-in" in ui.content
    assert "Findings     Unavailable" in ui.content
    assert "Could not read findings. Other totals are current." in ui.content
    assert "Synthetic outage" in ui.content
    assert "work/updates" not in paths
    failed = False
    assert "Findings     0 validated · 0 total · 0 priority" in asyncio.run(
        ui.refresh()
    )


def test_snapshot_panel_preserves_chat_and_composer():
    async def run():
        ui = TuiUI()
        ui.open_view(
            {"id": "project-a", "name": "Project A"},
            {"id": "chat-a", "title": "Conversation A"},
        )
        ui.composer.text = "Unsent draft"
        ui.say("Saved chat text")
        ui._render()

        async def refresh():
            return "Refreshed"

        task = asyncio.create_task(
            ui.show_snapshot("PROJECT SNAPSHOT", refresh=refresh)
        )
        await asyncio.sleep(0)
        assert ui._mode == "snapshot"
        assert ui._modal_visible
        assert "PROJECT SNAPSHOT" not in ui.transcript.text
        assert "R Refresh" in ui._footer_text()
        ui._prompt_future.set_result("")
        await task
        assert ui._mode == "main"
        assert not ui._modal_visible
        assert ui.composer.text == "Unsent draft"
        assert "Saved chat text" in ui.transcript.text

    asyncio.run(run())
