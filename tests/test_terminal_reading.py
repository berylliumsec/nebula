"""Catch-up, quoting, supporting evidence and turn details in the terminal client."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from nebula.terminal import reading
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from tests.test_terminal_shell import CONFIG, CORE, ENTRY, PROJECT, SESSION, UI


class ReadingUI(UI):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.revealed = []

    def reveal(self, text):
        self.revealed.append(text)
        return True


def path(request):
    return request.url.path.removeprefix("/api/v1/")


def shell_for(tmp_path, handler, ui=None):
    settings = Settings(tmp_path / "chat.json", {"cores": {CORE: dict(ENTRY)}}, CORE)
    api = AsyncApi(settings, transport=httpx.MockTransport(handler))
    shell = Shell(settings, ui=ui or ReadingUI(), api=api)
    shell.view = View(
        copy.deepcopy(PROJECT), copy.deepcopy(SESSION), copy.deepcopy(CONFIG)
    )
    return shell


THROUGH = "2026-11-14T12:00:00Z"


def catchup_core(record, writes):
    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/catch-up":
            assert request.url.params["device_id"] == "device-a"
            return httpx.Response(200, json=record)
        if route == "chat/sessions/chat-a/read-cursor":
            writes.append(json.loads(request.content))
            return httpx.Response(200, json={"revision": 1})
        if route == "chat/sessions/chat-a/turns/turn-f/summary":
            return httpx.Response(
                200,
                json={
                    "id": "turn-f",
                    "status": "failed",
                    "error": "provider timed out",
                    "model": "model-a",
                    "created_at": THROUGH,
                    "updated_at": THROUGH,
                    "message_id": None,
                },
            )
        pytest.fail(route)

    return handler


def test_first_visit_starts_the_read_cursor_silently(tmp_path):
    writes = []
    record = {"initialized": False, "revision": 0, "through_at": THROUGH, "items": []}

    async def run():
        shell = shell_for(tmp_path, catchup_core(record, writes))
        await reading.catchup_on_open(shell)
        assert writes == [
            {"device_id": "device-a", "expected_revision": 0, "through_at": THROUGH}
        ]
        assert shell.ui.output == []
        await shell.api.close()

    asyncio.run(run())


def test_catch_up_summarizes_and_marks_read_only_when_asked(tmp_path):
    writes = []
    record = {
        "initialized": True,
        "revision": 3,
        "through_at": THROUGH,
        "items": [
            {
                "id": "m1",
                "message_id": "m1",
                "kind": "completed",
                "text": "Scan finished.",
            },
            {
                "id": "turn-f",
                "turn_id": "turn-f",
                "kind": "failure",
                "text": "Response failed",
            },
        ],
        "pending": [{"id": "p1", "kind": "pending", "text": "Approve a command"}],
        "truncated": False,
    }

    async def run():
        shell = shell_for(
            tmp_path,
            catchup_core(record, writes),
            ui=ReadingUI(choices=["item:0", "item:1"]),
        )
        await reading.catchup_on_open(shell)
        assert shell.ui.output[0] == (
            "While you were away: 2 updates, 1 needing your action."
        )
        assert "  New answer · Scan finished." in shell.ui.output
        assert writes == []
        await reading.catchup(shell, [])
        assert shell.ui.revealed == ["Scan finished."]
        await reading.catchup(shell, [])
        assert any("Response failed · model-a" in line for line in shell.ui.output)
        assert "Error: provider timed out" in shell.ui.output
        await reading.catchup(shell, ["read"])
        assert [write["expected_revision"] for write in writes] == [3, 3, 3]
        await shell.api.close()

    asyncio.run(run())


def test_quote_uses_the_answer_without_progress_prose():
    progress = "Checked 2 files 👀."
    message = {
        "role": "assistant",
        "content": progress + "\n\nFinal answer\nsecond line",
        "metadata": {
            "progress_prefix_utf16_length": len(progress.encode("utf-16-le")) // 2
        },
    }
    assert reading.answer_text(message) == "Final answer\nsecond line"
    fenced = dict(message, content="```\ncode" + "\n\nrest")
    fenced["metadata"] = {"progress_prefix_utf16_length": len("```\ncode")}
    assert reading.answer_text(fenced) == fenced["content"]


def test_quote_appends_quoted_lines_to_the_draft(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"id": "m2", "role": "assistant", "content": "Use port 8443\nnot 80"}
            ],
        )

    async def run():
        shell = shell_for(tmp_path, handler, ui=ReadingUI(choices=["m2"]))
        shell.current().draft.text = "Context first"
        await reading.quote(shell, [])
        assert shell.current().draft.text == (
            "Context first\n\n> Use port 8443\n> not 80\n\n"
        )
        await shell.api.close()

    asyncio.run(run())


def test_evidence_lists_sources_observations_and_limits(tmp_path):
    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/messages":
            return httpx.Response(
                200, json=[{"id": "m3", "role": "assistant", "content": "Found it."}]
            )
        assert route == "chat/sessions/chat-a/messages/m3/evidence"
        return httpx.Response(
            200,
            json={
                "retrieved": [{"name": "notes.md", "excerpt": "port 8443", "page": 2}],
                "observations": [
                    {"kind": "observed", "label": "files.read", "summary": "read notes"}
                ],
                "artifacts": [{"label": "scan.txt", "kind": "tool_output"}],
                "limitations": ["Only the latest 20 sources are listed."],
            },
        )

    async def run():
        shell = shell_for(tmp_path, handler, ui=ReadingUI(choices=["m3"]))
        await reading.evidence(shell, [])
        output = "\n".join(shell.ui.output)
        assert "Source · notes.md, page 2: port 8443" in output
        assert "Observed · files.read: read notes" in output
        assert "Artifact · scan.txt (tool_output)" in output
        assert "Limitation · Only the latest 20 sources are listed." in output
        await shell.api.close()

    asyncio.run(run())


def test_turn_details_need_a_recorded_turn_and_reveal_the_source(tmp_path):
    rows = [
        {"id": "u1", "role": "user", "content": "Hi", "metadata": {}},
        {
            "id": "a1",
            "role": "assistant",
            "content": "Hello there",
            "metadata": {"chat_turn_id": "turn-a"},
        },
    ]

    def handler(request):
        route = path(request)
        if route == "chat/sessions/chat-a/messages":
            return httpx.Response(200, json=rows)
        assert route == "chat/sessions/chat-a/turns/turn-a/summary"
        return httpx.Response(
            200,
            json={
                "id": "turn-a",
                "status": "complete",
                "model": "model-a",
                "created_at": THROUGH,
                "updated_at": THROUGH,
                "error": None,
                "message_id": "a1",
            },
        )

    async def run():
        shell = shell_for(tmp_path, handler, ui=ReadingUI(choices=["u1", "a1"]))
        with pytest.raises(ClientError, match="no response details"):
            await reading.turn(shell, [])
        await reading.turn(shell, [])
        assert any(
            line.startswith("Response complete · model-a") for line in shell.ui.output
        )
        assert shell.ui.revealed == ["Hello there"]
        await shell.api.close()

    asyncio.run(run())


def test_a_quoted_draft_opens_with_the_cursor_below_the_quote():
    from nebula.terminal.tui import TuiUI

    async def run():
        ui = TuiUI()
        draft = "> Use port 8443\n\n"
        task = asyncio.create_task(ui.ask("You", default=draft, main=True))
        await asyncio.sleep(0)
        assert ui.composer.buffer.cursor_position == len(draft)
        ui.composer.buffer.insert_text("Agreed.")
        assert ui.composer.text == draft + "Agreed."
        ui._main_future.set_result(ui.composer.text)
        assert await task == draft + "Agreed."

    asyncio.run(run())


def test_answers_watched_live_are_read_unless_a_summary_waits(tmp_path):
    writes = []
    record = {
        "initialized": True,
        "revision": 4,
        "through_at": THROUGH,
        "items": [
            {"id": "m9", "message_id": "m9", "kind": "completed", "text": "Done."}
        ],
        "pending": [],
    }

    async def run():
        shell = shell_for(tmp_path, catchup_core(record, writes))
        view = shell.current()
        # Without a catch-up read on open, nothing is marked.
        await reading.mark_seen(shell, view)
        assert writes == []
        # A summary shown on open waits for /catchup; live answers leave it.
        await reading.catchup_on_open(shell)
        assert view.catchup == "waiting"
        await reading.mark_seen(shell, view)
        assert writes == []
        # Once reviewed, an answer watched live advances the read cursor.
        await reading.catchup(shell, ["read"])
        assert view.catchup == "current"
        await reading.mark_seen(shell, view)
        assert [write["expected_revision"] for write in writes] == [4, 4]
        await shell.api.close()

    asyncio.run(run())
