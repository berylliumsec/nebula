from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QSize, Qt
from PyQt6.QtGui import QColor, QResizeEvent
from PyQt6.QtWidgets import QListWidgetItem

from nebula import image_command_window


class FakeEvent:
    def __init__(self, x, y, button=Qt.MouseButton.LeftButton):
        self._position = QPointF(x, y)
        self._button = button

    def globalPosition(self):
        return self._position

    def button(self):
        return self._button


def build_manager(tmp_path):
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()
    return SimpleNamespace(load_config=lambda: {"SCREENSHOTS_DIR": str(screenshots_dir)})


def write_image(path, size=(20, 20), value=100):
    image = np.full((size[1], size[0], 3), value, dtype=np.uint8)
    cv2.imwrite(str(path), image)
    return image


def build_window(qapp, tmp_path, monkeypatch):
    manager = build_manager(tmp_path)
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; }")
    monkeypatch.setattr(
        image_command_window.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )
    write_image(Path(manager.load_config()["SCREENSHOTS_DIR"]) / "first.png")
    write_image(Path(manager.load_config()["SCREENSHOTS_DIR"]) / "second.jpg", value=150)
    window = image_command_window.ImageCommandWindow(str(stylesheet), manager)
    window.show()
    qapp.processEvents()
    return window, manager


def test_image_command_window_file_workflow(qapp, tmp_path, monkeypatch):
    window, manager = build_window(qapp, tmp_path, monkeypatch)
    screenshots_dir = Path(manager.load_config()["SCREENSHOTS_DIR"])
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir()
    write_image(extra_dir / "third.png", value=200)

    try:
        assert window.windowTitle() == "Image Editor"
        assert window.imageListWidget.count() >= 2

        window.center()
        window.mode = None
        window.updateCursor()
        assert window.cursor().shape() == Qt.CursorShape.ArrowCursor

        window.switchToDrawMode()
        assert window.mode == "draw"
        window.switchToBlurMode()
        assert window.mode == "blur"
        window.switchToArrowMode()
        assert window.mode == "arrow"
        window.switchToCropMode()
        assert window.mode == "crop"

        monkeypatch.setattr(
            image_command_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor("red"),
        )
        window.selectColor()
        assert window.selectedColor == (0, 0, 255)

        monkeypatch.setattr(
            image_command_window.QColorDialog,
            "getColor",
            lambda *args, **kwargs: QColor(),
        )
        window.selectColor()

        item = window.imageListWidget.item(0)
        window.loadImageFromList(item)
        assert window.image is not None

        original_imread = image_command_window.cv2.imread
        monkeypatch.setattr(image_command_window.cv2, "imread", lambda path: None)
        window.loadImageFromList(QListWidgetItem("missing.png"))
        monkeypatch.setattr(image_command_window.cv2, "imread", original_imread)

        chosen = screenshots_dir / "first.png"
        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(chosen), "png"),
        )
        window.loadImage()
        assert window.image is not None

        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: ("", ""),
        )
        window.loadImage()

        window.temp_image = write_image(tmp_path / "temp.png")
        saved = tmp_path / "saved.png"
        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(saved), "png"),
        )
        window.saveImage()
        assert saved.exists()

        window.temp_image = None
        window.saveImage()

        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getExistingDirectory",
            lambda *args, **kwargs: str(extra_dir),
        )
        window.openDirectory()
        assert window.dir_path == str(extra_dir)
        assert window.imageListWidget.count() == 1

        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getExistingDirectory",
            lambda *args, **kwargs: "",
        )
        window.openDirectory()

        window.loadImagesFromDirectory(None)
        window.loadImagesFromDirectory(str(tmp_path / "missing-dir"))

        window.history = [np.zeros((5, 5, 3), dtype=np.uint8)]
        window.image = np.ones((5, 5, 3), dtype=np.uint8)
        window.undoLastChange()
        assert window.image.shape == (5, 5, 3)

        window.history = []
        window.original_image = np.full((4, 4, 3), 7, dtype=np.uint8)
        window.undoLastChange()
        assert window.image.shape == (4, 4, 3)

        action = image_command_window.QAction("Save", window)
        hits = []
        window.change_icon_temporarily(action, str(saved), str(saved))
        window.provide_feedback_and_execute(
            action,
            str(saved),
            str(saved),
            lambda: hits.append(True),
        )
        assert hits == [True]

        assert window.convertCvImageToQImage(None).isNull() is True
        monkeypatch.setattr(
            image_command_window.cv2,
            "cvtColor",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad image")),
        )
        assert window.convertCvImageToQImage(np.zeros((2, 2, 3), dtype=np.uint8)).isNull()
    finally:
        window.close()


def test_image_command_window_drawing_and_resize_paths(qapp, tmp_path, monkeypatch):
    window, manager = build_window(qapp, tmp_path, monkeypatch)
    source = Path(manager.load_config()["SCREENSHOTS_DIR"]) / "first.png"
    window.image = cv2.imread(str(source))
    window.original_image = window.image.copy()
    window.updateImageDisplay()

    try:
        assert window.temp_image is not None
        x, y = window.convertEventCoordsToImageCoords(FakeEvent(10, 12))
        assert x >= 0 and y >= 0

        original_map = window.imageLabel.mapFromGlobal
        monkeypatch.setattr(
            window.imageLabel,
            "mapFromGlobal",
            lambda point: (_ for _ in ()).throw(RuntimeError("bad point")),
        )
        assert window.convertEventCoordsToImageCoords(FakeEvent(1, 1)) == (None, None)
        monkeypatch.setattr(window.imageLabel, "mapFromGlobal", original_map)

        window.switchToDrawMode()
        window.handleLabelClick(FakeEvent(2, 2))
        assert window.isDrawing is True
        window.mouseMoveEvent(FakeEvent(12, 12))
        window.mouseReleaseEvent(FakeEvent(14, 14))
        assert window.history

        window.switchToArrowMode()
        window.handleLabelClick(FakeEvent(3, 3))
        window.mouseMoveEvent(FakeEvent(16, 16))
        window.mouseReleaseEvent(FakeEvent(18, 18))

        window.switchToBlurMode()
        window.handleLabelClick(FakeEvent(1, 1))
        window.mouseMoveEvent(FakeEvent(10, 10))
        window.applyEffect()

        window.switchToCropMode()
        window.handleLabelClick(FakeEvent(2, 2))
        window.mouseMoveEvent(FakeEvent(8, 8))
        window.mouseReleaseEvent(FakeEvent(9, 9))
        assert window.image.shape[0] <= window.original_image.shape[0]

        window.startPoint = (0, 0)
        window.endPoint = (2, 2)
        window.isDrawing = False
        window.applyCrop()

        monkeypatch.setattr(
            image_command_window.QInputDialog,
            "getInt",
            lambda *args, **kwargs: (5, True),
        )
        window.selectThickness()
        assert window.selectedThickness == 5

        assert window.getScaleFactors() != (0, 0)
        window.temp_image = None
        assert window.getScaleFactors() == (1, 1)

        window.image = None
        window.updateImageDisplay()
        window.image = np.zeros((500, 500, 3), dtype=np.uint8)
        window.original_image = window.image.copy()
        window.mode = "draw"
        window.isDrawing = True
        window.startPoint = QPoint(10, 10)
        window.endPoint = QPoint(100, 100)
        window.updateImageDisplay()
        assert window.displayed_image_width is not None

        window.mode = "blur"
        window.updateImageDisplay()
        window.mode = "arrow"
        window.updateImageDisplay()
        window.mode = "crop"
        window.updateImageDisplay()

        resize_event = QResizeEvent(QSize(1200, 900), QSize(1000, 800))
        window.resizeEvent(resize_event)

        window.temp_image = None
        window.resizeEvent(resize_event)
    finally:
        window.close()


def test_image_command_window_branch_and_error_paths(qapp, tmp_path, monkeypatch):
    window, manager = build_window(qapp, tmp_path, monkeypatch)
    errors = []
    screenshots_dir = Path(manager.load_config()["SCREENSHOTS_DIR"])
    source = screenshots_dir / "first.png"
    monkeypatch.setattr(image_command_window.logger, "error", errors.append)

    def raise_error(message):
        return lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(message))

    try:
        monkeypatch.setattr(window, "unsetCursor", raise_error("cursor failed"))
        window.mode = None
        window.updateCursor()

        monkeypatch.setattr(window, "updateCursor", raise_error("mode failed"))
        window.switchToCropMode()
        window.selectColor()
        window.switchToArrowMode()
        window.switchToDrawMode()
        window.switchToBlurMode()

        window.history = [np.zeros((5, 5, 3), dtype=np.uint8)]
        real_update_image_display = window.updateImageDisplay
        monkeypatch.setattr(window, "updateImageDisplay", raise_error("undo failed"))
        window.undoLastChange()
        monkeypatch.setattr(window, "updateImageDisplay", real_update_image_display)

        original_imread = image_command_window.cv2.imread
        monkeypatch.setattr(image_command_window.cv2, "imread", lambda path: None)
        window.loadImageFromList(QListWidgetItem("missing.png"))
        monkeypatch.setattr(image_command_window.cv2, "imread", original_imread)

        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(source), "png"),
        )
        monkeypatch.setattr(image_command_window.cv2, "imread", lambda path: None)
        window.loadImage()
        monkeypatch.setattr(image_command_window.cv2, "imread", original_imread)

        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getOpenFileName",
            raise_error("open failed"),
        )
        window.loadImage()

        extra_dir = tmp_path / "extra"
        extra_dir.mkdir()
        monkeypatch.setattr(
            image_command_window.QFileDialog,
            "getExistingDirectory",
            lambda *args, **kwargs: str(extra_dir),
        )
        monkeypatch.setattr(window.file_system_watcher, "removePath", raise_error("watcher failed"))
        window.openDirectory()

        monkeypatch.setattr(window, "convertEventCoordsToImageCoords", raise_error("coords failed"))
        window.handleLabelClick(FakeEvent(1, 1))

        monkeypatch.setattr(window, "convertEventCoordsToImageCoords", lambda event: (4, 5))
        window.isDrawing = False
        window.mode = "draw"
        window.mouseMoveEvent(FakeEvent(4, 5))
        assert window.hoverPoint == QPoint(4, 5)

        monkeypatch.setattr(window, "convertEventCoordsToImageCoords", raise_error("move failed"))
        window.mouseMoveEvent(FakeEvent(1, 1))

        monkeypatch.setattr(window, "convertEventCoordsToImageCoords", raise_error("release failed"))
        window.isDrawing = True
        window.mouseReleaseEvent(FakeEvent(1, 1))

        window.image = None
        window.startPoint = (0, 0)
        window.endPoint = (2, 2)
        window.isDrawing = False
        window.applyCrop()

        class BadImage:
            @property
            def shape(self):
                raise RuntimeError("shape failed")

        window.temp_image = BadImage()
        window.getScaleFactors()

        window.image = np.zeros((800, 800, 3), dtype=np.uint8)
        window.original_image = window.image.copy()
        window.image = np.zeros((4000, 4000, 3), dtype=np.uint8)
        window.original_image = window.image.copy()
        window.imageLabel.resize(50, 50)
        window.updateImageDisplay()
        assert window.displayed_image_width <= 50

        monkeypatch.setattr(window, "convertCvImageToQImage", raise_error("convert failed"))
        window.updateImageDisplay()

        monkeypatch.setattr(window, "updateImageDisplay", raise_error("resize failed"))
        window.temp_image = np.zeros((10, 10, 3), dtype=np.uint8)
        window.resizeEvent(QResizeEvent(QSize(1200, 900), QSize(1000, 800)))

        assert any("cursor failed" in message for message in errors)
        assert any("watcher failed" in message for message in errors)
        assert any("convert failed" in message for message in errors)
    finally:
        window.close()
