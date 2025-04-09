import os
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot
from .conversation_memory import ConversationMemory
from langchain_ollama import ChatOllama
from langchain_community.tools import DuckDuckGoSearchRun
from . import constants
from .log_config import setup_logging
from .chroma_manager import ChromaManager
# Initialize a search tool (used by the LLM)
SEARCH_TOOL = DuckDuckGoSearchRun(return_direct=True)
logger = setup_logging(log_file=os.path.join(constants.SYSTEM_LOGS_DIR, "status_feed.log"))

########################################################################
# Worker Signals & status Feed Worker
########################################################################

class WorkerSignals(QObject):
    # Signal to send the status feed result back to the caller
    finished = pyqtSignal(object)

class statusFeedWorker(QRunnable):
    """
    A worker for querying the RAG index and retrieving a status feed via the LLM.
    """
    def __init__(self, query: str, llm, max_results: int = 2, status_feed_rag=None, command_history_rag=None):
        super().__init__()
        self.query = query
        self.llm = llm
        self.max_results = max_results
        self.status_feed_rag = status_feed_rag
        self.command_history_rag = command_history_rag
        self.signals = WorkerSignals()
        logger.info(f"[statusFeedWorker] Initialized with query: {query}")

    @pyqtSlot()
    def run(self):
        logger.info(f"[statusFeedWorker] Running with max_results = {self.max_results}")
        try:
            if not self.status_feed_rag:
                raise ValueError("statusFeedRAG is not initialized.")
            if not self.command_history_rag:
                raise ValueError("statusFeedRAG is not initialized.")

            # Build a prompt that instructs the LLM to generate a status feed
            prompt = (f"The following is the history of notes and suggestions during a penetration testing engagement. Cosntruct a query based on it that can be used to search a vector db which contains the results of commands. The query will be used to query another vector database to determine the which of the notes and suggestions have been actioned {self.query}"
            )
            vector_db_query = self.llm.invoke(prompt)

            logger.info(f"The result of query construction is {vector_db_query}")
            logger.info("[statusFeedWorker] Querying the RAG index...")
            # Query the RAG (vector store) using the provided query string
            docs = self.status_feed_rag(vector_db_query, k=self.max_results)
            formatted_results = [doc.page_content.strip() for doc in docs]
            logger.info(f"[statusFeedWorker] Retrieved {len(formatted_results)} documents from the RAG.")
            
            # Build a prompt that instructs the LLM to generate a status feed
            prompt = (
                f"Using the following context, generate a list of statuss. "
                f"If no statuss are found, simply respond with 'Answers not found'.\n"
                f"Context: {formatted_results}"
            )
            logger.info(f"[statusFeedWorker] Invoking LLM with prompt: {prompt}")
            response = self.llm.invoke(prompt)
            output = response.content if hasattr(response, "content") else response
            logger.info("[statusFeedWorker] LLM response obtained.")
        except Exception as e:
            logger.error(f"[statusFeedWorker] Error during status feed update: {e}")
            output = None
        # Emit the result back to the main thread (or caller)
        self.signals.finished.emit(output)

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
            file_path=os.path.join(self.CONFIG["MEMORY_DIRECTORY"], "conversation_memory.json")
        )
        self.suggestions_memory = ConversationMemory(
            file_path=os.path.join(self.CONFIG["MEMORY_DIRECTORY"], "suggestions_memory.json")
        )
        self.notes_memory = ConversationMemory(
            file_path=os.path.join(self.CONFIG["MEMORY_DIRECTORY"], "notes_memory.json")
        )
        
        # Get a global thread pool instance for running asynchronous tasks.
        self.thread_pool = QThreadPool.globalInstance()
        logger.info("[statusFeedManager] Initialized and obtained QThreadPool instance.")
        
        # Define the tools to bind with ChatOllama
        self.tools = [SEARCH_TOOL]

    def update_status_feed(self):
        """
        Retrieves the conversation context from the suggestions memory,
        creates a query, and uses a worker thread to fetch status feed data.
        """
        logger.info("[statusFeedManager] Starting status feed update...")
        
         # Initialize the vector store manager.
        self.command_history_rag = ChromaManager(collection_name="engagement_collection", persist_directory=os.path.join(self.CONFIG["ENGAGEMENT_FOLDER"], "engagementdb"))
        
        # Load documents from the directory.
        docs = self.command_history_rag.load_documents(self.CONFIG["LOG_DIRECTORY"])
        logger.info(f"Loaded {len(docs)} documents from {self.CONFIG['LOG_DIRECTORY']}")
        
        # Add the documents to the vector store.
        self.command_history_rag.add_documents(docs)
        # Build conversation context for the query
        # Create a context string for suggestions.
        suggestions_context = "\n".join(
            f"{msg['role']}: {msg['content']}" for msg in self.suggestions_memory.history
        )

        # Create a context string for notes.
        notes_context = "\n".join(
            f"{msg['role']}: {msg['content']}" for msg in self.notes_memory.history
        )

        # Combine them together with headings.
        conversation_context = (
            "Suggestions:\n" + suggestions_context + "\n\n" +
            "Notes:\n" + notes_context
        )
        logger.info(f"[statusFeedManager] Conversation context constructed: {conversation_context}")
        
        # Construct the query string based on conversation context.
        query = f"Generate a status feed based on the following conversation context:\n{conversation_context}"
        logger.info(f"[statusFeedManager] Constructed status feed query: {query}")
        
        # Initialize the RAG component (vector store) for status feed
        try:
            self.status_feed_rag = ChromaManager(
                collection_name="nebula_collection",
                persist_directory=self.CONFIG["THREAT_DB_PATH"],
            )
            logger.info("[statusFeedManager] statusFeedRAG initialized successfully.")
        except Exception as e:
            logger.error(f"[statusFeedManager] Failed to initialize statusFeedRAG: {e}")
            return
        
        # Initialize the LLM using ChatOllama and bind the search tool
        try:
            logger.info("[statusFeedManager] Initializing ChatOllama LLM...")
            self.llm = ChatOllama(model=self.CONFIG["MODEL"]).bind_tools(self.tools)
            logger.info("[statusFeedManager] ChatOllama LLM initialized successfully.")
        except Exception as e:
            logger.error(f"[statusFeedManager] Failed to initialize ChatOllama: {e}")
            self.llm = None
            return
        
        # Create the worker for status feed retrieval
        worker = statusFeedWorker(query, self.llm, max_results=2, status_feed_rag=self.status_feed_rag,command_history_rag=self.command_history_rag)
        worker.signals.finished.connect(self.on_status_feed_update)
        self.thread_pool.start(worker)
        logger.info("[statusFeedManager] status feed worker started.")

    def on_status_feed_update(self, result):
        """
        Callback function that is invoked when the status feed worker finishes.
        The 'result' is expected to be a string (or data structure) that you then
        pass to your UI update callback function.
        """
        logger.info(f"[statusFeedManager] status feed worker finished with result: {result}")
        if result is not None:
            # Call the callback (defined in main) to update the status feed UI.
            self.update_ui_callback(result)
            logger.info("[statusFeedManager] UI updated with new status feed data.")
        else:
            logger.error("[statusFeedManager] status feed update failed; no data received.")
