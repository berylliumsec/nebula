
from PyQt6.QtCore import Qt, QThreadPool
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from . import constants

# Import the worker from your chroma_manager module.
from .chroma_manager import AddDocumentsWorker
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/document_loader.log")


class DocumentLoaderDialog(QDialog):
    def __init__(self, vector_db, parent=None):
        logger.debug(
            "Initializing DocumentLoaderDialog with manager: %s, parent: %s",
            vector_db,
            parent,
        )
        super().__init__(parent)
        self.vector_db = vector_db
        self.setWindowTitle("Document Loader")
        self.init_ui()

    def init_ui(self):
        logger.debug("Initializing UI components for DocumentLoaderDialog")
        layout = QVBoxLayout()

        # Row: ComboBox and Browse Button
        top_layout = QHBoxLayout()
        self.type_combo = QComboBox()
        self.type_combo.addItems(
            ["url", "pdf", "text", "csv", "json", "jsonl", "directory"]
        )
        self.type_combo.currentTextChanged.connect(self.on_type_change)
        top_layout.addWidget(self.type_combo)

        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse)
        top_layout.addWidget(self.browse_button)

        layout.addLayout(top_layout)

        # Line Edit for URL or file/folder path.
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Enter URL or file/folder path")
        layout.addWidget(self.input_field)

        # Load Document Button.
        self.load_button = QPushButton("Load Document")
        self.load_button.clicked.connect(self.load_document)
        layout.addWidget(self.load_button)

        self.setLayout(layout)
        logger.debug("UI components set up successfully")
        self.on_type_change(self.type_combo.currentText())

    def on_type_change(self, type_str):
        logger.debug("Type changed to: %s", type_str)
        if type_str == "url":
            self.browse_button.setEnabled(False)
            self.input_field.setPlaceholderText("Enter URL here")
        elif type_str == "directory":
            self.browse_button.setEnabled(True)
            self.input_field.setPlaceholderText("Browse for a folder")
        else:
            self.browse_button.setEnabled(True)
            self.input_field.setPlaceholderText("Enter file path or browse...")

    def browse(self):
        input_type = self.type_combo.currentText()
        if input_type == "directory":
            folder = QFileDialog.getExistingDirectory(self, "Select Folder")
            if folder:
                self.input_field.setText(folder)
        else:
            file_filter = ""
            if input_type == "pdf":
                file_filter = "PDF Files (*.pdf)"
            elif input_type in ["text", "json", "jsonl"]:
                file_filter = "Text Files (*.txt *.json *.jsonl);;All Files (*)"
            elif input_type == "csv":
                file_filter = "CSV Files (*.csv)"
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Select File", "", file_filter
            )
            if file_path:
                self.input_field.setText(file_path)

    def load_document(self):
        source = self.input_field.text().strip()
        source_type = self.type_combo.currentText().strip()
        logger.info(
            "Load document triggered with source: '%s' and source_type: '%s'",
            source,
            source_type,
        )
        if not source:
            QMessageBox.warning(
                self, "Input Error", "Please enter a URL or file/folder path."
            )
            return
        try:
            docs = self.vector_db.load_documents(source, source_type=source_type)
            # Create a progress dialog.
            progress_dialog = QProgressDialog(
                "Adding documents...", "Cancel", 0, 100, self
            )
            progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
            progress_dialog.setValue(0)
            progress_dialog.show()

            # Use a batch size of 100 for more granular progress updates.
            batch_size = 100
            worker = AddDocumentsWorker(self.vector_db, docs, batch_size=batch_size)
            worker.signals.progress.connect(progress_dialog.setValue)
            worker.signals.finished.connect(
                lambda: (
                    progress_dialog.close(),
                    QMessageBox.information(
                        self,
                        "Documents Loaded",
                        f"Loaded {len(docs)} document(s) from {source} as {source_type}.",
                    ),
                )
            )
            worker.signals.error.connect(
                lambda e: (
                    progress_dialog.close(),
                    QMessageBox.critical(self, "Error", f"Error adding documents: {e}"),
                )
            )
            # Connect the Cancel button of the progress dialog to cancel the worker.
            progress_dialog.canceled.connect(
                lambda: worker.cancel() if hasattr(worker, "cancel") else None
            )
            QThreadPool.globalInstance().start(worker)
        except Exception as e:
            logger.exception(
                "Error loading documents from source: '%s', source_type: '%s'",
                source,
                source_type,
            )
            QMessageBox.critical(self, "Error", f"Error loading documents: {e}")
