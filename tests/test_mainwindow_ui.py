import json
import os
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QDialog, QLineEdit, QMainWindow, QTextEdit

from nebula import MainWindow as main_window_module


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````\x00"
    b"\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeCommandTerminal:
    def __init__(self):
        self.busy = Signal()
        self.password_mode = Signal()
        self.writes = []
        self.reset_calls = 0

    def write(self, data):
        self.writes.append(data)

    def reset_terminal(self):
        self.reset_calls += 1


class FakeCommandInputArea(QLineEdit):
    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.manager = manager
        self.terminal = FakeCommandTerminal()
        self.updateCentralDisplayArea = Signal()
        self.updateCentralDisplayAreaForApi = Signal()
        self.update_suggestions_notes = Signal()
        self.update_ai_notes = Signal()
        self.threads_status = Signal()
        self.model_busy_busy_signal = Signal()
        self.api_call_execution_finished = Signal()
        self.api_tasks = 0
        self.executed_api_calls = []
        self.executed_commands = []
        self.input_modes = []

    def set_input_mode(self, mode):
        self.input_modes.append(mode)

    def execute_api_call(self, *args):
        self.executed_api_calls.append(args)

    def execute_command(self, command):
        self.executed_commands.append(command)


class FakeCentralDisplayArea(QTextEdit):
    def __init__(self, parent=None, manager=None, command_input_area=None):
        super().__init__(parent)
        self.manager = manager
        self.command_input_area = command_input_area
        self.notes_signal_from_central_display_area = Signal()
        self.suggestions_signal_from_central_display_area = Signal()
        self.enabled_states = []
        self.font_sizes = []

    def enable_or_disable_due_to_model_creation(self, state):
        self.enabled_states.append(state)

    def set_font_size_for_copy_button(self, size):
        self.font_sizes.append(size)


class FakeSearchLineEdit(QLineEdit):
    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.manager = manager
        self.resultSelected = Signal()


class FakeAiNotes(QTextEdit):
    blank_initial_html = False

    def __init__(
        self,
        file_path=None,
        bookmarks_path=None,
        parent=None,
        manager=None,
        command_input_area=None,
        search_window=None,
    ):
        super().__init__(parent)
        self.file_path = file_path
        self.bookmarks_path = bookmarks_path
        self.manager = manager
        self.command_input_area = command_input_area
        self.search_window = search_window
        self.bookmarks = [{"name": "alpha", "position": 1}]
        self._raw_html = ""
        if not type(self).blank_initial_html:
            self.setHtml("<p>notes</p>")

    def append_text(self, text):
        self.insertHtml(text)

    def setHtml(self, text):
        self._raw_html = text
        super().setHtml(text)

    def toHtml(self):
        if self._raw_html == "":
            return ""
        return super().toHtml()


class FakeSuggestionsWindow(QMainWindow):
    def __init__(self, manager=None, command_input_area=None):
        super().__init__()
        self.manager = manager
        self.command_input_area = command_input_area
        self.updates = []

    def update_suggestions(self, text):
        self.updates.append(text)


class FakeSettings(QDialog):
    def __init__(self, engagement_folder=None):
        super().__init__()
        self.engagement_folder = engagement_folder
        self.setupCompleted = Signal()


class FakeToolsWindow(QMainWindow):
    def __init__(
        self,
        available_tools,
        selected_tools,
        icons_path,
        update_callback,
        add_tool_callback,
        parent=None,
    ):
        super().__init__(parent)
        self.available_tools = available_tools
        self.selected_tools = selected_tools
        self.icons_path = icons_path
        self.update_callback = update_callback
        self.add_tool_callback = add_tool_callback
        self.select_all_calls = 0
        self.updated = []

    def select_all_tools(self):
        self.select_all_calls += 1

    def update_config(self, available_tools, selected_tools):
        self.updated.append((list(available_tools), list(selected_tools)))


class FakeStatusFeedManager:
    def __init__(self, manager, update_ui_callback):
        self.manager = manager
        self.update_ui_callback = update_ui_callback
        self.update_calls = 0

    def update_status_feed(self):
        self.update_calls += 1
        self.update_ui_callback(["status-a", "status-b"])


class FakePopupWindow(QMainWindow):
    def __init__(self, notes_file_path=None, manager=None, command_input_area=None):
        super().__init__()
        self.notes_file_path = notes_file_path
        self.manager = manager
        self.command_input_area = command_input_area
        self.textUpdated = Signal()
        self.last_text = None

    def setTextInTextEdit(self, text):
        self.last_text = text


class FakeDialogWindow(QDialog):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.shown = False
        self.modalities = []

    def setWindowModality(self, modality):
        self.modalities.append(modality)

    def show(self):
        self.shown = True
        super().show()


class FakeTerminalWindow(QMainWindow):
    def __init__(self, parent=None, manager=None, terminal_emulator_number=0):
        super().__init__(parent)
        self.manager = manager
        self.terminal_emulator_number = terminal_emulator_number
        self.command_input_area = FakeCommandInputArea(manager=manager)


class FakeChromaManager:
    def __init__(self, collection_name=None, persist_directory=None):
        self.collection_name = collection_name
        self.persist_directory = persist_directory


class FakeWorker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.signals = SimpleNamespace(finished=Signal())
        self.moved = None
        self.deleted = False
        self.ran = False

    def moveToThread(self, thread):
        self.moved = thread

    def deleteLater(self):
        self.deleted = True

    def run(self):
        self.ran = True
        self.signals.finished.emit()


class FakeThread:
    def __init__(self):
        self.started = Signal()
        self.finished = Signal()
        self.started_count = 0
        self.wait_count = 0

    def start(self):
        self.started_count += 1
        self.started.emit()

    def quit(self):
        self.quitted = True

    def wait(self):
        self.wait_count += 1

    def deleteLater(self):
        pass


class FakeConfigManager:
    def __init__(self, engagement_folder=None):
        self.engagement_folder = engagement_folder
        base = Path(engagement_folder or ".")
        self.log_dir = base / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir = base / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir = base / "shots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = base / "config.json"
        self.config = {
            "LOG_DIRECTORY": str(self.log_dir),
            "SUGGESTIONS_NOTES_DIRECTORY": str(self.notes_dir),
            "SCREENSHOTS_DIR": str(self.screenshots_dir),
            "CHROMA_DB_PATH": str(base / "chroma"),
            "CONFIG_FILE_PATH": str(self.config_path),
            "AVAILABLE_TOOLS": ["nmap", "burp"],
            "SELECTED_TOOLS": ["nmap"],
        }

    def load_config(self):
        return dict(self.config)

    def setengagement_folder(self, text):
        self.engagement_folder = text


class FakeMessageBox:
    StandardButton = main_window_module.QMessageBox.StandardButton
    Icon = main_window_module.QMessageBox.Icon
    response = None

    def __init__(self, *args, **kwargs):
        self.window_title = None
        self.text = None
        self.informative_text = None
        self.stylesheet = None
        self.default_button = None
        self.icon = None

    def setWindowTitle(self, title):
        self.window_title = title

    def setText(self, text):
        self.text = text

    def setInformativeText(self, text):
        self.informative_text = text

    def setDefaultButton(self, button):
        self.default_button = button

    def setStandardButtons(self, buttons):
        self.buttons = buttons

    def setStyleSheet(self, stylesheet):
        self.stylesheet = stylesheet

    def setIcon(self, icon):
        self.icon = icon

    def move(self, point):
        self.point = point

    def frameGeometry(self):
        class Geometry:
            def __init__(self):
                self.center_point = None

            def moveCenter(self, point):
                self.center_point = point

            def topLeft(self):
                return QPoint(0, 0)

        return Geometry()

    def exec(self):
        return self.response

    @staticmethod
    def information(*args, **kwargs):
        return None

    @staticmethod
    def warning(*args, **kwargs):
        return None

    @staticmethod
    def critical(*args, **kwargs):
        return None


def make_paths(tmp_path):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; font-size: 10pt; }")
    icon = tmp_path / "icon.png"
    icon.write_bytes(PNG_BYTES)
    return stylesheet, icon


def patch_nebula_dependencies(monkeypatch, tmp_path):
    stylesheet, icon = make_paths(tmp_path)
    monkeypatch.setattr(
        main_window_module,
        "return_path",
        lambda path: str(stylesheet if path.endswith(".css") else icon),
    )
    monkeypatch.setattr(main_window_module, "ConfigManager", FakeConfigManager)
    monkeypatch.setattr(main_window_module, "settings", FakeSettings)
    monkeypatch.setattr(main_window_module, "CustomSearchLineEdit", FakeSearchLineEdit)
    monkeypatch.setattr(main_window_module, "CommandInputArea", FakeCommandInputArea)
    monkeypatch.setattr(
        main_window_module,
        "CentralDisplayAreaInMainWindow",
        FakeCentralDisplayArea,
    )
    monkeypatch.setattr(main_window_module, "SuggestionsPopOutWindow", FakeSuggestionsWindow)
    monkeypatch.setattr(main_window_module, "AiNotes", FakeAiNotes)
    monkeypatch.setattr(main_window_module, "AiNotesPopupWindow", FakePopupWindow)
    monkeypatch.setattr(main_window_module, "UserNoteTaking", FakeDialogWindow)
    monkeypatch.setattr(main_window_module, "ImageCommandWindow", FakeDialogWindow)
    monkeypatch.setattr(main_window_module, "TerminalEmulatorWindow", FakeTerminalWindow)
    monkeypatch.setattr(main_window_module, "HelpWindow", FakeDialogWindow)
    monkeypatch.setattr(main_window_module, "DocumentLoaderDialog", FakeDialogWindow)
    monkeypatch.setattr(main_window_module, "ChromaManager", FakeChromaManager)
    monkeypatch.setattr(main_window_module, "statusFeedManager", FakeStatusFeedManager)
    monkeypatch.setattr(main_window_module.tool_configuration, "ToolsWindow", FakeToolsWindow)
    monkeypatch.setattr(main_window_module, "QThread", FakeThread)
    monkeypatch.setattr(main_window_module, "InsightsProcessorWorker", FakeWorker)
    monkeypatch.setattr(main_window_module, "FileProcessorWorker", FakeWorker)
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )
    return stylesheet, icon


def build_nebula_window(qapp, tmp_path, monkeypatch):
    patch_nebula_dependencies(monkeypatch, tmp_path)
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    (engagement / "engagement_details.json").write_text(
        json.dumps({"engagement_name": "Acme"})
    )
    (engagement / "logs" / "result.txt").parent.mkdir(parents=True, exist_ok=True)
    (engagement / "logs" / "result.txt").write_text("scan output")
    window = main_window_module.Nebula(str(engagement))
    window.show()
    qapp.processEvents()
    return window, engagement


def test_workers_and_log_sidebar(qapp, tmp_path, monkeypatch):
    config_manager = FakeConfigManager(str(tmp_path))
    command_input = FakeCommandInputArea(manager=config_manager)

    monkeypatch.setattr(main_window_module.utilities, "is_nessus_file", lambda path: True)
    monkeypatch.setattr(main_window_module.utilities, "is_zap_file", lambda path: False)
    monkeypatch.setattr(main_window_module.utilities, "is_nmap_file", lambda path: False)
    monkeypatch.setattr(main_window_module.utilities, "is_nikto_file", lambda path: False)
    monkeypatch.setattr(main_window_module.utilities, "parse_nessus_file", lambda path: "insight")

    worker = main_window_module.InsightsProcessorWorker("scan.nessus", "notes", command_input)
    finished = []
    errors = []
    worker.signals.finished.connect(lambda: finished.append(True))
    worker.signals.error.connect(errors.append)
    worker.run()
    worker.on_api_call_finished()
    assert command_input.executed_api_calls[0] == ("insight", "notes")
    assert command_input.api_tasks == 1

    monkeypatch.setattr(
        main_window_module.utilities,
        "parse_nessus_file",
        lambda path: (_ for _ in ()).throw(RuntimeError("bad parse")),
    )
    error_worker = main_window_module.InsightsProcessorWorker(
        "scan.nessus", "notes", command_input
    )
    error_worker.signals.error.connect(errors.append)
    error_worker.load_insights_into_queue()
    assert isinstance(errors[-1], RuntimeError)

    file_path = tmp_path / "scan.txt"
    file_path.write_text("alpha beta gamma")
    monkeypatch.setattr(main_window_module, "token_counter", lambda content, encoding: 3)
    file_worker = main_window_module.FileProcessorWorker(str(file_path), "command", command_input)
    file_worker.run()
    assert command_input.executed_api_calls[-1] == ("alpha beta gamma", "command")

    monkeypatch.setattr(main_window_module, "token_counter", lambda content, encoding: 9000)
    monkeypatch.setattr(main_window_module, "tokenizer", lambda content, encoding: ["a", "b", "c", "d"])
    monkeypatch.setattr(
        main_window_module,
        "encoding_getter",
        lambda encoding: SimpleNamespace(decode=lambda chunk: "".join(chunk)),
    )
    chunk_worker = main_window_module.FileProcessorWorker(str(file_path), "command", command_input)
    chunk_worker._process_data("content")
    chunk_worker.process_next_chunk()
    chunk_worker.on_api_call_finished()
    assert chunk_worker.split_into_chunks_by_tokens("data", 2) == ["ab", "cd"]

    sidebar = main_window_module.LogSideBar(manager=config_manager)
    emitted_notes = []
    emitted_suggestions = []
    sidebar.send_to_ai_notes_signal.connect(lambda path, endpoint: emitted_notes.append((path, endpoint)))
    sidebar.send_to_ai_suggestions_signal.connect(
        lambda path, endpoint: emitted_suggestions.append((path, endpoint))
    )
    (Path(config_manager.config["LOG_DIRECTORY"]) / "keep.txt").write_text("data")
    sidebar.addItem("keep.txt")
    sidebar.setCurrentRow(0)
    monkeypatch.setattr(main_window_module.QMenu, "exec", lambda self, position: None)
    sidebar.contextMenuEvent(SimpleNamespace(pos=lambda: QPoint(1, 1), globalPos=lambda: QPoint(2, 2)))
    sidebar.enable_or_disable_due_to_model_creation(True)
    sidebar.enable_or_disable_due_to_model_creation(False)

    FakeMessageBox.response = main_window_module.QMessageBox.StandardButton.Yes
    monkeypatch.setattr(main_window_module, "QMessageBox", FakeMessageBox)
    assert sidebar.confirm_delete(str(Path(config_manager.config["LOG_DIRECTORY"]) / "keep.txt")) is True
    assert sidebar.confirm_delete_all_files() is True

    monkeypatch.setattr(sidebar, "confirm_delete_all_files", lambda *_: True)
    monkeypatch.setattr(sidebar, "confirm_delete", lambda *_: True)
    sidebar.delete_all_files()
    (Path(config_manager.config["LOG_DIRECTORY"]) / "keep.txt").write_text("data")
    sidebar.delete_file()
    assert not (Path(config_manager.config["LOG_DIRECTORY"]) / "keep.txt").exists()

    (Path(config_manager.config["LOG_DIRECTORY"]) / "send.txt").write_text("data")
    sidebar.addItem("send.txt")
    sidebar.setCurrentRow(0)
    sidebar.send_to_ai_notes()
    sidebar.send_to_ai_suggestions()
    assert emitted_notes[-1][1] == "notes_files"
    assert emitted_suggestions[-1][1] == "suggestion_files"

    (Path(config_manager.config["LOG_DIRECTORY"]) / "old.txt").write_text("data")
    sidebar.clear()
    sidebar.addItem("old.txt")
    sidebar.setCurrentRow(0)
    monkeypatch.setattr(
        main_window_module.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("new.txt", True),
    )
    sidebar.rename_file()
    assert (Path(config_manager.config["LOG_DIRECTORY"]) / "new.txt").exists()
    sidebar.close()


def test_worker_and_sidebar_edge_paths(qapp, tmp_path, monkeypatch):
    config_manager = FakeConfigManager(str(tmp_path))

    monkeypatch.setattr(
        main_window_module.utilities,
        "is_nessus_file",
        lambda path: path.endswith(".nessus"),
    )
    monkeypatch.setattr(
        main_window_module.utilities,
        "is_zap_file",
        lambda path: path.endswith(".zap"),
    )
    monkeypatch.setattr(
        main_window_module.utilities,
        "is_nmap_file",
        lambda path: path.endswith(".xml"),
    )
    monkeypatch.setattr(
        main_window_module.utilities,
        "is_nikto_file",
        lambda path: path.endswith(".nikto"),
    )
    monkeypatch.setattr(main_window_module.utilities, "parse_zap", lambda path: "zap insight")
    monkeypatch.setattr(
        main_window_module.utilities,
        "parse_nmap",
        lambda path: "nmap insight",
    )
    monkeypatch.setattr(
        main_window_module.utilities,
        "parse_nikto_xml",
        lambda path: "nikto insight",
    )

    finished = []
    idle_command_input = FakeCommandInputArea(manager=config_manager)
    idle_worker = main_window_module.InsightsProcessorWorker(
        "scan.txt", "notes", idle_command_input
    )
    idle_worker.signals.finished.connect(lambda: finished.append("done"))
    idle_worker.run()
    assert finished == ["done"]

    for file_name, expected in (
        ("scan.zap", "zap insight"),
        ("scan.xml", "nmap insight"),
        ("scan.nikto", "nikto insight"),
    ):
        branch_worker = main_window_module.InsightsProcessorWorker(
            file_name, "notes", FakeCommandInputArea(manager=config_manager)
        )
        branch_worker.load_insights_into_queue()
        assert branch_worker.insights_queue.get() == expected

    failing_command_input = FakeCommandInputArea(manager=config_manager)
    failing_command_input.execute_api_call = lambda *args: (_ for _ in ()).throw(
        RuntimeError("api failed")
    )
    failing_worker = main_window_module.InsightsProcessorWorker(
        "scan.nessus", "notes", failing_command_input
    )
    insight_errors = []
    failing_worker.signals.error.connect(insight_errors.append)
    failing_worker.insights_queue.put("broken")
    failing_worker.process_next_insight()
    assert str(insight_errors[-1]) == "api failed"

    file_path = tmp_path / "scan.txt"
    file_path.write_text("alpha beta gamma")
    monkeypatch.setattr(main_window_module, "token_counter", lambda content, encoding: 9001)
    monkeypatch.setattr(main_window_module, "tokenizer", lambda content, encoding: list("abcd"))
    monkeypatch.setattr(
        main_window_module,
        "encoding_getter",
        lambda encoding: SimpleNamespace(decode=lambda chunk: "".join(chunk)),
    )

    chunk_command_input = FakeCommandInputArea(manager=config_manager)
    queued_worker = main_window_module.FileProcessorWorker(
        str(file_path), "command", chunk_command_input
    )
    queued_worker.run()
    assert chunk_command_input.executed_api_calls[-1] == ("abcd", "command")

    halted_worker = main_window_module.FileProcessorWorker(
        str(file_path), "command", FakeCommandInputArea(manager=config_manager)
    )
    halted_worker.halt_processing = True
    halted_worker._process_data("halt me")
    assert halted_worker.chunks_queue.empty()

    inner_error_worker = main_window_module.FileProcessorWorker(
        str(file_path), "command", FakeCommandInputArea(manager=config_manager)
    )
    monkeypatch.setattr(
        inner_error_worker,
        "_process_data",
        lambda *_: (_ for _ in ()).throw(RuntimeError("inner failure")),
    )
    inner_error_worker.load_chunks_into_queue()

    outer_error_worker = main_window_module.FileProcessorWorker(
        str(tmp_path / "missing.txt"),
        "command",
        FakeCommandInputArea(manager=config_manager),
    )
    outer_error_worker.load_chunks_into_queue()

    chunk_error_input = FakeCommandInputArea(manager=config_manager)
    chunk_error_input.execute_api_call = lambda *args: (_ for _ in ()).throw(
        RuntimeError("chunk failure")
    )
    chunk_error_worker = main_window_module.FileProcessorWorker(
        str(file_path), "command", chunk_error_input
    )
    chunk_errors = []
    chunk_error_worker.signals.error.connect(chunk_errors.append)
    chunk_error_worker.chunks_queue.put("chunk")
    chunk_error_worker.process_next_chunk()
    assert str(chunk_errors[-1]) == "chunk failure"

    sidebar = main_window_module.LogSideBar(manager=config_manager)
    monkeypatch.setattr(main_window_module, "QMessageBox", FakeMessageBox)
    FakeMessageBox.response = main_window_module.QMessageBox.StandardButton.No
    assert sidebar.confirm_delete(str(tmp_path / "missing.txt")) is False
    assert sidebar.confirm_delete_all_files() is False

    cancel_file = Path(config_manager.config["LOG_DIRECTORY"]) / "cancel.txt"
    cancel_file.write_text("data")
    sidebar.addItem("cancel.txt")
    sidebar.setCurrentRow(0)
    with monkeypatch.context() as ctx:
        ctx.setattr(sidebar, "confirm_delete_all_files", lambda *_: False)
        sidebar.delete_all_files()

    assert cancel_file.exists()

    failing_delete_file = Path(config_manager.config["LOG_DIRECTORY"]) / "failing.txt"
    failing_delete_file.write_text("data")
    with monkeypatch.context() as ctx:
        ctx.setattr(sidebar, "confirm_delete_all_files", lambda *_: True)
        ctx.setattr(
            main_window_module.os,
            "remove",
            lambda path: (_ for _ in ()).throw(OSError("delete all failure")),
        )
        sidebar.delete_all_files()

    sidebar.clear()
    cancel_file.write_text("data")
    sidebar.addItem("cancel.txt")
    sidebar.setCurrentRow(0)
    with monkeypatch.context() as ctx:
        ctx.setattr(sidebar, "confirm_delete", lambda *_: False)
        sidebar.delete_file()
    assert cancel_file.exists()

    sidebar.clear()
    failing_delete_file.write_text("data")
    sidebar.addItem("failing.txt")
    sidebar.setCurrentRow(0)
    with monkeypatch.context() as ctx:
        ctx.setattr(sidebar, "confirm_delete", lambda *_: True)
        ctx.setattr(
            main_window_module.os,
            "remove",
            lambda path: (_ for _ in ()).throw(OSError("delete failure")),
        )
        sidebar.delete_file()

    sidebar.clear()
    sidebar.addItem("missing.txt")
    sidebar.setCurrentRow(0)
    sidebar.delete_file()

    warnings = []
    criticals = []
    existing_target = Path(config_manager.config["LOG_DIRECTORY"]) / "existing.txt"
    existing_target.write_text("occupied")
    rename_source = Path(config_manager.config["LOG_DIRECTORY"]) / "old.txt"
    rename_source.write_text("data")
    sidebar.clear()
    sidebar.addItem("old.txt")
    sidebar.setCurrentRow(0)
    monkeypatch.setattr(
        main_window_module.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("existing.txt", True),
    )
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    sidebar.rename_file()
    assert warnings

    rename_failure_source = Path(config_manager.config["LOG_DIRECTORY"]) / "rename_fail.txt"
    rename_failure_source.write_text("data")
    sidebar.clear()
    sidebar.addItem("rename_fail.txt")
    sidebar.setCurrentRow(0)
    monkeypatch.setattr(
        main_window_module.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("renamed.txt", True),
    )
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "critical",
        lambda *args: criticals.append(args),
    )
    with monkeypatch.context() as ctx:
        ctx.setattr(
            main_window_module.os,
            "rename",
            lambda *args: (_ for _ in ()).throw(OSError("rename failure")),
        )
        sidebar.rename_file()
    assert criticals
    sidebar.close()


def test_nebula_window_workflow(qapp, tmp_path, monkeypatch):
    window, engagement = build_nebula_window(qapp, tmp_path, monkeypatch)
    info_messages = []
    tooltip_messages = []
    warnings = []
    criticals = []
    monkeypatch.setattr(main_window_module.QMessageBox, "information", lambda *args: info_messages.append(args))
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    monkeypatch.setattr(main_window_module.QMessageBox, "critical", lambda *args: criticals.append(args))
    monkeypatch.setattr(main_window_module.QToolTip, "showText", lambda *args: tooltip_messages.append(args))

    try:
        assert window.windowTitle() == "Nebula - Acme"
        assert window.status_feed_list.count() == 2

        window.pop_out_status_feed()
        window.update_status_feed_ui(["one", "two"])
        assert window.status_feed_list.count() == 2

        window.show_document_loader_dialog()
        assert window.input_mode == "terminal"

        monkeypatch.setattr(window, "show_message", lambda message: info_messages.append(message))
        window.open_tour()
        window.enable_all_tools()
        assert window.tools_window.select_all_calls == 1

        fake_box = FakeMessageBox()
        monkeypatch.setattr(main_window_module, "QMessageBox", FakeMessageBox)
        FakeMessageBox.response = main_window_module.QMessageBox.StandardButton.No
        window.start_tour()
        FakeMessageBox.response = main_window_module.QMessageBox.StandardButton.Yes
        window.start_tour()
        window.create_centered_message_box(
            "Title",
            "Body",
            main_window_module.QMessageBox.StandardButton.Yes,
            main_window_module.QMessageBox.StandardButton.Yes,
        )
        window.next_step()
        window.highlight_action(window.help_actions[0])
        window.unhighlight_action(window.help_actions[0])
        assert tooltip_messages

        window.reset_terminal()
        assert window.command_input_area.terminal.reset_calls == 1
        window.bring_windows_to_front()
        window.update_clear_button_state(True)
        window.update_clear_button_state(False)
        window.switch_between_terminal_and_ai()
        window.switch_between_terminal_and_ai()
        window.change_clear_button_icon_temporarily(True)
        window.change_clear_button_icon_temporarily(False)
        window.stop_terminal_operations()
        assert window.command_input_area.terminal.writes[-2:] == ["<Ctrl-C>", "<Ctrl-\\>"]

        assert window.loadJsonData(str(engagement / "engagement_details.json"))["engagement_name"] == "Acme"
        assert window.loadJsonData(str(engagement / "missing.json")) == {}

        action = main_window_module.QAction("Act", window)
        hit = []
        window.change_icon_temporarily(action, "temp", "orig")
        window.provide_feedback_and_execute(action, "temp", "orig", lambda: hit.append(True))
        assert hit == [True]

        window.setThreadStatus("in_progress")
        window.setThreadStatus("completed")
        window.update_engagement_folder("updated")
        assert window.manager.engagement_folder == "updated"
        window.open_engagement()
        window.open_tools_window()
        window.center()

        window.add_new_tool("zmap")
        window.update_selected_tools(["burp"])
        window.save_config(
            {
                "CONFIG_FILE_PATH": str(tmp_path / "saved.json"),
                "value": "data",
            }
        )
        assert (tmp_path / "saved.json").exists()

        window.clear_screen()
        window.open_help()
        window.open_help()
        window.load_known_files()
        window.on_search_result_selected("result text")
        assert "result text" in window.central_display_area.toPlainText()

        window.eco_mode.setChecked(True)
        window.update_eco_mode_display()
        window.eco_mode.setChecked(False)
        window.update_eco_mode_display()

        window.suggestions_action.setChecked(True)
        window.update_suggestions_display()
        window.suggestions_action.setChecked(False)
        window.update_suggestions_display()

        window.update_terminal_output("line one")
        window.update_terminal_output("\x1b[2J")
        window.update_terminal_output_for_api("api line")
        assert "api line" in window.central_display_area.toPlainText()

        window.open_note_taking()
        window.openTerminalEmulator()
        window.open_suggestions_pop_out_window()
        window.open_image_command_window()
        window.ai_note_taking_function(True)
        window.ai_note_taking_function(False)

        screenshot_path = tmp_path / "screen.png"
        monkeypatch.setattr(
            main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(screenshot_path), "png"),
        )
        window.take_screenshot()
        assert screenshot_path.exists()

        size_before = window.current_font_size
        window.adjust_font_size(2)
        assert window.current_font_size == size_before + 2

        item = window.log_side_bar.item(0)
        if item is None:
            window.log_side_bar.addItem("result.txt")
            item = window.log_side_bar.item(0)
        window.on_file_item_clicked(item)
        assert window.command_input_area.executed_commands[-1].startswith("cat ")
        window.show_large_file_warning()

        source = tmp_path / "upload.txt"
        source.write_text("data")
        monkeypatch.setattr(window, "get_file_path", lambda: str(source))
        window.upload_file()
        assert (Path(window.CONFIG["LOG_DIRECTORY"]) / "upload.txt").exists()
        window.process_file(str(source))
        window.populate_file_list()

        window.pop_out_notes()
        window.update_main_notes("<b>updated</b>")
        assert "updated" in window.ai_notes.toHtml()
        window.load_stylesheet(str(tmp_path / "style.css"))

        window.suggestions_action.setChecked(True)
        window.ai_note_taking_action.setChecked(True)
        before = set(window.known_files)
        new_file = Path(window.CONFIG["LOG_DIRECTORY"]) / "fresh.txt"
        new_file.write_text("new")
        window.on_directory_changed(window.CONFIG["LOG_DIRECTORY"])
        assert "fresh.txt" in window.known_files or before != window.known_files

        window.update_ai_notes("raw <data>")
        window.update_suggestions_notes("tips")
        assert window.suggestions_pop_out_window.updates[-1] == "tips"

        window.eco_mode.setChecked(True)
        window.process_new_file_with_ai("fresh.txt", "notes_files")
        assert window.worker_threads
        file_key = next(iter(window.worker_threads))
        window.cleanup_worker(file_key)

        window.eco_mode.setChecked(False)
        window.process_file_in_chunks_and_send_to_ai_threadsafe(
            str(new_file), "notes_files"
        )
        worker_key = next(iter(window.worker_threads))
        window.cleanup_worker(worker_key)

        close_event = SimpleNamespace(accept=lambda: hit.append("accepted"))
        window.closeEvent(close_event)
        assert "accepted" in hit
    finally:
        window.close()


def test_nebula_window_edge_and_error_paths(qapp, tmp_path, monkeypatch):
    info_messages = []
    utility_messages = []
    warnings = []
    criticals = []

    blank_dir = tmp_path / "blank"
    blank_dir.mkdir()
    patch_nebula_dependencies(monkeypatch, blank_dir)
    monkeypatch.setattr(FakeAiNotes, "blank_initial_html", True)
    blank_engagement = blank_dir / "engagement"
    blank_engagement.mkdir()
    (blank_engagement / "engagement_details.json").write_text(
        json.dumps({"engagement_name": "Blank"})
    )
    blank_window = main_window_module.Nebula(str(blank_engagement))
    blank_window.show()
    qapp.processEvents()
    assert "AI notes will be displayed here" in blank_window.ai_notes.toPlainText()
    blank_window.close()

    load_error_dir = tmp_path / "load_error"
    load_error_dir.mkdir()
    with monkeypatch.context() as ctx:
        patch_nebula_dependencies(ctx, load_error_dir)
        engagement = load_error_dir / "engagement"
        engagement.mkdir()
        (engagement / "engagement_details.json").write_text(
            json.dumps({"engagement_name": "LoadError"})
        )
        ctx.setattr(
            main_window_module.Nebula,
            "loadJsonData",
            lambda self, path: (_ for _ in ()).throw(RuntimeError("bad json")),
        )
        load_error_window = main_window_module.Nebula(str(engagement))
        assert load_error_window.windowTitle() == "Nebula"
        load_error_window.close()

    window, engagement = build_nebula_window(qapp, tmp_path, monkeypatch)
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "information",
        lambda *args: info_messages.append(args),
    )
    monkeypatch.setattr(
        main_window_module.utilities,
        "show_message",
        lambda *args: utility_messages.append(args),
    )
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "critical",
        lambda *args: criticals.append(args),
    )

    try:
        window.show_message("tour")
        assert info_messages

        window.current_action_index = len(window.help_actions) - 1
        window.next_step()

        with monkeypatch.context() as ctx:
            ctx.setattr(window, "load_stylesheet", lambda *_: (_ for _ in ()).throw(RuntimeError("status feed fail")))
            window.pop_out_status_feed()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module,
                "DocumentLoaderDialog",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("dialog fail")),
            )
            window.show_document_loader_dialog()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module,
                "settings",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("settings fail")),
            )
            window.open_engagement()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module,
                "HelpWindow",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("help fail")),
            )
            window.open_help()

        window.CONFIG.pop("AVAILABLE_TOOLS", None)
        window.add_new_tool("masscan")
        assert "masscan" in window.CONFIG["AVAILABLE_TOOLS"]

        window.save_config({"CONFIG_FILE_PATH": str(tmp_path), "value": "data"})

        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module.os,
                "listdir",
                lambda path: (_ for _ in ()).throw(FileNotFoundError("missing directory")),
            )
            window.load_known_files()
        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module.os,
                "listdir",
                lambda path: (_ for _ in ()).throw(PermissionError("permission denied")),
            )
            window.load_known_files()
        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module.os,
                "listdir",
                lambda path: (_ for _ in ()).throw(RuntimeError("unexpected failure")),
            )
            window.load_known_files()
        assert len(utility_messages) == 3

        window.textEditor.hide()
        window.open_note_taking()
        with monkeypatch.context() as ctx:
            window.textEditor = SimpleNamespace(
                isVisible=lambda: False,
                show=lambda: (_ for _ in ()).throw(RuntimeError("note fail")),
            )
            window.open_note_taking()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module,
                "TerminalEmulatorWindow",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("terminal fail")),
            )
            window.openTerminalEmulator()

        window.suggestions_pop_out_window.hide()
        window.open_suggestions_pop_out_window()
        with monkeypatch.context() as ctx:
            window.suggestions_pop_out_window = SimpleNamespace(
                isVisible=lambda: False,
                show=lambda: (_ for _ in ()).throw(RuntimeError("suggestions fail")),
            )
            window.open_suggestions_pop_out_window()

        visible_image_calls = []
        window.image_command_window = SimpleNamespace(
            isVisible=lambda: True,
            raise_=lambda: visible_image_calls.append("raise"),
            activateWindow=lambda: visible_image_calls.append("activate"),
        )
        window.open_image_command_window()
        assert visible_image_calls == ["raise", "activate"]
        with monkeypatch.context() as ctx:
            window.image_command_window = SimpleNamespace(
                isVisible=lambda: False,
                show=lambda: (_ for _ in ()).throw(RuntimeError("image fail")),
            )
            window.open_image_command_window()

        window.on_file_item_clicked(None)
        window.on_file_item_clicked(SimpleNamespace(text=lambda: "missing.txt"))

        large_file = Path(window.CONFIG["LOG_DIRECTORY"]) / "large.txt"
        large_file.write_text("x" * (window.size_threshold + 1))
        large_warnings = []
        monkeypatch.setattr(window, "show_large_file_warning", lambda *_: large_warnings.append(True))
        window.on_file_item_clicked(SimpleNamespace(text=lambda: "large.txt"))
        assert large_warnings == [True]

        with monkeypatch.context() as ctx:
            missing_dir = tmp_path / "missing-logs"
            window.CONFIG["LOG_DIRECTORY"] = str(missing_dir)
            ctx.setattr(main_window_module.os.path, "exists", lambda path: False)
            window.populate_file_list()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                window,
                "get_file_path",
                lambda: (_ for _ in ()).throw(RuntimeError("picker fail")),
            )
            window.upload_file()

        class AcceptedFileDialog:
            class FileMode:
                ExistingFile = object()

            class DialogCode:
                Accepted = 1

            def __init__(self, parent):
                self.parent = parent

            def setFileMode(self, mode):
                self.mode = mode

            def setNameFilter(self, text):
                self.filter_text = text

            def setDirectory(self, directory):
                self.directory = directory

            def exec(self):
                return self.DialogCode.Accepted

            def selectedFiles(self):
                return [str(tmp_path / "picked.txt")]

        class RejectedFileDialog(AcceptedFileDialog):
            class DialogCode:
                Accepted = 1

            def exec(self):
                return 0

        monkeypatch.setattr(main_window_module, "QFileDialog", AcceptedFileDialog)
        assert window.get_file_path() == str(tmp_path / "picked.txt")
        monkeypatch.setattr(main_window_module, "QFileDialog", RejectedFileDialog)
        assert window.get_file_path() is None

        log_directory = tmp_path / "copied-logs"
        source_file = tmp_path / "source.txt"
        source_file.write_text("copied")
        window.CONFIG["LOG_DIRECTORY"] = str(log_directory)
        window.process_file(str(source_file))
        assert (log_directory / "source.txt").exists()

        pop_out_calls = []
        window.pop_out_window = SimpleNamespace(
            isVisible=lambda: True,
            raise_=lambda: pop_out_calls.append("raise"),
            activateWindow=lambda: pop_out_calls.append("activate"),
        )
        window.pop_out_notes()
        assert pop_out_calls == ["raise", "activate"]
        with monkeypatch.context() as ctx:
            window.pop_out_window = SimpleNamespace(isVisible=lambda: False)
            ctx.setattr(
                main_window_module,
                "AiNotesPopupWindow",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("popup fail")),
            )
            window.pop_out_notes()

        window.on_directory_changed(str(tmp_path / "does-not-exist"))
        current_files = set(os.listdir(window.CONFIG["LOG_DIRECTORY"]))
        window.known_files = set(current_files)
        window.on_directory_changed(window.CONFIG["LOG_DIRECTORY"])
        with monkeypatch.context() as ctx:
            ctx.setattr(
                main_window_module.os,
                "listdir",
                lambda path: (_ for _ in ()).throw(RuntimeError("watch fail")),
            )
            window.on_directory_changed(window.CONFIG["LOG_DIRECTORY"])

        with monkeypatch.context() as ctx:
            window.eco_mode.setChecked(False)
            ctx.setattr(
                window,
                "process_file_in_chunks_and_send_to_ai_threadsafe",
                lambda *args: (_ for _ in ()).throw(IOError("io fail")),
            )
            window.process_new_file_with_ai("source.txt", "notes_files")
        with monkeypatch.context() as ctx:
            window.eco_mode.setChecked(False)
            ctx.setattr(
                window,
                "process_file_in_chunks_and_send_to_ai_threadsafe",
                lambda *args: (_ for _ in ()).throw(RuntimeError("generic fail")),
            )
            window.process_new_file_with_ai("source.txt", "notes_files")

        if hasattr(window, "file_thread_counters"):
            delattr(window, "file_thread_counters")
        window.process_file_in_chunks_and_send_to_ai_threadsafe(
            str(source_file), "notes_files"
        )
        worker_key = next(iter(window.worker_threads))
        window.cleanup_worker(worker_key)

        bad_child = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("close fail")))
        window.child_windows.append(bad_child)
        accepted = []
        window.closeEvent(SimpleNamespace(accept=lambda: accepted.append(True)))
        assert accepted == [True]
    finally:
        window.close()
