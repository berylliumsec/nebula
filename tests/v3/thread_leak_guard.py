"""Opt-in session-end guard for suites that own async database worker threads."""

import sys
import os
from pathlib import Path
import threading
import time
import traceback

import pytest


def new_non_daemon_threads(baseline, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        leaked = [
            thread
            for thread in threading.enumerate()
            if thread is not threading.main_thread()
            and not thread.daemon
            and thread not in baseline
        ]
        if not leaked or time.monotonic() >= deadline:
            return leaked
        for thread in leaked:
            thread.join(timeout=max(0, min(0.05, deadline - time.monotonic())))


def pytest_configure(config):
    config._nebula_thread_baseline = set(threading.enumerate())


def pytest_sessionfinish(session, exitstatus):
    leaked = new_non_daemon_threads(session.config._nebula_thread_baseline)
    if not leaked:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    frames = sys._current_frames()
    for thread in leaked:
        detail = (
            "".join(traceback.format_stack(frames[thread.ident]))
            if thread.ident in frames
            else "No Python stack available"
        )
        message = f"Leaked non-daemon thread: {thread.name}\n{detail}"
        if reporter is not None:
            reporter.write_sep("!", message, red=True)
        else:
            sys.stderr.write(message + "\n")

    marker = os.environ.get("NEBULA_TEST_THREAD_LEAK_MARKER")
    if marker:
        Path(marker).write_text("leaked non-daemon test threads\n")
