import json
import os
import re
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QTextEdit

from nebula import utilities


class Emitter:
    def __init__(self):
        self.values = []

    def emit(self, value=None):
        self.values.append(value)


class FakeEncoding:
    def __init__(self, prefix):
        self.prefix = prefix

    def encode(self, text):
        return [f"{self.prefix}:{part}" for part in text.split()]

    def decode(self, tokens):
        return "|".join(tokens)


def test_long_press_button_signals(qapp):
    button = utilities.LongPressButton("Run")
    clicked = []
    long_pressed = []
    progress = []
    button.clicked.connect(lambda: clicked.append(True))
    button.longPressed.connect(lambda: long_pressed.append(True))
    button.longPressProgress.connect(progress.append)

    button.onStartPress()
    button.onEndPress()
    button.onLongPress()

    assert progress == [True, False, False]
    assert clicked == [True]
    assert long_pressed == [True]


def test_edit_command_dialog_modes(qapp):
    regular = utilities.EditCommandDialog("echo test")
    assistant = utilities.EditCommandDialog(
        "original command",
        command_input_area=SimpleNamespace(),
    )

    try:
        regular.user_input_edit.setText("ls -la")
        assistant.user_input_edit.setText("show ports")

        assert regular.get_command() == "ls -la"
        assert assistant.get_command() == "!show ports: original command"
        assert assistant.command_display.isReadOnly() is True
    finally:
        regular.close()
        assistant.close()


def test_set_terminal_size_packs_expected_values(monkeypatch):
    calls = []
    monkeypatch.setattr(utilities.fcntl, "ioctl", lambda fd, op, packed: calls.append((fd, op, packed)))

    utilities.set_terminal_size(9, 40, 80)

    assert calls[0][0] == 9
    assert calls[0][1] == utilities.termios.TIOCSWINSZ
    assert struct.unpack("HHHH", calls[0][2]) == (40, 80, 1180, 700)


def test_open_url_invokes_xdg_open(monkeypatch):
    calls = []
    monkeypatch.setattr(utilities.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))

    utilities.open_url("https://example.com")

    assert calls == [
        (
            ["xdg-open", "https://example.com"],
            {
                "stdout": utilities.subprocess.DEVNULL,
                "stderr": utilities.subprocess.DEVNULL,
            },
        )
    ]


def test_show_last_line_returns_last_block_text(qapp):
    editor = QTextEdit()
    try:
        editor.setPlainText("first\nsecond")
        assert utilities.show_last_line(editor.document()) == "second"
    finally:
        editor.close()


def test_strip_ansi_codes_handles_strings_bytes_and_errors(monkeypatch):
    assert utilities.strip_ansi_codes("hello\x1b[31m world") == "hello world"
    assert utilities.strip_ansi_codes(b"byte\x1b[0m text") == "byte text"

    messages = []
    monkeypatch.setattr(utilities.logger, "debug", messages.append)
    monkeypatch.setattr(utilities.re, "compile", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("compile failed")))

    assert utilities.strip_ansi_codes("raw") == "raw"
    assert messages and "compile failed" in messages[0]


def test_get_llm_instance_selects_openai(monkeypatch):
    created = []

    class FakeChatOpenAI:
        def __init__(self, model_name):
            created.append(model_name)

    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setattr(utilities, "ChatOpenAI", FakeChatOpenAI)

    llm, backend = utilities.get_llm_instance("gpt-demo")

    assert isinstance(llm, FakeChatOpenAI)
    assert backend == "openai"
    assert created == ["gpt-demo"]


def test_get_llm_instance_selects_ollama_and_emits_errors(monkeypatch):
    created = []

    class FakeChatOllama:
        def __init__(self, model, base_url=None):
            created.append((model, base_url))

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(utilities, "ChatOllama", FakeChatOllama)

    llm, backend = utilities.get_llm_instance("mistral", ollama_url="http://ollama")

    assert isinstance(llm, FakeChatOllama)
    assert backend == "openai"
    assert created == [("mistral", "http://ollama")]

    signals = SimpleNamespace(error=Emitter())
    monkeypatch.setattr(
        utilities,
        "ChatOllama",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ollama failed")),
    )

    with pytest.raises(RuntimeError, match="ollama failed"):
        utilities.get_llm_instance("mistral", signals=signals)

    assert signals.error.values == ["ollama failed"]


def test_show_message_uses_message_box(monkeypatch):
    created = []

    class FakeMessageBox:
        def __init__(self):
            self.window_title = None
            self.text = None
            self.stylesheet = None
            self.executed = False
            created.append(self)

        def setWindowTitle(self, title):
            self.window_title = title

        def setText(self, text):
            self.text = text

        def setStyleSheet(self, stylesheet):
            self.stylesheet = stylesheet

        def exec(self):
            self.executed = True

    monkeypatch.setattr(utilities, "QMessageBox", FakeMessageBox)

    utilities.show_message("Title", "Body")

    assert created[0].window_title == "Title"
    assert created[0].text == "Body"
    assert created[0].stylesheet == utilities.DARK_STYLE_SHEET
    assert created[0].executed is True


def test_is_included_command_and_password_detection(monkeypatch):
    assert utilities.is_included_command("nmap -sV", {"SELECTED_TOOLS": ["nmap"]}) is True
    assert utilities.is_included_command("ls -la", {"SELECTED_TOOLS": ["nmap"]}) is False
    assert utilities.is_included_command("pwd", {"SELECTED_TOOLS": []}) is False

    class BadConfig(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("bad config")

    assert utilities.is_included_command("pwd", BadConfig()) is False
    assert utilities.is_linux_asking_for_password("Password for user") is True
    assert utilities.is_linux_asking_for_password(b"login: root's password") is True
    assert utilities.is_linux_asking_for_password(b"\xff") is None

    monkeypatch.setattr(utilities.re, "search", lambda *args, **kwargs: (_ for _ in ()).throw(re.error("bad regex")))
    assert utilities.is_linux_asking_for_password("password:") is None


def test_log_command_output_and_filename_creation(tmp_path, monkeypatch):
    original_create_filename = utilities.create_filename_from_command
    messages = []
    monkeypatch.setattr(utilities.logger, "warning", messages.append)
    monkeypatch.setattr(utilities.logger, "error", messages.append)

    utilities.log_command_output("nmap -sV", "   ", {"LOG_DIRECTORY": str(tmp_path)})
    assert messages == ["Current command output is empty. Nothing to write."]

    existing = tmp_path / "scan_20240101000000"
    existing.write_text("old\n")
    monkeypatch.setattr(utilities, "create_filename_from_command", lambda command: "scan_20240101000000")

    utilities.log_command_output("nmap -sV", "result", {"LOG_DIRECTORY": str(tmp_path)})

    assert (tmp_path / "scan_20240101000000(1)").read_text() == "result\n"

    utilities.log_command_output("nmap -sV", "result", {"LOG_DIRECTORY": str(tmp_path)})
    assert (tmp_path / "scan_20240101000000(2)").read_text() == "result\n"

    monkeypatch.setattr(utilities, "create_filename_from_command", lambda command: None)
    utilities.log_command_output("nmap -sV", "result", {"LOG_DIRECTORY": str(tmp_path)})
    assert any("Failed to create a valid filename." in str(message) for message in messages)

    monkeypatch.setattr(utilities, "create_filename_from_command", lambda command: "broken")
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    utilities.log_command_output("nmap -sV", "result", {"LOG_DIRECTORY": str(tmp_path)})
    assert any("Error writing command output to file: disk full" in str(message) for message in messages)

    assert original_create_filename("nmap -sV").startswith("nmap_")
    assert original_create_filename(None) is None


def test_process_output_and_shell_commands(monkeypatch):
    processed = utilities.process_output("name\tvalue\r\nab\b")
    assert "\t" not in processed
    assert "\r" not in processed

    monkeypatch.setattr(utilities, "strip_ansi_codes", lambda data: (_ for _ in ()).throw(RuntimeError("bad output")))
    assert utilities.process_output("data") is None

    monkeypatch.setattr(utilities.subprocess, "check_output", lambda *args, **kwargs: b"pwd\nls\n")
    assert utilities.get_shell_commands() == ["ls", "pwd"]

    monkeypatch.setattr(
        utilities.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no shell")),
    )
    assert utilities.get_shell_commands() == []


def test_xml_type_detection_helpers(tmp_path, monkeypatch):
    nessus = tmp_path / "scan.nessus"
    zap = tmp_path / "scan.zap"
    nmap = tmp_path / "scan.nmap"
    nikto = tmp_path / "scan.nikto"
    bad = tmp_path / "bad.xml"

    nessus.write_text("<NessusClientData_v2></NessusClientData_v2>")
    zap.write_text("<OWASPZAPReport></OWASPZAPReport>")
    nmap.write_text("<nmaprun></nmaprun>")
    nikto.write_text("<niktoscan></niktoscan>")
    bad.write_text("<broken")

    assert utilities.is_nessus_file(str(nessus)) is True
    assert utilities.is_zap_file(str(zap)) is True
    assert utilities.is_nmap_file(str(nmap)) is True
    assert utilities.is_nikto_file(str(nikto)) is True

    assert utilities.is_nessus_file(str(bad)) is False
    assert utilities.is_zap_file(str(bad)) is False
    assert utilities.is_nmap_file(str(bad)) is False
    assert utilities.is_nikto_file(str(bad)) is False

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: (_ for _ in ()).throw(RuntimeError("parse failed")),
    )
    assert utilities.is_nessus_file("missing") is False
    assert utilities.is_zap_file("missing") is False
    assert utilities.is_nmap_file("missing") is False
    assert utilities.is_nikto_file("missing") is False


def test_parse_nmap_nessus_zap_and_nikto_files(tmp_path, monkeypatch):
    nmap_file = tmp_path / "report.xml"
    nmap_file.write_text(
        "<nmaprun><host><ports>"
        "<port><script id='one' output='value&#xa;line2'/></port>"
        "<port><script id='two' output='ERROR: Script execution failed (use -d to debug)'/></port>"
        "<port><script id='three'/></port>"
        "</ports></host></nmaprun>"
    )
    assert "ID: one" in utilities.parse_nmap(str(nmap_file))
    assert utilities.parse_nmap("missing.xml") is None

    nessus_file = tmp_path / "report.nessus"
    nessus_file.write_text(
        "<NessusClientData_v2><Report><ReportItem pluginName='Interesting' port='443'>"
        "<description>desc</description><cve>CVE-1</cve>"
        "</ReportItem>"
        "<ReportItem pluginName='Interesting' port='8080'><description>other</description></ReportItem>"
        "<ReportItem pluginName='Nessus Scan Information'><description>skip</description></ReportItem>"
        "</Report></NessusClientData_v2>"
    )
    parsed_nessus = utilities.parse_nessus_file(str(nessus_file))
    assert "CVE: CVE-1" in parsed_nessus
    assert "Port: 8080" in parsed_nessus
    assert utilities.parse_nessus_file(str(tmp_path / "broken.nessus")) is None

    zap_file = tmp_path / "zap.xml"
    zap_file.write_text(
        "<OWASPZAPReport><site><alerts><alertitem><desc>desc</desc></alertitem></alerts></site></OWASPZAPReport>"
    )
    assert utilities.parse_zap(str(zap_file)) == "desc"
    assert utilities.parse_zap(str(tmp_path / 'missing.zap')) == ""

    nikto_file = tmp_path / "nikto.xml"
    nikto_file.write_text(
        "<niktoscan><scandetails><item><description> issue one </description></item>"
        "<item><description>issue two</description></item></scandetails></niktoscan>"
    )
    assert utilities.parse_nikto_xml(str(nikto_file)) == "issue one\n\nissue two"

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: (_ for _ in ()).throw(RuntimeError("xml failed")),
    )
    assert utilities.parse_zap("broken.xml") == ""
    assert utilities.parse_nikto_xml("broken.xml") == ""


def test_process_text_token_helpers_and_initial_help(tmp_path, monkeypatch):
    text = "Use /tmp/report.txt and ```code block``` safely"
    assert utilities.process_text(text) == text
    assert utilities.process_text("") == ""
    assert utilities.process_text(None) is None

    monkeypatch.setattr(
        utilities.re,
        "compile",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("regex failed")),
    )
    assert utilities.process_text("fallback text") == "fallback text"

    monkeypatch.setattr(utilities.tiktoken, "get_encoding", lambda name: FakeEncoding(name))
    monkeypatch.setattr(utilities.tiktoken, "encoding_for_model", lambda name: FakeEncoding(f"model:{name}"))

    assert isinstance(utilities.encoding_getter("cl100k_base"), FakeEncoding)
    assert isinstance(utilities.encoding_getter("gpt-4"), FakeEncoding)
    assert utilities.tokenizer("hello world", "cl100k_base") == [
        "cl100k_base:hello",
        "cl100k_base:world",
    ]
    assert utilities.token_counter("hello world", "cl100k_base") == 2

    initial_help = tmp_path / "initial_help.ini"
    assert utilities.check_initial_help(str(initial_help)) is False
    assert initial_help.exists()

    initial_help.write_text("[Settings]\ninitialhelpshown = True\n")
    assert utilities.check_initial_help(str(initial_help)) is True

    initial_help.write_text("[Settings]\ninitialhelpshown = False\n")
    assert utilities.check_initial_help(str(initial_help)) is False


def test_misc_utility_helpers(monkeypatch):
    assert utilities.contains_escape_sequences("\x1b[31mred") is True
    assert utilities.contains_escape_sequences("plain") is False
    assert utilities.contains_only_spaces("   ") is True
    assert utilities.contains_only_spaces("#") is True
    assert utilities.contains_only_spaces("value") is False
    assert utilities.escape_file_path("two words.txt") == "'two words.txt'"
    assert utilities.escape_file_path(123) is None

    monkeypatch.setattr(utilities.sys, "frozen", False, raising=False)
    monkeypatch.delattr(utilities.sys, "_MEIPASS", raising=False)
    assert utilities.resource_path("demo.txt").endswith("/src/nebula/demo.txt")

    monkeypatch.setattr(utilities.sys, "frozen", True, raising=False)
    monkeypatch.setattr(utilities.sys, "_MEIPASS", "/tmp/bundle", raising=False)
    assert utilities.resource_path("demo.txt") == "/tmp/bundle/demo.txt"

    monkeypatch.setenv("IN_DOCKER", "1")
    assert utilities.is_run_as_package() is False

    monkeypatch.delenv("IN_DOCKER", raising=False)
    monkeypatch.setattr(
        utilities.os.path,
        "abspath",
        lambda _: "/tmp/site-packages/nebula/utilities.py",
    )
    assert utilities.is_run_as_package() is True


def test_additional_utility_error_branches(monkeypatch):
    assert utilities.strip_ansi_codes(123) == "123"
    assert utilities.process_output(".\b") == ""

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: (_ for _ in ()).throw(utilities.ET.ParseError("bad xml")),
    )
    assert utilities.parse_nessus_file("broken.nessus") is None
    assert utilities.parse_zap("broken.zap") == ""

    class BrokenReportItem:
        def get(self, key, default=None):
            values = {"pluginName": "Interesting", "port": "443"}
            return values.get(key, default)

        def find(self, name):
            return SimpleNamespace(text="desc") if name == "description" else None

        def findall(self, name):
            raise RuntimeError("cve failed")

    class BrokenNessusRoot:
        def findall(self, _path):
            return [BrokenReportItem()]

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: SimpleNamespace(getroot=lambda: BrokenNessusRoot()),
    )
    assert utilities.parse_nessus_file("processing-error.nessus") is None

    class SkippedReportItem:
        def get(self, key, default=None):
            values = {"pluginName": "Nessus Scan Information", "port": "0"}
            return values.get(key, default)

        def find(self, _name):
            return None

        def findall(self, _name):
            return []

    class SkippedRoot:
        def findall(self, _path):
            return [SkippedReportItem()]

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: SimpleNamespace(getroot=lambda: SkippedRoot()),
    )
    assert utilities.parse_nessus_file("no-findings.nessus") is None

    class BrokenZapRoot:
        def findall(self, _path):
            raise RuntimeError("desc failed")

    monkeypatch.setattr(
        utilities.ET,
        "parse",
        lambda path: SimpleNamespace(getroot=lambda: BrokenZapRoot()),
    )
    assert utilities.parse_zap("processing-error.zap") == ""
