from types import SimpleNamespace

from nebula import search


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakePool:
    def __init__(self):
        self.started = []

    def start(self, worker):
        self.started.append(worker)


class FakeSearchWorker:
    def __init__(self, query, llm, max_results=2, rag=None):
        self.query = query
        self.llm = llm
        self.max_results = max_results
        self.rag = rag
        self.signals = SimpleNamespace(finished=FakeSignal())


class FakeMenu:
    def __init__(self):
        self.separator_added = False
        self.executed = None

    def addSeparator(self):
        self.separator_added = True

    def exec(self, position):
        self.executed = position


def make_manager():
    return SimpleNamespace(
        load_config=lambda: {
            "MODEL": "demo-model",
            "OLLAMA_URL": "http://ollama",
            "CHROMA_DB_PATH": "/tmp/chroma",
        }
    )


def test_search_worker_emits_content_attribute():
    worker = search.SearchWorker(
        "ports",
        llm=SimpleNamespace(
            invoke=lambda prompt: SimpleNamespace(content=f"answer:{prompt}")
        ),
        rag=SimpleNamespace(
            query=lambda query, k=1: [SimpleNamespace(page_content=" doc result ")]
        ),
    )
    results = []
    worker.signals.finished.connect(results.append)

    worker.run()

    assert len(results) == 1
    assert results[0].startswith("answer:Answer this question: 'ports'")


def test_search_worker_emits_raw_response():
    worker = search.SearchWorker(
        "ports",
        llm=SimpleNamespace(invoke=lambda prompt: "plain answer"),
        rag=SimpleNamespace(
            query=lambda query, k=1: [SimpleNamespace(page_content=" doc result ")]
        ),
    )
    results = []
    worker.signals.finished.connect(results.append)

    worker.run()

    assert results == ["plain answer"]


def test_search_worker_emits_none_on_error():
    worker = search.SearchWorker("ports", llm="llm", rag=None)
    results = []
    worker.signals.finished.connect(results.append)

    worker.run()

    assert results == [None]


def test_custom_search_line_edit_initializes_and_uses_ui(qapp, monkeypatch):
    fake_rag = SimpleNamespace(
        vector_store=SimpleNamespace(
            _collection=SimpleNamespace(count=lambda: 3)
        )
    )
    monkeypatch.setattr(
        search.utilities,
        "get_llm_instance",
        lambda model, ollama_url="": ("llm", "openai"),
    )
    monkeypatch.setattr(search, "ChromaManager", lambda **kwargs: fake_rag)

    widget = search.CustomSearchLineEdit(manager=make_manager())

    try:
        assert widget.llm == "llm"
        assert widget.rag is fake_rag

        menu = FakeMenu()
        widget.createStandardContextMenu = lambda: menu
        widget.contextMenuEvent(SimpleNamespace(globalPos=lambda: "cursor-pos"))
        assert menu.separator_added is True
        assert menu.executed == "cursor-pos"

        results = []
        widget.resultSelected.connect(results.append)
        widget.setText("abc")
        widget.onReturnPressed()
        assert results[-1] == "abc"
        assert widget.text() == ""

        widget.llm = None
        widget.rag = fake_rag
        widget.setText("abcd")
        widget.onReturnPressed()
        assert results[-1] == "Search functionality is currently unavailable."

        widget.llm = "llm"
        widget.rag = fake_rag
        widget.threadpool = FakePool()
        monkeypatch.setattr(search, "SearchWorker", FakeSearchWorker)

        widget.setText("long query")
        widget.onReturnPressed()
        assert widget.isEnabled() is False
        assert widget.styleSheet() == "border: 2px solid orange;"
        assert widget.threadpool.started[0].query == "long query"

        completed = []
        widget.resultSelected.connect(completed.append)
        widget.completer.complete = lambda: completed.append("completed")
        widget.onSearchCompleted("answer")
        assert widget.isEnabled() is True
        assert widget.styleSheet() == ""
        assert widget.completer.model().stringList() == ["answer"]
        assert completed[-2:] == ["answer", "completed"]

        widget.onSearchCompleted(None)
        assert completed[-1] == "No results returned from search worker."

        widget.setText("chosen")
        widget.onResultSelected()
        assert completed[-1] == "chosen"
        assert widget.text() == ""
    finally:
        widget.close()


def test_custom_search_line_edit_handles_init_failures(qapp, monkeypatch):
    shown_messages = []
    monkeypatch.setattr(
        search.utilities,
        "get_llm_instance",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm unavailable")),
    )
    monkeypatch.setattr(
        search,
        "ChromaManager",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("rag unavailable")),
    )
    monkeypatch.setattr(
        search.utilities,
        "show_message",
        lambda title, message: shown_messages.append((title, message)),
    )

    widget = search.CustomSearchLineEdit(manager=make_manager())

    try:
        assert widget.llm is None
        assert widget.rag is None
        assert shown_messages == [
            (
                "Error Loading Ollama",
                "Ollama could not be loaded, please check the url in engagement settings and try again",
            )
        ]
    finally:
        widget.close()


def test_custom_search_line_edit_chromadb_info_branches(qapp, monkeypatch):
    monkeypatch.setattr(
        search.utilities,
        "get_llm_instance",
        lambda model, ollama_url="": ("llm", "openai"),
    )
    info_messages = []
    monkeypatch.setattr(search.logger, "info", info_messages.append)
    monkeypatch.setattr(
        search,
        "ChromaManager",
        lambda **kwargs: SimpleNamespace(vector_store=SimpleNamespace()),
    )

    no_collection_widget = search.CustomSearchLineEdit(manager=make_manager())
    assert any("Cannot determine item count" in message for message in info_messages)
    no_collection_widget.close()

    errors = []
    monkeypatch.setattr(search.logger, "error", errors.append)
    monkeypatch.setattr(
        search,
        "ChromaManager",
        lambda **kwargs: SimpleNamespace(
            vector_store=SimpleNamespace(
                _collection=SimpleNamespace(
                    count=lambda: (_ for _ in ()).throw(RuntimeError("count failed"))
                )
            )
        ),
    )

    widget = search.CustomSearchLineEdit(manager=make_manager())

    try:
        assert widget.llm == "llm"
        assert any("count failed" in message for message in errors)
    finally:
        widget.close()
