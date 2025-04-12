import os

from PyQt6 import QtCore
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QTextCharFormat,
    QTextListFormat,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTabBar,
    QTabWidget,
    QToolBar,
)

from . import constants
from .ai_notes_pop_up_window import AiNotes, CustomTitleBar
from .log_config import setup_logging
from .search_replace_dialog import SearchReplaceDialog
from .update_utils import return_path

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/user_note_taking.log")


class CustomTabBar(QTabBar):
    close_tab = pyqtSignal(int)  # Signal to emit the index of the tab to close
    tab_renamed = pyqtSignal(
        str, str
    )  # New signal for tab renaming, old name, new name

    def __init__(self, parent=None):
        super().__init__(parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            clickedTabIndex = self.tabAt(event.position().toPoint())
            if clickedTabIndex != -1:
                self.showContextMenu(clickedTabIndex, event.globalPosition().toPoint())
        else:
            super().mousePressEvent(event)

    def showContextMenu(self, tabIndex, position):
        menu = QMenu()
        renameAction = QAction("Rename Tab")
        closeTabAction = QAction("Close Tab")
        menu.addAction(renameAction)
        menu.addAction(closeTabAction)

        action = menu.exec(position)

        if action == renameAction:
            self.renameTab(tabIndex)
        elif action == closeTabAction:
            self.close_tab.emit(tabIndex)  # Emit the signal with the tab index

    def renameTab(self, tabIndex):
        oldName = self.tabText(tabIndex)
        newName, ok = QInputDialog.getText(
            self, "Rename Tab", "Enter a new name for the tab:", text=oldName
        )
        if (
            ok and newName and newName != oldName
        ):  # Check if the name is actually changed
            self.parent().setTabText(tabIndex, newName)
            self.tab_renamed.emit(oldName, newName)  # Emit the signal here


class CustomTabWidget(QTabWidget):
    close_tab = pyqtSignal(int)  # Define the signal in CustomTabWidget
    tab_changed = pyqtSignal(int)  # Define a new signal for tab change

    def __init__(self, parent=None):
        super().__init__(parent)
        self.customTabBar = CustomTabBar(self)
        self.setTabBar(self.customTabBar)
        self.customTabBar.close_tab.connect(
            self.close_tab.emit
        )  # Connect CustomTabBar's signal to CustomTabWidget's signal
        self.currentChanged.connect(
            self.tab_changed.emit
        )  # Connect internal signal to custom signal


class UserNoteTaking(QMainWindow):
    def __init__(self, _=None, manager=None, command_input_area=None):
        super().__init__()
        self.manager = manager

        self.existingTabNumbers = set()  # Keep track of existing tab numbers

        self.command_input_area = command_input_area
        self.CONFIG = self.manager.load_config()

        self.tabFilePaths = {}
        self.current_notes_file = None
        self.tabWidget = CustomTabWidget()
        self.tabWidget.close_tab.connect(self.closeCurrentTab)
        self.tabWidget.tab_changed.connect(
            self.updateBookmarksList
        )  # Update bookmarks when tab changes
        self.setCentralWidget(self.tabWidget)
        self.tabWidget.customTabBar.tab_renamed.connect(
            self.renameTabFile
        )  # Connect signal
        self.createBookmarkDock()
        self.createToolbar()

        self.addTab()
        self.setupAutosave()

        self.setWindowTitle("Notes")
        self.setGeometry(300, 300, 600, 400)

        with open(return_path("config/dark-stylesheet.css"), "r") as file:
            self.setStyleSheet(file.read())
        self.resize(1200, 600)
        self.center()

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def renameTabFile(self, oldName, newName):
        logger.debug("Performing cleanup operations after renaming tab file")
        oldFilePath = os.path.join(
            self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], f"{oldName}.html"
        )
        newFilePath = os.path.join(
            self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], f"{newName}.html"
        )
        newName = newName + ".html"
        logger.debug(f"new name is {newName}")
        if os.path.exists(oldFilePath):
            os.rename(oldFilePath, newFilePath)
            # Here, ensure the mapping is correctly updated
            for tab_index, filename in list(self.tabFilePaths.items()):
                oldName = oldName + ".html"
                logger.debug(
                    f"checking index {tab_index} and filename {filename} against {oldName}"
                )
                if filename == oldName:  # Previously matched based on tab name
                    logger.debug(f"found old filename {filename}")
                    self.tabFilePaths[tab_index] = newName  # Update with new file path
                    break

    def addTab(self, _=None, initialContent=None):
        # Find the smallest available tab number
        tab_count = 1
        while tab_count in self.existingTabNumbers:
            tab_count += 1

        # Use the new unique tab number as the tab name
        tab_name = str(tab_count)
        # Mark this tab number as used
        self.existingTabNumbers.add(tab_count)

        filename = f"{tab_name}.html"  # Construct filename based on tab name
        file_path = os.path.join(self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], filename)

        # If initialContent is None, check if the file already exists and load its content
        if initialContent is None:
            if os.path.exists(file_path):
                with open(file_path, "r") as file:
                    initialContent = file.read()
            else:
                # Set default initial content if the file does not exist
                initialContent = "<p>Welcome! Start taking notes here.</p>"

        bookmarks_name = f"{tab_name}.bookmarks"
        newTextEdit = AiNotes(
            manager=self.manager,
            command_input_area=self.command_input_area,
            bookmarks_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], bookmarks_name
            ),
        )
        newTextEdit.setObjectName("userNotes")
        newTextEdit.setHtml(initialContent)
        newTextEdit.set_bookmark_changed_callback(self.updateBookmarksList)
        icon_path = return_path("Images/single_tab.png")
        tabIcon = QIcon(icon_path)

        # Store the filename associated with the tab index
        self.tabFilePaths[self.tabWidget.count()] = filename

        # Use the tab name as the tab title and add the new tab
        self.tabWidget.addTab(newTextEdit, tabIcon, tab_name)

    def closeCurrentTab(self):
        currentTabIndex = self.tabWidget.currentIndex()
        if currentTabIndex != -1:
            # Extract the tab number and remove it from existingTabNumbers
            tabText = self.tabWidget.tabText(currentTabIndex)
            if tabText.isdigit():  # Check if the tab text is numerical
                self.existingTabNumbers.remove(int(tabText))

            # Close the tab
            self.tabWidget.removeTab(currentTabIndex)

            # Remove the associated file path from self.tabFilePaths if it exists
            if currentTabIndex in self.tabFilePaths:
                del self.tabFilePaths[currentTabIndex]

    def createToolbar(self, _=None):
        self.toolbar = QToolBar("Editor Toolbar")
        self.toolbar.setFixedHeight(30)  # Set the height to 50 pixels
        self.toolbar.setIconSize(QtCore.QSize(16, 16))
        self.addToolBar(self.toolbar)

        openAction = QAction("Open", self)
        openAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                openAction,
                return_path("Images/clicked.png"),
                return_path("Images/folder.png"),
                self.openFile,
            )
        )

        openAction_icon_path = return_path("Images/folder.png")

        saveAction = QAction("Save", self)
        saveAction.setShortcut(QKeySequence.StandardKey.Save)
        saveAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                saveAction,
                return_path("Images/clicked.png"),
                return_path("Images/save.png"),
                self.saveFile,
            )
        )

        saveAction_icon_path = return_path("Images/save.png")
        saveAction.setIcon(QIcon(saveAction_icon_path))
        self.toolbar.addAction(saveAction)

        newTabAction = QAction("New Tab", self)
        newTabAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                newTabAction,
                return_path("Images/clicked.png"),
                return_path("Images/tab.png"),
                self.addTab,
            )
        )

        newTabAction_icon_path = return_path("Images/tab.png")

        closeTabAction = QAction("Close Tab", self)
        closeTabAction.setIcon(
            QIcon(return_path("Images/close_tab.png"))
        )  # Ensure you have a suitable icon
        closeTabAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                newTabAction,
                return_path("Images/clicked.png"),
                return_path("Images/tab.png"),
                self.closeCurrentTab,
            )
        )

        newTabAction.setIcon(QIcon(newTabAction_icon_path))
        self.toolbar.addAction(newTabAction)
        self.toolbar.addAction(closeTabAction)
        openAction.setIcon(QIcon(openAction_icon_path))
        self.toolbar.addAction(openAction)
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
            QIcon(return_path("Images/search_replace.png")), "Search and Replace", self
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

        increaseFontAction = QAction(
            QIcon(return_path("Increase Font")), "Increase Font", self
        )
        increaseFontAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                increaseFontAction,
                return_path("Images/clicked.png"),
                return_path("Images/increase_font.png"),
                lambda: self.adjustFontSize(1),
            )
        )

        increaseFontAction_icon = QIcon(
            return_path(return_path("Images/increase_font.png"))
        )
        increaseFontAction.setIcon(increaseFontAction_icon)
        self.toolbar.addAction(increaseFontAction)

        decreaseFontAction = QAction(
            QIcon(return_path("Decrease Font")), "Decrease Font", self
        )
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
                self.undoText,
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
                self.redoText,
            )
        )
        redoAction_icon_path = return_path("Images/redo.png")
        redoAction.setIcon(QIcon(redoAction_icon_path))
        self.toolbar.addAction(redoAction)

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
        currentTextEdit = self.getCurrentTextEdit()
        if selectedItem:
            bookmarkName = selectedItem.text()

            bookmarkToRemove = next(
                (
                    bookmark
                    for bookmark in currentTextEdit.bookmarks
                    if bookmark["name"] == bookmarkName
                ),
                None,
            )
            if bookmarkToRemove:
                currentTextEdit.removeBookmark(bookmarkToRemove["position"])
                self.updateBookmarksList()

    def saveFile(self, _=None):
        currentTabIndex = self.tabWidget.currentIndex()
        textEdit = self.tabWidget.currentWidget()
        content = textEdit.toHtml()

        if content.strip():
            # Check if we already have a file path for this tab
            if currentTabIndex in self.tabFilePaths:
                filePath = self.tabFilePaths[currentTabIndex]
            else:
                # Otherwise, ask the user where to save the file
                self.CONFIG = self.manager.load_config()
                filePath, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save File",
                    self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"],
                    "HTML Files (*.html);;All Files (*)",
                )
                if filePath:
                    self.tabFilePaths[currentTabIndex] = filePath
                    baseName = os.path.splitext(os.path.basename(filePath))[0]
                    self.tabWidget.setTabText(currentTabIndex, baseName)
                    self.existingTabNumbers.add(
                        int(baseName)
                    )  # Mark this tab number as used
                else:
                    return  # Exit if no file was selected

            # Now save the content to the file
            try:
                with open(filePath, "w") as file:
                    file.write(content)
            except Exception as e:
                QMessageBox.critical(
                    self, "Error", f"An error occurred while saving the file: {e}"
                )

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
        if not self.bookmarksDock.isVisible():
            return
        self.bookmarksListWidget.clear()
        currentTextEdit = self.getCurrentTextEdit()
        if currentTextEdit:
            for bookmark in currentTextEdit.bookmarks:
                self.bookmarksListWidget.addItem(bookmark["name"])

    def add_bookmark(self):
        currentTextEdit = self.getCurrentTextEdit()
        currentTextEdit.toggle_bookmark()  # Ensure AiNotes has a method to add the current position as a bookmark
        self.updateBookmarksList()

    def gotoBookmarkFromList(self, item):
        currentTextEdit = self.getCurrentTextEdit()
        for bookmark in currentTextEdit.bookmarks:
            if bookmark["name"] == item.text():
                currentTextEdit.gotoBookmark(bookmark["position"])
                break

    def on_search_replace_triggered(self, _=None):
        currentTextEdit = self.getCurrentTextEdit()
        dialog = SearchReplaceDialog(currentTextEdit, self)
        dialog.show()

    def adjustFontSize(self, delta):
        currentTextEdit = self.getCurrentTextEdit()
        currentFont = currentTextEdit.currentFont()
        currentFontSize = currentFont.pointSize()
        newFontSize = max(1, currentFontSize + delta)
        currentFont.setPointSize(newFontSize)

        # Apply the new font size to the whole text edit, not just the current selection
        currentTextEdit.selectAll()
        currentTextEdit.setCurrentFont(currentFont)
        currentTextEdit.textCursor().clearSelection()  # Clear the selection to avoid highlighting all text

    def setupAutosave(self, _=None):
        self.autosaveTimer = QTimer(self)
        self.autosaveTimer.timeout.connect(self.autosave)
        self.autosaveTimer.start(3000)

    def autosave(self, _=None):
        suggestions_notes_directory = self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"]
        try:
            # Iterate through each tab currently open
            # logger.debug(f"tab file paths in autosave: {self.tabFilePaths}")
            # logger.debug(f" tab widget count: {self.tabWidget.count()}")
            for index in range(self.tabWidget.count()):
                # Check if this tab index has an associated file path
                if index in self.tabFilePaths:
                    logger.debug(
                        f"index {index} found in tabFilePaths {self.tabFilePaths}"
                    )
                    # Retrieve the editor widget and its content for this tab
                    textEdit = self.tabWidget.widget(index)
                    content = textEdit.toHtml()

                    # Get the current file path associated with this tab
                    unique_filename = self.tabFilePaths[index]
                    new_file_path = os.path.join(
                        suggestions_notes_directory, unique_filename
                    )

                    # Save the content to the associated file
                    try:
                        with open(new_file_path, "w") as file:
                            file.write(content)
                    except Exception as e:
                        QMessageBox.critical(
                            self, "Autosave Failed", f"Failed to autosave file: {e}"
                        )
                # If there is no associated file path for this tab, do nothing (skip autosave for this tab)
        except Exception as e:
            logger.error(f"Failed to autosave file: {e}")

    def openFile(self, _=None):
        self.CONFIG = self.manager.load_config()
        default_dir = self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"]

        filename, _ = QFileDialog.getOpenFileName(
            self, "Open File", default_dir, "All Files (*)"
        )
        if filename:
            try:
                with open(filename, "r") as file:
                    # Get the current text edit widget and the current tab index
                    currentTextEdit = self.tabWidget.currentWidget()
                    currentTabIndex = self.tabWidget.currentIndex()
                    if currentTextEdit:
                        fileContent = file.read()
                        currentTextEdit.setHtml(fileContent)
                        baseName = os.path.splitext(os.path.basename(filename))[0]
                        self.tabWidget.setTabText(currentTabIndex, baseName)
                        # Update the mapping with the new file name and path
                        self.tabFilePaths[currentTabIndex] = filename
                        if baseName.isnumeric():
                            self.existingTabNumbers.add(
                                int(baseName)
                            )  # Only add if baseName is numeric

                    # Ensure this number is marked as used
                    else:
                        QMessageBox.critical(
                            self, "Error", "No active text area to open the file in."
                        )
            except Exception as e:
                QMessageBox.critical(
                    self, "Error", f"An error occurred while opening the file: {e}"
                )

    def insertBulletList(self, _=None):
        currentTextEdit = self.getCurrentTextEdit()
        cursor = currentTextEdit.textCursor()

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
        currentTextEdit = self.getCurrentTextEdit()
        color = QColorDialog.getColor(QColor(255, 255, 0))
        if color.isValid():
            currentTextEdit.setTextBackgroundColor(color)

    def formatText(self, style):
        textEdit = self.tabWidget.currentWidget()
        cursor = textEdit.textCursor()

        if cursor.hasSelection():
            if style == "heading":
                self.applyHeadingStyle(cursor)

    def applyHeadingStyle(self, cursor):
        format = QTextCharFormat()
        format.setFontWeight(QFont.Weight.Bold)
        format.setFontPointSize(16)
        cursor.mergeCharFormat(format)

    def getCurrentTextEdit(self, _=None):
        return self.tabWidget.currentWidget()

    def undoText(self, _=None):
        currentTextEdit = self.getCurrentTextEdit()
        if currentTextEdit:
            currentTextEdit.undo()

    def redoText(self, _=None):
        currentTextEdit = self.getCurrentTextEdit()
        if currentTextEdit:
            currentTextEdit.redo()
