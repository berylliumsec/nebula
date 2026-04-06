from types import SimpleNamespace

from nebula import status_update_feed_manager


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


class FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.schema = None
        self.method = None

    def with_structured_output(self, schema, method=None):
        self.schema = schema
        self.method = method
        return self

    def invoke(self, query):
        if self.error is not None:
            raise self.error
        self.query = query
        return self.response


def test_status_feed_worker_emits_finished_results(monkeypatch):
    response = status_update_feed_manager.StatusFeed(
        status_feed=[
            status_update_feed_manager.Status(status="first"),
            status_update_feed_manager.Status(status="second"),
        ]
    )
    fake_llm = FakeLLM(response=response)
    monkeypatch.setattr(
        status_update_feed_manager.utilities,
        "get_llm_instance",
        lambda model, ollama_url="": (fake_llm, "openai"),
    )

    worker = status_update_feed_manager.statusFeedWorker(
        "query",
        SimpleNamespace(load_config=lambda: {"MODEL": "m", "OLLAMA_URL": "http://o"}),
    )
    finished = []
    errors = []
    worker.signals.finished.connect(finished.append)
    worker.signals.error.connect(errors.append)

    worker.run()

    assert finished == [["first", "second"]]
    assert errors == []
    assert fake_llm.schema is status_update_feed_manager.StatusFeed
    assert fake_llm.method == "json_schema"


def test_status_feed_worker_emits_error_when_llm_init_fails(monkeypatch):
    monkeypatch.setattr(
        status_update_feed_manager.utilities,
        "get_llm_instance",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm failed")),
    )

    worker = status_update_feed_manager.statusFeedWorker(
        "query",
        SimpleNamespace(load_config=lambda: {"MODEL": "m", "OLLAMA_URL": ""}),
    )
    errors = []
    worker.signals.error.connect(errors.append)

    worker.run()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_status_feed_worker_emits_error_when_invoke_fails(monkeypatch):
    monkeypatch.setattr(
        status_update_feed_manager.utilities,
        "get_llm_instance",
        lambda *args, **kwargs: (FakeLLM(error=ValueError("bad output")), "openai"),
    )

    worker = status_update_feed_manager.statusFeedWorker(
        "query",
        SimpleNamespace(load_config=lambda: {"MODEL": "m", "OLLAMA_URL": ""}),
    )
    errors = []
    worker.signals.error.connect(errors.append)

    worker.run()

    assert errors == [["bad output"]]


def test_status_feed_worker_emits_error_for_non_list_status_feed(monkeypatch):
    monkeypatch.setattr(
        status_update_feed_manager.utilities,
        "get_llm_instance",
        lambda *args, **kwargs: (
            FakeLLM(response=SimpleNamespace(status_feed=("first", "second"))),
            "openai",
        ),
    )

    worker = status_update_feed_manager.statusFeedWorker(
        "query",
        SimpleNamespace(load_config=lambda: {"MODEL": "m", "OLLAMA_URL": ""}),
    )
    finished = []
    errors = []
    worker.signals.finished.connect(finished.append)
    worker.signals.error.connect(errors.append)

    worker.run()

    assert finished == []
    assert errors == [["Error querying LLM"]]


def test_status_feed_manager_builds_query_and_starts_worker(monkeypatch):
    pool = FakePool()
    created_paths = []
    created_workers = []

    monkeypatch.setattr(
        status_update_feed_manager,
        "ConversationMemory",
        lambda file_path: created_paths.append(file_path)
        or SimpleNamespace(file_path=file_path, history=[]),
    )
    monkeypatch.setattr(
        status_update_feed_manager.QThreadPool,
        "globalInstance",
        lambda: pool,
    )

    class FakeWorker:
        def __init__(self, query, manager):
            self.query = query
            self.manager = manager
            self.signals = SimpleNamespace(
                finished=FakeSignal(),
                error=FakeSignal(),
            )
            created_workers.append(self)

    monkeypatch.setattr(status_update_feed_manager, "statusFeedWorker", FakeWorker)

    cfg = {"MEMORY_DIRECTORY": "/tmp/memory"}
    manager = status_update_feed_manager.statusFeedManager(
        SimpleNamespace(load_config=lambda: cfg),
        lambda result: result,
    )
    manager.suggestions_memory.history = [{"role": "assistant", "content": "s1"}]
    manager.notes_memory.history = [{"role": "user", "content": "n1"}]
    manager.commands_memory.history = [{"role": "assistant", "content": "c1"}]

    manager.update_status_feed()

    assert created_paths == [
        "/tmp/memory/conversation_memory.json",
        "/tmp/memory/suggestions_memory.json",
        "/tmp/memory/notes_memory.json",
        "/tmp/memory/commands_memory.json",
    ]
    assert pool.started == created_workers
    assert "Suggestions:\nassistant: s1" in created_workers[0].query
    assert "Notes:\nuser: n1" in created_workers[0].query
    assert "Commands/AI responses:\nassistant: c1" in created_workers[0].query


def test_status_feed_manager_handles_worker_start_error(monkeypatch):
    monkeypatch.setattr(
        status_update_feed_manager,
        "ConversationMemory",
        lambda file_path: SimpleNamespace(file_path=file_path, history=[]),
    )
    monkeypatch.setattr(
        status_update_feed_manager.QThreadPool,
        "globalInstance",
        lambda: FakePool(),
    )
    monkeypatch.setattr(
        status_update_feed_manager,
        "statusFeedWorker",
        lambda query, manager: (_ for _ in ()).throw(RuntimeError("worker failed")),
    )

    manager = status_update_feed_manager.statusFeedManager(
        SimpleNamespace(load_config=lambda: {"MEMORY_DIRECTORY": "/tmp/memory"}),
        lambda result: result,
    )

    manager.update_status_feed()


def test_status_feed_manager_callbacks(monkeypatch):
    shown_messages = []
    updated = []
    monkeypatch.setattr(
        status_update_feed_manager.utilities,
        "show_message",
        lambda title, message: shown_messages.append((title, message)),
    )
    monkeypatch.setattr(
        status_update_feed_manager,
        "ConversationMemory",
        lambda file_path: SimpleNamespace(file_path=file_path, history=[]),
    )
    monkeypatch.setattr(
        status_update_feed_manager.QThreadPool,
        "globalInstance",
        lambda: FakePool(),
    )

    manager = status_update_feed_manager.statusFeedManager(
        SimpleNamespace(load_config=lambda: {"MEMORY_DIRECTORY": "/tmp/memory"}),
        updated.append,
    )

    manager.on_status_feed_update_error(["broken"])
    manager.on_status_feed_update(["ready"])
    manager.on_status_feed_update(None)

    assert shown_messages == [("Error Loading Model", "['broken']")]
    assert updated == [["ready"]]
