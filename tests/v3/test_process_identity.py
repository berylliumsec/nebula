"""Real disposable OS processes verify birth identity and safe signaling."""

import os
import signal
import subprocess
import sys
import time

import pytest

from nebula.v3 import process_identity
from nebula.v3.process_identity import (
    birth_boot,
    kill_process_session,
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


_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads Linux /proc births"
)


def forget_pidfd_probe():
    probe = getattr(process_identity, "pidfd_supported", None)
    if probe is not None:
        probe.cache_clear()


@pytest.fixture
def without_pidfd(monkeypatch):
    """Run as a Python built without os.pidfd_open (macOS, some 3.11 builds)."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    forget_pidfd_probe()
    yield
    monkeypatch.undo()
    forget_pidfd_probe()


def _sleeper(code="import time; time.sleep(60)"):
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def _end(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _earlier_incarnation(birth: str) -> str:
    """The birth an earlier process had at the same PID before it was reused."""
    prefix, _, start = birth.rpartition(":")
    return f"{prefix}:{int(start) - 1}"


def test_pidfd_support_probe_rejects_missing_or_refused_pidfd(monkeypatch):
    forget_pidfd_probe()
    try:
        monkeypatch.delattr(os, "pidfd_open", raising=False)
        assert process_identity.pidfd_supported() is False
        forget_pidfd_probe()
        monkeypatch.undo()
        if not hasattr(os, "pidfd_open"):
            return

        def refused(pid):
            raise PermissionError("seccomp")

        monkeypatch.setattr(os, "pidfd_open", refused)
        assert process_identity.pidfd_supported() is False
    finally:
        monkeypatch.undo()
        forget_pidfd_probe()


@_linux_only
def test_reused_pid_is_never_signalled_without_pidfd(without_pidfd, monkeypatch):
    process = _sleeper()
    sent = []
    real_kill = os.kill
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: (sent.append((pid, sig)), real_kill(pid, sig))
    )
    try:
        birth = process_birth(process.pid)
        assert birth is not None
        # The PID now names a newer incarnation than the one recorded.
        recorded = _earlier_incarnation(birth)
        assert not same_process(process.pid, recorded)
        assert signal_process(process.pid, recorded, signal.SIGKILL) is False
        assert kill_process_session(process.pid, recorded) == 0
        assert sent == [] and process.poll() is None
        # The recorded incarnation itself is signalled.
        assert signal_process(process.pid, birth, 0) is True
        assert signal_process(process.pid, birth, signal.SIGKILL) is True
        assert process.wait(timeout=5) == -signal.SIGKILL
        # Exited and reaped: nothing left to signal, and no error.
        assert signal_process(process.pid, birth, signal.SIGKILL) is False
        assert kill_process_session(process.pid, birth) == 0
    finally:
        _end(process)


@_linux_only
def test_frozen_session_is_killed_without_pidfd_only_when_birth_matches(
    without_pidfd, tmp_path
):
    marker = tmp_path / "grandchild.pid"
    process = _sleeper(
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '300'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(300)\n"
    )
    try:
        for _ in range(500):
            if marker.exists() and marker.read_text():
                break
            time.sleep(0.01)
        grandchild = int(marker.read_text())
        grandchild_birth = process_birth(grandchild)
        birth = process_birth(process.pid)
        assert birth and grandchild_birth
        os.kill(process.pid, signal.SIGSTOP)
        assert kill_process_session(process.pid, "linux:other:1") == 0
        assert same_process(grandchild, grandchild_birth)
        assert kill_process_session(process.pid, birth) == 2
        assert process.wait(timeout=5) == -signal.SIGKILL
        deadline = time.monotonic() + 5
        while same_process(grandchild, grandchild_birth):
            assert time.monotonic() < deadline, "the session member survived"
            time.sleep(0.02)
    finally:
        _end(process)


def test_session_kill_refuses_this_process_and_its_session(without_pidfd):
    birth = process_birth(os.getpid())
    assert birth is not None
    with pytest.raises(process_identity.ProcessIdentityUnavailable):
        kill_process_session(os.getpid(), birth)
