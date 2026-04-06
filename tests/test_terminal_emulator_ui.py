import builtins
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QTextCursor
from PyQt6.QtWidgets import QDialog, QLineEdit, QTextEdit, QWidget

from nebula import terminal_emulator


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeMemory:
    def __init__(self):
        self.history = []
        self.saved = 0

    def add_message(self, role, content):
        self.history.append({"role": role, "content": content})

    def save(self):
        self.saved += 1


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self):
        self.prompts = []
        self.bound_tools = None

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return FakeResponse(f"reply:{prompt}")

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self


class FakeAgent:
    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return f"ran:{command}"


class FakeExecutor:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def invoke(self, payload):
        self.calls.append(payload)
        return {"output": f"out:{payload['input']}"}


class FakeManager:
    def __init__(self, tmp_path):
        self.memory_dir = tmp_path / "memory"
        self.memory_dir.mkdir()
        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        self.screenshots_dir = tmp_path / "shots"
        self.screenshots_dir.mkdir()
        self.history_file = tmp_path / "history.txt"
        self.history_file.write_text("first\nsecond\n")
        self.privacy_file = tmp_path / "privacy.txt"
        self.privacy_file.write_text("")
        self.config = {
            "MODEL": "demo-model",
            "OLLAMA_URL": "http://ollama",
            "MEMORY_DIRECTORY": str(self.memory_dir),
            "HISTORY_FILE": str(self.history_file),
            "LOG_DIRECTORY": str(self.log_dir),
            "SCREENSHOTS_DIR": str(self.screenshots_dir),
            "PRIVACY_FILE": str(self.privacy_file),
            "SELECTED_TOOLS": ["cat", "ls"],
        }

    def load_config(self):
        return self.config


class FakeWindowTerminal:
    def __init__(self):
        self.password_mode = Signal()
        self.busy = Signal()
        self.writes = []
        self.reset_calls = 0

    def write(self, data):
        self.writes.append(data)

    def reset_terminal(self):
        self.reset_calls += 1


class FakeWindowCommandInputArea(QLineEdit):
    def __init__(self, parent=None, manager=None):
        super().__init__(parent)
        self.manager = manager
        self.terminal = FakeWindowTerminal()
        self.updateCentralDisplayArea = Signal()
        self.updateCentralDisplayAreaForApi = Signal()
        self.update_ai_notes = Signal()
        self.update_suggestions_notes = Signal()
        self.threads_status = Signal()
        self.executed_api_calls = []

    def execute_api_call(self, *args):
        self.executed_api_calls.append(args)


class FakeCentralDisplayArea(QTextEdit):
    def __init__(self, manager=None, command_input_area=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.command_input_area = command_input_area
        self.notes_signal_from_central_display_area = Signal()
        self.suggestions_signal_from_central_display_area = Signal()
        self.font_sizes = []

    def set_font_size_for_copy_button(self, size):
        self.font_sizes.append(size)


class FakeBackend:
    def __init__(self, resolver, parent=None):
        self.resolver = resolver
        self.parent = parent
        self.data_ready = Signal()
        self.error = Signal()
        self.finished = Signal()
        self.started = 0
        self.writes = []
        self.restarted = 0

    def start(self):
        self.started += 1

    def write(self, data):
        self.writes.append(data)

    def restart(self):
        self.restarted += 1


class FakeTerminal:
    def __init__(self, manager):
        self.manager = manager
        self.password_mode = Signal()
        self.data_ready = Signal()
        self.busy = Signal()
        self.current_directory_changed = Signal()
        self.writes = []
        self.started = 0
        self.reset_calls = 0
        self.commands = []

    def start(self):
        self.started += 1

    def write(self, data):
        self.writes.append(data)

    def reset_terminal(self):
        self.reset_calls += 1

    def update_current_command(self, command):
        self.commands.append(command)


class FakeDialog:
    def __init__(self, command_text, parent=None, command_input_area=None):
        self.command_text = command_text

    def exec(self):
        return QDialog.DialogCode.Accepted

    def get_command(self):
        return f"dialog:{self.command_text}"


class FakeNotifier:
    Type = SimpleNamespace(Read=1)

    def __init__(self, fd, notifier_type, parent=None):
        self.fd = fd
        self.type = notifier_type
        self.parent = parent
        self.activated = Signal()
        self.enabled = False
        self.deleted = False

    def setEnabled(self, enabled):
        self.enabled = enabled

    def deleteLater(self):
        self.deleted = True


class FakeChild:
    def __init__(self, chunks=None, alive=True):
        self.child_fd = 3
        self._chunks = list(chunks or [])
        self._alive = alive
        self.writes = []
        self.terminated = []
        self.exitstatus = 4

    def isalive(self):
        return self._alive

    def write(self, data):
        self.writes.append(data)

    def terminate(self, force=False):
        self.terminated.append(force)
        self._alive = False

    def read_nonblocking(self, size=4096, timeout=0):
        item = self._chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeProcess:
    ProcessState = SimpleNamespace(Running=1, NotRunning=0)
    ProcessChannelMode = SimpleNamespace(MergedChannels=1)

    def __init__(self, parent=None):
        self.parent = parent
        self._state = self.ProcessState.NotRunning
        self.readyReadStandardOutput = Signal()
        self.errorOccurred = Signal()
        self.finished = Signal()
        self.channel_mode = None
        self.environment = None
        self.started = None
        self.writes = []
        self.terminated = False
        self.killed = False
        self.output = b"shell output"
        self.wait_results = [True]

    def setProcessChannelMode(self, mode):
        self.channel_mode = mode

    def state(self):
        return self._state

    def setProcessEnvironment(self, environment):
        self.environment = environment

    def start(self, program, arguments):
        self._state = self.ProcessState.Running
        self.started = (program, arguments)

    def write(self, data):
        self.writes.append(data)

    def terminate(self):
        self.terminated = True

    def waitForFinished(self, _timeout):
        return self.wait_results.pop(0) if self.wait_results else True

    def kill(self):
        self.killed = True
        self._state = self.ProcessState.NotRunning

    def readAllStandardOutput(self):
        return self.output


class FakeEnvironment:
    def __init__(self):
        self.values = {}

    def insert(self, key, value):
        self.values[key] = value


def make_manager(tmp_path):
    return FakeManager(tmp_path)


def select_document(widget):
    cursor = widget.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    widget.setTextCursor(cursor)


def test_agent_task_runner_run_and_query_paths(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    llm = FakeLLM()
    notes_memory = FakeMemory()
    suggestions_memory = FakeMemory()
    conversation_memory = FakeMemory()

    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_llm_instance",
        lambda model, ollama_url="", signals=None: (llm, "ollama"),
    )
    fake_agent = FakeAgent()
    monkeypatch.setattr(
        terminal_emulator,
        "initialize_agent",
        lambda *args, **kwargs: fake_agent,
    )

    notes_runner = terminal_emulator.AgentTaskRunner(
        query="take notes",
        endpoint="notes",
        notes_memory=notes_memory,
        manager=manager,
    )
    note_results = []
    notes_runner.signals.result.connect(lambda endpoint, command, result: note_results.append((endpoint, command, result)))
    notes_runner.run()
    assert note_results[-1][0:2] == ("notes", "ai")
    assert note_results[-1][2].startswith("reply:")

    suggestion_runner = terminal_emulator.AgentTaskRunner(
        query="suggest",
        endpoint="suggestion",
        suggestions_memory=suggestions_memory,
        manager=manager,
    )
    suggestion_runner.run()
    assert suggestions_memory.saved == 1

    command_runner = terminal_emulator.AgentTaskRunner(
        query="ls",
        endpoint="command",
        conversation_memory=conversation_memory,
        manager=manager,
    )
    command_runner.run()
    assert conversation_memory.saved == 1
    assert fake_agent.commands == ["You are a penetration testing assistant. :ls"]

    errors = []
    failing_runner = terminal_emulator.AgentTaskRunner(
        query="boom",
        endpoint="command",
        conversation_memory=FakeMemory(),
        manager=manager,
    )
    failing_runner.signals.error.connect(errors.append)
    monkeypatch.setattr(
        failing_runner,
        "query_llm",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("llm failed")),
    )
    failing_runner.run()
    assert errors == ["llm failed"]

    openai_executor = FakeExecutor()
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_llm_instance",
        lambda model, ollama_url="", signals=None: (llm, "openai"),
    )
    monkeypatch.setattr(
        terminal_emulator,
        "create_openai_tools_agent",
        lambda *args, **kwargs: "agent",
    )
    monkeypatch.setattr(
        terminal_emulator,
        "AgentExecutor",
        lambda *args, **kwargs: openai_executor,
    )
    monkeypatch.setattr(
        terminal_emulator.ChatPromptTemplate,
        "from_messages",
        lambda messages: messages,
    )
    openai_runner = terminal_emulator.AgentTaskRunner(
        query="pwd",
        endpoint="command",
        conversation_memory=conversation_memory,
        manager=manager,
    )
    assert openai_runner.query_llm("pwd", "command", model="demo", ollama_url="x") == "out:pwd"
    assert openai_executor.calls[-1] == {"input": "pwd"}

    notes_runner.notes_memory = FakeMemory()
    assert "reply:" in notes_runner.query_llm("facts", "notes")
    suggestion_runner.suggestions_memory = FakeMemory()
    assert "reply:" in suggestion_runner.query_llm("next", "suggestion")

    monkeypatch.setattr(
        terminal_emulator,
        "initialize_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("agent init failed")),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_llm_instance",
        lambda model, ollama_url="", signals=None: (llm, "ollama"),
    )
    with pytest.raises(RuntimeError, match="agent init failed"):
        command_runner.query_llm("ls", "command")


def test_terminal_emulator_window_paths(qapp, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; font-size: 10pt; }")
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"")
    monkeypatch.setattr(
        terminal_emulator,
        "return_path",
        lambda path: str(stylesheet if path.endswith(".css") else icon),
    )
    monkeypatch.setattr(
        terminal_emulator,
        "CommandInputArea",
        FakeWindowCommandInputArea,
    )
    monkeypatch.setattr(
        terminal_emulator,
        "CentralDisplayAreaInMainWindow",
        FakeCentralDisplayArea,
    )
    monkeypatch.setattr(
        terminal_emulator.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "contains_escape_sequences",
        lambda text: False,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "contains_only_spaces",
        lambda text: text.strip() == "",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "show_last_line",
        lambda document: "previous",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "process_text",
        lambda text: f"processed:{text}",
    )

    window = terminal_emulator.TerminalEmulatorWindow(
        manager=manager,
        terminal_emulator_number=2,
    )
    window.show()
    qapp.processEvents()

    try:
        assert window.windowTitle() == "Terminal - 2"
        window.clear_screen()
        assert window.command_input_area.terminal.writes[-1] == "\n"

        window.reset_terminal()
        assert window.command_input_area.terminal.reset_calls == 1

        source = tmp_path / "scan.txt"
        source.write_text("result")
        monkeypatch.setattr(window, "get_file_path", lambda: str(source))
        window.upload_file()
        assert (Path(manager.config["LOG_DIRECTORY"]) / "scan.txt").exists()

        action = terminal_emulator.QAction("Run", window)
        hits = []
        window.change_clear_button_icon_temporarily(True)
        window.change_clear_button_icon_temporarily(False)
        window.change_icon_temporarily(action, str(icon), str(icon))
        window.provide_feedback_and_execute(
            action,
            str(icon),
            str(icon),
            lambda: hits.append(True),
        )
        assert hits == [True]

        window.adjust_font_size(2)
        assert window.central_display_area.font_sizes[-1] == 12

        save_path = tmp_path / "shot.png"
        monkeypatch.setattr(
            terminal_emulator.QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(save_path), "png"),
        )
        window.take_screenshot()
        assert save_path.exists()

        window.update_terminal_output("line one")
        assert "line one" in window.central_display_area.toPlainText()
        window.update_terminal_output("\x1b[2J")
        assert window.central_display_area.toPlainText() == ""

        window.update_terminal_output_for_api("answer")
        assert "processed:answer" in window.central_display_area.toPlainText()
    finally:
        window.close()


def test_shell_backends_and_terminal_core(qapp, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    original_qprocess_backend = terminal_emulator.QProcessShellBackend
    original_pexpect_backend = terminal_emulator.PexpectShellBackend
    monkeypatch.setattr(
        terminal_emulator,
        "ConversationMemory",
        lambda file_path: FakeMemory(),
    )
    monkeypatch.setattr(terminal_emulator, "QProcessShellBackend", FakeBackend)
    monkeypatch.setattr(terminal_emulator, "PexpectShellBackend", FakeBackend)

    terminal = terminal_emulator.TerminalEmulator(manager=manager)
    outputs = []
    directories = []
    busy_values = []
    pwd_values = []
    done = []
    terminal.data_ready.connect(outputs.append)
    terminal.busy.connect(busy_values.append)
    terminal.password_mode.connect(pwd_values.append)
    terminal.autonomous_terminal_execution_iteration_is_done.connect(lambda: done.append(True))
    terminal.current_directory_changed.connect(directories.append)

    terminal.start()
    assert terminal.backend.started == 1

    monkeypatch.setattr(
        terminal_emulator.utilities,
        "is_linux_asking_for_password",
        lambda text: "password" in text.lower(),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "process_output",
        lambda text: f"processed:{text}" if text else "",
    )
    terminal._handle_backend_output("Password:")
    assert pwd_values[-1] is True

    terminal.current_command = "pwd"
    terminal._handle_backend_output("/tmp\nnebula$")
    assert directories[-1] == "/tmp"

    terminal.current_command = "ls"
    terminal.current_command_output = "out"
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "is_included_command",
        lambda command, config: True,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "log_command_output",
        lambda command, output, config: outputs.append((command, output)),
    )
    terminal.check_for_prompt("nebula$")
    assert busy_values[-1] is False

    monkeypatch.setattr(
        terminal_emulator.utilities,
        "extract_data_for_web",
        lambda text: f"web:{text}",
        raising=False,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "run_hooks",
        lambda text, path: f"hooked:{text}",
        raising=False,
    )
    terminal.autonomous_mode = True
    terminal.number_of_autonomous_commands = 1
    terminal.current_command = "whoami"
    terminal.check_for_prompt("nebula$")
    assert done

    terminal.number_of_autonomous_commands = 0
    terminal.web_mode = "enabled"
    terminal.current_command = "cat file"
    terminal.current_command_output = "data"
    terminal.check_for_prompt("nebula$")
    assert isinstance(outputs[-1], tuple)

    terminal.current_command = "reset"
    terminal.check_for_prompt("nebula$")
    assert busy_values[-1] is False

    assert terminal.extract_current_directory("/tmp\nnebula$") == "/tmp"
    assert terminal.extract_current_directory("other") is None

    terminal.reset_terminal()
    assert terminal.backend.restarted == 1

    terminal.write("<Ctrl-C>")
    terminal.write("<Up>")
    terminal.write("ls -la\n")
    assert terminal.backend.writes[-3:] == ["\x03", "\x1b[A", "ls -la\n"]

    terminal.update_current_command("alpha")
    terminal.autonomous_mode = True
    terminal.update_current_command("beta")
    assert terminal.current_command.startswith("beta-")

    monkeypatch.setenv("NEBULA_TERMINAL_SHELL", "/bin/zsh")
    terminal.backend_mode = "pexpect"
    shell_path, args, env = terminal._resolve_shell_command()
    assert shell_path == "/bin/zsh"
    assert "-d" in args
    assert "PS1" in env

    monkeypatch.delenv("NEBULA_TERMINAL_SHELL", raising=False)
    terminal.backend_mode = "qprocess"
    shell_path, args, env = terminal._resolve_shell_command()
    assert shell_path == "/bin/bash"
    assert "-i" in args

    fake_os = SimpleNamespace(name="nt", environ={"COMSPEC": "cmd.exe"}, path=os.path)
    monkeypatch.setattr(terminal_emulator, "os", fake_os)
    shell_path, args, env = terminal._resolve_shell_command()
    assert shell_path.endswith("cmd.exe")
    monkeypatch.setattr(terminal_emulator, "os", os)

    errors = []
    terminal._handle_backend_error("bad shell")
    terminal.backend.started = 0
    monkeypatch.setattr(
        terminal_emulator.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )
    terminal._handle_backend_finished(1)
    assert terminal.backend.started == 1

    monkeypatch.setattr(terminal_emulator, "QProcess", FakeProcess)
    monkeypatch.setattr(
        terminal_emulator,
        "QProcessShellBackend",
        original_qprocess_backend,
    )
    monkeypatch.setattr(
        terminal_emulator,
        "QProcessEnvironment",
        SimpleNamespace(systemEnvironment=lambda: FakeEnvironment()),
    )
    backend = terminal_emulator.QProcessShellBackend(
        lambda: ("bash", ["-i"], {"TERM": "xterm"})
    )
    data_ready = []
    finished = []
    backend.data_ready.connect(data_ready.append)
    backend.finished.connect(finished.append)
    backend.start()
    backend.write("pwd\n")
    backend._emit_output()
    backend._handle_error("bad")
    backend._handle_finished(0, None)
    assert data_ready == ["shell output"]
    assert finished == [0]
    backend.process.wait_results = [False, True]
    backend.stop()

    child = FakeChild(["chunk", terminal_emulator.pexpect.exceptions.TIMEOUT("timeout"), terminal_emulator.pexpect.exceptions.EOF("eof")])
    monkeypatch.setattr(
        terminal_emulator,
        "PexpectShellBackend",
        original_pexpect_backend,
    )
    monkeypatch.setattr(
        terminal_emulator.pexpect,
        "spawn",
        lambda *args, **kwargs: child,
    )
    monkeypatch.setattr(terminal_emulator, "QSocketNotifier", FakeNotifier)
    pexpect_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    emitted = []
    pexpect_backend.data_ready.connect(emitted.append)
    pexpect_backend.start()
    pexpect_backend.write("ls\n")
    pexpect_backend._read_ready()
    pexpect_backend._read_ready()
    pexpect_backend._read_ready()
    assert emitted == ["chunk"]
    pexpect_backend.stop()


def test_dynamic_completer_and_command_input_area(qapp, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_shell_commands",
        lambda: ["cat", "ls"],
    )
    completer = terminal_emulator.DynamicCompleter()
    assert "cat" in completer.completer_model("c")

    folder = tmp_path / "paths"
    folder.mkdir()
    (folder / "alpha.txt").write_text("a")
    (folder / "alpha two.txt").write_text("b")
    results = completer.list_paths(str(folder / "alp"))
    assert any("alpha.txt" in path for path in results)
    assert completer.list_paths(str(folder / "alp")) == results

    monkeypatch.setattr(terminal_emulator.os, "listdir", lambda path: (_ for _ in ()).throw(OSError("bad dir")))
    assert completer.list_paths(str(folder / "alp")) == results
    monkeypatch.setattr(terminal_emulator.os, "listdir", os.listdir)

    completer.update_model("cat " + str(folder / "alp"))
    index = completer.model().index(0, 0)
    line_edit = QLineEdit()
    line_edit.setText("cat")
    completer.setWidget(line_edit)
    completer.path_cache.clear()
    assert completer.pathFromIndex(index).startswith("cat ")

    monkeypatch.setattr(
        terminal_emulator,
        "TerminalEmulator",
        FakeTerminal,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "EditCommandDialog",
        FakeDialog,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "process_output",
        lambda text: f"processed:{text}",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "contains_only_spaces",
        lambda text: text.strip() == "",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "escape_file_path",
        lambda path: path.replace(" ", "\\ "),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "show_message",
        lambda title, message: None,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "log_command_output",
        lambda command, result, config: None,
    )
    monkeypatch.setattr(
        terminal_emulator,
        "ConversationMemory",
        lambda file_path: FakeMemory(),
    )

    widget = terminal_emulator.CommandInputArea(manager=manager)
    widget.show()
    qapp.processEvents()

    try:
        assert widget.terminal.started == 1
        widget.set_input_mode("ai")
        assert widget.input_mode == "ai"
        widget.set_password_mode(True)
        assert widget.password_mode is True
        widget.set_password_mode(False)
        assert widget.password_mode is False
        widget.set_style_sheet(True)
        widget.set_style_sheet(False)
        widget.on_current_directory_changed("/tmp")
        assert widget.current_directory == "/tmp"

        emitted = []
        widget.updateCentralDisplayArea.connect(emitted.append)
        widget.update_terminal_output("line")
        assert emitted[-1] == "processed:line"

        completed = []
        widget.threads_status.connect(completed.append)
        widget.updateCentralDisplayAreaForApi.connect(emitted.append)
        widget.api_tasks = 1
        widget.update_terminal_output_for_api("cmd", "answer")
        assert completed[-1] == "completed"
        assert emitted[-1] == "answer"

        widget.history = ["one", "two"]
        widget.history_index = 1
        widget.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
        )
        assert widget.text() == "one"
        widget.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        )

        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_C,
                Qt.KeyboardModifier.ControlModifier,
            )
        )
        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Backslash,
                Qt.KeyboardModifier.ControlModifier,
            )
        )
        assert widget.terminal.writes[-2:] == ["<Ctrl-C>", "<Ctrl-\\>"]

        qapp.clipboard().setText("x" * 101)
        pasted = []
        original_handle_large_paste = widget.handle_large_paste
        monkeypatch.setattr(widget, "handle_large_paste", pasted.append)
        widget.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_V,
                Qt.KeyboardModifier.ControlModifier,
            )
        )
        assert pasted == ["x" * 101]
        monkeypatch.setattr(widget, "handle_large_paste", original_handle_large_paste)

        widget.completer = lambda: SimpleNamespace(
            popup=lambda: SimpleNamespace(isVisible=lambda: True)
        )
        widget.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        )

        widget.handle_large_paste("paste me")
        assert widget.text().startswith("dialog:")

        widget.history_file = str(manager.history_file)
        widget.load_command_history()
        manager.history_file.unlink()
        widget.load_command_history()
        assert widget.history == []

        widget.add_to_command_history("echo test")
        widget.add_to_command_history("echo test")
        assert widget.history[-1] == "echo test"

        original_execute_api_call = widget.execute_api_call
        widget.execute_api_call = lambda command=None, endpoint=None: completed.append((command, endpoint))
        widget.input_mode = "ai"
        widget.execute_command("!whoami")
        widget.input_mode = "terminal"
        widget.execute_command("!pwd")
        assert completed[-2:] == [("whoami", "command"), ("pwd", "command")]
        widget.execute_api_call = original_execute_api_call

        widget.execute_command("   ")

        small_file = tmp_path / "small.txt"
        small_file.write_text("ok")
        widget.current_directory = str(tmp_path)
        widget.execute_command("cat small.txt")
        assert widget.terminal.writes[-1].endswith("\n")

        big_file = tmp_path / "big.txt"
        big_file.write_text("x" * (1024 * 1024 + 5))
        warnings = []
        monkeypatch.setattr(widget, "show_large_file_warning", lambda *_: warnings.append(True))
        widget.execute_command("cat big.txt")
        assert warnings == [True]

        widget.execute_command("cat missing.txt")
        widget.execute_command("ls -la")
        assert widget.terminal.commands

        ai_updates = []
        suggestion_updates = []
        widget.update_ai_notes.connect(ai_updates.append)
        widget.update_suggestions_notes.connect(suggestion_updates.append)
        widget.onTaskResult("command", "ai", "answer")
        widget.api_tasks = 1
        widget.onTaskResult("notes", "cmd", "note")
        assert ai_updates[-1] == "note"
        widget.api_tasks = 1
        widget.onTaskResult("suggestion", "cmd", "tip")
        assert suggestion_updates[-1] == "tip"

        widget.onTaskFinished()
        widget.onModelError("broken")
        monkeypatch.setattr(widget.threadpool, "start", lambda task: completed.append(task))
        widget.execute_api_call("help", "command")
        assert completed[-1] is widget.model_task

        widget.update_suggestion_notes_function("cmd", "data")
        widget.update_ai_notes_function("cmd", "data")

        menu = widget.createContextMenu()
        assert menu.actions()
        widget.setText("selected")
        widget.selectAll()
        widget.excludeWord()
        assert "selected" in Path(manager.privacy_file).read_text()
        widget.copy_selected_text()
        assert qapp.clipboard().text() == "selected"

        widget.setText("menu text")
        widget.selectAll()
        monkeypatch.setattr(widget, "selectedText", lambda: "menu text")
        widget.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(1, 1),
                QPointF(1, 1),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        positions = []
        monkeypatch.setattr(widget.contextMenu, "exec", lambda position: positions.append(position))
        widget.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(1, 1),
                QPointF(1, 1),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert positions
    finally:
        widget.close()


def test_agent_window_and_backend_edge_paths(qapp, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    llm = FakeLLM()
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_llm_instance",
        lambda model, ollama_url="", signals=None: (llm, "ollama"),
    )
    monkeypatch.setattr(terminal_emulator, "initialize_agent", lambda *args, **kwargs: FakeAgent())

    notes_runner = terminal_emulator.AgentTaskRunner(query="facts", endpoint="notes", manager=manager)
    suggestions_runner = terminal_emulator.AgentTaskRunner(
        query="next",
        endpoint="suggestion",
        manager=manager,
    )
    command_runner = terminal_emulator.AgentTaskRunner(query="ls", endpoint="command", manager=manager)
    assert notes_runner.query_llm("facts", "notes").startswith("reply:")
    assert suggestions_runner.query_llm("next", "suggestion").startswith("reply:")
    assert command_runner.query_llm("ls", "command").startswith("ran:")

    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QWidget { color: white; font-size: 10pt; }")
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"")
    monkeypatch.setattr(
        terminal_emulator,
        "return_path",
        lambda path: str(stylesheet if path.endswith(".css") else icon),
    )
    monkeypatch.setattr(terminal_emulator, "CommandInputArea", FakeWindowCommandInputArea)
    monkeypatch.setattr(
        terminal_emulator,
        "CentralDisplayAreaInMainWindow",
        FakeCentralDisplayArea,
    )
    monkeypatch.setattr(
        terminal_emulator.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )

    window = terminal_emulator.TerminalEmulatorWindow(
        manager=manager,
        terminal_emulator_number=3,
    )
    window.show()
    qapp.processEvents()

    try:
        with monkeypatch.context() as ctx:
            ctx.setattr(
                window,
                "get_file_path",
                lambda: (_ for _ in ()).throw(RuntimeError("picker failed")),
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

            def setNameFilter(self, value):
                self.name_filter = value

            def exec(self):
                return self.DialogCode.Accepted

            def selectedFiles(self):
                return [str(tmp_path / "picked.txt")]

        class RejectedFileDialog(AcceptedFileDialog):
            def exec(self):
                return 0

        monkeypatch.setattr(terminal_emulator, "QFileDialog", AcceptedFileDialog)
        assert window.get_file_path() == str(tmp_path / "picked.txt")
        monkeypatch.setattr(terminal_emulator, "QFileDialog", RejectedFileDialog)
        assert window.get_file_path() is None

        log_dir = Path(manager.config["LOG_DIRECTORY"])
        shutil_path = tmp_path / "copy-me.txt"
        shutil_path.write_text("copy")
        for file in log_dir.iterdir():
            file.unlink()
        log_dir.rmdir()
        window.process_file(str(shutil_path))
        assert (log_dir / "copy-me.txt").exists()
        window.process_file(str(shutil_path))

        class ExplodingSaveDialog(RejectedFileDialog):
            @staticmethod
            def getSaveFileName(*args, **kwargs):
                raise RuntimeError("save failed")

        monkeypatch.setattr(terminal_emulator, "QFileDialog", ExplodingSaveDialog)
        window.take_screenshot()
    finally:
        window.close()

    base_backend = terminal_emulator.BaseShellBackend()
    with pytest.raises(NotImplementedError):
        base_backend.start()
    with pytest.raises(NotImplementedError):
        base_backend.write("pwd")
    with pytest.raises(NotImplementedError):
        base_backend.stop()

    class RestartBackend(terminal_emulator.BaseShellBackend):
        def __init__(self):
            super().__init__()
            self.calls = []

        def start(self):
            self.calls.append("start")

        def write(self, data: str):
            self.calls.append(data)

        def stop(self):
            self.calls.append("stop")

    restart_backend = RestartBackend()
    restart_backend.restart()
    assert restart_backend.calls == ["stop", "start"]

    monkeypatch.setattr(terminal_emulator, "QProcess", FakeProcess)
    monkeypatch.setattr(
        terminal_emulator,
        "QProcessEnvironment",
        SimpleNamespace(systemEnvironment=lambda: FakeEnvironment()),
    )
    process_backend = terminal_emulator.QProcessShellBackend(lambda: ("bash", ["-i"], {"TERM": "xterm"}))
    process_backend.process._state = process_backend.process.ProcessState.Running
    process_backend.start()
    process_backend.process._state = process_backend.process.ProcessState.NotRunning
    process_backend.write("pwd\n")
    process_backend.stop()
    assert process_backend.process.writes == []
    assert process_backend.process.terminated is False

    monkeypatch.setattr(terminal_emulator, "QSocketNotifier", FakeNotifier)
    alive_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    alive_backend.child = FakeChild(alive=True)
    alive_backend.start()

    spawned_env = {}
    env_child = FakeChild(alive=True)
    with monkeypatch.context() as ctx:
        ctx.setattr(
            terminal_emulator.pexpect,
            "spawn",
            lambda *args, **kwargs: (spawned_env.update(kwargs["env"]) or env_child),
        )
        env_backend = terminal_emulator.PexpectShellBackend(
            lambda: ("bash", [], {"CUSTOM_ENV": "1"})
        )
        env_backend.start()
    assert spawned_env["CUSTOM_ENV"] == "1"

    spawn_errors = []
    with monkeypatch.context() as ctx:
        ctx.setattr(
            terminal_emulator.pexpect,
            "spawn",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
        )
        failing_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
        failing_backend.error.connect(spawn_errors.append)
        failing_backend.start()
        assert failing_backend.child is None
    assert spawn_errors[-1] == "spawn failed"

    inactive_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    inactive_backend.write("ignored")
    inactive_backend.child = FakeChild(alive=False)
    inactive_backend.write("ignored")

    write_errors = []
    failing_write_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    failing_write_backend.child = SimpleNamespace(
        isalive=lambda: True,
        write=lambda data: (_ for _ in ()).throw(RuntimeError("write failed")),
    )
    failing_write_backend.error.connect(write_errors.append)
    failing_write_backend.write("boom")
    assert write_errors[-1] == "write failed"

    stop_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    stop_backend._notifier = FakeNotifier(3, FakeNotifier.Type.Read)
    stop_backend.child = SimpleNamespace(
        terminate=lambda force=False: (_ for _ in ()).throw(RuntimeError("stop failed"))
    )
    stop_backend.stop()
    assert stop_backend.child is None

    no_child_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    no_child_backend._read_ready()

    read_errors = []
    exploding_child = FakeChild([RuntimeError("read failed")], alive=True)
    exploding_backend = terminal_emulator.PexpectShellBackend(lambda: ("bash", [], {}))
    exploding_backend.child = exploding_child
    exploding_backend.error.connect(read_errors.append)
    exploding_backend._read_ready()
    assert read_errors[-1] == "read failed"


def test_terminal_and_command_input_edge_paths(qapp, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(
        terminal_emulator,
        "ConversationMemory",
        lambda file_path: FakeMemory(),
    )
    monkeypatch.setattr(terminal_emulator, "QProcessShellBackend", FakeBackend)
    monkeypatch.setattr(terminal_emulator, "PexpectShellBackend", FakeBackend)

    monkeypatch.setenv("NEBULA_TERMINAL_BACKEND", "pexpect")
    pexpect_terminal = terminal_emulator.TerminalEmulator(manager=manager)
    assert pexpect_terminal.backend_mode == "pexpect"
    monkeypatch.delenv("NEBULA_TERMINAL_BACKEND", raising=False)

    terminal = terminal_emulator.TerminalEmulator(manager=manager)
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "is_linux_asking_for_password",
        lambda text: False,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "process_output",
        lambda text: "" if text == "empty" else text,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "is_included_command",
        lambda command, config: True,
    )
    logged_outputs = []
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "log_command_output",
        lambda command, output, config: logged_outputs.append((command, output)),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "run_hooks",
        lambda command, path: f"hooked:{command}",
        raising=False,
    )

    terminal._handle_backend_output("")
    terminal._handle_backend_output("empty")

    terminal.incognito_mode = True
    terminal.current_command = "ls"
    terminal.current_command_output = "result"
    terminal.check_for_prompt("nebula$")
    assert logged_outputs[-1][0] == "hooked:ls"

    terminal.current_command = "scan"
    terminal.current_command_output = ""
    terminal.check_for_prompt("partial output")
    assert terminal.current_command_output == "partial output"

    with monkeypatch.context() as ctx:
        ctx.setattr(
            terminal_emulator.re,
            "search",
            lambda *args, **kwargs: (_ for _ in ()).throw(re.error("regex failed")),
        )
        terminal.check_for_prompt("boom")

    with monkeypatch.context() as ctx:
        ctx.setattr(
            terminal_emulator.utilities,
            "process_output",
            lambda text: (_ for _ in ()).throw(RuntimeError("process failed")),
        )
        terminal.check_for_prompt("boom")

    terminal.backend = SimpleNamespace(
        restart=lambda: (_ for _ in ()).throw(RuntimeError("restart failed"))
    )
    terminal.reset_terminal()

    terminal.autonomous_mode = False
    terminal.update_current_command("simple")
    assert terminal.current_command == "simple"

    monkeypatch.delenv("NEBULA_TERMINAL_SHELL", raising=False)
    monkeypatch.delenv("SHELL", raising=False)
    terminal.backend_mode = "pexpect"
    shell_path, args, env = terminal._resolve_shell_command()
    assert shell_path == "/bin/bash"

    monkeypatch.setenv("NEBULA_TERMINAL_SHELL", "/bin/zsh")
    terminal.backend_mode = "qprocess"
    shell_path, args, env = terminal._resolve_shell_command()
    assert args == ["-d", "-f"]

    monkeypatch.setenv("NEBULA_TERMINAL_SHELL", "/bin/fish")
    shell_path, args, env = terminal._resolve_shell_command()
    assert env["PS1"] == "nebula> "

    monkeypatch.setattr(
        terminal_emulator.utilities,
        "get_shell_commands",
        lambda: ["cat", "ls"],
    )
    completer = terminal_emulator.DynamicCompleter()
    with monkeypatch.context() as ctx:
        ctx.setattr(
            completer,
            "completer_model",
            lambda text: (_ for _ in ()).throw(RuntimeError("model failed")),
        )
        completer.update_model("cat")
    assert completer.list_paths(str(tmp_path / "missing" / "file")) == []
    completer.stringListModel.setStringList(["alpha"])
    line_edit = QLineEdit()
    line_edit.setText("echo")
    completer.setWidget(line_edit)
    assert completer.pathFromIndex(completer.model().index(0, 0)) == "alpha"

    monkeypatch.setattr(terminal_emulator, "TerminalEmulator", FakeTerminal)
    monkeypatch.setattr(terminal_emulator.utilities, "EditCommandDialog", FakeDialog)
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "process_output",
        lambda text: f"processed:{text}",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "contains_only_spaces",
        lambda text: text.strip() == "",
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "escape_file_path",
        lambda path: path.replace(" ", "\\ "),
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "show_message",
        lambda title, message: None,
    )
    monkeypatch.setattr(
        terminal_emulator.utilities,
        "log_command_output",
        lambda command, result, config: None,
    )
    monkeypatch.setattr(
        terminal_emulator,
        "ConversationMemory",
        lambda file_path: FakeMemory(),
    )

    widget = terminal_emulator.CommandInputArea(manager=manager)
    widget.show()
    qapp.processEvents()

    try:
        with monkeypatch.context() as ctx:
            ctx.setattr(
                widget,
                "setEchoMode",
                lambda mode: (_ for _ in ()).throw(RuntimeError("echo failed")),
            )
            widget.set_password_mode(True)

        widget.history = ["one"]
        widget.history_index = 0
        widget.setText("one")
        widget.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        )
        assert widget.history_index == len(widget.history)
        assert widget.text() == ""

        qapp.clipboard().setText("short")
        small_paste_calls = []
        with monkeypatch.context() as ctx:
            ctx.setattr(
                terminal_emulator.QLineEdit,
                "keyPressEvent",
                lambda self, event: small_paste_calls.append(event.key()),
            )
            widget.keyPressEvent(
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_V,
                    Qt.KeyboardModifier.ControlModifier,
                )
            )
        assert small_paste_calls[-1] == Qt.Key.Key_V

        enter_calls = []
        widget.password_mode = True
        widget.setEchoMode(QLineEdit.EchoMode.Password)
        widget.completer = lambda: SimpleNamespace(
            popup=lambda: SimpleNamespace(isVisible=lambda: False)
        )
        with monkeypatch.context() as ctx:
            ctx.setattr(
                terminal_emulator.QLineEdit,
                "keyPressEvent",
                lambda self, event: enter_calls.append(event.key()),
            )
            widget.keyPressEvent(
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_Return,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
        assert enter_calls[-1] == Qt.Key.Key_Return
        assert widget.password_mode is False

        large_paste_calls = []
        with monkeypatch.context() as ctx:
            ctx.setattr(terminal_emulator.QLineEdit, "keyPressEvent", lambda self, event: None)
            ctx.setattr(widget, "text", lambda: "x" * 101)
            ctx.setattr(widget, "handle_large_paste", lambda text: large_paste_calls.append(text))
            widget.keyPressEvent(
                QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
            )
        assert large_paste_calls == ["x" * 101]

        with monkeypatch.context() as ctx:
            ctx.setattr(
                terminal_emulator.QLineEdit,
                "keyPressEvent",
                lambda self, event: (_ for _ in ()).throw(RuntimeError("keypress failed")),
            )
            widget.keyPressEvent(
                QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
            )

        with monkeypatch.context() as ctx:
            ctx.setattr(terminal_emulator.os.path, "exists", lambda path: True)
            ctx.setattr(
                builtins,
                "open",
                lambda *args, **kwargs: (_ for _ in ()).throw(IOError("read failed")),
            )
            assert widget.load_command_history() == []

        with monkeypatch.context() as ctx:
            ctx.setattr(terminal_emulator.os.path, "exists", lambda path: False)
            ctx.setattr(
                builtins,
                "open",
                lambda *args, **kwargs: (_ for _ in ()).throw(IOError("create failed")),
            )
            widget.load_command_history()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                builtins,
                "open",
                lambda *args, **kwargs: (_ for _ in ()).throw(IOError("write failed")),
            )
            widget.write_history_to_file("cmd", widget.CONFIG["HISTORY_FILE"])

        widget.command_input_area = SimpleNamespace(text=lambda: "pwd")
        widget.execute_command()

        widget.execute_command("cat")

        absolute_file = tmp_path / "absolute.txt"
        absolute_file.write_text("ok")
        widget.execute_command(f"cat {absolute_file}")

        with monkeypatch.context() as ctx:
            ctx.setattr(
                widget,
                "set_style_sheet",
                lambda data: (_ for _ in ()).throw(RuntimeError("style failed")),
            )
            widget.execute_command("ls")

        class FakeMessageBox:
            Icon = SimpleNamespace(Warning=1)

            def setIcon(self, icon):
                self.icon = icon

            def setText(self, text):
                self.text = text

            def setInformativeText(self, text):
                self.informative_text = text

            def setWindowTitle(self, title):
                self.title = title

            def setStyleSheet(self, stylesheet):
                self.stylesheet = stylesheet

            def exec(self):
                return 0

        monkeypatch.setattr(terminal_emulator, "QMessageBox", FakeMessageBox)
        widget.show_large_file_warning()

        with monkeypatch.context() as ctx:
            ctx.setattr(
                widget,
                "update_ai_notes_function",
                lambda *args: (_ for _ in ()).throw(TypeError("notes failed")),
            )
            widget.onTaskResult("notes", "cmd", "note")

        with monkeypatch.context() as ctx:
            ctx.setattr(
                widget,
                "update_suggestion_notes_function",
                lambda *args: (_ for _ in ()).throw(TypeError("suggestions failed")),
            )
            widget.onTaskResult("suggestion", "cmd", "tip")

        with monkeypatch.context() as ctx:
            ctx.setattr(
                terminal_emulator,
                "AgentTaskRunner",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model failed")),
            )
            widget.execute_api_call("help", "command")

        widget.setText("fallback text")
        monkeypatch.setattr(widget, "selectedText", lambda: "")
        widget.excludeWord()
        assert "fallback text" in Path(manager.privacy_file).read_text()
    finally:
        widget.close()
