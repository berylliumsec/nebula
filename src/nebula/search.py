from langchain_huggingface import HuggingFaceEmbeddings
from langchain_ollama import ChatOllama
from PyQt6.QtCore import (
    QObject,
    QRunnable,
    QStringListModel,
    Qt,
    QThreadPool,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtWidgets import QCompleter, QLineEdit

from . import constants, utilities
from .chroma_manager import ChromaManager
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/search.log")

embeddings_model = HuggingFaceEmbeddings(model_name="all-MiniLM-L12-v2")


class WorkerSignals(QObject):
    finished = pyqtSignal(object)  # Signal to send the search results back.


class SearchWorker(QRunnable):
    def __init__(self, query: str, llm, max_results: int = 2, rag=None):
        super().__init__()
        self.query = query
        self.llm = llm
        self.max_results = max_results
        self.rag = rag
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        logger.info(
            f"[Worker] Starting search for query: {self.query} with max_results={self.max_results}"
        )
        try:
            if not self.rag:
                raise ValueError("ChromaManager (rag) is not initialized.")
            logger.info("[Worker] Querying the ChromaDB index...")
            docs = self.rag.query(self.query, k=1)
            formatted_results = [doc.page_content.strip() for doc in docs]
            logger.info(
                f"[Worker] Retrieved {len(formatted_results)} documents from the index"
            )
            prompt = (
                f"Answer this question: {self.query} based on the context, "
                f"if the context does not contain the answer to the question, simply respond with 'Answers not found', "
                f"context: {formatted_results}."
            )
            logger.info(f"[Worker] Invoking LLM with prompt: {prompt}")
            response = self.llm.invoke(prompt)
            # If response is an object with a 'content' attribute, use that; otherwise, use the raw response.
            output = response.content if hasattr(response, "content") else response
            logger.info("[Worker] LLM response obtained.")
        except Exception as e:
            logger.error(f"[Worker] Error during search: {e}")
            output = None
        # Emit the result back to the main thread.
        self.signals.finished.emit(output)


class CustomSearchLineEdit(QLineEdit):
    resultSelected = pyqtSignal(str)

    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        # Set up completer for the search suggestions.
        self.completer = QCompleter(self)
        self.completer.setModel(QStringListModel())
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.activated.connect(self.onResultSelected)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(self.completer)
        self.manager = manager
        self.CONFIG = self.manager.load_config()

        # Initialize ChatOllama LLM.
        try:
            logger.info("[Main] Initializing ChatOllama with model")
            if self.CONFIG["OLLAMA_URL"]:
                self.llm = ChatOllama(
                    model=self.CONFIG["MODEL"], base_url=self.CONFIG["OLLAMA_URL"]
                )
            else:
                self.llm = ChatOllama(model=self.CONFIG["MODEL"])
            logger.info("[Main] ChatOllama initialized successfully.")
        except Exception as e:
            logger.error(f"[Main] Failed to initialize ChatOllama: {e}")
            utilities.show_message(
                "Error Loading Ollama",
                "Ollama could not be loaded, please check the url in engagement settings and try again",
            )

            self.llm = None

        self.returnPressed.connect(self.onReturnPressed)
        self.threadpool = QThreadPool.globalInstance()

        # Initialize ChromaManager for ChromaDB.
        try:
            logger.info(
                f"[Main] Initializing ChromaManager for collection at {self.CONFIG['CHROMA_DB_PATH']}"
            )
            self.rag = ChromaManager(
                collection_name="nebula_collection",
                persist_directory=self.CONFIG["CHROMA_DB_PATH"],
            )
            logger.info("[Main] ChromaManager initialized successfully.")
        except Exception as e:
            logger.error(f"[Main] Failed to initialize ChromaManager: {e}")
            self.rag = None

        # One-time check for number of items in the ChromaDB.
        if self.rag:
            try:
                if hasattr(self.rag, "vector_store") and hasattr(
                    self.rag.vector_store, "_collection"
                ):
                    num_items = self.rag.vector_store._collection.count()
                    logger.info(f"[Main] ChromaDB contains {num_items} items.")
                else:
                    logger.info(
                        "[Main] Cannot determine item count; 'vector_store' does not expose '_collection'."
                    )
            except Exception as e:
                logger.error(f"[Main] Failed to check ChromaDB item count: {e}")

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        menu.exec(event.globalPos())

    def onReturnPressed(self):
        query = self.text()
        logger.info(f"[Main] Return pressed. Query received: '{query}'")
        if len(query) > 3:
            if self.llm is None or self.rag is None:
                logger.error(
                    "[Main] LLM or ChromaManager is not available. Aborting search."
                )
                self.resultSelected.emit(
                    "Search functionality is currently unavailable."
                )
                return

            logger.info(
                "[Main] Query length sufficient. Disabling input and starting search worker."
            )
            self.setStyleSheet("border: 2px solid orange;")
            self.setEnabled(False)
            self.clear()
            worker = SearchWorker(query, self.llm, max_results=2, rag=self.rag)
            worker.signals.finished.connect(self.onSearchCompleted)
            self.threadpool.start(worker)
        else:
            logger.info(
                "[Main] Query too short. Emitting resultSelected without search."
            )
            self.resultSelected.emit(query)
            self.clear()

    @pyqtSlot(object)
    def onSearchCompleted(self, response):
        logger.info(
            "[Main] Search worker completed. Re-enabling input and resetting style."
        )
        self.setEnabled(True)
        self.setStyleSheet("")
        if response:
            logger.info(f"[Main] Search results received: {response}")
            self.completer.model().setStringList([response])
            self.resultSelected.emit(response)
            self.completer.complete()
        else:
            logger.info("[Main] No results returned from search worker.")
            self.resultSelected.emit("No results returned from search worker.")
        self.clear()

    def onResultSelected(self, _=None):
        text = self.text()
        logger.info(f"[Main] Result selected: '{text}'")
        self.resultSelected.emit(text)
        self.clear()
