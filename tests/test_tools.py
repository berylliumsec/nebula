import subprocess

from nebula.tools.searchsploit import SearchInput, searchsploit
from nebula.tools.terminal import TerminalCommandInput, run_terminal_command


def test_terminal_tool_metadata_and_success(monkeypatch):
    commands = []

    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kwargs: commands.append((command, kwargs)) or "ok\n",
    )

    assert TerminalCommandInput(command="pwd").command == "pwd"
    assert run_terminal_command.name == "terminal"
    assert run_terminal_command.func("pwd") == "ok\n"
    assert commands == [
        (
            "pwd",
            {"shell": True, "stderr": subprocess.STDOUT, "text": True},
        )
    ]


def test_terminal_tool_returns_error_output(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "ls", output="failure")
        ),
    )

    assert run_terminal_command.func("ls") == "Error executing command: failure"


def test_searchsploit_tool_metadata_and_success(monkeypatch):
    commands = []

    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kwargs: commands.append((command, kwargs)) or "matches",
    )

    assert SearchInput(query="apache 2.4").query == "apache 2.4"
    assert searchsploit.name == "searchsploit-tool"
    assert searchsploit.func("apache 2.4") == "matches"
    assert commands == [
        (
            ["searchsploit", "apache 2.4"],
            {"stderr": subprocess.STDOUT, "text": True},
        )
    ]


def test_searchsploit_tool_returns_error_output(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(
                1, ["searchsploit", "apache"], output="not installed"
            )
        ),
    )

    assert (
        searchsploit.func("apache")
        == "Error running searchsploit: not installed"
    )
