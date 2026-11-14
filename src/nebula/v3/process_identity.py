"""Kernel process birth identities; PID reuse never authorizes cleanup."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import sys


class ProcessIdentityUnavailable(RuntimeError):
    pass


def process_birth(pid: int) -> str | None:
    if type(pid) is not int or pid < 1:
        raise ValueError("a positive OS process identity is required")
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # comm can contain spaces and parentheses; fields after its final
            # closing parenthesis begin at field3, with starttime at field22.
            fields = stat.rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"linux:{boot}:{fields[19]}"
        except FileNotFoundError:
            # diagnostic-expected: an exited incarnation is absent or reaped.
            return None
        except (OSError, IndexError) as error:
            raise ProcessIdentityUnavailable(
                "the worker's kernel birth identity could not be read"
            ) from error
    if sys.platform == "darwin":

        class BsdInfo(ctypes.Structure):
            _fields_ = [
                ("prefix", ctypes.c_uint32 * 12),
                ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32),
                ("suffix", ctypes.c_uint32 * 6),
                ("start_seconds", ctypes.c_uint64),
                ("start_microseconds", ctypes.c_uint64),
            ]

        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        info = BsdInfo()
        length = library.proc_pidinfo(
            pid, 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info)
        )
        if length == ctypes.sizeof(info):
            return f"darwin:{info.start_seconds}:{info.start_microseconds}"
        if ctypes.get_errno() == errno.ESRCH:
            return None
        raise ProcessIdentityUnavailable(
            "the worker's kernel birth identity could not be read"
        )
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise ProcessIdentityUnavailable("the worker process cannot be inspected")
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        try:
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(system),
                ctypes.byref(user),
            ):
                raise ProcessIdentityUnavailable(
                    "the worker birth timestamp is unavailable"
                )
            return f"windows:{created.dwHighDateTime}:{created.dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    raise ProcessIdentityUnavailable(
        "this host cannot verify detached worker identities"
    )


def same_process(pid: int, birth: str) -> bool:
    return process_birth(pid) == birth


def process_origin() -> str | None:
    """A PID lookup is authoritative only in the launcher's original namespace."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        if os.readlink("/proc/self") != str(os.getpid()):
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = Path("/proc/self/ns/pid").stat().st_ino
        return f"linux:{boot}:pidns:{namespace}"
    except OSError:
        # diagnostic-expected: hidden/foreign procfs cannot prove owner death.
        return None


def origin_boot(origin: str | None) -> str | None:
    """The kernel boot named by a ``process_origin()`` value, if it is one."""
    parts = origin.split(":") if isinstance(origin, str) else ()
    if (
        len(parts) == 4
        and parts[0] == "linux"
        and parts[1]
        and parts[2] == "pidns"
        and parts[3].isdigit()
    ):
        return parts[1]
    return None


def birth_boot(birth: str | None) -> str | None:
    """The kernel boot named by a Linux ``process_birth()`` value, if it is one."""
    parts = birth.split(":") if isinstance(birth, str) else ()
    if len(parts) == 3 and parts[0] == "linux" and parts[1] and parts[2].isdigit():
        return parts[1]
    return None


def origin_from_previous_boot(recorded: str | None, current: str | None) -> bool:
    """Whether every process launched under ``recorded`` ended with an older boot.

    The boot id is kernel-global rather than per-namespace, changes on every
    boot, and no process survives its kernel. A different PID namespace on the
    same boot proves nothing: the old namespace and its workers can still run.
    An origin that cannot be read (``None``) never proves anything either.
    """
    old, new = origin_boot(recorded), origin_boot(current)
    return old is not None and new is not None and old != new


def launch_owner_ended(origin: str | None, pid: int, birth: str) -> bool:
    """Kernel-level proof that a recorded launching process has ended.

    In its own PID namespace the exact birth is checked; a parseable earlier
    kernel boot proves it ended with that kernel on this host (callers need
    lease evidence for hosts sharing the database). A foreign namespace or an
    unreadable origin proves nothing.
    """
    current = process_origin()
    if origin_from_previous_boot(origin, current):
        return True
    return current is not None and origin == current and not same_process(pid, birth)


def signal_process(pid: int, birth: str, selected_signal: int = signal.SIGTERM) -> bool:
    if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
        # Other platforms use authenticated IPC for graceful exit. Never use a
        # check-then-kill PID fallback that could signal a replacement process.
        raise ProcessIdentityUnavailable(
            "use authenticated worker shutdown on this host"
        )
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        # diagnostic-expected: a worker can exit before its pidfd is opened.
        return False
    try:
        if not same_process(pid, birth):
            return False
        signal.pidfd_send_signal(descriptor, selected_signal)
        return True
    finally:
        os.close(descriptor)
