"""Thread checks report genuine leaks without leaving the test process blocked."""

import threading

from tests.v3.thread_leak_guard import new_non_daemon_threads


def test_reports_only_new_non_daemon_threads_and_joins_cleanup():
    baseline = set(threading.enumerate())
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="synthetic-owned-thread")
    thread.start()
    try:
        assert new_non_daemon_threads(baseline, timeout=0.01) == [thread]
        assert new_non_daemon_threads(baseline | {thread}, timeout=0) == []
    finally:
        release.set()
        thread.join(timeout=2)
    assert new_non_daemon_threads(baseline, timeout=0) == []


def test_daemon_threads_do_not_block_interpreter_exit():
    baseline = set(threading.enumerate())
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    try:
        assert new_non_daemon_threads(baseline, timeout=0) == []
    finally:
        release.set()
        thread.join(timeout=2)


def test_session_hook_reports_leak_and_subprocess_exits_after_owner_cleanup(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    test = tmp_path / "test_synthetic_thread.py"
    test.write_text(
        "import threading\n"
        "def test_starts_owned_thread():\n"
        "    threading.Thread(target=lambda: threading.Event().wait(), "
        "name='synthetic-leak').start()\n"
    )
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_pytest_thread_guard.py",
            "--termination-grace",
            "1",
            "--",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.v3.thread_leak_guard",
            "--junitxml",
            str(tmp_path / "result.xml"),
            str(test),
        ],
        cwd=repository,
        env={**os.environ, "PYTHONPATH": str(repository)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Leaked non-daemon thread: synthetic-leak" in result.stdout
    assert (tmp_path / "result.xml").is_file()
    assert "stopping its owned process group" in result.stderr
