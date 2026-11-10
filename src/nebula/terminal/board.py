"""Read-only project Work board for the paired terminal client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, TextArea

from .transport import ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell
    from .transport import AsyncApi
    from .tui import TuiUI


STATUSES = ("backlog", "ready", "in_progress", "blocked", "review", "done")
PAGE_SIZE = 500


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


class BoardScreen:
    """Keep transient browsing separate from the conversation and its draft."""

    def __init__(self, ui: TuiUI, project_name: str, service: ProjectBoard) -> None:
        self.ui = ui
        self.service = service
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
        self.container = Frame(
            HSplit(
                [
                    self.search,
                    self.list_window,
                    self.details,
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
            return [("", "No Work items yet. Create one in the web Work page.")]
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
            self.status = f"{len(rows)} Work items | Read-only | R refresh"
        except (ClientError, httpx.HTTPError) as exc:
            self.status = f"R retries | Board read failed: {_label(exc)} | Previous view may be stale."
        finally:
            self.busy = False
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
            tasks = [task for task in (self.refresh_task, self.detail_task) if task]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.ui._finish_modal()


async def show_project_board(shell: Shell) -> None:
    from .tui import TuiUI

    view = shell.current()
    service = ProjectBoard(shell.api, view.project_id)
    if isinstance(shell.ui, TuiUI):
        await BoardScreen(
            shell.ui, str(view.project.get("name") or "Project"), service
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
        lines.append("No Work items yet. Create one in the web Work page.")
    shell.ui.say("\n".join(lines))
