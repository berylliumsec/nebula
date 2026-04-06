import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PyQt6.QtCore import QtMsgType

from nebula import log_config


class Recorder:
    def __init__(self):
        self.calls = []

    def log(self, level, message):
        self.calls.append((level, message))


def cleanup_logger(logger):
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_qt_message_handler_maps_known_levels(monkeypatch):
    recorder = Recorder()
    context = SimpleNamespace(file="widget.cpp", line=99)

    monkeypatch.setattr(log_config, "logger", recorder)

    log_config.qt_message_handler(QtMsgType.QtInfoMsg, context, "info")
    log_config.qt_message_handler(QtMsgType.QtWarningMsg, context, "warning")
    log_config.qt_message_handler(QtMsgType.QtCriticalMsg, context, "critical")
    log_config.qt_message_handler(QtMsgType.QtFatalMsg, context, "fatal")

    assert recorder.calls == [
        (
            logging.INFO,
            "QtMsgType: QtMsgType.QtInfoMsg, File: widget.cpp, Line: 99, Message: info",
        ),
        (
            logging.WARNING,
            "QtMsgType: QtMsgType.QtWarningMsg, File: widget.cpp, Line: 99, Message: warning",
        ),
        (
            logging.CRITICAL,
            "QtMsgType: QtMsgType.QtCriticalMsg, File: widget.cpp, Line: 99, Message: critical",
        ),
        (
            logging.FATAL,
            "QtMsgType: QtMsgType.QtFatalMsg, File: widget.cpp, Line: 99, Message: fatal",
        ),
    ]


def test_qt_message_handler_defaults_to_debug_and_unknown_context(monkeypatch):
    recorder = Recorder()
    context = SimpleNamespace(file=None, line=None)

    monkeypatch.setattr(log_config, "logger", recorder)

    log_config.qt_message_handler(object(), context, "debug")

    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == logging.DEBUG
    assert recorder.calls[0][1].startswith("QtMsgType: <object object at ")
    assert "File: Unknown, Line: Unknown, Message: debug" in recorder.calls[0][1]


def test_create_log_directory_creates_missing_directory(tmp_path):
    log_dir = tmp_path / "logs"

    log_config.create_log_directory(log_dir)
    log_config.create_log_directory(log_dir)

    assert log_dir.exists()
    assert log_dir.is_dir()


def test_setup_logging_creates_single_rotating_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    log_name = f"unit-{uuid4().hex}.log"

    logger = log_config.setup_logging(log_name, level=logging.INFO, max_size=64, backup_count=1)
    logger_again = log_config.setup_logging(log_name, level=logging.INFO, max_size=64, backup_count=1)

    try:
        assert logger is logger_again
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert Path(logger.handlers[0].baseFilename) == (
            tmp_path / ".local" / "share" / "nebula" / "logs" / log_name
        )
    finally:
        cleanup_logger(logger)
