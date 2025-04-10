import os
from typing import List

from langchain_community.tools import DuckDuckGoSearchRun
from langchain_ollama import ChatOllama
from pydantic import BaseModel
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from . import constants
from .conversation_memory import ConversationMemory
from .log_config import setup_logging

# Initialize a search tool (used by the LLM)
SEARCH_TOOL = DuckDuckGoSearchRun(return_direct=True)
logger = setup_logging(
    log_file=os.path.join(constants.SYSTEM_LOGS_DIR, "status_feed.log")
)


########################################################################
# Worker Signals & status Feed Worker
########################################################################
class Status(BaseModel):
    status: str


class StatusFeed(BaseModel):
    # Each item in the list must be a SomeStatus instance.
    status_feed: List[Status]


class WorkerSignals(QObject):
    # Signal to send the status feed result back to the caller
    finished = pyqtSignal(object)


class statusFeedWorker(QRunnable):
    """
    A worker for querying the RAG index and retrieving a status feed via the LLM.
    """

    def __init__(
        self,
        query: str,
        llm,
    ):
        super().__init__()
        self.query = query
        self.llm = llm
        self.signals = WorkerSignals()
        logger.info(f"[statusFeedWorker] Initialized with query: {query}")

    @pyqtSlot()
    def run(self):
        max_retries = 3
        attempt = 0
        status_feed = []

        while attempt < max_retries:
            try:
                # Invoke the LLM with the structured output configuration.
                response = self.llm.with_structured_output(
                    StatusFeed, method="json_schema"
                ).invoke(self.query)
                logger.info(f"[statusFeedWorker] LLM response obtained: {status_feed}")

                # Extract the status strings, handling both Status objects and tuples.
                status_list = [status.status for status in response.status_feed]

                # Check that status_list is a list.
                if not isinstance(status_list, list):
                    raise ValueError("Extracted status_feed is not a list")

                break  # Successfully parsed

            except Exception as error:
                attempt += 1
                logger.error(
                    f"Error on attempt {attempt}: {error}",
                    exc_info=True,
                )
                if attempt < max_retries:
                    logger.info("Retrying LLM invocation...")
                else:
                    raise

        # Emit the extracted list back to the main thread (or caller).
        self.signals.finished.emit(status_list)


########################################################################
# statusFeedManager Class
########################################################################


class statusFeedManager:
    def __init__(self, manager, update_ui_callback):
        """
        manager: An object that provides configuration (via load_config())
                 and might be your main application manager.
        update_ui_callback: A function to call with the status feed result.
                            This callback will update your status feed widget.
        """
        self.manager = manager
        self.CONFIG = self.manager.load_config()
        self.update_ui_callback = update_ui_callback

        # Initialize conversation memories
        self.conversation_memory = ConversationMemory(
            file_path=os.path.join(
                self.CONFIG["MEMORY_DIRECTORY"], "conversation_memory.json"
            )
        )
        self.suggestions_memory = ConversationMemory(
            file_path=os.path.join(
                self.CONFIG["MEMORY_DIRECTORY"], "suggestions_memory.json"
            )
        )
        self.notes_memory = ConversationMemory(
            file_path=os.path.join(self.CONFIG["MEMORY_DIRECTORY"], "notes_memory.json")
        )

        self.commands_memory = ConversationMemory(
            file_path=os.path.join(
                self.CONFIG["MEMORY_DIRECTORY"], "commands_memory.json"
            )
        )
        # Get a global thread pool instance for running asynchronous tasks.
        self.thread_pool = QThreadPool.globalInstance()
        logger.info(
            "[statusFeedManager] Initialized and obtained QThreadPool instance."
        )

        # Define the tools to bind with ChatOllama
        self.tools = [SEARCH_TOOL]

    def update_status_feed(self):
        """
        Retrieves the conversation context from the suggestions memory,
        creates a query, and uses a worker thread to fetch status feed data.
        """
        logger.info("[statusFeedManager] Starting status feed update...")

        # Build conversation context for the query
        # Create a context string for suggestions.
        suggestions_context = "\n".join(
            f"{msg['role']}: {msg['content']}"
            for msg in self.suggestions_memory.history
        )

        # Create a context string for notes.
        notes_context = "\n".join(
            f"{msg['role']}: {msg['content']}" for msg in self.notes_memory.history
        )

        commands_context = "\n".join(
            f"{msg['role']}: {msg['content']}" for msg in self.commands_memory.history
        )

        # Combine them together with headings.
        conversation_context = (
            "Suggestions:\n"
            + suggestions_context
            + "\n\n"
            + "Notes:\n"
            + notes_context
            + "Commands/AI responses:\n"
            + commands_context
        )
        logger.info(
            f"[statusFeedManager] Conversation context constructed: {conversation_context}"
        )

        # Construct the query string based on conversation context.
        query = f"Generate a status feed based on the following conversation context:{conversation_context}"

        logger.info(f"[statusFeedManager] Constructed status feed query: {query}")

        # Initialize the LLM using ChatOllama and bind the search tool
        try:
            logger.info("[statusFeedManager] Initializing ChatOllama LLM...")
            self.llm = ChatOllama(model=self.CONFIG["MODEL"])

            logger.info("[statusFeedManager] ChatOllama LLM initialized successfully.")
        except Exception as e:
            logger.error(f"[statusFeedManager] Failed to initialize ChatOllama: {e}")
            self.llm = None
            return

        # Create the worker for status feed retrieval
        worker = statusFeedWorker(
            query,
            self.llm,
        )
        worker.signals.finished.connect(self.on_status_feed_update)
        self.thread_pool.start(worker)
        logger.info("[statusFeedManager] status feed worker started.")

    def on_status_feed_update(self, result):
        """
        Callback function that is invoked when the status feed worker finishes.
        The 'result' is expected to be a string (or data structure) that you then
        pass to your UI update callback function.
        """
        logger.info(
            f"[statusFeedManager] status feed worker finished with result: {result}"
        )
        if result is not None:
            # Call the callback (defined in main) to update the status feed UI.
            self.update_ui_callback(result)
            logger.info("[statusFeedManager] UI updated with new status feed data.")
        else:
            logger.error(
                "[statusFeedManager] status feed update failed; no data received."
            )
