"""Full-screen presentation contracts independent of Core or a terminal device."""

from __future__ import annotations

import asyncio

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
        assert "You · sending\nPlease review this" in ui.transcript.text
        assert "Assistant\nPartial answer" in ui.transcript.text
        assert "Tool: Search index" in ui._activity_text()
        ui.say("\nAction required: review the call")
        assert "Action required" in ui._footer_text()
        ui.say("Decision recorded: rejected")
        assert "Action required" not in ui._footer_text()
        ui.mark_submission("accepted")
        ui._render()
        assert "You\nPlease review this" in ui.transcript.text
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
