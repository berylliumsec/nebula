import configparser
import datetime
import fcntl
import os
import re
import shlex
import struct
import subprocess
import sys
import termios
import xml.etree.ElementTree as ET

import tiktoken
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QMessageBox,
                             QPushButton, QScrollArea, QTextEdit, QVBoxLayout)

from . import constants
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/utilities.log")


DARK_STYLE_SHEET = """
    QMessageBox {
        background-color:  #1e1e1e;
        color: white;
    }
    QPushButton {
        background-color: #1E1E1E;
        color: white;
        border: 1px solid  #333333;
        border-radius: 5px;
        padding: 5px;
        min-height: 20px;  /* Adjust size as needed */
    }
    QPushButton:hover {
        background-color: #333333;
    }
    QPushButton:pressed {
        background-color:  #333333;
    }
"""


class LongPressButton(QPushButton):
    clicked = pyqtSignal()  # Emitted on regular click
    longPressed = pyqtSignal()  # Custom signal for long press
    longPressProgress = pyqtSignal(bool)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFixedHeight(50)
        self.longPressTimer = QTimer(self)
        self.longPressTimer.setSingleShot(True)
        self.longPressTimer.timeout.connect(self.onLongPress)
        self.pressed.connect(
            self.onStartPress
        )  # Built-in signal, emitted when the button is initially pressed
        self.released.connect(self.onEndPress)

    def onStartPress(self):
        self.longPressTimer.start(3000)  # 3000 ms equals 3 seconds
        self.longPressProgress.emit(True)

    def onEndPress(self):
        if self.longPressTimer.isActive():
            self.longPressTimer.stop()  # The button was released before the timer finished, so it's a regular click
            self.longPressProgress.emit(False)
            self.onClick()

    def onClick(self):
        self.clicked.emit()  # Emit the regular clicked signal

    def onLongPress(self):
        self.longPressed.emit()  # Emit the custom longPressed signal
        self.longPressProgress.emit(False)
        logger.debug("Long pressed")


class EditCommandDialog(QDialog):
    def __init__(self, command_text, parent=None, command_input_area=None):
        super().__init__(parent)

        self.setObjectName("EditCommandDialog")
        self.setModal(True)
        self.command_input_area = command_input_area
        self.text = command_text

        self.layout = QVBoxLayout(self)

        if self.command_input_area:
            self.setWindowTitle("Ask Terminal Assistant")

            # Create the QTextEdit for displaying commands
            self.command_display = QTextEdit(self.text, self)
            self.command_display.setReadOnly(True)
            self.command_display.setStyleSheet(
                "color: white; background-color: #333333; font-style: italic;"
            )

            # Create a QScrollArea and set command_display as its widget
            scroll_area = QScrollArea(self)
            scroll_area.setWidgetResizable(True)  # Make it resizable
            scroll_area.setWidget(self.command_display)

            self.layout.addWidget(scroll_area)
            self.user_input_edit = QTextEdit(self)
            self.layout.addWidget(self.user_input_edit)
        else:
            self.setWindowTitle("Edit Command|Question")
            self.user_input_edit = QTextEdit(self)
            self.user_input_edit.setText(self.text)
            self.user_input_edit.moveCursor(QTextCursor.MoveOperation.End)
            self.layout.addWidget(self.user_input_edit)

        self.setup_buttons()
        self.resize(800, 600)

    def setup_buttons(self):
        buttons_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons_layout.addWidget(self.ok_button)
        buttons_layout.addWidget(self.cancel_button)
        self.layout.addLayout(buttons_layout)

    def get_command(self):
        user_input = self.user_input_edit.toPlainText().strip()
        if self.command_input_area:
            if not user_input.startswith("!"):
                user_input = "!" + user_input
        return f"{user_input}: {self.text}" if self.command_input_area else user_input


def set_terminal_size(fd, rows, cols):
    winsize = struct.pack("HHHH", rows, cols, 1180, 700)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def open_url(url):
    subprocess.run(
        ["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def show_last_line(document):
    last_block = document.lastBlock()
    last_line = last_block.text()
    return last_line


def strip_ansi_codes(text):
    try:
        if not isinstance(text, str):
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            else:
                text = str(text)

        # Updated regex pattern to also catch sequences ending with the ASCII Bell character
        ansi_escape_extended = re.compile(
            r"""
            (\x1B   # ESC
            [\[\]()#;?]*  # Optional intro characters
            (?:    # 7-bit C1 Fe (except CSI)
                [@-Z\\-_]  # Ending characters for CSI codes
            |      # or [ for CSI, followed by a more extensive control sequence
                \[
                [0-?]*  # Parameter bytes
                [ -/]*  # Intermediate bytes
                [@-~]   # Final byte
            ))
            |(\x1B\[  # ESC[
            [0-9;?]*  # Optional parameters
            [a-zA-Z])  # Final character (letter)
            |[0-9]+l  # Matches like '9l' without beginning ESC
            |\x1B[>]  # Matches like ESC> without following characters
            |\[\!?  # Matches like '[!' or '[?'
            |\][0-9]+  # Matches like ']104'
            |[0-9]+;  # Matches like '3;'
            |[0-9]+  # Matches sequences ending with the ASCII Bell character
            |\x07  # ASCII Bell character
            |\\x1bc
            """,
            re.VERBOSE,
        )
        return ansi_escape_extended.sub("", text)

    except Exception as e:
        # Assuming logger.debug is defined elsewhere in your code
        logger.debug(f"Unexpected error processing input: {e}")
        return text if isinstance(text, str) else ""


def get_llm_instance(model: str, ollama_url: str = "", signals: pyqtSignal = None):
    """
    Factory method to create the LLM instance.
    If OPENAI_API_KEY is set, use LangChain's ChatOpenAI.
    Otherwise, use ChatOllama.
    This design allows adding additional AI service branches later.
    """
    if os.getenv("OPENAI_API_KEY"):
        logger.info("OPENAI_API_KEY found. Using ChatOpenAI from LangChain.")

        # If the provided model is not suitable for OpenAI (e.g. 'mistral' is an Ollama default),
        # you might remap it to an appropriate default like 'gpt-3.5-turbo'. Adjust as needed.

        llm_instance = ChatOpenAI(model_name=model)
        ollama_or_openai = "openai"
    else:
        logger.info("OPENAI_API_KEY not set. Using ChatOllama.")
        try:
            ollama_or_openai = "openai"
            if ollama_url:
                llm_instance = ChatOllama(model=model, base_url=ollama_url)
            else:
                llm_instance = ChatOllama(model=model)
        except Exception as e:
            signals.error.emit(str(e))
            logger.error("Error Loading Ollama", e)
            raise e
    return llm_instance, ollama_or_openai


def show_message(title, message):
    msg_box = QMessageBox()
    msg_box.setWindowTitle(title)
    msg_box.setText(message)
    msg_box.setStyleSheet(DARK_STYLE_SHEET)
    msg_box.exec()


def is_included_command(command, CONFIG):
    try:
        selected_tools = CONFIG.get("SELECTED_TOOLS", [])

        if selected_tools:
            if any(command.startswith(inc_cmd) for inc_cmd in selected_tools):
                logger.debug(f"Command '{command}' is included, hence will be logged")
                return True
            else:
                logger.debug(
                    f"Command '{command}' is not included, hence will not be logged"
                )
        else:
            logger.debug("No selected tools specified in the configuration.")

    except Exception as e:
        logger.error(f"An error occurred: {e}")

    return False


def is_linux_asking_for_password(text):
    """
    Checks if the given text (which can be a str or bytes) indicates that Linux is asking for a password.

    Args:
    - text (str or bytes): The text to check.

    Returns:
    - bool: True if the text matches the pattern indicating a password request, False otherwise.
            Returns None if an error occurs during processing.
    """
    try:
        # If text is bytes, convert to string first
        if isinstance(text, bytes):
            text = text.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.error(f"Error decoding bytes to string: {e}")
        return None  # Or any other indication that an error occurred

    try:
        # Enhanced pattern to catch more generic password prompts
        pattern = r"(\[sudo\] password for \w+|\bpassword\b:|Password for [\w\\]+|Enter [Pp]assword|[\w\s]+@[\w\.]+\'s password|\w+'s password|password for \w+|please enter [Pp]assword|authentication [Pp]assword|login: \w+'s password|[\w\s]+ password required|Password required for \w+|password to connect to [\w\.]+)"
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error as e:
        logger.error(f"Regex error: {e}")
        return None  # Or any other indication that an error occurred


def log_command_output(command, current_command_output, CONFIG):
    if not current_command_output.strip():
        logger.warning("Current command output is empty. Nothing to write.")
        return
    logger.info(f"The command output to be intelligently named is {command}")
    try:
        base_filename = create_filename_from_command(command)
        if not base_filename:
            logger.error("Failed to create a valid filename.")
            return

        file_path = os.path.join(CONFIG["LOG_DIRECTORY"], base_filename)

        counter = 1
        while os.path.exists(file_path):
            name, ext = os.path.splitext(base_filename)
            new_filename = f"{name}({counter}){ext}"
            file_path = os.path.join(CONFIG["LOG_DIRECTORY"], new_filename)
            counter += 1

        with open(file_path, "w") as file:
            if not current_command_output.endswith("\n"):
                current_command_output += "\n"
            file.write(current_command_output)
            logger.debug(f"Successfully wrote command output to {file_path}")

    except Exception as e:
        logger.error(f"Error writing command output to file: {str(e)}")


def create_filename_from_command(command):
    try:
        logger.debug("Creating filename...")

        name = command.split()[0]
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"{name}_{timestamp}"  # Default filename pattern

        return filename

    except Exception as e:
        logger.error(f"Error in creating filename: {str(e)}")
        return None


def process_output(data):
    try:
        data = strip_ansi_codes(data)

        def replace_tabs_with_spaces(match):
            current_line = match.group(1)
            length_so_far = len(current_line) % 8
            return current_line + " " * (8 - length_so_far)

        data = re.sub(r"(.*?)\t", replace_tabs_with_spaces, data)
        while ".\b" in data:  # Continuously remove character followed by backspace
            data = re.sub(".\b", "", data)
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        data = re.sub(r"^%+\s*\n", "", data, flags=re.MULTILINE)
        return data

    except Exception as e:
        logger.error(
            "An error occurred while processing the output: %s", e, exc_info=True
        )
        return None


def get_shell_commands():
    try:
        commands = (
            subprocess.check_output('/bin/bash -c "compgen -c"', shell=True)
            .decode()
            .split()
        )
        commands.sort()
        return commands
    except Exception:
        return []


def is_nessus_file(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        if root.tag == "NessusClientData_v2":
            return True

    except ET.ParseError:
        logger.error("The file is not a valid XML file.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")

    return False


def is_zap_file(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        if root.tag == "OWASPZAPReport":
            return True

    except ET.ParseError:
        logger.error("The file is not a valid XML file.")
    except Exception as e:
        logger.error(f"An error occurred while checking for ZAP file: {e}")

    return False


def is_nmap_file(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        if root.tag == "nmaprun":
            return True

    except ET.ParseError:
        logger.error("The file is not a valid XML file.")
    except Exception as e:
        logger.error(f"An error occurred while checking for Nmap file: {e}")

    return False


def is_nikto_file(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        if root.tag == "niktoscan":
            return True

    except ET.ParseError:
        logger.error("The file is not a valid XML file.")
    except Exception as e:
        logger.error(f"An error occurred while checking for Nikto file: {e}")

    return False


def parse_nmap(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()

        script_results_str = ""
        for host in root.findall("./host"):
            for port in host.findall("./ports/port"):
                for script in port.findall("./script"):
                    output = script.attrib.get("output")
                    if (
                        not output
                        or "ERROR: Script execution failed (use -d to debug)" in output
                    ):
                        continue

                    cleaned_output = output.replace("&#xa;", "\n")
                    script_data_str = (
                        f"ID: {script.attrib.get('id')}\nOutput:\n{cleaned_output}\n\n"
                    )
                    script_results_str += script_data_str

        return script_results_str if script_results_str else None
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        return None


def parse_nessus_file(file_path):
    skip_plugins = [
        "OS Security Patch Assessment Not Available",
        "Nessus Scan Information",
        "Backported Security Patch Detection (WWW)",
        "Nessus SYN scanner",
        "Unknown Service Detection: Banner Retrieval",
        "Service Detection",
        "Backported Security Patch Detection (FTP)",
        "WMI Not Available",
    ]

    try:
        tree = ET.parse(file_path)
    except ET.ParseError as e:
        logger.error(f"XML parsing error in file {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
        return None

    vulnerabilities = ""
    root = tree.getroot()

    try:
        for block in root.findall(".//ReportItem"):
            plugin_name = block.get("pluginName")
            if plugin_name in skip_plugins:
                continue

            description_elem = block.find("description")
            description = (
                description_elem.text if description_elem is not None else None
            )

            cves = [cve.text for cve in block.findall("cve") if cve.text is not None]

            port = block.get(
                "port", "Unknown port"
            )  # Default port to 'Unknown' if not found

            if cves:
                for cve in cves:
                    vuln_info_str = (
                        f"CVE: {cve}\nDescription: {description}\nPort: {port}\n\n"
                    )
                    vulnerabilities += vuln_info_str
            else:
                vuln_info_str = (
                    f"CVE: None\nDescription: {description}\nPort: {port}\\n"
                )
                vulnerabilities += vuln_info_str
    except Exception as e:
        logger.error(f"Error processing vulnerabilities in file {file_path}: {e}")
        return None

    if not vulnerabilities:
        logger.debug("No vulnerabilities found.")
        return None

    return vulnerabilities


def parse_zap(file_path):
    try:
        tree = ET.parse(file_path)
    except ET.ParseError as e:
        logger.error(f"XML parsing error in file {file_path}: {e}")
        return ""
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        return ""
    except Exception as e:
        logger.error(f"Unexpected error reading file {file_path}: {e}")
        return ""

    descriptions_str = ""
    root = tree.getroot()

    try:
        for desc in root.findall(".//desc"):
            if desc.text:
                cleaned_text = re.sub(r"</?p>", "", desc.text)
                descriptions_str += cleaned_text + "\n\n"
    except Exception as e:
        logger.error(f"Error processing descriptions in file {file_path}: {e}")
        return ""

    return descriptions_str.strip()


def parse_nikto_xml(file_path):
    descriptions_str = ""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        for item in root.findall(".//scandetails/item"):
            description_elem = item.find("description")
            if description_elem is not None and description_elem.text:
                descriptions_str += description_elem.text.strip() + "\n\n"

        return descriptions_str.strip()
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        return ""


def process_text(text):
    if text is None or text.strip() == "":
        return text  # Or raise ValueError("Input text cannot be None or empty.")

    try:
        # Pattern to identify code blocks.
        code_block_pattern = re.compile(r"```.*?```", re.DOTALL)
        # Pattern to cautiously match command lines and file paths, considering spaces and potential line breaks.
        # This pattern aims to preserve such lines by not adding newlines within them.
        file_path_or_command_pattern = re.compile(
            r"(?:[~/]|\w:)[^\s]*[\w./-]+(?:\s+[^\s]+)*\.\w+", re.DOTALL
        )

        segments = code_block_pattern.split(text)
        delimiters = code_block_pattern.findall(text)

        processed_segments = []
        for segment in segments:
            if not segment.strip().startswith("```"):
                # Split the text while preserving file paths or command lines.
                sub_segments = file_path_or_command_pattern.split(segment)
                path_delimiters = file_path_or_command_pattern.findall(segment)

                processed_sub_segments = []
                for i, sub_segment in enumerate(sub_segments):
                    processed_sub_segments.append(sub_segment)
                    if i < len(path_delimiters):
                        # Reinsert file path or command line segments unchanged.
                        processed_sub_segments.append(path_delimiters[i])

                processed_segments.append("".join(processed_sub_segments))
            else:
                processed_segments.append(segment)

        result_text = ""
        for i, segment in enumerate(processed_segments):
            result_text += segment
            if i < len(delimiters):
                result_text += delimiters[i]

        return result_text
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return text  # Or raise the exception


def encoding_getter(encoding_type: str):
    """
    Returns the appropriate encoding based on the given encoding type (either an encoding string or a model name).
    """
    if "k_base" in encoding_type:
        return tiktoken.get_encoding(encoding_type)
    else:
        return tiktoken.encoding_for_model(encoding_type)


def tokenizer(string: str, encoding_type: str) -> list:
    """
    Returns the tokens in a text string using the specified encoding.
    """
    encoding = encoding_getter(encoding_type)
    tokens = encoding.encode(string)
    return tokens


def token_counter(string: str, encoding_type: str) -> int:
    """
    Returns the number of tokens in a text string using the specified encoding.
    """
    num_tokens = len(tokenizer(string, encoding_type))
    return num_tokens


def check_initial_help(file_path: str = constants.INITIAL_HELP) -> bool:
    """
    Check if the initial help setting is shown. If the file or setting doesn't exist, create it.

    :param file_path: Path to the settings file.
    :return: True if initial help is shown on subsequent runs, otherwise False the first time.
    """
    config = configparser.ConfigParser()

    # Check if the file exists
    if not os.path.exists(file_path):
        logger.debug(
            f"{file_path} does not exist. Creating the file with initialhelpshown = True."
        )
        config["Settings"] = {"initialhelpshown": "True"}
        with open(file_path, "w") as configfile:
            config.write(configfile)
        return False

    # Read the file
    config.read(file_path)

    # Check if the setting exists and is set to True
    if "Settings" in config and config["Settings"].getboolean(
        "initialhelpshown", fallback=False
    ):
        logger.debug("initialhelpshown is True.")
        return True

    # If the setting is not found or is set to False, return False
    logger.debug("initialhelpshown is False or not found. Returning False.")
    return False


def contains_escape_sequences(s):
    # This pattern is expanded to match a broader range of escape sequences.
    # It starts with ESC (escape character), followed by an optional [ or ? or >,
    # then any number of characters not including ESC, and ending with a letter or a few special characters.
    escape_sequence_pattern = re.compile(r"\x1b[\[\]?;]*[0-9;]*[a-zA-Z>~]")
    return bool(escape_sequence_pattern.search(s))


def contains_only_spaces(s):
    # Check if the stripped string is empty
    return s.strip() == "" or s.strip() == "#"


def escape_file_path(file_path):
    # This function now purely escapes paths
    if not isinstance(file_path, str):
        logger.error("Input must be a string.")
        return None

    return shlex.quote(file_path)


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    if getattr(sys, "frozen", False):
        base_path = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def is_run_as_package():
    if os.environ.get("IN_DOCKER"):
        return False
    return "site-packages" in os.path.abspath(__file__)
