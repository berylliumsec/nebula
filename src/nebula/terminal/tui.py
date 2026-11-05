"""Full-screen, network-only Assistant presentation built on prompt_toolkit.

The shell owns Core state and commands. This module owns only visible terminal
state, transient input, and menus; it never stores transcripts or credentials.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from typing import Any

import httpx
from prompt_toolkit.application import Application
from prompt_toolkit.completion import CompleteEvent, FuzzyWordCompleter, PathCompleter
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import Markdown

from .transport import ClientError, terminal_text
from .ui import CancelledInput, InterruptedPrompt, SlashCommandCompleter, TerminalUI

SIDEBAR_WIDTH = 32
SIDEBAR_MIN_COLUMNS = 110


def _edge(text: str, width: int, *, tail: bool = False) -> str:
    """Keep a terminal label within its cell budget, including wide glyphs."""
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    budget = width - 1
    if not tail:
        result = ""
        for char in text:
            if get_cwidth(result + char) > budget:
                break
            result += char
        return result + "…"
    result = ""
    for char in reversed(text):
        if get_cwidth(char + result) > budget:
            break
        result = char + result
    return "…" + result


def _middle(text: str, width: int) -> str:
    """Keep distinctive suffixes visible in long project and chat names."""
    if get_cwidth(text) <= width:
        return text
    suffix_width = min(11, max(3, width // 2))
    prefix = _edge(text, width - suffix_width)
    suffix = _edge(text, suffix_width + 1, tail=True)
    return prefix[:-1] + "…" + suffix[1:]


def _plain_title(value: Any) -> str:
    """Keep generated Markdown titles readable in one-line navigation."""
    first_line = terminal_text(value or "Conversation").splitlines()[0]
    clean = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", first_line)
    clean = re.sub(r"^#{1,6}\s+", "", clean)
    clean = clean.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(clean.strip(" *|#").split()) or "Conversation"


@lru_cache(maxsize=96)
def _readable_markdown(source: str, width: int) -> str:
    """Render Markdown to selectable text at a comfortable reading width."""
    output = StringIO()
    Console(file=output, width=width, force_terminal=False, no_color=True).print(
        Markdown(source)
    )
    lines: list[str] = []
    for line in output.getvalue().splitlines():
        clean = line.rstrip()
        if clean or (lines and lines[-1]):
            lines.append(clean)
    return "\n".join(lines).strip("\n")


@dataclass
class Picker:
    title: str
    items: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]]
    more: Callable[[], Awaitable[list[dict[str, Any]]]] | None
    index: int = 0
    loading: bool = False


class TuiUI(TerminalUI):
    """Persistent chat, navigation, activity and composer for an interactive TTY."""

    full_screen = True

    def __init__(self) -> None:
        super().__init__(plain=True)
        self._status = "Connecting"
        self._context = "Choose a Core"
        self._core = ""
        self._project = ""
        self._project_label = ""
        self._chat = ""
        self._projects: list[dict[str, Any]] = []
        self._chats: list[dict[str, Any]] = []
        self._entries: list[tuple[str, str]] = []
        self._activity: list[str] = []
        self._notice = ""
        self._streaming = False
        self._source_view = False
        self._render_width = 0
        self._mode = "main"
        self._picker: Picker | None = None
        self._main_future: asyncio.Future[str] | None = None
        self._prompt_future: asyncio.Future[str] | None = None
        self._modal_input: TextArea | None = None
        self._modal_container: Any = Window(height=0)
        self._modal_visible = False
        self._preserved_draft = ""
        self._render_scheduled = False
        self._app_task: asyncio.Task[None] | None = None

        composer_keys = KeyBindings()

        @composer_keys.add("enter")
        def submit_main(_event: Any) -> None:
            self._submit_main()

        @composer_keys.add("escape", "enter")
        def newline(_event: Any) -> None:
            self.composer.buffer.insert_text("\n")

        self.composer = TextArea(
            multiline=True,
            wrap_lines=True,
            height=Dimension(min=2, preferred=2, max=4),
            prompt="> ",
            completer=SlashCommandCompleter(lambda: self.command_names),
            style="class:composer",
        )
        self.composer.control.key_bindings = composer_keys
        self.transcript = TextArea(
            read_only=True,
            focusable=True,
            wrap_lines=True,
            scrollbar=True,
            style="class:transcript",
        )
        self._sidebar = Window(
            FormattedTextControl(self._sidebar_text),
            width=SIDEBAR_WIDTH,
            wrap_lines=False,
            style="class:sidebar",
        )
        self._activity_window = Window(
            FormattedTextControl(self._activity_text),
            height=1,
            wrap_lines=False,
            style="class:activity",
        )
        keys = KeyBindings()

        @keys.add("c-c")
        def interrupt(_event: Any) -> None:
            self._interrupt()

        @keys.add("c-d")
        def exit_or_close(_event: Any) -> None:
            if self._mode == "main":
                self._complete_main(EOFError())
            else:
                self._cancel_modal()

        @keys.add("c-g")
        def close_modal(_event: Any) -> None:
            if self._mode != "main":
                self._cancel_modal()

        @keys.add("tab")
        def toggle_focus(_event: Any) -> None:
            if self._mode == "main":
                buffer = self.composer.buffer
                state = buffer.complete_state
                if state is not None and state.completions:
                    completion = state.current_completion or state.completions[0]
                    buffer.apply_completion(completion)
                    return
                # Completion refreshes asynchronously while typing. A quick
                # slash, prefix, Tab sequence must not move focus instead.
                matches = (
                    list(
                        buffer.completer.get_completions(
                            buffer.document, CompleteEvent(completion_requested=True)
                        )
                    )
                    if buffer.completer is not None
                    else []
                )
                if matches:
                    buffer.apply_completion(matches[0])
                    return
                self.app.layout.focus(
                    self.composer
                    if self.app.layout.current_control == self.transcript.control
                    else self.transcript
                )

        for key, command in (
            ("f1", "help"),
            ("f2", "projects"),
            ("f3", "chats"),
            ("f4", "commands"),
            ("f5", "activity"),
            ("f6", "follow"),
            ("f7", "input"),
            ("f8", "approvals"),
            ("f9", "history"),
        ):
            keys.add(key)(lambda _event, name=command: self._shortcut(name))

        @keys.add("f10")
        def toggle_source(_event: Any) -> None:
            if self._mode == "main":
                self._source_view = not self._source_view
                self._render()

        modal_visible = Condition(lambda: self._modal_visible)
        wide = Condition(
            lambda: (
                self.app.output.get_size().columns >= SIDEBAR_MIN_COLUMNS
                and self.app.output.get_size().rows >= 18
            )
        )
        root = FloatContainer(
            content=HSplit(
                [
                    Window(
                        FormattedTextControl(self._header_text),
                        height=1,
                        style="class:header",
                    ),
                    VSplit(
                        [
                            ConditionalContainer(self._sidebar, filter=wide),
                            HSplit(
                                [
                                    self.transcript,
                                    self._activity_window,
                                ]
                            ),
                        ]
                    ),
                    Frame(self.composer, title="Message or /command"),
                    Window(
                        FormattedTextControl(self._footer_text),
                        height=1,
                        style="class:footer",
                    ),
                ]
            ),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=CompletionsMenu(
                        max_height=8,
                        scroll_offset=1,
                        extra_filter=has_focus(self.composer.buffer),
                    ),
                ),
                Float(
                    content=ConditionalContainer(
                        DynamicContainer(lambda: self._modal_container),
                        filter=modal_visible,
                    ),
                    left=2,
                    right=2,
                    top=2,
                    bottom=2,
                ),
            ],
        )
        self.app: Application[None] = Application(
            layout=Layout(root, focused_element=self.composer),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict(
                {}
                if "NO_COLOR" in os.environ
                else {
                    "header": "bg:#263445 #ffffff bold",
                    "sidebar": "bg:#18222e #d1dce8",
                    "transcript": "bg:#111821 #e3e8ee",
                    "activity": "bg:#202c37 #b6d3db",
                    "composer": "bg:#18222e #ffffff",
                    "footer": "bg:#263445 #c5d2df",
                    "selected": "reverse bold",
                    "completion-menu": "bg:#263445 #ffffff",
                    "completion-menu.completion.current": "bg:#42617e #ffffff bold",
                }
            ),
        )
        self.app.after_render += self._check_render_width

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = terminal_text(value)
        self._invalidate()

    async def start(self) -> None:
        self._app_task = asyncio.create_task(self.app.run_async())
        self._app_task.add_done_callback(self._screen_closed)
        await asyncio.sleep(0)

    async def close(self) -> None:
        if self.app.is_running:
            self.app.exit()
        if self._app_task is not None:
            await self._app_task

    def _screen_closed(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            error: BaseException = EOFError()
        else:
            error = task.exception() or EOFError()
        for future in (self._main_future, self._prompt_future):
            if future is not None and not future.done():
                future.set_exception(error)
        if self._picker is not None and not self._picker.future.done():
            self._picker.future.set_exception(error)

    def _invalidate(self) -> None:
        if hasattr(self, "app") and self.app.is_running:
            self.app.invalidate()

    def _schedule_render(self) -> None:
        if self._render_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._render()
            return
        self._render_scheduled = True
        loop.call_later(0.08, self._render)

    def _reading_width(self) -> int:
        columns = self.app.output.get_size().columns
        sidebar = SIDEBAR_WIDTH if columns >= SIDEBAR_MIN_COLUMNS else 0
        return max(12, min(100, columns - sidebar - 4))

    def _check_render_width(self, _app: Application[None]) -> None:
        if self._render_width != self._reading_width():
            self._schedule_render()

    def _render(self) -> None:
        self._render_scheduled = False
        width = self._reading_width()
        self._render_width = width
        parts = []
        for kind, source in self._entries:
            if not kind:
                parts.append(source.strip("\n"))
                continue
            body = source if self._source_view else _readable_markdown(source, width)
            parts.append(
                kind + "\n" + "\n".join("  " + line for line in body.splitlines())
            )
        value = "\n\n".join(parts)[-120_000:]
        self.transcript.text = value
        self.transcript.buffer.cursor_position = len(value)
        self._invalidate()

    def _prune_entries(self) -> None:
        total = 0
        for index in range(len(self._entries) - 1, -1, -1):
            label, content = self._entries[index]
            total += len(label) + len(content)
            if total > 120_000:
                if index == len(self._entries) - 1:
                    self._entries = [(label, content[-120_000:])]
                else:
                    self._entries = self._entries[index + 1 :]
                break

    def _header_text(self) -> str:
        width = self.app.output.get_size().columns
        progress = self._status.rsplit(" · ", 1)[-1]
        status = "[" + _edge(progress, min(26, max(8, width // 3))) + "]"
        left = " Nebula  " + _middle(
            self._context, max(0, width - get_cwidth(status) - 11)
        )
        left = _edge(left, max(0, width - get_cwidth(status) - 1))
        return (
            left + " " * max(1, width - get_cwidth(left) - get_cwidth(status)) + status
        )

    def _rail_rows(
        self, rows: list[dict[str, Any]], selected: str, slots: int, key: str
    ) -> list[str]:
        active = next((row for row in rows if row.get("id") == selected), None)
        ordered = ([active] if active is not None else []) + [
            row for row in rows if row is not active
        ]
        visible = ordered[: slots - 1] if len(ordered) > slots else ordered[:slots]
        lines = [
            (" › " if row.get("id") == selected else "   ")
            + _middle(
                _plain_title(row.get(key) or row.get("name") or row.get("id"))
                if key == "title"
                else terminal_text(row.get(key) or row.get("name") or row.get("id")),
                SIDEBAR_WIDTH - 4,
            )
            for row in visible
        ]
        if len(ordered) > slots:
            lines.append(f"   +{len(ordered) - len(visible)} more")
        return lines

    def _sidebar_text(self) -> str:
        rows = self.app.output.get_size().rows
        project_slots = min(6, max(2, (rows - 12) // 3))
        chat_slots = max(1, min(9, rows - 12 - project_slots))
        lines = [
            " CORE",
            "  " + _middle(self._core or "Connecting", SIDEBAR_WIDTH - 3),
            "",
            " PROJECTS · F2",
        ]
        if self._projects:
            lines.extend(
                self._rail_rows(self._projects, self._project, project_slots, "name")
            )
        else:
            lines.append("   Browse projects")
        lines.extend(["", " CONVERSATIONS · F3"])
        if self._chats:
            lines.extend(self._rail_rows(self._chats, self._chat, chat_slots, "title"))
        else:
            lines.append("   Browse conversations")
        return "\n".join(lines)

    def _activity_text(self) -> str:
        width = self.app.output.get_size().columns
        if self._notice:
            detail = "ACTION  " + self._notice
        elif self._streaming:
            detail = "WORKING  " + (
                self._activity[-1] if self._activity else "Waiting for Core"
            )
        else:
            detail = self._activity[-1] if self._activity else "Ready"
        return _edge(" " + detail, width - 1)

    def _footer_text(self) -> str:
        width = self.app.output.get_size().columns if hasattr(self, "app") else 80
        state = self.composer.buffer.complete_state
        if self._mode == "main" and state is not None and state.completions:
            return _edge(" ↑↓ Select  Tab Insert  Enter Run", width - 1)
        source = "Render" if self._source_view else "Source"
        if width < 60:
            hint = f" F1 Help  F2 Projects  F10 {source}"
        elif width < 110:
            hint = f" F1 Help  F2 Projects  F3 Chats  F8 Approvals  F10 {source}"
        else:
            hint = (
                " F1 Help  F2 Projects  F3 Chats  F4 Commands  F5 Activity"
                f"  F8 Approvals  F10 {source}  Tab Read  ^C Stop  ^D Quit"
            )
        return _edge(hint, width - 1)

    def open_view(
        self, project: dict[str, Any], session: dict[str, Any] | None
    ) -> None:
        self.set_identity(project, session)
        self._entries.clear()
        self._activity.clear()
        self._notice = ""
        self._streaming = False
        self._render()

    def open_core(self, core: str) -> None:
        self._core = core
        self._project = ""
        self._project_label = ""
        self._chat = ""
        self._projects = []
        self._chats = []
        self._context = "Choose a project"
        self._entries.clear()
        self._activity.clear()
        self._notice = ""
        self._streaming = False
        self._render()

    def set_identity(
        self, project: dict[str, Any], session: dict[str, Any] | None
    ) -> None:
        self._project = project["id"]
        self._project_label = terminal_text(project.get("name") or "Project")
        self._chat = session["id"] if session else ""
        self._context = (
            self._project_label
            + " / "
            + _plain_title((session or {}).get("title") or "New conversation")
        )
        self._invalidate()

    def set_navigation(
        self,
        core: str,
        projects: list[dict[str, Any]],
        chats: list[dict[str, Any]],
    ) -> None:
        self._core = core
        self._projects = projects
        self._chats = chats
        selected = next((row for row in chats if row.get("id") == self._chat), None)
        if selected is not None:
            self._context = (
                self._project_label
                + " / "
                + _plain_title(selected.get("title") or "Conversation")
            )
        self._invalidate()

    def activity(self, value: str) -> None:
        clean = terminal_text(value).strip()
        if clean and (not self._activity or self._activity[-1] != clean):
            self._activity.append(clean[:180])
            self._activity = self._activity[-30:]
            self._invalidate()

    def clear_notice(self) -> None:
        self._notice = ""
        self._invalidate()

    def say(self, value: Any = "") -> None:
        clean = terminal_text(value)
        notice = clean.lstrip()
        if notice.startswith(("Action required", "Connection lost", "Cannot ")):
            self._notice = notice.splitlines()[0]
        elif notice.startswith(
            (
                "Decision recorded",
                "Question status: answered",
                "Response complete",
                "Turn cancelled",
            )
        ):
            self.clear_notice()
        if clean:
            self._entries.append(("", clean))
            self._prune_entries()
            self._schedule_render()

    def delta(self, value: str) -> None:
        clean = terminal_text(value)
        if not clean:
            return
        self._streaming = True
        if self._entries and self._entries[-1][0] == "Assistant":
            kind, previous = self._entries[-1]
            self._entries[-1] = kind, (previous + clean)[-100_000:]
        else:
            self._entries.append(("Assistant", clean))
        self._prune_entries()
        self._schedule_render()

    def markdown(self, value: str) -> None:
        clean = terminal_text(value)
        if clean:
            self._entries.append(("Assistant", clean))
            self._prune_entries()
            self._schedule_render()

    def submission(self, text: str, *, queued: bool = False) -> None:
        self._streaming = not queued
        self._entries.append(
            ("You · queued" if queued else "You · sending", terminal_text(text))
        )
        self._prune_entries()
        self._notice = ""
        self._schedule_render()

    def finish_turn(self) -> None:
        self._streaming = False
        self.activity("Ready")
        self._invalidate()

    def mark_submission(self, state: str) -> None:
        for index in range(len(self._entries) - 1, -1, -1):
            label, content = self._entries[index]
            if label == "You · sending":
                self._entries[index] = (
                    "You" if state == "accepted" else f"You · {state}",
                    content,
                )
                self._schedule_render()
                return

    def show_history(self, rows: list[dict[str, Any]], *, older: bool = False) -> None:
        from .state import visible_message

        saved: list[tuple[str, str]] = []
        for row in rows:
            if not visible_message(row):
                continue
            content = terminal_text(row.get("content") or "")
            for block in row.get("content_blocks") or []:
                if block.get("type") == "image":
                    content += (
                        "\n[Image: "
                        + terminal_text(
                            block.get("alt") or block.get("artifact_id") or "attachment"
                        )
                        + "]"
                    )
            saved.append(("You" if row["role"] == "user" else "Assistant", content))
        if older:
            self._entries = saved + self._entries
        else:
            live = next(
                (entry for entry in reversed(self._entries) if entry[0] == "Assistant"),
                None,
            )
            self._entries = saved
            if (
                self._streaming
                and live is not None
                and not any(
                    row[0] == "Assistant" and row[1] == live[1] for row in saved
                )
            ):
                self._entries.append(live)
        self._prune_entries()
        self._render()
        if older:
            self.transcript.buffer.cursor_position = 0

    def take_preserved_draft(self) -> str:
        value, self._preserved_draft = self._preserved_draft, ""
        return value

    def _complete_main(self, error: BaseException) -> None:
        if self._main_future is not None and not self._main_future.done():
            self._main_future.set_exception(error)

    def _submit_main(self) -> None:
        if self._main_future is None or self._main_future.done():
            return
        value = self.composer.text
        if value == "/":
            state = self.composer.buffer.complete_state
            if state is not None and state.completions:
                self.composer.buffer.complete_next()
                return
        if not value.strip():
            return
        self.composer.text = ""
        self._main_future.set_result(value)

    def set_commands(self, names: list[str]) -> None:
        previous = self.command_names
        super().set_commands(names)
        if self.command_names != previous and self.app.is_running:
            buffer = self.composer.buffer
            if buffer.text.startswith("/") and not any(
                char.isspace() for char in buffer.text
            ):
                buffer.start_completion(select_first=False)
            self._invalidate()

    def _shortcut(self, name: str) -> None:
        if (
            self._mode != "main"
            or self._main_future is None
            or self._main_future.done()
        ):
            return
        self._preserved_draft = self.composer.text
        self.composer.text = ""
        self._main_future.set_result("/" + name)

    def _interrupt(self) -> None:
        if self._mode == "main":
            self._complete_main(InterruptedPrompt())
        else:
            self._cancel_modal()

    def _cancel_modal(self) -> None:
        if self._picker is not None and not self._picker.future.done():
            self._picker.future.set_exception(CancelledInput())
        if self._prompt_future is not None and not self._prompt_future.done():
            self._prompt_future.set_exception(CancelledInput())

    def _finish_modal(self) -> None:
        self._mode = "main"
        self._picker = None
        self._prompt_future = None
        self._modal_input = None
        self._modal_container = Window(height=0)
        self._modal_visible = False
        self.app.layout.focus(self.composer)
        self._invalidate()

    async def ask(
        self,
        label: str,
        *,
        default: str = "",
        secret: bool = False,
        choices: list[str] | None = None,
        main: bool = False,
        path: bool = False,
    ) -> str:
        if main:
            if default and not self.composer.text:
                self.composer.text = default
            self._mode = "main"
            self.app.layout.focus(self.composer)
            self._main_future = asyncio.get_running_loop().create_future()
            self._invalidate()
            try:
                return await self._main_future
            finally:
                self._main_future = None
        self._mode = "prompt"
        self._prompt_future = asyncio.get_running_loop().create_future()
        prompt_keys = KeyBindings()

        @prompt_keys.add("enter")
        def submit(_event: Any) -> None:
            assert self._modal_input is not None
            if self._prompt_future is not None and not self._prompt_future.done():
                self._prompt_future.set_result(self._modal_input.text)

        self._modal_input = TextArea(
            text=default,
            multiline=False,
            password=secret,
            history=DummyHistory(),
            completer=(
                PathCompleter(expanduser=True)
                if path
                else FuzzyWordCompleter(choices)
                if choices
                else None
            ),
        )
        self._modal_input.control.key_bindings = prompt_keys
        hint = (
            "Sensitive entry is hidden and never saved locally."
            if secret
            else "Enter confirms · Ctrl-G cancels"
        )
        self._modal_container = Frame(
            HSplit(
                [
                    self._modal_input,
                    Window(FormattedTextControl(hint), height=1),
                ]
            ),
            title=terminal_text(label),
        )
        self._modal_visible = True
        self.app.layout.focus(self._modal_input)
        self._invalidate()
        try:
            value = await self._prompt_future
            if value.strip() == "/cancel":
                raise CancelledInput()
            return value
        finally:
            self._finish_modal()

    def _matching_items(self) -> list[dict[str, Any]]:
        if self._picker is None or self._modal_input is None:
            return []
        query = self._modal_input.text.casefold().strip()
        rows = [
            row
            for row in self._picker.items
            if query in str(row.get("label") or row.get("name") or "").casefold()
        ]
        if self._picker.more and not query:
            rows.append({"id": "__load_more", "name": "Load more from Core…"})
        return rows

    def _menu_text(self) -> str:
        if self._picker is None:
            return ""
        rows = self._matching_items()
        if not rows:
            return " No matches. Edit search or press Ctrl-G."
        page_size = max(4, min(20, self.app.output.get_size().rows - 11))
        start = self._picker.index // page_size * page_size
        visible = rows[start : start + page_size]
        return "\n".join(
            (" › " if start + offset == self._picker.index else "   ")
            + terminal_text(row.get("label") or row.get("name") or row["id"])[:150]
            for offset, row in enumerate(visible)
        ) + ("\n   Loading…" if self._picker.loading else "")

    def _menu_move(self, change: int) -> None:
        if self._picker is None:
            return
        rows = self._matching_items()
        if rows:
            self._picker.index = max(0, min(len(rows) - 1, self._picker.index + change))
            self._invalidate()

    async def _load_more(self, picker: Picker) -> None:
        if picker.more is None:
            return
        picker.loading = True
        self._invalidate()
        try:
            rows = await picker.more()
            if self._picker is picker:
                picker.items.extend(rows)
                if not rows:
                    picker.more = None
                picker.index = max(0, len(picker.items) - len(rows))
        except (ClientError, OSError, httpx.HTTPError) as exc:
            self.say(str(exc))
        finally:
            picker.loading = False
            self._invalidate()

    def _menu_select(self) -> None:
        picker = self._picker
        if picker is None or picker.loading:
            return
        rows = self._matching_items()
        if not rows:
            return
        row = rows[min(picker.index, len(rows) - 1)]
        if row["id"] == "__load_more":
            asyncio.create_task(self._load_more(picker))
        elif not picker.future.done():
            picker.future.set_result(row)

    async def choose(
        self,
        title: str,
        items: list[dict[str, Any]],
        *,
        selected: str | None = None,
        more: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> dict[str, Any]:
        if selected:
            matches = [
                row
                for row in items
                if row["id"] == selected
                or str(row["name"]).casefold() == selected.casefold()
            ]
            if len(matches) == 1:
                return matches[0]
            raise ClientError(
                f"No unique matching {title}. Choose from the listed names."
            )
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._mode = "menu"
        picker = Picker(title, list(items), future, more)
        self._picker = picker
        menu_keys = KeyBindings()

        @menu_keys.add("enter")
        def select(_event: Any) -> None:
            self._menu_select()

        @menu_keys.add("up")
        def up(_event: Any) -> None:
            self._menu_move(-1)

        @menu_keys.add("down")
        def down(_event: Any) -> None:
            self._menu_move(1)

        @menu_keys.add("pageup")
        def page_up(_event: Any) -> None:
            self._menu_move(-10)

        @menu_keys.add("pagedown")
        def page_down(_event: Any) -> None:
            self._menu_move(10)

        self._modal_input = TextArea(
            multiline=False,
            prompt="Search: ",
        )
        self._modal_input.control.key_bindings = menu_keys
        self._modal_input.buffer.on_text_changed += lambda _buffer: self._menu_changed()
        self._modal_container = Frame(
            HSplit(
                [
                    self._modal_input,
                    Window(FormattedTextControl(self._menu_text), wrap_lines=True),
                    Window(
                        FormattedTextControl("↑↓ Select · Enter Open · Ctrl-G Cancel"),
                        height=1,
                    ),
                ]
            ),
            title=terminal_text(title),
        )
        self._modal_visible = True
        self.app.layout.focus(self._modal_input)
        self._invalidate()
        try:
            return await future
        finally:
            self._finish_modal()

    def _menu_changed(self) -> None:
        if self._picker is not None:
            self._picker.index = 0
        self._invalidate()

    def help(self) -> None:
        self.say(
            "Keys: F2 projects · F3 chats · F4 commands · F5 activity · F6 follow · F8 approvals · Tab transcript · Ctrl-C stop · Ctrl-D quit"
        )
        super().help()
