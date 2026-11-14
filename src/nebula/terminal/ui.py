"""Scrollback-friendly asynchronous prompts and searchable numbered menus."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyWordCompleter,
    PathCompleter,
)
from prompt_toolkit.document import Document
from prompt_toolkit.history import DummyHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown

from .transport import ClientError, terminal_text


class CancelledInput(Exception):
    """The operator closed a menu without changing anything."""


class InterruptedPrompt(CancelledInput):
    """Ctrl-C at the main composer, distinct from closing a menu."""


def toggle_selection(
    items: list[dict[str, Any]], chosen: list[str], row: dict[str, Any]
) -> str:
    """Toggle a menu row in place and explain unavailable or limited choices."""
    identifier = row["id"]
    if identifier in chosen:
        chosen.remove(identifier)
        return ""
    if row.get("disabled"):
        return str(row.get("disabled_reason") or "Unavailable on Core.")
    group = row.get("selection_group")
    limit = row.get("selection_group_limit")
    if group and isinstance(limit, int):
        members = [
            item["id"]
            for item in items
            if item.get("selection_group") == group and item["id"] in chosen
        ]
        if len(members) >= limit:
            if limit != 1:
                return f"Choose up to {limit} {group} skills."
            for prior in members:
                chosen.remove(prior)
            chosen.append(identifier)
            return "Only one installed harness skill can be invoked per turn; replaced the previous one."
    chosen.append(identifier)
    return ""


COMMANDS = {
    "draft": "Review a previous submission retained in memory",
    "core": "Switch paired Core",
    "projects": "Browse or create a project",
    "snapshot": "Read this project's Core-owned summary",
    "board": "Browse this project's Work board and check-ins (read-only)",
    "notes": "Browse and edit this project's saved Notes",
    "chats": "Browse saved conversations",
    "new": "Start a new conversation",
    "butler": "Open this project's Butler",
    "chat": "Manage this conversation: rename, pin, flag, archive, edit, retry, export, delete, bulk",
    "search": "Search saved messages in this conversation or project; bookmarked only",
    "bookmark": "Bookmark a saved message, or remove its bookmark",
    "side": "Open, create or close a side chat of this conversation",
    "history": "Read saved messages; /history more reads earlier messages",
    "thinking": "Show or hide the model's thinking in the transcript",
    "context": "Inspect context usage",
    "activity": "Inspect activity and plans",
    "results": "Read saved results",
    "model": "Select a model and runtime",
    "settings": "Change next-turn Assistant settings",
    "skills": "Choose up to eight project skills and one installed skill for one turn",
    "mcp": "Select MCP servers for this conversation",
    "goal": "Create, start, pause, complete or edit this conversation's goal; child goals",
    "agents": "Open subagent and main chats; stop one subagent or all",
    "approvals": "Approve, edit, reject or stop pending actions, including subagents'",
    "input": "Answer a pending question",
    "queue": "Manage follow-ups: add, edit, move, send now, pause, remove or clear",
    "now": "Stop the running turn and send this message next",
    "schedule": "Manage a Core schedule",
    "attach": "Attach a local document/image or Core file",
    "attachments": "Review/remove pending attachments",
    "status": "Read authoritative Core state",
    "follow": "Follow current output",
    "detach": "Detach the viewer; leave Core work running",
    "steer": "Guide a supported active turn",
    "stop": "Cancel the selected turn",
    "quit": "Exit; leave Core work running",
    "help": "Show commands",
    "commands": "Browse commands",
}


class SlashCommandCompleter(Completer):
    """Offer commands only at the start of a slash command, not in message text."""

    def __init__(self, names: Callable[[], tuple[str, ...]]):
        self.names = names

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> list[Completion]:
        prefix = document.text_before_cursor
        if (
            document.text_after_cursor
            or not prefix.startswith("/")
            or any(char.isspace() for char in prefix)
        ):
            return []
        query = prefix[1:].casefold()
        return [
            Completion(
                "/" + name,
                start_position=-len(prefix),
                display_meta=COMMANDS[name],
            )
            for name in self.names()
            if name.startswith(query)
        ]


def bindings() -> KeyBindings:
    keys = KeyBindings()

    @keys.add("enter")
    def send(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    def newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    return keys


class TerminalUI:
    def __init__(self, *, plain: bool | None = None):
        self.plain = (
            plain
            if plain is not None
            else (
                not sys.stdin.isatty()
                or not sys.stdout.isatty()
                or os.environ.get("TERM") == "dumb"
                or os.getenv("NEBULA_CHAT_PLAIN") == "1"
            )
        )
        self.console = Console(
            markup=False,
            highlight=False,
            no_color="NO_COLOR" in os.environ,
            force_terminal=False if self.plain else None,
        )
        self._status = ""
        self._last_activity = ""
        self.command_names = tuple(COMMANDS)
        self.session: PromptSession[str] | None = None
        if not self.plain:
            self.session = PromptSession(
                history=InMemoryHistory(),
                key_bindings=bindings(),
                completer=SlashCommandCompleter(lambda: self.command_names),
                bottom_toolbar=lambda: terminal_text(self.status),
            )

    def set_commands(self, names: list[str]) -> None:
        self.command_names = tuple(name for name in names if name in COMMANDS)

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = terminal_text(value)
        if self.session is not None and self.session.app.is_running:
            self.session.app.invalidate()

    def activity(self, value: str) -> None:
        clean = terminal_text(value).strip()
        if clean and clean != self._last_activity:
            self._last_activity = clean
            self.say("Working: " + clean)

    def submission(self, text: str, *, queued: bool = False) -> None:
        self.say("Queued in Core: " if queued else "Sending to Core…")

    def say(self, text: Any = "") -> None:
        self.console.print(terminal_text(text), markup=False, highlight=False)

    async def show_snapshot(
        self, content: str, *, refresh: Callable[[], Awaitable[str]]
    ) -> None:
        self.say(content)

    def delta(self, text: str) -> None:
        self.console.print(
            terminal_text(text), end="", markup=False, highlight=False, soft_wrap=True
        )

    def markdown(self, text: str) -> None:
        clean = terminal_text(text)
        self.console.print(clean if self.plain else Markdown(clean))

    show_thinking = False

    def set_show_thinking(self, value: bool) -> None:
        self.show_thinking = value

    def reasoning(self, text: str) -> None:
        """Scrolling output keeps thinking out of the answer; /thinking reads it."""

    def turn_meta(self, detail: str, *, stopped: bool = False) -> None:
        label = " · ".join(
            part for part in ("Stopped" if stopped else "", detail) if part
        )
        if label:
            self.say(f"({terminal_text(label)})")

    def set_editing(self, label: str) -> None:
        if label:
            self.say(
                "Editing a saved message. Sending rewinds the conversation to it "
                "and sends the edited text; /cancel at a prompt or an empty send keeps it."
            )

    def reveal(self, text: str) -> bool:
        return False

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
        try:
            if self.plain:
                if secret:
                    import getpass

                    value = await asyncio.to_thread(
                        getpass.getpass, terminal_text(label) + ": "
                    )
                else:
                    if default:
                        self.say("Current value:\n" + default)
                    value = await asyncio.to_thread(input, terminal_text(label) + ": ")
                    if not value:
                        value = default
            else:
                session = (
                    PromptSession(history=DummyHistory()) if secret else self.session
                )
                assert session is not None
                options: dict[str, Any] = {"default": default, "is_password": secret}
                if path:
                    options["completer"] = PathCompleter(expanduser=True)
                elif choices is not None:
                    options["completer"] = FuzzyWordCompleter(
                        [terminal_text(value) for value in choices]
                    )
                value = await session.prompt_async(
                    terminal_text(label) + ": ", **options
                )
            if value.strip() == "/cancel":
                raise CancelledInput()
            return value
        except KeyboardInterrupt as exc:
            raise (InterruptedPrompt() if main else CancelledInput()) from exc

    async def confirm(self, text: str) -> bool:
        return (
            await self.ask(text + " [y/N]", choices=["yes", "no"])
        ).strip().lower() in {"y", "yes"}

    async def choose(
        self,
        title: str,
        items: list[dict[str, Any]],
        *,
        selected: str | None = None,
        more: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> dict[str, Any]:
        if selected:
            matches = [item for item in items if item["id"] == selected]
            if not matches:
                matches = [
                    item
                    for item in items
                    if str(item["name"]).casefold() == selected.casefold()
                ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise ClientError(f"No matching {title}. Choose from the listed names.")
            items = matches
        found, page = items, 0
        while True:
            visible = found[page * 20 : (page + 1) * 20]
            self.say(title + (f" (page {page + 1})" if len(found) > 20 else ""))
            for index, item in enumerate(visible, 1):
                self.say(f"  {index}. {item.get('label') or item['name']}")
            if not found:
                self.say("  No matches. Enter another search or /cancel.")
            self.say(
                "Number or search; /next and /back browse; "
                + ("more loads another catalog page; " if more else "")
                + "/cancel closes."
            )
            answer = (
                await self.ask(
                    "Choose",
                    choices=[str(item["name"]) for item in found]
                    + ["/next", "/back", "/cancel"],
                )
            ).strip()
            if answer == "/cancel":
                raise CancelledInput()
            if answer == "/next":
                if (page + 1) * 20 < len(found):
                    page += 1
                else:
                    self.say(
                        "Last loaded page. Use more to load another catalog page."
                        if more
                        else "Last page."
                    )
                continue
            if answer == "/back":
                page = max(0, page - 1)
                continue
            if answer == "more" and more:
                additions = await more()
                page = len(items) // 20 if additions else page
                items.extend(additions)
                found = items
                if not additions:
                    more = None
                continue
            if answer.isdigit() and 1 <= int(answer) <= len(visible):
                return visible[int(answer) - 1]
            exact = [
                item
                for item in found
                if str(item["name"]).casefold() == answer.casefold()
            ]
            if len(exact) == 1:
                return exact[0]
            found = [
                item
                for item in items
                if answer.casefold() in str(item["name"]).casefold()
            ]
            page = 0
            if len(found) == 1:
                return found[0]

    async def multiple(
        self, title: str, items: list[dict[str, Any]], selected: list[str]
    ) -> list[str]:
        chosen = list(dict.fromkeys(selected))
        while True:
            options = [
                {
                    **item,
                    "label": ("[x] " if item["id"] in chosen else "[ ] ")
                    + str(item.get("label") or item["name"]),
                }
                for item in items
            ]
            item = await self.choose(
                f"{title} · {len(chosen)} selected",
                [{"id": "__done", "name": "Done · save selection"}, *options],
            )
            if item["id"] == "__done":
                return chosen
            notice = toggle_selection(items, chosen, item)
            if notice:
                self.say(notice)

    def help(self) -> None:
        for name, detail in COMMANDS.items():
            self.say(f"/{name} — {detail}")
        self.say(
            "Enter sends; Esc+Enter adds a line. Text while busy queues a follow-up. Ctrl-C stops the selected turn; Ctrl-D exits."
        )
