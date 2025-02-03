from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QScrollArea

from . import constants
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/image_display_label.log")


class ImageDisplayLabel(QLabel):
    clicked = pyqtSignal(QMouseEvent)

    def __init__(self, parent=None):
        super(ImageDisplayLabel, self).__init__(parent)
        self.scrollArea = self.findParentScrollArea()
        self.cursorPosition = None
        self.cursorVisible = False
        self.blinkTimer = QTimer(self)
        self.blinkTimer.timeout.connect(self.toggleCursorVisibility)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def findParentScrollArea(self, _=None):
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parent()
        return None

    def mousePressEvent(self, event):
        self.clicked.emit(event)
        super().mousePressEvent(event)

    def setCursorPosition(self, position):
        self.cursorPosition = position
        self.cursorVisible = True
        self.blinkTimer.start(500)
        self.update()

    def toggleCursorVisibility(self, _=None):
        if self.cursorPosition is not None:
            self.cursorVisible = not self.cursorVisible
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.cursorPosition and self.cursorVisible:
            painter = QPainter(self)
            painter.setPen(QPen(Qt.GlobalColor.black, 2))
            fm = QFontMetrics(self.font())
            x, y = self.cursorPosition.x(), self.cursorPosition.y()
            painter.drawLine(x, y, x, y - fm.height())

            painter.end()
