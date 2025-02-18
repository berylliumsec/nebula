import json
import os
import pty
import re
import select
import shutil
import signal
import threading
import time
import warnings

import torch
from langchain.agents import AgentType, initialize_agent
from langchain.memory import ConversationBufferMemory
from langchain_community.llms import HuggingFacePipeline
from langchain_community.tools import DuckDuckGoSearchRun, ShellTool
from PyQt6 import QtCore
from PyQt6.QtCore import (QFile, QFileSystemWatcher, QObject, QRunnable,
                          QStringListModel, Qt, QThread, QThreadPool, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import QAction, QIcon, QMouseEvent, QPixmap, QTextCursor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QCompleter, QDialog,
                             QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QMainWindow, QMenu,
                             QMessageBox, QPushButton, QScrollArea,
                             QSizePolicy, QTextEdit, QToolBar, QVBoxLayout,
                             QWidget)
from transformers import (AutoModelForCausalLM, AutoTokenizer,
                          BitsAndBytesConfig, pipeline)
from . import constants, utilities
from .central_display_area_in_main_window import CentralDisplayAreaInMainWindow
from .log_config import setup_logging
from .tools.subprocess_tool import SubprocessTool
from .update_utils import return_path
import re
warnings.filterwarnings("ignore")
logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/test_agents.log")

MEMORY = ConversationBufferMemory(
    memory_key="chat_history",  # key used in the prompt to include past messages
    return_messages=True,  # whether to return messages as a list
)
BASH_TOOL = ShellTool(return_direct=True)
SEARCH_TOOL = DuckDuckGoSearchRun(return_direct=True)
SUBPROCESS_EXECUTOR = SubprocessTool(return_direct=True)

warnings.filterwarnings("ignore")

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/terminal_emulator.log")

class ModelWorkerSignals(QObject):
    # Signal to emit when the model is created
    modelCreated = pyqtSignal(object)
    modelCreationInProgress = pyqtSignal(bool)
    modelName = pyqtSignal(str)


class ModelCreationWorker(QRunnable):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.signals = ModelWorkerSignals()

    def run(self):
        logger.debug("creating model")
        self.signals.modelCreationInProgress.emit(True)

        try:
            config = self.manager.load_config()
            model_name = config["MODEL"]
            cache_dir = config["CACHE_DIR"]
            bnb_config = BitsAndBytesConfig(load_in_8bit=True)
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                model_max_length=32000,
                low_cpu_mem_usage=True,
                cache_dir=cache_dir,
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                low_cpu_mem_usage=True,
                quantization_config=bnb_config,
                device_map="auto",
                cache_dir=cache_dir,
            )
            raw_pipe = pipeline(
                "text-generation",
                model=self.model,
                tokenizer=self.tokenizer,
                max_new_tokens=32000,
                use_fast=True,
                return_full_text=False,
            )
            self.pipe = HuggingFacePipeline(pipeline=raw_pipe)
        except Exception as e:
            logger.error(f"Could not start model: {e}")
            return  # Early exit; finally block will run.
        finally:
            self.signals.modelCreationInProgress.emit(False)
        self.signals.modelCreated.emit(self.pipe)
        self.signals.modelName.emit(model_name)


class AgentTaskRunnerSignals(QObject):
    """
    Defines the signals available from a running worker thread.
    Supported signals are:
    - finished: No data
    - result: `tuple` containing (`endpoint`, `command`, `result`)
    """

    finished = pyqtSignal()
    result = pyqtSignal(str, str, object)  # endpoint, command, result
    error = pyqtSignal(str)




class AgentTaskRunner(QRunnable):
    """
    Worker thread to run agent queries.
    """

    def __init__(self, agent, query, endpoint):
        super().__init__()
        self.agent = agent
        self.query = query
        self.signals =AgentTaskRunnerSignals()
        self.endpoint = endpoint
    def run(self):
        try:
            response = self.agent.run(self.query)
            self.signals.result.emit(self.endpoint, "ai", response)
        except Exception as e:
            logger.error(f"Error during agent query: {e}")
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()



class CheckListDialog(QDialog):
    def __init__(
        self,
        commands_to_run=None,
        information_text=None,
        text_edit_content="Please ensure that you have the relevant tools installed on your system. Check/uncheck the commands to run and edit them as needed",
        vulnerabilities_confirmed="",
        evidence="",
        parent=None,
        manager=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Suggested Commands")
        self.setStyleSheet(
            """
            QDialog {
                background-color: #1e1e1e;
                color: white;
            }
            QLabel, QCheckBox, QTextEdit, QLineEdit {
                font-family: 'Courier';
                font-size: 10pt;
            }
            QPushButton {
                background-color: #1e1e1e;
                color: #FFFFFF;
                border-radius: 5px;
                padding: 5px;
                font-weight: bold;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            QTextEdit, QLineEdit {
                background-color: #1E1E1E;
                color: white;
                border: 1px solid #333333;
                border-radius: 5px;
                padding: 5px;
            }
            
            QScrollBar:vertical {
                border: none;
                background: #2e2e2e;
                width: 10px;
                margin: 15px 0 15px 0;
                border-radius: 4px;
            }
            
             QScrollBar::handle:vertical {
                background-color: #555555;
            }
        """
        )
        self.resize(1000, 600)
        sizePolicy = QSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.setSizePolicy(sizePolicy)
        mainLayout = QVBoxLayout()
        mainLayout.addStretch(1)
        self.setLayout(mainLayout)

        modified_information_text = self.modify_information_text(information_text)
        self.informationTextEdit = QTextEdit()
        self.informationTextEdit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.informationTextEdit.setText(modified_information_text)
        self.informationTextEdit.setReadOnly(True)
        mainLayout.addWidget(self.informationTextEdit)

        # Vulnerabilities Found Text Edit
        self.vulnerabilitiesContentLabel = QLabel("Vulnerabilities Discovered")
        self.vulnerabilitiesContentLabel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.vulnerabilitiesContentLabel.setStyleSheet("QLabel { color : white; }")
        self.vulnerabilitiesContentLabel.setWordWrap(True)
        mainLayout.addWidget(self.vulnerabilitiesContentLabel)

        self.vulnerabilitiesTextEdit = QTextEdit()
        self.vulnerabilitiesTextEdit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.vulnerabilitiesTextEdit.setText(vulnerabilities_confirmed)
        self.vulnerabilitiesTextEdit.setReadOnly(
            True
        )  # Set to False if you want it to be editable
        mainLayout.addWidget(self.vulnerabilitiesTextEdit)

        self.evidenceContentLabel = QLabel("Evidence")
        self.evidenceContentLabel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.evidenceContentLabel.setWordWrap(True)
        self.evidenceContentLabel.setStyleSheet("QLabel { color : white; }")
        mainLayout.addWidget(self.evidenceContentLabel)
        self.evidenceTextEdit = QTextEdit()
        self.evidenceTextEdit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.evidenceTextEdit.setText(evidence)
        self.evidenceTextEdit.setReadOnly(
            True
        )  # Set to False if you want it to be editable
        mainLayout.addWidget(self.evidenceTextEdit)
        # Text Edit Content Label
        self.textEditContentLabel = QLabel(text_edit_content)
        self.textEditContentLabel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.textEditContentLabel.setStyleSheet("QLabel { color : white; }")
        self.textEditContentLabel.setWordWrap(True)
        self.manager = manager
        self.config = self.manager.load_config()

        self.save_file = os.path.join(
            self.config["AUTONOMOUS_DIRECTORY"], "last_job.json"
        )
        mainLayout.addWidget(self.textEditContentLabel)
        # Checkboxes for Items with QLineEdit for editable text
        self.editableChecklistItems = []
        scrollArea = QScrollArea()
        scrollArea.setWidgetResizable(True)
        scrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scrollArea.verticalScrollBar().setStyleSheet("")
        containerWidget = QWidget()
        containerWidget.setStyleSheet("background-color: #1e1e1e;")
        layout = QVBoxLayout(containerWidget)
        for item in commands_to_run:
            itemLayout = QHBoxLayout()
            checkBox = QCheckBox()
            checkBox.setChecked(True)
            lineEdit = QTextEdit(item)
            lineEdit.setLineWrapMode(
                QTextEdit.LineWrapMode.WidgetWidth
            )  # Correct enumeration for line wrapping

            lineEdit.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )  # Hide vertical scroll bar
            lineEdit.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )  # Hide horizontal scroll bar
            lineEdit.setFixedHeight(50)  # Adjust height to look similar to a line edit

            # Create a copy button and connect its clicked signal to the copy_to_clipboard function
            copyButton = QPushButton("Copy")
            copyButton.setStyleSheet(
                """
            QPushButton:hover {
                background-color: #333333;  /* Blue background when hovered */
            }
        """
            )
            copyButton.clicked.connect(
                lambda checked, text=lineEdit.toPlainText(): self.copy_to_clipboard(
                    text
                )
            )

            # Update the itemLayout to include the copy button
            itemLayout.addWidget(checkBox)
            itemLayout.addWidget(lineEdit)
            itemLayout.addWidget(copyButton)  # Add the copy button to the mainLayout

            self.editableChecklistItems.append((checkBox, lineEdit))
            layout.addLayout(itemLayout)

        scrollArea.setWidget(containerWidget)
        mainLayout.addWidget(scrollArea)
        # Button Box
        self.buttonBox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        okButton = self.buttonBox.button(QDialogButtonBox.StandardButton.Ok)
        okButton.setText("Run")
        self.saveButton = QPushButton("Save")
        self.saveButton.clicked.connect(self.save_to_file)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        mainLayout.addWidget(self.saveButton)
        mainLayout.addWidget(self.buttonBox)
        mainLayout.addStretch(1)

    def save_to_file(self):
        # Pre-determined file path and name

        data = {
            "information_text": self.informationTextEdit.toPlainText(),
            "vulnerabilities_confirmed": self.vulnerabilitiesTextEdit.toPlainText(),
            "evidence": self.evidenceTextEdit.toPlainText(),
            "commands_to_run": [
                lineEdit.toPlainText() for _, lineEdit in self.editableChecklistItems
            ],
            "checked_states": [
                checkBox.isChecked() for checkBox, _ in self.editableChecklistItems
            ],
        }
        try:
            with open(self.save_file, "w") as f:
                json.dump(data, f, indent=4)
            # Show a message box to inform the user
            QMessageBox.information(
                self,
                "Save Successful",
                f"Data has been successfully saved to {self.save_file}",
            )
        except Exception as e:
            # Show a message box if there was an error during save
            utilities.show_message(f"An error occurred while saving: {e}")

    def checkedItems(self):
        # Return the text of checked items that are checked
        return [
            lineEdit.toPlainText()
            for checkBox, lineEdit in self.editableChecklistItems
            if checkBox.isChecked()
        ]

    def modify_information_text(self, text):
        # Ensure the text is a string
        if text is None:
            text = ""  # Convert None to an empty string
        elif not isinstance(text, str):
            text = str(text)  # Convert other types to string
        # Replace periods with a newline, except when followed by a digit (list markers)
        return re.sub(r"\. (?!\d)", ".\n", text)

    def copy_to_clipboard(self, text):
        clipboard = QApplication.clipboard()
        clipboard.setText(text)


class TerminalEmulatorWindow(QMainWindow):
    def __init__(
        self, parent=None, nltk=None, manager=None, terminal_emulator_number=0
    ):
        super().__init__(parent)
        self.nltk = nltk
        self.terminal_emulator_number = terminal_emulator_number
        title = f"Terminal - {self.terminal_emulator_number}"
        self.setWindowTitle(title)

        self.manager = manager
        self.CONFIG = self.manager.load_config()

        self.command_input_area = CommandInputArea(manager=self.manager)
        self.central_display_area = CentralDisplayAreaInMainWindow(
            manager=self.manager, command_input_area=self.command_input_area
        )
        self.central_display_area.notes_signal_from_central_display_area.connect(
            self.command_input_area.execute_api_call
        )
        self.central_display_area.suggestions_signal_from_central_display_area.connect(
            self.command_input_area.execute_api_call
        )
        self.command_input_area.updateCentralDisplayArea.connect(
            self.update_terminal_output
        )
        self.command_input_area.updateCentralDisplayAreaForApi.connect(
            self.update_terminal_output_for_api
        )

        layout = QVBoxLayout()

        lineEditLayout = QHBoxLayout()
        self.clear_button = utilities.LongPressButton()
        self.clear_button.setFixedHeight(35)
        self.clear_button.setObjectName("clearButton")
        self.clear_button.setToolTip("Clear the display area")
        self.clear_button_icon_path = return_path(("Images/clear.png"))
        self.clear_button.clicked.connect(self.clear_screen)
        self.clear_button.longPressed.connect(self.reset_terminal)
        self.clear_button.longPressProgress.connect(
            self.change_clear_button_icon_temporarily
        )

        self.clear_button.setIcon(QIcon(self.clear_button_icon_path))

        self.uploadButton = QPushButton()
        self.uploadButton.setIcon(
            QIcon(return_path("Images/upload.png"))
        )  # Ensure you have this icon in your resources
        self.uploadButton.setFixedHeight(35)
        self.uploadButton.clicked.connect(self.upload_file)
        lineEditLayout.addWidget(self.clear_button)
        lineEditLayout.addWidget(self.command_input_area)
        lineEditLayout.addWidget(self.uploadButton)
        lineEditContainer = QWidget()
        lineEditContainer.setLayout(lineEditLayout)

        # Line Edit Layout
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))

        self.current_font_size = 10

        self.current_command_output = ""
        self.toolbar = QToolBar("Terminal Toolbar")
        self.toolbar.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(self.toolbar)
        screenshot_action = QAction("Take Screenshot", self)
        self.toolbar.addAction(screenshot_action)
        screenshot_icon_path = return_path("Images/screenshot.png")
        screenshot_action.setIcon(QIcon(screenshot_icon_path))
        screenshot_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                screenshot_action,
                return_path("Images/clicked.png"),
                return_path("Images/screenshot.png"),
                self.take_screenshot,
            )
        )

        increase_font_action = QAction("Increase Font Size", self)
        decrease_font_action = QAction("Decrease Font Size", self)
        increase_font_icon = QIcon(return_path("Images/increase_font.png"))
        increase_font_action.setIcon(increase_font_icon)
        decrease_font_icon = QIcon(return_path("Images/decrease_font.png"))
        decrease_font_action.setIcon(decrease_font_icon)
        self.toolbar.addAction(increase_font_action)
        self.toolbar.addAction(decrease_font_action)
        self.lock = threading.Lock()

        increase_font_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                increase_font_action,
                return_path("Images/clicked.png"),
                return_path("Images/increase_font.png"),
                lambda: self.adjust_font_size(1),
            )
        )
        decrease_font_action.triggered.connect(
            lambda: self.provide_feedback_and_execute(
                decrease_font_action,
                return_path("Images/clicked.png"),
                return_path("Images/decrease_font.png"),
                lambda: self.adjust_font_size(-1),
            )
        )

        self.incognito_mode = False

        layout.addWidget(self.central_display_area)
        layout.addWidget(lineEditContainer)
        centralWidget = QWidget()
        centralWidget.setLayout(layout)
        self.setCentralWidget(centralWidget)
        self.resize(1200, 600)
        self.center()

    def clear_screen(self, _=None):
        self.command_input_area.terminal.write("reset \n")

    def reset_terminal(self):
        self.command_input_area.terminal.password_mode.emit(False)
        self.command_input_area.terminal.reset_terminal()
        self.central_display_area.clear()

    def upload_file(self, _=None):
        try:
            file_path = self.get_file_path()
            if file_path:
                self.process_file(file_path)
        except Exception as e:
            logger.error(f"An error occurred: {e}")

    def get_file_path(self, _=None):
        file_dialog = QFileDialog(self)
        file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_dialog.setNameFilter("All Files (*.*)")
        if file_dialog.exec() == QFileDialog.DialogCode.Accepted:
            return file_dialog.selectedFiles()[0]
        return None

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

    def center(self):
        screen = QApplication.primaryScreen()

        screen_geometry = screen.availableGeometry()

        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

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

    def load_stylesheet(self, filename):
        style_file = QFile(filename)
        style_file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text)
        self.original_stylesheet = style_file.readAll().data().decode("utf-8")
        self.setStyleSheet(self.original_stylesheet)

    def adjust_font_size(self, delta):
        self.current_font_size += delta
        self.central_display_area.set_font_size_for_copy_button(self.current_font_size)
        updated_stylesheet = re.sub(
            r"font-size: \d+pt;",
            f"font-size: {self.current_font_size}pt;",
            self.original_stylesheet,
        )
        self.setStyleSheet(updated_stylesheet)

    def take_screenshot(self, _=None):
        try:
            self.CONFIG = self.manager.load_config()
            pixmap = QPixmap(self.central_display_area.size())
            self.central_display_area.render(pixmap)

            filename, _ = QFileDialog.getSaveFileName(
                self,
                "Save Screenshot",
                self.CONFIG["SCREENSHOTS_DIR"],
                "PNG Files (*.png);;All Files (*)",
            )
            if filename:
                pixmap.save(filename)
                logger.debug(f"Screenshot saved as {filename}")

        except Exception as e:
            logger.error(f"Error taking screenshot: {e}")

    def set_incognito_mode(self, data):
        logger.debug("setting incognito mode")
        if data:
            self.incognito_mode = True
        else:
            self.incognito_mode = False

    def update_terminal_output(self, data):
        logger.debug("received data to update terminal with")

        if (
            "\x1b[2J" in data
            or "\x1b[1J" in data
            or "\x1b[0J" in data
            or "\x1b[3J" in data
            or "\x1bc" in data
            or "\x1b[0m" in data
            or "\x1b[H" in data
        ):
            self.central_display_area.clear()
        else:
            self.central_display_area.moveCursor(QTextCursor.MoveOperation.End)

            logger.debug("inserting data into central display area")
            if not utilities.contains_escape_sequences(
                data
            ) and not utilities.contains_only_spaces(data):
                last_line = utilities.show_last_line(
                    self.central_display_area.document()
                )
                if not self.central_display_area.toPlainText().endswith(
                    "\n"
                ) and not re.search(constants.CUSTOM_PROMPT_PATTERN, last_line):
                    self.central_display_area.insertPlainText("\n")
                self.central_display_area.insertPlainText(data)
                self.current_command_output += data

    def update_terminal_output_for_api(self, data):
        logger.debug("received data for terminal update from signal")
        self.central_display_area.moveCursor(QTextCursor.MoveOperation.End)

        data = utilities.process_text(data)
        # Ensure that the new text starts on a new line by checking if the last character is not already a newline
        last_line = utilities.show_last_line(self.central_display_area.document())
        if not self.central_display_area.toPlainText().endswith("\n") and not re.search(
            constants.CUSTOM_PROMPT_PATTERN, last_line
        ):
            self.central_display_area.insertPlainText("\n")
        self.central_display_area.insertPlainText(data)


class TerminalEmulator(QThread):
    data_ready = pyqtSignal(str)
    current_directory_changed = pyqtSignal(str)
    autonomous_terminal_execution_iteration_is_done = pyqtSignal()
    busy = pyqtSignal(bool)
    password_mode = pyqtSignal(bool)

    def __init__(self, manager):
        super().__init__()
        self.current_command = ""
        self.current_command_output = ""
        self.manager = manager
        self.autonomous_mode = False
        self.number_of_autonomous_commands = 0
        self.current_command_concatenated_in_autonomous_mode = ""
        self.shell_pid = None
        self.web_mode = ""
        self.prompt_signs = [
            "$",  # Common Unix/Linux Bash shell for regular users
            "#",  # Common Unix/Linux Bash shell for root user or privileged mode
            "%",  # Common in C Shell (csh) and Tenex C Shell (tcsh)
            ">",  # Used in Windows command prompt, Fish shell, and Node.js REPL
            "C:\\>",  # Windows command prompt showing the drive
            "PS",  # Windows PowerShell prefix, typically followed by a path and '>'
            ">>>",  # Python interactive shell
            "...",  # Continuation lines in Python and Node.js REPL
            "irb(main):001:0>",  # Ruby's interactive shell (IRB)
            "In [1]:",  # IPython prompt
            "(config)#",  # Cisco Global Configuration mode
        ]

        self.incognito_mode = False
        try:
            self.master, self.slave = pty.openpty()
            self.shell_pid = os.fork()

            if self.shell_pid == 0:
                try:
                    utilities.set_terminal_size(self.slave, 90, 90)
                    os.environ["TERM"] = "xterm-256color"
                    os.setsid()
                    os.dup2(self.slave, 0)
                    os.dup2(self.slave, 1)
                    os.dup2(self.slave, 2)

                    if self.slave > 2:
                        os.close(self.slave)

                    default_shell = os.environ.get("SHELL", "/bin/bash")

                    if "zsh" in default_shell:
                        os.environ["PS1"] = "nebula %~%# "
                        os.execv(
                            default_shell, [default_shell, "-d", "-f", "--interactive"]
                        )

                    elif "bash" in default_shell:
                        os.environ["PS1"] = "nebula \\w\\$ "
                        os.execv(
                            default_shell, [default_shell, "--noprofile", "--norc"]
                        )

                    else:
                        os.execv(default_shell, [default_shell])

                except Exception as e:
                    logger.error(f"Error in child process: {e}")
                    os._exit(1)

            logger.debug("Pseudo-terminal set up successfully")

        except Exception as e:
            logger.error(f"Error setting up pseudo-terminal: {e}")

    def update_number_of_autonomous_commands(self, data):
        logger.debug(f"number of autonomous_jobs = {data}")
        self.number_of_autonomous_commands = data

    def set_incognito_mode(self, data):
        logger.debug("setting incognito mode")
        if data:
            self.incognito_mode = True
        else:
            self.incognito_mode = False

    def set_web_mode(self, data):
        logger.debug("setting web mode")
        if data:
            self.web_mode = True
        else:
            self.web_mode = False

    def set_autonomous_mode(self, data):
        logger.debug("setting autonomous mode")
        if data:
            self.autonomous_mode = True
        else:
            self.autonomous_mode = False

    def check_for_prompt(self, data):
        self.CONFIG = self.manager.load_config()
        try:
            if self.current_command == "pwd":
                pwd_output = self.extract_current_directory(data)
                if pwd_output:
                    self.current_directory_changed.emit(pwd_output)
                    self.current_command = ""
            non_printable_pattern = b"[^\x20-\x7E\t\n]"
            cleaned_data = re.sub(non_printable_pattern, b"", data)
            decoded_data = cleaned_data.decode("utf-8")
            # logger.debug(f"Raw decoded data: {decoded_data}")
            formatted_decoded_data = utilities.process_output(decoded_data)
            # logger.debug(f"Formatted data: {formatted_decoded_data}")
            if re.search(constants.CUSTOM_PROMPT_PATTERN, decoded_data):

                if self.incognito_mode:
                    logger.debug(
                        "Incognito mode is enabled. Running hooks on the command."
                    )
                    self.current_command = utilities.run_hooks(
                        self.current_command, self.CONFIG["PRIVACY_FILE"]
                    )

                logger.debug(f"Autonomous state: {self.autonomous_mode}")
                if not self.autonomous_mode and utilities.is_included_command(
                    self.current_command, self.CONFIG
                ):
                    utilities.log_command_output(
                        self.current_command,
                        self.current_command_output,
                        self.CONFIG,
                    )
                    self.busy.emit(False)
                    self.current_command_output = ""
                elif (
                    self.autonomous_mode
                    and "reset" not in self.current_command
                    and self.number_of_autonomous_commands > 0
                ):
                    logger.debug(
                        f"autonomous mode has been activated and the number of autonomous jobs is {self.number_of_autonomous_commands}"
                    )
                    self.current_command_concatenated_in_autonomous_mode += (
                        f" {self.current_command}"
                    )
                    self.current_command_output += formatted_decoded_data
                    self.autonomous_terminal_execution_iteration_is_done.emit()
                    self.busy.emit(True)
                elif (
                    self.autonomous_mode
                    and "reset" not in self.current_command
                    and self.number_of_autonomous_commands == 0
                    and utilities.is_included_command(self.current_command, self.CONFIG)
                ):
                    logger.debug(
                        f"autonomous mode has been activated and the number of autonomous jobs is {self.number_of_autonomous_commands}"
                    )
                    self.current_command_concatenated_in_autonomous_mode += (
                        f" {self.current_command}"
                    )

                    if self.web_mode:

                        self.current_command_output = utilities.extract_data_for_web(
                            self.current_command_output
                        )

                    utilities.log_command_output(
                        self.current_command_concatenated_in_autonomous_mode,
                        self.current_command_output,
                        self.CONFIG,
                    )
                    self.autonomous_terminal_execution_iteration_is_done.emit()
                    self.busy.emit(False)

                else:
                    self.current_command_output = ""

                    if self.autonomous_mode:
                        self.autonomous_terminal_execution_iteration_is_done.emit()
                    self.busy.emit(False)

            else:
                self.current_command_output += formatted_decoded_data
        except UnicodeDecodeError as e:
            logger.error(f"Error decoding data: {e}")
        except re.error as e:
            logger.error(f"Error in regex search: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in check_for_prompt: {e}")

    def extract_current_directory(self, data):
        pattern = r"^(.+)\nnebula\$"
        match = re.search(pattern, data.decode("utf-8", errors="ignore"), re.MULTILINE)
        if match:
            logger.debug(f"PWD is {match}")
            return match.group(1).strip()

        return None

    def process_terminal_output(self, data):
        processed_data = bytearray()
        for byte in data:
            if byte == 8:  # Backspace character in ASCII
                if processed_data:
                    processed_data.pop()  # Remove the last character added
            else:
                processed_data.append(byte)
        return bytes(processed_data)

    def run(self, _=None):
        buffer = ""
        while True:
            # Attempt to re-initialize if self.master is None (but only if necessary)
            if self.master is None:
                logger.debug(
                    "Detected closed master file descriptor, attempting to reinitialize."
                )
                self.reset_terminal()
                if self.master is None:
                    logger.error("Failed to reinitialize terminal. Retrying...")
                    time.sleep(1)  # Wait a bit before retrying to avoid spamming.
                    continue

            try:
                r, _, _ = select.select([self.master], [], [], 0.1)
                if r:
                    data = os.read(self.master, 4096)

                    # Process the data as before
                    if data:
                        data = self.process_terminal_output(data)
                        if utilities.is_linux_asking_for_password(data):
                            self.password_mode.emit(True)

                            processed_data = utilities.process_output(
                                data.decode("utf-8", errors="ignore")
                            )
                            self.data_ready.emit(processed_data)
                            continue

                        self.check_for_prompt(data)

                        processed_data = utilities.process_output(
                            data.decode("utf-8", errors="ignore")
                        )

                        # logger.debug(f"Processed data: {processed_data}")

                        # Here you handle the processed data, like emitting it to the UI or storing it.
                        buffer += processed_data
                        if "\n" in buffer or any(
                            prompt in buffer for prompt in self.prompt_signs
                        ):
                            self.data_ready.emit(
                                buffer
                            )  # Assuming this is a method to handle the ready data
                            buffer = ""
            except OSError as e:
                logger.error(f"Error with file operations on master: {e}")
                self.master = None  # Reset master to handle in next loop iteration
                continue
            except Exception as e:
                # Handle other exceptions that may occur
                logger.error(f"Unexpected error: {e}")
                continue  # Depending on the error, you may choose not to continue

    def reset_terminal(self):
        self.queued_autonomous_items_for_api = 0
        self.number_of_autonomous_commands = 0
        self.current_command = ""
        self.busy.emit(False)
        # Terminate current terminal session
        if self.shell_pid > 0:
            try:
                os.kill(
                    self.shell_pid, signal.SIGTERM
                )  # Send termination signal to the shell process
            except Exception as e:
                logger.error(f"Error killing terminal process: {e}")

        # Close master and slave ptys if they exist
        try:
            os.close(self.master)
            os.close(self.slave)
        except Exception as e:
            logger.error(f"Error closing pseudo-terminals: {e}")

        # Re-initialize terminal (similar to what is done in __init__)
        try:
            self.master, self.slave = pty.openpty()
            self.shell_pid = os.fork()

            if self.shell_pid == 0:  # Child process
                try:
                    utilities.set_terminal_size(self.slave, 90, 90)
                    os.environ["TERM"] = "xterm-256color"
                    os.setsid()
                    os.dup2(self.slave, 0)
                    os.dup2(self.slave, 1)
                    os.dup2(self.slave, 2)

                    if self.slave > 2:
                        os.close(self.slave)

                    default_shell = os.environ.get("SHELL", "/bin/bash")
                    # Set up the shell environment again
                    if "zsh" in default_shell:
                        os.environ["PS1"] = "nebula %~%# "
                        os.execv(
                            default_shell, [default_shell, "-d", "-f", "--interactive"]
                        )
                    elif "bash" in default_shell:
                        os.environ["PS1"] = "nebula \\w\\$ "
                        os.execv(
                            default_shell, [default_shell, "--noprofile", "--norc"]
                        )
                    else:
                        os.execv(default_shell, [default_shell])

                except Exception as e:
                    logger.error(f"Error in child process during reset: {e}")
                    os._exit(1)

            logger.debug("Terminal reset and pseudo-terminal set up successfully")

        except Exception as e:
            logger.error(f"Error resetting pseudo-terminal: {e}")

    def write(self, data):
        # logger.debug(f"writing data: {data}")
        if data == "<Ctrl-C>":
            os.write(self.master, b"\x03")
        elif data == "<Ctrl-\\>":
            os.write(self.master, b"\x1c")
        elif data == "<Ctrl-Z>":
            os.write(self.master, b"\x1a")
        elif data == "<Ctrl-D>":
            os.write(self.master, b"\x04")

        elif data == "<Up>":
            os.write(self.master, b"\x1b[A")
        elif data == "<Down>":
            os.write(self.master, b"\x1b[B")
        elif data == "<Right>":
            os.write(self.master, b"\x1b[C")
        elif data == "<Left>":
            os.write(self.master, b"\x1b[D")

        else:
            # logger.debug(f"data encode: {data.encode()}")
            self.busy.emit(True)
            os.write(self.master, data.encode())

    def update_current_command(self, command):
        if not self.autonomous_mode:
            self.current_command = command
        else:
            # Update the current command with the new command, typically preceding it.
            self.current_command = command + "-" + self.current_command


class CustomCompleter(QCompleter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.command = None

    def disable_completer(self, _=None):
        self.lineEdit.setCompleter(None)

    def splitPath(self, path):
        self.command, path = self.parse_input(path)
        return [path]

    def pathFromIndex(self, index):
        path = super().pathFromIndex(index)
        return f"{self.command} {path}"

    def parse_input(self, input_text):
        match = re.match(r"(\S+)\s+(.*)$", input_text)
        if match:
            return match.group(1), match.group(2)
        else:
            return "", input_text


class DynamicCompleter(QCompleter):
    def __init__(self, parent=None):
        super(DynamicCompleter, self).__init__(parent)
        self.stringListModel = QStringListModel(self)
        self.setModel(self.stringListModel)
        self.commands = utilities.get_shell_commands()
        self.path_cache = {}

    def update_model(self, text):
        try:
            self.stringListModel.setStringList(self.completer_model(text))
        except Exception as e:
            logger.error(f"Error in setting string list: {e}")

    def completer_model(self, text):
        parts = text.split()
        if not parts:
            return []

        if parts[0] in self.commands and len(parts) > 1:
            path_fragment = " ".join(parts[1:])
            return self.list_paths(path_fragment)
        elif len(parts) == 1:
            return self.commands + self.list_paths(parts[0])
        else:
            return self.list_paths(text.strip())

    def list_paths(self, path_fragment):
        path_fragment = os.path.expanduser(path_fragment)
        if not os.path.isabs(path_fragment):
            path_fragment = os.path.abspath(path_fragment)

        if path_fragment in self.path_cache:
            return self.path_cache[path_fragment]

        directory, partial_file = os.path.split(path_fragment)
        if not os.path.isdir(directory):
            return []

        try:
            paths = [
                os.path.join(directory, f)
                for f in os.listdir(directory)
                if f.startswith(partial_file)
            ]
            escaped_paths = [path.replace(" ", r"\ ") for path in paths]
            self.path_cache[path_fragment] = escaped_paths
            return escaped_paths
        except Exception as e:
            logger.error(f"Error returning paths: {e}")
            return []

    def splitPath(self, path):
        parts = path.split(" ", 1)
        if len(parts) > 1 and parts[0] in self.commands:
            return [parts[1]]
        return [path]

    def pathFromIndex(self, index):
        text = super().pathFromIndex(index)
        currentText = self.widget().text()
        command = currentText.split(" ", 1)[0]
        if command in self.commands:
            return f"{command} {text}"
        return text


class CommandInputArea(QLineEdit):
    updateCentralDisplayArea = pyqtSignal(str)
    updateCentralDisplayAreaForApi = pyqtSignal(str)
    update_ai_notes = pyqtSignal(str)
    update_code_analysis = pyqtSignal(str)
    update_suggestions_notes = pyqtSignal(str)
    indexing_completed = pyqtSignal()
    report_submitted_successfully = pyqtSignal()
    report_not_submitted_successfully = pyqtSignal()
    feedback_submitted_successfully = pyqtSignal()
    api_call_execution_finished = pyqtSignal()
    threads_status = pyqtSignal(str)
    number_of_autonomous_commands_signal = pyqtSignal(int)
    halt_autonomous_jobs = pyqtSignal()
    update_store = pyqtSignal(str)
    update_data = pyqtSignal(str)
    update_data_error = pyqtSignal(str)
    model_created = pyqtSignal(bool)
    model_busy_busy_signal = pyqtSignal(bool)

    def __init__(self, parent=None, manager=None):
        super().__init__(parent)

        self.apiCallThread = None
        self.start_up = False
        self.isSelectingText = False
        self.setToolTip("Enter a command, start your command with ! for API calls")
        self.api_tasks = 0
        self.fileCompleter = DynamicCompleter(self)
        self.fileCompleter.popup().setStyleSheet("QListView { color: white; }")
        self.fileCompleter.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(self.fileCompleter)
        self.fileCompleter.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.textChanged.connect(self.fileCompleter.update_model)
        self.autonomous_mode = False
        self.web_mode = False
        self.tools_agent_mode = False
        self.contextMenu = self.createContextMenu()
        self.history = []
        self.manager = manager
        self.free_model_creation_in_progress = False
        self.CONFIG = self.manager.load_config()
        self.load_command_history()
        self.history_watcher = QFileSystemWatcher([self.CONFIG["HISTORY_FILE"]])
        self.history_watcher.fileChanged.connect(self.load_command_history)
        self.commands = []
        self.currentCommandIndex = 0
        self.history_index = -1
        self.returnPressed.connect(lambda: self.execute_command(self.text()))
        self.number_of_autonomous_commands = 0
        self.terminal = TerminalEmulator(manager=self.manager)
        self.terminal.password_mode.connect(self.set_password_mode)
        self.password_mode = False
        self.number_of_autonomous_commands_signal.connect(
            self.terminal.update_number_of_autonomous_commands
        )
        self.threadpool = QThreadPool()
        self.model = None
        self.terminal.start()
        self.terminal.data_ready.connect(self.update_terminal_output)
        self.terminal.busy.connect(self.set_style_sheet)
        self.unique_commands_to_run = set()
        self.unique_information = set()
        self.unique_vulnerabilities_confirmed = set()
        self.unique_evidence = set()
        self.incognito_mode = False
        self.queued_autonomous_items_for_api = 0
        self.model_busy = False
        num_cores = os.cpu_count()

        self.threadPool = QThreadPool()
        self.threadPool.setMaxThreadCount(num_cores)
        self.terminal.current_directory_changed.connect(
            self.on_current_directory_changed
        )
        self.current_directory = os.getcwd()
        self.command_prefixes = [
            "cat",
            "more",
            "less",
            "head",
            "tail",
            "nl",
            "tac",
            "bat",
            "awk",
            "sed",
        ]
        self.tools_agent = None
        self.general_agent = None

    def set_agent_mode(self, mode):
        if mode:
            self.tools_agent_mode = True
        else:
            self.tools_agent_mode = False

    def create_agent(self, agent_type):
        if agent_type == "tools_agent":
            agent_attr, tools, template = "tools_agent", [BASH_TOOL, SEARCH_TOOL], None
        elif agent_type == "notes_agent":
            agent_attr, tools, template = "notes_agent", [SEARCH_TOOL], constants.NOTES_TEMPLATE
        elif agent_type == "suggestions_agent":
            agent_attr, tools, template = "suggestions_agent", [SEARCH_TOOL], constants.SUGGESTION_TEMPLATE
        else:
            agent_attr, tools, template = "general_agent", [SEARCH_TOOL], None

        # Only create the agent if it hasn't been created yet and if self.model is available
        if not getattr(self, agent_attr) and self.model:
            try:
                # If a template is provided, pass it via agent_kwargs (using system_message)
                agent_kwargs = {"system_message": template} if template else {}
                agent = initialize_agent(
                    tools,
                    self.model,
                    agent=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION,
                    verbose=True,
                    handle_parsing_errors=True,
                    memory=MEMORY,
                    agent_kwargs=agent_kwargs,
                )
                setattr(self, agent_attr, agent)
                return agent
            except Exception as e:
                logger.error("Error during agent creation: %s", e, exc_info=True)
                raise

    def set_password_mode(self, data: bool):
        """
        Set password mode based on the input data.

        Args:
            data (bool): A flag to determine if password mode should be enabled.

        """
        try:
            if data:
                self.setEchoMode(QLineEdit.EchoMode.Password)
                self.password_mode = True
                logger.info("Password mode enabled.")
            else:
                self.setEchoMode(QLineEdit.EchoMode.Normal)
                self.password_mode = False
                logger.info("Password mode disabled.")
        except Exception as e:
            logger.error(f"An error occurred while setting password mode: {e}")

    def set_style_sheet(self, data):
        if data:
            self.setStyleSheet(
                """
    QLineEdit {
        border: 1px solid orange; /* Change 'red' to your preferred color */
    }
"""
            )
        else:
            self.setStyleSheet(
                """
    QLineEdit {
        border: 1px solid #555; 
"""
            )

    def on_current_directory_changed(self, current_directory):
        self.current_directory = current_directory

    def set_incognito_mode(self, data):
        logger.debug("setting incognito mode")
        if data:
            self.incognito_mode = True
        else:
            self.incognito_mode = False

    def update_terminal_output(self, data):
        data = utilities.process_output(data)
        # logger.debug(
        #     # f"received data for central display area update from signal: {data}"
        # )
        if data and data != " ":
            self.updateCentralDisplayArea.emit(data)

    def update_terminal_output_for_api(self, command, data):
        # logger.debug(f"received data for terminal update from signal {data}")
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")
        self.updateCentralDisplayAreaForApi.emit(data)

    def update_model_status_in_autonomous_mode(self):
        self.threads_status.emit("completed")

    def keyPressEvent(self, event):
        try:
            # Handling arrow keys for history navigation
            if event.key() == Qt.Key.Key_Up:
                if self.history and self.history_index > 0:
                    self.history_index -= 1
                self.setText(self.history[self.history_index])
                event.accept()

            elif event.key() == Qt.Key.Key_Down:
                if self.history_index < len(self.history) - 1:
                    self.history_index += 1
                    self.setText(self.history[self.history_index])
                else:
                    self.history_index = len(self.history)
                    self.clear()
                event.accept()

            # Handling special key combinations (Ctrl+C, Ctrl+\)
            elif (
                event.key() == Qt.Key.Key_C
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.terminal.write("<Ctrl-C>")
                if self.autonomous_mode:
                    self.halt_autonomous_jobs.emit()

                self.currentCommandIndex = 0
                self.commands = []
                self.number_of_autonomous_commands = 0
                self.queued_autonomous_items_for_api = 0
                self.terminal.autonomous_terminal_execution_iteration_is_done.emit()
                self.terminal.busy.emit(False)

            elif (
                event.key() == Qt.Key.Key_Backslash
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self.terminal.write("<Ctrl-\\>")
                if self.autonomous_mode:
                    self.halt_autonomous_jobs.emit()
                self.currentCommandIndex = 0
                self.commands = []
                self.number_of_autonomous_commands = 0
                self.queued_autonomous_items_for_api = 0
                self.terminal.autonomous_terminal_execution_iteration_is_done.emit()
                self.terminal.busy.emit(False)

            # Handling paste operation with Ctrl+V
            elif (
                event.key() == Qt.Key.Key_V
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                clipboard = QApplication.clipboard()
                clipboard_text = clipboard.text()
                if len(clipboard_text) > 100:  # Example threshold
                    self.handle_large_paste(clipboard_text)
                else:
                    super().keyPressEvent(event)

            # Handling the Enter/Return key
            elif event.key() in [Qt.Key.Key_Enter, Qt.Key.Key_Return]:
                if self.completer() and self.completer().popup().isVisible():
                    event.ignore()
                else:
                    super().keyPressEvent(event)
                    self.setEchoMode(QLineEdit.EchoMode.Normal)
                    self.password_mode = False

            # For all other keys, use the default handling
            else:
                super().keyPressEvent(event)
                # After handling the key press, check if the text length exceeds the threshold
                current_text = self.text()
                if len(current_text) > 100:  # Adjust the threshold as needed
                    self.handle_large_paste(current_text)

        except Exception as e:
            logger.error(f"An error occurred: {e}")

    def handle_large_paste(self, text):

        command_input_area = False
        current_text = self.text()  # Get the current text from the widget
        dialog = utilities.EditCommandDialog(
            command_text=current_text,
            parent=self,
            command_input_area=command_input_area,
        )
        if dialog.exec():
            edited_text = dialog.get_command()
            self.setText(edited_text)  # Set the text area to the edited text

    def load_command_history(self, _filepath=None):
        if os.path.exists(self.CONFIG["HISTORY_FILE"]):
            try:
                with open(self.CONFIG["HISTORY_FILE"], "r") as file:
                    self.history = file.read().splitlines()
            except IOError as e:
                logger.error(f"An error occurred while reading the file: {e}")
                return []
        else:
            try:
                open(self.CONFIG["HISTORY_FILE"], "a").close()
            except IOError as e:
                logger.error(f"An error occurred while creating the file: {e}")
            self.history = []

    def add_to_command_history(self, command):
        if not self.history or (self.history and self.history[-1] != command):
            if not self.password_mode:
                self.history.append(command.replace("\n", "").replace("\r", ""))
                self.history_index = len(self.history)
                self.write_history_to_file(command, self.CONFIG["HISTORY_FILE"])

    def write_history_to_file(self, command, filename):
        try:
            with open(filename, "a") as file:
                file.write(command + "\n")
        except IOError as e:
            logger.error(f"An error occurred while writing to the file: {e}")

    def execute_command(self, command=None):
        if self.autonomous_mode and (
            command.startswith("!") or command.startswith("?!")
        ):
            utilities.show_message(
                "Autonomous Mode is enabled",
                "Disable autonomous mode to interact with the AI Assistants",
            )
            return
        if command is None:
            command = self.command_input_area.text().strip()
        self.add_to_command_history(command)
        if command.startswith("!"):
            logger.debug("command assistant invoked")
            command = command.replace("!", "").strip()

            self.execute_api_call(command, endpoint="command")

            self.clear()

        else:
            if utilities.contains_only_spaces(command):
                return
            try:
                self.set_style_sheet(True)
                # logger.debug(f"executing next non autonomous command {command}")
                split_command = command.split(maxsplit=1)
                actual_command = split_command[0].strip()
                # logger.debug(f"actual command is: {actual_command}")

                if len(split_command) > 1:
                    # logger.debug(f"path is: {split_command[1]}")
                    file_path_argument = split_command[
                        1
                    ].strip()  # Only extract if there's a second part
                else:
                    file_path_argument = ""

                # logger.debug(f"length of command is {len(split_command)}")

                if actual_command in self.command_prefixes and file_path_argument:
                    # logger.debug(f"File viewing command detected: {command}")

                    # Adjust the file path for relative paths
                    if not os.path.isabs(file_path_argument):
                        file_path = os.path.join(
                            self.current_directory, file_path_argument
                        )
                    else:
                        file_path = file_path_argument

                    # Check if the file exists and its size
                    if os.path.isfile(file_path):
                        file_size = os.path.getsize(file_path)
                        if file_size > 1024 * 1024:  # File is larger than 1MB
                            self.show_large_file_warning()
                            self.set_style_sheet(False)
                            self.clear()
                            return
                    else:
                        logger.error(f"File not found: {file_path}")
                        return  # Exit if file does not exist

                    # Escape only the file path part of the command
                    escaped_path = utilities.escape_file_path(file_path)
                    # logger.debug(f"escaped: {escaped_path}")
                    command_escaped = f"{actual_command} {escaped_path}"  # Reconstruct the command with the escaped path
                    command_escaped = command_escaped.replace("\n", " ")
                    # logger.debug(f"Command to run: {command_escaped}")

                    self.terminal.write(command_escaped + "\n")

                else:
                    # Handle commands that are not associated with file operations or missing a path
                    # logger.debug(f"Non-file operation command detected: {command}")
                    command_escaped = command  # Non-file commands are used as they are without escaping
                    command_escaped = command_escaped.replace("\n", " ")
                    # logger.debug(f"Command to run: {command_escaped}")
                    self.terminal.write(command_escaped + "\n")

                self.terminal.update_current_command(command_escaped)
                self.clear()

            except Exception as e:
                logger.error(f"Error executing command '{command}': {e}")

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

    def set_neutron_creation_status(self, data):
        logger.debug(f"setting model creation progress to {data}")
        self.free_model_creation_in_progress = data

    def startModelCreation(self):
        worker = ModelCreationWorker(self.manager)
        if not worker:
            utilities.show_message(
                "Unable to load model",
                "Could not start model, ensure you have access to it, check the logs for more info",
            )
            return
        worker.signals.modelCreated.connect(self.onModelCreated)  # Connect to your slot
        worker.signals.modelName.connect(self.setModelName)
        worker.signals.modelCreationInProgress.connect(self.set_neutron_creation_status)
        try:
            self.threadPool.start(worker)
        except Exception as e:
            logger.error(f"Unable to create model{e}")
        logger.info("model creation in progress")

    def setModelName(self, model_name):
        self.model_name = model_name

    def onModelCreated(self, model):
        if model:
            self.threads_status.emit("completed")
            self.model = model  # Now you have your model ready to be used
            self.model_created.emit(True)

    def create_model(self, status):
        # Load the configuration to get the requested model name.
        config = self.manager.load_config()
        requested_model_name = config.get("MODEL", "")

        # If a model is already loaded, check if it's the same as requested.
        if self.model is not None:
            if self.model_name == requested_model_name:
                logger.info(
                    "Requested model '%s' is already loaded. Skipping creation.",
                    requested_model_name,
                )
                return
            else:
                logger.info(
                    "Different model requested. Offloading current model and creating new one."
                )
                self.offload_current_model()

        # Now that we have ensured no model or an old model is present, create the new model.
        if status is True:
            self.threads_status.emit("in_progress")
            logger.debug("Loading model '%s'.", requested_model_name)
            self.startModelCreation()

    def offload_current_model(self):
        """
        Offload the current model from GPU memory.
        This function moves the model to CPU (if possible), deletes the model instance,
        and clears the CUDA cache.
        """
        if self.model is not None:
            # Remove the model reference so that it can be garbage-collected.
            del self.model
            self.model = None
            torch.cuda.empty_cache()
            logger.info("Current model offloaded and GPU cache cleared.")

    def onTaskResult(self, endpoint, command, result):
        if not any(sub in endpoint for sub in ["suggestion", "notes"]):

            utilities.log_command_output("ai", result, self.CONFIG)
        if endpoint == "command":
            logger.debug(f"free model results for command endpoint {result}")
            self.update_terminal_output_for_api(command, result)

        elif endpoint == "notes" or endpoint == "notes_files":
            self.api_tasks += 1
            logger.debug(
                "Endpoint is NOTE_TAKING_API_GATEWAY_ENDPOINT. Setting up finished signal."
            )
            try:

                self.update_ai_notes_function(command, result)

                logger.debug("Successfully connected to update_ai_notes_function.")
            except TypeError as e:
                logger.debug(f"Failed to connect to update_ai_notes_function: {e}")
        elif endpoint == "suggestion" or endpoint == "suggestion_files":
            self.api_tasks += 1
            logger.debug(
                "Endpoint is SUGGESTIONS_API_GATEWAY_ENDPOINT. Setting up finished signal."
            )
            try:

                self.update_suggestion_notes_function(command, result)

                logger.debug(
                    "Successfully connected to update_suggestion_notes_function."
                )
            except TypeError as e:
                logger.debug(
                    f"Failed to connect to update_suggestion_notes_function: {e}"
                )

    def onTaskFinished(self):
        self.threads_status.emit("completed")
        QApplication.restoreOverrideCursor()
        self.model_busy_busy_signal.emit(False)
        self.model_busy = False

    def execute_api_call(self, command=None, endpoint=None):
        logger.debug(
            f"Model Creation Progress is {self.free_model_creation_in_progress}"
        )
        if self.free_model_creation_in_progress:
            utilities.show_message(
                "Model Creation In Progress",
                "The Model is still being downloaded or loaded, please wait. You can check the progress in the terminal where you launched Nebula",
            )
            return
        logger.debug(f"Starting execute_api_call to endpoint: {endpoint} in free mode")

        if not self.model:
            logger.error("No loaded model")
            utilities.show_message(
                "No model loaded",
                "An error may have occurred while loading the selected model during Nebula's startup, also, the models can only be used from the central display area",
            )
            return
        if self.model_busy:
            utilities.show_message(
                "Model is busy",
                "The model is busy, please wait untill the current inference is completed",
            )
            return

        self.threads_status.emit("in_progress")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        if not self.tools_agent_mode:
            # Determine which agent to use based on the endpoint
            if "command" in endpoint:
                if not self.general_agent:
                    self.general_agent = self.create_agent("general_agent")
                agent = self.general_agent
            elif "notes" in endpoint:
                if not self.notes_agent:
                    self.notes_agent = self.create_agent("notes_agent")
                agent = self.notes_agent
            elif "suggestions" in endpoint:
                if not self.suggestions_agent:
                    self.suggestions_agent = self.create_agent("suggestions_agent")
                agent = self.suggestions_agent
            else:
                # Fallback to the general agent if no matching endpoint is found
                if not self.general_agent:
                    self.general_agent = self.create_agent("general_agent")
                agent = self.general_agent

            # Create and start the task with the chosen agent
            model_task = AgentTaskRunner(agent, command, endpoint)
            self.model_busy = True
            self.model_busy_busy_signal.emit(True)
            model_task.signals.result.connect(self.onTaskResult)
            model_task.signals.finished.connect(self.onTaskFinished)
            self.threadpool.start(model_task)
        else:
            if not self.tools_agent:
                self.tools_agent=self.create_agent("tools_agent")
            model_task = AgentTaskRunner(self.tools_agent, command,endpoint)
            self.model_busy = True
            self.model_busy_busy_signal.emit(True)
            model_task.signals.result.connect(self.onTaskResult)
            model_task.signals.finished.connect(self.onTaskFinished)  # If needed
            self.threadpool.start(model_task)

    def parse_json(self, data):
        try:
            # Check the data type
            if isinstance(data, list):
                # Directly parse the first element of the list as JSON
                return json.loads(data[0])
            else:
                # If data is a string, load it directly
                return json.loads(data)
        except Exception as e:
            logger.error(f"error while parsing json: {e}")
            return None  # You might want to return None or handle the error differently

    def tell_main_window_file_processing_that_api_call_execution_is_ready(self):
        self.api_call_execution_finished.emit()

    def tell_user_that_feedback_has_been_submitted(self, _=None):
        self.threads_status.emit("completed")
        self.feedback_submitted_successfully.emit()

    def update_suggestion_notes_function(self, command, data):
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")

        self.update_suggestions_notes.emit(data)

    def update_ai_notes_function(self, command, data):
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")
        start_time = time.time()

        self.update_ai_notes.emit(data)

        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating AI Notes function took {duration} seconds.")

    def update_store_function(self, data):
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")
        start_time = time.time()

        self.update_store.emit(data)

        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating store function took {duration} seconds.")

    def update_data_function(self, data):
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")
        start_time = time.time()

        if "error" in data:
            self.update_data_error.emit(data)
        else:
            logger.debug(f"updating data {data}")
            self.update_data.emit(data)
        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating decoy function took {duration} seconds.")

    def update_code_analysis_function(self, command, data):
        self.api_tasks -= 1
        if self.api_tasks <= 0:
            self.threads_status.emit("completed")
        start_time = time.time()

        self.update_code_analysis.emit(data)

        end_time = time.time()
        duration = end_time - start_time
        logger.debug(f"Updating code analysis function took {duration} seconds.")

    def disable_completer(self, _=None):
        self.setCompleter(None)

    def createContextMenu(self, _=None):
        context_menu = QMenu(self)
        context_menu.setStyleSheet(
            """
            QMenu::item:selected {
                background-color:#333333; 
            }
        """
        )
        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(self.copy_selected_text)
        context_menu.addAction(copy_action)
        exclude_action = QAction("Exclude", self)
        exclude_action.triggered.connect(self.excludeWord)
        context_menu.addAction(exclude_action)
        return context_menu

    def excludeWord(self, _=None):
        selected_text = self.selectedText()  # If lineEdit is your QLineEdit
        # If no text is selected, selectedText will be an empty string
        if not selected_text:
            # If you want to work with the entire content if no selection
            selected_text = self.text()

        if selected_text.strip():
            self.CONFIG = self.manager.load_config()
            with open(self.CONFIG["PRIVACY_FILE"], "a") as file:
                file.write(selected_text + "\n")

    def copy_selected_text(self, _=None):
        QApplication.clipboard().setText(self.selectedText())

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.isSelectingText = True

    def mouseReleaseEvent(self, event: QMouseEvent):
        super().mouseReleaseEvent(event)
        if self.isSelectingText and self.selectedText():
            self.contextMenu.exec(event.globalPosition().toPoint())
        self.isSelectingText = False

    def on_indexing_completed(self, _=None):
        self.indexing_completed.emit()
