from types import SimpleNamespace

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QLabel

from nebula import initial_logic


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


def make_stylesheet_assets(tmp_path):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; }")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"")
    return stylesheet, logo


def test_dialog_worker_and_progress_window(tmp_path, monkeypatch):
    stylesheet, _ = make_stylesheet_assets(tmp_path)
    monkeypatch.setattr(initial_logic, "return_path", lambda path: str(stylesheet))
    app = initial_logic.QApplication.instance() or initial_logic.MainApplication([])

    dialog = initial_logic.ErrorDialog()
    assert dialog.windowTitle() == "Error"
    assert dialog.findChild(QLabel).text().startswith("Something went wrong")
    dialog.close()

    class SignalTarget:
        def __init__(self):
            self.values = []

        def emit(self, value):
            self.values.append(value)

    target = SignalTarget()
    worker = initial_logic.Worker(SimpleNamespace(main_window_loaded=target), "/tmp/eng")
    monkeypatch.setattr(initial_logic.QThread, "sleep", lambda seconds: None)
    worker.run()
    assert target.values == [{"engagement_folder": "/tmp/eng"}]

    monkeypatch.setattr(
        initial_logic.QThread,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(RuntimeError("sleep failed")),
    )
    worker.run()
    assert target.values[-1] is None

    monkeypatch.setattr(
        initial_logic.QApplication,
        "primaryScreen",
        lambda: SimpleNamespace(
            availableGeometry=lambda: SimpleNamespace(center=lambda: QPoint(20, 20))
        ),
    )
    window = initial_logic.ProgressWindow()
    assert window.windowTitle() == "Loading.. Please wait"
    assert window.progressBar.minimum() == 0
    assert window.progressBar.maximum() == 0
    window.close()


def test_main_application_flow(tmp_path, monkeypatch):
    stylesheet, logo = make_stylesheet_assets(tmp_path)
    monkeypatch.setattr(
        initial_logic,
        "return_path",
        lambda path: str(logo if path.endswith("logo.png") else stylesheet),
    )

    class FakeConfig:
        def __init__(self):
            self.folder = None

        def setengagement_folder(self, folder):
            self.folder = folder

    class FakeSetupWindow:
        def __init__(self, engagement_folder=None):
            self.engagement_folder = engagement_folder
            self.setupCompleted = FakeSignal()
            self.shown = False
            self.closed = False

        def show(self):
            self.shown = True

        def close(self):
            self.closed = True

    saved_settings = []
    app = SimpleNamespace(
        config=None,
        setupWindow=None,
        progressWindow=None,
        mainWindow=None,
        engagement_folder=None,
        thread_pool=None,
        worker_signals=FakeSignal(),
        update_engagement_folder=lambda text: None,
        user_settings=SimpleNamespace(
            value=lambda *args, **kwargs: str(tmp_path),
            setValue=lambda key, value: saved_settings.append((key, value)),
        ),
    )

    created = []
    monkeypatch.setattr(initial_logic.configuration_manager, "ConfigManager", FakeConfig)
    monkeypatch.setattr(
        initial_logic,
        "settings",
        lambda engagement_folder=None: created.append(FakeSetupWindow(engagement_folder))
        or created[-1],
    )
    monkeypatch.setattr(initial_logic.os.path, "isdir", lambda path: True)

    initial_logic.MainApplication.show_setup(app)
    assert isinstance(app.config, FakeConfig)
    assert created[0].engagement_folder == str(tmp_path)
    assert created[0].shown is True

    init_calls = []
    app.init_main_window = lambda: init_calls.append(True)
    initial_logic.MainApplication.update_engagement_folder(app, "/tmp/engagement")
    assert app.engagement_folder == "/tmp/engagement"
    assert app.config.folder == "/tmp/engagement"
    assert init_calls == [True]

    started_workers = []
    progress_windows = []
    monkeypatch.setattr(
        initial_logic,
        "ProgressWindow",
        lambda: progress_windows.append(
            SimpleNamespace(close=lambda: None, deleteLater=lambda: None)
        )
        or progress_windows[-1],
    )
    monkeypatch.setattr(
        initial_logic,
        "Worker",
        lambda signals, engagement_folder: SimpleNamespace(
            signals=signals, engagement_folder=engagement_folder
        ),
    )
    app.thread_pool = SimpleNamespace(start=lambda worker: started_workers.append(worker))
    app.setupWindow = created[0]
    app.engagement_folder = "/tmp/engagement"
    initial_logic.MainApplication.init_main_window(app)
    assert created[0].closed is True
    assert started_workers[0].engagement_folder == "/tmp/engagement"

    splash_calls = []
    monkeypatch.setattr(
        initial_logic.QTimer,
        "singleShot",
        lambda delay, callback: splash_calls.append((delay, callback)),
    )

    class FakeMainWindow:
        def __init__(self, folder):
            self.folder = folder
            self.shown = False
            self.search_area = SimpleNamespace(setText=lambda text: setattr(self, "search_text", text))
            self.command_input_area = SimpleNamespace(setText=lambda text: setattr(self, "command_text", text))
            self.tour_started = False

        def show(self):
            self.shown = True

        def start_tour(self):
            self.tour_started = True

    monkeypatch.setattr(initial_logic, "Nebula", FakeMainWindow)
    app.progressWindow = SimpleNamespace(
        close=lambda: splash_calls.append("closed"),
        deleteLater=lambda: splash_calls.append("deleted"),
    )
    app.splash_finished = lambda: None
    initial_logic.MainApplication.on_main_window_loaded(
        app, {"engagement_folder": "/tmp/engagement"}
    )
    assert isinstance(app.mainWindow, FakeMainWindow)
    assert app.mainWindow.shown is True
    assert (0, app.splash_finished) in splash_calls

    exits = []
    dialogs = []
    monkeypatch.setattr(initial_logic, "ErrorDialog", lambda: dialogs.append(SimpleNamespace(exec=lambda: dialogs.append("exec"))) or dialogs[-1])
    monkeypatch.setattr(initial_logic.sys, "exit", exits.append)
    initial_logic.MainApplication.on_main_window_loaded(app, None)
    assert dialogs[-1] == "exec"
    assert exits == [0]

    monkeypatch.setattr(initial_logic.utilities, "check_initial_help", lambda: False)
    app.mainWindow = FakeMainWindow("/tmp/engagement")
    initial_logic.MainApplication.start_app_tour(app)
    assert app.mainWindow.tour_started is True
    assert "Search for commands here" in app.mainWindow.search_text
    assert app.mainWindow.command_text.startswith("! Say hello")

    monkeypatch.setattr(initial_logic.utilities, "check_initial_help", lambda: True)
    initial_logic.MainApplication.start_app_tour(app)

    called = []
    app.start_app_tour = lambda: called.append(True)
    initial_logic.MainApplication.splash_finished(app)
    assert called == [True]


def test_main_application_start_and_exception_branches(tmp_path, monkeypatch):
    stylesheet, logo = make_stylesheet_assets(tmp_path)
    monkeypatch.setattr(
        initial_logic,
        "return_path",
        lambda path: str(logo if path.endswith("logo.png") else stylesheet),
    )
    app = SimpleNamespace(
        user_settings=SimpleNamespace(value=lambda *args, **kwargs: "/missing"),
        config=None,
        setupWindow=None,
        progressWindow=None,
        mainWindow=None,
        engagement_folder=None,
        worker_signals=FakeSignal(),
        thread_pool=SimpleNamespace(start=lambda worker: None),
        update_engagement_folder=lambda text: None,
    )
    shown = []
    names = []
    icon_paths = []
    app.show_setup = lambda: shown.append(True)
    app.setApplicationName = lambda name: names.append(name)
    app.setOrganizationName = lambda name: names.append(name)
    app.setWindowIcon = lambda icon: icon_paths.append(icon)
    initial_logic.MainApplication.start(app)
    assert names == ["Nebula", "Beryllium"]
    assert shown == [True]

    class FakeConfig:
        def setengagement_folder(self, folder):
            self.folder = folder

    class FakeSetupWindow:
        def __init__(self, engagement_folder=None):
            self.engagement_folder = engagement_folder
            self.setupCompleted = FakeSignal()
            self.shown = False

        def show(self):
            self.shown = True

    created = []
    monkeypatch.setattr(initial_logic.configuration_manager, "ConfigManager", FakeConfig)
    monkeypatch.setattr(
        initial_logic,
        "settings",
        lambda engagement_folder=None: created.append(
            FakeSetupWindow(engagement_folder)
        )
        or created[-1],
    )
    monkeypatch.setattr(app.user_settings, "value", lambda *args, **kwargs: "/missing")
    monkeypatch.setattr(initial_logic.os.path, "isdir", lambda path: False)
    initial_logic.MainApplication.show_setup(app)
    assert created[-1].engagement_folder is None

    exceptions = []
    monkeypatch.setattr(
        initial_logic.logger,
        "exception",
        lambda *args: exceptions.append(" ".join(map(str, args))),
    )
    app.setupWindow = SimpleNamespace(
        close=lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
    )
    initial_logic.MainApplication.init_main_window(app)
    assert any("close failed" in message for message in exceptions)

    monkeypatch.setattr(
        initial_logic,
        "Nebula",
        lambda folder: (_ for _ in ()).throw(RuntimeError("window failed")),
    )
    app.progressWindow = None
    app.splash_finished = lambda: None
    initial_logic.MainApplication.on_main_window_loaded(
        app, {"engagement_folder": "/tmp/engagement"}
    )
    assert any("window failed" in message for message in exceptions)
