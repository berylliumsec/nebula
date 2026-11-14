"""Work board actions: the GUI's Work page writes, each read back from Core."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nebula.terminal.board import BoardScreen, ProjectBoard, run_action
from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, ClientError, Settings
from nebula.terminal.tui import TuiUI
from nebula.terminal.ui import CancelledInput, TerminalUI

SESSION = {"id": "chat-a", "title": "Parser chat", "engagement_id": "project-a"}


class WorkCore:
    """Applies Work writes the way Core does, including its two quirks."""

    def __init__(self):
        self.project = {"id": "project-a", "revision": 3, "work_enabled": False}
        self.items = {
            "work-a": self.item("work-a", "Review parser", "in_progress"),
            "work-b": self.item(
                "work-b", "Owned elsewhere", "ready", assignee_session_id="chat-z"
            ),
        }
        self.writes = []

    @staticmethod
    def item(identifier, title, status, **changes):
        return {
            "id": identifier,
            "engagement_id": "project-a",
            "title": title,
            "description": "",
            "status": status,
            "priority": "normal",
            "revision": 1,
            **changes,
        }

    def __call__(self, request):
        path = request.url.path.removeprefix("/api/v1/engagements/project-a")
        body = json.loads(request.content) if request.content else None
        if request.method != "GET":
            self.writes.append((request.method, path, body))
        if path == "":
            return httpx.Response(200, json=self.project)
        if path == "/work" and request.method == "GET":
            return httpx.Response(200, json=list(self.items.values()))
        if path == "/work" and request.method == "POST":
            identifier = (
                "tracked-" + body["source_id"]
                if body.get("request_id")
                else f"new-{len(self.items)}"
            )
            # Core's create is idempotent per request_id: a re-track returns
            # the existing item even when it is already done.
            existing = self.items.get(identifier)
            if existing is None:
                existing = self.item(
                    identifier,
                    body["title"],
                    body["status"],
                    description=body.get("description", ""),
                    assignee_session_id=body.get("assignee_session_id"),
                )
                self.items[identifier] = existing
            return httpx.Response(200, json=existing)
        if path == "/work/setting":
            assert body["expected_revision"] == self.project["revision"]
            self.project.update(
                work_enabled=body["enabled"], revision=self.project["revision"] + 1
            )
            return httpx.Response(200, json=self.project)
        identifier = path.split("/")[2]
        current = self.items[identifier]
        if path.endswith("/updates"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            # Core forces status=blocked whenever a blocker is given.
            current["status"] = "blocked" if body.get("blocker") else body["status"]
            return httpx.Response(
                200,
                json={
                    "id": "update-1",
                    "engagement_id": "project-a",
                    "item_id": identifier,
                    "summary": body["summary"],
                },
            )
        if request.method == "PATCH":
            assert body["expected_revision"] == current["revision"]
            current.update(
                assignee_session_id=body["assignee_session_id"],
                revision=current["revision"] + 1,
            )
        return httpx.Response(200, json=current)


class Prompter:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.labels = []

    async def ask(self, label, *, default="", choices=None):
        self.labels.append((label, default, choices))
        answer = self.answers.pop(0)
        if answer is CancelledInput:
            raise CancelledInput()
        return default if answer is None else answer


def _api(tmp_path, core):
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
    return settings, AsyncApi(settings, transport=httpx.MockTransport(core))


def run(coro):
    return asyncio.run(coro)


def test_new_item_is_ready_and_read_back(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        service = ProjectBoard(api, "project-a")
        message, selected = await run_action(
            "new",
            service,
            Prompter("Draft report", "Outline first"),
            item=None,
            session=None,
        )
        assert core.writes == [
            (
                "POST",
                "/work",
                {
                    "title": "Draft report",
                    "description": "Outline first",
                    "status": "ready",
                },
            )
        ]
        assert message == "Created Draft report in Ready."
        assert selected in core.items
        await api.close()

    run(go())


def test_check_in_reports_cores_resulting_status(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        service = ProjectBoard(api, "project-a")
        item = core.items["work-a"]
        prompter = Prompter("Parsed the trace", "Write tests", "Waiting on logs", None)
        message, _ = await run_action(
            "update", service, prompter, item=dict(item), session=SESSION
        )
        assert core.writes[-1] == (
            "POST",
            "/work/work-a/updates",
            {
                "summary": "Parsed the trace",
                "next_step": "Write tests",
                "blocker": "Waiting on logs",
                "status": "in_progress",
            },
        )
        # The status prompt defaults to the item's own status, with every choice.
        assert prompter.labels[-1][1:] == (
            "in_progress",
            ["backlog", "ready", "in_progress", "blocked", "review", "done"],
        )
        assert message == (
            "Check-in saved. Core status: Blocked (you chose In Progress)."
        )
        message, _ = await run_action(
            "update",
            service,
            Prompter("Done now", "", "", "done"),
            item=dict(core.items["work-a"]),
            session=SESSION,
        )
        assert message == "Check-in saved. Core status: Done."
        with pytest.raises(ClientError, match="Choose a status"):
            await run_action(
                "update",
                service,
                Prompter("x", "", "", "finished"),
                item=dict(core.items["work-a"]),
                session=SESSION,
            )
        await api.close()

    run(go())


def test_link_sends_the_items_revision_and_refuses_another_chats_item(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        service = ProjectBoard(api, "project-a")
        message, _ = await run_action(
            "link", service, Prompter(), item=core.items["work-a"], session=SESSION
        )
        assert core.writes == [
            (
                "PATCH",
                "/work/work-a",
                {"assignee_session_id": "chat-a", "expected_revision": 1},
            )
        ]
        assert message == "Linked Review parser to this conversation."
        with pytest.raises(ClientError, match="linked to another conversation"):
            await run_action(
                "link", service, Prompter(), item=core.items["work-b"], session=SESSION
            )
        assert len(core.writes) == 1
        with pytest.raises(ClientError, match="saved conversation"):
            await run_action(
                "link", service, Prompter(), item=core.items["work-a"], session=None
            )
        await api.close()

    run(go())


def test_track_creates_and_links_then_shows_cores_existing_done_item(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        service = ProjectBoard(api, "project-a")
        prompter = Prompter(None)
        message, selected = await run_action(
            "track", service, prompter, item=None, session=SESSION
        )
        assert prompter.labels[0][1] == "Parser chat"
        assert core.writes[-1] == (
            "POST",
            "/work",
            {
                "title": "Parser chat",
                "status": "in_progress",
                "source_kind": "chat",
                "source_id": "chat-a",
                "assignee_session_id": "chat-a",
                "request_id": "track:chat-a",
            },
        )
        assert message == "Tracking this conversation as Parser chat (In Progress)."
        core.items[selected]["status"] = "done"
        message, again = await run_action(
            "track", service, Prompter("A new effort"), item=None, session=SESSION
        )
        # Shown as Core returned it; the client does not mask this behaviour.
        assert again == selected
        assert "existing tracked item Parser chat (Done)" in message
        await api.close()

    run(go())


def test_agent_tools_toggle_uses_the_project_revision(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        service = ProjectBoard(api, "project-a")
        message, _ = await run_action(
            "tools", service, Prompter(), item=None, session=None
        )
        assert core.writes == [
            ("PATCH", "/work/setting", {"enabled": True, "expected_revision": 3})
        ]
        assert message.startswith("Agent Work tools are on")
        message, _ = await run_action(
            "tools", service, Prompter(), item=None, session=None
        )
        assert core.project["work_enabled"] is False
        assert message.startswith("Agent Work tools are off")
        await api.close()

    run(go())


def test_board_screen_prompts_inline_and_escape_saves_nothing(tmp_path):
    core = WorkCore()

    async def go():
        _, api = _api(tmp_path, core)
        ui = TuiUI()
        screen = BoardScreen(ui, "Project A", ProjectBoard(api, "project-a"), SESSION)
        # Mounted as run() mounts it, so the inline prompt can take focus.
        ui._modal_container = screen.container
        ui._modal_visible = True
        await screen.refresh()
        assert "Agent tools off" in screen.status
        assert "N new" in screen.status

        answered = []

        async def answer(text):
            # Each field opens a new inline prompt; answer the next one.
            while (
                screen.prompt_future is None
                or screen.prompt_future.done()
                or screen.prompt_future in answered
            ):
                await asyncio.sleep(0)
            answered.append(screen.prompt_future)
            screen.prompt_input.text = text
            screen.prompt_future.set_result(text)

        screen.start_action("new")
        await answer("Inline item")
        await answer("")
        await screen.action_task
        assert screen.selected["title"] == "Inline item"
        assert screen.status.startswith("Created Inline item in Ready.")
        writes = len(core.writes)
        screen.start_action("update")
        while screen.prompt_future is None or screen.prompt_future.done():
            await asyncio.sleep(0)
        assert "Check-in summary" in screen.prompt_label
        screen.prompt_future.set_result(None)  # Escape.
        await screen.action_task
        assert len(core.writes) == writes
        assert screen.status.startswith("Cancelled; nothing was saved.")
        await api.close()

    run(go())


def test_plain_terminal_board_lists_actions_and_runs_them_by_name(tmp_path, capsys):
    core = WorkCore()
    settings, api = _api(tmp_path, core)

    class PlainUI(TerminalUI):
        def __init__(self):
            super().__init__(plain=True)
            self.answers = ["Summary from the plain terminal", "", "", "review"]

        async def ask(self, label, **kwargs):
            return self.answers.pop(0)

        async def choose(self, title, items, **kwargs):
            return next(row for row in items if row["id"] == "work-a")

    shell = Shell(settings, api=api, ui=PlainUI())
    shell.view = View({"id": "project-a", "name": "Project A"}, dict(SESSION))
    asyncio.run(shell.command("/board"))
    assert (
        "Actions: /board new | update | track | link | tools" in capsys.readouterr().out
    )
    asyncio.run(shell.command("/board update"))
    assert core.items["work-a"]["status"] == "review"
    assert "Check-in saved. Core status: Review." in capsys.readouterr().out
    with pytest.raises(ClientError, match="/board new"):
        asyncio.run(shell.command("/board remove"))
