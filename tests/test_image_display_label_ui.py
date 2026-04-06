from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLabel, QScrollArea

from nebula import image_display_label


class FakePainter:
    instances = []

    def __init__(self, widget):
        self.widget = widget
        self.pen = None
        self.lines = []
        self.ended = False
        type(self).instances.append(self)

    def setPen(self, pen):
        self.pen = pen

    def drawLine(self, x1, y1, x2, y2):
        self.lines.append((x1, y1, x2, y2))

    def end(self):
        self.ended = True


class FakeFontMetrics:
    def __init__(self, font):
        self.font = font

    def height(self):
        return 7


def test_image_display_label_behaviour(qapp, monkeypatch):
    updates = []
    parent_calls = []
    clicked_events = []

    label = image_display_label.ImageDisplayLabel()
    scroll = QScrollArea()
    scroll.setWidget(label)

    monkeypatch.setattr(label, "update", lambda: updates.append(True))
    monkeypatch.setattr(
        QLabel,
        "mousePressEvent",
        lambda self, event: parent_calls.append(event),
    )

    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(3, 4),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    label.clicked.connect(clicked_events.append)

    label.mousePressEvent(event)
    assert clicked_events == [event]
    assert parent_calls == [event]

    assert label.findParentScrollArea() is scroll
    label.setCursorPosition(QPoint(5, 9))
    assert label.cursorVisible is True
    assert label.blinkTimer.isActive() is True
    assert updates

    previous_visibility = label.cursorVisible
    label.toggleCursorVisibility()
    assert label.cursorVisible is not previous_visibility

    label.cursorPosition = None
    label.cursorVisible = True
    update_count = len(updates)
    label.toggleCursorVisibility()
    assert len(updates) == update_count


def test_image_display_label_paint_event_draws_cursor(monkeypatch):
    FakePainter.instances = []
    label = image_display_label.ImageDisplayLabel()
    label.cursorPosition = QPoint(5, 9)
    label.cursorVisible = True

    monkeypatch.setattr(QLabel, "paintEvent", lambda self, event: None)
    monkeypatch.setattr(image_display_label, "QPainter", FakePainter)
    monkeypatch.setattr(image_display_label, "QFontMetrics", FakeFontMetrics)

    label.paintEvent(object())

    assert FakePainter.instances[0].lines == [(5, 9, 5, 2)]
    assert FakePainter.instances[0].ended is True


def test_image_display_label_paint_event_skips_hidden_cursor(monkeypatch):
    label = image_display_label.ImageDisplayLabel()
    label.cursorPosition = QPoint(1, 1)
    label.cursorVisible = False

    monkeypatch.setattr(QLabel, "paintEvent", lambda self, event: None)
    monkeypatch.setattr(
        image_display_label,
        "QPainter",
        lambda widget: (_ for _ in ()).throw(AssertionError("painter should not run")),
    )

    label.paintEvent(object())
