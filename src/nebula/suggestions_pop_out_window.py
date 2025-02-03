import os
import time

from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence,
                         QMouseEvent, QTextCharFormat, QTextCursor,
                         QTextListFormat)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QDockWidget,
                             QFileDialog, QInputDialog, QListWidget,
                             QMainWindow, QMenu, QMessageBox, QToolBar,
                             QVBoxLayout, QWidget)

from . import constants
from .ai_notes_pop_up_window import AiNotes, CustomTitleBar
from .log_config import setup_logging
from .search_replace_dialog import SearchReplaceDialog
from .update_utils import return_path

logger = setup_logging(
    log_file=constants.SYSTEM_LOGS_DIR + "/suggestions_pop_out_window.log"
)


class SuggestionsDisplayAreaClickableTextEdit(QWidget):
    def __init__(
        self,
        parent=None,
        autosave_interval=1000,
        default_save_path=None,
        command_input_area=None,
        manager=None,
    ):
        super().__init__(parent)
        self.manager = manager()
        container = QWidget(self)
        layout = QVBoxLayout(container)
        self.command_input_area = command_input_area

        self.CONFIG = self.manager.load_config()
        self.text_edit = AiNotes(
            self.current_notes_file,
            bookmarks_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"],
                "suggestions_bookmarks.bookmarks",
            ),
            command_input_area=self.command_input_area,
            file_path=self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"] + "/suggestions.html",
        )

        self.text_edit.setReadOnly(False)

        layout.addWidget(self.text_edit)

        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave_content)
        self.autosave_timer.start(autosave_interval)
        self.autosave_path = default_save_path
        self.load_content()

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(container)
        self.setLayout(main_layout)
        self.isSelectingText = False

    def createContextMenu(self, _=None):
        context_menu = QMenu(self.text_edit)
        context_menu.setStyleSheet(
            """
            QMenu::item:selected {
                background-color:#333333; 
            }
        """
        )
        copy_action = QAction("Copy", self.text_edit)
        copy_action.triggered.connect(self.copy)
        context_menu.addAction(copy_action)
        exclude_action = QAction("Exclude", self)
        exclude_action.triggered.connect(self.excludeWord)
        context_menu.addAction(exclude_action)
        return context_menu

    def excludeWord(self, _=None):
        cursor = self.textEdit.textCursor()
        selected_text = cursor.selectedText()
        if selected_text.strip():
            self.CONFIG = self.manager.load_config()
            with open(self.CONFIG["PRIVACY_FILE"], "a") as file:
                file.write(selected_text + "\n")

    def load_content(self, _=None):
        try:
            if os.path.exists(self.autosave_path):
                with open(self.autosave_path, "r") as file:
                    self.text_edit.setHtml(file.read())
                    logger.debug(f"Content loaded from {self.autosave_path}")
            else:
                logger.warning(f"Autosave path does not exist: {self.autosave_path}")

        except IOError as e:
            logger.error(f"Error reading from {self.autosave_path}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during loading content: {e}")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.isSelectingText = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.isSelectingText = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if self.textCursor().hasSelection():
            context_menu = self.createContextMenu()
            context_menu.exec_(event.globalPos())

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.text_edit.textCursor().hasSelection():
            super().mouseMoveEvent(event)
        else:
            cursor = self.text_edit.cursorForPosition(event.pos())
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            self.text_edit.setTextCursor(cursor)

            super().mouseMoveEvent(event)


class SuggestionsPopOutWindow(QMainWindow):
    def __init__(
        self,
        manager=None,
        command_input_area=None,
    ):
        super().__init__()
        self.manager = manager
        self.CONFIG = self.manager.load_config()
        self.autosave_path = os.path.join(
            self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], "suggestions.html"
        )
        self.command_input_area = command_input_area
        self.initUI()

        self.current_notes_file = None

        self.load_content()

        self.resize(1200, 600)
        self.center()
        with open(return_path("config/dark-stylesheet.css"), "r") as file:
            self.setStyleSheet(file.read())

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def initUI(self, _=None):
        self.textEdit = AiNotes(
            manager=self.manager,
            command_input_area=self.command_input_area,
            file_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], "suggestions.html"
            ),
            bookmarks_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"],
                "ai_notes_bookmarks.bookmarks",
            ),
        )
        self.setCentralWidget(self.textEdit)
        self.createBookmarkDock()
        self.createToolbar()
        self.setWindowTitle("AI Suggestions")
        self.setGeometry(300, 300, 600, 400)
        self.textEdit.bookmark_changed_callback = (
            self.updateBookmarksList
        )  # Set callback

    def update_suggestions(self, text):
        start_time = time.time()
        logger.debug("Updating Suggestions")
        html_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html_text = html_text.replace("\n", "<br>")
        self.textEdit.append(html_text)

        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating Suggestions took {duration} seconds.")

    def setTextInTextEdit(self, text):
        try:
            self.textEdit.setText(text)
        except Exception as e:
            logger.error(f"Error in setTextInTextEdit: {e}")

    def createToolbar(self, _=None):
        self.toolbar = QToolBar("Editor Toolbar")
        self.toolbar.setFixedHeight(30)
        self.toolbar.setIconSize(QtCore.QSize(32, 32))
        self.addToolBar(self.toolbar)

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

        self.updateBookmarksList()

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

    def createBookmarkDock(self):
        self.bookmarksDock = QDockWidget("Bookmarks", self)
        self.bookmarksDock = QDockWidget("Bookmarks", self)
        customTitleBar = CustomTitleBar(self.bookmarksDock)
        self.bookmarksDock.setTitleBarWidget(customTitleBar)  # Use the custom title bar
        self.bookmarksListWidget = QListWidget()
        self.bookmarksListWidget.setObjectName("bookmarksListWidget")
        self.bookmarksDock.setWidget(self.bookmarksListWidget)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.bookmarksDock)
        self.bookmarksDock.hide()  # Initially hide the dock

        self.bookmarksListWidget.itemClicked.connect(self.gotoBookmarkFromList)

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
        self.autosaveTimer.start(1000)

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
                    file_content = file.read()
                    html_content = (
                        file_content.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    html_content = html_content.replace("\n", "<br>")

                    self.textEdit.setHtml(html_content)
                    self.current_notes_file = filename

            except Exception as e:
                QMessageBox.critical(
                    self, "Error", f"An error occurred while opening the file: {e}"
                )

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

    def load_content(self, _=None):
        try:
            if os.path.exists(self.autosave_path):
                with open(self.autosave_path, "r") as file:
                    self.textEdit.setHtml(file.read())
                    logger.debug(f"Content loaded from {self.autosave_path}")
            else:
                logger.warning(f"Autosave path does not exist: {self.autosave_path}")

        except IOError as e:
            logger.error(f"Error reading from {self.autosave_path}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during loading content: {e}")
