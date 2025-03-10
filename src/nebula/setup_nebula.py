import json
import os
import warnings

import torch
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QTextEdit,
                             QVBoxLayout, QWidget, QCheckBox)  # Added QCheckBox

from . import constants, utilities
from .log_config import setup_logging

warnings.filterwarnings("ignore")

logger = setup_logging(log_file=os.path.join(constants.SYSTEM_LOGS_DIR, "setup.log"))


class settings(QWidget):
    setupCompleted = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.engagementFolder = None
        self.engagementName = None

        # Set default cache directory: use the TRANSFORMERS_CACHE env variable if available, otherwise fallback
        self.defaultCacheDir = os.getenv(
            "TRANSFORMERS_CACHE",
            os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "transformers"),
        )
        self.cacheDir = self.defaultCacheDir
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

        self.ipAddressesInput.setPlaceholderText("Enter IP addresses (one per line) (Optional)")
        self.urlsInput.setPlaceholderText("Enter URLs (one per line) (Optional)")
        self.lookoutInput.setPlaceholderText("Enter things to lookout for (one per line) (Optional)")

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
        self.modelComboBox.currentTextChanged.connect(self.onModelChanged)
        modelLayout = QHBoxLayout()
        modelLayout.addWidget(self.modelComboBox)
        layout.addLayout(modelLayout)

        # --- Ollama Checkbox ---
        # This checkbox indicates that the model choice is Ollama.
        # If checked, downstream code should skip model creation and use Ollama.
        self.ollamaCheckbox = QCheckBox("Use Ollama")
        self.ollamaCheckbox.setFont(QFont("Arial", 10))
        self.ollamaCheckbox.setStyleSheet("color: white;")
        layout.addWidget(self.ollamaCheckbox)

        # Now that the checkbox exists, update the model change state.
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
        # Read-only because we want users to use the "Browse" button
        self.cacheDirLineEdit.setReadOnly(True)
        self.cacheDirBtn = QPushButton("Browse...")
        self.cacheDirBtn.setFont(QFont("Arial", 10))
        self.cacheDirBtn.clicked.connect(self.selectCacheDir)
        cacheLayout.addWidget(self.cacheDirLineEdit)
        cacheLayout.addWidget(self.cacheDirBtn)
        layout.addLayout(cacheLayout)

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
        self.ollamaCheckbox.setEnabled(enabled)  # Enable/disable the checkbox as well
        self.cacheDirLineEdit.setEnabled(enabled)
        self.cacheDirBtn.setEnabled(enabled)
        self.saveBtn.setEnabled(enabled)

    def onModelChanged(self, text):
        self.model_name = text
        logger.debug(f"Model selected: {text}")
        
        # If using Ollama, bypass GPU/model-creation checks.
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
            folder_path = QFileDialog.getExistingDirectory(self, "Select Engagement Folder")
            if folder_path:
                # Optionally warn if switching folders might overwrite unsaved changes.
                self.engagementFolder = folder_path
                self.engagementName = os.path.basename(folder_path)
                self.folderPathLabel.setText(self.engagementName)
                logger.info(f"Engagement folder set to: {folder_path}")
                self.loadEngagementDetails()
                # Enable settings now that an engagement folder is selected.
                self.enableSettings(True)
        except Exception as e:
            logger.error(f"Error selecting folder: {e}")
            logger.debug("Failed in selectFolder")

    def selectCacheDir(self):
        try:
            selected_dir = QFileDialog.getExistingDirectory(self, "Select Cache Directory")
            if selected_dir:
                self.cacheDir = selected_dir
                self.cache_dir_updated = True  # Mark that the cache directory has been updated by the user
                self.cacheDirLineEdit.setText(selected_dir)
                logger.info(f"Cache directory updated to: {selected_dir}")
        except Exception as e:
            logger.error(f"Error selecting cache directory: {e}")

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
                self.ipAddressesInput.setText("\n".join(details.get("ip_addresses", [])))
                self.urlsInput.setText("\n".join(details.get("urls", [])))
                self.lookoutInput.setText("\n".join(details.get("lookout_items", [])))
                self.model_name = details.get("model", "deepseek-ai/DeepSeek-R1-Distill-Llama-8B")
                self.modelComboBox.setCurrentText(self.model_name)
                
                # If the config has an "ollama" flag, update the checkbox state accordingly.
                if details.get("ollama", False):
                    self.ollamaCheckbox.setChecked(True)
                else:
                    self.ollamaCheckbox.setChecked(False)

                # Only update the cache directory if the user has not already changed it
                if not self.cache_dir_updated:
                    self.cacheDir = details.get("cache_dir", self.defaultCacheDir)
                    self.cacheDirLineEdit.setText(self.cacheDir)

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
            # Directly read the current values from the UI.
            ip_addresses = [ip.strip() for ip in self.ipAddressesInput.toPlainText().splitlines() if ip.strip()]
            urls = [url.strip() for url in self.urlsInput.toPlainText().splitlines() if url.strip()]
            lookout_items = [item.strip() for item in self.lookoutInput.toPlainText().splitlines() if item.strip()]
            # Capture the current model selection
            self.model_name = self.modelComboBox.currentText()
            cache_dir = self.cacheDirLineEdit.text()

            # Build the settings dictionary using the current UI values.
            current_engagement_settings = {
                "engagement_name": self.engagementName,
                "ip_addresses": ip_addresses,
                "urls": urls,
                "lookout_items": lookout_items,
                "model": self.model_name,
                "cache_dir": cache_dir,
                "ollama": self.ollamaCheckbox.isChecked()  # Save the state of the Ollama checkbox
            }

            # Save the settings to the file.
            file_path = os.path.join(self.engagementFolder, "engagement_details.json")
            with open(file_path, "w") as file:
                json.dump(current_engagement_settings, file, indent=4)

            self.folderPathLabel.setText(f"Engagement details saved in {self.engagementFolder}")
            logger.info(f"Engagement details saved successfully in {self.engagementFolder}.")
            self.setupCompleted.emit(self.engagementFolder)
            # Reset the flag after saving.
            self.cache_dir_updated = False

        except Exception as e:
            self.folderPathLabel.setText("Error saving engagement details.")
            logger.error(f"Error saving engagement details: {e}")
