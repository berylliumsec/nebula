import ast
import sys

from nebula import run_python


class FakeResult:
    def __init__(self, error_in_exec=None):
        self.error_in_exec = error_in_exec


class FakeShell:
    def __init__(self, result, action=None):
        self.result = result
        self.action = action

    def run_cell(self, script):
        if self.action is not None:
            self.action(script)
        return self.result


def test_interactive_script_detector_flags_name_calls():
    detector = run_python.InteractiveScriptDetector()
    detector.visit(ast.parse("input('name?')"))

    assert detector.interactive_detected is True


def test_detect_interactive_script_flags_attribute_calls():
    assert run_python.detect_interactive_script("terminal.input('name?')") is True


def test_detect_interactive_script_returns_false_for_regular_code():
    assert run_python.detect_interactive_script("value = 1 + 1") is False


def test_detect_interactive_script_logs_syntax_errors(monkeypatch):
    messages = []

    monkeypatch.setattr(run_python.logger, "error", messages.append)

    assert run_python.detect_interactive_script("if True print('broken')") is False
    assert messages and "Syntax error while parsing the script" in messages[0]


def test_execute_python_script_rejects_interactive_code(monkeypatch):
    monkeypatch.setattr(run_python, "detect_interactive_script", lambda script: True)

    output = run_python.execute_python_script("input('blocked')")

    assert (
        output
        == "Interactive scripts are not supported. Please use the terminal instead."
    )


def test_execute_python_script_captures_stdout(monkeypatch):
    monkeypatch.setattr(run_python, "detect_interactive_script", lambda script: False)
    stdout_before = sys.stdout
    stderr_before = sys.stderr

    def action(script):
        print(f"ran:{script}")

    monkeypatch.setattr(
        run_python.InteractiveShell,
        "instance",
        lambda: FakeShell(FakeResult(), action),
    )

    output = run_python.execute_python_script("print('ok')")

    assert output == "ran:print('ok')\n"
    assert sys.stdout is stdout_before
    assert sys.stderr is stderr_before


def test_execute_python_script_returns_stderr_when_ipython_reports_exec_error(
    monkeypatch,
):
    monkeypatch.setattr(run_python, "detect_interactive_script", lambda script: False)

    def action(script):
        sys.stderr.write("boom on stderr")

    monkeypatch.setattr(
        run_python.InteractiveShell,
        "instance",
        lambda: FakeShell(FakeResult(ValueError("boom")), action),
    )

    output = run_python.execute_python_script("raise ValueError('boom')")

    assert output == "boom on stderr"


def test_execute_python_script_reports_unexpected_exceptions(monkeypatch):
    monkeypatch.setattr(run_python, "detect_interactive_script", lambda script: False)
    stdout_before = sys.stdout
    stderr_before = sys.stderr

    def action(script):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(
        run_python.InteractiveShell,
        "instance",
        lambda: FakeShell(FakeResult(), action),
    )

    output = run_python.execute_python_script("print('ok')")

    assert "An exception occurred: executor exploded" in output
    assert "RuntimeError: executor exploded" in output
    assert sys.stdout is stdout_before
    assert sys.stderr is stderr_before


def test_execute_script_in_thread_returns_future(monkeypatch):
    monkeypatch.setattr(
        run_python, "execute_python_script", lambda script: f"completed:{script}"
    )

    future = run_python.execute_script_in_thread("print('threaded')")

    assert future.result() == "completed:print('threaded')"
