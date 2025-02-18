import json
import sys
import warnings

from PyQt6.QtCore import (QObject, QRunnable, Qt, QThread, QThreadPool, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import (  # This module helps in opening URLs in the default browser
    QFont, QIcon)
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                             QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import configuration_manager, constants, utilities
from .log_config import setup_logging
from .MainWindow import Nebula
from .setup_nebula import settings
from .update_utils import return_path

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/InitialLogic.log")
warnings.filterwarnings("ignore")


class ErrorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Error")
        self.setGeometry(100, 100, 400, 100)
        self.setFont(QFont("Courier", 10))

        # Set dark theme styles
        self.setStyleSheet(
            """
            QDialog {
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                color: #FFFFFF;
            }
            QDialogButtonBox {
                background-color: #1e1e1e;
            }
            QPushButton {
                background-color: #333333;
                color: #FFFFFF;
                border: 1px solid #333333;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            QPushButton:pressed {
                background-color: #333333;
            }
        """
        )

        # Create layout and add widgets
        layout = QVBoxLayout()

        # Error message label
        message = "Something went wrong with selecting the engagement folder."
        label = QLabel(message)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        # Add standard buttons (OK) and connect the accepted signal to close the dialog
        buttons = QDialogButtonBox.StandardButton.Ok
        buttonBox = QDialogButtonBox(buttons)
        buttonBox.accepted.connect(self.accept)
        layout.addWidget(buttonBox)

        self.setLayout(layout)


class DisclaimerDialog(QDialog):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Disclaimer")
        self.setModal(True)
        self.setup_ui()

    def setup_ui(self, _=None):
        layout = QVBoxLayout()
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout()
        label = QLabel(
            """
                       
Software Disclaimer 

1. No Warranty: The Nebula ("Software") is provided "AS IS" and "AS AVAILABLE," without warranty of any kind. Berylliumsec expressly disclaims all warranties, whether express, implied, statutory, or otherwise, including but not limited to any warranties of merchantability, fitness for a particular purpose, and non-infringement. Berylliumsec does not warrant that the Software will meet your requirements, will be uninterrupted, timely, secure, or error-free, or that defects, if any, will be corrected.

2. Limitation of Liability: To the fullest extent permitted by applicable law, in no event shall Berylliumsec, its affiliates, directors, employees, agents, suppliers, or licensors be liable for any indirect, incidental, special, consequential or punitive damages, including without limitation, loss of profits, data, use, goodwill, or other intangible losses, resulting from (i) your access to or use of or inability to access or use the Software; (ii) any conduct or content of any third party on the Software; (iii) any content obtained from the Software; and (iv) unauthorized access, use or alteration of your transmissions or content, whether based on warranty, contract, tort (including negligence) or any other legal theory, whether or not we have been informed of the possibility of such damage, and even if a remedy set forth herein is found to have failed of its essential purpose.

3. Prohibited Use: You agree not to use the Software for any illegal or unauthorized purpose, or to engage in any activity that would violate the rights of Berylliumsec or others. Any unauthorized use of the Software is strictly prohibited and can lead to termination of your access to the Software and may subject you to legal penalties and consequences.

4. Governing Law: This disclaimer shall be governed by and construed in accordance with the laws of UNITED STATES, without regard to its conflict of law provisions.

5. Acknowledgement: By using the Software, you acknowledge that you have read this disclaimer and agree to its terms."""
        )
        label.setWordWrap(True)
        scroll_layout.addWidget(label)
        scroll_widget.setLayout(scroll_layout)
        scroll_area.setWidget(scroll_widget)
        layout.addWidget(scroll_area)
        button_layout = QHBoxLayout()

        self.agree_button = QPushButton("I Agree")
        self.agree_button.clicked.connect(self.accept)
        button_layout.addWidget(self.agree_button)

        self.disagree_button = QPushButton("I Disagree")
        self.disagree_button.clicked.connect(self.reject)
        button_layout.addWidget(self.disagree_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)
        with open(return_path("config/dark-stylesheet.css"), "r") as file:
            self.setStyleSheet(file.read())

    def exec_(self, _=None):
        if super().exec() == QDialog.DialogCode.Accepted:
            return True
        else:
            QMessageBox.warning(
                self,
                "Disagreement",
                "You must agree to the disclaimer to use this application.",
            )
            return False


class WorkerSignals(QObject):
    main_window_loaded = pyqtSignal(object)


class Worker(QRunnable):
    def __init__(self, signals: WorkerSignals, engagement_folder: str):
        super().__init__()
        self.signals = signals
        self.engagement_folder = engagement_folder

    def run(self):
        try:
            # Simulate a heavy loading task (replace this with actual data loading)
            QThread.sleep(5)  # Simulate a delay
            # Replace with actual data loading and preparation logic
            data = {"engagement_folder": self.engagement_folder}
            self.signals.main_window_loaded.emit(data)
        except Exception as e:
            logger.exception("An error occurred in Worker.run: %s", e)
            self.signals.main_window_loaded.emit(None)


class ProgressWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Loading..Please wait")
        self.setFixedSize(300, 100)

        self.progressBar = QProgressBar(self)
        self.progressBar.setGeometry(50, 40, 200, 25)
        self.progressBar.setRange(0, 0)  # Indeterminate mode

        layout = QVBoxLayout()
        layout.addWidget(self.progressBar)
        self.setLayout(layout)
        self.setStyleSheet("background-color: #2e2e2e; color: white;")

        self.center()
        self.show()

    def center(self):
        screen = QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())


class MainApplication(QApplication):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setupWindow = None
        self.mainWindow = None
        self.constants = None
        self.engagement_folder = None
        self.thread_pool = QThreadPool()
        self.worker_signals = WorkerSignals()
        self.worker_signals.main_window_loaded.connect(self.on_main_window_loaded)

        with open(return_path("config/dark-stylesheet.css"), "r") as file:
            self.setStyleSheet(file.read())

    def start(self):
        self.setApplicationName("Nebula")
        self.setOrganizationName("Beryllium")
        self.setWindowIcon(QIcon(return_path("Images/logo.png")))
        self.show_setup()

    def show_setup(self):
        self.config = configuration_manager.ConfigManager()
        self.setupWindow = settings()
        self.setupWindow.setupCompleted.connect(self.update_engagement_folder)
        self.setupWindow.show()

    def update_engagement_folder(self, text: str):
        logger.debug(f"Updating engagement folder as {text}")
        self.engagement_folder = text
        self.config.setengagement_folder(text)
        self.init_main_window()

    def init_main_window(self):
        try:
            logger.debug("Closing setup window")
            self.setupWindow.close()
            logger.debug("Now loading main window")

            self.progressWindow = ProgressWindow()

            worker = Worker(self.worker_signals, self.engagement_folder)
            self.thread_pool.start(worker)
        except Exception as e:
            logger.exception("An error occurred in init_main_window: %s", e)

    def on_main_window_loaded(self, data):
        logger.debug("main window loading signal received")
        if data:
            try:
                logger.debug("Closing progress window")
                # self.progressWindow.close()
                self.progressWindow.deleteLater()

                logger.debug("Showing main window")
                self.mainWindow = Nebula(
                    data["engagement_folder"]
                )  # Create the main window here
                self.mainWindow.show()
                self.progressWindow.close()
                QTimer.singleShot(0, self.splash_finished)
            except Exception as e:
                logger.exception("An error occurred: %s", e)
        else:
            logger.error("Failed to load main window")
            dialog = ErrorDialog()
            dialog.exec()
            sys.exit(0)


    def start_app_tour(self):
        experienced_user = utilities.check_initial_help()
        if not experienced_user:
            self.mainWindow.start_tour()
            self.mainWindow.search_area.setText(
                "Search for commands here using protocol names like SMB, FTP etc"
            )
            self.mainWindow.command_input_area.setText(
                "! Say hello to your AI Assistant"
            )
        else:
            logger.debug("Not showing first timers message")

    def splash_finished(self):
        logger.debug("Progress window finished, starting app tour")
        self.start_app_tour()


if __name__ == "__main__":
    app = MainApplication(sys.argv)
    app.start()
    sys.exit(app.exec())
