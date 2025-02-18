import json
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (QApplication, QGridLayout, QLabel, QLineEdit,
                             QMainWindow, QTextEdit, QToolBar, QWidget)

# Assuming these are set up correctly in your project
from . import constants
from .log_config import setup_logging
from .update_utils import return_path

# Placeholder for your logging setup


logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/engagement.log")


class EngagementWindow(QMainWindow):
    def __init__(self, engagement_file=None):
        super().__init__()
        self.setWindowTitle("Engagement")
        self.setObjectName("Engagement")
        self.setGeometry(100, 100, 800, 600)
        self.engagement_file = engagement_file
        self.data = {}
        try:
            logger.info("Initializing engagement window.")

            self.data = self.loadJsonData(engagement_file)
            self.initUI()
            self.createToolBar()
            logger.info("Engagement window initialized successfully.")
            with open(return_path("config/dark-stylesheet.css"), "r") as file:
                self.setStyleSheet(file.read())
        except Exception as e:
            logger.error(f"Failed to initialize engagement window: {e}")

    def loadJsonData(self, file_path):
        if file_path:
            try:
                with open(file_path, "r") as file:
                    data = json.load(file)
                    logger.info(
                        f"Successfully loaded JSON data {data} from {file_path}"
                    )
                    return data
            except Exception as e:
                logger.error(f"Failed to load JSON data from {file_path}: {e}")
                return {}
        else:
            logger.warning("No file path provided for loading JSON data.")
            return {}

    def initUI(self):
        centralWidget = QWidget()
        self.setCentralWidget(centralWidget)
        layout = QGridLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        # QLabel for "Engagement Name:"
        nameLabel = QLabel("Engagement Name:")
        layout.addWidget(
            nameLabel, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.nameEdit = QLineEdit(self.data.get("engagement_name", "N/A"))
        self.nameEdit.textChanged.connect(self.autoSaveJsonData)
        layout.addWidget(self.nameEdit, 0, 1)

        # QLabel for "IP Addresses (one per line):"
        ipLabel = QLabel("IP Addresses (one per line):")
        layout.addWidget(
            ipLabel, 1, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.ipEdit = QTextEdit()
        self.ipEdit.setText("\n".join(self.data.get("ip_addresses", [])))
        self.ipEdit.textChanged.connect(self.autoSaveJsonData)
        layout.addWidget(self.ipEdit, 1, 1)

        # QLabel for "URLs (one per line):"
        urlLabel = QLabel("URLs (one per line):")
        layout.addWidget(
            urlLabel, 2, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.urlEdit = QTextEdit()
        self.urlEdit.setText("\n".join(self.data.get("urls", [])))
        self.urlEdit.textChanged.connect(self.autoSaveJsonData)
        layout.addWidget(self.urlEdit, 2, 1)

        centralWidget.setLayout(layout)

    def createToolBar(self):
        self.toolbar = QToolBar("Main Toolbar")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        self.actionRefresh = QAction(QIcon("icons/refresh.png"), "Refresh")
        self.actionRefresh.triggered.connect(
            self.onRefreshClicked
        )  # Connect to a method
        self.toolbar.addAction(self.actionRefresh)

    def onRefreshClicked(self):
        # Placeholder for refresh action
        logger.info("Refresh clicked")
        self.loadJsonData(self.engagement_file)  # Reload data
        self.initUI()  # Reinitialize UI with the new data

    def autoSaveJsonData(self):
        """Automatically save the JSON data when any changes are made."""
        self.data["engagement_name"] = self.nameEdit.text()
        self.data["ip_addresses"] = self.ipEdit.toPlainText().strip().split("\n")
        self.data["urls"] = self.urlEdit.toPlainText().strip().split("\n")
        # Update item_states for checkboxes if needed
        self.saveJsonData()

    def saveJsonData(self):
        if self.engagement_file:
            try:
                with open(self.engagement_file, "w") as file:
                    json.dump(self.data, file, indent=4)
                    logger.info(
                        f"Successfully saved JSON data to {self.engagement_file}"
                    )
            except Exception as e:
                logger.error(f"Failed to save JSON data to {self.engagement_file}: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = EngagementWindow(
        "path/to/your/engagement.json"
    )  # Make sure this path is correct
    window.show()
    sys.exit(app.exec())
