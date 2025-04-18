import json
import os
import re
import shutil
import time
import warnings
from queue import Queue

from PyQt6 import QtCore
from PyQt6.QtCore import (QFile, QFileSystemWatcher, QObject, QPoint,
                          QSize, Qt, QThread, QThreadPool, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import (  # This module helps in opening URLs in the default browser
    QAction, QGuiApplication, QIcon, QPixmap, QTextCursor)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                             QFrame, QHBoxLayout, QInputDialog, QLabel,
                             QListWidget, QListWidgetItem, QMainWindow, QMenu,
                             QMessageBox, QPushButton, QSizePolicy, QToolBar,
                             QToolTip, QVBoxLayout, QWidget)

from . import constants, tool_configuration, utilities
from .ai_notes_pop_up_window import AiNotes, AiNotesPopupWindow
from .central_display_area_in_main_window import CentralDisplayAreaInMainWindow
from .chroma_manager import ChromaManager
from .configuration_manager import ConfigManager
from .document_loader import DocumentLoaderDialog
from .help import HelpWindow
from .image_command_window import ImageCommandWindow
from .log_config import setup_logging
from .search import CustomSearchLineEdit
from .setup_nebula import settings
from .status_update_feed_manager import statusFeedManager
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

            logger.debug(
                f"Finished chunking, sending to API, gateway endpoint is {self.gate_way_endpoint}, the content being sent is {file_contents}"
            )
            self.command_input_area.execute_api_call(
                file_contents, self.gate_way_endpoint
            )

    def process_next_chunk(self):
        try:
            if not self.chunks_queue.empty():
                next_chunk = self.chunks_queue.get()
                logger.debug("Processing next chunk")
                num_items = self.chunks_queue.qsize()
                logger.debug(f"Number of items to be processed is {num_items}")

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


class LogSideBar(QListWidget):
    send_to_ai_notes_signal = pyqtSignal(str, str)
    send_to_ai_suggestions_signal = pyqtSignal(str, str)
    send_to_autonomous_ai_signal = pyqtSignal(str, str)

    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.lastHoverPos = QPoint()
        self.manager = manager
        self.CONFIG = self.manager.load_config()
        self.send_to_ai_notes_action = QAction("Send to AI Notes", self)
        self.send_to_ai_notes_action.triggered.connect(self.send_to_ai_notes)
        self.send_to_ai_suggestions_action = QAction("Send to AI Suggestions", self)
        self.send_to_ai_suggestions_action.triggered.connect(
            self.send_to_ai_suggestions
        )
        self.delete_action = QAction("Delete", self)
        self.delete_action.triggered.connect(self.delete_file)
        self.delete_all_files_action = QAction("Delete All files", self)
        self.delete_all_files_action.triggered.connect(self.delete_all_files)
        self.rename_action = QAction("Rename", self)
        self.rename_action.triggered.connect(self.rename_file)
        self.context_menu = QMenu(self)

    def contextMenuEvent(self, event):
        self.lastHoverPos = event.pos()

        self.context_menu.addAction(self.send_to_ai_notes_action)

        self.context_menu.addAction(self.send_to_ai_suggestions_action)

        self.context_menu.addAction(self.delete_action)

        self.context_menu.addAction(self.delete_all_files_action)

        self.context_menu.addAction(self.rename_action)

        self.context_menu.exec(event.globalPos())

    def enable_or_disable_due_to_model_creation(self, signal):
        if signal:
            logger.debug("Disabling send to ai and send to suggestions")
            self.send_to_ai_notes_action.setEnabled(False)
            self.send_to_ai_suggestions_action.setEnabled(False)
            self.context_menu.update()
        else:
            logger.debug("Enabling send to ai and send to suggestions")
            self.send_to_ai_notes_action.setEnabled(True)
            self.send_to_ai_suggestions_action.setEnabled(True)
            self.context_menu.update()

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


class Nebula(QMainWindow):
    input_mode_signal = pyqtSignal(str)
    main_window_loaded = pyqtSignal(bool)
    model_creation_in_progress = pyqtSignal(bool)

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
        self.log_side_bar = LogSideBar(manager=self.manager)
        self.child_windows.append(self.log_side_bar)

        num_cores = os.cpu_count()
        self.threadPool = QThreadPool()
        self.threadPool.setMaxThreadCount(num_cores)
        self.log_side_bar.send_to_ai_notes_signal.connect(self.process_new_file_with_ai)
        self.log_side_bar.send_to_autonomous_ai_signal.connect(
            self.process_new_file_with_ai
        )
        self.setupWindow = settings()
        self.log_side_bar.send_to_ai_suggestions_signal.connect(
            self.process_new_file_with_ai
        )
        self.log_side_bar.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.log_side_bar.setSpacing(5)
        self.log_side_bar.setMaximumWidth(300)

        self.log_side_bar.setObjectName("prevResultsList")
        self.log_side_bar.itemClicked.connect(self.on_file_item_clicked)

        self.populate_file_list()
        self.file_system_watcher = QFileSystemWatcher([self.CONFIG["LOG_DIRECTORY"]])
        self.file_system_watcher.directoryChanged.connect(self.populate_file_list)

        self.current_font_size = 10
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))

        self.main_layout = QHBoxLayout()
        self.v_layout = QVBoxLayout()

        self.suggestions_layout = QHBoxLayout()
        self.suggestions_layout.setContentsMargins(
            0, 0, 0, 0
        )  # Reduce the top margin to bring the label closer to the toolbar

        self.search_area = CustomSearchLineEdit(manager=self.manager)
        self.search_area.setFixedHeight(40)
        self.search_area.setPlaceholderText("Search")
        self.search_area.setObjectName("searchArea")
        self.search_area.setToolTip("Search using RAG")
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
        self.input_mode_signal.connect(self.command_input_area.set_input_mode)

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

        self.ai_or_bash = QPushButton()
        self.ai_or_bash.setFixedHeight(50)
        self.ai_or_bash.setObjectName("AiOrBashButton")
        self.ai_or_bash.setToolTip("Switch between bash command or ai prompts")
        self.ai_or_bash_icon_path = return_path(("Images/terminal.png"))
        self.ai_or_bash.setIcon(QIcon(self.ai_or_bash_icon_path))
        self.input_mode = "terminal"
        self.ai_or_bash.clicked.connect(self.switch_between_terminal_and_ai)

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
        self.input_frame_layout.addWidget(self.ai_or_bash)
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
            "color: white; font-size: 10px; font-family: Source Code Pro;border: none; background-color: None"
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
        self.ai_note_taking_action.triggered.connect(self.ai_note_taking_function)
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

        # --- Notes Pop-out Button (reuse existing) ---
        self.notes_pop_out_button = QPushButton()
        self.notes_pop_out_button.setIcon(QIcon(self.pop_out_button_icon_path))
        self.notes_pop_out_button.setToolTip("Expand Notes")
        self.notes_pop_out_button.setFixedSize(30, 30)
        self.notes_pop_out_button.clicked.connect(
            lambda: self.provide_feedback_and_execute(
                self.notes_pop_out_button,
                return_path("Images/clicked.png"),
                return_path("Images/pop_out.png"),
                self.pop_out_notes,
            )
        )

        # --- Notes Top Layout with title ---
        self.notes_top_layout = QHBoxLayout()
        self.notes_label = QLabel("AI Notes")
        self.notes_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.notes_top_layout.addWidget(self.notes_label)
        self.notes_top_layout.addStretch()
        self.notes_top_layout.addWidget(self.notes_pop_out_button)

        # --- Notes Frame ---
        self.notes_frame = QFrame()
        self.notes_frame_layout = QVBoxLayout(self.notes_frame)
        self.notes_frame_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_frame_layout.setSpacing(2)
        self.notes_frame_layout.addLayout(self.notes_top_layout)
        self.notes_frame_layout.addWidget(self.ai_notes)

        # --- status Feed Pop-out Button ---
        self.status_feed_pop_out_button = QPushButton()
        self.status_feed_pop_out_button.setIcon(QIcon(self.pop_out_button_icon_path))
        self.status_feed_pop_out_button.setToolTip("Expand status Feed")
        self.status_feed_pop_out_button.setFixedSize(30, 30)

        self.status_feed_pop_out_button.clicked.connect(
            lambda: self.provide_feedback_and_execute(
                self.status_feed_pop_out_button,
                return_path("Images/clicked.png"),
                return_path("Images/pop_out.png"),
                self.pop_out_status_feed,  # Define this function separately
            )
        )

        # --- status Feed Top Layout ---
        self.status_feed_top_layout = QHBoxLayout()
        self.status_feed_label = QLabel("Live status Feed")
        self.status_feed_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.status_feed_top_layout.addWidget(self.status_feed_label)
        self.status_feed_top_layout.addStretch()
        self.status_feed_top_layout.addWidget(self.status_feed_pop_out_button)

        # --- status Feed List Widget ---
        self.status_feed_list = QListWidget()
        self.status_feed_list.setSpacing(4)
        self.status_feed_list.model().rowsInserted.connect(
            lambda: self.status_feed_list.scrollToBottom()
        )

        # --- status Feed Frame ---
        self.status_feed_frame = QFrame()
        status_feed_layout = QVBoxLayout(self.status_feed_frame)
        status_feed_layout.setContentsMargins(0, 0, 0, 0)
        status_feed_layout.setSpacing(2)
        status_feed_layout.addLayout(self.status_feed_top_layout)
        status_feed_layout.addWidget(self.status_feed_list)

        # Right-side horizontal layout (notes + status feed)
        self.right_side_layout = QHBoxLayout()
        self.right_side_layout.setContentsMargins(0, 0, 0, 0)
        self.right_side_layout.setSpacing(0)
        self.right_side_layout.addWidget(self.notes_frame)
        self.right_side_layout.addWidget(self.status_feed_frame)

        # Equal stretch factors
        self.right_side_layout.setStretch(0, 1)  # Notes
        self.right_side_layout.setStretch(1, 1)  # status Feed

        # Central layout (main content area)
        self.v_layout.addWidget(self.middle_frame)
        self.v_layout.addWidget(self.input_frame)

        # Main layout setup
        self.main_layout = QHBoxLayout()
        self.main_layout.addWidget(self.log_side_bar)
        self.main_layout.addLayout(self.v_layout)
        self.main_layout.addLayout(self.right_side_layout)

        # Corrected main layout stretch factors
        self.main_layout.setStretch(0, 1)  # Sidebar (log_side_bar)
        self.main_layout.setStretch(
            1, 4
        )  # Central main content (middle_frame + input_frame)
        self.main_layout.setStretch(2, 2)  # Right side (notes + status feed)

        # Set the main layout
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
        self.add_document_icon = QIcon(return_path("Images/vector.svg"))
        # Create an action with an icon (adjust the icon path as needed).
        self.loader_action = QAction(self.add_document_icon, "Load Document", self)
        self.help_actions.append(self.loader_action)
        self.loader_action.triggered.connect(self.show_document_loader_dialog)
        self.toolbar.addAction(self.loader_action)
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

        self.image_command_window = ImageCommandWindow(
            return_path("config/dark-stylesheet.css"), self.manager
        )
        self.child_windows.append(ImageCommandWindow)
        self.worker_threads = {}

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
        self.status_feed_manager = statusFeedManager(
            manager=self.manager, update_ui_callback=self.update_status_feed_ui
        )
        # Do an initial update of the status feed
        self.status_feed_manager.update_status_feed()

        # Create a QTimer to update the status feed every 15 minutes (900,000 ms)
        self.status_feed_timer = QTimer(self)
        self.status_feed_timer.timeout.connect(
            self.status_feed_manager.update_status_feed
        )
        self.status_feed_timer.start(300000)  # 5 minutes
        self.engagement_json = {}
        window_title = "Nebula"
        self.worker = None
        try:
            self.engagement_json = self.loadJsonData(self.engagement_details_file)
        except Exception as e:
            logger.error(f"unable to load engagement details {e}")
        if self.engagement_json:
            window_title = (
                window_title + " - " + self.engagement_json["engagement_name"]
            )

        self.setWindowTitle(window_title)

        self.center()
        logger.debug("centered application")

        self.current_action_index = 0
        self.tour_timer = QTimer()
        self.tour_timer.setSingleShot(True)
        logger.debug("Starting tour")
        self.tour_timer.timeout.connect(self.next_step)
        self.main_window_loaded.emit(True)

        self.command_input_area.model_busy_busy_signal.connect(
            self.central_display_area.enable_or_disable_due_to_model_creation
        )
        self.command_input_area.model_busy_busy_signal.connect(
            self.log_side_bar.enable_or_disable_due_to_model_creation
        )
        self.model_creation_in_progress.connect(
            self.log_side_bar.enable_or_disable_due_to_model_creation
        )
        self.pop_out_window = AiNotesPopupWindow(
            notes_file_path=os.path.join(
                self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], "ai_notes.html"
            ),
            manager=self.manager,
            command_input_area=self.command_input_area,
        )
        logger.debug("main window loaded")
        self.vector_db = ChromaManager(
            collection_name="nebula_collection",
            persist_directory=self.CONFIG["CHROMA_DB_PATH"],
        )

    def pop_out_status_feed(self):
        try:
            # Create or reuse a separate window for the status feed pop-out
            self.status_feed_window = QMainWindow(self)
            self.status_feed_window.setWindowTitle("Live Status Feed")

            status_feed_widget = QListWidget()
            status_feed_widget.setSpacing(4)
            self.load_stylesheet(return_path("config/dark-stylesheet.css"))

            # Recreate wrapped items from existing widgets
            for i in range(self.status_feed_list.count()):
                item_widget = self.status_feed_list.itemWidget(
                    self.status_feed_list.item(i)
                )

                if isinstance(item_widget, QLabel):
                    text = item_widget.text()

                    item = QListWidgetItem()
                    label = QLabel(text)
                    label.setWordWrap(True)
                    label.setContentsMargins(4, 4, 4, 4)
                    label.setSizePolicy(
                        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                    )
                    label.adjustSize()

                    item.setSizeHint(label.sizeHint())
                    status_feed_widget.addItem(item)
                    status_feed_widget.setItemWidget(item, label)

            self.status_feed_window.setCentralWidget(status_feed_widget)
            self.status_feed_window.resize(500, 700)
            self.status_feed_window.show()
        except Exception as e:
            logger.error(f"{e}")

    def update_status_feed_ui(self, status_feed_data):

        self.status_feed_list.clear()

        for text in status_feed_data:
            item = QListWidgetItem()
            label = QLabel(text)
            label.setWordWrap(True)
            label.setContentsMargins(4, 4, 4, 4)
            label.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            label.adjustSize()

            item.setSizeHint(label.sizeHint())
            self.status_feed_list.addItem(item)
            self.status_feed_list.setItemWidget(item, label)

        self.status_feed_list.scrollToBottom()

    def show_document_loader_dialog(self):
        try:
            # Create and show the pop-out dialog.
            dialog = DocumentLoaderDialog(self.vector_db, self)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.resize(800, 600)  # Set default size to 800x600 pixels
            dialog.show()
        except Exception as e:
            logger.error(f"{e}")

    def open_tour(self):
        self.current_action_index = 0
        self.show_message("Let's take a tour of the main toolbar.")
        QTimer.singleShot(
            1000,
            lambda: self.highlight_action(self.help_actions[self.current_action_index]),
        )

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

    def reset_terminal(self):
        self.command_input_area.terminal.password_mode.emit(False)
        self.command_input_area.terminal.reset_terminal()

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
                logger.error(f"{e}")

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

    def switch_between_terminal_and_ai(self):
        if self.input_mode == "ai":
            # Change to terminal mode
            self.input_mode = "terminal"

            # Update the icon for terminal mode
            self.ai_or_bash.setIcon(QIcon(return_path("Images/terminal.png")))
        else:
            # Change back to ai mode
            self.input_mode = "ai"
            # Update the icon for ai mode
            self.ai_or_bash.setIcon(QIcon(return_path("Images/agent_off.png")))

        self.input_mode_signal.emit(self.input_mode)

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

    def update_engagement_folder(self, text: str):
        logger.debug(f"Updating engagement folder as {text}")
        self.engagement_folder = text
        self.manager.setengagement_folder(text)

    def open_engagement(self):
        try:
            self.setupWindow = settings()
            self.setupWindow.setupCompleted.connect(self.update_engagement_folder)
            self.setupWindow.show()
        except Exception as e:
            logger.error(f"{e}")

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

    def clear_screen(self, _=None):
        self.command_input_area.terminal.write("reset \n")

    def open_help(self, _=None):
        try:
            # If the help window already exists and is visible, bring it to the front.
            if self.help_window is not None:
                if self.help_window.isVisible():
                    self.help_window.raise_()
                    self.help_window.activateWindow()
                    return

            # Otherwise, create a new instance of the HelpWindow.
            self.help_window = HelpWindow()
            self.child_windows.append(self.help_window)
            self.help_window.show()
        except Exception as e:
            logger.error(f"{e}")

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

    def on_search_result_selected(self, result):
        self.central_display_area.insertPlainText(result)

    def update_eco_mode_display(self, _=None):
        if self.eco_mode.isChecked():
            self.eco_mode.setIcon(self.eco_mode_on_icon)

        else:
            self.eco_mode.setIcon(self.eco_mode_off_icon)

    def update_suggestions_display(self, _=None):
        if self.suggestions_action.isChecked():
            self.suggestions_action.setIcon(QIcon(self.suggestions_off_icon_path))
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
        try:
            if self.textEditor.isVisible():
                self.textEditor.raise_()
                self.textEditor.activateWindow()
            else:
                self.textEditor.show()
        except Exception as e:
            logger.error(f"{e}")

    def openTerminalEmulator(self, _=None):
        try:
            self.terminal_emulator_number += 1
            self.terminalWindow = TerminalEmulatorWindow(
                self,
                manager=self.manager,
                terminal_emulator_number=self.terminal_emulator_number,
            )

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
            self.terminalWindow.show()
        except Exception as e:
            logger.error(f"{e}")

    def open_suggestions_pop_out_window(self, _=None):
        try:
            self.suggestions_button.setIcon(
                QIcon(self.suggestions_not_available_icon_path)
            )

            # Check if the window is already visible.
            if self.suggestions_pop_out_window.isVisible():
                self.suggestions_pop_out_window.activateWindow()
            else:
                self.suggestions_pop_out_window.show()
        except Exception as e:
            logger.error(f"{e}")

    def open_image_command_window(self, _=None):
        try:
            if self.image_command_window.isVisible():
                self.image_command_window.raise_()
                self.image_command_window.activateWindow()
            else:
                self.image_command_window.show()
        except Exception as e:
            logger.error(f"{e}")

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
            self.log_side_bar.clear()
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
                    self.log_side_bar.addItem(item)
            else:
                raise FileNotFoundError(f"Directory not found: {log_directory}")

        except Exception as e:
            logger.error(f"Error populating file list: {e}")

    def upload_file(self, _=None):
        """Initiate the file upload process and handle the user's choice."""
        try:

            file_path = self.get_file_path()

            self.process_file(file_path)

        except Exception as e:
            logger.error(f"An error occurred: {e}")

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

    def pop_out_notes(self, _=None):
        try:
            # If the pop-out window already exists and is visible, bring it to the front.
            if self.pop_out_window.isVisible():
                self.pop_out_window.raise_()
                self.pop_out_window.activateWindow()
                return

            # Otherwise, create a new notes popup window.
            self.CONFIG = self.manager.load_config()
            self.pop_out_window = AiNotesPopupWindow(
                notes_file_path=os.path.join(
                    self.CONFIG["SUGGESTIONS_NOTES_DIRECTORY"], "ai_notes.html"
                ),
                manager=self.manager,
                command_input_area=self.command_input_area,
            )
            self.pop_out_window.setTextInTextEdit(self.ai_notes.toHtml())
            self.pop_out_window.textUpdated.connect(self.update_main_notes)
            self.pop_out_window.show()
        except Exception as e:
            logger.error(f"{e}")

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
                        if not file.startswith("ai"):
                            self.process_new_file_with_ai(file, "suggestion_files")
                            logger.debug(f"File processed for suggestions: {file}")
                    if self.ai_note_taking_action.isChecked():
                        if not file.startswith("ai"):
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
