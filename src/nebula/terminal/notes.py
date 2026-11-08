"""Project Notes in the paired full-screen terminal client."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from .transport import ApiError, ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell
    from .transport import AsyncApi
    from .tui import TuiUI


NOTE_TYPES = {"note", "ai_tool_note", "ai_execution_note"}
PAGE_SIZE = 100


class ProjectNotes:
    """Keep Core as the authority for saved project notes and revisions."""

    def __init__(self, api: AsyncApi, project_id: str) -> None:
        self.api = api
        self.project_id = project_id

    def _note(self, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("engagement_id") != self.project_id
            or value.get("observation_type") not in NOTE_TYPES
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("revision"), int)
        ):
            raise ClientError("Core returned an invalid project note.")
        return value

    async def page(self, offset: int) -> tuple[list[dict[str, Any]], int, bool]:
        values = await self.api.request(
            "GET",
            "observations",
            params={
                "engagement_id": self.project_id,
                "offset": offset,
                "limit": PAGE_SIZE,
            },
        )
        if not isinstance(values, list):
            raise ClientError("Core returned an invalid project note list.")
        notes = [
            self._note(value)
            for value in values
            if isinstance(value, dict) and value.get("observation_type") in NOTE_TYPES
        ]
        return notes, offset + len(values), len(values) == PAGE_SIZE

    async def get(self, note_id: str) -> dict[str, Any]:
        return self._note(await self.api.request("GET", f"observations/{key(note_id)}"))

    async def find_create(self, marker: str) -> dict[str, Any] | None:
        offset = 0
        while True:
            rows, next_offset, more = await self.page(offset)
            for row in rows:
                if (row.get("metadata") or {}).get("terminal_create_id") == marker:
                    return row
            if not more:
                return None
            offset = next_offset

    async def create(
        self, title: str, body: str, marker: str, *, reconcile: bool
    ) -> dict[str, Any]:
        if reconcile:
            existing = await self.find_create(marker)
            if existing is not None:
                if existing.get("title") == title and existing.get("body") == body:
                    return existing
                return await self.update(existing, title, body)
        try:
            created = await self.api.request(
                "POST",
                "observations",
                json={
                    "engagement_id": self.project_id,
                    "observation_type": "note",
                    "title": title,
                    "body": body,
                    "source": "operator-note",
                    "metadata": {"terminal_create_id": marker},
                },
            )
        except (ClientError, httpx.HTTPError):
            # A lost create response must be reconciled by its stable marker.
            existing = await self.find_create(marker)
            if existing is not None:
                if existing.get("title") == title and existing.get("body") == body:
                    return existing
                return await self.update(existing, title, body)
            raise
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise ClientError("Core did not identify the saved note. Refresh Notes.")
        return await self.get(created["id"])

    async def update(
        self, note: dict[str, Any], title: str, body: str
    ) -> dict[str, Any]:
        note_id = note["id"]
        try:
            await self.api.request(
                "PATCH",
                f"observations/{key(note_id)}",
                json={
                    "changes": {"title": title, "body": body},
                    "expected_revision": note["revision"],
                },
            )
        except (ClientError, httpx.HTTPError):
            # Read back before a retry: Core may have committed a timed-out write.
            current = await self.get(note_id)
            if current.get("title") == title and current.get("body") == body:
                return current
            raise
        return await self.get(note_id)

    async def deletable(self, note: dict[str, Any]) -> bool:
        result = await self.api.request(
            "GET", f"observations/{key(note['id'])}/dependencies"
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("deletable"), bool
        ):
            raise ClientError("Core did not confirm whether this note can be deleted.")
        return result["deletable"]

    async def delete(self, note: dict[str, Any]) -> None:
        await self.api.request(
            "DELETE",
            f"observations/{key(note['id'])}",
            headers={"If-Match": str(note["revision"])},
        )
        try:
            await self.get(note["id"])
        except ApiError as exc:
            if exc.status == 404:
                return
            raise
        raise ClientError("Core still shows this note. Refresh Notes before retrying.")


class NotesScreen:
    """One on-demand Notes panel with local drafts and explicit save feedback."""

    def __init__(self, ui: TuiUI, project_name: str, service: ProjectNotes) -> None:
        self.ui = ui
        self.service = service
        self.project_name = terminal_text(project_name).strip() or "Project"
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.rows: list[dict[str, Any]] = []
        self.index = 0
        self.offset = 0
        self.more = False
        self.is_new = False
        self.create_marker: str | None = None
        self.create_attempted = False
        self.base_title = ""
        self.base_body = ""
        self.busy = False
        self.loading_list = True
        self.status = "Loading project notes…"
        self.confirm_close = False
        self.confirm_delete = False
        self.confirm_reload = False
        self._syncing = False
        self.tasks: set[asyncio.Task[None]] = set()

        list_keys = KeyBindings()
        edit_keys = KeyBindings()

        @list_keys.add("up")
        def up(_event: Any) -> None:
            self._start(self.select(self.index - 1))

        @list_keys.add("down")
        def down(_event: Any) -> None:
            self._start(self.select(self.index + 1))

        @list_keys.add("pageup")
        def page_up(_event: Any) -> None:
            self._start(self.select(self.index - 5))

        @list_keys.add("pagedown")
        def page_down(_event: Any) -> None:
            self._start(self.select(self.index + 5))

        @list_keys.add("n")
        def new_from_list(_event: Any) -> None:
            if self.confirm_delete:
                self.confirm_delete = False
                self._status("Delete cancelled.")
            else:
                self._start(self.new())

        @list_keys.add("r")
        def refresh_from_list(_event: Any) -> None:
            self._start(self.refresh())

        @list_keys.add("m")
        def more_from_list(_event: Any) -> None:
            self._start(self.load_more())

        @list_keys.add("d")
        def delete_from_list(_event: Any) -> None:
            if self.confirm_close:
                self.discard_and_close()
            else:
                self._start(self.request_delete())

        @list_keys.add("y")
        def confirm_delete(_event: Any) -> None:
            if self.confirm_delete:
                self._start(self.delete())

        @list_keys.add("enter")
        def edit_selected(_event: Any) -> None:
            self.ui.app.layout.focus(self.title)

        @edit_keys.add("d", filter=Condition(lambda: self.confirm_close))
        def discard_from_editor(_event: Any) -> None:
            self.discard_and_close()

        @edit_keys.add(
            "enter", filter=Condition(lambda: self.ui.app.layout.has_focus(self.title))
        )
        def title_enter(_event: Any) -> None:
            self.ui.app.layout.focus(self.body)

        common = KeyBindings()

        @common.add("c-s")
        def save(_event: Any) -> None:
            self._start(self.save())

        @common.add("c-n")
        def new(_event: Any) -> None:
            self._start(self.new())

        @common.add("c-r")
        def refresh(_event: Any) -> None:
            self._start(self.refresh())

        @common.add("escape")
        def close(_event: Any) -> None:
            if self.confirm_delete:
                self.confirm_delete = False
                self._status("Delete cancelled.")
            elif self.confirm_close:
                self.confirm_close = False
                self._status("Unsaved changes remain.")
            else:
                self.request_close()

        @common.add("tab")
        def next_focus(_event: Any) -> None:
            layout = self.ui.app.layout
            layout.focus(
                self.title
                if layout.has_focus(self.list_window)
                else self.body
                if layout.has_focus(self.title)
                else self.list_window
            )

        @common.add("s-tab")
        def previous_focus(_event: Any) -> None:
            layout = self.ui.app.layout
            layout.focus(
                self.body
                if layout.has_focus(self.list_window)
                else self.list_window
                if layout.has_focus(self.title)
                else self.title
            )

        self.list_window = Window(
            FormattedTextControl(
                self._list_fragments,
                focusable=True,
                key_bindings=merge_key_bindings([common, list_keys]),
                get_cursor_position=lambda: Point(x=0, y=self.index),
            ),
            height=5,
            wrap_lines=False,
            style="class:sidebar",
        )
        read_only = Condition(lambda: self.busy or not (self.is_new or self.selected))
        self.title = TextArea(multiline=False, height=1, read_only=read_only)
        self.body = TextArea(
            multiline=True, wrap_lines=True, scrollbar=True, read_only=read_only
        )
        self.title.control.key_bindings = merge_key_bindings([common, edit_keys])
        self.body.control.key_bindings = merge_key_bindings([common, edit_keys])
        self.title.buffer.on_text_changed += lambda _buffer: self._changed()
        self.body.buffer.on_text_changed += lambda _buffer: self._changed()
        self.container = Frame(
            HSplit(
                [
                    Frame(self.list_window, title="Saved notes · ↑↓ select · N new"),
                    Frame(self.title, title="Title"),
                    Frame(self.body, title="Markdown"),
                    Window(
                        FormattedTextControl(self._status_text),
                        height=1,
                        style="class:activity",
                    ),
                ]
            ),
            title="Project Notes · " + self.project_name,
        )

    @property
    def selected(self) -> dict[str, Any] | None:
        return self.rows[self.index] if 0 <= self.index < len(self.rows) else None

    @property
    def dirty(self) -> bool:
        if self.is_new:
            return bool(self.title.text or self.body.text)
        return self.selected is not None and (
            self.title.text != self.base_title or self.body.text != self.base_body
        )

    def _status(self, value: str) -> None:
        lines = terminal_text(value).splitlines()
        self.status = (lines[0] if lines else "Core request failed; retry the action.")[
            :180
        ]
        self.ui._invalidate()

    def _status_text(self) -> str:
        return " " + self.status

    def _list_fragments(self) -> StyleAndTextTuples:
        if not self.rows:
            label = (
                "  Loading notes from Core…"
                if self.loading_list
                else "  New note draft. Ctrl-S saves it."
                if self.is_new
                else "  No notes in this page. Press M for more."
                if self.more
                else "  No notes yet. Ctrl-N creates one."
            )
            return [("class:rail-idle", label)]
        width = max(20, self.ui.app.output.get_size().columns - 10)
        fragments: StyleAndTextTuples = []
        for position, note in enumerate(self.rows):
            tag = " · AI" if note.get("observation_type") != "note" else ""
            title = terminal_text(note.get("title") or "Untitled").replace("\n", " ")
            label = (
                (" › " if position == self.index and not self.is_new else "   ")
                + title
                + tag
            )
            fragments.append(
                (
                    "class:rail-selected"
                    if position == self.index and not self.is_new
                    else "",
                    label[:width] + "\n",
                )
            )
        if self.more:
            fragments.append(("class:rail-idle", "   More notes · press M\n"))
        return fragments

    def _changed(self) -> None:
        if not self._syncing and not self.busy:
            self.confirm_close = False
            self.confirm_reload = False
            self._status("Unsaved · Ctrl-S saves to Core")

    def _show(self, note: dict[str, Any] | None) -> None:
        self.confirm_close = False
        self.confirm_reload = False
        self.confirm_delete = False
        self._syncing = True
        try:
            self.title.text = str(note.get("title") or "") if note else ""
            self.body.text = str(note.get("body") or "") if note else ""
            self.base_title = self.title.text
            self.base_body = self.body.text
        finally:
            self._syncing = False
        self._status("Ready · Tab edits · Ctrl-S saves · Esc returns to chat")

    def _start(self, operation: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(operation)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        self.ui.clear_loading()
        self.ui._mode = "notes"
        self.ui._notes_screen = self
        self.ui._prompt_future = self.future
        self.ui._modal_container = self.container
        self.ui._modal_visible = True
        self.ui.app.layout.focus(self.list_window)
        self.ui._invalidate()
        self._start(self.refresh())
        try:
            await self.future
        finally:
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            self.ui._finish_modal()

    async def load_more(self) -> None:
        if self.busy or not self.more:
            return
        await self._load(reset=False)

    async def refresh(self) -> None:
        if self.busy:
            return
        if self.dirty and not self.confirm_reload:
            self.confirm_reload = True
            self._status(
                "Unsaved draft · copy it first; Ctrl-R again discards and reloads"
            )
            return
        self.confirm_reload = False
        await self._load(reset=True)

    async def _load(self, *, reset: bool) -> None:
        self.busy = True
        self.loading_list = True
        self._status("Refreshing project notes…" if reset else "Loading more notes…")
        previous_id = self.selected["id"] if self.selected and not self.is_new else None
        try:
            rows, offset, more = await self.service.page(0 if reset else self.offset)
            if reset:
                self.rows = rows
                self.index = next(
                    (i for i, row in enumerate(rows) if row["id"] == previous_id), 0
                )
                self.is_new = False
                self._show(self.selected)
            else:
                self.rows.extend(rows)
            self.offset = offset
            self.more = more
            self.busy = False
            self.loading_list = False
            self._status(
                f"{len(self.rows)} project notes · Ctrl-N new · Ctrl-S save"
                if self.rows
                else "No notes in this page · M loads more"
                if self.more
                else "No notes yet · Ctrl-N creates one"
            )
        except (ClientError, httpx.HTTPError, OSError, ValueError) as exc:
            self._status(f"Could not load notes: {exc}. Ctrl-R retries.")
        finally:
            self.busy = False
            self.loading_list = False
            self.ui._invalidate()

    async def select(self, index: int) -> None:
        if self.busy or not self.rows or self.confirm_delete or self.confirm_close:
            return
        index = max(0, min(index, len(self.rows) - 1))
        if index == self.index and not self.is_new:
            return
        if self.dirty:
            self._status(
                "Save with Ctrl-S or close and discard before selecting a note."
            )
            return
        self.index = index
        self.is_new = False
        self._show(self.selected)
        self.ui._invalidate()

    async def new(self) -> None:
        if self.busy or self.confirm_delete or self.confirm_close:
            return
        if self.dirty:
            self._status(
                "Save with Ctrl-S or close and discard before creating a note."
            )
            return
        self.is_new = True
        self.create_marker = str(uuid4())
        self.create_attempted = False
        self._show(None)
        self._status("New note · Enter title, Tab to Markdown, Ctrl-S save")
        self.ui.app.layout.focus(self.title)
        self.ui._invalidate()

    async def save(self) -> bool:
        if self.busy:
            return False
        note = None if self.is_new else self.selected
        if note is None and not self.is_new:
            self._status("Create a note with Ctrl-N first.")
            return False
        title = self.title.text.strip()
        body = self.body.text
        if not title:
            self._status("Enter a title before saving; your draft remains here.")
            return False
        if not self.dirty and not self.is_new:
            self._status("Already saved in Core.")
            return True
        self.busy = True
        self._status("Saving note to Core…")
        try:
            if self.is_new:
                assert self.create_marker is not None
                attempted = self.create_attempted
                self.create_attempted = True
                saved = await self.service.create(
                    title, body, self.create_marker, reconcile=attempted
                )
                self.rows.insert(0, saved)
                self.index = 0
                self.is_new = False
                self.create_marker = None
            else:
                assert note is not None
                saved = await self.service.update(note, title, body)
                self.rows[self.index] = saved
            self._show(saved)
            self._status("Saved in Core · revision " + str(saved["revision"]))
            return True
        except ApiError as exc:
            if exc.status == 409:
                self._status(
                    "Note changed in Core. Draft retained; copy it before Ctrl-R twice to reload."
                )
            else:
                self._status(f"Save failed: {exc}. Ctrl-S retries; draft remains.")
            return False
        except (ClientError, httpx.HTTPError, OSError, ValueError) as exc:
            self._status(
                f"Save unconfirmed: {exc}. Ctrl-S checks Core before retrying."
            )
            return False
        finally:
            self.busy = False
            self.ui._invalidate()

    async def request_delete(self) -> None:
        if self.busy or self.confirm_close or self.confirm_delete or self.is_new:
            return
        note = self.selected
        if note is None:
            return
        if self.dirty:
            self._status("Save this note with Ctrl-S before deleting it.")
            return
        self.busy = True
        self._status("Checking note links before deletion…")
        try:
            if not await self.service.deletable(note):
                self._status("This note is used by a report and cannot be deleted.")
                return
            self.confirm_delete = True
            self._status(
                "Delete this note? Y confirms · Esc cancels. Evidence remains."
            )
        except (ClientError, httpx.HTTPError, OSError, ValueError) as exc:
            self._status(f"Could not check note links: {exc}. D retries.")
        finally:
            self.busy = False

    async def delete(self) -> None:
        if self.busy or not self.confirm_delete or self.selected is None:
            return
        note = self.selected
        self.confirm_delete = False
        self.busy = True
        self._status("Deleting note from Core…")
        try:
            await self.service.delete(note)
            self.rows.pop(self.index)
            self.index = min(self.index, max(0, len(self.rows) - 1))
            self._show(self.selected)
            self._status("Deleted from Core. Linked evidence was retained.")
        except (ClientError, httpx.HTTPError, OSError, ValueError) as exc:
            self._status(f"Delete unconfirmed: {exc}. Ctrl-R refreshes Core state.")
        finally:
            self.busy = False
            self.ui._invalidate()

    def request_close(self) -> None:
        if self.busy:
            self._status("Wait for the current Notes operation to finish.")
        elif self.dirty:
            self.confirm_close = True
            self._status("Unsaved changes · Ctrl-S save · D discard · Esc stay")
        elif not self.future.done():
            self.future.set_result(None)

    def discard_and_close(self) -> None:
        if self.confirm_close and not self.busy and not self.future.done():
            self.future.set_result(None)


async def show_project_notes(shell: Shell) -> None:
    from .tui import TuiUI

    view = shell.current()
    if not isinstance(shell.ui, TuiUI):
        raise ClientError(
            "Project Notes require the full-screen TUI. Run nebula-chat in an interactive terminal."
        )
    screen = NotesScreen(
        shell.ui,
        str(view.project.get("name") or "Project"),
        ProjectNotes(shell.api, view.project_id),
    )
    await screen.run()
