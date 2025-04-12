from PyQt6.QtGui import QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from . import constants
from .log_config import setup_logging
from .update_utils import return_path

logger = setup_logging(
    log_file=f"{constants.SYSTEM_LOGS_DIR}/search_replace_dialog.log"
)


class SearchReplaceDialog(QDialog):
    def __init__(self, textEdit, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchReplaceDialog")
        self.textEdit = textEdit
        self.initUI()

    def initUI(self):
        self.loadStyleSheet("config/dark-stylesheet.css")
        self.setWindowTitle("Search and Replace")
        self.configureLayout()

    def loadStyleSheet(self, path):
        with open(return_path(path), "r") as file:
            self.setStyleSheet(file.read())

    def configureLayout(self):
        layout = QVBoxLayout(self)
        self.setupSearchFields(layout)
        self.setupButtons(layout)
        self.adjustSizeAndPosition()

    def setupSearchFields(self, layout):
        self.search_field = self.createLineEdit("Search for:", layout)
        self.replace_field = self.createLineEdit("Replace with:", layout)

    def createLineEdit(self, label, layout):
        layout.addWidget(QLabel(label))
        lineEdit = QLineEdit(self)
        lineEdit.setMinimumWidth(200)
        lineEdit.setMaximumHeight(30)
        layout.addWidget(lineEdit)
        return lineEdit

    def setupButtons(self, layout):
        buttons_layout = QHBoxLayout()
        self.search_button = QPushButton("Search", self)
        self.next_button = QPushButton("Next", self)  # Added Next button
        self.previous_button = QPushButton("Previous", self)
        self.replace_button = QPushButton("Replace", self)
        self.replace_all_button = QPushButton("Replace All", self)
        buttons_layout.addWidget(self.search_button)
        buttons_layout.addWidget(self.next_button)  # Add Next button to layout
        buttons_layout.addWidget(self.previous_button)
        buttons_layout.addWidget(self.replace_button)
        buttons_layout.addWidget(self.replace_all_button)
        buttons_layout.addStretch(1)
        layout.addLayout(buttons_layout)
        self.search_button.clicked.connect(
            lambda: self.on_search(next=False, backwards=False)
        )
        self.next_button.clicked.connect(
            lambda: self.on_search(next=True, backwards=False)
        )  # Connect Next button
        self.previous_button.clicked.connect(lambda: self.on_previous())
        self.replace_button.clicked.connect(self.on_replace)
        self.replace_all_button.clicked.connect(self.on_replace_all)

    def adjustSizeAndPosition(self):
        self.resize(400, 200)
        self.setMinimumSize(300, 180)
        self.setMaximumSize(500, 250)

    def on_search(self, next=True, backwards=False):
        search_term = self.search_field.text()
        self.performSearch(search_term, next=next, backwards=backwards)

    def on_previous(self):
        search_term = self.search_field.text()
        self.performSearch(search_term, next=True, backwards=True)

    def performSearch(self, term, next=True, backwards=False):
        if term:
            cursor = self.textEdit.textCursor()

            if not next:
                movePosition = (
                    QTextCursor.MoveOperation.End
                    if backwards
                    else QTextCursor.MoveOperation.Start
                )
                cursor.movePosition(movePosition)
                self.textEdit.setTextCursor(cursor)

            search_flag = (
                QTextDocument.FindFlag.FindBackward
                if backwards
                else QTextDocument.FindFlag(0)
            )
            if not self.textEdit.find(term, search_flag):
                QMessageBox.information(self, "Search", "Text not found.")

    def on_replace(self):
        cursor = self.textEdit.textCursor()
        if cursor.hasSelection():
            replace_term = self.replace_field.text()
            cursor.insertText(replace_term)
            self.textEdit.setTextCursor(cursor)

    def on_replace_all(self):
        search_term = self.search_field.text()
        replace_term = self.replace_field.text()  # This can be an empty string
        if (
            search_term
        ):  # Checking only for search_term because replace_term can be empty
            cursor = self.textEdit.textCursor()
            cursor.beginEditBlock()
            self.textEdit.moveCursor(QTextCursor.MoveOperation.Start)
            count = 0
            while self.textEdit.find(search_term):
                self.textEdit.textCursor().insertText(replace_term)
                count += 1

            cursor.endEditBlock()
            QMessageBox.information(
                self, "Replace All", f"All {count} occurrences replaced."
            )
