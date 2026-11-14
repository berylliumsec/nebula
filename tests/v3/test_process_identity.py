"""Real disposable OS processes verify birth identity and safe signaling."""

import os
import subprocess
import sys

import pytest

from nebula.v3.process_identity import (
    birth_boot,
    origin_from_previous_boot,
    process_birth,
    same_process,
    signal_process,
)


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


def test_only_a_different_kernel_boot_proves_recorded_processes_ended():
    earlier = "linux:00000000-1111-4222-8333-444444444444:pidns:4026531836"
    current = "linux:a6aeca01-620e-45c4-a209-05319d532780:pidns:4026531836"
    assert origin_from_previous_boot(earlier, current)
    assert not origin_from_previous_boot(current, current)
    # The same boot in another PID namespace can still have live workers.
    other_namespace = current.replace("4026531836", "4026599999")
    assert not origin_from_previous_boot(current, other_namespace)
    assert not origin_from_previous_boot(other_namespace, current)
    # A container gets a new PID namespace on every start; the boot decides.
    assert origin_from_previous_boot(earlier, other_namespace)
    for unreadable in (None, "", "darwin:1:2", "linux::pidns:1", "linux:b:pidns:x"):
        assert not origin_from_previous_boot(unreadable, current)
        assert not origin_from_previous_boot(earlier, unreadable)
    assert birth_boot("linux:a6aeca01:12345") == "a6aeca01"
    for unreadable in (None, "pending", "darwin:1:2", "linux:a6aeca01:x"):
        assert birth_boot(unreadable) is None
