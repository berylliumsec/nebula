"""Full-screen presentation contracts independent of Core or a terminal device."""

from __future__ import annotations

import asyncio

import httpx

from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, Settings
from nebula.terminal.tui import TuiUI
from nebula.terminal.ui import TerminalUI


def test_tui_keeps_progress_visible_and_replaces_chat_history() -> None:
    async def run() -> None:
        ui = TuiUI()
        project = {"id": "project", "name": "Project Ω"}
        ui.open_view(project, {"id": "first", "title": "First"})
        ui.submission("Please review this")
        ui.activity("Core accepted turn")
        ui.activity("Tool: Search index")
        ui.delta("Partial answer")
        ui._render()
        assert "You · sending\n  Please review this" in ui.transcript.text
        assert "Assistant\n  Partial answer" in ui.transcript.text
        assert "Tool: Search index" in ui._activity_text()
        ui.say("\nAction required: review the call")
        assert "Action required" in ui._activity_text()
        ui.say("Decision recorded: rejected")
        assert "Action required" not in ui._activity_text()
        ui.mark_submission("accepted")
        ui._render()
        assert "You\n  Please review this" in ui.transcript.text
        ui.show_history(
            [
                {"role": "user", "content": "Saved question", "metadata": {}},
                {"role": "assistant", "content": "Saved answer", "metadata": {}},
            ]
        )
        assert ui.transcript.text.index("Saved question") < ui.transcript.text.index(
            "Saved answer"
        )
        ui.open_view(project, {"id": "second", "title": "Second"})
        assert "Saved answer" not in ui.transcript.text
        assert "Second" in ui._header_text()
        ui.set_navigation(
            "https://paired-core.example",
            [project],
            [{"id": "second", "title": "Renamed on Core"}],
        )
        assert "Renamed on Core" in ui._header_text()
        ui.open_core("https://other-core.example")
        assert "Saved question" not in ui.transcript.text
        assert "Choose a project" in ui._header_text()

    asyncio.run(run())


def test_tui_renders_saved_and_streamed_markdown_with_source_view() -> None:
    ui = TuiUI()
    ui.open_view(
        {"id": "project", "name": "Project"}, {"id": "chat", "title": "Butler"}
    )
    answer = (
        "**Paused** [parent](/projects/project/workbench?view=chat&session=chat)\n\n"
        "| Goal | State |\n|---|---|\n| HEIF | Running |"
    )
    ui.show_history([{"role": "assistant", "content": answer, "metadata": {}}])
    assert "Paused parent" in ui.transcript.text
    assert "HEIF" in ui.transcript.text
    assert "Running" in ui.transcript.text
    assert "**Paused**" not in ui.transcript.text
    assert "|---|" not in ui.transcript.text
    assert "/projects/project" not in ui.transcript.text

    ui.delta("\n\n## Live update\n\n**Done**")
    ui._render()
    assert "Live update" in ui.transcript.text
    assert "**Done**" not in ui.transcript.text
    ui.finish_turn()
    ui._source_view = True
    ui._render()
    assert "**Paused**" in ui.transcript.text
    assert "/projects/project/workbench?view=chat&session=chat" in ui.transcript.text
    assert "|---|---|" in ui.transcript.text
    assert "**Done**" in ui.transcript.text


def test_tui_sidebar_keeps_distinct_suffixes_and_selected_chat_visible() -> None:
    ui = TuiUI()
    project = {"id": "project", "name": "Apple Bug Bounty"}
    ui.open_view(project, {"id": "butler", "title": "Butler"})
    ui.set_navigation(
        "https://core.example:8765",
        [
            project,
            {"id": "other-1", "name": "Studio live browser check 179104001"},
            {"id": "other-2", "name": "Studio live browser check 179104002"},
        ],
        [
            {"id": "other-1", "title": "Complete the macOS 27.2 26B5091g 1"},
            {"id": "other-2", "title": "Complete the macOS 27.2 26B5091g 2"},
            {"id": "butler", "title": "Butler"},
        ],
    )
    sidebar = ui._sidebar_text()
    assert "› Apple Bug Bounty" in sidebar
    assert "› Butler" in sidebar
    assert "179104001" in sidebar and "179104002" in sidebar
    assert "26B5091g 1" in sidebar and "26B5091g 2" in sidebar


def test_tui_generated_markdown_title_stays_readable_in_navigation() -> None:
    ui = TuiUI()
    project = {"id": "project", "name": "Apple Bug Bounty"}
    chat = {"id": "chat", "title": "**Synthetic answer.** | Goal |"}
    ui.open_view(project, chat)
    ui.set_navigation("https://core.example:8765", [project], [chat])
    assert "Synthetic answer." in ui._header_text()
    assert "**" not in ui._header_text()
    assert "› Synthetic answer." in ui._sidebar_text()
    assert "**" not in ui._sidebar_text()


def test_navigation_rail_includes_the_selected_butler(tmp_path) -> None:
    core = "https://core.example:8765"
    project = {"id": "project", "name": "Apple Bug Bounty"}
    butler = {
        "id": "butler",
        "title": "Butler",
        "metadata": {"control_assistant": True},
    }
    main = {"id": "main", "title": "Main", "metadata": {}}

    def handler(request):
        if request.url.path.endswith("/engagements"):
            return httpx.Response(200, json=[{"id": "other", "name": "Other"}])
        if request.url.path.endswith("/chat-sessions"):
            return httpx.Response(200, json=[main, butler])
        raise AssertionError(request.url)

    async def run() -> None:
        settings = Settings(
            tmp_path / "chat.json",
            {
                "cores": {
                    core: {
                        "device_id": "device",
                        "device_token": "synthetic-token",
                        "csrf_token": "synthetic-csrf",
                    }
                }
            },
            core,
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(handler))
        ui = TuiUI()
        shell = Shell(settings, ui=ui, api=api)
        shell.view = View(project, butler)
        ui.open_view(project, butler)
        await shell.refresh_navigation()
        assert [row["id"] for row in ui._projects] == ["project", "other"]
        assert "› Apple Bug Bounty" in ui._sidebar_text()
        assert [row["id"] for row in ui._chats] == ["butler", "main"]
        assert "› Butler" in ui._sidebar_text()
        await api.close()

    asyncio.run(run())


def test_tui_searches_and_selects_a_menu_without_opaque_ids() -> None:
    async def run() -> None:
        ui = TuiUI()
        choice = asyncio.create_task(
            ui.choose(
                "Project",
                [{"id": "id-a", "name": "Alpha"}, {"id": "id-b", "name": "Beta"}],
            )
        )
        await asyncio.sleep(0)
        assert ui._modal_input is not None
        ui._modal_input.text = "Beta"
        assert [item["id"] for item in ui._matching_items()] == ["id-b"]
        ui._menu_select()
        assert (await choice)["id"] == "id-b"

    asyncio.run(run())


def test_plain_fallback_prints_progress_transitions_once(capsys) -> None:
    ui = TerminalUI(plain=True)
    ui.submission("hello")
    ui.activity("Tool: Search index")
    ui.activity("Tool: Search index")
    output = capsys.readouterr().out
    assert "Sending to Core" in output
    assert output.count("Working: Tool: Search index") == 1
