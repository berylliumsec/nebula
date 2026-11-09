"""Real disposable OS processes verify birth identity and safe signaling."""

import os
import subprocess
import sys

import pytest

from nebula.v3.process_identity import process_birth, same_process, signal_process


def test_current_process_birth_is_stable_and_distinguishes_another_incarnation():
    birth = process_birth(os.getpid())
    assert birth and process_birth(os.getpid()) == birth
    assert same_process(os.getpid(), birth)
    assert not same_process(os.getpid(), "a different process incarnation")


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="pidfd signaling is a Linux kernel contract",
)
def test_pidfd_signal_cannot_target_a_process_with_the_wrong_birth():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        birth = process_birth(process.pid)
        assert birth is not None
        assert signal_process(process.pid, "different-birth") is False
        assert process.poll() is None
        assert signal_process(process.pid, birth) is True
        process.wait(timeout=5)
        assert not same_process(process.pid, birth)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_process_identity_rejects_non_process_values():
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            process_birth(value)
