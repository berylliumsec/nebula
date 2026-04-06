from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QTextCursor
from PyQt6.QtWidgets import QListWidgetItem, QTextEdit, QWidget

from nebula import suggestions_pop_out_window, user_note_taking


class FakeSearchReplaceDialog:
    instances = []

    def __init__(self, text_edit, parent):
        self.text_edit = text_edit
        self.parent = parent
        self.shown = False
        type(self).instances.append(self)

    def show(self):
        self.shown = True


class FakeTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)


class FakeAiNotes(QTextEdit):
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
        self.bookmarks = [{"name": "alpha", "position": 1}]
        self.bookmark_changed_callback = None
        self.removed_positions = []
        self.goto_positions = []

    def set_bookmark_changed_callback(self, callback):
        self.bookmark_changed_callback = callback

    def toggle_bookmark(self):
        position = len(self.bookmarks) + 1
        self.bookmarks.append({"name": f"mark-{position}", "position": position})
        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def removeBookmark(self, position):
        self.removed_positions.append(position)
        self.bookmarks = [bookmark for bookmark in self.bookmarks if bookmark["position"] != position]
        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def gotoBookmark(self, position):
        self.goto_positions.append(position)


def patch_note_window_dependencies(module, monkeypatch, tmp_path):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; }")
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"")

    monkeypatch.setattr(
        module,
        "return_path",
        lambda path: str(stylesheet if path.endswith(".css") else icon),
    )
    monkeypatch.setattr(module, "AiNotes", FakeAiNotes)
    monkeypatch.setattr(module, "CustomTitleBar", FakeTitleBar)
    monkeypatch.setattr(module, "SearchReplaceDialog", FakeSearchReplaceDialog)
    monkeypatch.setattr(
        module.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )


def build_manager(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(exist_ok=True)
    return SimpleNamespace(load_config=lambda: {"SUGGESTIONS_NOTES_DIRECTORY": str(notes_dir)})


def test_suggestions_pop_out_window_workflow(qapp, tmp_path, monkeypatch):
    patch_note_window_dependencies(suggestions_pop_out_window, monkeypatch, tmp_path)
    manager = build_manager(tmp_path)
    autosave_path = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"]) / "suggestions.html"
    autosave_path.write_text("<p>saved</p>")

    infos = []
    errors = []
    monkeypatch.setattr(
        suggestions_pop_out_window.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    window = suggestions_pop_out_window.SuggestionsPopOutWindow(
        manager=manager,
        command_input_area=SimpleNamespace(),
    )
    window.show()
    qapp.processEvents()

    try:
        assert window.windowTitle() == "AI Suggestions"
        assert "saved" in window.textEdit.toHtml()

        window.update_suggestions("one <two>")
        assert "one &lt;two&gt;" in window.textEdit.toHtml()

        window.setTextInTextEdit("plain")
        assert window.textEdit.toPlainText() == "plain"
        monkeypatch.setattr(
            window.textEdit,
            "setText",
            lambda text: (_ for _ in ()).throw(RuntimeError("set failed")),
        )
        window.setTextInTextEdit("broken")

        window.toggleBookmarksDock()
        assert window.bookmarksDock.isHidden() is False
        assert window.bookmarksListWidget.count() >= 1
        window.gotoBookmarkFromList(window.bookmarksListWidget.item(0))
        assert window.textEdit.goto_positions == [1]
        window.add_bookmark()
        assert window.bookmarksListWidget.count() >= 1
        window.bookmarksListWidget.setCurrentRow(0)
        window.removeBookmark()
        assert window.textEdit.removed_positions == [1]

        window.textEdit.setPlainText("alpha beta")
        cursor = window.textEdit.textCursor()
        cursor.setPosition(0)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfWord, QTextCursor.MoveMode.KeepAnchor)
        window.textEdit.setTextCursor(cursor)
        window.makeBold()
        window.makeItalic()
        window.makeUnderline()
        window.makeList(suggestions_pop_out_window.QTextListFormat.Style.ListDecimal)
        window.insertBulletList()

        monkeypatch.setattr(
            suggestions_pop_out_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor("red"),
        )
        window.changeColor()
        window.highlightText()

        monkeypatch.setattr(
            suggestions_pop_out_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor(),
        )
        window.changeColor()
        window.highlightText()

        window.on_search_replace_triggered()
        assert FakeSearchReplaceDialog.instances[-1].shown is True

        current_font_size = window.textEdit.currentFont().pointSize()
        window.adjustFontSize(2)
        assert window.textEdit.currentFont().pointSize() >= current_font_size

        window.current_notes_file = str(tmp_path / "autosave.html")
        window.autosave()
        assert Path(window.current_notes_file).exists()

        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
        window.autosave()
        assert errors[-1] == ("Autosave Failed", "Failed to autosave file: write failed")

    finally:
        window.close()


def test_suggestions_pop_out_window_open_and_formatting_branches(
    qapp,
    tmp_path,
    monkeypatch,
):
    patch_note_window_dependencies(suggestions_pop_out_window, monkeypatch, tmp_path)
    manager = build_manager(tmp_path)
    input_file = tmp_path / "input.txt"
    input_file.write_text("alpha\nbeta")
    errors = []
    monkeypatch.setattr(
        suggestions_pop_out_window.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    window = suggestions_pop_out_window.SuggestionsPopOutWindow(
        manager=manager,
        command_input_area=SimpleNamespace(),
    )
    window.show()
    qapp.processEvents()

    try:
        original_open = open
        monkeypatch.setattr(
            suggestions_pop_out_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(input_file), "txt"),
        )
        window.openFile()
        assert "alpha<br />beta" in window.textEdit.toHtml()

        monkeypatch.setattr(
            suggestions_pop_out_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: ("", ""),
        )
        window.openFile()

        monkeypatch.setattr(
            suggestions_pop_out_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(input_file), "txt"),
        )
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        window.openFile()
        assert errors[-1] == ("Error", "An error occurred while opening the file: read failed")
        monkeypatch.setattr("builtins.open", original_open)

        window.textEdit.setPlainText("heading text")
        cursor = window.textEdit.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        window.textEdit.setTextCursor(cursor)
        window.formatText("heading")
        window.applyHeadingStyle(window.textEdit.textCursor())

        window.setupAutosave()
        assert window.autosaveTimer.isActive() is True
        window.autosaveTimer.stop()

        window.autosave_path = str(tmp_path / "missing.html")
        window.load_content()

        existing = tmp_path / "existing.html"
        existing.write_text("<p>loaded</p>")
        window.autosave_path = str(existing)
        window.load_content()
        assert "loaded" in window.textEdit.toHtml()
    finally:
        window.close()


def test_user_note_taking_workflow(qapp, tmp_path, monkeypatch):
    patch_note_window_dependencies(user_note_taking, monkeypatch, tmp_path)
    manager = build_manager(tmp_path)
    notes_dir = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"])
    (notes_dir / "1.html").write_text("<p>existing</p>")

    infos = []
    errors = []
    monkeypatch.setattr(
        user_note_taking.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    window = user_note_taking.UserNoteTaking(
        manager=manager,
        command_input_area=SimpleNamespace(),
    )
    window.show()
    qapp.processEvents()

    try:
        assert window.windowTitle() == "Notes"
        assert window.tabWidget.count() == 1
        assert "existing" in window.getCurrentTextEdit().toHtml()

        old_file = notes_dir / "1.html"
        window.renameTabFile("1", "renamed")
        assert (notes_dir / "renamed.html").exists()
        assert window.tabFilePaths[0] == "renamed.html"

        window.addTab(initialContent="<p>new</p>")
        assert window.tabWidget.count() == 2
        window.tabWidget.setCurrentIndex(1)
        window.closeCurrentTab()
        assert window.tabWidget.count() == 1

        current = window.getCurrentTextEdit()
        current.bookmarks = [{"name": "alpha", "position": 1}]
        window.toggleBookmarksDock()
        window.updateBookmarksList()
        assert window.bookmarksListWidget.count() == 1
        window.add_bookmark()
        assert len(current.bookmarks) >= 1
        window.gotoBookmarkFromList(window.bookmarksListWidget.item(0))
        assert current.goto_positions == [1]
        window.bookmarksListWidget.setCurrentRow(0)
        window.removeBookmark()
        assert current.removed_positions == [1]

        current.setPlainText("alpha beta")
        cursor = current.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        current.setTextCursor(cursor)
        window.on_search_replace_triggered()
        assert FakeSearchReplaceDialog.instances[-1].shown is True

        size_before = current.currentFont().pointSize()
        window.adjustFontSize(2)
        assert current.currentFont().pointSize() >= size_before
        window.setupAutosave()
        assert window.autosaveTimer.isActive() is True
        window.autosaveTimer.stop()

        monkeypatch.setattr(
            user_note_taking.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(notes_dir / "saved.html"), "html"),
        )
        window.saveFile()
        assert (notes_dir / "renamed.html").exists()

        monkeypatch.setattr(
            user_note_taking.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(notes_dir / "open.html"), "html"),
        )
        (notes_dir / "open.html").write_text("<p>open</p>")
        window.openFile()
        assert "open" in current.toHtml()

        window.insertBulletList()
        monkeypatch.setattr(
            user_note_taking.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor("yellow"),
        )
        window.highlightText()
        window.formatText("heading")
        window.applyHeadingStyle(current.textCursor())
        window.undoText()
        window.redoText()
    finally:
        window.close()


def test_user_note_taking_branch_paths(qapp, tmp_path, monkeypatch):
    patch_note_window_dependencies(user_note_taking, monkeypatch, tmp_path)
    manager = build_manager(tmp_path)
    notes_dir = Path(manager.load_config()["SUGGESTIONS_NOTES_DIRECTORY"])
    errors = []
    warnings = []
    logged_errors = []
    monkeypatch.setattr(
        user_note_taking.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )
    monkeypatch.setattr(
        user_note_taking.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(user_note_taking.logger, "error", logged_errors.append)

    window = user_note_taking.UserNoteTaking(
        manager=manager,
        command_input_area=SimpleNamespace(),
    )
    window.show()
    qapp.processEvents()

    try:
        bar = window.tabWidget.customTabBar
        shown = []
        real_rename_tab = bar.renameTab
        real_show_context_menu = bar.showContextMenu

        class FakeMenu:
            next_action_index = 0

            def __init__(self):
                self.actions = []

            def addAction(self, action):
                self.actions.append(action)

            def exec(self, _position):
                return self.actions[self.next_action_index]

        monkeypatch.setattr(user_note_taking, "QMenu", FakeMenu)
        monkeypatch.setattr(
            user_note_taking.QTabBar,
            "mousePressEvent",
            lambda self, event: shown.append(("left", event.button())),
        )
        monkeypatch.setattr(bar, "tabAt", lambda point: 0)
        monkeypatch.setattr(
            bar,
            "showContextMenu",
            lambda index, position: shown.append((index, position)),
        )

        right_click_event = SimpleNamespace(
            button=lambda: Qt.MouseButton.RightButton,
            position=lambda: SimpleNamespace(toPoint=lambda: QPoint(1, 1)),
            globalPosition=lambda: SimpleNamespace(toPoint=lambda: QPoint(2, 2)),
        )
        left_click_event = SimpleNamespace(
            button=lambda: Qt.MouseButton.LeftButton,
            position=lambda: SimpleNamespace(toPoint=lambda: QPoint(3, 3)),
            globalPosition=lambda: SimpleNamespace(toPoint=lambda: QPoint(4, 4)),
        )
        bar.mousePressEvent(right_click_event)
        bar.mousePressEvent(left_click_event)
        assert shown[0] == (0, QPoint(2, 2))
        assert shown[1] == ("left", Qt.MouseButton.LeftButton)

        monkeypatch.setattr(bar, "showContextMenu", real_show_context_menu)
        rename_hits = []
        close_hits = []
        monkeypatch.setattr(bar, "renameTab", lambda index: rename_hits.append(index))
        bar.close_tab.connect(close_hits.append)
        FakeMenu.next_action_index = 0
        bar.showContextMenu(0, QPoint(5, 5))
        FakeMenu.next_action_index = 1
        bar.showContextMenu(0, QPoint(6, 6))
        assert rename_hits == [0]
        assert close_hits == [0]

        monkeypatch.setattr(bar, "renameTab", real_rename_tab)
        renamed_tabs = []
        set_tab_text_calls = []
        bar.tab_renamed.connect(lambda old, new: renamed_tabs.append((old, new)))
        monkeypatch.setattr(
            bar.parent(),
            "setTabText",
            lambda index, text: set_tab_text_calls.append((index, text)),
        )
        monkeypatch.setattr(
            user_note_taking.QInputDialog,
            "getText",
            lambda *args, **kwargs: ("renamed-tab", True),
        )
        bar.renameTab(0)
        assert set_tab_text_calls == [(0, "renamed-tab")]
        assert renamed_tabs[-1][1] == "renamed-tab"

        window.existingTabNumbers = {1}
        window.addTab()
        window.tabWidget.setCurrentIndex(window.tabWidget.count() - 1)
        assert "Welcome! Start taking notes here." in window.getCurrentTextEdit().toHtml()

        action = user_note_taking.QAction("Run", window)
        hit = []
        window.change_icon_temporarily(action, str(notes_dir / "temp.png"), str(notes_dir / "orig.png"))
        window.provide_feedback_and_execute(
            action,
            str(notes_dir / "temp.png"),
            str(notes_dir / "orig.png"),
            lambda: hit.append(True),
        )
        assert hit == [True]

        current_index = window.tabWidget.currentIndex()
        current_edit = window.getCurrentTextEdit()
        current_edit.setHtml("<p>save me</p>")
        window.tabFilePaths.pop(current_index, None)

        monkeypatch.setattr(
            user_note_taking.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: ("", ""),
        )
        window.saveFile()
        assert current_index not in window.tabFilePaths

        original_open = open
        monkeypatch.setattr(
            user_note_taking.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(notes_dir / "3.html"), "html"),
        )
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("save failed")),
        )
        window.saveFile()
        assert errors[-1] == ("Error", "An error occurred while saving the file: save failed")
        monkeypatch.setattr("builtins.open", original_open)

        window.tabFilePaths = {0: "autosave.html"}
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("autosave failed")),
        )
        window.autosave()
        assert errors[-1] == ("Autosave Failed", "Failed to autosave file: autosave failed")
        monkeypatch.setattr("builtins.open", original_open)

        class WriteFailingFile:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, content):
                raise OSError("write failed")

        monkeypatch.setattr("builtins.open", lambda *args, **kwargs: WriteFailingFile())
        window.autosave()
        assert errors[-1] == ("Autosave Failed", "Failed to autosave file: write failed")
        monkeypatch.setattr("builtins.open", original_open)

        monkeypatch.setattr(
            window.tabWidget,
            "count",
            lambda: (_ for _ in ()).throw(RuntimeError("count failed")),
        )
        window.autosave()
        assert any("count failed" in message for message in logged_errors)

        numeric_file = notes_dir / "2.html"
        numeric_file.write_text("<p>numeric</p>")
        monkeypatch.setattr(
            user_note_taking.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(numeric_file), "html"),
        )
        monkeypatch.setattr(window.tabWidget, "currentWidget", lambda: current_edit)
        monkeypatch.setattr(window.tabWidget, "currentIndex", lambda: 0)
        window.openFile()
        assert 2 in window.existingTabNumbers

        monkeypatch.setattr(window.tabWidget, "currentWidget", lambda: None)
        window.openFile()
        assert errors[-1] == ("Error", "No active text area to open the file in.")

        monkeypatch.setattr(window.tabWidget, "currentWidget", lambda: current_edit)
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        window.openFile()
        assert errors[-1] == ("Error", "An error occurred while opening the file: read failed")
        monkeypatch.setattr("builtins.open", original_open)

        class FakeListFormat:
            def __init__(self):
                self.styles = []

            def setStyle(self, style):
                self.styles.append(style)

        class FakeList:
            def __init__(self):
                self._format = FakeListFormat()
                self.applied = []

            def format(self):
                return self._format

            def setFormat(self, fmt):
                self.applied.append(list(fmt.styles))

        class FakeCursor:
            def __init__(self):
                self.current_list = FakeList()

            def currentList(self):
                return self.current_list

            def createList(self, list_format):
                self.created = list_format

        fake_cursor = FakeCursor()
        monkeypatch.setattr(current_edit, "textCursor", lambda: fake_cursor)
        window.insertBulletList()
        assert fake_cursor.current_list.applied == [
            [user_note_taking.QTextListFormat.Style.ListDisc]
        ]

        selected_cursor = SimpleNamespace(hasSelection=lambda: True)
        monkeypatch.setattr(
            window.tabWidget,
            "currentWidget",
            lambda: SimpleNamespace(textCursor=lambda: selected_cursor),
        )
        formatted = []
        monkeypatch.setattr(window, "applyHeadingStyle", formatted.append)
        window.formatText("heading")
        assert formatted == [selected_cursor]
    finally:
        window.close()


def test_suggestions_pop_out_window_branch_paths(qapp, tmp_path, monkeypatch):
    patch_note_window_dependencies(suggestions_pop_out_window, monkeypatch, tmp_path)
    manager = build_manager(tmp_path)
    errors = []
    monkeypatch.setattr(suggestions_pop_out_window.logger, "error", errors.append)

    window = suggestions_pop_out_window.SuggestionsPopOutWindow(
        manager=manager,
        command_input_area=SimpleNamespace(),
    )
    window.show()
    qapp.processEvents()

    try:
        action = suggestions_pop_out_window.QAction("Run", window)
        hit = []
        window.change_icon_temporarily(action, str(tmp_path / "temp.png"), str(tmp_path / "orig.png"))
        window.provide_feedback_and_execute(
            action,
            str(tmp_path / "temp.png"),
            str(tmp_path / "orig.png"),
            lambda: hit.append(True),
        )
        assert hit == [True]

        cursor = window.textEdit.textCursor()
        cursor.clearSelection()
        window.textEdit.setTextCursor(cursor)
        window.makeBold()

        class FakeListFormat:
            def __init__(self):
                self.styles = []

            def setStyle(self, style):
                self.styles.append(style)

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
                return SimpleNamespace(indent=lambda: 1, setIndent=lambda value: None)

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
        window.makeList(suggestions_pop_out_window.QTextListFormat.Style.ListDecimal)
        assert list_cursor.created == [
            [suggestions_pop_out_window.QTextListFormat.Style.ListDecimal]
        ]

        bullet_cursor = FakeCursorWithoutList()
        monkeypatch.setattr(window.textEdit, "textCursor", lambda: bullet_cursor)
        window.insertBulletList()
        assert bullet_cursor.created == [
            suggestions_pop_out_window.QTextListFormat.Style.ListDisc
        ]

        existing = tmp_path / "existing.html"
        existing.write_text("<p>saved</p>")
        window.autosave_path = str(existing)
        original_open = open
        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        window.load_content()
        monkeypatch.setattr("builtins.open", original_open)

        monkeypatch.setattr(window.textEdit, "setHtml", lambda html: (_ for _ in ()).throw(RuntimeError("set failed")))
        window.load_content()

        assert any("read failed" in message for message in errors)
        assert any("set failed" in message for message in errors)
    finally:
        window.close()
