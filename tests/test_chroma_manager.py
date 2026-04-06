from types import SimpleNamespace

import pytest
from langchain.schema import Document

from nebula import chroma_manager


class LoaderFactory:
    def __init__(self):
        self.instances = []

    def __call__(self, *args, **kwargs):
        instance = SimpleNamespace(args=args, kwargs=kwargs)
        instance.load = lambda: [("loaded", args, kwargs)]
        self.instances.append(instance)
        return instance


def build_manager():
    manager = object.__new__(chroma_manager.ChromaManager)
    manager.loader_mapping = {
        "pdf": LoaderFactory(),
        "text": LoaderFactory(),
        "url": LoaderFactory(),
        "csv": LoaderFactory(),
        "directory": LoaderFactory(),
        "json": LoaderFactory(),
    }
    manager.vector_store = SimpleNamespace()
    return manager


def test_chroma_manager_init_builds_vector_store(monkeypatch):
    embeddings = []
    chroma_calls = []

    class FakeEmbeddings:
        def __init__(self, model_name):
            embeddings.append(model_name)

    class FakeChroma:
        def __init__(
            self,
            collection_name,
            embedding_function,
            persist_directory,
        ):
            chroma_calls.append(
                (collection_name, embedding_function, persist_directory)
            )

    monkeypatch.setattr(chroma_manager, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(chroma_manager, "Chroma", FakeChroma)

    manager = chroma_manager.ChromaManager(
        collection_name="test_collection",
        persist_directory="/tmp/chroma",
    )

    assert embeddings == ["all-MiniLM-L12-v2"]
    assert chroma_calls[0][0] == "test_collection"
    assert chroma_calls[0][2] == "/tmp/chroma"
    assert set(manager.loader_mapping) == {
        "pdf",
        "text",
        "url",
        "csv",
        "directory",
        "json",
    }


def test_load_jsonl_skips_invalid_lines(tmp_path):
    jsonl_file = tmp_path / "docs.jsonl"
    jsonl_file.write_text(
        '{"text": "alpha", "id": 1}\n'
        'not-json\n'
        '{"title": "beta"}\n'
    )

    docs = chroma_manager.ChromaManager._load_jsonl(build_manager(), str(jsonl_file))

    assert docs[0].page_content == "alpha"
    assert docs[0].metadata["id"] == 1
    assert docs[1].page_content == "{'title': 'beta'}"


def test_load_documents_handles_explicit_jsonl(monkeypatch):
    manager = build_manager()
    monkeypatch.setattr(manager, "_load_jsonl", lambda source, **kwargs: ["jsonl"])

    assert manager.load_documents("items.jsonl", source_type="json") == ["jsonl"]


@pytest.mark.parametrize(
    ("source_type", "source", "expected_args", "expected_kwargs"),
    [
        ("json", "/tmp/data.json", ("/tmp/data.json",), {"jq_schema": "."}),
        ("url", "https://example.com", (), {"urls": ["https://example.com"]}),
        ("pdf", "/tmp/file.pdf", ("/tmp/file.pdf",), {}),
        ("csv", "/tmp/file.csv", ("/tmp/file.csv",), {}),
    ],
)
def test_load_documents_handles_explicit_source_types(
    source_type,
    source,
    expected_args,
    expected_kwargs,
):
    manager = build_manager()
    factory = manager.loader_mapping[source_type]

    docs = manager.load_documents(source, source_type=source_type)

    assert docs == [("loaded", expected_args, expected_kwargs)]
    assert factory.instances[0].args == expected_args
    assert factory.instances[0].kwargs == expected_kwargs


def test_load_documents_rejects_unsupported_source_type():
    manager = build_manager()

    with pytest.raises(ValueError, match="Unsupported source_type"):
        manager.load_documents("file.bin", source_type="binary")


def test_load_documents_auto_detects_sources(monkeypatch, tmp_path):
    manager = build_manager()
    url_loader = LoaderFactory()
    dir_loader = LoaderFactory()
    pdf_loader = LoaderFactory()
    text_loader = LoaderFactory()
    csv_loader = LoaderFactory()
    json_loader = LoaderFactory()
    generic_loader = LoaderFactory()

    monkeypatch.setattr(chroma_manager, "UnstructuredURLLoader", url_loader)
    monkeypatch.setattr(chroma_manager, "DirectoryLoader", dir_loader)
    monkeypatch.setattr(chroma_manager, "PyPDFLoader", pdf_loader)
    monkeypatch.setattr(chroma_manager, "TextLoader", text_loader)
    monkeypatch.setattr(chroma_manager, "CSVLoader", csv_loader)
    monkeypatch.setattr(chroma_manager, "JSONLoader", json_loader)
    monkeypatch.setattr(chroma_manager, "UnstructuredFileLoader", generic_loader)
    monkeypatch.setattr(
        manager,
        "_load_jsonl",
        lambda source, **kwargs: [("jsonl", source, kwargs)],
    )

    directory = tmp_path / "docs"
    directory.mkdir()

    assert manager.load_documents("https://example.com") == [
        ("loaded", (), {"urls": ["https://example.com"]})
    ]
    assert manager.load_documents(str(directory)) == [
        (
            "loaded",
            (str(directory),),
            {"glob": "**/*.*", "loader_cls": chroma_manager.UnstructuredFileLoader},
        )
    ]
    assert manager.load_documents("/tmp/report.pdf") == [
        ("loaded", ("/tmp/report.pdf",), {})
    ]
    assert manager.load_documents("/tmp/notes.txt") == [
        ("loaded", ("/tmp/notes.txt",), {})
    ]
    assert manager.load_documents("/tmp/table.csv") == [
        ("loaded", ("/tmp/table.csv",), {})
    ]
    assert manager.load_documents("/tmp/items.jsonl") == [
        ("jsonl", "/tmp/items.jsonl", {})
    ]
    assert manager.load_documents("/tmp/items.json") == [
        ("loaded", ("/tmp/items.json",), {"jq_schema": "."})
    ]
    assert manager.load_documents("/tmp/file.bin") == [
        ("loaded", ("/tmp/file.bin",), {})
    ]


def test_add_documents_retries_after_eof_error(monkeypatch):
    calls = []
    retry_calls = []

    class FirstVectorStore:
        def add_documents(self, batch):
            calls.append(batch)
            raise EOFError("corrupt store")

    class RetryVectorStore:
        def add_documents(self, batch):
            retry_calls.append(batch)

    manager = build_manager()
    manager.vector_store = FirstVectorStore()
    monkeypatch.setattr(manager, "_create_vector_store", lambda: RetryVectorStore())

    manager.add_documents(["a", "b"], batch_size=1)

    assert calls == [["a"]]
    assert retry_calls == [["a"], ["b"]]


def test_query_uses_retriever():
    expected = [Document(page_content="answer")]

    class Retriever:
        def invoke(self, query_text):
            assert query_text == "question"
            return expected

    class VectorStore:
        def as_retriever(self, search_type, search_kwargs):
            assert search_type == "mmr"
            assert search_kwargs == {"k": 1, "fetch_k": 5}
            return Retriever()

    manager = build_manager()
    manager.vector_store = VectorStore()

    assert manager.query("question") == expected


class SignalRecorder:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


def test_add_documents_worker_emits_progress_and_finished():
    added_batches = []
    worker = chroma_manager.AddDocumentsWorker(
        SimpleNamespace(
            vector_store=SimpleNamespace(
                add_documents=lambda batch: added_batches.append(batch)
            )
        ),
        ["a", "b", "c"],
        batch_size=2,
    )
    worker.signals.progress = SignalRecorder()
    worker.signals.finished = SignalRecorder()
    worker.signals.error = SignalRecorder()

    progress = []
    finished = []
    errors = []
    worker.signals.progress.connect(progress.append)
    worker.signals.finished.connect(lambda: finished.append(True))
    worker.signals.error.connect(errors.append)

    worker.run()

    assert added_batches == [["a", "b"], ["c"]]
    assert progress == [66, 100]
    assert finished == [True]
    assert errors == []


def test_add_documents_worker_emits_error_on_eof():
    worker = chroma_manager.AddDocumentsWorker(
        SimpleNamespace(
            vector_store=SimpleNamespace(
                add_documents=lambda batch: (_ for _ in ()).throw(EOFError("bad"))
            )
        ),
        ["a"],
    )
    worker.signals.progress = SignalRecorder()
    worker.signals.finished = SignalRecorder()
    worker.signals.error = SignalRecorder()

    errors = []
    finished = []
    worker.signals.error.connect(errors.append)
    worker.signals.finished.connect(lambda: finished.append(True))

    worker.run()

    assert len(errors) == 1
    assert isinstance(errors[0], EOFError)
    assert finished == []
