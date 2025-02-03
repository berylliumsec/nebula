import logging
import os
from logging.handlers import RotatingFileHandler

from PyQt6.QtCore import QtMsgType, qInstallMessageHandler


def qt_message_handler(mode, context, message):
    """
    Custom Qt message handler to redirect Qt logs to Python logging.
    :param mode: Message type (e.g., QtMsgType.Info, QtMsgType.Warning, etc.)
    :param context: Message context, containing file, line, function, etc.
    :param message: The actual message
    """
    if mode == QtMsgType.QtInfoMsg:
        level = logging.INFO
    elif mode == QtMsgType.QtWarningMsg:
        level = logging.WARNING
    elif mode == QtMsgType.QtCriticalMsg:
        level = logging.CRITICAL
    elif mode == QtMsgType.QtFatalMsg:
        level = logging.FATAL
    else:
        level = logging.DEBUG
    file = context.file if context.file else "Unknown"
    line = context.line if context.line else "Unknown"

    logger.log(
        level, f"QtMsgType: {mode}, File: {file}, Line: {line}, Message: {message}"
    )


def create_log_directory(log_dir):
    """
    Creates a log directory if it doesn't exist.
    :param log_dir: Path to the log directory.
    """
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)


def setup_logging(log_file, level=logging.DEBUG, max_size=10485760, backup_count=3):
    """
    Sets up the logging configuration to log to a file.
    :param log_file: Name of the log file.
    :param level: Logging level, e.g., logging.INFO, logging.DEBUG.
    """
    home_dir = os.environ["HOME"]
    log_dir = os.path.join(home_dir, ".local", "share", "nebula", "logs")
    create_log_directory(log_dir)
    log_file_path = os.path.join(log_dir, log_file)
    logger = logging.getLogger(log_file)
    logger.setLevel(level)
    if not logger.handlers:
        file_handler = RotatingFileHandler(
            log_file_path, maxBytes=max_size, backupCount=backup_count
        )
        file_handler.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


logger = setup_logging("qt_errors.log")
qInstallMessageHandler(qt_message_handler)
