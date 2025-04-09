import json
import os
import warnings

import torch
from PyQt6.QtCore import QSettings, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QTextEdit, QVBoxLayout, QWidget)

from . import constants, utilities
from .log_config import setup_logging

warnings.filterwarnings("ignore")

logger = setup_logging(log_file=os.path.join(constants.SYSTEM_LOGS_DIR, "setup.log"))


class settings(QWidget):
    setupCompleted = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.settings = QSettings("Beryllium Security", "Nebula")
        self.engagementFolder = None
        self.engagementName = None

        # Set default cache directory: use the TRANSFORMERS_CACHE env variable if available, otherwise fallback
        self.defaultCacheDir = os.getenv(
            "TRANSFORMERS_CACHE",
            os.path.join(
                os.path.expanduser("~"), ".cache", "huggingface", "transformers"
            ),
        )
        self.cacheDir = self.defaultCacheDir
        # Initialize default ChromaDB directory to empty (it is required to be set by the user)
        self.chromadbDir = ""
        # Flag to indicate whether the user has updated the cache directory in this session
        self.cache_dir_updated = False
        self.initUI()

    def initUI(self):
        self.setWindowTitle("Engagement Settings")
        self.setObjectName("EngagementSettings")
        self.setGeometry(100, 100, 400, 500)
        self.setStyleSheet(
            """QWidget {
                border: 1px solid #333333;
                background-color: #1e1e1e;
                color: #c5c5c5;
            }
            QPushButton, QRadioButton {
                border: 1px solid #333333;
                background-color: #1e1e1e;
                border: none;
                padding: 5px;
                border-radius: 2px;
                color: white;
            }
            QRadioButton::indicator:checked {
                background-color: #add8e6;
                border: 1px solid white;
            }
            QPushButton:hover, QRadioButton:hover {
                background-color: #333333;
            }
            QTextEdit {
                border: 1px solid #333333;
                border-radius: 2px;
                padding: 5px;
                background-color: #1e1e1e;
                color: white;
            }
            QLabel {
                color: #1e1e1e;
                border: none;
            }"""
        )

        layout = QVBoxLayout()

        # --- Engagement Folder ---
        self.folderBtn = QPushButton("Select Engagement Folder")
        self.folderBtn.setFont(QFont("Arial", 10))
        self.folderBtn.clicked.connect(self.selectFolder)
        layout.addWidget(self.folderBtn)
        logger.info("Setup folder button")

        self.folderPathLabel = QLabel("No engagement folder selected (Required)")
        self.folderPathLabel.setFont(QFont("Arial", 10))
        self.folderPathLabel.setStyleSheet("color: #add8e6;")
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
            inputWidget.setFont(QFont("Arial", 10))
            layout.addWidget(inputWidget)
        logger.info("Setup IP addresses, URLs and lookout items inputs")

        # --- Model Selection ---
        self.model_name = ""
        self.modelLabel = QLabel("Select a model")
        self.modelLabel.setFont(QFont("Arial", 10))
        self.modelLabel.setStyleSheet("color: white;")
        layout.addWidget(self.modelLabel)

        self.modelComboBox = QComboBox()
        self.modelComboBox.setFont(QFont("Arial", 10))
        self.modelComboBox.addItem("mistralai/Mistral-7B-Instruct-v0.2")
        self.modelComboBox.addItem("deepseek-ai/DeepSeek-R1-Distill-Llama-8B")
        self.modelComboBox.addItem("meta-llama/Llama-3.1-8B-Instruct")
        self.modelComboBox.addItem("qwen2.5-coder:32b")
        self.modelComboBox.currentTextChanged.connect(self.onModelChanged)
        modelLayout = QHBoxLayout()
        modelLayout.addWidget(self.modelComboBox)
        layout.addLayout(modelLayout)

        # --- Ollama Checkbox ---
        self.ollamaCheckbox = QCheckBox("Use Ollama")
        self.ollamaCheckbox.setFont(QFont("Arial", 10))
        self.ollamaCheckbox.setStyleSheet("color: white;")
        layout.addWidget(self.ollamaCheckbox)
        self.onModelChanged(self.modelComboBox.currentText())

        # --- Cache Directory Selection ---
        cacheDirTitleLabel = QLabel("Cache Directory")
        cacheDirTitleLabel.setFont(QFont("Arial", 10))
        cacheDirTitleLabel.setStyleSheet("color: white;")
        layout.addWidget(cacheDirTitleLabel)

        cacheLayout = QHBoxLayout()
        self.cacheDirLineEdit = QLineEdit()
        self.cacheDirLineEdit.setFont(QFont("Arial", 10))
        self.cacheDirLineEdit.setText(self.cacheDir)
        self.cacheDirLineEdit.setReadOnly(True)
        self.cacheDirBtn = QPushButton("Browse...")
        self.cacheDirBtn.setFont(QFont("Arial", 10))
        self.cacheDirBtn.clicked.connect(self.selectCacheDir)
        cacheLayout.addWidget(self.cacheDirLineEdit)
        cacheLayout.addWidget(self.cacheDirBtn)
        layout.addLayout(cacheLayout)

        # --- ChromaDB Directory Selection (Required) ---
        chromadbDirTitleLabel = QLabel("ChromaDB Directory (Required)")
        chromadbDirTitleLabel.setFont(QFont("Arial", 10))
        chromadbDirTitleLabel.setStyleSheet("color: white;")
        layout.addWidget(chromadbDirTitleLabel)

        chromadbLayout = QHBoxLayout()
        self.chromadbDirLineEdit = QLineEdit()
        self.chromadbDirLineEdit.setFont(QFont("Arial", 10))
        self.chromadbDirLineEdit.setReadOnly(True)
        self.chromadbDirBtn = QPushButton("Browse...")
        self.chromadbDirBtn.setFont(QFont("Arial", 10))
        self.chromadbDirBtn.clicked.connect(self.selectChromaDBDir)
        chromadbLayout.addWidget(self.chromadbDirLineEdit)
        chromadbLayout.addWidget(self.chromadbDirBtn)
        layout.addLayout(chromadbLayout)

        # --- threatDB Directory Selection (Required) ---
        threatdbDirTitleLabel = QLabel("ThreatDB Directory (Required)")
        threatdbDirTitleLabel.setFont(QFont("Arial", 10))
        threatdbDirTitleLabel.setStyleSheet("color: white;")
        layout.addWidget(threatdbDirTitleLabel)

        threatdbLayout = QHBoxLayout()
        self.threatdbDirLineEdit = QLineEdit()
        self.threatdbDirLineEdit.setFont(QFont("Arial", 10))
        self.threatdbDirLineEdit.setReadOnly(True)
        self.threatdbDirBtn = QPushButton("Browse...")
        self.threatdbDirBtn.setFont(QFont("Arial", 10))
        self.threatdbDirBtn.clicked.connect(self.selectthreatDBDir)
        threatdbLayout.addWidget(self.threatdbDirLineEdit)
        threatdbLayout.addWidget(self.threatdbDirBtn)
        layout.addLayout(threatdbLayout)

        # --- Save Button ---
        self.saveBtn = QPushButton("Save Engagement")
        self.saveBtn.setFont(QFont("Arial", 10))
        self.saveBtn.setStyleSheet(
            """
            QPushButton {
                border: none;
                background-color: #1e1e1e;
                padding: 5px;
                border-radius: 2px;
                color: white;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            """
        )
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
        self.modelComboBox.setEnabled(enabled)
        self.ollamaCheckbox.setEnabled(enabled)
        self.cacheDirLineEdit.setEnabled(enabled)
        self.cacheDirBtn.setEnabled(enabled)
        self.chromadbDirLineEdit.setEnabled(enabled)
        self.chromadbDirBtn.setEnabled(enabled)
        self.saveBtn.setEnabled(enabled)

    def onModelChanged(self, text):
        self.model_name = text
        logger.debug(f"Model selected: {text}")
        if not self.ollamaCheckbox.isChecked() and not torch.cuda.is_available():
            utilities.show_systems_requirements_message(
                "Important information",
                "No GPU(s) available. You will not be able to use any models",
            )
            return

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
                self.loadEngagementDetails()
                self.enableSettings(True)
        except Exception as e:
            logger.error(f"Error selecting folder: {e}")
            logger.debug("Failed in selectFolder")

    def selectCacheDir(self):
        try:
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select Cache Directory"
            )
            if selected_dir:
                self.cacheDir = selected_dir
                self.cache_dir_updated = True
                self.cacheDirLineEdit.setText(selected_dir)
                logger.info(f"Cache directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting cache directory: {e}")

    def selectChromaDBDir(self):
        try:
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select ChromaDB Directory"
            )
            if selected_dir:
                self.chromadbDir = selected_dir
                self.chromadbDirLineEdit.setText(selected_dir)
                logger.info(f"ChromaDB directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting ChromaDB directory: {e}")

    def selectthreatDBDir(self):
        try:
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select threatDB Directory"
            )
            if selected_dir:
                self.threatdbDir = selected_dir
                self.threatdbDirLineEdit.setText(selected_dir)
                logger.info(f"threatDB directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting threatDB directory: {e}")

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
                self.model_name = details.get(
                    "model", "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
                )
                self.modelComboBox.setCurrentText(self.model_name)
                if details.get("ollama", False):
                    self.ollamaCheckbox.setChecked(True)
                else:
                    self.ollamaCheckbox.setChecked(False)
                if not self.cache_dir_updated:
                    self.cacheDir = details.get("cache_dir", self.defaultCacheDir)
                    self.cacheDirLineEdit.setText(self.cacheDir)
                # Load the ChromaDB directory from details if available.
                self.chromadbDir = details.get("chromadb_dir", "")
                self.chromadbDirLineEdit.setText(self.chromadbDir)
                self.threatdbDir = details.get("threatdb_dir", "")
                self.threatdbDirLineEdit.setText(self.threatdbDir)
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
            self.model_name = self.modelComboBox.currentText()
            cache_dir = self.cacheDirLineEdit.text()
            chromadb_dir = self.chromadbDirLineEdit.text().strip()
            threatdb_dir = self.chromadbDirLineEdit.text().strip()
            if not chromadb_dir:
                QMessageBox.warning(
                    self, "Input Error", "Please select a ChromaDB directory."
                )
                return
            if not threatdb_dir:
                QMessageBox.warning(
                    self, "Input Error", "Please select a threatDB directory."
                )
                return
            current_engagement_settings = {
                "engagement_name": self.engagementName,
                "ip_addresses": ip_addresses,
                "urls": urls,
                "lookout_items": lookout_items,
                "model": self.model_name,
                "cache_dir": cache_dir,
                "chromadb_dir": chromadb_dir,
                "threatdb_dir": threatdb_dir,
                "ollama": self.ollamaCheckbox.isChecked(),
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
            self.cache_dir_updated = False
        except Exception as e:
            self.folderPathLabel.setText("Error saving engagement details.")
            logger.error(f"Error saving engagement details: {e}")
