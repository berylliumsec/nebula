import json
import os
import warnings

from PyQt6.QtCore import QFile, QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QTextEdit, QVBoxLayout)

from . import constants
from .log_config import setup_logging
from .update_utils import return_path

warnings.filterwarnings("ignore")

logger = setup_logging(log_file=os.path.join(constants.SYSTEM_LOGS_DIR, "setup.log"))


class settings(QDialog):
    setupCompleted = pyqtSignal(str)

    def __init__(self, engagement_folder=None):
        super().__init__()
        self.settings = QSettings("Beryllium Security", "Nebula")
        self.engagementFolder = engagement_folder
        self.engagementName = (
            os.path.basename(engagement_folder) if engagement_folder else None
        )

        self.setObjectName("EngagementSettings")

        # Initialize default ChromaDB directory to empty (it is required to be set by the user)
        self.chromadbDir = ""
        self.threatdbDir = ""
        self.default_ollama_url = "http://localhost:11434"
        self.ollama_url = self.default_ollama_url

        self.initUI()
        if self.engagementFolder:
            self.folderPathLabel.setText(self.engagementName)
            self.handle_engagement_selection()

    def load_stylesheet(self, filename):
        style_file = QFile(filename)
        style_file.open(QFile.OpenModeFlag.ReadOnly | QFile.OpenModeFlag.Text)
        self.original_stylesheet = style_file.readAll().data().decode("utf-8")
        self.setStyleSheet(self.original_stylesheet)

    def initUI(self):
        self.load_stylesheet(return_path("config/dark-stylesheet.css"))
        self.setWindowTitle("Engagement Settings")
        self.setObjectName("EngagementSettings")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setSizeGripEnabled(True)
        self.setMinimumSize(500, 600)
        self.resize(700, 800)

        layout = QVBoxLayout()

        # --- Engagement Folder ---
        self.folderBtn = QPushButton("Select Engagement Folder")
        self.folderBtn.clicked.connect(self.selectFolder)
        layout.addWidget(self.folderBtn)
        logger.info("Setup folder button")

        self.folderPathLabel = QLabel(
            "You must select an Engagement Folder before any other option"
        )
        layout.addWidget(self.folderPathLabel)
        logger.info("Setup folder label")

        # --- Text Inputs for IPs, URLs, and Lookout Items ---
        self.ipAddressesInput = QTextEdit()
        self.urlsInput = QTextEdit()
        self.lookoutInput = QTextEdit()

        self.ipAddressesInput.setPlaceholderText(
            "Enter IP addresses (one per line) (Optional)"
        )
        self.urlsInput.setPlaceholderText("Enter URLs (one per line) (Optional)")
        self.lookoutInput.setPlaceholderText(
            "Enter things to lookout for (one per line) (Optional)"
        )

        for inputWidget in [self.ipAddressesInput, self.urlsInput, self.lookoutInput]:
            layout.addWidget(inputWidget)
        logger.info("Setup IP addresses, URLs and lookout items inputs")

        # Provider selection is intentionally independent of installed API keys.
        providerLabel = QLabel("AI Provider")
        layout.addWidget(providerLabel)
        self.providerComboBox = QComboBox()
        self.providerComboBox.addItem("Ollama (local)", "ollama")
        self.providerComboBox.addItem("OpenAI", "openai")
        layout.addWidget(self.providerComboBox)
        self.internetSearchCheckbox = QCheckBox(
            "Allow the AI assistant to search the internet"
        )
        self.internetSearchCheckbox.setChecked(False)
        layout.addWidget(self.internetSearchCheckbox)

        # --- Model Selection ---
        self.model_name = ""
        self.modelLabel = QLabel("Enter model name e.g: deepseek-r1:32b ")
        layout.addWidget(self.modelLabel)

        self.modelLineEdit = QLineEdit()
        self.modelLineEdit.setPlaceholderText("deepseek-r1:32b")
        self.modelLineEdit.textChanged.connect(self.onModelChanged)
        modelLayout = QHBoxLayout()
        modelLayout.addWidget(self.modelLineEdit)
        layout.addLayout(modelLayout)

        self.onModelChanged(self.modelLineEdit.text())

        # --- ChromaDB Directory Selection (Required) ---
        ollamaTitleLabel = QLabel(
            "Ollama URL (Optional, will use the default if not provided)"
        )
        layout.addWidget(ollamaTitleLabel)

        # Create an editable QLineEdit with placeholder text
        self.ollamaLineEdit = QLineEdit()
        self.ollamaLineEdit.setPlaceholderText("https://your-ollama-server:port")
        self.ollamaLineEdit.setText(self.default_ollama_url)
        layout.addWidget(self.ollamaLineEdit)

        # --- ChromaDB Directory Selection (Required) ---
        chromadbDirTitleLabel = QLabel("ChromaDB Directory (Required)")
        layout.addWidget(chromadbDirTitleLabel)

        chromadbLayout = QHBoxLayout()
        self.chromadbDirLineEdit = QLineEdit()

        self.chromadbDirLineEdit.setReadOnly(True)
        self.chromadbDirBtn = QPushButton("Browse...")
        self.chromadbDirBtn.clicked.connect(self.selectChromaDBDir)
        chromadbLayout.addWidget(self.chromadbDirLineEdit)
        chromadbLayout.addWidget(self.chromadbDirBtn)
        layout.addLayout(chromadbLayout)

        # Retained only so old engagement files remain round-trippable. There is
        # no ThreatDB integration in the open-source 2.x runtime.
        threatdbDirTitleLabel = QLabel(
            "Legacy ThreatDB Directory (compatibility only; currently unused)"
        )

        layout.addWidget(threatdbDirTitleLabel)

        threatdbLayout = QHBoxLayout()
        self.threatdbDirLineEdit = QLineEdit()
        self.threatdbDirLineEdit.setReadOnly(True)
        self.threatdbDirBtn = QPushButton("Browse...")
        self.threatdbDirBtn.clicked.connect(self.selectthreatDBDir)
        threatdbLayout.addWidget(self.threatdbDirLineEdit)
        threatdbLayout.addWidget(self.threatdbDirBtn)
        layout.addLayout(threatdbLayout)

        # --- Save Button ---
        self.saveBtn = QPushButton("Save Engagement")

        self.saveBtn.clicked.connect(self.saveEngagement)
        layout.addWidget(self.saveBtn)

        self.setLayout(layout)
        self.center()
        logger.info("Setup layout completed")

        # Disable all configuration widgets until an engagement folder is selected.
        self.enableSettings(False)

    def enableSettings(self, enabled: bool):
        """Enable or disable configuration settings until an engagement folder is selected."""
        self.ipAddressesInput.setEnabled(enabled)
        self.urlsInput.setEnabled(enabled)
        self.lookoutInput.setEnabled(enabled)
        self.providerComboBox.setEnabled(enabled)
        self.internetSearchCheckbox.setEnabled(enabled)
        self.modelLineEdit.setEnabled(enabled)
        self.chromadbDirLineEdit.setEnabled(enabled)
        self.chromadbDirBtn.setEnabled(enabled)
        self.saveBtn.setEnabled(enabled)

    def onModelChanged(self, text):
        self.model_name = text
        logger.debug(f"Model selected: {text}")

    def center(self):
        screen = QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        window_geometry = self.frameGeometry()
        window_geometry.moveCenter(screen_geometry.center())
        self.move(window_geometry.topLeft())

    def selectFolder(self):
        try:
            # Retrieve last used directory or default to the user's home directory.
            last_directory = self.settings.value(
                "last_directory", os.path.expanduser("~")
            )
            folder_path = QFileDialog.getExistingDirectory(
                self, "Select Engagement Folder", last_directory
            )
            if folder_path:
                # Save the chosen directory for next time.
                self.settings.setValue("last_directory", folder_path)

                self.engagementFolder = folder_path
                self.engagementName = os.path.basename(folder_path)
                self.folderPathLabel.setText(self.engagementName)
                logger.info(f"Engagement folder set to: {folder_path}")
                self.handle_engagement_selection()
        except Exception as e:
            logger.error(f"Error selecting folder: {e}")
            logger.debug("Failed in selectFolder")

    def selectChromaDBDir(self):
        try:
            start_dir = (
                self.chromadbDir or self.engagementFolder or os.path.expanduser("~")
            )
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select ChromaDB Directory", start_dir
            )
            if selected_dir:
                self.chromadbDir = selected_dir
                self.chromadbDirLineEdit.setText(selected_dir)
                logger.info(f"ChromaDB directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting ChromaDB directory: {e}")

    def selectthreatDBDir(self):
        try:
            start_dir = (
                self.threatdbDir or self.engagementFolder or os.path.expanduser("~")
            )
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select threatDB Directory", start_dir
            )
            if selected_dir:
                self.threatdbDir = selected_dir
                self.threatdbDirLineEdit.setText(selected_dir)
                logger.info(f"threatDB directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting threatDB directory: {e}")

    def handle_engagement_selection(self):
        self.prefill_paths_with_engagement()
        self.loadEngagementDetails()
        self.apply_default_paths()
        self.enableSettings(True)

    def apply_default_paths(self):
        if not self.engagementFolder:
            return

        if not self.chromadbDirLineEdit.text().strip():
            self.chromadbDir = self.engagementFolder
            self.chromadbDirLineEdit.setText(self.engagementFolder)

        if not self.threatdbDirLineEdit.text().strip():
            self.threatdbDir = self.engagementFolder
            self.threatdbDirLineEdit.setText(self.engagementFolder)

        if not self.ollamaLineEdit.text().strip():
            self.ollama_url = self.default_ollama_url
            self.ollamaLineEdit.setText(self.default_ollama_url)

    def prefill_paths_with_engagement(self):
        if not self.engagementFolder:
            return
        self.chromadbDir = self.engagementFolder
        self.threatdbDir = self.engagementFolder
        self.chromadbDirLineEdit.setText(self.engagementFolder)
        self.threatdbDirLineEdit.setText(self.engagementFolder)

    def loadEngagementDetails(self):
        if not self.engagementFolder:
            logger.debug("No engagement folder set.")
            return {}

        file_path = os.path.join(self.engagementFolder, "engagement_details.json")
        if not os.path.exists(file_path):
            logger.debug(f"Engagement details file does not exist at path: {file_path}")
            return {}

        try:
            with open(file_path, "r") as file:
                details = json.load(file)
                self.ipAddressesInput.setText(
                    "\n".join(details.get("ip_addresses", []))
                )
                self.urlsInput.setText("\n".join(details.get("urls", [])))
                self.lookoutInput.setText("\n".join(details.get("lookout_items", [])))
                self.model_name = details.get("model")
                provider = details.get("ai_provider")
                if not provider and isinstance(details.get("ollama"), bool):
                    provider = "ollama" if details["ollama"] else "openai"
                provider = str(provider or "ollama").strip().lower()
                provider_index = self.providerComboBox.findData(provider)
                if provider_index >= 0:
                    self.providerComboBox.setCurrentIndex(provider_index)
                self.internetSearchCheckbox.setChecked(
                    bool(details.get("use_internet_search", False))
                )
                self.ollama_url = details.get(
                    "ollama_url", self.default_ollama_url
                )
                self.modelLineEdit.setText(self.model_name)

                # Load the ChromaDB directory from details if available.
                default_dir = self.engagementFolder or ""
                self.chromadbDir = details.get("chromadb_dir", default_dir)
                self.chromadbDirLineEdit.setText(self.chromadbDir)
                self.threatdbDir = details.get("threatdb_dir", default_dir)
                self.threatdbDirLineEdit.setText(self.threatdbDir)
                self.ollamaLineEdit.setText(self.ollama_url)
                return details
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in loadEngagementDetails: {e}")
            logger.debug(f"Failed in loadEngagementDetails with file_path: {file_path}")
            return {}
        except Exception as e:
            logger.error(f"Error loading engagement details: {e}")
            logger.debug(f"Failed in loadEngagementDetails with file_path: {file_path}")
            return {}

    def saveEngagement(self):
        if not self.engagementFolder:
            self.folderPathLabel.setText("Please select an engagement folder.")
            logger.warning("No engagement folder selected when trying to save.")
            return

        try:
            ip_addresses = [
                ip.strip()
                for ip in self.ipAddressesInput.toPlainText().splitlines()
                if ip.strip()
            ]
            urls = [
                url.strip()
                for url in self.urlsInput.toPlainText().splitlines()
                if url.strip()
            ]
            lookout_items = [
                item.strip()
                for item in self.lookoutInput.toPlainText().splitlines()
                if item.strip()
            ]

            chromadb_dir = self.chromadbDirLineEdit.text().strip()
            threatdb_dir = (
                self.threatdbDirLineEdit.text().strip() or self.engagementFolder
            )
            self.model_name = self.modelLineEdit.text().strip()
            if not chromadb_dir:
                QMessageBox.warning(
                    self, "Input Error", "Please select a ChromaDB directory."
                )
                return
            if not self.model_name:
                QMessageBox.warning(self, "Input Error", "Please enter a model name")
                return
            ollama_url = self.ollamaLineEdit.text().strip()
            ai_provider = self.providerComboBox.currentData()
            current_engagement_settings = {
                "engagement_name": self.engagementName,
                "ip_addresses": ip_addresses,
                "urls": urls,
                "lookout_items": lookout_items,
                "ai_provider": ai_provider,
                "model": self.model_name,
                "chromadb_dir": chromadb_dir,
                "threatdb_dir": threatdb_dir,
                # Retain the old flag so previous 2.x builds can still open the
                # engagement without using API-key presence as selection logic.
                "ollama": ai_provider == "ollama",
                "ollama_url": ollama_url,
                "use_internet_search": self.internetSearchCheckbox.isChecked(),
            }
            file_path = os.path.join(self.engagementFolder, "engagement_details.json")
            with open(file_path, "w") as file:
                json.dump(current_engagement_settings, file, indent=4)
            self.folderPathLabel.setText(
                f"Engagement details saved in {self.engagementFolder}"
            )
            logger.info(
                f"Engagement details saved successfully in {self.engagementFolder}."
            )
            self.setupCompleted.emit(self.engagementFolder)

        except Exception as e:
            self.folderPathLabel.setText("Error saving engagement details.")
            logger.error(f"Error saving engagement details: {e}")
