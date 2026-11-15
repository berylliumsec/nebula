"""Composer and picker input: typed-ahead commands and multi-word picker search."""

from __future__ import annotations

import asyncio

from nebula.terminal.tui import TuiUI


def test_space_toggles_only_before_a_search_is_typed() -> None:
    async def run() -> None:
        ui = TuiUI()
        items = [
            {"id": "all", "name": "Select all 2 conversations"},
            {"id": "one", "name": "Release notes"},
        ]
        task = asyncio.create_task(ui.multiple("Select conversations", items, []))
        await asyncio.sleep(0)
        ui._menu_move(2)
        ui._menu_space()  # Empty search: Space toggles the highlighted row.
        assert ui._picker.checked == ["one"]
        ui._menu_space()
        assert ui._picker.checked == []
        ui._modal_input.buffer.insert_text("Select")
        ui._menu_space()
        ui._modal_input.buffer.insert_text("all")
        assert ui._modal_input.text == "Select all"
        assert ui._picker.checked == []
        assert [row["id"] for row in ui._matching_items()] == ["all"]
        ui._menu_select()
        assert ui._picker.checked == ["all"]
        ui._menu_commit()
        assert await task == ["all"]

    asyncio.run(run())


def test_enter_while_a_command_finishes_runs_the_entry_next() -> None:
    async def run() -> None:
        ui = TuiUI()
        ui.composer.text = "/status"
        ui._submit_main()  # No prompt is waiting: the entry is kept, not dropped.
        assert ui.composer.text == ""
        assert "Next: /status" in ui._footer_text()
        assert await ui.ask("You", main=True) == "/status"
        assert ui._typed_ahead is None

    asyncio.run(run())


def test_ctrl_c_returns_a_typed_ahead_entry_for_editing() -> None:
    ui = TuiUI()
    ui.composer.text = "Follow up on the logs"
    ui._submit_main()
    ui._interrupt()
    assert ui.composer.text == "Follow up on the logs"
    assert ui._typed_ahead is None


def test_a_prefilled_prompt_continues_after_its_default() -> None:
    async def run() -> None:
        ui = TuiUI()
        task = asyncio.create_task(ui.ask("Work item title", default="Parser chat"))
        await asyncio.sleep(0)
        assert ui._modal_input.buffer.cursor_position == len("Parser chat")
        ui._modal_input.buffer.insert_text(" follow-up")
        assert ui._modal_input.text == "Parser chat follow-up"
        ui._prompt_future.set_result(ui._modal_input.text)
        assert await task == "Parser chat follow-up"

    asyncio.run(run())
