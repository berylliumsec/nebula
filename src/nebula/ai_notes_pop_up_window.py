import json
import os
from collections import Counter

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from PyQt6 import QtCore
from PyQt6.QtCore import (QFile, QFileSystemWatcher, QObject, QRect, QRunnable,
                          QStringListModel, Qt, QThreadPool, QTimer, QUrl,
                          pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence,
                         QTextCharFormat, QTextCursor, QTextListFormat)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QCompleter, QDialog,
                             QDockWidget, QFileDialog, QHBoxLayout,
                             QInputDialog, QLabel, QListWidget, QMainWindow,
                             QMenu, QMessageBox, QTextEdit, QToolBar, QWidget)

from . import constants, update_utils, utilities
from .log_config import setup_logging
from .search_replace_dialog import SearchReplaceDialog
from .update_utils import return_path

logger = setup_logging(
    log_file=constants.SYSTEM_LOGS_DIR + "/ai_notes_pop_up_window.log"
)


class AutocompleteUpdater(QObject):
    update = pyqtSignal(list)


class CustomCompleter(QCompleter):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Make the completer case-insensitive in PyQt6
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def complete(self, rect=QRect()):
        """Position the completer popup to the right of the text cursor's position."""
        cursorRect = self.widget().cursorRect()
        # Adjust the popup's position: move it to the right of the cursor position
        offset = 15
        cursorRect.setX(cursorRect.x() + offset)
        cursorRect.setWidth(
            self.popup().sizeHintForColumn(0)
            + self.popup().verticalScrollBar().sizeHint().width()
        )
        super().complete(cursorRect)  # Use the adjusted cursorRect for positioning


class FileProcessor(QRunnable):
    def __init__(self, directory, callback):
        super().__init__()
        self.directory = directory
        self.callback = callback

    def run(self):
        word_freq = Counter()

        for filename in os.listdir(self.directory):
            path = os.path.join(self.directory, filename)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as file:
                    for line in file:
                        # Splitting the line by any sequence of whitespace characters
                        tokens = line.split()
                        word_freq.update(tokens)

        # Filter to remove very common or very short words if needed
        common_min_length = 2
        max_common = 1000  # Adjust based on your needs
        suggestions = [
            word
            for word, count in word_freq.items()
            if len(word) >= common_min_length and count <= max_common
        ]

        self.callback(suggestions)


class CustomTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayout(QHBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        title = QLabel("Bookmarks")
        title.setStyleSheet("color: white;")  # Set the title color
        self.layout().addWidget(title)


class AiNotes(QTextEdit):
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
        self.bookmarks_path = bookmarks_path
        if file_path:
            self.autosave_timer = QTimer(self)
            self.autosave_timer.timeout.connect(self.save_notes_if_changed)
            self.autosave_timer.start(1000)

        self.file_path = file_path
        self.manager = manager
        self.CONFIG = self.manager.load_config()
        self.completer = CustomCompleter(self)
        self.completer.setModel(QStringListModel(["Example", "Autocomplete", "Text"]))
        self.setCompleter(self.completer)
        self.directory = self.CONFIG["LOG_DIRECTORY"]
        self.updater = AutocompleteUpdater()
        self.updater.update.connect(self.updateCompleter)

        self.watcher = QFileSystemWatcher([self.directory])
        self.watcher.directoryChanged.connect(self.refreshAutocomplete)

        self.threadPool = QThreadPool()
        self.refreshAutocomplete()
        self.bookmarks = []
        self.command_input_area = command_input_area
        self.bookmark_changed_callback = None  # Callback attribute
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))

        self.load_notes()
        self.load_bookmarks()
        self.search_window = search_window

    def load_stylesheet(self, filename):
        style_file = QFile(filename)
        style_file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text)
        self.original_stylesheet = style_file.readAll().data().decode("utf-8")
        self.setStyleSheet(self.original_stylesheet)

    def setCompleter(self, completer):
        completer.setWidget(self)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.activated.connect(self.insertCompletion)

    def insertCompletion(self, completion):
        tc = self.textCursor()
        extra = len(completion) - len(self.completer.completionPrefix())
        tc.movePosition(tc.MoveOperation.Left)
        tc.movePosition(tc.MoveOperation.EndOfWord)
        tc.insertText(
            completion[-extra:]
        )  # insert the remaining part of the completion
        self.setTextCursor(tc)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)

        # Ignore key presses that don't modify text or are meant for the completer.
        if event.key() in (
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Escape,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        ):
            # If the popup is visible, these keys should be handled by the completer.
            if self.completer.popup().isVisible():
                event.ignore()
            return

        # Arrow keys should not hide the popup but need not trigger an update.
        if event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        ):
            if self.completer.popup().isVisible():
                return  # Let the completer handle navigating its suggestions.

        # Update the completion prefix based on the current text under the cursor.
        completionPrefix = self.textUnderCursor()
        if self.completer:
            if len(completionPrefix) >= 2:
                self.completer.setCompletionPrefix(completionPrefix)
                self.completer.popup().setCurrentIndex(
                    self.completer.completionModel().index(0, 0)
                )
                # Call complete() to ensure the popup's suggestions and position are updated.
                self.completer.complete()
            else:
                self.completer.popup().hide()

    def mouseDoubleClickEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        anchor = cursor.charFormat().anchorHref()
        if anchor:
            utilities.open_url(anchor)  # Custom URL opening function
        else:
            super().mouseDoubleClickEvent(event)

    def refreshAutocomplete(self):
        runnable = FileProcessor(self.directory, self.updater.update.emit)
        self.threadPool.start(runnable)

    def updateCompleter(self, wordList):
        self.completer.setModel(QStringListModel(wordList))

    def textUnderCursor(self):
        tc = self.textCursor()
        tc.select(tc.SelectionType.WordUnderCursor)
        return tc.selectedText()

    def focusInEvent(self, event):
        if self.completer:
            self.completer.setWidget(self)
        super().focusInEvent(event)

    def load_notes(self, _=None):
        try:
            if not os.path.exists(self.file_path):
                raise FileNotFoundError(f"File not found: {self.file_path}")

            with open(self.file_path, "r") as file:
                file_content = file.read()

                self.setHtml(file_content)
                logger.debug(f"Notes loaded from {self.file_path}")

        except Exception as e:
            logger.error(f"Error loading notes: {e}")

    def set_notes_file(self, path):
        self.file_path = path
        self.load_notes()

    def save_notes_if_changed(self, _=None):
        try:
            with open(self.file_path, "w") as file:
                file.write(self.toHtml())
        except Exception as e:
            logger.error(f"Error saving notes: {e}")

    def mouseReleaseEvent(self, event):
        try:
            super().mouseReleaseEvent(event)
            if self.textCursor().hasSelection():
                self.show_context_menu(event.pos())
        except Exception as e:
            logger.error(f"An error occurred in mouseReleaseEvent: {e}")

    def change_icon_temporarily(
        self, action, temp_icon_path, original_icon_path, delay=500
    ):
        action.setIcon(QIcon(temp_icon_path))
        self.window().repaint()
        QApplication.processEvents()  # Force the UI to update
        QTimer.singleShot(delay, lambda: action.setIcon(QIcon(original_icon_path)))

    def provide_feedback_and_execute(
        self, action, temp_icon_path, original_icon_path, function
    ):
        self.change_icon_temporarily(action, temp_icon_path, original_icon_path)
        function()

    def show_context_menu(self, position):
        context_menu = QMenu(self)
        context_menu.setStyleSheet(
            """
            QMenu::item:selected {
                background-color:#333333;
            }
        """
        )
        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(self.copy_selected_text)
        context_menu.addAction(copy_action)

        ask_assistant = QAction("Ask Terminal Assistant", self)
        ask_assistant.triggered.connect(self.ask_assistant)
        context_menu.addAction(ask_assistant)

        edit_and_run = QAction("Edit and Run", self)
        edit_and_run.triggered.connect(self.edit_and_run)
        context_menu.addAction(edit_and_run)

        index = QAction("Index", self)
        index.triggered.connect(self.index)
        context_menu.addAction(index)

        exclude_action = QAction("Exclude", self)
        exclude_action.triggered.connect(self.excludeWord)
        context_menu.addAction(exclude_action)
        bold_action = QAction("Bold", self)
        bold_action.triggered.connect(self.set_bold)
        context_menu.addAction(bold_action)

        italic_action = QAction("Italic", self)
        italic_action.triggered.connect(self.set_italic)
        context_menu.addAction(italic_action)

        underline_action = QAction("Underline", self)
        underline_action.triggered.connect(self.set_underline)
        context_menu.addAction(underline_action)

        list_action = QAction("Bullet List", self)
        list_action.triggered.connect(self.set_bullet_list)
        context_menu.addAction(list_action)

        italic_action = QAction("Italic", self)
        italic_action.triggered.connect(self.set_italic)
        context_menu.addAction(italic_action)

        heading_action = QAction("Heading", self)
        heading_action.triggered.connect(self.set_heading)
        context_menu.addAction(heading_action)

        color_action = QAction("Color", self)
        color_action.triggered.connect(self.set_color)
        context_menu.addAction(color_action)

        number_list_action = QAction("Numbered List", self)
        number_list_action.triggered.connect(self.set_number_list)
        context_menu.addAction(number_list_action)

        toggle_bookmark_action = QAction("Toggle Bookmark", self)
        toggle_bookmark_action.triggered.connect(self.toggle_bookmark)
        context_menu.addAction(toggle_bookmark_action)

        next_bookmark_action = QAction("Next Bookmark", self)
        next_bookmark_action.triggered.connect(self.goto_next_bookmark)
        context_menu.addAction(next_bookmark_action)

        prev_bookmark_action = QAction("Previous Bookmark", self)
        prev_bookmark_action.triggered.connect(self.goto_prev_bookmark)
        context_menu.addAction(prev_bookmark_action)

        remove_bookmark_action = QAction("Remove Bookmark", self)
        remove_bookmark_action.triggered.connect(self.remove_current_bookmark)
        context_menu.addAction(remove_bookmark_action)
        linkFileAction = QAction("Link File", self)
        linkFileAction.triggered.connect(self.insertFileLink)
        context_menu.addAction(linkFileAction)

        # Add Format Code action
        formatCodeAction = QAction("Format Code", self)
        formatCodeAction.triggered.connect(self.formatSelectedCode)
        context_menu.addAction(formatCodeAction)

        context_menu.exec(self.mapToGlobal(position))

        context_menu.exec(self.mapToGlobal(position))

    def index(self):
        indexdir = update_utils.return_path("command_search_index")
        selected_text = self.textCursor().selectedText()
        self.search_window.add_to_index(selected_text, indexdir)

    def handle_anchor_clicked(self, url):
        # Handle local file links specially, otherwise use QDesktopServices for http/https links
        if url.scheme() == "file":
            # The path might need adjustments depending on how it's stored
            file_path = url.toLocalFile()
            # Try opening the file with the default application
            if not utilities.open_url(url):
                logger.error(f"Failed to open file: {file_path}")
        else:
            utilities.open_url(url)

    def insertFileLink(self):
        directory = self.manager.load_config()["LOG_DIRECTORY"]  # Adjust as necessary
        filename, _ = QFileDialog.getOpenFileName(
            self, "Link File", directory, "All Files (*)"
        )
        if filename:
            file_url = QUrl.fromLocalFile(filename).toString()
            display_text = os.path.basename(filename)  # Text to display
            link_html = f'<a href="{file_url}">{display_text}</a>'
            self.insertHtml(link_html + " ")  # Insert the link and add a space after it

    def formatSelectedCode(self):
        selected_text = self.textCursor().selectedText()
        formatted_text = self.format_code(selected_text)
        self.textCursor().insertHtml(formatted_text)

    def format_code(self, code):
        try:
            lexer = guess_lexer(
                code
            )  # Automatically guess the lexer for syntax highlighting
        except ValueError:
            lexer = get_lexer_by_name("text")  # Default to plain text if guessing fails
        formatter = HtmlFormatter(linenos=True, cssclass="code")
        result = highlight(code, lexer, formatter)
        return result

    def set_bookmark_changed_callback(self, callback):
        """Set the callback function to be called when bookmarks change."""
        self.bookmark_changed_callback = callback

    def edit_and_run(self):
        selected_text = self.textCursor().selectedText()

        # Check if any text is selected
        if not selected_text:
            # Optionally handle the case of no selection, or simply return
            return

        dialog = utilities.EditCommandDialog(selected_text)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            edited_command = dialog.get_command()
            # Execute the command with the possibly edited text
            self.command_input_area.execute_command(edited_command)

    def ask_assistant(self):
        selected_text = self.textCursor().selectedText()

        # Check if any text is selected
        if not selected_text:
            # Optionally handle the case of no selection, or simply return
            return

        dialog = utilities.EditCommandDialog(selected_text, command_input_area=True)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            edited_command = dialog.get_command()
            # Execute the command with the possibly edited text
            self.command_input_area.execute_command(edited_command)

    def remove_current_bookmark(self):
        cursor = self.textCursor()
        position = cursor.position()

        self.removeBookmark(position)

    def excludeWord(self, _=None):
        selected_text = self.textCursor().selectedText()
        if selected_text.strip():
            self.CONFIG = self.manager.load_config()
            with open(self.CONFIG["PRIVACY_FILE"], "a") as file:
                file.write(selected_text + "\n")

    def set_number_list(self, _=None):
        try:
            cursor = self.textCursor()
            list_format = QTextListFormat()
            list_format.setStyle(QTextListFormat.Style.ListDecimal)
            cursor.createList(list_format)
        except Exception as e:
            logger.error(f"Error in set_number_list: {e}")

    def set_color(self, _=None):
        try:
            color = QColorDialog.getColor()
            if color.isValid():
                current_format = self.textCursor().charFormat()
                current_format.setForeground(color)
                self.textCursor().mergeCharFormat(current_format)
        except Exception as e:
            logger.error(f"Error in set_color: {e}")

    def set_heading(self, _=None):
        try:
            current_format = self.textCursor().charFormat()
            current_format.setFontPointSize(16)
            current_format.setFontWeight(QFont.Weight.Bold)
            self.textCursor().mergeCharFormat(current_format)
        except Exception as e:
            logger.error(f"Error in set_heading: {e}")

    def set_italic(self, _=None):
        try:
            current_format = self.textCursor().charFormat()
            current_format.setFontItalic(not current_format.fontItalic())
            self.textCursor().mergeCharFormat(current_format)
        except Exception as e:
            logger.error(f"Error in set_italic: {e}")

    # just something so i can push
    def set_bold(self, _=None):
        try:
            if self.fontWeight() == QFont.Weight.Bold:
                self.setFontWeight(QFont.Weight.Normal)
            else:
                self.setFontWeight(QFont.Weight.Bold)
        except Exception as e:
            logger.error(f"Error in set_bold: {e}")

    def set_underline(self, _=None):
        try:
            self.setFontUnderline(not self.fontUnderline())
        except Exception as e:
            logger.error(f"Error in set_underline: {e}")

    def set_bullet_list(self, _=None):
        try:
            cursor = self.textCursor()
            list_format = QTextListFormat()
            list_format.setStyle(QTextListFormat.Style.ListDisc)
            cursor.createList(list_format)
        except Exception as e:
            logger.error(f"Error in set_bullet_list: {e}")

    def copy_selected_text(self, _=None):
        try:
            selected_text = self.textCursor().selectedText()
            clipboard = QApplication.clipboard()
            clipboard.setText(selected_text)
        except Exception as e:
            logger.error(f"Error in copy_selected_text: {e}")

    def append_text(self, text):
        try:
            self.insertHtml("\n" + text)
        except Exception as e:
            logger.error(f"Error in append_text: {e}")

    def removeBookmark(self, position):
        self.bookmarks = [
            bookmark for bookmark in self.bookmarks if bookmark["position"] != position
        ]

        self.save_bookmarks()

        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def toggle_bookmark(self):
        cursor = self.textCursor()
        position = cursor.position()
        existing_bookmark = next(
            (b for b in self.bookmarks if b["position"] == position), None
        )

        if existing_bookmark:
            self.bookmarks.remove(existing_bookmark)
        else:
            name, ok = QInputDialog.getText(
                self, "Bookmark Name", "Enter a name for the bookmark:"
            )
            if ok and name:
                self.bookmarks.append({"position": position, "name": name})
        self.save_bookmarks()

        if self.bookmark_changed_callback:
            self.bookmark_changed_callback()

    def gotoBookmark(self, position):
        """Navigate to the specified bookmark position and highlight the line."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)

        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)

        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )

        self.setTextCursor(cursor)

        self.ensureCursorVisible()

    def goto_next_bookmark(self):
        cursor = self.textCursor()
        current_position = cursor.position()
        next_bookmark = None

        for bookmark in self.bookmarks:
            if bookmark["position"] > current_position:
                next_bookmark = bookmark["position"]
                break

        if next_bookmark is not None:
            cursor.setPosition(next_bookmark)
            self.setTextCursor(cursor)

    def goto_prev_bookmark(self):
        cursor = self.textCursor()
        current_position = cursor.position()
        prev_bookmark = None

        for bookmark in reversed(self.bookmarks):
            if bookmark["position"] < current_position:
                prev_bookmark = bookmark["position"]
                break

        if prev_bookmark is not None:
            cursor.setPosition(prev_bookmark)
            self.setTextCursor(cursor)

    def save_bookmarks(self):
        with open(self.bookmarks_path, "w") as f:
            json.dump(self.bookmarks, f)

    def load_bookmarks(self):
        if os.path.exists(self.bookmarks_path):
            with open(self.bookmarks_path, "r") as f:
                try:
                    loaded_bookmarks = json.load(f)

                    if isinstance(loaded_bookmarks, list):
                        self.bookmarks = loaded_bookmarks
                    else:
                        logger.debug(
                            "Bookmarks file does not contain a list. Starting with an empty bookmarks list."
                        )
                except json.JSONDecodeError:
                    logger.debug(
                        "Error decoding JSON from bookmarks file. Starting with an empty bookmarks list."
                    )
        else:
            logger.debug(
                "Bookmarks file does not exist. Starting with an empty bookmarks list."
            )


class AiNotesPopupWindow(QMainWindow):
    textUpdated = pyqtSignal(str)

    def __init__(self, notes_file_path, manager, command_input_area):
        super().__init__()
        self.current_notes_file = notes_file_path
        self.manager = manager
        self.command_input_area = command_input_area
        self.initUI()
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))

        self.resize(1200, 600)
        self.center()

    def load_stylesheet(self, filename):
        style_file = QFile(filename)
        style_file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text)
        self.original_stylesheet = style_file.readAll().data().decode("utf-8")
        self.setStyleSheet(self.original_stylesheet)

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def initUI(self, _=None):
        self.CONFIG = self.manager.load_config()
        self.textEdit = AiNotes(
            bookmarks_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"],
                "ai_notes_bookmarks.bookmarks",
            ),
            manager=self.manager,
            file_path=self.current_notes_file,
            command_input_area=self.command_input_area,
        )
        self.createBookmarkDock()
        self.textEdit.textChanged.connect(self.on_text_changed)
        self.textEdit.currentCharFormatChanged.connect(self.on_text_changed)
        self.textEdit.cursorPositionChanged.connect(self.on_text_changed)
        self.textEdit.setObjectName("userNotes")
        self.setCentralWidget(self.textEdit)

        self.createToolbar()
        self.setWindowTitle("AI Notes")
        self.setGeometry(300, 300, 600, 400)

    def on_text_changed(self, _=None):
        self.textUpdated.emit(self.textEdit.toHtml())

    def setTextInTextEdit(self, text):
        try:
            self.textEdit.setHtml(text)
            self.update()
        except Exception as e:
            logger.error(f"Error in setTextInTextEdit: {e}")

    def change_icon_temporarily(
        self, action, temp_icon_path, original_icon_path, delay=500
    ):
        action.setIcon(QIcon(temp_icon_path))
        self.window().repaint()
        QApplication.processEvents()  # Force the UI to update
        QTimer.singleShot(delay, lambda: action.setIcon(QIcon(original_icon_path)))

    def provide_feedback_and_execute(
        self, action, temp_icon_path, original_icon_path, function
    ):
        self.change_icon_temporarily(action, temp_icon_path, original_icon_path)
        function()

    def createToolbar(self, _=None):
        self.toolbar = QToolBar("Editor Toolbar")
        self.toolbar.setFixedHeight(30)
        self.toolbar.setIconSize(QtCore.QSize(32, 32))
        self.addToolBar(self.toolbar)

        openAction = QAction("Open", self)
        openAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                openAction,
                return_path("Images/clicked.png"),
                return_path("Images/file.png"),
                self.openFile,
            )
        )
        openAction_icon_path = return_path("Images/file.png")
        openAction.setIcon(QIcon(openAction_icon_path))
        self.toolbar.addAction(openAction)

        boldAction = QAction(QIcon(return_path("Images/bold.png")), "Bold", self)
        boldAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                boldAction,
                return_path("Images/clicked.png"),
                return_path("Images/bold.png"),
                self.makeBold,
            )
        )
        self.toolbar.addAction(boldAction)

        italicAction = QAction(QIcon(return_path("Images/italic.png")), "Italic", self)
        italicAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                italicAction,
                return_path("Images/clicked.png"),
                return_path("Images/italic.png"),
                self.makeItalic,
            )
        )
        self.toolbar.addAction(italicAction)

        underlineAction = QAction(
            QIcon(return_path("Images/underline.png")), "Underline", self
        )
        underlineAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                underlineAction,
                return_path("Images/clicked.png"),
                return_path("Images/underline.png"),
                self.makeUnderline,
            )
        )
        self.toolbar.addAction(underlineAction)

        colorAction = QAction(
            QIcon(return_path("Images/select_color.png")), "Color", self
        )
        colorAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                colorAction,
                return_path("Images/clicked.png"),
                return_path("Images/select_color.png"),
                self.changeColor,
            )
        )
        self.toolbar.addAction(colorAction)

        numberedListAction = QAction(
            QIcon(return_path("Images/numbered_list.png")), "Numbered List", self
        )
        numberedListAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                numberedListAction,
                return_path("Images/clicked.png"),
                return_path("Images/numbered_list.png"),
                lambda: self.makeList(QTextListFormat.Style.ListDecimal),
            )
        )
        self.toolbar.addAction(numberedListAction)

        bulletListAction = QAction("Bullet List", self)
        bulletListAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                bulletListAction,
                return_path("Images/clicked.png"),
                return_path("Images/bullet_list.png"),
                self.insertBulletList,
            )
        )
        bulletListAction_icon_path = return_path("Images/bullet_list.png")
        bulletListAction.setIcon(QIcon(bulletListAction_icon_path))
        self.toolbar.addAction(bulletListAction)

        highlightAction = QAction("Highlight Text", self)
        highlightAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                highlightAction,
                return_path("Images/clicked.png"),
                return_path("Images/highlight.png"),
                self.highlightText,
            )
        )
        highlightAction_icon_path = return_path("Images/highlight.png")
        highlightAction.setIcon(QIcon(highlightAction_icon_path))
        self.toolbar.addAction(highlightAction)

        headingAction = QAction("Heading", self)
        headingAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                headingAction,
                return_path("Images/clicked.png"),
                return_path("Images/heading.png"),
                lambda: self.formatText("heading"),
            )
        )
        headingAction_icon_path = return_path("Images/heading.png")
        headingAction.setIcon(QIcon(headingAction_icon_path))
        self.toolbar.addAction(headingAction)

        self.replace_action = QAction(
            QIcon(return_path("Images/search_replace.png")), "Replace", self
        )
        self.replace_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.replace_action,
                return_path("Images/clicked.png"),
                return_path("Images/search_replace.png"),
                self.on_search_replace_triggered,
            )
        )
        self.toolbar.addAction(self.replace_action)

        increaseFontAction = QAction("Increase Font", self)
        increaseFontAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                increaseFontAction,
                return_path("Images/clicked.png"),
                return_path("Images/increase_font.png"),
                lambda: self.adjustFontSize(1),
            )
        )
        increaseFontAction_icon = QIcon(return_path("Images/increase_font.png"))
        increaseFontAction.setIcon(increaseFontAction_icon)
        self.toolbar.addAction(increaseFontAction)

        decreaseFontAction = QAction("Decrease Font", self)
        decreaseFontAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                decreaseFontAction,
                return_path("Images/clicked.png"),
                return_path("Images/decrease_font.png"),
                lambda: self.adjustFontSize(-1),
            )
        )
        decreaseFontAction_icon = QIcon(return_path("Images/decrease_font.png"))
        decreaseFontAction.setIcon(decreaseFontAction_icon)
        self.toolbar.addAction(decreaseFontAction)

        undoAction = QAction("Undo", self)
        undoAction.setShortcut(QKeySequence.StandardKey.Undo)
        undoAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                undoAction,
                return_path("Images/clicked.png"),
                return_path("Images/undo.png"),
                self.textEdit.undo,
            )
        )
        undoAction_icon_path = return_path("Images/undo.png")
        undoAction.setIcon(QIcon(undoAction_icon_path))
        self.toolbar.addAction(undoAction)

        redoAction = QAction("Redo", self)
        redoAction.setShortcut(QKeySequence.StandardKey.Redo)
        redoAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                redoAction,
                return_path("Images/clicked.png"),
                return_path("Images/redo.png"),
                self.textEdit.redo,
            )
        )
        redoAction_icon_path = return_path("Images/redo.png")
        redoAction.setIcon(QIcon(redoAction_icon_path))
        self.toolbar.addAction(redoAction)

        self.toggleBookmarksDockAction = QAction("Toggle Bookmarks", self)
        self.toggleBookmarksDockAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.toggleBookmarksDockAction,
                return_path("Images/clicked.png"),
                return_path("Images/bookmarks.png"),
                self.toggleBookmarksDock,
            )
        )
        toggleBookmarksDockAction_icon_path = return_path("Images/bookmarks.png")
        self.toggleBookmarksDockAction.setIcon(
            QIcon(toggleBookmarksDockAction_icon_path)
        )
        self.toolbar.addAction(self.toggleBookmarksDockAction)

        addBookmarkAction = QAction("Add Bookmark", self)
        addBookmarkAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                addBookmarkAction,
                return_path("Images/clicked.png"),
                return_path("Images/add_bookmark.png"),
                self.add_bookmark,
            )
        )
        addBookmarkAction_icon_path = return_path("Images/add_bookmark.png")
        addBookmarkAction.setIcon(QIcon(addBookmarkAction_icon_path))
        self.toolbar.addAction(addBookmarkAction)

        removeBookmarkAction = QAction("Remove Bookmark", self)
        removeBookmarkAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                removeBookmarkAction,
                return_path("Images/clicked.png"),
                return_path("Images/remove_bookmark.png"),
                self.removeBookmark,
            )
        )
        removeBookmarkAction_icon_path = return_path("Images/remove_bookmark.png")
        removeBookmarkAction.setIcon(QIcon(removeBookmarkAction_icon_path))
        self.toolbar.addAction(removeBookmarkAction)

        # Assuming self.updateBookmarksList() and self.textEdit.bookmark_changed_callback are set as needed

        self.textEdit.bookmark_changed_callback = (
            self.updateBookmarksList
        )  # Set callback

        self.updateBookmarksList()

    def createBookmarkDock(self):
        self.bookmarksDock = QDockWidget("Bookmarks", self)
        customTitleBar = CustomTitleBar(self.bookmarksDock)
        self.bookmarksDock.setTitleBarWidget(customTitleBar)  # Use the custom title bar

        self.bookmarksListWidget = QListWidget()
        self.bookmarksListWidget.setObjectName("bookmarksListWidget")

        self.bookmarksDock.setWidget(self.bookmarksListWidget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.bookmarksDock)
        self.bookmarksDock.hide()  # Initially hide the dock

        self.bookmarksListWidget.itemClicked.connect(self.gotoBookmarkFromList)

    def removeBookmark(self):
        selectedItem = self.bookmarksListWidget.currentItem()
        if selectedItem:
            bookmarkName = selectedItem.text()

            bookmarkToRemove = next(
                (
                    bookmark
                    for bookmark in self.textEdit.bookmarks
                    if bookmark["name"] == bookmarkName
                ),
                None,
            )
            if bookmarkToRemove:
                self.textEdit.removeBookmark(bookmarkToRemove["position"])
                self.updateBookmarksList()

    def toggleBookmarksDock(self):
        self.bookmarksDock.setVisible(not self.bookmarksDock.isVisible())
        if self.bookmarksDock.isVisible():
            self.updateBookmarksList()

    def updateBookmarksList(self):
        self.bookmarksListWidget.clear()
        for bookmark in self.textEdit.bookmarks:
            self.bookmarksListWidget.addItem(bookmark["name"])

    def gotoBookmarkFromList(self, item):
        for bookmark in self.textEdit.bookmarks:
            if bookmark["name"] == item.text():
                self.textEdit.gotoBookmark(bookmark["position"])
                break

    def add_bookmark(self):
        self.textEdit.toggle_bookmark()  # Ensure AiNotes has a method to add the current position as a bookmark
        self.updateBookmarksList()

    def get_input(self, title, label):
        text, ok = QInputDialog.getText(self, title, label)
        return text if ok else None

    def on_search_replace_triggered(self, _=None):
        dialog = SearchReplaceDialog(self.textEdit, self)
        dialog.show()

    def adjustFontSize(self, delta):
        currentFont = self.textEdit.currentFont()
        currentFontSize = currentFont.pointSize()
        newFontSize = max(1, currentFontSize + delta)
        currentFont.setPointSize(newFontSize)

        # Apply the new font size to the whole text edit, not just the current selection
        self.textEdit.selectAll()
        self.textEdit.setCurrentFont(currentFont)
        self.textEdit.textCursor().clearSelection()  # Clear the selection to avoid highlighting all text

    def setupAutosave(self, _=None):
        self.autosaveTimer = QTimer(self)
        self.autosaveTimer.timeout.connect(self.autosave)

    def autosave(self, _=None):
        if self.current_notes_file:
            try:
                with open(self.current_notes_file, "w") as file:
                    file.write(self.textEdit.toHtml())
            except Exception as e:
                QMessageBox.critical(
                    self, "Autosave Failed", f"Failed to autosave file: {e}"
                )

    def openFile(self, _=None):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open File", "", "Text Files (*.txt);;All Files (*)"
        )
        if filename:
            try:
                with open(filename, "r") as file:
                    self.textEdit.setText(file.read())
                    self.current_notes_file = filename
                    self.textEdit.set_notes_file(filename)
            except Exception as e:
                QMessageBox.critical(
                    self, "Error", f"An error occurred while opening the file: {e}"
                )

    def makeBold(self, _=None):
        cursor = self.textEdit.textCursor()
        if not cursor.hasSelection():
            return

        format = QTextCharFormat()
        format.setFontWeight(
            QFont.Weight.Bold
            if cursor.charFormat().fontWeight() != QFont.Weight.Bold
            else QFont.Weight.Normal
        )
        cursor.mergeCharFormat(format)

    def makeItalic(self, _=None):
        cursor = self.textEdit.textCursor()
        format = QTextCharFormat()
        format.setFontItalic(not cursor.charFormat().fontItalic())
        cursor.mergeCharFormat(format)

    def makeUnderline(self, _=None):
        cursor = self.textEdit.textCursor()
        format = QTextCharFormat()
        format.setFontUnderline(not cursor.charFormat().fontUnderline())
        cursor.mergeCharFormat(format)

    def changeColor(self, _=None):
        color = QColorDialog.getColor()
        if color.isValid():
            cursor = self.textEdit.textCursor()
            format = QTextCharFormat()
            format.setForeground(color)
            cursor.mergeCharFormat(format)

    def makeList(self, listStyle):
        cursor = self.textEdit.textCursor()
        cursor.beginEditBlock()

        blockFormat = cursor.blockFormat()
        listFormat = QTextListFormat()

        if cursor.currentList():
            listFormat = cursor.currentList().format()
        else:
            listFormat.setIndent(blockFormat.indent() + 1)
            blockFormat.setIndent(0)
            cursor.setBlockFormat(blockFormat)

        listFormat.setStyle(listStyle)
        cursor.createList(listFormat)

        cursor.endEditBlock()

    def insertBulletList(self, _=None):
        cursor = self.textEdit.textCursor()

        currentList = cursor.currentList()

        if currentList:
            listFormat = currentList.format()
            listFormat.setStyle(QTextListFormat.Style.ListDisc)
            currentList.setFormat(listFormat)
        else:
            listFormat = QTextListFormat()
            listFormat.setStyle(QTextListFormat.Style.ListDisc)
            cursor.createList(listFormat)

    def highlightText(self, _=None):
        color = QColorDialog.getColor(QColor(255, 255, 0))
        if color.isValid():
            self.textEdit.setTextBackgroundColor(color)

    def formatText(self, style):
        cursor = self.textEdit.textCursor()

        if cursor.hasSelection():
            if style == "heading":
                self.applyHeadingStyle(cursor)

    def applyHeadingStyle(self, cursor):
        format = QTextCharFormat()
        format.setFontWeight(QFont.Weight.Bold)
        format.setFontPointSize(16)
        cursor.mergeCharFormat(format)
