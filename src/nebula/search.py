from typing import List

from PyQt6.QtCore import (QObject, QRunnable, QStringListModel, Qt,
                          QThreadPool, pyqtSignal, pyqtSlot)
from PyQt6.QtWidgets import QCompleter, QLineEdit, QMessageBox
from whoosh import scoring
from whoosh.fields import ID, TEXT, Schema
from whoosh.index import create_in, exists_in, open_dir
from whoosh.qparser import MultifieldParser, OrGroup
from whoosh.writing import AsyncWriter

from . import constants, update_utils
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/search.log")


class WorkerSignals(QObject):
    finished = pyqtSignal()


class IndexingWorker(QRunnable):
    def __init__(self, text: str, indexdir: str, search_window):
        super().__init__()
        self.text = text
        self.indexdir = indexdir
        self.search_window = search_window
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        logger.info("Indexing worker started")
        self.search_window.add_to_index(self.text, self.indexdir)
        self.signals.finished.emit()
        logger.info("Indexing worker finished")


class CustomSearchLineEdit(QLineEdit):
    resultSelected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.completer = QCompleter(self)
        self.completer.setModel(QStringListModel())
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.activated.connect(self.onResultSelected)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(self.completer)
        self.textChanged.connect(self.onTextChanged)
        self.returnPressed.connect(self.onResultSelected)
        self.thread_pool = QThreadPool()

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        menu.exec(event.globalPos())

    def onTextChanged(self, text):
        if len(text) > 3:
            self.perform_search(text)

    def perform_search(self, text):
        logger.info(f"Text changed: {text}")
        results = self.search_index(
            text, update_utils.return_path("command_search_index"), max_results=100
        )
        logger.info(f"Search results: {results}")
        self.completer.model().setStringList(results)
        self.completer.complete()

    def onResultSelected(self, _=None):
        text = self.text()
        parts = text.split(":", 1)
        if len(parts) > 1:
            text_after_colon = parts[1].strip()
            self.resultSelected.emit(text_after_colon)
        else:
            self.resultSelected.emit(self.text())
        self.clear()

    def add_to_index(self, text: str, indexdir: str):
        logger.info(f"Adding text to index: {text}")
        try:
            if not exists_in(indexdir):
                schema = Schema(id=ID(stored=True), content=TEXT(stored=True))
                ix = create_in(indexdir, schema)
            else:
                ix = open_dir(indexdir)

            writer = AsyncWriter(ix)
            writer.add_document(content=text)
            writer.commit()
            logger.info(f"Successfully added entry to index: {text}")
        except Exception as e:
            logger.error(f"Error occurred while adding entry {text} to index: {e}")

    def search_index(
        self, query: str, indexdir: str, max_results: int = 100
    ) -> List[str]:
        """
        Search the index for the given query and sort results by relevance.

        Args:
            query (str): The search query.
            indexdir (str): The directory where the index is stored.
            max_results (int): The maximum number of results to return.

        Returns:
            List[str]: A list of search results.
        """
        logger.info(f"Searching index for query: {query}")
        try:
            ix = open_dir(indexdir)
        except Exception as e:
            logger.error(f"Error occurred while opening index directory: {e}")
            return []

        formatted_results = []

        with ix.searcher(weighting=scoring.BM25F()) as searcher:
            query_parser = MultifieldParser(
                ["content"], schema=ix.schema, group=OrGroup
            )
            parsed_query = query_parser.parse(query)
            logger.info(f"Parsed query: {parsed_query}")

            try:
                results = searcher.search(parsed_query, limit=max_results)
                for res in results:
                    content = res["content"]
                    lines = content.splitlines()
                    for line in lines:
                        if query.lower() in line.lower():
                            formatted_results.append(line.strip())
                            if len(formatted_results) >= max_results:
                                break
            except Exception as e:
                logger.error(f"Error occurred while searching for query {query}: {e}")

        # Log the final results for debugging
        logger.info(f"Formatted search results: {formatted_results}")

        return formatted_results

    def index_file(self, file_path: str):
        """Index the file for search by reading its content and processing each line."""
        logger.info(f"Indexing file for search: {file_path}")
        try:
            with open(file_path, "r") as file:
                lines = file.readlines()
                for line in lines:
                    worker = IndexingWorker(
                        line.strip(),
                        update_utils.return_path("command_search_index"),
                        self,
                    )
                    worker.signals.finished.connect(self.show_indexing_complete_message)
                    self.thread_pool.start(worker)
        except Exception as e:
            logger.error(f"Failed to index file: {e}")

    def show_indexing_complete_message(self):
        """Show a message indicating that indexing is complete."""
        logger.info("Indexing complete")
        QMessageBox.information(
            self,
            "Indexing Complete",
            "The file has been successfully indexed for search.",
        )
