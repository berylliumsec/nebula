import re

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QPushButton, QTextEdit

from . import constants, utilities
from .log_config import setup_logging
from .run_python import execute_script_in_thread

logger = setup_logging(
    log_file=constants.SYSTEM_LOGS_DIR + "/sensitive_information_removal.log"
)

CUSTOM_PROMPT_PATTERN = (
    r"^(?:nebula[ ~/.\w-]*[%$#]*\s*|"
    r"[\w-]+@[\w-]+:[~\w/.\-]+[%$#]\s*|"
    r"[\w-]+:[~\w/.\-]+[%$#]\s*|"
    r"\$\s*|>\s*|#\s*|\d{2}:\d{2}:\d{2} [\w.-]+@[\w.-]+ \w+ [±|][\w_ ]+ [✗✔︎]?[\|→]\s*)"
)


class CentralDisplayAreaInMainWindow(QTextEdit):
    suggestions_signal_from_central_display_area = pyqtSignal(str, str)
    notes_signal_from_central_display_area = pyqtSignal(str, str)
    model_creation_in_progress = pyqtSignal(bool)

    def __init__(self, parent=None, manager=None, command_input_area=None):
        super().__init__(parent)

        self.setMouseTracking(True)
        self.copyButton = QPushButton("Copy", self)
        font = QFont()
        font.setPointSize(10)
        self.adjustButtonSize()
        self.copyButton.setFont(font)
        self.copyButton.clicked.connect(self.copyText)
        self.copyButton.hide()
        self.copyButtonTimer = QTimer(self)
        self.copyButtonTimer.timeout.connect(self.resetCopyButtonText)
        self.copyButtonTimer.setSingleShot(True)
        self.command_input_area = command_input_area
        self.highlightColor = QColor("#007ACC")
        self.incognito_mode = False
        self.isHovering = False
        self.isSelectingText = False
        self.lastHoverPos = None
        self.free_mode = False
        self.setLineWrapMode(self.LineWrapMode.WidgetWidth)
        self.manager = manager
        try:
            self.CONFIG = self.manager.load_config()
        except Exception as e:
            logger.debug({e})

        # Create all actions and connect their signals
        self.copy_action = QAction("Copy", self)
        self.copy_action.triggered.connect(self.copy)

        self.ask_assistant_action = QAction("Ask Terminal Assistant", self)
        self.ask_assistant_action.triggered.connect(self.ask_assistant)

        self.edit_and_run_action = QAction("Edit and Run", self)
        self.edit_and_run_action.triggered.connect(self.edit_and_run)

        self.edit_and_run_python_action = QAction("Run Python", self)
        self.edit_and_run_python_action.triggered.connect(self.edit_and_run_python)

        self.exclude_action = QAction("Exclude", self)
        self.exclude_action.triggered.connect(self.excludeWord)

        self.send_to_ai_notes_action = QAction("Send to AI Notes", self)
        self.send_to_ai_notes_action.triggered.connect(self.send_to_ai_notes)

        self.send_to_ai_suggestions_action = QAction("Send to AI Suggestions", self)
        self.send_to_ai_suggestions_action.triggered.connect(
            self.send_to_ai_suggestions
        )

        # Connect any signals that affect the enabled/disabled state
        self.model_creation_in_progress.connect(
            self.enable_or_disable_due_to_model_creation
        )

    def set_font_size_for_copy_button(self, size):
        font = QFont()
        font.setPointSize(size)
        self.adjustButtonSize()
        self.copyButton.setFont(font)

    def mouseMoveEvent(self, event: QMouseEvent):
        try:
            super().mouseMoveEvent(event)
            self.lastHoverPos = event.pos()
            if not self.isSelectingText:
                self.isHovering = True
                self.clearLineHighlight()
                self.highlightLineUnderCursor(event.pos())
                self.positionCopyButton(event.pos())
        except Exception as e:
            logger.error(f"Error in mouseMoveEvent: {e}")

    def mousePressEvent(self, event: QMouseEvent):
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self.isSelectingText = True
                self.clearLineHighlight()
            super().mousePressEvent(event)
        except Exception as e:
            logger.error(f"Error in mousePressEvent: {e}")

    def mouseReleaseEvent(self, event: QMouseEvent):
        try:
            super().mouseReleaseEvent(event)
            if self.isSelectingText and self.textCursor().hasSelection():
                self.showContextMenu(event.globalPosition().toPoint())
            self.isSelectingText = False
            self.isHovering = False
        except Exception as e:
            logger.error(f"Error in mouseReleaseEvent: {e}")

    def highlightLineUnderCursor(self, position):
        try:
            if self.isHovering and not self.isSelectingText:
                cursor = self.cursorForPosition(position)
                self.clearLineHighlight()
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                format = QTextCharFormat()
                format.setBackground(self.highlightColor)
                cursor.mergeCharFormat(format)
        except Exception as e:
            logger.error(f"Error in highlightLineUnderCursor: {e}")

    def positionCopyButton(self, position):
        try:
            rect = self.cursorRect(self.cursorForPosition(position))
            self.copyButton.move(
                self.viewport().width() - self.copyButton.width(), rect.top()
            )
            self.copyButton.show()
        except Exception as e:
            logger.error(f"Error in positionCopyButton: {e}")

    def clearLineHighlight(self, _=None):
        try:
            cursor = QTextCursor(self.document())
            cursor.select(QTextCursor.SelectionType.Document)
            format = QTextCharFormat()
            format.setBackground(QColor(0, 0, 0, 0))
            cursor.setCharFormat(format)
        except Exception as e:
            logger.error(f"Error in clearLineHighlight: {e}")

    def leaveEvent(self, event: QMouseEvent):
        try:
            self.copyButton.hide()
            self.isHovering = False
            self.clearLineHighlight()
            super().leaveEvent(event)
        except Exception as e:
            logger.error(f"Error in leaveEvent: {e}")

    def createContextMenu(self, _=None):
        try:
            context_menu = QMenu(self)
            context_menu.aboutToShow.connect(self.prepareContextMenu)
            context_menu.setStyleSheet(
                """
                QMenu::item:selected {
                    background-color:#1e1e1e; 
                }
                """
            )
            # Add the pre-created actions to the context menu
            context_menu.addAction(self.copy_action)
            context_menu.addAction(self.ask_assistant_action)
            context_menu.addAction(self.edit_and_run_action)
            context_menu.addAction(self.edit_and_run_python_action)
            context_menu.addAction(self.exclude_action)
            context_menu.addAction(self.send_to_ai_notes_action)
            context_menu.addAction(self.send_to_ai_suggestions_action)
            return context_menu
        except Exception as e:
            logger.error(f"Error in createContextMenu: {e}")
            return QMenu(self)

    def enable_or_disable_due_to_model_creation(self, signal):
        if signal:
            logger.debug("Disabling send to ai and send to suggestions")
            self.send_to_ai_notes_action.setEnabled(False)
            self.send_to_ai_suggestions_action.setEnabled(False)
        else:
            logger.debug(
                "Enabling send to ai and send to suggestions while model is being created"
            )
            self.send_to_ai_notes_action.setEnabled(True)
            self.send_to_ai_suggestions_action.setEnabled(True)

    def edit_and_run(self):
        cursor = self.textCursor()
        selected_text = cursor.selectedText()
        if not selected_text:
            return
        else:
            dialog = utilities.EditCommandDialog(command_text=selected_text)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                edited_command = dialog.get_command()
                self.command_input_area.execute_command(edited_command)

    def ask_assistant(self):
        cursor = self.textCursor()
        selected_text = cursor.selectedText()
        if not selected_text:
            return
        else:
            dialog = utilities.EditCommandDialog(selected_text, command_input_area=True)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                edited_command = dialog.get_command()
                self.command_input_area.execute_command(edited_command)

    def excludeWord(self, _=None):
        cursor = self.textCursor()
        selected_text = cursor.selectedText()
        if selected_text.strip():
            self.CONFIG = self.manager.load_config()
            with open(self.CONFIG["PRIVACY_FILE"], "a") as file:
                file.write(selected_text + "\n")

    def contextMenuEvent(self, event):
        try:
            context_menu = self.createContextMenu()
            context_menu.exec(event.globalPos())
        except Exception as e:
            logger.error(f"Error in contextMenuEvent: {e}")

    def showContextMenu(self, position):
        try:
            context_menu = self.createContextMenu()
            context_menu.exec(position)
        except Exception as e:
            logger.error(f"Error in showContextMenu: {e}")

    def prepareContextMenu(self):
        if self.free_mode:
            logger.debug("free mode activated, disabling actions")
            self.send_to_ai_notes_action.setEnabled(False)
            self.send_to_ai_suggestions_action.setEnabled(False)
        else:
            self.send_to_ai_notes_action.setEnabled(True)
            self.send_to_ai_suggestions_action.setEnabled(True)

    def send_to_ai_notes(self, _=None):
        try:
            if self.lastHoverPos:
                cursor = self.cursorForPosition(self.lastHoverPos)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                selectedText = cursor.selectedText()
                if self.incognito_mode:
                    self.CONFIG = self.manager.load_config()
                    selectedText = utilities.run_hooks(
                        selectedText, self.CONFIG["PRIVACY_FILE"]
                    )
                self.notes_signal_from_central_display_area.emit(selectedText, "notes")
        except Exception as e:
            logger.error(f"Error in send_to_ai_notes: {e}")

    def send_to_ai_suggestions(self, _=None):
        try:
            if self.lastHoverPos:
                cursor = self.cursorForPosition(self.lastHoverPos)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                selectedText = cursor.selectedText()
                if self.incognito_mode:
                    self.CONFIG = self.manager.load_config()
                    selectedText = utilities.run_hooks(
                        selectedText, self.CONFIG["PRIVACY_FILE"]
                    )

                self.suggestions_signal_from_central_display_area.emit(
                    selectedText, "suggestion"
                )
        except Exception as e:
            logger.error(f"Error in send_to_ai_suggestions: {e}")

    def adjustButtonSize(self, _=None):
        try:
            self.copyButton.resize(self.copyButton.sizeHint())
        except Exception as e:
            logger.error(f"Error in adjustButtonSize: {e}")

    def copyText(self, _=None):
        try:
            if self.lastHoverPos:
                cursor = self.cursorForPosition(self.lastHoverPos)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                selectedText = cursor.selectedText()
                QApplication.clipboard().setText(selectedText)
                self.copyButton.setText("Copied")
                self.adjustButtonSize()
                self.copyButtonTimer.start(2000)
            else:
                logger.debug("No text selected for copy")
        except Exception as e:
            logger.error(f"Error in copyText: {e}")

    def resetCopyButtonText(self, _=None):
        try:
            self.copyButton.setText("Copy")
            self.adjustButtonSize()
        except Exception as e:
            logger.error(f"Error in resetCopyButtonText: {e}")

    def keyPressEvent(self, event: QKeyEvent):
        if (
            event.key() == Qt.Key.Key_Return
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            cursor = self.textCursor()

            if cursor.hasSelection():
                # If there is a selection, use the selected text
                command_text = cursor.selectedText().strip()
            else:
                # Move the cursor to the start of the line
                cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)

                # Move the cursor to the end of the line while keeping the selection
                cursor.movePosition(
                    QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
                )

                # Get the entire line's text
                command_text = cursor.selectedText().strip()

            # Strip out the prompts using the custom pattern
            command_text = re.sub(CUSTOM_PROMPT_PATTERN, "", command_text).strip()

            if command_text:
                if command_text == "clear":
                    self.clear()
                    self.command_input_area.terminal.reset_terminal()
                    return
                self.command_input_area.execute_command(command_text)

                # Move the cursor to the end of the current line
                cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
                # Clear the text from the end of the current line to the end of the document
                cursor.movePosition(
                    QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor
                )
                cursor.removeSelectedText()

                self.append("")  # Move to the next line
            return
        super().keyPressEvent(event)

    def edit_and_run_python(self, _=None):
        # Use the existing text cursor, which reflects the current selection
        cursor = self.textCursor()

        # No need to check for selection; directly get the selected text
        selected_text = cursor.selection().toPlainText()
        future = execute_script_in_thread(selected_text)
        output = (
            future.result()
        )  # This will block until the script execution is complete
        self.setText(output)
