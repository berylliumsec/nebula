"""Full-screen, network-only Assistant presentation built on prompt_toolkit.

The shell owns Core state and commands. This module owns only visible terminal
state, transient input, and menus; it never stores transcripts or credentials.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from typing import Any

import httpx
from prompt_toolkit.application import Application
from prompt_toolkit.completion import CompleteEvent, FuzzyWordCompleter, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
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
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import Markdown

from .transport import ClientError, terminal_text
from .ui import (
    CancelledInput,
    InterruptedPrompt,
    SlashCommandCompleter,
    TerminalUI,
    toggle_selection,
)

SIDEBAR_WIDTH = 32
SIDEBAR_MIN_COLUMNS = 110
SIDEBAR_MIN_ROWS = 24
SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
TRANSCRIPT_PREFIX_WIDTH = 3
TRANSCRIPT_SCROLLBAR_WIDTH = 1
TRANSCRIPT_WRAP_MARGIN = 1
TRANSCRIPT_READING_MAX_WIDTH = 136
REPEATED_REQUEST_MIN_LENGTH = 100
INTERRUPT_REPEAT_SECONDS = 4.0
RECALL_LIMIT = 100


def activity_label(state: str | None) -> str:
    if state is None:
        return "?"
    return {"working": "RUN", "waiting": "WAIT", "idle": "IDLE"}.get(state, "?")


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
    error: str = ""
    checked: list[str] | None = None


class _TranscriptLexer(Lexer):
    """Color role lines without adding decoration to copied transcript text."""

    def __init__(self, ui: TuiUI) -> None:
        self._ui = ui

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        styles = tuple(self._ui._transcript_line_styles)

        def lex_line(lineno: int) -> StyleAndTextTuples:
            style = styles[lineno] if lineno < len(styles) else ""
            return [(f"class:transcript-{style}", document.lines[lineno])]

        return lex_line


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
        self._chat_title = "Choose a conversation"
        self._model = ""
        self._draft_skill = ""
        self._selected_mcp_count = 0
        self._projects: list[dict[str, Any]] = []
        self._chats: list[dict[str, Any]] = []
        self._chat_activity: dict[str, str] | None = None
        self._entries: list[tuple[str, str]] = []
        self._message_history: list[str] = []
        self._local_submissions: list[str] = []
        self._recall_index: int | None = None
        self._recall_draft: Document | None = None
        self._applying_recall = False
        self._transcript_line_styles: list[str] = []
        self._activity: list[str] = []
        self._notice = ""
        self._loading = ""
        self._loading_since: float | None = None
        self._streaming = False
        self._turn_phase = ""
        self._turn_since: float | None = None
        self._spinner_index = 0
        self._animation_task: asyncio.Task[None] | None = None
        self._reduced_motion = os.environ.get(
            "NEBULA_CHAT_REDUCED_MOTION", ""
        ).lower() not in {
            "",
            "0",
            "false",
            "no",
        }
        self._source_view = False
        self._render_width = 0
        self._mode = "main"
        self._picker: Picker | None = None
        self._notes_screen: Any = None
        self._main_future: asyncio.Future[str] | None = None
        self._prompt_future: asyncio.Future[str] | None = None
        self._snapshot_refresh_task: asyncio.Task[None] | None = None
        self._modal_input: TextArea | None = None
        self._modal_container: Any = Window(height=0)
        self._modal_visible = False
        self._preserved_draft = ""
        self.on_draft_cleared: Callable[[], None] | None = None
        self._interrupt_armed = False
        self._interrupt_timer: asyncio.TimerHandle | None = None
        self._interrupt_cleared_draft = False
        self._render_scheduled = False
        self._app_task: asyncio.Task[None] | None = None

        composer_keys = KeyBindings()

        @composer_keys.add("enter")
        def submit_main(_event: Any) -> None:
            self._submit_main()

        @composer_keys.add("escape", "enter")
        def newline(_event: Any) -> None:
            self.composer.buffer.insert_text("\n")

        @composer_keys.add("up", filter=Condition(self._can_recall_up))
        def recall_previous(_event: Any) -> None:
            self._recall_previous()

        @composer_keys.add("down", filter=Condition(self._can_recall_down))
        def recall_next(_event: Any) -> None:
            self._recall_next()

        self.composer = TextArea(
            multiline=True,
            wrap_lines=True,
            height=2,
            prompt="> ",
            completer=SlashCommandCompleter(lambda: self.command_names),
            style="class:composer",
        )
        self.composer.control.key_bindings = composer_keys
        self.composer.buffer.on_text_changed += lambda _buffer: self._composer_changed()
        self.transcript = TextArea(
            read_only=True,
            focusable=True,
            wrap_lines=True,
            scrollbar=True,
            lexer=_TranscriptLexer(self),
            get_line_prefix=self._transcript_line_prefix,
            style="class:transcript",
        )
        self._sidebar = Window(
            FormattedTextControl(self._sidebar_fragments),
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
        self._conversation_heading = Window(
            FormattedTextControl(self._conversation_heading_text),
            height=2,
            wrap_lines=False,
            style="class:heading",
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
            ("f11", "snapshot"),
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
                and self.app.output.get_size().rows >= SIDEBAR_MIN_ROWS
            )
        )
        reading = HSplit(
            [
                self._conversation_heading,
                self.transcript,
                self._activity_window,
                Frame(self.composer, title="Message or /command"),
            ],
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
                            ConditionalContainer(
                                Window(width=2, char=" ", style="class:rail-divider"),
                                filter=wide,
                            ),
                            Window(width=2, char=" ", style="class:gutter"),
                            reading,
                            Window(width=2, char=" ", style="class:gutter"),
                        ]
                    ),
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
            # Let the terminal select and copy chat text with a normal drag.
            # Pickers retain mouse reporting for their own controls.
            mouse_support=Condition(
                lambda: self._mode not in {"main", "snapshot", "notes"}
            ),
            style=Style.from_dict(
                {}
                if "NO_COLOR" in os.environ
                else {
                    "header": "bg:#192631 #e7eef3 bold",
                    "sidebar": "bg:#141f2a #b5c4ce",
                    "rail-divider": "bg:#344654 #344654",
                    "gutter": "bg:#101720 #101720",
                    "rail-selected": "bg:#233747 #e7eef3 bold",
                    "rail-running": "#83d9c6",
                    "rail-waiting": "#f0c47b",
                    "rail-idle": "#8497a4",
                    "rail-unknown": "#8497a4",
                    "heading": "bg:#101720 #e7eef3 bold",
                    "heading-meta": "#8497a4",
                    "transcript": "bg:#101720 #e7eef3",
                    "transcript-user-heading": "#9fc1e7 bold",
                    "transcript-assistant-heading": "#83d9c6 bold",
                    "transcript-user-body": "#c8d8e6",
                    "transcript-assistant-body": "#e7eef3",
                    "transcript-repeat-body": "#8497a4 italic",
                    "transcript-notice-body": "#91a5b3",
                    "transcript-user-gutter": "#6485a3",
                    "transcript-assistant-gutter": "#568e86",
                    "transcript-notice-gutter": "#677d8b",
                    "activity": "bg:#1d373b #e7eef3",
                    "composer": "bg:#1c2a36 #e7eef3",
                    "footer": "bg:#192631 #b5c4ce",
                    "selected": "reverse bold",
                    "completion-menu": "bg:#192631 #e7eef3",
                    "completion-menu.completion.current": "bg:#233747 #e7eef3 bold",
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
        self._sync_animation()

    async def close(self) -> None:
        if self._animation_task is not None:
            self._animation_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._animation_task
        if self.app.is_running:
            self.app.exit()
        if self._app_task is not None:
            await self._app_task

    def _screen_closed(self, task: asyncio.Task[None]) -> None:
        if self._animation_task is not None:
            self._animation_task.cancel()
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
            self._sync_animation()
            self.app.invalidate()

    def _sync_animation(self) -> None:
        if not self.app.is_running:
            return
        active = self._display_state()[2] or bool(self._picker and self._picker.loading)
        if self._reduced_motion:
            active = False
        if active and self._animation_task is None:
            self._animation_task = asyncio.create_task(self._animate())
        elif not active and self._animation_task is not None:
            self._animation_task.cancel()
            self._animation_task = None
            self._spinner_index = 0

    async def _animate(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.24)
                if not self.app.is_running or not (
                    self._display_state()[2] or (self._picker and self._picker.loading)
                ):
                    return
                self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
                self.app.invalidate()
        except asyncio.CancelledError:
            pass
        finally:
            if self._animation_task is asyncio.current_task():
                self._animation_task = None

    def _spinner(self) -> str:
        return "●" if self._reduced_motion else SPINNER_FRAMES[self._spinner_index]

    def _display_state(self) -> tuple[str, str, bool, float | None]:
        """Keep Core turn state separate from local reads and stream transport."""
        core_state = (self._chat_activity or {}).get(self._chat) if self._chat else None
        if self._loading:
            verb = "OPENING" if self._loading.startswith("Opening ") else "LOADING"
            return verb, self._loading, True, self._loading_since
        if self._notice:
            label = (
                "OFFLINE"
                if self._notice.startswith("Connection lost")
                else "ACTION"
                if self._notice.startswith("Action required")
                else "ERROR"
            )
            return label, self._notice, False, None
        if self._turn_phase == "queued":
            return (
                "QUEUED",
                self._activity[-1] if self._activity else "Waiting in Core",
                False,
                None,
            )
        if self._turn_phase == "waiting":
            return (
                "WAITING",
                self._activity[-1] if self._activity else "Check /status",
                False,
                None,
            )
        if self._turn_phase == "reconnecting":
            return (
                "RECONNECTING",
                "Reading the active Core turn",
                True,
                self._turn_since,
            )
        if core_state == "waiting":
            return "WAITING", "Check /status for the next action", False, None
        if self._turn_phase == "sending":
            return (
                "SENDING",
                "Waiting for Core to accept the turn",
                True,
                self._turn_since,
            )
        if self._turn_phase == "working":
            return (
                "WORKING",
                self._activity[-1] if self._activity else "Turn active",
                True,
                self._turn_since,
            )
        if core_state == "idle":
            return "IDLE", "Ready for a message", False, None
        if core_state == "working":
            return "WORKING", "Turn active in Core · F6 Follow", True, None
        if not self._core:
            return "CONNECTING", "Connecting to Core", True, None
        if self._chat and core_state is None:
            return "UNKNOWN", "Turn state unavailable · /status", False, None
        return "READY", "Ready for a message", False, None

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
        size = self.app.output.get_size()
        sidebar = (
            SIDEBAR_WIDTH + 2
            if columns >= SIDEBAR_MIN_COLUMNS and size.rows >= SIDEBAR_MIN_ROWS
            else 0
        )
        # The TextArea reserves one column for its scrollbar. Prefixes occupy
        # three more columns on every visual row, including wrapped rows.
        return min(
            TRANSCRIPT_READING_MAX_WIDTH,
            max(
                12,
                columns
                - sidebar
                - 4
                - TRANSCRIPT_PREFIX_WIDTH
                - TRANSCRIPT_SCROLLBAR_WIDTH
                - TRANSCRIPT_WRAP_MARGIN,
            ),
        )

    def _transcript_line_prefix(
        self, lineno: int, wrap_count: int
    ) -> StyleAndTextTuples:
        style = (
            self._transcript_line_styles[lineno]
            if lineno < len(self._transcript_line_styles)
            else ""
        )
        if style.startswith("user-"):
            marker = "◆  " if style == "user-heading" and not wrap_count else "│  "
            return [("class:transcript-user-gutter", marker)]
        if style.startswith("assistant-"):
            marker = "✦  " if style == "assistant-heading" and not wrap_count else "│  "
            return [("class:transcript-assistant-gutter", marker)]
        if style == "notice-body":
            return [("class:transcript-notice-gutter", "·  ")]
        return [("", " " * TRANSCRIPT_PREFIX_WIDTH)]

    def _check_render_width(self, _app: Application[None]) -> None:
        if self._render_width != self._reading_width():
            self._schedule_render()

    def _render(self) -> None:
        self._render_scheduled = False
        width = self._reading_width()
        self._render_width = width
        lines: list[str] = []
        styles: list[str] = []
        seen_requests: set[str] = set()
        for kind, source in self._entries:
            if lines:
                lines.append("")
                styles.append("")
            if not kind:
                note = source.strip("\n")
                note_lines = note.split("\n") if note else [""]
                lines.extend(note_lines)
                styles.extend("notice-body" for _ in note_lines)
                continue
            role = "user" if kind.startswith("You") else "assistant"
            repeated = (
                role == "user"
                and kind == "You"
                and len(source) >= REPEATED_REQUEST_MIN_LENGTH
                and source in seen_requests
                and not self._source_view
            )
            if role == "user" and kind == "You":
                seen_requests.add(source)
            heading = kind.upper() + (" · REPEATED" if repeated else "")
            lines.append(heading)
            styles.append(f"{role}-heading")
            if repeated:
                body = "Same request as earlier · F10 Source shows the full text."
            elif self._source_view:
                body = source
            else:
                body = _readable_markdown(source, width)
            body_lines = body.split("\n") if body else [""]
            lines.extend(body_lines)
            styles.extend(
                "repeat-body" if repeated else f"{role}-body" for _ in body_lines
            )
        value = "\n".join(lines)
        if len(value) > 120_000:
            cut = len(value) - 120_000
            line_end = value.find("\n", cut)
            if line_end >= 0:
                styles = styles[value[: line_end + 1].count("\n") :]
                value = value[line_end + 1 :]
            else:
                styles = styles[-1:]
                value = value[-120_000:]
        self._transcript_line_styles = styles
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
        label, _, animated, _ = self._display_state()
        status = f"{self._spinner() if animated else '●'} {label}"
        core = "● CORE" if self._core else "◐ CORE"
        right = f"{core}  {status}" if width >= 80 else status
        context = self._project_label or "Choose a project"
        if self._chat:
            context += " / " + self._chat_title
        left = " ✦ NEBULA  " + _middle(
            context, min(56, max(8, width - get_cwidth(right) - 14))
        )
        left = _edge(left, max(0, width - get_cwidth(right) - 1))
        return left + " " * max(1, width - get_cwidth(left) - get_cwidth(right)) + right

    def _conversation_heading_text(self) -> list[tuple[str, str]]:
        width = min(72, self._reading_width() + 4)
        title = self._chat_title if self._project else "Choose a project"
        details = self._project_label or self._core or "Connecting to Core"
        if self._model and self._chat:
            details += "  ·  " + self._model
        selections = []
        if self._draft_skill:
            selections.append("Next turn: " + self._draft_skill)
        if self._selected_mcp_count:
            selections.append(f"MCP: {self._selected_mcp_count} selected")
        if selections:
            details = "  ·  ".join([*selections, details])
        return [
            ("bold", " " + _edge(title, width - 2) + "\n"),
            ("class:heading-meta", " " + _edge(details, width - 2)),
        ]

    def _rail_rows(
        self, rows: list[dict[str, Any]], selected: str, slots: int, key: str
    ) -> list[str]:
        active = next((row for row in rows if row.get("id") == selected), None)
        remaining = [row for row in rows if row is not active]
        ordered = ([active] if active is not None else []) + remaining
        visible = ordered[: slots - 1] if len(ordered) > slots else ordered[:slots]
        lines = []
        for row in visible:
            title = _middle(
                _plain_title(row.get(key) or row.get("name") or row.get("id"))
                if key == "title"
                else terminal_text(row.get(key) or row.get("name") or row.get("id")),
                SIDEBAR_WIDTH - 4,
            )
            lines.append((" › " if row.get("id") == selected else "   ") + title)
            if key == "title" and row.get("id") == selected:
                state = (self._chat_activity or {}).get(str(row.get("id")))
                state_text = {
                    "working": "● Running",
                    "waiting": "◐ Waiting for action",
                    "idle": "○ Idle",
                }.get(state, "? State unknown")
                lines.append("   " + state_text)
        if len(ordered) > slots:
            lines.append(f"   +{len(ordered) - len(visible)} more")
        return lines

    def _sidebar_text(self) -> str:
        rows = self.app.output.get_size().rows
        project_slots = min(4, max(3, (rows - 10) // 8))
        chat_slots = max(1, min(6, (rows - 9 - project_slots) // 2))
        lines = [
            " CORE",
            "  " + _middle(self._core or "Connecting", SIDEBAR_WIDTH - 4),
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

    def _sidebar_fragments(self) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        selected_status = False
        for line in self._sidebar_text().splitlines():
            style = ""
            if line.startswith(" › "):
                style = "class:rail-selected"
                selected_status = True
            elif line.startswith("   ● Running"):
                style = (
                    "class:rail-selected" if selected_status else "class:rail-running"
                )
                selected_status = False
            elif line.startswith("   ◐ Waiting"):
                style = (
                    "class:rail-selected" if selected_status else "class:rail-waiting"
                )
                selected_status = False
            elif line.startswith("   ○ Idle") or line.startswith("   ? State"):
                style = "class:rail-selected" if selected_status else "class:rail-idle"
                selected_status = False
            else:
                selected_status = False
            fragments.append((style, line + "\n"))
        return fragments

    def _activity_text(self) -> str:
        width = self._reading_width() + 4
        label, detail, animated, since = self._display_state()
        glyph = self._spinner() if animated else "●"
        prefix = f" {glyph} {label}  "
        elapsed = ""
        if since is not None and not self._reduced_motion and width >= 42:
            minutes, seconds = divmod(int(max(0, time.monotonic() - since)), 60)
            elapsed = f"  {minutes:02}:{seconds:02} "
        return _edge(prefix + detail, width - get_cwidth(elapsed)) + elapsed

    def _footer_text(self) -> str:
        width = self.app.output.get_size().columns if hasattr(self, "app") else 80
        if self._mode == "main" and self._interrupt_armed:
            detail = " · Draft cleared" if self._interrupt_cleared_draft else ""
            return _edge(" ^C again to stop turn" + detail, width - 1)
        state = self.composer.buffer.complete_state
        if self._mode == "main" and state is not None and state.completions:
            return _edge(" ↑↓ Select  Tab Insert  Enter Run", width - 1)
        if self._mode == "snapshot":
            return _edge(" Project Snapshot    R Refresh    Esc Close", width - 1)
        if self._mode == "notes":
            return _edge(
                " Project Notes    ↑↓ Select    Tab Edit    ^N New    ^S Save    ^R Refresh    D Delete    Esc Chat",
                width - 1,
            )
        if self._mode == "main" and self._recall_index is not None:
            return _edge(
                f" Recalled message {self._recall_index + 1}/{len(self._message_history)}"
                "    ↑ Older    ↓ Newer or draft    Enter Send",
                width - 1,
            )
        source = "Render" if self._source_view else "Source"
        if width < 60:
            hint = " F1 Help  F3 Chats  ↑ Recall  ^C Clear/Stop"
        elif width < 110:
            hint = f" F1 Help  F2 Projects  F3 Chats  ↑ Recall  F11 Snapshot  ^C Clear/Stop  F10 {source}"
        else:
            hint = (
                " F1 Help    F2 Projects    F3 Chats    F8 Approvals"
                f"    F10 {source}    F11 Snapshot    ↑ Recall    Tab Read    ^C Clear/Stop    ^D Quit"
            )
        return _edge(hint, width - 1)

    def open_view(
        self, project: dict[str, Any], session: dict[str, Any] | None
    ) -> None:
        self.set_identity(project, session)
        self._draft_skill = ""
        self._selected_mcp_count = 0
        self._entries.clear()
        self._message_history.clear()
        self._local_submissions.clear()
        self._reset_recall()
        self._activity.clear()
        self._notice = ""
        self._streaming = False
        self._turn_phase = ""
        self._turn_since = None
        self._render()

    def open_core(self, core: str) -> None:
        self._core = core
        self._project = ""
        self._project_label = ""
        self._chat = ""
        self._chat_title = "Choose a conversation"
        self._model = ""
        self._projects = []
        self._chats = []
        self._chat_activity = None
        self._draft_skill = ""
        self._selected_mcp_count = 0
        self._context = "Choose a project"
        self._entries.clear()
        self._message_history.clear()
        self._local_submissions.clear()
        self._reset_recall()
        self._activity.clear()
        self._notice = ""
        self._streaming = False
        self._turn_phase = ""
        self._turn_since = None
        self._render()

    def set_identity(
        self, project: dict[str, Any], session: dict[str, Any] | None
    ) -> None:
        if self._project != project["id"]:
            self._chat_activity = None
        self._project = project["id"]
        self._project_label = terminal_text(project.get("name") or "Project")
        self._chat = session["id"] if session else ""
        self._chat_title = _plain_title(
            (session or {}).get("title") or "New conversation"
        )
        self._model = terminal_text((session or {}).get("model") or "")
        self._context = self._project_label + " / " + self._chat_title
        self._invalidate()

    def set_model(self, model: str | None) -> None:
        self._model = terminal_text(model or "")
        self._invalidate()

    def set_conversation_activity(self, states: dict[str, str] | None) -> None:
        """Show the selected chat's Core state; None means it is unknown."""
        self._chat_activity = states
        if (
            self._turn_phase == "waiting"
            and self._chat
            and states is not None
            and states.get(self._chat) in {"working", "idle"}
        ):
            self._turn_phase = ""
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
            self._chat_title = _plain_title(selected.get("title") or "Conversation")
            self._context = self._project_label + " / " + self._chat_title
        self._invalidate()

    def activity(self, value: str) -> None:
        clean = terminal_text(value).strip()
        if clean and (not self._activity or self._activity[-1] != clean):
            self._activity.append(clean[:180])
            self._activity = self._activity[-30:]
            self._invalidate()

    def begin_loading(self, value: str) -> None:
        self._loading = terminal_text(value).strip()[:180]
        self._loading_since = time.monotonic()
        self._invalidate()

    def clear_loading(self) -> None:
        if self._loading:
            self._loading = ""
            self._loading_since = None
            self._invalidate()

    def clear_notice(self) -> None:
        self._notice = ""
        self._invalidate()

    def show_problem(self, value: str) -> None:
        self.clear_loading()
        lines = terminal_text(value).strip().splitlines()
        self._notice = (
            lines[0]
            if lines
            else "Core request failed. Check saved state before retrying."
        )[:180]
        self._invalidate()

    def set_turn_phase(self, phase: str) -> None:
        if phase not in {"sending", "working", "queued", "waiting", "reconnecting", ""}:
            raise ValueError(f"Unknown turn phase: {phase}")
        if phase in {"sending", "working", "reconnecting"} and self._turn_since is None:
            self._turn_since = time.monotonic()
        elif phase in {"queued", "waiting", ""}:
            self._turn_since = None
        if phase == "waiting":
            self._streaming = False
        self._turn_phase = phase
        self._invalidate()

    def stream_stopped(self) -> None:
        """Stop the local spinner; Core may still own an active background turn."""
        self._streaming = False
        self.set_turn_phase("")

    def say(self, value: Any = "") -> None:
        clean = terminal_text(value)
        notice = clean.lstrip()
        if notice.startswith(("Action required", "Connection lost", "Cannot ")):
            self._notice = notice.splitlines()[0]
            self.clear_loading()
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
        self.set_turn_phase("working")
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
        self.clear_loading()
        self._streaming = not queued
        self.set_turn_phase("queued" if queued else "sending")
        clean = terminal_text(text)
        if clean.strip():
            self._local_submissions = (self._local_submissions + [clean])[
                -RECALL_LIMIT:
            ]
            self._message_history = (self._message_history + [clean])[-RECALL_LIMIT:]
        self._reset_recall()
        self._entries.append(("You · queued" if queued else "You · sending", clean))
        self._prune_entries()
        self._notice = ""
        self._schedule_render()

    def finish_turn(self) -> None:
        self.stream_stopped()
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

        if self._recall_index is not None:
            draft = self._recall_draft or Document("")
            self._reset_recall()
            self._show_recalled(draft)
        saved: list[tuple[str, str]] = []
        saved_inputs: list[str] = []
        for row in rows:
            if not visible_message(row):
                continue
            input_text = terminal_text(row.get("content") or "")
            if row["role"] == "user" and input_text.strip():
                saved_inputs.append(input_text)
            content = input_text
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
            self._message_history = (saved_inputs + self._message_history)[
                -RECALL_LIMIT:
            ]
        else:
            overlap = next(
                (
                    count
                    for count in range(
                        min(len(saved_inputs), len(self._local_submissions)),
                        0,
                        -1,
                    )
                    if saved_inputs[-count:] == self._local_submissions[:count]
                ),
                0,
            )
            self._local_submissions = self._local_submissions[overlap:]
            self._message_history = (saved_inputs + self._local_submissions)[
                -RECALL_LIMIT:
            ]
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

    def _reset_recall(self) -> None:
        self._recall_index = None
        self._recall_draft = None

    def _composer_changed(self) -> None:
        self._disarm_interrupt()
        if not self._applying_recall and self._recall_index is not None:
            self._reset_recall()
            self._invalidate()

    def _can_recall_up(self) -> bool:
        buffer = self.composer.buffer
        return (
            self._mode == "main"
            and bool(self._message_history)
            and buffer.complete_state is None
            and (
                self._recall_index is not None
                or buffer.document.cursor_position_row == 0
            )
        )

    def _can_recall_down(self) -> bool:
        return (
            self._mode == "main"
            and self._recall_index is not None
            and self.composer.buffer.complete_state is None
        )

    def _show_recalled(self, document: Document) -> None:
        self._applying_recall = True
        try:
            self.composer.buffer.set_document(document)
        finally:
            self._applying_recall = False
        self._invalidate()

    def _recall_previous(self) -> None:
        if not self._can_recall_up():
            return
        if self._recall_index is None:
            self._recall_draft = self.composer.buffer.document
            self._recall_index = len(self._message_history) - 1
        else:
            self._recall_index = max(0, self._recall_index - 1)
        text = self._message_history[self._recall_index]
        self._show_recalled(Document(text, cursor_position=len(text)))

    def _recall_next(self) -> None:
        if not self._can_recall_down():
            return
        assert self._recall_index is not None
        if self._recall_index == len(self._message_history) - 1:
            draft = self._recall_draft or Document("")
            self._reset_recall()
            self._show_recalled(draft)
            return
        self._recall_index += 1
        text = self._message_history[self._recall_index]
        self._show_recalled(Document(text, cursor_position=len(text)))

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
        self._disarm_interrupt()
        self.composer.text = ""
        if value.startswith("/"):
            command = value[1:].split(maxsplit=1)
            action = f"Running /{command[0]}…" if command else "Running command…"
        else:
            action = "Sending message…"
        self.begin_loading(action)
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
        self._disarm_interrupt()
        self._preserved_draft = (
            self._recall_draft.text
            if self._recall_index is not None and self._recall_draft is not None
            else self.composer.text
        )
        self.composer.text = ""
        self.begin_loading(f"Running /{name}…")
        self._main_future.set_result("/" + name)

    def _disarm_interrupt(self) -> None:
        was_armed = self._interrupt_armed
        self._interrupt_armed = False
        self._interrupt_cleared_draft = False
        if self._interrupt_timer is not None:
            self._interrupt_timer.cancel()
            self._interrupt_timer = None
        if was_armed:
            self._invalidate()

    def _interrupt(self) -> None:
        if self._mode != "main":
            self._cancel_modal()
            return
        if (
            self._interrupt_armed
            and self._main_future is not None
            and not self._main_future.done()
        ):
            self._disarm_interrupt()
            self._complete_main(InterruptedPrompt())
            return
        had_draft = bool(self.composer.text)
        if had_draft:
            self.composer.text = ""
            if self.on_draft_cleared is not None:
                self.on_draft_cleared()
        if self._main_future is not None and not self._main_future.done():
            self._interrupt_armed = True
            self._interrupt_cleared_draft = had_draft
            self._interrupt_timer = asyncio.get_running_loop().call_later(
                INTERRUPT_REPEAT_SECONDS, self._disarm_interrupt
            )
            self._invalidate()

    def _cancel_modal(self) -> None:
        if self._mode == "notes" and self._notes_screen is not None:
            self._notes_screen.request_close()
            return
        if self._picker is not None and not self._picker.future.done():
            self._picker.future.set_exception(CancelledInput())
        if self._prompt_future is not None and not self._prompt_future.done():
            self._prompt_future.set_exception(CancelledInput())

    def _finish_modal(self) -> None:
        self._disarm_interrupt()
        if self._snapshot_refresh_task is not None:
            self._snapshot_refresh_task.cancel()
            self._snapshot_refresh_task = None
        self._mode = "main"
        self._picker = None
        self._notes_screen = None
        self._prompt_future = None
        self._modal_input = None
        self._modal_container = Window(height=0)
        self._modal_visible = False
        self.app.layout.focus(self.composer)
        self._invalidate()

    async def show_snapshot(
        self, content: str, *, refresh: Callable[[], Awaitable[str]]
    ) -> None:
        """Keep project state separate from the selected chat's transcript."""
        self._mode = "snapshot"
        self._prompt_future = asyncio.get_running_loop().create_future()
        snapshot = TextArea(
            text=content,
            read_only=True,
            focusable=True,
            wrap_lines=True,
            scrollbar=True,
            style="class:transcript",
        )
        snapshot_keys = KeyBindings()

        @snapshot_keys.add("escape")
        def close(_event: Any) -> None:
            if self._prompt_future is not None and not self._prompt_future.done():
                self._prompt_future.set_result("")

        @snapshot_keys.add("r")
        def reload(_event: Any) -> None:
            if (
                self._snapshot_refresh_task is not None
                and not self._snapshot_refresh_task.done()
            ):
                return

            async def read() -> None:
                self.begin_loading("Refreshing project snapshot…")
                previous = snapshot.text
                try:
                    snapshot.text = await refresh()
                except Exception as exc:
                    # diagnostic-expected: retain the previous read for retry.
                    snapshot.text = (
                        previous
                        + "\n\nRefresh failed: "
                        + terminal_text(exc)
                        + ". Press R to retry."
                    )
                finally:
                    self.clear_loading()
                    self._invalidate()

            self._snapshot_refresh_task = asyncio.create_task(read())

        snapshot.control.key_bindings = snapshot_keys
        self._modal_container = Frame(
            HSplit(
                [
                    snapshot,
                    Window(
                        FormattedTextControl(
                            " ↑↓/PgUp/PgDn Scroll · R Refresh · Esc Close"
                        ),
                        height=1,
                    ),
                ]
            ),
            title="Project Snapshot",
        )
        self._modal_visible = True
        self.app.layout.focus(snapshot)
        self._invalidate()
        try:
            await self._prompt_future
        finally:
            self._finish_modal()

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
            self.clear_loading()
            self._disarm_interrupt()
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
        self.clear_loading()
        self._mode = "prompt"
        self._prompt_future = asyncio.get_running_loop().create_future()
        prompt_keys = KeyBindings()

        @prompt_keys.add("enter")
        def submit(_event: Any) -> None:
            assert self._modal_input is not None
            if self._prompt_future is not None and not self._prompt_future.done():
                self.begin_loading("Processing answer…")
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

    def set_selection_summary(self, *, skill: str, mcp_count: int) -> None:
        clean = terminal_text(skill)
        if (clean, mcp_count) != (self._draft_skill, self._selected_mcp_count):
            self._draft_skill = clean
            self._selected_mcp_count = mcp_count
            self._invalidate()

    def _menu_text(self) -> str:
        if self._picker is None:
            return ""
        rows = self._matching_items()
        checked = self._picker.checked
        summary = ""
        if checked is not None:
            names = [
                terminal_text(row.get("name") or row["id"])
                for row in self._picker.items
                if row["id"] in checked and row["id"] != "__done"
            ]
            summary = (
                _edge(
                    f" {len(checked)} selected"
                    + (" · " + ", ".join(names) if names else ""),
                    max(20, self.app.output.get_size().columns - 8),
                )
                + "\n"
            )
        if not rows:
            message = " No matches. Edit search or press Ctrl-G."
            if self._picker.loading:
                message += f"\n {self._spinner()} Loading more from Core…"
            if self._picker.error:
                message += "\n " + _edge(self._picker.error, 100)
            return summary + message
        page_size = max(
            4,
            min(
                20,
                self.app.output.get_size().rows - (12 if checked is not None else 11),
            ),
        )
        start = self._picker.index // page_size * page_size
        visible = rows[start : start + page_size]
        lines = [
            (" › " if start + offset == self._picker.index else "   ")
            + (
                ""
                if checked is None or row["id"] == "__done"
                else "[x] "
                if row["id"] in checked
                else "[ ] "
            )
            + terminal_text(row.get("label") or row.get("name") or row["id"])[:150]
            for offset, row in enumerate(visible)
        ]
        message = summary + "\n".join(lines)
        if self._picker.loading:
            message += f"\n   {self._spinner()} Loading more from Core…"
        if self._picker.error:
            message += "\n   " + _edge(self._picker.error, 100)
        return message

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
        picker.error = ""
        self._invalidate()
        try:
            rows = await picker.more()
            if self._picker is picker:
                picker.items.extend(rows)
                if not rows:
                    picker.more = None
                picker.index = max(0, len(picker.items) - len(rows))
        except (ClientError, OSError, httpx.HTTPError) as exc:
            picker.error = "Could not load more. Press Enter to retry: " + str(exc)
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
        elif picker.checked is not None:
            if row["id"] == "__done":
                self._menu_commit()
            else:
                picker.error = toggle_selection(picker.items, picker.checked, row)
                self._invalidate()
        elif not picker.future.done():
            self.begin_loading(f"Opening {picker.title.lower()}…")
            picker.future.set_result(row)

    def _menu_commit(self) -> None:
        picker = self._picker
        if (
            picker is not None
            and picker.checked is not None
            and not picker.future.done()
        ):
            self.begin_loading("Saving selection…")
            picker.future.set_result({"id": "__done", "checked": list(picker.checked)})

    async def choose(
        self,
        title: str,
        items: list[dict[str, Any]],
        *,
        selected: str | None = None,
        more: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        multi_selected: list[str] | None = None,
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
        self.clear_loading()
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._mode = "menu"
        picker = Picker(
            title,
            list(items),
            future,
            more,
            checked=list(dict.fromkeys(multi_selected))
            if multi_selected is not None
            else None,
        )
        self._picker = picker
        menu_keys = KeyBindings()

        @menu_keys.add("enter")
        def select(_event: Any) -> None:
            self._menu_select()

        if multi_selected is not None:

            @menu_keys.add(" ")
            def toggle(_event: Any) -> None:
                self._menu_select()

            @menu_keys.add("c-s")
            def commit(_event: Any) -> None:
                self._menu_commit()

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
                        FormattedTextControl(
                            "↑↓ Move · Space/Enter Toggle · Ctrl-S Save · Ctrl-G Cancel"
                            if multi_selected is not None
                            else "↑↓ Select · Enter Open · Ctrl-G Cancel"
                        ),
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

    async def multiple(
        self, title: str, items: list[dict[str, Any]], selected: list[str]
    ) -> list[str]:
        result = await self.choose(
            title,
            [{"id": "__done", "name": "Done · save selection"}, *items],
            multi_selected=selected,
        )
        return list(result["checked"])

    def _menu_changed(self) -> None:
        if self._picker is not None:
            self._picker.index = 0
        self._invalidate()

    def help(self) -> None:
        self.say(
            "Keys: F2 projects · F3 chats · F4 commands · F5 activity · F6 follow · F8 approvals · F11 snapshot · Tab transcript · Ctrl-C clear draft, twice within 4s stop turn · Ctrl-D quit"
        )
        super().help()
