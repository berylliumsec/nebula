from types import SimpleNamespace

import pytest

from nebula import document_loader


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeWorker:
    def __init__(self, manager, docs, batch_size=100):
        self.manager = manager
        self.docs = docs
        self.batch_size = batch_size
        self.cancelled = False
        self.signals = SimpleNamespace(
            progress=FakeSignal(),
            finished=FakeSignal(),
            error=FakeSignal(),
        )

    def cancel(self):
        self.cancelled = True


class FakeProgressDialog:
    instances = []

    def __init__(self, label_text, cancel_text, minimum, maximum, parent):
        self.label_text = label_text
        self.cancel_text = cancel_text
        self.minimum = minimum
        self.maximum = maximum
        self.parent = parent
        self.value = None
        self.closed = False
        self.shown = False
        self.window_modality = None
        self.canceled = FakeSignal()
        type(self).instances.append(self)

    def setWindowModality(self, modality):
        self.window_modality = modality

    def setValue(self, value):
        self.value = value

    def show(self):
        self.shown = True

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self):
        self.started = []

    def start(self, worker):
        self.started.append(worker)


def build_dialog(qapp):
    return document_loader.DocumentLoaderDialog(
        SimpleNamespace(load_documents=lambda *args, **kwargs: ["doc"])
    )


def test_document_loader_on_type_change_updates_controls(qapp):
    dialog = build_dialog(qapp)

    try:
        dialog.on_type_change("url")
        assert dialog.browse_button.isEnabled() is False
        assert dialog.input_field.placeholderText() == "Enter URL here"

        dialog.on_type_change("directory")
        assert dialog.browse_button.isEnabled() is True
        assert dialog.input_field.placeholderText() == "Browse for a folder"

        dialog.on_type_change("pdf")
        assert dialog.input_field.placeholderText() == "Enter file path or browse..."
    finally:
        dialog.close()


def test_document_loader_browse_directory_updates_field(qapp, monkeypatch):
    dialog = build_dialog(qapp)
    dialog.type_combo.setCurrentText("directory")
    monkeypatch.setattr(
        document_loader.QFileDialog,
        "getExistingDirectory",
        lambda *args: "/tmp/folder",
    )

    try:
        dialog.browse()
        assert dialog.input_field.text() == "/tmp/folder"
    finally:
        dialog.close()


@pytest.mark.parametrize(
    ("input_type", "expected_filter"),
    [
        ("pdf", "PDF Files (*.pdf)"),
        ("text", "Text Files (*.txt *.json *.jsonl);;All Files (*)"),
        ("csv", "CSV Files (*.csv)"),
    ],
)
def test_document_loader_browse_file_updates_field(
    qapp,
    monkeypatch,
    input_type,
    expected_filter,
):
    dialog = build_dialog(qapp)
    dialog.type_combo.setCurrentText(input_type)
    seen_filters = []
    monkeypatch.setattr(
        document_loader.QFileDialog,
        "getOpenFileName",
        lambda *args: (
            seen_filters.append(args[3]) or "/tmp/file",
            expected_filter,
        ),
    )

    try:
        dialog.browse()
        assert dialog.input_field.text() == "/tmp/file"
        assert seen_filters == [expected_filter]
    finally:
        dialog.close()


def test_document_loader_warns_for_missing_source(qapp, monkeypatch):
    warnings = []
    dialog = build_dialog(qapp)
    monkeypatch.setattr(
        document_loader.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((title, message)),
    )

    try:
        dialog.load_document()
        assert warnings == [
            ("Input Error", "Please enter a URL or file/folder path.")
        ]
    finally:
        dialog.close()


def test_document_loader_starts_worker_and_handles_success(qapp, monkeypatch):
    infos = []
    pool = FakePool()
    vector_db = SimpleNamespace(load_documents=lambda source, source_type=None: ["a", "b"])
    dialog = document_loader.DocumentLoaderDialog(vector_db)

    monkeypatch.setattr(document_loader, "AddDocumentsWorker", FakeWorker)
    monkeypatch.setattr(document_loader, "QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(
        document_loader.QThreadPool,
        "globalInstance",
        lambda: pool,
    )
    monkeypatch.setattr(
        document_loader.QMessageBox,
        "information",
        lambda parent, title, message: infos.append((title, message)),
    )

    try:
        dialog.type_combo.setCurrentText("text")
        dialog.input_field.setText("/tmp/file.txt")
        dialog.load_document()

        worker = pool.started[0]
        progress_dialog = FakeProgressDialog.instances[-1]

        assert worker.docs == ["a", "b"]
        assert worker.batch_size == 100
        assert progress_dialog.shown is True

        worker.signals.progress.emit(50)
        assert progress_dialog.value == 50

        progress_dialog.canceled.emit()
        assert worker.cancelled is True

        worker.signals.finished.emit()
        assert infos == [
            (
                "Documents Loaded",
                "Loaded 2 document(s) from /tmp/file.txt as text.",
            )
        ]
        assert progress_dialog.closed is True
    finally:
        dialog.close()


def test_document_loader_handles_worker_error(qapp, monkeypatch):
    errors = []
    pool = FakePool()
    dialog = document_loader.DocumentLoaderDialog(
        SimpleNamespace(load_documents=lambda source, source_type=None: ["a"])
    )

    monkeypatch.setattr(document_loader, "AddDocumentsWorker", FakeWorker)
    monkeypatch.setattr(document_loader, "QProgressDialog", FakeProgressDialog)
    monkeypatch.setattr(
        document_loader.QThreadPool,
        "globalInstance",
        lambda: pool,
    )
    monkeypatch.setattr(
        document_loader.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    try:
        dialog.type_combo.setCurrentText("text")
        dialog.input_field.setText("/tmp/file.txt")
        dialog.load_document()
        worker = pool.started[0]
        worker.signals.error.emit("boom")

        assert errors == [("Error", "Error adding documents: boom")]
        assert FakeProgressDialog.instances[-1].closed is True
    finally:
        dialog.close()


def test_document_loader_handles_load_exception(qapp, monkeypatch):
    errors = []
    dialog = document_loader.DocumentLoaderDialog(
        SimpleNamespace(
            load_documents=lambda source, source_type=None: (_ for _ in ()).throw(
                RuntimeError("load failed")
            )
        )
    )
    monkeypatch.setattr(
        document_loader.QMessageBox,
        "critical",
        lambda parent, title, message: errors.append((title, message)),
    )

    try:
        dialog.input_field.setText("/tmp/file.txt")
        dialog.load_document()
        assert errors == [("Error", "Error loading documents: load failed")]
    finally:
        dialog.close()
