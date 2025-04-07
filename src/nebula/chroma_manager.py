#!/usr/bin/env python3
import json
import os

from langchain.schema import Document
from langchain_chroma import Chroma
from langchain_community.document_loaders import (
    CSVLoader,
    DirectoryLoader,
    JSONLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredFileLoader,
    UnstructuredURLLoader,
)
from langchain_huggingface import HuggingFaceEmbeddings

# PyQt imports for QRunnable and signals.
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot


class ChromaManager:
    """
    A class to manage a Chroma vector store using HuggingFaceEmbeddings.
    Provides a unified interface for loading documents from multiple sources.
    """

    def __init__(
        self, collection_name="example_collection", persist_directory="./chroma_db"
    ):
        self.collection_name = collection_name
        self.persist_directory = persist_directory

        # Initialize embeddings.
        self.embedding_model = HuggingFaceEmbeddings(model_name="all-MiniLM-L12-v2")

        # Create or load the Chroma vector store.
        self.vector_store = self._create_vector_store()

        # Mapping for supported source types.
        self.loader_mapping = {
            "pdf": PyPDFLoader,
            "text": TextLoader,
            "url": UnstructuredURLLoader,
            "csv": CSVLoader,
            "directory": DirectoryLoader,
            "json": JSONLoader,
        }

    def _create_vector_store(self):
        vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embedding_model,
            persist_directory=self.persist_directory,
        )
        print(
            f"Vector store '{self.collection_name}' created/loaded from {self.persist_directory}."
        )
        return vector_store

    def _load_jsonl(self, source, **kwargs):
        docs = []
        with open(source, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        page_content = data.get("text", str(data))
                        docs.append(Document(page_content=page_content, metadata=data))
                    except json.JSONDecodeError as e:
                        print(f"Error decoding line: {line}\nError: {e}")
        return docs

    def load_documents(self, source, source_type=None, **kwargs):
        if source_type == "json" and source.lower().endswith(".jsonl"):
            source_type = "jsonl"

        if source_type:
            if source_type == "jsonl":
                docs = self._load_jsonl(source, **kwargs)
                print(f"Loaded {len(docs)} document(s) from {source} (jsonl).")
                return docs
            elif source_type in self.loader_mapping:
                LoaderClass = self.loader_mapping[source_type]
                if source_type == "json":
                    jq_schema = kwargs.get("jq_schema", ".")
                    loader = LoaderClass(source, jq_schema=jq_schema, **kwargs)
                elif source_type == "url":
                    loader = LoaderClass(urls=[source], **kwargs)
                else:
                    loader = LoaderClass(source, **kwargs)
            else:
                raise ValueError(f"Unsupported source_type: {source_type}")
        else:
            if source.startswith("http://") or source.startswith("https://"):
                loader = UnstructuredURLLoader(urls=[source], **kwargs)
            elif os.path.isdir(source):
                loader = DirectoryLoader(
                    source, glob="**/*.*", loader_cls=UnstructuredFileLoader, **kwargs
                )
            else:
                ext = os.path.splitext(source)[1].lower()
                if ext == ".pdf":
                    loader = PyPDFLoader(source, **kwargs)
                elif ext in [".txt", ".md"]:
                    loader = TextLoader(source, **kwargs)
                elif ext == ".csv":
                    loader = CSVLoader(source, **kwargs)
                elif ext == ".jsonl":
                    docs = self._load_jsonl(source, **kwargs)
                    print(f"Loaded {len(docs)} document(s) from {source} (jsonl).")
                    return docs
                elif ext == ".json":
                    jq_schema = kwargs.get("jq_schema", ".")
                    loader = JSONLoader(source, jq_schema=jq_schema, **kwargs)
                else:
                    loader = UnstructuredFileLoader(source, **kwargs)

        docs = loader.load()
        print(f"Loaded {len(docs)} document(s) from {source}.")
        return docs

    def add_documents(self, docs, batch_size=100):
        """
        Adds new documents to the vector store in batches.
        Catches EOFError (which may indicate an empty or corrupted persisted file)
        and reinitializes the vector store if needed.
        """
        total_docs = len(docs)
        for i in range(0, total_docs, batch_size):
            batch = docs[i : i + batch_size]
            try:
                self.vector_store.add_documents(batch)
            except EOFError as e:
                print(
                    f"EOFError while adding batch {i // batch_size + 1}: {e}. "
                    "Reinitializing vector store and retrying batch."
                )
                self.vector_store = self._create_vector_store()
                self.vector_store.add_documents(batch)
            print(
                f"Added batch {i // batch_size + 1} with {len(batch)} document(s) to the vector store."
            )

    def query(self, query_text, k=2):
        results = self.vector_store.similarity_search(query_text, k=k)
        return results


# Define signals for the add documents worker.
class AddDocumentsWorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(Exception)
    progress = pyqtSignal(int)  # Emits progress percentage


# QRunnable to run add_documents in a background thread with progress updates.
class AddDocumentsWorker(QRunnable):
    def __init__(self, manager, docs, batch_size=100):
        super().__init__()
        self.manager = manager
        self.docs = docs
        self.batch_size = batch_size
        self.signals = AddDocumentsWorkerSignals()

    @pyqtSlot()
    def run(self):
        total_docs = len(self.docs)
        for i in range(0, total_docs, self.batch_size):
            batch = self.docs[i : i + self.batch_size]
            try:
                self.manager.vector_store.add_documents(batch)
            except EOFError as e:
                self.signals.error.emit(e)
                return
            progress_value = int((i + len(batch)) / total_docs * 100)
            self.signals.progress.emit(progress_value)
        self.signals.finished.emit()
