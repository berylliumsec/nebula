"""Importable, inert jobs used only by the process-worker regression tests."""

import os
from pathlib import Path
import time


def identify(value=None):
    return os.getpid(), value


def hold(release_path, started_path):
    Path(started_path).write_text(str(os.getpid()))
    deadline = time.monotonic() + 20
    while not Path(release_path).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("synthetic CPU job was not released")
        time.sleep(0.01)
    return os.getpid()


def write_marker(path):
    Path(path).write_text("executed")
    return os.getpid()


def fail():
    raise ValueError("synthetic payload text must stay inside the worker")


def exit_worker(marker):
    Path(marker).write_text("one execution")
    os._exit(23)


def no_startup_ack(connection):
    time.sleep(20)


def blocked_pdf(snapshot, font_root, expected_hashes):
    release, started = snapshot["report"]["executive_summary"].split("|")
    hold(release, started)
    return b"discarded synthetic PDF result", []
