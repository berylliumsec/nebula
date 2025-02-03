import json
import os
import re
import shutil
import threading
import time
import warnings
from queue import Queue

import requests
from PyQt6 import QtCore
from PyQt6.QtCore import (QFile, QFileSystemWatcher, QObject, QPoint,
                          QRunnable, QSize, Qt, QThread, QThreadPool, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import (  # This module helps in opening URLs in the default browser
    QAction, QGuiApplication, QIcon, QPixmap, QTextCursor)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                             QDialog, QDialogButtonBox, QFileDialog, QFrame,
                             QHBoxLayout, QInputDialog, QLabel, QListWidget,
                             QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                             QPushButton, QRadioButton, QSizePolicy, QToolBar,
                             QToolTip, QVBoxLayout, QWidget)

from . import constants, eclipse, tool_configuration, update_utils, utilities
from .ai_notes_pop_up_window import AiNotes, AiNotesPopupWindow
from .central_display_area_in_main_window import CentralDisplayAreaInMainWindow
from .configuration_manager import ConfigManager
from .eclipse_window import EclipseWindow
from .engagement import EngagementWindow
from .help import HelpWindow
from .image_command_window import ImageCommandWindow
from .log_config import setup_logging
from .search import CustomSearchLineEdit
from .suggestions_pop_out_window import SuggestionsPopOutWindow
from .terminal_emulator import CommandInputArea, TerminalEmulatorWindow
from .update_utils import return_path
from .user_note_taking import UserNoteTaking
from .utilities import encoding_getter, token_counter, tokenizer

warnings.filterwarnings("ignore")


logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/MainWindow.log")


class FileProcessorSignal(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(Exception)


class IndexFileTask(QRunnable):
    """
    QRunnable class for indexing a file.
    """

    def __init__(self, file_path: str, index_line_function):
        super().__init__()
        self.file_path = file_path
        self.index_line_function = index_line_function

    def run(self):
        logger.info(f"Indexing file for search: {self.file_path}")
        try:
            with open(self.file_path, "r") as file:
                for line in file:
                    self.index_line_function(line.strip())
            utilities.show_message(
                "Indexing Complete",
                "The file has been successfully indexed for search.",
            )
        except Exception as e:
            logger.error(f"Failed to index file: {e}")


class InsightsProcessorWorker(QObject):
    def __init__(self, file_path, gate_way_endpoint, command_input_area):
        super().__init__()
        self.file_path = file_path
        self.gate_way_endpoint = gate_way_endpoint
        self.command_input_area = command_input_area
        self.insights_queue = Queue()
        self.signals = FileProcessorSignal()

    def run(self):
        logger.debug("Starting insights processing")
        self.load_insights_into_queue()

        if not self.insights_queue.empty():
            self.process_next_insight()
        else:
            logger.debug("No insights to process.")
            self.signals.finished.emit()

    def load_insights_into_queue(self):
        logger.debug("Processing insights")

        try:
            insights = None  # Initialize insights

            if utilities.is_nessus_file(self.file_path):
                logger.debug("Processing nessus file in eco mode")
                insights = utilities.parse_nessus_file(self.file_path)
            elif utilities.is_zap_file(self.file_path):
                logger.debug("Processing zap file in eco mode")
                insights = utilities.parse_zap(self.file_path)
            elif utilities.is_nmap_file(self.file_path):
                logger.debug("Processing nmap file in eco mode")
                insights = utilities.parse_nmap(self.file_path)
            elif utilities.is_nikto_file(self.file_path):
                logger.debug("Processing nikto file in eco mode")
                insights = utilities.parse_nikto_xml(self.file_path)

            if insights is not None:
                self.insights_queue.put(insights)

        except Exception as e:
            logger.error(f"Error processing file in eco mode {self.file_path}: {e}")
            self.signals.error.emit(e)

    def process_next_insight(self):
        try:
            if not self.insights_queue.empty():
                next_insight = self.insights_queue.get()
                logger.debug(f"Processing next insight: {next_insight}")

                self.command_input_area.execute_api_call(
                    next_insight, self.gate_way_endpoint
                )
                self.command_input_area.api_tasks += 1
            else:
                logger.debug("No more insights in queue.")
                self.signals.finished.emit()  # Emit finished signal if all insights are processed
        except Exception as e:
            logger.error(f"Error in process_next_insight: {e}")
            self.signals.error.emit(e)

    def on_api_call_finished(self):
        logger.debug("API call finished, checking for next insight.")
        self.process_next_insight()


class FileProcessorWorker(QObject):
    def __init__(
        self,
        file_path: str,
        gate_way_endpoint: str,
        command_input_area,  # The specific type should be used here if known
    ):
        super(FileProcessorWorker, self).__init__()
        self.file_path = file_path
        self.gate_way_endpoint = gate_way_endpoint
        self.command_input_area = command_input_area
        self.signals = FileProcessorSignal()
        self.chunks = []
        self.chunks_queue = Queue()
        self.command_input_area.api_call_execution_finished.connect(
            self.on_api_call_finished
        )
        self.halt_processing = False

        self.encoding_type = "gpt-4"

    def run(self):
        logger.debug("Starting file processing")
        self.load_chunks_into_queue()

        if not self.chunks_queue.empty():
            self.process_next_chunk()
        else:
            logger.debug("Queue is empty")
            self.signals.finished.emit()

    def load_chunks_into_queue(self):
        logger.debug(f"Starting to process file: {self.file_path}")

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                file_contents = f.read()

                try:
                    self._process_data(file_contents)
                except Exception as e:
                    logger.error(
                        f"Error processing file in non-web mode{self.file_path}: {e}"
                    )
        except Exception as e:
            logger.error(f"Error processing file {self.file_path}: {e}")

    def _process_data(self, file_contents: str):
        logger.debug("Loading chunks into queue in basic autonomous mode")
        token_count = token_counter(file_contents, self.encoding_type)

        if token_count > 8000:
            chunks = self.split_into_chunks_by_tokens(file_contents, 2400)
            for chunk in chunks:
                self.chunks_queue.put(chunk)
            if self.halt_processing:
                logger.debug("Stop processing command received")
                self.chunks_queue.get()
                self.chunks_queue.task_done()
                return
        else:
            self.command_input_area.queued_autonomous_items_for_api = 0
            logger.debug(
                f"Finished chunking, sending to API, gateway endpoint is {self.gate_way_endpoint}"
            )
            self.command_input_area.execute_api_call(
                file_contents, self.gate_way_endpoint
            )

    def _extract_content(self, chunk: str, start_placeholder: str) -> str:
        content = chunk.split(start_placeholder)[1].strip()
        logger.debug(f"Raw content after stripping placeholders: {content}")
        return content

    def process_next_chunk(self):
        try:
            if not self.chunks_queue.empty():
                next_chunk = self.chunks_queue.get()
                logger.debug("Processing next chunk")
                num_items = self.chunks_queue.qsize()
                logger.debug(f"Number of items to be processed is {num_items}")
                if self.autonomous_mode:
                    self.command_input_area.queued_autonomous_items_for_api = num_items
                    logger.debug(f"Setting autonomous_queue to {num_items}")
                self.command_input_area.execute_api_call(
                    next_chunk, self.gate_way_endpoint
                )
            else:
                logger.debug("No more chunks in queue.")
                self.signals.finished.emit()
        except Exception as e:
            logger.error(f"Error in process_next_chunk: {e}")
            self.signals.error.emit(e)

    def on_api_call_finished(self):
        logger.debug("API call finished, checking for next chunk.")
        self.process_next_chunk()

    def split_into_chunks_by_tokens(self, content: str, max_tokens: int) -> list:
        tokens = tokenizer(content, self.encoding_type)
        chunks = []
        current_chunk = []

        for token in tokens:
            current_chunk.append(token)
            if len(current_chunk) >= max_tokens:
                chunks.append(current_chunk)
                current_chunk = []

        if current_chunk:
            chunks.append(current_chunk)

        encoding = encoding_getter(self.encoding_type)
        string_chunks = [encoding.decode(chunk) for chunk in chunks]

        return string_chunks


class PreviousResults(QListWidget):
    send_to_ai_notes_signal = pyqtSignal(str, str)
    send_to_ai_suggestions_signal = pyqtSignal(str, str)
    send_to_autonomous_ai_signal = pyqtSignal(str, str)

    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.lastHoverPos = QPoint()
        self.manager = manager
        self.CONFIG = self.manager.load_config()

    def contextMenuEvent(self, event):
        self.lastHoverPos = event.pos()

        context_menu = QMenu(self)
        context_menu.setStyleSheet(
            """
            QMenu::item:selected {
                background-color:#333333; 
            }
        """
        )
        self.send_to_ai_notes_action = QAction("Send to AI Notes", self)
        self.send_to_ai_notes_action.triggered.connect(self.send_to_ai_notes)
        context_menu.addAction(self.send_to_ai_notes_action)

        self.send_to_ai_suggestions_action = QAction("Send to AI Suggestions", self)
        self.send_to_ai_suggestions_action.triggered.connect(
            self.send_to_ai_suggestions
        )
        context_menu.addAction(self.send_to_ai_suggestions_action)
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(self.delete_file)
        context_menu.addAction(delete_action)

        delete_all_files_action = QAction("Delete All files", self)
        delete_all_files_action.triggered.connect(self.delete_all_files)
        context_menu.addAction(delete_all_files_action)
        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(self.rename_file)
        context_menu.addAction(rename_action)

        context_menu.exec(event.globalPos())

    def confirm_delete(self, file_path):
        msgBox = QMessageBox()
        msgBox.setWindowTitle("Delete File")
        msgBox.setText(
            f"Are you sure you want to delete '{os.path.basename(file_path)}'?"
        )
        msgBox.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msgBox.setDefaultButton(QMessageBox.StandardButton.No)
        msgBox.setStyleSheet("QMessageBox { background-color: #2b2b2b; color: white; }")

        response = msgBox.exec()
        if response == QMessageBox.StandardButton.Yes:
            return True
        else:
            return False

    def confirm_delete_all_files(self, _=None):
        msgBox = QMessageBox()
        msgBox.setWindowTitle("Delete All Files")
        msgBox.setText("Are you sure you want to delete all files?")
        msgBox.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msgBox.setDefaultButton(QMessageBox.StandardButton.No)
        msgBox.setStyleSheet("QMessageBox { background-color: #2b2b2b; color: white; }")

        response = msgBox.exec()
        if response == QMessageBox.StandardButton.Yes:
            return True
        else:
            return False

    def delete_all_files(self, _=None):
        self.CONFIG = self.manager.load_config()
        log_directory = self.CONFIG["LOG_DIRECTORY"]

        if self.confirm_delete_all_files():
            try:
                for file_name in os.listdir(log_directory):
                    file_path = os.path.join(log_directory, file_name)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logger.debug(f"File deleted: {file_path}")
                logger.debug("All files in the log directory have been deleted.")
            except Exception as e:
                logger.error(f"Failed to delete files: {e}")
        else:
            logger.debug("Deletion of all files cancelled.")

    def delete_file(self, _=None):
        self.CONFIG = self.manager.load_config()
        selected_item = self.currentItem()
        file_name = selected_item.text()
        file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)

        if os.path.exists(file_path):
            if self.confirm_delete(file_path):
                try:
                    os.remove(file_path)
                    logger.debug(f"File deleted: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete file: {e}")
            else:
                logger.debug("File deletion cancelled.")
        else:
            logger.warning(f"File not found: {file_path}")

    def send_to_ai_suggestions(self, _=None):
        selected_item = self.currentItem()
        if selected_item:
            file_name = selected_item.text()
            file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)
            self.send_to_ai_suggestions_signal.emit(file_path, "suggestion_files")

    def send_to_autonomous_ai(self, _=None):
        selected_item = self.currentItem()
        if selected_item:
            file_name = selected_item.text()
            file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)

            self.send_to_ai_notes_signal.emit(file_path, "autonomous")

    def send_to_ai_notes(self, _=None):
        selected_item = self.currentItem()
        if selected_item:
            file_name = selected_item.text()
            file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)

            self.send_to_ai_notes_signal.emit(file_path, "notes_files")

    def rename_file(self, _=None):
        self.CONFIG = self.manager.load_config()
        selected_item = self.currentItem()
        if selected_item:
            file_name = selected_item.text()
            file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)

            # Ask the user for a new file name
            new_file_name, ok = QInputDialog.getText(
                self, "Rename File", "Enter new file name:", text=file_name
            )
            if ok and new_file_name:
                new_file_path = os.path.join(
                    self.CONFIG["LOG_DIRECTORY"], new_file_name
                )
                if not os.path.exists(new_file_path):
                    try:
                        os.rename(file_path, new_file_path)
                        selected_item.setText(
                            new_file_name
                        )  # Update the list item to reflect the new file name
                        logger.debug(f"File renamed: {file_path} to {new_file_path}")
                    except Exception as e:
                        logger.error(f"Failed to rename file: {e}")
                        QMessageBox.critical(
                            self, "Error", "Failed to rename the file."
                        )
                else:
                    QMessageBox.warning(
                        self, "Rename File", "A file with this name already exists."
                    )


class SpacerWidget(QWidget):
    def __init__(self, _=None):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)


class NebulaPro(QMainWindow):

    autonomous_mode_status = pyqtSignal(bool)
    web_autonomous_mode_status = pyqtSignal(bool)
    model_signal = pyqtSignal(bool)
    main_window_loaded = pyqtSignal(bool)

    def __init__(self, engagement_folder=None):
        super().__init__()
        logger.debug("begin showing main window")
        self.engagement_details_file = None
        self.help_actions = []
        if engagement_folder:
            logger.debug(f"engagement folder has been set to {engagement_folder}")
            self.engagement_details_file = os.path.join(
                engagement_folder, "engagement_details.json"
            )
        self.manager = ConfigManager(engagement_folder)
        self.CONFIG = self.manager.load_config()

        self.child_windows = (
            []
        )  # Initialize an empty list to keep track of child windows
        self.eclipse_model_path = os.path.join(
            self.CONFIG["ECLIPSE_DIRECTORY"], "ner_model_bert"
        )
        if not os.path.exists(self.eclipse_model_path):
            eclipse.ensure_model_folder_exists(self.eclipse_model_path)
        self.prev_results_list = PreviousResults(manager=self.manager)
        self.child_windows.append(self.prev_results_list)
        self.auth = None

        num_cores = os.cpu_count()
        self.threadPool = QThreadPool()
        self.threadPool.setMaxThreadCount(num_cores)
        self.prev_results_list.send_to_ai_notes_signal.connect(
            self.process_new_file_with_ai
        )
        self.prev_results_list.send_to_autonomous_ai_signal.connect(
            self.process_new_file_with_ai
        )

        self.prev_results_list.send_to_ai_suggestions_signal.connect(
            self.process_new_file_with_ai
        )
        self.prev_results_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.prev_results_list.setSpacing(5)
        self.prev_results_list.setMaximumWidth(300)

        self.prev_results_list.setObjectName("prevResultsList")
        self.prev_results_list.itemClicked.connect(self.on_file_item_clicked)

        self.populate_file_list()
        self.file_system_watcher = QFileSystemWatcher([self.CONFIG["LOG_DIRECTORY"]])
        self.file_system_watcher.directoryChanged.connect(self.populate_file_list)

        self.dark_mode = True

        self.current_font_size = 10
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))

        self.main_layout = QHBoxLayout()
        self.v_layout = QVBoxLayout()

        self.command_in_progress = False
        self.suggestions_layout = QHBoxLayout()
        self.suggestions_layout.setContentsMargins(
            0, 0, 0, 0
        )  # Reduce the top margin to bring the label closer to the toolbar

        self.search_area = CustomSearchLineEdit()
        self.search_area.setFixedHeight(40)
        self.search_area.setPlaceholderText("Search")
        self.search_area.setObjectName("searchArea")
        self.search_area.setToolTip("Search for commands")
        self.search_area.resultSelected.connect(self.on_search_result_selected)
        self.suggestions_layout.addWidget(self.search_area, 9)

        self.suggestions_button = QPushButton(self)
        self.suggestions_button.setIconSize(QSize(24, 24))
        self.suggestions_not_available_icon_path = return_path(
            "Images/suggestion_not_available.png"
        )
        self.suggestions_available_icon_path = return_path(
            "Images/suggestion_available.png"
        )
        self.suggestions_button.setIcon(QIcon(self.suggestions_not_available_icon_path))
        self.suggestions_button.clicked.connect(
            lambda: self.provide_feedback_and_execute(
                self.suggestions_button,
                return_path("Images/clicked.png"),
                return_path("Images/suggestion_not_available.png"),
                self.open_suggestions_pop_out_window,
            )
        )

        self.suggestions_button.setFixedHeight(40)
        self.suggestions_button.setToolTip("View Suggestions")

        self.suggestions_layout.addWidget(self.suggestions_button, 1)

        self.command_input_area = CommandInputArea(manager=self.manager)

        self.command_input_area.setFixedHeight(50)
        self.command_input_area.setObjectName("commandInputArea")
        self.command_input_area.setToolTip(
            "Enter a command, start your command with ! for API calls, ? for feedback and ?? to report a bug"
        )

        self.command_input_area.updateCentralDisplayArea.connect(
            self.update_terminal_output
        )
        self.command_input_area.updateCentralDisplayAreaForApi.connect(
            self.update_terminal_output_for_api
        )
        self.command_input_area.update_suggestions_notes.connect(
            self.update_suggestions_notes
        )

        self.command_input_area.update_ai_notes.connect(self.update_ai_notes)

        self.command_input_area.terminal.busy.connect(self.update_clear_button_state)
        self.central_display_area = CentralDisplayAreaInMainWindow(
            parent=self,
            manager=self.manager,
            command_input_area=self.command_input_area,
        )

        self.central_display_area.notes_signal_from_central_display_area.connect(
            self.command_input_area.execute_api_call
        )
        self.central_display_area.suggestions_signal_from_central_display_area.connect(
            self.command_input_area.execute_api_call
        )
        self.suggestions_pop_out_window = SuggestionsPopOutWindow(
            manager=self.manager, command_input_area=self.command_input_area
        )

        self.central_display_area.setObjectName("centralDisplayArea")
        self.child_windows.append(self.central_display_area)
        self.child_windows.append(self.suggestions_pop_out_window)
        self.upload_icon_path = return_path(("Images/upload.png"))

        self.clear_button = utilities.LongPressButton()
        self.clear_button.setFixedHeight(50)
        self.clear_button.setObjectName("clearButton")
        self.clear_button.setToolTip("Clear the display area")
        self.clear_button_icon_path = return_path(("Images/clear.png"))
        self.clear_button.clicked.connect(self.clear_screen)
        self.clear_button.longPressed.connect(self.reset_terminal)
        self.clear_button.longPressProgress.connect(
            self.change_clear_button_icon_temporarily
        )

        self.clear_button.setIcon(QIcon(self.clear_button_icon_path))
        self.upload_button = QPushButton()
        self.upload_button.setFixedHeight(50)
        self.upload_button.setObjectName("uploadButton")
        self.upload_button.setToolTip("Upload a file for analysis")
        self.upload_button.clicked.connect(self.upload_file)
        self.upload_button.setIcon(QIcon(self.upload_icon_path))
        self.input_frame = QFrame()
        self.input_frame.setObjectName("inputFrame")
        self.input_frame_layout = QHBoxLayout(self.input_frame)
        self.input_frame_layout.addWidget(self.clear_button)
        self.input_frame_layout.addWidget(self.command_input_area)
        self.input_frame_layout.addWidget(self.upload_button)
        self.middle_frame = QFrame()
        self.middle_frame.setObjectName("middleFrame")
        self.middle_frame_layout = QVBoxLayout(self.middle_frame)
        self.middle_frame_layout.addLayout(self.suggestions_layout)
        self.middle_frame_layout.addWidget(self.central_display_area)

        disclaimer_text = "AI can make mistakes. Consider cross-checking suggestions."
        disclaimer_label = QLabel(disclaimer_text)
        disclaimer_label.setStyleSheet(
            "color: white; font-size: 10px; font-family: Courier;border: none; background-color: None"
        )

        # Add the disclaimer label to the layout with horizontal centering
        self.middle_frame_layout.addWidget(
            disclaimer_label, alignment=Qt.AlignmentFlag.AlignCenter
        )

        self.toolbar = QToolBar("Central Toolbar")
        self.toolbar.setFixedHeight(30)  # Set the height to 50 pixels

        self.addToolBar(self.toolbar)
        self.toolbar.setIconSize(QtCore.QSize(18, 18))

        self.increase_font_action = QAction("Click here to Increase Font Size", self)

        self.help_actions.append(self.increase_font_action)
        self.toolbar.addAction(self.increase_font_action)
        self.decrease_font_action = QAction("Click here to Decrease Font Size", self)
        self.help_actions.append(self.decrease_font_action)
        self.increase_font_icon = QIcon(return_path("Images/increase_font.png"))
        self.increase_font_action.setIcon(self.increase_font_icon)
        self.decrease_font_icon = QIcon(return_path("Images/decrease_font.png"))
        self.decrease_font_action.setIcon(self.decrease_font_icon)

        self.toolbar.addAction(self.decrease_font_action)

        self.ai_note_taking_action = QAction(
            "Click here to Activate AI Note Taking", self
        )
        self.help_actions.append(self.ai_note_taking_action)
        self.ai_note_taking_action.setCheckable(True)
        self.ai_notes_off_icon = QIcon(return_path("Images/ai_note_taking_off.png"))
        self.ai_notes_on_icon = QIcon(return_path("Images/ai_note_taking_on.png"))
        self.ai_note_taking_action.setIcon(self.ai_notes_off_icon)
        self.toolbar.addAction(self.ai_note_taking_action)
        self.suggestions_action = QAction("Click Here to Activate AI Suggestions", self)
        self.toolbar.addAction(self.suggestions_action)
        self.help_actions.append(self.suggestions_action)
        self.suggestions_action.setCheckable(True)
        self.suggestions_on_icon_path = return_path("Images/suggestions_on.png")
        self.suggestions_off_icon_path = return_path("Images/suggestions_off.png")
        self.suggestions_action.setIcon(QIcon(self.suggestions_off_icon_path))
        self.suggestions_action.triggered.connect(self.update_suggestions_display)

        self.increase_font_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.increase_font_action,
                return_path("Images/clicked.png"),
                return_path("Images/increase_font.png"),
                lambda: self.adjust_font_size(1),
            )
        )

        self.decrease_font_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.decrease_font_action,
                return_path("Images/clicked.png"),
                return_path("Images/decrease_font.png"),
                lambda: self.adjust_font_size(-1),
            )
        )

        self.ai_note_taking_action.triggered.connect(self.ai_note_taking_function)

        self.open_image_command_window_action = QAction(
            "Click Here to Open the Image Editing Window", self
        )
        self.help_actions.append(self.open_image_command_window_action)
        self.image_edit_icon_path = return_path("Images/image_edit.png")

        self.open_image_command_window_action.setIcon(QIcon(self.image_edit_icon_path))

        self.open_image_command_window_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.open_image_command_window_action,
                return_path("Images/clicked.png"),
                return_path("Images/image_edit.png"),
                self.open_image_command_window,
            )
        )

        self.toolbar.addAction(self.open_image_command_window_action)

        self.open_image_command_window_action.triggered.connect(
            self.open_image_command_window
        )
        self.screenshot_action = QAction("Click Here to Take a Screenshot", self)
        self.help_actions.append(self.screenshot_action)
        self.toolbar.addAction(self.screenshot_action)

        self.screenshot_icon_path = return_path("Images/screenshot.png")
        self.screenshot_action.setIcon(QIcon(self.screenshot_icon_path))
        self.screenshot_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.screenshot_action,
                return_path("Images/clicked.png"),
                return_path("Images/screenshot.png"),
                self.take_screenshot,
            )
        )

        self.terminal_action = QAction("Click Here to Open a New Terminal", self)
        self.help_actions.append(self.terminal_action)
        self.toolbar.addAction(self.terminal_action)
        self.terminal_icon = QIcon(return_path("Images/terminal.png"))
        self.terminal_action.setIcon(self.terminal_icon)

        self.terminal_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.terminal_action,
                return_path("Images/clicked.png"),
                return_path("Images/terminal.png"),
                self.openTerminalEmulator,
            )
        )

        self.select_tools_action = QAction(
            QIcon(return_path("Images/tools.png")),
            "Click Here to Choose Tools for Output Logging",
            self,
        )
        self.help_actions.append(self.select_tools_action)
        self.select_tools_action.setStatusTip(
            "Click Here to Choose Tools for Output Logging"
        )

        self.select_tools_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.select_tools_action,
                return_path("Images/clicked.png"),
                return_path("Images/tools.png"),
                self.open_tools_window,
            )
        )
        self.toolbar.addAction(self.select_tools_action)
        self.note_taking_editor = QAction(
            QIcon(), "Click Here to Take Notes Manually", self
        )
        self.help_actions.append(self.note_taking_editor)
        self.note_taking_editor_icon = QIcon(return_path("Images/text_editor.png"))
        self.note_taking_editor.setIcon(self.note_taking_editor_icon)

        self.note_taking_editor.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.note_taking_editor,
                return_path("Images/clicked.png"),
                return_path("Images/text_editor.png"),
                self.open_note_taking,
            )
        )

        self.toolbar.addAction(self.note_taking_editor)

        self.ai_notes = AiNotes(
            bookmarks_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"],
                "ai_notes_bookmarks.bookmarks",
            ),
            file_path=self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"] + "/ai_notes.html",
            manager=self.manager,
            command_input_area=self.command_input_area,
            search_window=self.search_area,
        )

        self.ai_notes.setObjectName("notesBox")
        if not self.ai_notes.toHtml():
            self.ai_notes.setHtml("AI notes will be displayed here")

        self.known_files = set()
        self.load_known_files()
        self.toolbar.addAction(self.note_taking_editor)
        self.help_actions.append(self.note_taking_editor)
        self.pop_out_button = QPushButton()
        self.pop_out_button_icon_path = return_path("Images/pop_out.png")
        self.pop_out_button.setIcon(QIcon(self.pop_out_button_icon_path))
        self.pop_out_button.setToolTip("Expand")
        self.pop_out_button.setFixedSize(30, 30)

        self.pop_out_button.clicked.connect(
            lambda: self.provide_feedback_and_execute(
                self.pop_out_button,
                return_path("Images/clicked.png"),
                return_path("Images/pop_out.png"),
                self.pop_out_notes,
            )
        )

        self.notes_top_layout = QHBoxLayout()
        self.notes_top_layout.addWidget(
            self.pop_out_button,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )

        self.notes_frame = QFrame()
        self.notes_frame_layout = QVBoxLayout(self.notes_frame)
        self.notes_frame_layout.addLayout(self.notes_top_layout)
        self.notes_frame_layout.addWidget(self.ai_notes)
        self.v_layout.addWidget(self.middle_frame)
        self.v_layout.addWidget(self.input_frame)
        self.v_layout_two = QVBoxLayout()
        self.v_layout_two.addWidget(self.notes_frame)

        self.v_layout_two.setStretch(0, 1)
        self.v_layout_two.setStretch(1, 1)

        self.main_layout.addWidget(self.prev_results_list)
        self.main_layout.addLayout(self.v_layout)
        self.main_layout.addLayout(self.v_layout_two)

        self.main_layout.setStretch(0, 1)  # Stretch factor for the prev_results_list
        self.main_layout.setStretch(
            1, 3
        )  # Stretch factor for the v_layout (main content area)
        self.main_layout.setStretch(
            2, 1
        )  # Stretch factor for the v_layout_two (notes area)

        self.central_widget = QWidget()
        self.central_widget.setLayout(self.main_layout)
        self.setCentralWidget(self.central_widget)
        self.resize(1320, 700)
        self.ai_file_watcher = QFileSystemWatcher([self.CONFIG["LOG_DIRECTORY"]])
        self.ai_file_watcher.directoryChanged.connect(self.on_directory_changed)

        self.eco_mode = QAction("Click Here to Activate Eco mode", self)
        self.help_actions.append(self.eco_mode)
        self.eco_mode.setCheckable(True)
        self.eco_mode_on_icon = QIcon(return_path("Images/eco_mode_on.png"))
        self.eco_mode_off_icon = QIcon(return_path("Images/eco_mode_off.png"))
        self.eco_mode.setIcon(self.eco_mode_off_icon)
        self.eco_mode.triggered.connect(self.update_eco_mode_display)
        self.toolbar.addAction(self.eco_mode)

        self.api_progress_action = QAction(
            QIcon(return_path("Images/work_complete.png")),
            "This Indicates that one of the AI Models is Busy or Stuck",
            self,
        )
        self.help_actions.append(self.api_progress_action)
        self.toolbar.addAction(self.api_progress_action)

        self.command_input_area.threads_status.connect(self.setThreadStatus)

        self.eclipse_action = QAction(
            QIcon(), "Click Here to Open the Eclipse Window", self
        )
        self.help_actions.append(self.eclipse_action)
        self.toolbar.addAction(self.eclipse_action)

        self.eclipse_icon = QIcon(return_path("Images/eclipse.png"))
        self.eclipse_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.eclipse_action,
                return_path("Images/clicked.png"),
                return_path("Images/eclipse.png"),
                self.open_eclipse,
            )
        )

        self.eclipse_action.setIcon(self.eclipse_icon)
        self.eclipse_action.setToolTip("Click Here to Open The Eclipse Window")

        self.engagement = QAction(
            QIcon(), "Click Here to Modify Engagement Details", self
        )
        self.help_actions.append(self.engagement)
        self.toolbar.addAction(self.engagement)

        self.engagement_icon = QIcon(return_path("Images/engagement.png"))
        self.engagement.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.engagement,
                return_path("Images/clicked.png"),
                return_path("Images/engagement.png"),
                self.open_engagement,
            )
        )

        self.engagement.setIcon(self.engagement_icon)
        self.engagement.setToolTip("Click Here to Modify Engagement Details")
        self.toolbar.addAction(self.engagement)

        self.bring_windows_to_front_action = QAction(
            QIcon(), "Click Here to Reveal all Open Windows", self
        )
        self.help_actions.append(self.bring_windows_to_front_action)
        self.bring_windows_to_front_icon = QIcon(return_path("Images/windows.png"))
        self.bring_windows_to_front_action.setIcon(self.bring_windows_to_front_icon)
        self.bring_windows_to_front_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.bring_windows_to_front_action,
                return_path("Images/clicked.png"),
                return_path("Images/windows.png"),
                self.bring_windows_to_front,
            )
        )
        self.bring_windows_to_front_action.setToolTip("Bring all windows to the front")
        self.toolbar.addAction(self.bring_windows_to_front_action)
        self.help = QAction(QIcon(), "Click Here to View Nebula's Manual", self)
        self.help_actions.append(self.help)
        self.help_icon = QIcon(return_path("Images/help.png"))
        self.help.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.help,
                return_path("Images/clicked.png"),
                return_path("Images/help.png"),
                self.open_help,
            )
        )

        self.help.setIcon(self.help_icon)
        self.help.setToolTip("Help")
        self.toolbar.addAction(self.help)

        self.tour = QAction(QIcon(), "Click Here to Tour the Main Tool Bar", self)
        self.help_actions.append(self.tour)
        self.tour_icon = QIcon(return_path("Images/tour.png"))
        self.tour.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                self.tour,
                return_path("Images/clicked.png"),
                return_path("Images/tour.png"),
                self.open_tour,
            )
        )

        self.tour.setIcon(self.tour_icon)
        self.tour.setToolTip("tour")
        self.toolbar.addAction(self.tour)

        self.terminal_emulator_number = 0
        self.note_taking_editor.triggered.connect(self.open_note_taking)

        self.textEditor = UserNoteTaking(
            manager=self.manager, command_input_area=self.command_input_area
        )
        self.child_windows.append(self.textEditor)
        self.worker_thread = None
        self.size_threshold = 1 * 1024 * 1024

        self.lock = threading.Lock()
        self.image_command_window = ImageCommandWindow(
            return_path("config/dark-stylesheet.css"), self.manager
        )
        self.child_windows.append(ImageCommandWindow)
        self.worker_threads = {}
        self.updateSearchAction = QAction(
            QIcon(return_path("Images/search_off.png")),
            "Click to toggle search on or off",
            self,
        )
        self.toolbar.addAction(self.updateSearchAction)
        self.help_actions.append(self.updateSearchAction)
        self.updateSearchAction.setCheckable(True)
        self.updateSearchAction.toggled.connect(
            lambda state: self.updatePreference("USE_INTERNET_SEARCH", state)
        )

        self.icons_path = "path/to/tools/icons"  # Adjust to your icons path
        self.tools_window = tool_configuration.ToolsWindow(
            self.CONFIG.get("AVAILABLE_TOOLS", []),
            self.CONFIG.get("SELECTED_TOOLS", []),
            self.icons_path,
            self.update_selected_tools,
            self.add_new_tool,  # Add callback for adding a new tool
            self,
        )
        self.autonomous_mode = False

        self.start_up = True

        engagement_json = {}
        window_title = "Nebula"
        self.worker = None
        try:
            engagement_json = self.loadJsonData(self.engagement_details_file)
        except Exception as e:
            logger.error(f"unable to load engagement details {e}")
        if engagement_json:
            window_title = window_title + " - " + engagement_json["engagement_name"]

        self.setWindowTitle(window_title)
        self.model_signal.connect(self.command_input_area.create_model)
        self.model_signal.emit(True)
        self.center()
        logger.debug("centered application")
        self.current_action_index = 0
        self.tour_timer = QTimer()
        self.tour_timer.setSingleShot(True)
        logger.debug("Starting tour")
        self.tour_timer.timeout.connect(self.next_step)
        self.main_window_loaded.emit(True)
        logger.debug("main window loaded")

    def open_tour(self):
        self.current_action_index = 0
        self.show_message("Let's take a tour of the main toolbar.")
        QTimer.singleShot(
            1000,
            lambda: self.highlight_action(self.help_actions[self.current_action_index]),
        )


    def updatePreference(self, preference: str, value: bool):
        # Load the current configuration.
        self.CONFIG = self.manager.load_config()
        self.engagement_folder = self.CONFIG["ENGAGEMENT_FOLDER"]

        # Build the path to the config.json file.
        self.CONFIG_FILE_PATH = os.path.join(self.engagement_folder, "config.json")

        # Read the existing JSON data from config.json, if it exists.
        if os.path.exists(self.CONFIG_FILE_PATH):
            with open(self.CONFIG_FILE_PATH, "r") as file:
                data = json.load(file)
        else:
            data = {}

        # Update the configuration with the new value.
        data[preference] = value
        logger.debug(f"Dumping preference data: {data}")

        # Write the updated data back to config.json.
        with open(self.CONFIG_FILE_PATH, "w") as file:
            json.dump(data, file, indent=4)

        # Update the icon based on the toggle state.
        if preference == "USE_INTERNET_SEARCH":
            icon_file = "Images/search_on.png" if value else "Images/search_off.png"
            new_icon = QIcon(return_path(icon_file))
            self.updateSearchAction.setIcon(new_icon)

    def enable_all_tools(self):
        self.tools_window.select_all_tools()

    def start_tour(self):
        reply = self.create_centered_message_box(
            "Welcome",
            "Welcome to Nebula! Would you like to take a tour of the main toolbar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.show_message("Let's take a tour of the main toolbar.")
            QTimer.singleShot(
                1000,
                lambda: self.highlight_action(
                    self.help_actions[self.current_action_index]
                ),
            )
        else:
            self.show_message(
                "Tour skipped. Hover over the icons to discover their functions, click the 'Help' icon, or click the 'Tour' icon to start the tour. Enjoy using Nebula!"
            )

    def create_centered_message_box(self, title, text, buttons, default_button):
        message_box = QMessageBox(QMessageBox.Icon.Question, title, text, buttons, self)
        message_box.setDefaultButton(default_button)
        # Center the message box
        message_box.move(self.center_message_box(message_box))
        return message_box.exec()

    def center_message_box(self, message_box):
        screen_geometry = QGuiApplication.primaryScreen().geometry()
        message_box_geometry = message_box.frameGeometry()
        center_point = screen_geometry.center()
        message_box_geometry.moveCenter(center_point)
        return message_box_geometry.topLeft()

    def next_step(self):
        self.unhighlight_action(self.help_actions[self.current_action_index])
        self.current_action_index += 1
        if self.current_action_index < len(self.help_actions):
            self.highlight_action(self.help_actions[self.current_action_index])
        else:
            self.show_message(
                "Tour completed! Hover over the icons to discover their functions, click 'Help' for assistance, or retake the tour by clicking its icon. Enjoy using Nebula!"
            )

    def highlight_action(self, action: QAction):
        action_widget = self.toolbar.widgetForAction(action)
        if action_widget:  # Ensure the widget exists
            action_widget.setStyleSheet(
                "background-color: rgba(173, 216, 230, 0.5);"
            )  # Semi-transparent blue

            self.show_tooltip(action_widget, f"{action.text()}")
            self.tour_timer.start(3000)  # Show each step for 2 seconds

    def unhighlight_action(self, action: QAction):
        action_widget = self.toolbar.widgetForAction(action)
        if action_widget:  # Ensure the widget exists
            action_widget.setStyleSheet("")

    def show_tooltip(self, action_widget, message: str):
        QToolTip.showText(
            action_widget.mapToGlobal(action_widget.rect().center()),
            message,
            action_widget,
        )

    def show_message(self, message: str):
        QMessageBox.information(self, "Main ToolBar Tour", message)

    def halt_autonomous_jobs(self):
        if self.worker:
            self.worker.halt_processing = True

    def reset_terminal(self):
        self.command_input_area.terminal.password_mode.emit(False)
        self.command_input_area.terminal.reset_terminal()
        self.command_input_area.currentCommandIndex = 0
        self.command_input_area.commands = []
        self.command_input_area.number_of_autonomous_commands = 0
        self.command_input_area.queued_autonomous_items_for_api = 0
        self.command_input_area.terminal.autonomous_terminal_execution_iteration_is_done.emit()
        self.command_input_area.terminal.busy.emit(False)
        self.central_display_area.clear()

    def bring_windows_to_front(self):
        for window in self.child_windows:
            try:
                window.show()  # Ensure the window is visible
                window.raise_()  # Bring the window to the front
                window.activateWindow()  # Give the window focus
            except Exception as e:
                logger.debug(f"Error bringing window to front: {e}")
                # Optionally, remove the window from the list if it's no longer valid
                self.child_windows.remove(window)

    def update_clear_button_state(self, is_busy):
        if is_busy:
            # Change the button's icon, functionality, and tooltip when the terminal is busy
            logger.debug("terminal is busy, changing clear button functionality")
            self.clear_button.setIcon(
                QIcon(return_path("Images/stop.png"))
            )  # Update path
            self.clear_button.clicked.disconnect()
            self.clear_button.clicked.connect(self.stop_terminal_operations)
            self.clear_button.setToolTip("Stop Terminal Operations")
        else:
            # Revert the button's icon, functionality, and tooltip when the terminal is not busy
            logger.debug(
                "terminal is no longer busy, changing clear button functionality back to normal"
            )
            self.clear_button.setIcon(
                QIcon(return_path("Images/clear.png"))
            )  # Update path
            self.clear_button.clicked.disconnect()
            self.clear_button.clicked.connect(self.clear_screen)
            self.clear_button.setToolTip("Clear the display area, Long press to reset")

    def change_clear_button_icon_temporarily(self, data):
        if data:
            # Change the button's icon, functionality, and tooltip when the terminal is busy
            logger.debug("button is long pressed, changing icon temporarily")
            self.clear_button.setIcon(
                QIcon(return_path("Images/clicked.png"))
            )  # Update path

        else:
            # Revert the button's icon, functionality, and tooltip when the terminal is not busy
            logger.debug("button is no longer long pressed, changing icon back")
            self.clear_button.setIcon(
                QIcon(return_path("Images/clear.png"))
            )  # Update path

    def stop_terminal_operations(self):
        logger.debug("Stopping Terminal Operations")
        self.command_input_area.terminal.write("<Ctrl-C>")
        self.command_input_area.terminal.write("<Ctrl-\\>")

    def loadJsonData(self, file_path):
        try:
            with open(file_path, "r") as file:
                data = json.load(file)
            logger.info(f"Successfully loaded data from {file_path}.")
            return data
        except Exception as e:
            logger.error(f"Failed to load data from {file_path}: {e}")
            return {}

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

    def setThreadStatus(self, data):
        if data == "in_progress":
            self.api_progress_action.setIcon(
                QIcon(return_path("Images/work_in_progress.png"))
            )
        elif data == "completed":
            self.api_progress_action.setIcon(
                QIcon(return_path("Images/work_complete.png"))
            )

    def open_engagement(self):
        engagement_window = None
        try:
            engagement_window = EngagementWindow(
                engagement_file=self.engagement_details_file
            )
        except Exception as e:
            logger.error(
                f"Engagement file not found :{e}, file is {self.engagement_details_file}"
            )
        try:
            self.child_windows.append(engagement_window)
            engagement_window.show()
        except Exception as e:
            logger.error(
                f"An error occurred while trying to open the engagement window {e}"
            )

    def open_eclipse(self):
        try:
            eclipse_window = EclipseWindow(
                command_input_area=self.command_input_area, manager=self.manager
            )
        except Exception as e:
            logger.error(f"Unable to open eclipse {e}")
        try:
            self.child_windows.append(eclipse_window)
            eclipse_window.show()
        except Exception as e:
            logger.error(
                f"An error occurred while trying to open the engagement window {e}"
            )

    def open_tools_window(self):
        self.tools_window.show()

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def add_new_tool(self, tool_name):
        if "AVAILABLE_TOOLS" not in self.CONFIG:
            self.CONFIG["AVAILABLE_TOOLS"] = []
        if (
            tool_name not in self.CONFIG["AVAILABLE_TOOLS"]
        ):  # Check if the tool doesn't already exist
            self.CONFIG["AVAILABLE_TOOLS"].append(tool_name)
            self.save_config(self.CONFIG)
            if hasattr(
                self, "tools_window"
            ):  # Check if tools_window has been initialized
                self.tools_window.update_config(
                    self.CONFIG.get("AVAILABLE_TOOLS", []),
                    self.CONFIG.get("SELECTED_TOOLS", []),
                )

    def update_selected_tools(self, selected_tools):
        self.CONFIG["SELECTED_TOOLS"] = selected_tools
        self.save_config(self.CONFIG)  # Save the updated configuration to the file

    def save_config(self, config):
        try:
            with open(config["CONFIG_FILE_PATH"], "w") as f:
                json.dump(config, f, indent=4)
            logger.debug("Configuration saved successfully.")
        except Exception as e:
            logger.debug(f"Error saving configuration: {e}")

    def show_message_dialog(self, message):
        message_box = QMessageBox(self)
        message_box.setWindowTitle("Configuration Error")
        message_box.setText(message)
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.exec()

    def clear_screen(self, _=None):
        self.command_input_area.terminal.write("reset \n")

    def open_help(self, _=None):
        self.help_window = HelpWindow()
        self.child_windows.append(self.help_window)
        self.help_window.show()

    def closeEvent(self, event):
        # Close all child windows first
        for window in self.child_windows:
            try:
                window.close()
            except Exception as e:
                logger.debug(f"Error closing window {window}: {e}")
        event.accept()  # Proceed with the main window's closure

    def load_known_files(self, _=None):
        try:
            self.known_files = set(os.listdir(self.CONFIG["LOG_DIRECTORY"]))
        except FileNotFoundError:
            utilities.show_message(
                "Not found",
                f"Directory not found: {self.CONFIG['LOG_DIRECTORY']}\nPlease fix the 'LOG_DIRECTORY' in settings.",
            )
        except PermissionError:
            utilities.show_message(
                "Permission",
                f"Permission denied when accessing: {self.CONFIG['LOG_DIRECTORY']}\nPlease check the directory permissions.",
            )
        except Exception as e:
            utilities.show_message(
                "Permission",
                f"An error occurred while listing {self.CONFIG['LOG_DIRECTORY']}: {e}",
            )

    def show_error_message(self, message):
        QMessageBox.critical(self, "Error", message)

    def on_search_result_selected(self, result):
        self.central_display_area.insertPlainText(result)

    def update_eco_mode_display(self, _=None):
        if self.eco_mode.isChecked():
            self.eco_mode.setIcon(self.eco_mode_on_icon)

        else:
            self.eco_mode.setIcon(self.eco_mode_off_icon)

    def update_suggestions_display(self, _=None):
        if self.suggestions_action.isChecked():
            self.statusBar().showMessage(
                "AI suggestions has been activated",
                6000,
            )
            self.suggestions_action.setIcon(QIcon(self.suggestions_on_icon_path))

        else:
            self.suggestions_action.setIcon(QIcon(self.suggestions_off_icon_path))

    def update_terminal_output(self, data):
        if "\x1b[2J" in data or "\x1bc" in data:
            self.central_display_area.clear()
        else:
            self.central_display_area.moveCursor(QTextCursor.MoveOperation.End)
            last_line = utilities.show_last_line(self.central_display_area.document())
            if not self.central_display_area.toPlainText().endswith(
                "\n"
            ) and not re.search(constants.CUSTOM_PROMPT_PATTERN, last_line):
                self.central_display_area.insertPlainText("\n")
            self.central_display_area.insertPlainText(data)

            self.central_display_area.moveCursor(QTextCursor.MoveOperation.End)

    def update_terminal_output_for_api(self, data):
        self.central_display_area.moveCursor(QTextCursor.MoveOperation.End)
        last_line = utilities.show_last_line(self.central_display_area.document())
        if not self.central_display_area.toPlainText().endswith("\n") and not re.search(
            constants.CUSTOM_PROMPT_PATTERN, last_line
        ):
            self.central_display_area.insertPlainText("\n")
        data = utilities.process_text(data)
        self.central_display_area.insertPlainText(data)

    def open_note_taking(self, _=None):
        self.textEditor.show()

    def openTerminalEmulator(self, _=None):
        self.terminal_emulator_number += 1
        self.terminalWindow = TerminalEmulatorWindow(
            self,
            manager=self.manager,
            terminal_emulator_number=self.terminal_emulator_number,
            model=self.model,
        )
        self.model_signal.connect(self.terminalWindow.command_input_area.create_model)
        self.child_windows.append(self.terminalWindow)

        self.terminalWindow.command_input_area.update_ai_notes.connect(
            self.update_ai_notes
        )
        self.terminalWindow.command_input_area.update_suggestions_notes.connect(
            self.update_suggestions_notes
        )

        self.terminalWindow.command_input_area.threads_status.connect(
            self.setThreadStatus
        )

        self.autonomous_mode_status.connect(
            self.terminalWindow.command_input_area.set_autonomous_mode
        )

        self.autonomous_mode_status.connect(
            self.terminalWindow.command_input_area.terminal.set_autonomous_mode
        )
        self.terminalWindow.show()

    def open_suggestions_pop_out_window(self, _=None):
        self.suggestions_button.setIcon(QIcon(self.suggestions_not_available_icon_path))
        self.suggestions_pop_out_window.show()

    def open_image_command_window(self, _=None):
        self.image_command_window.show()

    def ai_note_taking_function(self, checked):
        if checked:
            self.ai_note_taking_action.setIcon(self.ai_notes_on_icon)
            self.statusBar().showMessage(
                "AI Note taking has been activated",
                6000,
            )
        else:
            self.ai_note_taking_action.setIcon(self.ai_notes_off_icon)

    def take_screenshot(self, _=None):
        self.CONFIG = self.manager.load_config()
        self.pixmap = QPixmap(self.central_display_area.size())
        self.central_display_area.render(self.pixmap)

        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Screenshot",
            self.CONFIG["SCREENSHOTS_DIR"],
            "PNG Files (*.png);;All Files (*)",
        )
        if filename:
            self.pixmap.save(filename)

    def adjust_font_size(self, delta):
        self.current_font_size += delta
        self.central_display_area.set_font_size_for_copy_button(self.current_font_size)
        updated_stylesheet = re.sub(
            r"font-size: \d+pt;",
            f"font-size: {self.current_font_size}pt;",
            self.original_stylesheet,
        )
        self.setStyleSheet(updated_stylesheet)

    def on_file_item_clicked(self, item):
        try:
            if not item or not item.text():
                raise ValueError("Invalid item selected")

            file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], item.text())
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            if os.path.getsize(file_path) > self.size_threshold:
                self.show_large_file_warning()
                return

            self.command_input_area.execute_command(f"cat {file_path}")
            logger.debug(f"Executing command on file:  {file_path}")

        except Exception as e:
            logger.error(f"Error processing file item click: {e}")

    def show_large_file_warning(self, _=None):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("The file is too large for display.")
        msg.setInformativeText(
            "Please choose a smaller file (< 1 MB) or open it with an external editor."
        )
        msg.setWindowTitle("File Too Large")
        msg.setStyleSheet("QMessageBox { background-color: #333; color: white; }")
        msg.exec()

    def populate_file_list(self, _=None):
        try:
            self.prev_results_list.clear()
            self.CONFIG = self.manager.load_config()
            log_directory = self.CONFIG["LOG_DIRECTORY"]
            if os.path.exists(log_directory):
                # Create a list of (filename, modification_time) tuples
                files_with_mtime = [
                    (filename, os.path.getmtime(os.path.join(log_directory, filename)))
                    for filename in os.listdir(log_directory)
                    if os.path.isfile(os.path.join(log_directory, filename))
                ]

                # Sort the list by modification time in descending order
                files_sorted_by_mtime = sorted(
                    files_with_mtime, key=lambda x: x[1], reverse=True
                )

                # Add sorted files to the list
                for filename, _ in files_sorted_by_mtime:
                    item = QListWidgetItem(filename)
                    self.prev_results_list.addItem(item)
            else:
                raise FileNotFoundError(f"Directory not found: {log_directory}")

        except Exception as e:
            logger.error(f"Error populating file list: {e}")

    def upload_file(self, _=None):
        """Initiate the file upload process and handle the user's choice."""
        try:
            choice = self.get_user_choice()
            if choice:
                file_path = self.get_file_path()
                if file_path:
                    if choice == "AI Processing":
                        self.process_file(file_path)
                    elif choice == "Index for Search":
                        self.index_file(file_path)
        except Exception as e:
            logger.error(f"An error occurred: {e}")

    def get_user_choice(self) -> str:
        """Present a dialog to the user to choose between AI processing and indexing for search."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Action")

        layout = QVBoxLayout()

        ai_radio = QRadioButton("AI Processing")
        index_radio = QRadioButton("Index for Search")
        ai_radio.setChecked(True)  # Default selection

        button_group = QButtonGroup(dialog)
        button_group.addButton(ai_radio)
        button_group.addButton(index_radio)

        layout.addWidget(ai_radio)
        layout.addWidget(index_radio)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        dialog.setLayout(layout)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            if ai_radio.isChecked():
                return "AI Processing"
            elif index_radio.isChecked():
                return "Index for Search"
        return None

    def get_file_path(self) -> str:
        """Prompt the user to select a file from the Downloads folder."""
        downloads_folder = os.path.join(os.path.expanduser("~"), "Downloads")
        file_dialog = QFileDialog(self)
        file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_dialog.setNameFilter("All Files (*.*)")
        file_dialog.setDirectory(downloads_folder)
        if file_dialog.exec() == QFileDialog.DialogCode.Accepted:
            return file_dialog.selectedFiles()[0]
        return None

    def process_file(self, file_path):
        if not os.path.exists(self.CONFIG["LOG_DIRECTORY"]):
            os.makedirs(self.CONFIG["LOG_DIRECTORY"])

        file_name = os.path.basename(file_path)
        destination_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file_name)

        if not os.path.exists(destination_path):
            shutil.copy(file_path, destination_path)
        else:
            logger.debug(f"File already exists: {destination_path}")

        logger.debug(f"File copied to: {destination_path}")

    def index_file(self, file_path: str):
        """Index the file for search by reading its content and processing each line."""
        task = IndexFileTask(file_path, self.index_line)
        self.threadPool.start(task)

    def index_line(self, line: str):
        """Index a single line of text."""
        try:
            indexdir = update_utils.return_path("command_search_index")
            self.search_area.add_to_index(line, indexdir)
            logger.info(f"Indexed line: {line}")
        except Exception as e:
            logger.error(f"Failed to index line: {line} - {e}")

    def pop_out_notes(self, _=None):
        self.CONFIG = self.manager.load_config()
        self.pop_out_window = AiNotesPopupWindow(
            notes_file_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], "/ai_notes.html"
            ),
            manager=self.manager,
            command_input_area=self.command_input_area,
        )
        self.pop_out_window.setTextInTextEdit(self.ai_notes.toHtml())
        self.pop_out_window.textUpdated.connect(self.update_main_notes)
        self.pop_out_window.show()

    def update_main_notes(self, text):
        self.ai_notes.setHtml(text)
        cursor = self.ai_notes.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.ai_notes.setTextCursor(cursor)
        self.ai_notes.repaint()

    def load_stylesheet(self, filename):
        style_file = QFile(filename)
        style_file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text)
        self.original_stylesheet = style_file.readAll().data().decode("utf-8")
        self.setStyleSheet(self.original_stylesheet)

    def on_directory_changed(self, path):
        logger.debug(f"Directory change detected: {path}")
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Directory not found: {path}")
            logger.debug("Directory exists. Scanning for new files.")

            current_files = set(os.listdir(path))
            new_files = current_files - self.known_files
            if new_files:
                logger.info(f"New files detected: {new_files}")
                for file in new_files:

                    logger.debug(f"Processing file: {file}")
                    if self.suggestions_action.isChecked():
                        self.process_new_file_with_ai(file, "suggestion_files")
                        logger.debug(f"File processed for suggestions: {file}")
                    if self.ai_note_taking_action.isChecked():
                        self.process_new_file_with_ai(file, "notes_files")
                        logger.debug(f"File processed for AI note-taking: {file}")

            else:
                logger.debug("No new files found.")

            self.known_files = current_files
            logger.debug("Updated known files list.")

        except FileNotFoundError as fnf_error:
            logger.error(f"FileNotFoundError: {fnf_error}")
        except Exception as e:
            logger.error(f"Error processing directory change: {e}")
        finally:
            logger.debug("Finished processing directory change.")

    def update_ai_notes(self, data):
        start_time = time.time()
        logger.debug("Updating AI Notes")
        html_data = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html_data = html_data.replace("\n", "<br>")
        self.ai_notes.append_text(html_data)

        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating AI Notes took {duration} seconds.")

    def update_suggestions_notes(self, data):
        self.suggestions_pop_out_window.update_suggestions(data)
        self.suggestions_button.setIcon(QIcon(self.suggestions_available_icon_path))

    def process_new_file_with_ai(self, file, gate_way_endpoint):
        file_path = os.path.join(self.CONFIG["LOG_DIRECTORY"], file)
        logger.debug("Processing file with AI: %s", file)

        if not hasattr(self, "file_thread_counters"):
            self.file_thread_counters = {}

        self.file_thread_counters[file] = self.file_thread_counters.get(file, 0) + 1

        unique_file_key = f"{file}_{self.file_thread_counters[file]}"

        try:
            if self.eco_mode.isChecked():
                logger.debug("Eco mode activated")
                self.statusBar().showMessage(
                    "Eco mode has been activated",
                    6000,
                )
                worker = InsightsProcessorWorker(
                    file_path, gate_way_endpoint, self.command_input_area
                )
                worker_thread = QThread()

                self.worker_threads[unique_file_key] = {
                    "worker": worker,
                    "thread": worker_thread,
                }

                worker.moveToThread(worker_thread)
                worker.signals.finished.connect(
                    lambda: self.cleanup_worker(unique_file_key)
                )
                worker_thread.finished.connect(worker_thread.deleteLater)

                worker_thread.started.connect(worker.run)

                worker_thread.start()
                return
            logger.debug("Eco mode not activated")

            self.process_file_in_chunks_and_send_to_ai_threadsafe(
                file_path, gate_way_endpoint
            )

        except IOError as e:
            logger.error(f"Error reading file {file_path}: {e}")
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while processing {file_path}: {e}"
            )
        finally:
            pass

    def cleanup_worker(self, file):
        self.setThreadStatus("completed")
        if file in self.worker_threads:
            worker_info = self.worker_threads[file]
            worker_info["thread"].quit()
            worker_info["thread"].wait()  # Wait for the thread to finish
            worker_info["worker"].deleteLater()
            del self.worker_threads[file]

    def process_file_in_chunks_and_send_to_ai_threadsafe(
        self, file_path, gate_way_endpoint
    ):
        logger.debug(f"Starting thread-safe file processing for: {file_path}")

        if not hasattr(self, "file_thread_counters"):
            self.file_thread_counters = {}

        file_name = os.path.basename(file_path)

        self.file_thread_counters[file_name] = (
            self.file_thread_counters.get(file_name, 0) + 1
        )

        unique_file_key = f"{file_name}_{self.file_thread_counters[file_name]}"
        logger.debug("about to process file with file processor worker")
        self.worker = FileProcessorWorker(
            file_path,
            gate_way_endpoint,
            self.command_input_area,
        )
        worker_thread = QThread()

        self.worker_threads[unique_file_key] = {
            "worker": self.worker,
            "thread": worker_thread,
        }

        self.worker.moveToThread(worker_thread)
        worker_thread.finished.connect(lambda: self.cleanup_worker(unique_file_key))
        worker_thread.finished.connect(worker_thread.deleteLater)

        worker_thread.started.connect(self.worker.run)

        worker_thread.start()
