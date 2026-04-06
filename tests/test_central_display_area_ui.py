from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent, QTextCursor
from PyQt6.QtWidgets import QDialog

from nebula import central_display_area_in_main_window


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


class FakeCursor:
    def __init__(self, text):
        self._text = text
        self.selected = None

    def select(self, selection):
        self.selected = selection

    def selectedText(self):
        return self._text


class FakeFuture:
    def result(self):
        return "python output"


class FakeCommandInputArea:
    def __init__(self):
        self.commands = []
        self.terminal = SimpleNamespace(reset_terminal=self._reset)
        self.reset_calls = 0

    def _reset(self):
        self.reset_calls += 1

    def execute_command(self, command):
        self.commands.append(command)


def build_widget(qapp, tmp_path, monkeypatch):
    privacy_file = tmp_path / "privacy.txt"
    privacy_file.write_text("")
    manager = SimpleNamespace(load_config=lambda: {"PRIVACY_FILE": str(privacy_file)})
    command_input = FakeCommandInputArea()
    monkeypatch.setattr(
        central_display_area_in_main_window.utilities,
        "EditCommandDialog",
        FakeEditDialog,
    )
    widget = central_display_area_in_main_window.CentralDisplayAreaInMainWindow(
        manager=manager,
        command_input_area=command_input,
    )
    widget.resize(400, 200)
    widget.show()
    qapp.processEvents()
    return widget, command_input, privacy_file


def select_document(widget):
    cursor = widget.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    widget.setTextCursor(cursor)


def test_central_display_area_context_actions(qapp, tmp_path, monkeypatch):
    widget, command_input, privacy_file = build_widget(qapp, tmp_path, monkeypatch)
    exec_positions = []
    notes = []
    suggestions = []
    monkeypatch.setattr(
        central_display_area_in_main_window.QMenu,
        "exec",
        lambda self, position: exec_positions.append(position),
    )
    widget.notes_signal_from_central_display_area.connect(lambda text, endpoint: notes.append((text, endpoint)))
    widget.suggestions_signal_from_central_display_area.connect(
        lambda text, endpoint: suggestions.append((text, endpoint))
    )

    try:
        widget.setPlainText("first line\nsecond line")
        widget.set_font_size_for_copy_button(12)
        assert widget.copyButton.font().pointSize() == 12

        widget.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert widget.isHovering is True
        assert widget.copyButton.isHidden() is False

        widget.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert widget.isSelectingText is True

        select_document(widget)
        shown = []
        original_show_context_menu = widget.showContextMenu
        monkeypatch.setattr(widget, "showContextMenu", lambda position: shown.append(position))
        widget.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert shown
        monkeypatch.setattr(widget, "showContextMenu", original_show_context_menu)

        widget.highlightLineUnderCursor(QPoint(0, 0))
        widget.positionCopyButton(QPoint(0, 0))
        widget.clearLineHighlight()
        widget.leaveEvent(QEvent(QEvent.Type.Leave))
        assert widget.copyButton.isHidden() is True

        menu = widget.createContextMenu()
        assert menu.actions()
        widget.enable_or_disable_due_to_model_creation(True)
        assert widget.send_to_ai_notes_action.isEnabled() is False
        widget.enable_or_disable_due_to_model_creation(False)
        assert widget.send_to_ai_notes_action.isEnabled() is True

        widget.setPlainText("nmap -sV")
        select_document(widget)
        FakeEditDialog.accepted = True
        widget.edit_and_run()
        widget.ask_assistant()
        assert command_input.commands[-2:] == ["edited:nmap -sV", "edited:nmap -sV"]

        widget.setPlainText("secret")
        select_document(widget)
        widget.excludeWord()
        assert "secret" in privacy_file.read_text()

        widget.contextMenuEvent(
            SimpleNamespace(globalPos=lambda: QPoint(1, 2))
        )
        widget.showContextMenu(QPoint(3, 4))
        assert exec_positions[-2:] == [QPoint(1, 2), QPoint(3, 4)]

        widget.free_mode = True
        widget.prepareContextMenu()
        assert widget.send_to_ai_notes_action.isEnabled() is False
        widget.free_mode = False
        widget.prepareContextMenu()
        assert widget.send_to_ai_notes_action.isEnabled() is True

        widget.cursorForPosition = lambda point: FakeCursor("hovered line")
        widget.lastHoverPos = QPoint(3, 3)
        widget.send_to_ai_notes()
        widget.send_to_ai_suggestions()
        assert notes[-1] == ("hovered line", "notes")
        assert suggestions[-1] == ("hovered line", "suggestion")

        monkeypatch.setattr(
            central_display_area_in_main_window.utilities,
            "run_hooks",
            lambda text, path: f"hooked:{text}",
            raising=False,
        )
        widget.incognito_mode = True
        widget.send_to_ai_notes()
        widget.send_to_ai_suggestions()
        assert notes[-1][0] == "hooked:hovered line"
        assert suggestions[-1][0] == "hooked:hovered line"

        widget.copyText()
        assert qapp.clipboard().text() == "hovered line"
        widget.lastHoverPos = None
        widget.copyText()
        widget.resetCopyButtonText()
        assert widget.copyButton.text() == "Copy"
    finally:
        widget.close()


def test_central_display_area_key_and_python_paths(qapp, tmp_path, monkeypatch):
    widget, command_input, _privacy_file = build_widget(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(
        central_display_area_in_main_window,
        "execute_script_in_thread",
        lambda text: FakeFuture(),
    )

    try:
        widget.setPlainText("clear")
        cursor = widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        widget.setTextCursor(cursor)
        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert command_input.reset_calls == 1

        widget.setPlainText("nebula $ ls -la\nnext")
        cursor = widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        widget.setTextCursor(cursor)
        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert command_input.commands[-1] == "ls -la"

        widget.setPlainText("python('hi')")
        select_document(widget)
        widget.edit_and_run_python()
        assert widget.toPlainText() == "python output"

        widget.setPlainText("selected command")
        select_document(widget)
        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert command_input.commands[-1] == "selected command"

        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_A,
                Qt.KeyboardModifier.NoModifier,
                "a",
            )
        )
    finally:
        widget.close()


def test_central_display_area_exception_paths(qapp, tmp_path, monkeypatch):
    debug_messages = []
    monkeypatch.setattr(central_display_area_in_main_window.logger, "debug", debug_messages.append)
    bad_widget = central_display_area_in_main_window.CentralDisplayAreaInMainWindow(
        manager=SimpleNamespace(
            load_config=lambda: (_ for _ in ()).throw(RuntimeError("config failed"))
        ),
        command_input_area=FakeCommandInputArea(),
    )
    bad_widget.close()
    assert debug_messages

    widget, command_input, _privacy_file = build_widget(qapp, tmp_path, monkeypatch)
    errors = []
    monkeypatch.setattr(central_display_area_in_main_window.logger, "error", errors.append)

    def raise_error(message):
        return lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(message))

    try:
        widget.setPlainText("alpha")
        widget.highlightLineUnderCursor = raise_error("hover failed")
        widget.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        monkeypatch.setattr(
            central_display_area_in_main_window.QTextEdit,
            "mousePressEvent",
            raise_error("press failed"),
        )
        widget.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        monkeypatch.setattr(
            central_display_area_in_main_window.QTextEdit,
            "mouseReleaseEvent",
            raise_error("release failed"),
        )
        widget.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

        monkeypatch.setattr(widget, "cursorForPosition", raise_error("cursor failed"))
        widget.isHovering = True
        widget.isSelectingText = False
        central_display_area_in_main_window.CentralDisplayAreaInMainWindow.highlightLineUnderCursor(
            widget, QPoint(1, 1)
        )
        central_display_area_in_main_window.CentralDisplayAreaInMainWindow.positionCopyButton(
            widget, QPoint(1, 1)
        )

        monkeypatch.setattr(widget, "document", raise_error("document failed"))
        central_display_area_in_main_window.CentralDisplayAreaInMainWindow.clearLineHighlight(
            widget
        )

        monkeypatch.setattr(widget, "clearLineHighlight", raise_error("leave failed"))
        widget.leaveEvent(QEvent(QEvent.Type.Leave))

        original_set_stylesheet = central_display_area_in_main_window.QMenu.setStyleSheet

        def failing_set_stylesheet(self, stylesheet):
            central_display_area_in_main_window.QMenu.setStyleSheet = original_set_stylesheet
            raise RuntimeError("menu style failed")

        monkeypatch.setattr(
            central_display_area_in_main_window.QMenu,
            "setStyleSheet",
            failing_set_stylesheet,
        )
        assert widget.createContextMenu() is not None

        monkeypatch.setattr(
            widget,
            "createContextMenu",
            lambda: SimpleNamespace(exec=raise_error("context failed")),
        )
        widget.contextMenuEvent(SimpleNamespace(globalPos=lambda: QPoint(2, 2)))
        widget.showContextMenu(QPoint(3, 3))

        widget.setPlainText("no selection")
        cursor = widget.textCursor()
        cursor.clearSelection()
        widget.setTextCursor(cursor)
        previous_commands = list(command_input.commands)
        widget.edit_and_run()
        widget.ask_assistant()
        assert command_input.commands == previous_commands

        widget.lastHoverPos = QPoint(1, 1)
        monkeypatch.setattr(widget, "cursorForPosition", raise_error("hover select failed"))
        widget.send_to_ai_notes()
        widget.send_to_ai_suggestions()

        monkeypatch.setattr(widget.copyButton, "resize", raise_error("resize failed"))
        widget.adjustButtonSize()

        monkeypatch.setattr(widget.copyButton, "setText", raise_error("set text failed"))
        widget.resetCopyButtonText()

        monkeypatch.setattr(
            central_display_area_in_main_window.QApplication,
            "clipboard",
            lambda: SimpleNamespace(setText=raise_error("clipboard failed")),
        )
        widget.lastHoverPos = QPoint(1, 1)
        monkeypatch.setattr(widget, "cursorForPosition", lambda point: FakeCursor("copied"))
        widget.copyText()

        assert any("hover failed" in message for message in errors)
        assert any("context failed" in message for message in errors)
        assert any("clipboard failed" in message for message in errors)
    finally:
        widget.close()
