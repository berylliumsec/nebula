import json
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QFocusEvent, QKeyEvent, QMouseEvent, QTextCursor
from PyQt6.QtWidgets import QDialog, QTextEdit, QWidget

from nebula import ai_notes_pop_up_window


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````\x00"
    b"\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ImmediateThreadPool:
    def __init__(self, *args, **kwargs):
        self.started = []

    def start(self, runnable):
        self.started.append(runnable)
        runnable.run()


class FakeEditDialog:
    accepted = True

    def __init__(self, command_text, parent=None, command_input_area=None):
        self.command_text = command_text
        self.parent = parent
        self.command_input_area = command_input_area

    def exec(self):
        return (
            QDialog.DialogCode.Accepted
            if self.accepted
            else QDialog.DialogCode.Rejected
        )

    def get_command(self):
        return f"edited:{self.command_text}"


class FakeSearchReplaceDialog:
    instances = []

    def __init__(self, text_edit, parent):
        self.text_edit = text_edit
        self.parent = parent
        self.shown = False
        type(self).instances.append(self)

    def show(self):
        self.shown = True


class FakeSearchWindow:
    def __init__(self):
        self.calls = []

    def add_to_index(self, text, index_dir):
        self.calls.append((text, index_dir))


class FakeCommandInputArea:
    def __init__(self):
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)


class FakePopupAiNotes(QTextEdit):
    def __init__(
        self,
        file_path=None,
        bookmarks_path=None,
        parent=None,
        manager=None,
        command_input_area=None,
        search_window=None,
    ):
        super().__init__(parent)
        self.file_path = file_path
        self.bookmarks_path = bookmarks_path
        self.manager = manager
        self.command_input_area = command_input_area
        self.search_window = search_window
        self.bookmark_changed_callback = None
        self.bookmarks = [{"name": "alpha", "position": 1}]
        self.goto_positions = []
        if file_path and Path(file_path).exists():
            self.setHtml(Path(file_path).read_text())

    def set_notes_file(self, path):
        self.file_path = path

    def toggle_bookmark(self):
        position = len(self.bookmarks) + 1
        self.bookmarks.append({"name": f"bookmark-{position}", "position": position})
        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def removeBookmark(self, position):
        self.bookmarks = [
            bookmark for bookmark in self.bookmarks if bookmark["position"] != position
        ]
        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def gotoBookmark(self, position):
        self.goto_positions.append(position)


class PopupStub:
    def __init__(self, visible=False):
        self.visible = visible
        self.hidden = False
        self.current_index = None

    def isVisible(self):
        return self.visible

    def hide(self):
        self.hidden = True
        self.visible = False

    def setCurrentIndex(self, index):
        self.current_index = index

    def sizeHintForColumn(self, _column):
        return 20

    def verticalScrollBar(self):
        return SimpleNamespace(sizeHint=lambda: SimpleNamespace(width=lambda: 10))


class CompleterStub:
    def __init__(self, visible=False):
        self.prefixes = []
        self.completed = 0
        self.widget_value = None
        self.popup_value = PopupStub(visible=visible)

    def setWidget(self, widget):
        self.widget_value = widget

    def popup(self):
        return self.popup_value

    def setCompletionMode(self, mode):
        self.mode = mode

    def activated(self, callback):
        self.callback = callback

    def setCompletionPrefix(self, prefix):
        self.prefixes.append(prefix)

    def completionModel(self):
        return SimpleNamespace(index=lambda row, column: (row, column))

    def complete(self):
        self.completed += 1

    def completionPrefix(self):
        return self.prefixes[-1] if self.prefixes else ""


def make_paths(tmp_path):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; font-size: 10pt; }")
    icon = tmp_path / "icon.png"
    icon.write_bytes(PNG_BYTES)
    return stylesheet, icon


def make_manager(tmp_path):
    log_dir = tmp_path / "logs"
    notes_dir = tmp_path / "notes"
    log_dir.mkdir()
    notes_dir.mkdir()
    privacy_file = tmp_path / "privacy.txt"
    privacy_file.write_text("")
    config = {
        "LOG_DIRECTORY": str(log_dir),
        "SUGGESTIONS_NOTES_DIRECTORY": str(notes_dir),
        "PRIVACY_FILE": str(privacy_file),
    }
    return SimpleNamespace(load_config=lambda: config)


def patch_common(monkeypatch, tmp_path):
    stylesheet, icon = make_paths(tmp_path)
    monkeypatch.setattr(
        ai_notes_pop_up_window,
        "return_path",
        lambda path: str(stylesheet if path.endswith(".css") else icon),
    )
    monkeypatch.setattr(ai_notes_pop_up_window, "QThreadPool", ImmediateThreadPool)
    monkeypatch.setattr(
        ai_notes_pop_up_window.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )
    monkeypatch.setattr(
        ai_notes_pop_up_window.utilities,
        "EditCommandDialog",
        FakeEditDialog,
    )
    monkeypatch.setattr(
        ai_notes_pop_up_window,
        "SearchReplaceDialog",
        FakeSearchReplaceDialog,
    )
    return stylesheet, icon


def build_notes_widget(tmp_path, monkeypatch):
    patch_common(monkeypatch, tmp_path)
    manager = make_manager(tmp_path)
    log_file = Path(manager.load_config()["LOG_DIRECTORY"]) / "scan.txt"
    log_file.write_text("alpha beta\nalpha gamma\n")
    notes_file = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"]) / "ai.html"
    notes_file.write_text("<p>initial</p>")
    bookmarks_path = (
        Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"]) / "marks.json"
    )
    bookmarks_path.write_text(json.dumps([{"name": "mark", "position": 1}]))
    search_window = FakeSearchWindow()
    command_input = FakeCommandInputArea()
    notes = ai_notes_pop_up_window.AiNotes(
        file_path=str(notes_file),
        bookmarks_path=str(bookmarks_path),
        manager=manager,
        command_input_area=command_input,
        search_window=search_window,
    )
    return notes, manager, command_input, search_window, notes_file, bookmarks_path


def select_document(widget):
    cursor = widget.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    widget.setTextCursor(cursor)


def test_ai_notes_helper_classes(qapp, tmp_path, monkeypatch):
    patch_common(monkeypatch, tmp_path)
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "one.txt").write_text("red blue\nred")
    (directory / "two.txt").write_text("green blue")

    results = []
    processor = ai_notes_pop_up_window.FileProcessor(str(directory), results.extend)
    processor.run()
    assert "red" in results and "green" in results

    title_bar = ai_notes_pop_up_window.CustomTitleBar()
    try:
        assert title_bar.layout().count() == 1
    finally:
        title_bar.close()

    editor = QTextEdit()
    completer = ai_notes_pop_up_window.CustomCompleter(editor)
    try:
        editor.setPlainText("alpha")
        completer.setWidget(editor)
        popup = PopupStub()
        popup.sizeHintForColumn = lambda _column: 20
        popup.verticalScrollBar = lambda: SimpleNamespace(
            sizeHint=lambda: SimpleNamespace(width=lambda: 10)
        )
        completed_rects = []
        monkeypatch.setattr(completer, "popup", lambda: popup)
        monkeypatch.setattr(
            ai_notes_pop_up_window.QCompleter,
            "complete",
            lambda self, rect=None: completed_rects.append(rect),
        )
        completer.complete(QRect())
        assert completer.caseSensitivity() == Qt.CaseSensitivity.CaseInsensitive
        assert completed_rects
    finally:
        editor.close()


def test_ai_notes_widget_behaviour(qapp, tmp_path, monkeypatch):
    notes, manager, command_input, search_window, notes_file, bookmarks_path = (
        build_notes_widget(tmp_path, monkeypatch)
    )
    opened_urls = []
    menu_execs = []
    monkeypatch.setattr(
        ai_notes_pop_up_window.utilities,
        "open_url",
        opened_urls.append,
    )
    monkeypatch.setattr(
        ai_notes_pop_up_window.QMenu,
        "exec",
        lambda self, position: menu_execs.append(position),
    )

    try:
        assert "initial" in notes.toHtml()
        assert notes.bookmarks == [{"name": "mark", "position": 1}]

        notes.refreshAutocomplete()
        assert "alpha" in notes.completer.model().stringList()
        notes.updateCompleter(["delta", "omega"])
        assert notes.completer.model().stringList() == ["delta", "omega"]

        notes.setPlainText("alpha beta")
        cursor = notes.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfWord,
            QTextCursor.MoveMode.KeepAnchor,
        )
        notes.setTextCursor(cursor)
        assert notes.textUnderCursor() == "alpha"

        fake_completer = CompleterStub()
        notes.completer = fake_completer
        notes.textUnderCursor = lambda: "alpha"
        notes.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_A,
                Qt.KeyboardModifier.NoModifier,
                "a",
            )
        )
        assert fake_completer.prefixes[-1] == "alpha"
        assert fake_completer.completed == 1

        hidden_completer = CompleterStub()
        notes.completer = hidden_completer
        notes.textUnderCursor = lambda: "a"
        notes.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_B,
                Qt.KeyboardModifier.NoModifier,
                "b",
            )
        )
        assert hidden_completer.popup().hidden is True

        visible_completer = CompleterStub(visible=True)
        notes.completer = visible_completer
        notes.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Up,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        notes.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        notes.completer = ai_notes_pop_up_window.CustomCompleter(notes)
        notes.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        assert notes.completer.widget() is notes

        notes.cursorForPosition = lambda point: SimpleNamespace(
            charFormat=lambda: SimpleNamespace(anchorHref=lambda: "https://example.com")
        )
        notes.mouseDoubleClickEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                QPointF(0, 0),
                QPointF(0, 0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert opened_urls == ["https://example.com"]

        notes.cursorForPosition = lambda point: SimpleNamespace(
            charFormat=lambda: SimpleNamespace(anchorHref=lambda: "")
        )
        notes.mouseDoubleClickEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                QPointF(0, 0),
                QPointF(0, 0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        missing_path = tmp_path / "missing.html"
        notes.set_notes_file(str(missing_path))
        notes.set_notes_file(str(notes_file))
        notes.setPlainText("saved")
        notes.save_notes_if_changed()
        assert "saved" in notes_file.read_text()

        select_document(notes)
        notes.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(1, 1),
                QPointF(1, 1),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert menu_execs[-2:] == [QPoint(1, 1), QPoint(1, 1)]

        notes.show_context_menu(QPoint(2, 3))
        assert menu_execs[-2:] == [QPoint(2, 3), QPoint(2, 3)]

        select_document(notes)
        notes.index()
        assert search_window.calls and search_window.calls[-1][0]

        linked = tmp_path / "linked.txt"
        linked.write_text("link")
        monkeypatch.setattr(
            ai_notes_pop_up_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(linked), "txt"),
        )
        notes.insertFileLink()
        assert "linked.txt" in notes.toHtml()

        notes.setPlainText("print('hi')")
        select_document(notes)
        notes.formatSelectedCode()
        monkeypatch.setattr(
            ai_notes_pop_up_window,
            "guess_lexer",
            lambda code: (_ for _ in ()).throw(ValueError("no lexer")),
        )
        assert "code" in notes.format_code("value")

        triggered = []
        notes.set_bookmark_changed_callback(lambda: triggered.append("changed"))
        notes.command_input_area = command_input
        notes.setPlainText("nmap -sV")
        select_document(notes)
        FakeEditDialog.accepted = True
        notes.edit_and_run()
        notes.ask_assistant()
        assert command_input.commands[-2:] == ["edited:nmap -sV", "edited:nmap -sV"]

        notes.moveCursor(QTextCursor.MoveOperation.Start)
        monkeypatch.setattr(
            ai_notes_pop_up_window.QInputDialog,
            "getText",
            lambda *args, **kwargs: ("bookmark-2", True),
        )
        notes.toggle_bookmark()
        assert any(bookmark["name"] == "bookmark-2" for bookmark in notes.bookmarks)
        notes.toggle_bookmark()
        assert not any(bookmark["name"] == "bookmark-2" for bookmark in notes.bookmarks)

        notes.bookmarks = [{"name": "stay", "position": 1}]
        cursor = notes.textCursor()
        cursor.setPosition(1)
        notes.setTextCursor(cursor)
        notes.remove_current_bookmark()
        assert notes.bookmarks == []

        notes.setPlainText("private")
        select_document(notes)
        notes.excludeWord()
        assert "private" in Path(manager.load_config()["PRIVACY_FILE"]).read_text()

        notes.setPlainText("list text")
        select_document(notes)
        notes.set_number_list()
        notes.set_heading()
        notes.set_italic()
        notes.set_bold()
        notes.set_bold()
        notes.set_underline()
        notes.set_bullet_list()
        notes.copy_selected_text()
        assert "list text" in qapp.clipboard().text()

        monkeypatch.setattr(
            ai_notes_pop_up_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor("red"),
        )
        notes.set_color()
        notes.append_text("extra")
        assert "extra" in notes.toHtml()

        notes.bookmarks = [
            {"name": "one", "position": 1},
            {"name": "two", "position": 4},
        ]
        notes.gotoBookmark(4)
        notes.goto_next_bookmark()
        notes.goto_prev_bookmark()
        notes.save_bookmarks()
        assert json.loads(bookmarks_path.read_text())[0]["name"] == "one"

        bookmarks_path.write_text(json.dumps({"invalid": True}))
        notes.load_bookmarks()
        bookmarks_path.write_text("{bad json")
        notes.load_bookmarks()
        bookmarks_path.unlink()
        notes.load_bookmarks()

        action = ai_notes_pop_up_window.QAction("Run", notes)
        notes.change_icon_temporarily(action, str(notes_file), str(bookmarks_path))
        notes.provide_feedback_and_execute(
            action,
            str(notes_file),
            str(bookmarks_path),
            lambda: triggered.append("executed"),
        )
        assert triggered[-1] == "executed"
    finally:
        notes.close()


def test_ai_notes_popup_window_workflow(qapp, tmp_path, monkeypatch):
    patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ai_notes_pop_up_window, "AiNotes", FakePopupAiNotes)
    manager = make_manager(tmp_path)
    notes_dir = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"])
    current_notes = notes_dir / "popup.html"
    current_notes.write_text("<p>popup</p>")
    (notes_dir / "ai_notes_bookmarks.bookmarks").write_text(
        json.dumps([{"name": "alpha", "position": 1}])
    )
    errors = []
    monkeypatch.setattr(
        ai_notes_pop_up_window.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    window = ai_notes_pop_up_window.AiNotesPopupWindow(
        str(current_notes),
        manager,
        FakeCommandInputArea(),
    )
    window.show()
    qapp.processEvents()

    try:
        assert window.windowTitle() == "AI Notes"
        assert window.bookmarksDock.isHidden() is True
        assert "popup" in window.textEdit.toHtml()

        updates = []
        window.textUpdated.connect(updates.append)
        window.on_text_changed()
        assert updates

        window.setTextInTextEdit("plain")
        assert window.textEdit.toPlainText() == "plain"
        monkeypatch.setattr(
            window.textEdit,
            "setHtml",
            lambda text: (_ for _ in ()).throw(RuntimeError("set failed")),
        )
        window.setTextInTextEdit("broken")

        action = ai_notes_pop_up_window.QAction("Act", window)
        window.change_icon_temporarily(action, str(current_notes), str(current_notes))
        hit = []
        window.provide_feedback_and_execute(
            action,
            str(current_notes),
            str(current_notes),
            lambda: hit.append(True),
        )
        assert hit == [True]

        window.toggleBookmarksDock()
        assert window.bookmarksDock.isHidden() is False
        window.updateBookmarksList()
        assert window.bookmarksListWidget.count() >= 1
        window.gotoBookmarkFromList(window.bookmarksListWidget.item(0))
        monkeypatch.setattr(
            ai_notes_pop_up_window.QInputDialog,
            "getText",
            lambda *args, **kwargs: ("popup-bookmark", True),
        )
        window.add_bookmark()
        window.bookmarksListWidget.setCurrentRow(0)
        window.removeBookmark()

        window.on_search_replace_triggered()
        assert FakeSearchReplaceDialog.instances[-1].shown is True

        font_before = window.textEdit.currentFont().pointSize()
        window.adjustFontSize(2)
        assert window.textEdit.currentFont().pointSize() >= font_before

        window.setupAutosave()
        window.current_notes_file = str(notes_dir / "autosave.html")
        window.autosave()
        assert Path(window.current_notes_file).exists()

        original_open = open
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
        window.autosave()
        assert errors[-1] == ("Autosave Failed", "Failed to autosave file: write failed")
        monkeypatch.setattr("builtins.open", original_open)

        opened = notes_dir / "opened.txt"
        opened.write_text("alpha\nbeta")
        monkeypatch.setattr(
            ai_notes_pop_up_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(opened), "txt"),
        )
        window.openFile()
        assert "alpha" in window.textEdit.toHtml()

        monkeypatch.setattr(
            ai_notes_pop_up_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: ("", ""),
        )
        window.openFile()

        monkeypatch.setattr(
            ai_notes_pop_up_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(opened), "txt"),
        )
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        window.openFile()
        assert errors[-1] == ("Error", "An error occurred while opening the file: read failed")
        monkeypatch.setattr("builtins.open", original_open)

        window.textEdit.setPlainText("heading")
        select_document(window.textEdit)
        window.makeBold()
        window.makeItalic()
        window.makeUnderline()
        monkeypatch.setattr(
            ai_notes_pop_up_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor("yellow"),
        )
        window.changeColor()
        window.highlightText()
        window.makeList(ai_notes_pop_up_window.QTextListFormat.Style.ListDecimal)
        window.insertBulletList()
        window.formatText("heading")
        window.applyHeadingStyle(window.textEdit.textCursor())

        empty_cursor = window.textEdit.textCursor()
        empty_cursor.clearSelection()
        window.textEdit.setTextCursor(empty_cursor)
        window.makeBold()

        monkeypatch.setattr(
            ai_notes_pop_up_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor(),
        )
        window.changeColor()
        window.highlightText()
    finally:
        window.close()


def test_ai_notes_additional_branch_paths(qapp, tmp_path, monkeypatch):
    notes, _manager, command_input, _search_window, _notes_file, _bookmarks_path = (
        build_notes_widget(tmp_path, monkeypatch)
    )
    logged_errors = []
    monkeypatch.setattr(ai_notes_pop_up_window.logger, "error", logged_errors.append)

    def raise_error(message):
        return lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(message))

    try:
        notes.completer = SimpleNamespace(completionPrefix=lambda: "alph")
        notes.setPlainText("alph")
        cursor = notes.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        notes.setTextCursor(cursor)
        notes.insertCompletion("alpha")
        assert notes.toPlainText() == "alpha"

        original_open = open
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("save failed")),
        )
        notes.save_notes_if_changed()
        monkeypatch.setattr("builtins.open", original_open)

        monkeypatch.setattr(
            notes,
            "show_context_menu",
            raise_error("menu failed"),
        )
        notes.setPlainText("selected text")
        select_document(notes)
        notes.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(1, 1),
                QPointF(1, 1),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        empty_cursor = notes.textCursor()
        empty_cursor.clearSelection()
        notes.setTextCursor(empty_cursor)
        previous_commands = list(command_input.commands)
        notes.edit_and_run()
        notes.ask_assistant()
        assert command_input.commands == previous_commands

        bad_cursor_notes = SimpleNamespace(textCursor=raise_error("bad cursor"))
        ai_notes_pop_up_window.AiNotes.set_number_list(bad_cursor_notes)
        ai_notes_pop_up_window.AiNotes.set_heading(bad_cursor_notes)
        ai_notes_pop_up_window.AiNotes.set_italic(bad_cursor_notes)
        ai_notes_pop_up_window.AiNotes.set_bullet_list(bad_cursor_notes)

        monkeypatch.setattr(
            ai_notes_pop_up_window.QColorDialog,
            "getColor",
            raise_error("bad color"),
        )
        ai_notes_pop_up_window.AiNotes.set_color(SimpleNamespace(textCursor=lambda: None))

        bad_bold_notes = SimpleNamespace(
            fontWeight=raise_error("bad bold"),
            setFontWeight=lambda value: None,
        )
        ai_notes_pop_up_window.AiNotes.set_bold(bad_bold_notes)

        bad_underline_notes = SimpleNamespace(
            fontUnderline=raise_error("bad underline"),
            setFontUnderline=lambda value: None,
        )
        ai_notes_pop_up_window.AiNotes.set_underline(bad_underline_notes)

        copy_notes = SimpleNamespace(
            textCursor=lambda: SimpleNamespace(
                selectedText=raise_error("copy failed"),
            )
        )
        ai_notes_pop_up_window.AiNotes.copy_selected_text(copy_notes)

        append_notes = SimpleNamespace(insertHtml=raise_error("append failed"))
        ai_notes_pop_up_window.AiNotes.append_text(append_notes, "more")

        notes.bookmarks = [
            {"name": "one", "position": 3},
            {"name": "two", "position": 8},
        ]
        cursor = notes.textCursor()
        cursor.setPosition(0)
        notes.setTextCursor(cursor)
        notes.goto_next_bookmark()
        assert notes.textCursor().position() == 3
        cursor = notes.textCursor()
        cursor.setPosition(10)
        notes.setTextCursor(cursor)
        notes.goto_prev_bookmark()
        assert notes.textCursor().position() == 8

        assert any("save failed" in message for message in logged_errors)
        assert any("menu failed" in message for message in logged_errors)
        assert any("append failed" in message for message in logged_errors)
    finally:
        notes.close()


def test_ai_notes_popup_window_list_branches(qapp, tmp_path, monkeypatch):
    patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ai_notes_pop_up_window, "AiNotes", FakePopupAiNotes)
    manager = make_manager(tmp_path)
    notes_dir = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"])
    current_notes = notes_dir / "popup.html"
    current_notes.write_text("<p>popup</p>")
    (notes_dir / "ai_notes_bookmarks.bookmarks").write_text(
        json.dumps([{"name": "alpha", "position": 1}])
    )

    window = ai_notes_pop_up_window.AiNotesPopupWindow(
        str(current_notes),
        manager,
        FakeCommandInputArea(),
    )
    window.show()
    qapp.processEvents()

    try:
        class FakeListFormat:
            def __init__(self):
                self.styles = []
                self.indent_value = None

            def setStyle(self, style):
                self.styles.append(style)

            def setIndent(self, value):
                self.indent_value = value

        class FakeBlockFormat:
            def __init__(self):
                self._indent = 1

            def indent(self):
                return self._indent

            def setIndent(self, value):
                self._indent = value

        class FakeCurrentList:
            def __init__(self):
                self.list_format = FakeListFormat()
                self.applied = []

            def format(self):
                return self.list_format

            def setFormat(self, fmt):
                self.applied.append(list(fmt.styles))

        class FakeCursorWithList:
            def __init__(self):
                self.current_list = FakeCurrentList()
                self.created = []

            def beginEditBlock(self):
                pass

            def endEditBlock(self):
                pass

            def blockFormat(self):
                return FakeBlockFormat()

            def currentList(self):
                return self.current_list

            def setBlockFormat(self, block_format):
                self.block_format = block_format

            def createList(self, list_format):
                self.created.append(list(list_format.styles))

        class FakeCursorWithoutList:
            def __init__(self):
                self.created = []

            def currentList(self):
                return None

            def createList(self, list_format):
                self.created.append(list_format.style())

        list_cursor = FakeCursorWithList()
        monkeypatch.setattr(window.textEdit, "textCursor", lambda: list_cursor)
        window.makeList(ai_notes_pop_up_window.QTextListFormat.Style.ListDecimal)
        assert list_cursor.created == [
            [ai_notes_pop_up_window.QTextListFormat.Style.ListDecimal]
        ]

        bullet_cursor = FakeCursorWithoutList()
        monkeypatch.setattr(window.textEdit, "textCursor", lambda: bullet_cursor)
        window.insertBulletList()
        assert bullet_cursor.created == [
            ai_notes_pop_up_window.QTextListFormat.Style.ListDisc
        ]
    finally:
        window.close()
