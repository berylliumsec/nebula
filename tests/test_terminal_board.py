"""The terminal board reads Core on demand without mutating Work or chat."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from prompt_toolkit.document import Document

from nebula.terminal.board import BoardScreen, PAGE_SIZE, ProjectBoard, STATUSES
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from nebula.terminal.tui import TuiUI
from nebula.terminal.ui import CancelledInput, SlashCommandCompleter, TerminalUI


def _item(identifier="work-a", **changes):
    return {
        "id": identifier,
        "engagement_id": "project-a",
        "title": "Review parser",
        "description": "A complete task description.",
        "status": "in_progress",
        "priority": "high",
        **changes,
    }


def _api(tmp_path, handler):
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
    return settings, AsyncApi(settings, transport=httpx.MockTransport(handler))


def test_board_reads_every_project_page_and_sanitizes_checkins(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/work"):
            offset = int(request.url.params["offset"])
            rows = (
                [_item(f"work-{i}") for i in range(PAGE_SIZE)]
                if offset == 0
                else [_item("last")]
            )
            return httpx.Response(200, json=rows)
        if path.endswith("/updates"):
            return httpx.Response(
                200,
                json=[
                    {
                        "engagement_id": "project-a",
                        "item_id": "last",
                        "summary": "\x1b[31mSaved progress",
                        "next_step": "Read trace",
                        "blocker": "Needs review",
                        "actor_kind": "operator",
                        "actor_id": "Alex",
                    }
                ],
            )
        return httpx.Response(200, json=_item("last", title="Long title " * 30))

    _, api = _api(tmp_path, handler)
    service = ProjectBoard(api, "project-a")
    rows = asyncio.run(service.items())
    assert len(rows) == PAGE_SIZE + 1
    assert requests[1].url.params["offset"] == str(PAGE_SIZE)
    detail = asyncio.run(service.detail("last"))
    assert "Next: Read trace" in detail
    assert "Blocked: Needs review" in detail
    assert "operator Alex" in detail
    assert "Long title " * 30 in detail
    assert "\x1b" not in detail


@pytest.mark.parametrize(
    "value", [{}, [_item(engagement_id="other")], [_item(status="unknown")]]
)
def test_board_rejects_invalid_or_cross_project_records(tmp_path, value):
    _, api = _api(tmp_path, lambda _: httpx.Response(200, json=value))
    with pytest.raises(ClientError, match="invalid project"):
        asyncio.run(ProjectBoard(api, "project-a").items())


def test_board_refuses_repeated_pages_instead_of_looping(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[_item(str(i)) for i in range(PAGE_SIZE)])

    _, api = _api(tmp_path, handler)
    with pytest.raises(ClientError, match="paging"):
        asyncio.run(ProjectBoard(api, "project-a").items())
    assert len(calls) == 2


def test_board_is_discoverable_without_a_chat_and_plain_mode_is_read_only(
    tmp_path, capsys
):
    settings, api = _api(tmp_path, lambda _: httpx.Response(200, json=[_item()]))
    shell = Shell(settings, api=api, ui=TerminalUI())
    assert "board" not in shell.available_commands()
    shell.view = View({"id": "project-a", "name": "Project A"}, None)
    assert "board" in shell.available_commands()
    shell.ui.set_commands(shell.available_commands())
    completions = SlashCommandCompleter(lambda: shell.ui.command_names).get_completions(
        Document("/boa"), None
    )
    assert any(row.text == "/board" for row in completions)
    asyncio.run(shell.command("/board"))
    output = capsys.readouterr().out
    assert "PROJECT BOARD | Project A" in output
    assert "In Progress (1)" in output
    assert "Review parser" in output
    assert shell.view.session is None


def test_board_search_selection_refresh_and_deleted_item(tmp_path):
    async def run():
        rows = [
            _item("a", status="backlog"),
            _item("b", title="Check trace", status="blocked"),
        ]

        def handler(request):
            if request.url.path.endswith("/work"):
                return httpx.Response(200, json=rows)
            if request.url.path.endswith("/updates"):
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=next(
                    row for row in rows if request.url.path.endswith("/" + row["id"])
                ),
            )

        _, api = _api(tmp_path, handler)
        screen = BoardScreen(TuiUI(), "Project A", ProjectBoard(api, "project-a"))
        await screen.refresh()
        screen.select(1)
        await screen.detail_task
        assert "Check trace" in screen.details.text
        await screen.refresh()
        assert screen.selected["id"] == "b"
        screen.search.text = "trace"
        assert [row["id"] for row in screen.visible] == ["b"]
        rows.pop()
        await screen.refresh()
        assert screen.selected is None
        assert "No matching" in screen._fragments()[0][1]
        screen.search.text = ""
        await screen.detail_task
        assert screen.selected["id"] == "a"
        fragments = "".join(text for _, text in screen._fragments())
        assert all(status.replace("_", " ").title() in fragments for status in STATUSES)
        assert screen._cursor().y == 1

    asyncio.run(run())


def test_board_initial_failure_retry_and_stale_refresh_are_explicit(tmp_path):
    async def run():
        failed = True

        def handler(request):
            if failed:
                return httpx.Response(503, json={"detail": "Synthetic outage"})
            if request.url.path.endswith("/work"):
                return httpx.Response(200, json=[_item()])
            if request.url.path.endswith("/updates"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=_item())

        _, api = _api(tmp_path, handler)
        screen = BoardScreen(TuiUI(), "Project", ProjectBoard(api, "project-a"))
        await screen.refresh()
        assert "Synthetic outage" in screen.status
        assert "unavailable" in screen._fragments()[0][1]
        failed = False
        await screen.refresh()
        await screen.detail_task
        assert "Review parser" in screen.details.text
        failed = True
        await screen.refresh()
        assert "stale" in screen.status
        assert screen.selected["id"] == "work-a"

    asyncio.run(run())


def test_board_detail_failure_is_visible_and_retryable(tmp_path):
    async def run():
        def handler(request):
            if request.url.path.endswith("/work"):
                return httpx.Response(200, json=[_item()])
            return httpx.Response(404, json={"detail": "Item deleted"})

        _, api = _api(tmp_path, handler)
        screen = BoardScreen(TuiUI(), "Project", ProjectBoard(api, "project-a"))
        await screen.refresh()
        await screen.detail_task
        assert "Item deleted" in screen.details.text
        assert "Press R" in screen.details.text

    asyncio.run(run())


def test_board_panel_preserves_transcript_draft_and_cancels_pending_reads(tmp_path):
    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        settings, api = _api(tmp_path, handler)
        ui = TuiUI()
        project, chat = {"id": "project-a", "name": "Project A"}, {"id": "chat-a"}
        shell = Shell(settings, ui=ui, api=api)
        shell.view = View(project, chat)
        ui.open_view(project, chat)
        ui.composer.text = "Unsent draft"
        ui.say("Saved transcript")
        ui._render()
        task = asyncio.create_task(shell.command("/board"))
        await started.wait()
        assert ui._mode == "board"
        assert "R Refresh" in ui._footer_text()
        ui._cancel_modal()
        with pytest.raises(CancelledInput):
            await task
        assert cancelled.is_set()
        assert ui._mode == "main"
        assert not ui._modal_visible
        assert ui.composer.text == "Unsent draft"
        assert "Saved transcript" in ui.transcript.text
        assert shell.current().session_id == "chat-a"

    asyncio.run(run())
