"""Project Work board for the paired terminal client, with the GUI's Work actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, TextArea

from .transport import ClientError, key, terminal_text
from .ui import CancelledInput

if TYPE_CHECKING:
    from .shell import Shell
    from .transport import AsyncApi
    from .tui import TuiUI


STATUSES = ("backlog", "ready", "in_progress", "blocked", "review", "done")
PAGE_SIZE = 500
ACTIONS = {
    "new": "New Work item",
    "update": "Check in on a Work item",
    "track": "Track this conversation as a new Work item",
    "link": "Link a Work item to this conversation",
    "tools": "Enable or disable agent Work tools",
}


def _label(value: Any) -> str:
    return " ".join(terminal_text(value).split())


def _status(value: str) -> str:
    return value.replace("_", " ").title()


class ProjectBoard:
    def __init__(self, api: AsyncApi, project_id: str) -> None:
        self.api = api
        self.project_id = project_id
        self.path = f"engagements/{key(project_id)}/work"

    def _item(self, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("engagement_id") != self.project_id
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("title"), str)
            or value.get("status") not in STATUSES
        ):
            raise ClientError("Core returned an invalid project Work item.")
        return value

    async def items(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            page = await self.api.request(
                "GET", self.path, params={"offset": len(rows), "limit": PAGE_SIZE}
            )
            if not isinstance(page, list):
                raise ClientError("Core returned an invalid project board.")
            rows.extend(self._item(row) for row in page)
            if len({row["id"] for row in rows}) != len(rows):
                raise ClientError("Board changed while paging. Press R to reload.")
            if len(page) < PAGE_SIZE:
                return rows

    async def work_enabled(self) -> bool:
        project = await self.api.request("GET", f"engagements/{key(self.project_id)}")
        return bool((project or {}).get("work_enabled"))

    async def set_agent_tools(self, enabled: bool) -> bool:
        """Turn agent Work tools on or off, as the GUI's Work page does."""
        project = await self.api.request("GET", f"engagements/{key(self.project_id)}")
        await self.api.request(
            "PATCH",
            f"{self.path}/setting",
            json={"enabled": enabled, "expected_revision": project["revision"]},
        )
        observed = await self.work_enabled()
        if observed != enabled:
            raise ClientError("Core did not confirm the agent tools setting.")
        return observed

    async def read(self, item_id: str) -> dict[str, Any]:
        return self._item(await self.api.request("GET", f"{self.path}/{key(item_id)}"))

    async def create(self, title: str, description: str) -> dict[str, Any]:
        """The GUI's New item: a ready task with Core's saved readback."""
        created = self._item(
            await self.api.request(
                "POST",
                self.path,
                json={"title": title, "description": description, "status": "ready"},
            )
        )
        return await self.read(created["id"])

    async def check_in(
        self,
        item_id: str,
        *,
        summary: str,
        next_step: str | None,
        blocker: str | None,
        status: str,
    ) -> dict[str, Any]:
        """Save a check-in; Core decides the resulting status, read back here."""
        await self.api.request(
            "POST",
            f"{self.path}/{key(item_id)}/updates",
            json={
                "summary": summary,
                "next_step": next_step,
                "blocker": blocker,
                "status": status,
            },
        )
        return await self.read(item_id)

    async def link(self, item_id: str, session_id: str) -> dict[str, Any]:
        """Assign an item to a conversation, refusing one tracked elsewhere."""
        current = await self.read(item_id)
        assigned = current.get("assignee_session_id")
        if assigned and assigned != session_id:
            raise ClientError(
                "This Work item is linked to another conversation. Nothing changed."
            )
        if assigned != session_id:
            await self.api.request(
                "PATCH",
                f"{self.path}/{key(item_id)}",
                json={
                    "assignee_session_id": session_id,
                    "expected_revision": current["revision"],
                },
            )
        observed = await self.read(item_id)
        if observed.get("assignee_session_id") != session_id:
            raise ClientError("Core did not confirm the link. Press R to reload.")
        return observed

    async def track(self, session_id: str, title: str) -> dict[str, Any]:
        """The GUI's Create and link for a conversation; Core's item is shown."""
        created = self._item(
            await self.api.request(
                "POST",
                self.path,
                json={
                    "title": title,
                    "status": "in_progress",
                    "source_kind": "chat",
                    "source_id": session_id,
                    "assignee_session_id": session_id,
                    "request_id": "track:" + session_id,
                },
            )
        )
        return await self.read(created["id"])

    async def detail(self, item_id: str) -> str:
        path = f"{self.path}/{key(item_id)}"
        item = self._item(await self.api.request("GET", path))
        updates = await self.api.request("GET", f"{path}/updates")
        if not isinstance(updates, list) or any(
            not isinstance(row, dict)
            or row.get("engagement_id") != self.project_id
            or row.get("item_id") != item_id
            or not isinstance(row.get("summary"), str)
            for row in updates
        ):
            raise ClientError("Core returned invalid Work check-ins.")
        lines = [
            terminal_text(item["title"]),
            f"{_status(item['status'])} | {_label(item.get('priority', 'normal'))} priority",
        ]
        if item.get("assignee_session_id"):
            lines.append(
                "Assigned conversation: " + _label(item["assignee_session_id"])
            )
        lines.extend(["", terminal_text(item.get("description") or "No description.")])
        lines.extend(["", "RECENT CHECK-INS (up to 200)"])
        if not updates:
            lines.append("No check-ins yet.")
        for update in updates:
            lines.extend(
                [
                    "",
                    f"{_label(update.get('created_at', ''))} | "
                    f"{_label(update.get('actor_kind', ''))} {_label(update.get('actor_id', ''))}",
                    terminal_text(update["summary"]),
                ]
            )
            if update.get("next_step"):
                lines.append("Next: " + terminal_text(update["next_step"]))
            if update.get("blocker"):
                lines.append("Blocked: " + terminal_text(update["blocker"]))
        return "\n".join(lines)


async def run_action(
    action: str,
    service: ProjectBoard,
    prompter: Any,
    *,
    item: dict[str, Any] | None,
    session: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """One Work action with Core readback; returns a message and item to select."""
    if action == "new":
        title = (await prompter.ask("New Work item title")).strip()
        if not title:
            raise ClientError("A Work item needs a title. Nothing was created.")
        description = (await prompter.ask("Description (optional)")).strip()
        created = await service.create(title, description)
        return (
            f"Created {_label(created['title'])} in {_status(created['status'])}.",
            created["id"],
        )
    if action == "tools":
        enabled = await service.set_agent_tools(not await service.work_enabled())
        return (
            "Agent Work tools are on: agents can add and update Work."
            if enabled
            else "Agent Work tools are off; saved Work stays visible.",
            None,
        )
    if action in {"track", "link"} and session is None:
        raise ClientError(
            "Open a saved conversation first; new chats have no Work link."
        )
    if action == "track":
        assert session is not None
        title = (
            await prompter.ask(
                "Work item title for this conversation",
                default=_label(session.get("title") or "Conversation"),
            )
        ).strip()
        if not title:
            raise ClientError("A Work item needs a title. Nothing was created.")
        tracked = await service.track(session["id"], title)
        if tracked["status"] == "done":
            return (
                f"Core returned this conversation's existing tracked item "
                f"{_label(tracked['title'])} (Done); it did not create a new one.",
                tracked["id"],
            )
        return (
            f"Tracking this conversation as {_label(tracked['title'])} "
            f"({_status(tracked['status'])}).",
            tracked["id"],
        )
    if item is None:
        raise ClientError("Select a Work item first.")
    if action == "link":
        assert session is not None
        linked = await service.link(item["id"], session["id"])
        return f"Linked {_label(linked['title'])} to this conversation.", linked["id"]
    if action == "update":
        summary = (await prompter.ask("Check-in summary")).strip()
        if not summary:
            raise ClientError("A check-in needs a summary. Nothing was saved.")
        next_step = (await prompter.ask("Next step (optional)")).strip() or None
        blocker = (await prompter.ask("Blocker (optional)")).strip() or None
        status = (
            await prompter.ask(
                "Status after this check-in",
                default=item["status"],
                choices=list(STATUSES),
            )
        ).strip()
        if status not in STATUSES:
            raise ClientError(
                "Choose a status: " + ", ".join(STATUSES) + ". Nothing was saved."
            )
        observed = await service.check_in(
            item["id"],
            summary=summary,
            next_step=next_step,
            blocker=blocker,
            status=status,
        )
        result = f"Check-in saved. Core status: {_status(observed['status'])}"
        if observed["status"] != status:
            result += f" (you chose {_status(status)})"
        return result + ".", observed["id"]
    raise ClientError("Choose a listed board action.")


class BoardScreen:
    """Keep transient browsing separate from the conversation and its draft."""

    def __init__(
        self,
        ui: TuiUI,
        project_name: str,
        service: ProjectBoard,
        session: dict[str, Any] | None = None,
    ) -> None:
        self.ui = ui
        self.service = service
        self.session = session
        self.agent_tools: bool | None = None
        self.prompting = False
        self.prompt_label = ""
        self.prompt_future: asyncio.Future[str | None] | None = None
        self.action_task: asyncio.Task[None] | None = None
        self.rows: list[dict[str, Any]] = []
        self.visible: list[dict[str, Any]] = []
        self.index = 0
        self.busy = False
        self.loaded = False
        self.status = "Loading project board..."
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.refresh_task: asyncio.Task[None] | None = None
        self.detail_task: asyncio.Task[None] | None = None
        common, browse = KeyBindings(), KeyBindings()

        @common.add("escape")
        def close(_event: Any) -> None:
            if not self.future.done():
                self.future.set_result("")

        @common.add("c-r")
        @browse.add("r")
        def refresh(_event: Any) -> None:
            self.start_refresh()

        @common.add("tab")
        def focus_next(_event: Any) -> None:
            layout = self.ui.app.layout
            layout.focus(
                self.details
                if layout.has_focus(self.list_window)
                else self.search
                if layout.has_focus(self.details)
                else self.list_window
            )

        @browse.add("/")
        def search(_event: Any) -> None:
            self.ui.app.layout.focus(self.search)

        for letter, action in (
            ("n", "new"),
            ("u", "update"),
            ("t", "track"),
            ("l", "link"),
            ("a", "tools"),
        ):
            browse.add(letter)(lambda _event, name=action: self.start_action(name))

        list_keys = KeyBindings()
        for name, delta in (("up", -1), ("down", 1), ("pageup", -5), ("pagedown", 5)):
            list_keys.add(name)(
                lambda _event, step=delta: self.select(self.index + step)
            )

        @list_keys.add("enter")
        def inspect(_event: Any) -> None:
            self.ui.app.layout.focus(self.details)

        search_keys = KeyBindings()

        @search_keys.add("enter")
        def search_done(_event: Any) -> None:
            self.ui.app.layout.focus(self.list_window)

        self.search = TextArea(height=1, multiline=False, prompt="Search: ")
        self.search.control.key_bindings = merge_key_bindings([common, search_keys])
        self.search.buffer.on_text_changed += lambda _buffer: self.filter()
        self.list_window = Window(
            FormattedTextControl(
                self._fragments,
                focusable=True,
                key_bindings=merge_key_bindings([common, browse, list_keys]),
                get_cursor_position=self._cursor,
            ),
            height=Dimension(min=3, preferred=8, max=10),
            wrap_lines=False,
            style="class:sidebar",
        )
        self.details = TextArea(
            read_only=True,
            wrap_lines=True,
            scrollbar=True,
            height=Dimension(min=3, weight=2),
        )
        self.details.control.key_bindings = merge_key_bindings([common, browse])
        prompt_keys = KeyBindings()

        @prompt_keys.add("enter")
        def prompt_done(_event: Any) -> None:
            if self.prompt_future is not None and not self.prompt_future.done():
                self.prompt_future.set_result(self.prompt_input.text)

        @prompt_keys.add("escape")
        @prompt_keys.add("c-g")
        def prompt_cancel(_event: Any) -> None:
            if self.prompt_future is not None and not self.prompt_future.done():
                self.prompt_future.set_result(None)

        self.prompt_input = TextArea(height=1, multiline=False, prompt="> ")
        self.prompt_input.control.key_bindings = prompt_keys
        prompt_row = ConditionalContainer(
            HSplit(
                [
                    Window(
                        FormattedTextControl(lambda: self.prompt_label),
                        height=1,
                        style="class:heading",
                    ),
                    self.prompt_input,
                ]
            ),
            filter=Condition(lambda: self.prompting),
        )
        self.container = Frame(
            HSplit(
                [
                    self.search,
                    self.list_window,
                    self.details,
                    prompt_row,
                    Window(
                        FormattedTextControl(lambda: self.status),
                        height=1,
                        style="class:activity",
                    ),
                ]
            ),
            title="Project Board | " + _label(project_name),
        )

    @property
    def selected(self) -> dict[str, Any] | None:
        return self.visible[self.index] if self.visible else None

    def _fragments(self) -> StyleAndTextTuples:
        if not self.loaded:
            return [
                (
                    "",
                    "Loading project board..."
                    if self.busy
                    else "Board unavailable. Press R to retry.",
                )
            ]
        if not self.rows:
            return [("", "No Work items yet. Press N to create one.")]
        if not self.visible:
            return [("", "No matching Work items. Clear Search to show all.")]
        fragments: StyleAndTextTuples = []
        for status in STATUSES:
            group = [row for row in self.visible if row["status"] == status]
            fragments.append(("class:heading", f"{_status(status)} ({len(group)})\n"))
            for row in group:
                selected = row is self.selected
                fragments.append(
                    (
                        "class:rail-selected" if selected else "",
                        (" > " if selected else "   ")
                        + _label(row["title"])
                        + " | "
                        + _label(row.get("priority", "normal"))
                        + "\n",
                    )
                )
        return fragments

    def _cursor(self) -> Point:
        row = self.selected
        heading_count = STATUSES.index(row["status"]) + 1 if row else 0
        return Point(x=0, y=self.index + heading_count)

    def filter(self) -> None:
        previous = self.selected["id"] if self.selected else None
        query = self.search.text.casefold().strip()
        self.visible = [
            row
            for status in STATUSES
            for row in self.rows
            if row["status"] == status
            and query in (row["title"] + " " + row.get("description", "")).casefold()
        ]
        index = next(
            (i for i, row in enumerate(self.visible) if row["id"] == previous), 0
        )
        self.select(index)

    def select(self, index: int) -> None:
        self.index = min(max(0, index), max(0, len(self.visible) - 1))
        if self.detail_task is not None:
            self.detail_task.cancel()
        row = self.selected
        self.details.text = "Loading Work item..." if row else ""
        if row:
            self.detail_task = asyncio.create_task(self._detail(row["id"]))
        self.ui._invalidate()

    async def _detail(self, item_id: str) -> None:
        try:
            content = await self.service.detail(item_id)
        except (ClientError, httpx.HTTPError) as exc:
            content = f"Could not load Work item: {terminal_text(exc)}\nPress R to refresh; chat is unchanged."
        if self.selected and self.selected["id"] == item_id:
            self.details.text = content
            self.ui._invalidate()

    def start_refresh(self) -> None:
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self.refresh())

    async def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.status = "Loading project board..."
        self.ui._invalidate()
        try:
            rows = await self.service.items()
            self.rows = rows
            self.loaded = True
            self.filter()
            try:
                self.agent_tools = await self.service.work_enabled()
            except (ClientError, httpx.HTTPError):
                self.agent_tools = None
            self.status = self._summary()
        except (ClientError, httpx.HTTPError) as exc:
            self.status = f"R retries | Board read failed: {_label(exc)} | Previous view may be stale."
        finally:
            self.busy = False
            self.ui._invalidate()

    def _summary(self) -> str:
        tools = {True: "on", False: "off", None: "unknown"}[self.agent_tools]
        return f"{len(self.rows)} Work items | Agent tools {tools} | N new  U update  T track  L link  A tools"

    async def ask(
        self, label: str, *, default: str = "", choices: list[str] | None = None
    ) -> str:
        """Prompt inside the board; Escape cancels without a Core write."""
        from prompt_toolkit.completion import WordCompleter

        self.prompt_label = " " + _label(label) + " · Enter saves · Esc cancels"
        self.prompt_input.text = default
        self.prompt_input.buffer.cursor_position = len(default)
        self.prompt_input.buffer.completer = WordCompleter(choices) if choices else None
        self.prompt_future = asyncio.get_running_loop().create_future()
        self.prompting = True
        try:
            self.ui.app.layout.focus(self.prompt_input)
            self.ui._invalidate()
            value = await self.prompt_future
        finally:
            self.prompting = False
            self.prompt_future = None
            self.ui.app.layout.focus(self.list_window)
            self.ui._invalidate()
        if value is None:
            raise CancelledInput()
        return value

    def start_action(self, action: str) -> None:
        if self.busy or (self.action_task is not None and not self.action_task.done()):
            return
        self.action_task = asyncio.create_task(self.action(action))

    async def action(self, action: str) -> None:
        try:
            message, select = await run_action(
                action, self.service, self, item=self.selected, session=self.session
            )
        except CancelledInput:
            self.status = "Cancelled; nothing was saved. " + self._summary()
            self.ui._invalidate()
            return
        except (ClientError, httpx.HTTPError) as exc:
            self.status = "Not saved: " + _label(exc)
            self.ui._invalidate()
            return
        await self.refresh()
        if select is not None:
            self.search.text = ""
            index = next(
                (i for i, row in enumerate(self.visible) if row["id"] == select), None
            )
            if index is not None:
                self.select(index)
        self.status = _label(message) + " | " + self._summary()
        self.ui._invalidate()

    async def run(self) -> None:
        self.ui.clear_loading()
        self.ui._mode = "board"
        self.ui._prompt_future = self.future
        self.ui._modal_container = self.container
        self.ui._modal_visible = True
        self.ui.app.layout.focus(self.list_window)
        self.ui._invalidate()
        self.start_refresh()
        try:
            await self.future
        finally:
            if self.prompt_future is not None and not self.prompt_future.done():
                self.prompt_future.set_result(None)
            tasks = [
                task
                for task in (self.refresh_task, self.detail_task, self.action_task)
                if task
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.ui._finish_modal()


class _ShellPrompter:
    """The shell's own prompts, for /board actions outside the panel."""

    def __init__(self, shell: Shell) -> None:
        self.shell = shell

    async def ask(
        self, label: str, *, default: str = "", choices: list[str] | None = None
    ) -> str:
        return await self.shell.ui.ask(label, default=default, choices=choices)


async def show_project_board(shell: Shell, args: list[str] | None = None) -> None:
    from .tui import TuiUI

    view = shell.current()
    service = ProjectBoard(shell.api, view.project_id)
    action = (args or [None])[0]
    if action is not None:
        if action not in ACTIONS:
            raise ClientError("Use /board, or /board " + " | ".join(ACTIONS) + ".")
        item = None
        if action in {"update", "link"}:
            rows = await service.items()
            if not rows:
                raise ClientError("No Work items yet. Use /board new.")
            item = await shell.ui.choose(
                "Work item",
                [
                    {
                        **row,
                        "name": f"{_label(row['title'])} | {_status(row['status'])}",
                    }
                    for row in rows
                ],
            )
        message, _select = await run_action(
            action, service, _ShellPrompter(shell), item=item, session=view.session
        )
        shell.ui.say(message)
        return
    if isinstance(shell.ui, TuiUI):
        await BoardScreen(
            shell.ui,
            str(view.project.get("name") or "Project"),
            service,
            view.session,
        ).run()
        return
    rows = await service.items()
    lines = ["PROJECT BOARD | " + _label(view.project.get("name") or "Project")]
    for status in STATUSES:
        group = [row for row in rows if row["status"] == status]
        lines.extend(["", f"{_status(status)} ({len(group)})"])
        lines.extend(
            f"  {_label(row['title'])} | {_label(row.get('priority', 'normal'))} priority"
            for row in group
        )
    if not rows:
        lines.append("No Work items yet.")
    lines.extend(
        ["", "Actions: /board new | update | track | link | tools (agent Work tools)"]
    )
    shell.ui.say("\n".join(lines))
