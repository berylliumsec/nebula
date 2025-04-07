import os

import cv2
from PyQt6 import QtCore
from PyQt6.QtCore import QFileSystemWatcher, QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QToolBar,
    QVBoxLayout,
)

from . import constants
from .image_display_label import ImageDisplayLabel
from .log_config import setup_logging
from .update_utils import return_path

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/image_command_window.log")

icons = {
    "add_image": return_path("Images/add_image.png"),
    "draw_box": return_path("Images/draw_box.png"),
    "blur_area": return_path("Images/blur.png"),
    "undo": return_path("Images/undo.png"),
    "save": return_path("Images/save.png"),
    "open_directory": return_path("Images/folder.png"),
    "draw_arrow": return_path("Images/arrow.png"),
    "select_color": return_path("Images/select_color.png"),
    "select_thickness": return_path("Images/thickness.png"),
    "crop": return_path("Images/crop.png"),
    "add_text": return_path("Images/write_text.png"),
    "text_size": return_path("Images/text_size.png"),
    "upper_case": return_path("Images/uppercase.png"),
    "lower_case": return_path("Images/lowercase.png"),
}


class ImageCommandWindow(QDialog):
    def __init__(self, stylesheet_path, manager):
        super().__init__()
        self.manager = manager
        self.CONFIG = self.manager.load_config()
        self.setWindowTitle("Image Editor")
        self.setGeometry(100, 100, 800, 600)
        self.setWindowFlags(Qt.WindowType.Window)

        self.selectedThickness = 1
        self.toolbar = QToolBar("Image Toolbar")
        self.toolbar.setIconSize(QtCore.QSize(18, 18))
        self.addImageAction = QAction(QIcon(icons["add_image"]), "Add Image", self)

        self.addImageAction.setToolTip("Add an image")
        self.addImageAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.addImageAction,
                return_path("Images/clicked.png"),
                return_path("Images/add_image.png"),
                self.loadImage,
            )
        )

        self.drawBoxAction = QAction(QIcon(icons["draw_box"]), "Draw Box", self)
        self.drawBoxAction.setToolTip("Draw a box on the image")
        self.drawBoxAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.drawBoxAction,
                return_path("Images/clicked.png"),
                return_path("Images/draw_box.png"),
                self.switchToDrawMode,
            )
        )

        self.blurAreaAction = QAction(QIcon(icons["blur_area"]), "Blur Area", self)
        self.blurAreaAction.setToolTip("Blur an area on the image")
        self.blurAreaAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.blurAreaAction,
                return_path("Images/clicked.png"),
                return_path("Images/blur.png"),
                self.switchToBlurMode,
            )
        )

        self.undoAction = QAction(QIcon(icons["undo"]), "Undo", self)
        self.undoAction.setShortcut("Ctrl+Z")
        self.undoAction.setToolTip("Undo the last change")
        self.undoAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.undoAction,
                return_path("Images/clicked.png"),
                return_path("Images/undo.png"),
                self.undoLastChange,
            )
        )

        self.saveAction = QAction(QIcon(icons["save"]), "Save", self)
        self.saveAction.setShortcut("Ctrl+S")
        self.saveAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.saveAction,
                return_path("Images/clicked.png"),
                return_path("Images/save.png"),
                self.saveImage,
            )
        )

        self.openDirAction = QAction(
            QIcon(icons["open_directory"]), "Open Directory", self
        )
        self.openDirAction.setToolTip("Open a directory")
        self.openDirAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.openDirAction,
                return_path("Images/clicked.png"),
                return_path("Images/folder.png"),
                self.openDirectory,
            )
        )

        self.drawArrowAction = QAction(QIcon(icons["draw_arrow"]), "Draw Arrow", self)
        self.drawArrowAction.setToolTip("Draw an arrow on the image")
        self.drawArrowAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.drawArrowAction,
                return_path("Images/clicked.png"),
                return_path("Images/arrow.png"),
                self.switchToArrowMode,
            )
        )

        self.selectColorAction = QAction(
            QIcon(icons["select_color"]), "Select Color", self
        )
        self.selectColorAction.setToolTip("Select the color for drawing")
        self.selectColorAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.selectColorAction,
                return_path("Images/clicked.png"),
                return_path("Images/select_color.png"),
                self.selectColor,
            )
        )

        self.selectThicknessAction = QAction(
            QIcon(icons["select_thickness"]), "Select Thickness", self
        )
        self.selectThicknessAction.setToolTip("Select the thickness for drawing")
        self.selectThicknessAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.selectThicknessAction,
                return_path("Images/clicked.png"),
                return_path("Images/thickness.png"),
                self.selectThickness,
            )
        )

        self.cropAction = QAction(QIcon(icons["crop"]), "Crop Image", self)
        self.cropAction.setToolTip("Crop the image")
        self.cropAction.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.cropAction,
                return_path("Images/clicked.png"),
                return_path("Images/crop.png"),
                self.switchToCropMode,
            )
        )

        self.toolbar.addAction(self.openDirAction)
        self.toolbar.addAction(self.addImageAction)
        self.toolbar.addAction(self.drawBoxAction)
        self.toolbar.addAction(self.blurAreaAction)
        self.toolbar.addAction(self.undoAction)
        self.toolbar.addAction(self.saveAction)
        self.toolbar.addAction(self.selectColorAction)
        self.toolbar.addAction(self.drawArrowAction)
        self.toolbar.addAction(self.selectThicknessAction)
        self.toolbar.addAction(self.cropAction)

        self.imageListWidget = QListWidget()
        self.imageListWidget.itemClicked.connect(self.loadImageFromList)

        self.imageLabel = ImageDisplayLabel()
        self.imageLabel.setPixmap(QPixmap())

        self.imageLabel.clicked.connect(self.handleLabelClick)

        self.image = None
        self.temp_image = None
        self.displayed_image_width = None
        self.displayed_image_height = None
        self.current_pixmap = None

        self.hoverPoint = None

        topLayout = QHBoxLayout()
        topLayout.addWidget(self.toolbar)

        centralLayout = QHBoxLayout()
        leftVerticalLayout = QVBoxLayout()
        leftVerticalLayout.addWidget(self.imageLabel, 3)
        centralLayout.addLayout(leftVerticalLayout, 75)
        centralLayout.addWidget(self.imageListWidget, 25)

        mainVerticalLayout = QVBoxLayout()
        mainVerticalLayout.addLayout(topLayout)
        mainVerticalLayout.addLayout(centralLayout)

        self.setLayout(mainVerticalLayout)

        self.history = []

        self.dir_path = os.path.join(self.CONFIG["SCREENSHOTS_DIR"])
        self.file_system_watcher = QFileSystemWatcher([self.dir_path])
        self.file_system_watcher.directoryChanged.connect(
            lambda: self.loadImagesFromDirectory(self.dir_path)
        )
        self.file_system_watcher.fileChanged.connect(
            lambda: self.loadImagesFromDirectory(self.dir_path)
        )

        self.loadImagesFromDirectory(self.dir_path)
        self.interactiveRect = None
        self.isResizing = False
        self.isMoving = False
        self.isDrawing = False
        self.startPoint = QPoint()
        self.endPoint = QPoint()
        self.mode = None
        self.image_path = None
        self.selectedColor = (255, 255, 0)
        if stylesheet_path:
            with open(stylesheet_path, "r") as f:
                self.setStyleSheet(f.read())
        self.setFixedSize(1400, 900)
        self.center()

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def updateCursor(self, _=None):
        try:
            if self.mode == "draw":
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self.mode == "arrow":
                self.setCursor(Qt.CursorShape.UpArrowCursor)
            elif self.mode == "blur":
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self.mode == "crop":
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.unsetCursor()
        except Exception as e:
            logger.error(f"An error occurred in updateCursor: {e}")

    def switchToCropMode(self, _=None):
        try:
            self.mode = "crop"
            self.updateCursor()
            self.isDrawing = False
        except Exception as e:
            logger.error(f"An error occurred in switchToCropMode: {e}")

    def selectColor(self, _=None):
        try:
            self.updateCursor()
            color = QColorDialog.getColor()
            if color.isValid():
                self.selectedColor = (color.blue(), color.green(), color.red())
        except Exception as e:
            logger.error(f"An error occurred in selectColor: {e}")

    def switchToArrowMode(self, _=None):
        try:
            self.mode = "arrow"
            self.updateCursor()
            self.isDrawing = False
        except Exception as e:
            logger.error(f"An error occurred in switchToArrowMode: {e}")

    def undoLastChange(self, _=None):
        try:
            if self.history:
                self.image = self.history.pop()
                self.updateImageDisplay()
            else:
                self.image = self.original_image.copy()
                self.updateImageDisplay()
        except Exception as e:
            logger.error(f"An error occurred in undoLastChange: {e}")

    def saveImage(self, _=None):
        try:
            if self.temp_image is None:
                raise ValueError("No temporary image to save")

            filename, _ = QFileDialog.getSaveFileName(
                self,
                "Save Image",
                self.CONFIG["SCREENSHOTS_DIR"],
                "Image Files (*.png *.jpg *.jpeg)",
            )
            if filename:
                cv2.imwrite(filename, self.temp_image)
                logger.debug(f"Image saved as {filename}")
        except Exception as e:
            logger.error(f"Error saving image: {e}")

    def openDirectory(self, _=None):
        try:
            self.CONFIG = self.manager.load_config()
            new_dir_path = QFileDialog.getExistingDirectory(
                self,
                "Open Directory",
                self.CONFIG["SCREENSHOTS_DIR"],
                QFileDialog.Option.ShowDirsOnly,
            )
            if new_dir_path:
                self.file_system_watcher.removePath(self.dir_path)
                self.dir_path = new_dir_path
                self.file_system_watcher.addPath(self.dir_path)
                self.loadImagesFromDirectory(self.dir_path)
        except Exception as e:
            logger.error(f"Error opening directory: {e}")

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

    def loadImagesFromDirectory(self, dir_path):
        if dir_path is None:
            dir_path = self.dir_path
        try:
            if not os.path.isdir(dir_path):
                raise FileNotFoundError(f"Directory not found: {dir_path}")

            self.imageListWidget.clear()
            for file_name in os.listdir(dir_path):
                if file_name.lower().endswith((".png", ".jpg", ".jpeg")):
                    self.imageListWidget.addItem(file_name)

        except Exception as e:
            logger.error(f"Error loading images from directory: {e}")

    def loadImageFromList(self, item):
        try:
            image_path = os.path.join(self.dir_path, item.text())
            self.image_path = image_path
            self.image = cv2.imread(image_path)
            self.original_image = self.image.copy()
            if self.image is None:
                raise ValueError(f"Failed to load the image from {image_path}")

            self.updateImageDisplay()

        except Exception as e:
            logger.error(f"Error loading image from list: {e}")

    def loadImage(self, _=None):
        try:
            fname, _ = QFileDialog.getOpenFileName(
                self, "Open file", "/home", "Image files (*.jpg *.gif *.png)"
            )
            if fname:
                self.image = cv2.imread(fname)
                if self.image is not None:
                    self.original_image = self.image.copy()
                    self.updateImageDisplay()
                    logger.debug(f"Image loaded from {fname}")
                else:
                    logger.error(
                        "Failed to load the image or unsupported image format."
                    )
        except Exception as e:
            logger.error(f"Error loading image: {e}")

    def switchToDrawMode(self, _=None):
        try:
            self.mode = "draw"
            self.updateCursor()
            self.isDrawing = False
        except Exception as e:
            logger.error(f"An error occurred in switchToDrawMode: {e}")

    def switchToBlurMode(self, _=None):
        try:
            self.mode = "blur"
            self.updateCursor()
            self.isDrawing = False
        except Exception as e:
            logger.error(f"An error occurred in switchToBlurMode: {e}")

    def convertEventCoordsToImageCoords(self, event):
        try:
            x = event.position().x()
            y = event.position().y()

            # Since there's no scrollbar, we adjust the calculation to directly use the event's x and y.
            # Assuming the image is centered within the widget, we calculate the offsets
            offset_x = (self.imageLabel.width() - self.displayed_image_width) // 2
            offset_y = (self.imageLabel.height() - self.displayed_image_height) // 2

            # Adjusting x and y based on the offsets if the image is centered
            widget_x = x - offset_x
            widget_y = y - offset_y

            scale_w, scale_h = self.getScaleFactors()
            image_x = int(widget_x * scale_w)
            image_y = int(widget_y * scale_h)

            # Ensure coordinates are not negative
            return max(0, image_x), max(0, image_y)
        except Exception as e:
            logger.error(f"Error in convertEventCoordsToImageCoords: {e}")
            return None, None

    def handleLabelClick(self, event):
        try:
            self.isDrawing = True
            image_x, image_y = self.convertEventCoordsToImageCoords(event)
            self.startPoint = QPoint(image_x, image_y)
            self.endPoint = self.startPoint
            self.updateImageDisplay()
        except Exception as e:
            logger.error(f"An error occurred in handleLabelClick: {e}")

    def mouseMoveEvent(self, event):
        try:
            image_x, image_y = self.convertEventCoordsToImageCoords(event)
            if self.isDrawing and self.mode == "arrow":
                self.endPoint = QPoint(image_x, image_y)
                self.updateImageDisplay()

            if not self.isDrawing:
                self.hoverPoint = QPoint(image_x, image_y)
            else:
                self.endPoint = QPoint(image_x, image_y)

            if self.isDrawing and self.mode == "crop":
                self.endPoint = self.convertEventCoordsToImageCoords(event)
                self.updateImageDisplay()

            self.updateImageDisplay()
        except Exception as e:
            logger.error(f"An error occurred in mouseMoveEvent: {e}")

    def mouseReleaseEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.LeftButton and self.isDrawing:
                self.isDrawing = False
                image_x, image_y = self.convertEventCoordsToImageCoords(event)
                self.endPoint = QPoint(image_x, image_y)
                self.applyEffect()

            if self.mode == "arrow" or self.mode == "crop":
                self.isDrawing = False
                image_x, image_y = self.convertEventCoordsToImageCoords(event)
                self.endPoint = QPoint(image_x, image_y)
                if self.mode == "arrow":
                    self.applyEffect()
                else:
                    self.applyCrop()

        except Exception as e:
            logger.error(f"An error occurred in mouseReleaseEvent: {e}")

    def applyCrop(self, _=None):
        if not self.isDrawing:
            try:
                if isinstance(self.startPoint, QPoint):
                    x1, y1 = self.startPoint.x(), self.startPoint.y()
                else:
                    x1, y1 = self.startPoint
                if isinstance(self.endPoint, QPoint):
                    x2, y2 = self.endPoint.x(), self.endPoint.y()
                else:
                    x2, y2 = self.endPoint
                self.image = self.image[
                    min(y1, y2) : max(y1, y2), min(x1, x2) : max(x1, x2)
                ]
                self.updateImageDisplay()

            except Exception as e:
                logger.error(f"An error occurred during cropping: {e}")

    def selectThickness(self, _=None):
        thickness, okPressed = QInputDialog.getInt(
            self, "Select Thickness", "Thickness:", 2, 1, 10, 1
        )
        if okPressed:
            self.selectedThickness = thickness

    def applyEffect(self, _=None):
        try:
            self.history.append(self.image.copy())

            if self.mode == "arrow":
                start_point = (self.startPoint.x(), self.startPoint.y())
                end_point = (self.endPoint.x(), self.endPoint.y())
                cv2.arrowedLine(
                    self.image,
                    start_point,
                    end_point,
                    self.selectedColor,
                    self.selectedThickness,
                    cv2.LINE_AA,
                )
            elif self.mode == "draw":
                start_point = (self.startPoint.x(), self.startPoint.y())
                end_point = (self.endPoint.x(), self.endPoint.y())
                cv2.rectangle(
                    self.image,
                    start_point,
                    end_point,
                    self.selectedColor,
                    self.selectedThickness,
                )
            elif self.mode == "blur":
                start_point = (self.startPoint.x(), self.startPoint.y())
                end_point = (self.endPoint.x(), self.endPoint.y())
                roi = self.image[
                    start_point[1] : end_point[1], start_point[0] : end_point[0]
                ]
                kernel_size = (5, 5)
                self.image[
                    start_point[1] : end_point[1], start_point[0] : end_point[0]
                ] = cv2.blur(roi, kernel_size)

            self.updateImageDisplay()
        except Exception as e:
            logger.error(f"An error occurred in applyEffect: {e}")

    def getScaleFactors(self, _=None):
        try:
            if self.temp_image is None:
                return 1, 1

            original_width = self.temp_image.shape[1]
            original_height = self.temp_image.shape[0]

            scale_w = (
                original_width / self.displayed_image_width
                if self.displayed_image_width > 0
                else 1
            )
            scale_h = (
                original_height / self.displayed_image_height
                if self.displayed_image_height > 0
                else 1
            )

            return scale_w, scale_h
        except Exception as e:
            logger.error(f"An error occurred in getScaleFactors: {e}")
            return 1, 1

    def updateImageDisplay(self, _=None):
        self.imageLabel.setFixedSize(QtCore.QSize(1024, 768))

        try:
            if self.image is None:
                return

            self.temp_image = self.image.copy()

            if self.isDrawing:
                start_point_tuple, end_point_tuple = None, None
                if isinstance(self.startPoint, QPoint):
                    start_point_tuple = (self.startPoint.x(), self.startPoint.y())
                if isinstance(self.endPoint, QPoint):
                    end_point_tuple = (self.endPoint.x(), self.endPoint.y())

                if self.mode == "crop":
                    cv2.rectangle(
                        self.temp_image,
                        start_point_tuple or self.startPoint,
                        end_point_tuple or self.endPoint,
                        self.selectedColor,
                        1,
                    )

                if self.mode == "draw":
                    cv2.rectangle(
                        self.temp_image,
                        start_point_tuple or self.startPoint,
                        end_point_tuple or self.endPoint,
                        self.selectedColor,
                        self.selectedThickness,
                    )
                elif self.mode == "blur":
                    if (
                        start_point_tuple[1] < end_point_tuple[1]
                        and start_point_tuple[0] < end_point_tuple[0]
                    ):
                        roi = self.temp_image[
                            start_point_tuple[1] : end_point_tuple[1],
                            start_point_tuple[0] : end_point_tuple[0],
                        ]
                        kernel_size = (25, 25)
                        self.temp_image[
                            start_point_tuple[1] : end_point_tuple[1],
                            start_point_tuple[0] : end_point_tuple[0],
                        ] = cv2.blur(roi, kernel_size)
                elif self.mode == "arrow":
                    cv2.arrowedLine(
                        self.temp_image,
                        start_point_tuple or self.startPoint,
                        end_point_tuple or self.endPoint,
                        self.selectedColor,
                        self.selectedThickness,
                        cv2.LINE_AA,
                    )

            q_img = self.convertCvImageToQImage(self.temp_image)
            originalPixmap = QPixmap.fromImage(q_img)

            labelSize = self.imageLabel.size()
            blackBackgroundPixmap = QPixmap(labelSize)
            blackBackgroundPixmap.fill(QColor("black"))

            # Calculate the scaled size while maintaining aspect ratio
            scaledSize = originalPixmap.size().scaled(
                labelSize, Qt.AspectRatioMode.KeepAspectRatio
            )
            self.displayed_image_width, self.displayed_image_height = (
                scaledSize.width(),
                scaledSize.height(),
            )
            logger.debug(
                f"Displayed Image Height: {self.displayed_image_height}, Displayed Image Width: { self.displayed_image_width}"
            )

            # Calculate the position to center the image on the black background
            x = (labelSize.width() - self.displayed_image_width) // 2
            y = (labelSize.height() - self.displayed_image_height) // 2

            # Use QPainter to draw the original image onto the center of the black background
            painter = QPainter(blackBackgroundPixmap)
            painter.drawPixmap(
                x,
                y,
                originalPixmap.scaled(
                    scaledSize,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            )
            painter.end()

            # Set the pixmap with the centered image and black background to the label
            self.imageLabel.setPixmap(blackBackgroundPixmap)

        except Exception as e:
            logger.error(f"An error occurred in updateImageDisplay: {e}")

    def convertCvImageToQImage(self, cv_img):
        if cv_img is None:
            logger.debug("Received an empty image.")
            return QImage()

        try:
            rgb_image = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_image.shape
            bytes_per_line = ch * w
            convert_to_Qt_format = QImage(
                rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
            )
            return convert_to_Qt_format.copy()
        except Exception as e:
            logger.error(f"Error in converting CV image to QImage: {e}")
            return QImage()

    def resizeEvent(self, event):
        try:
            if self.temp_image is not None:
                self.updateImageDisplay()
            super().resizeEvent(event)
        except Exception as e:
            logger.error(f"An error occurred in resizeEvent: {e}")
