"""Find and stop processes a Host command or harness CLI left behind.

Core marks every process tree it starts outside a protected memory group with
an environment variable (``NEBULA_PROCESS_OWNER=<token>``). Children inherit
it, including ones that call ``setsid`` or double-fork to escape their process
group, so a ``/proc`` scan finds them after their parent exits. The marker is
only a tracking aid, not containment: a process that execs with a cleared or
rewritten environment (``env -i``, ``sudo``, a service manager such as
``systemd-run``/``at``/``docker``) is not found. The protected memory runner's
cgroup is the containment boundary; this module covers the paths without one.

Each candidate is pinned with a pidfd and its marker re-read before it is
signalled, so a recycled PID is never signalled. Only Linux exposes the
environment of a process this way; elsewhere ``marker_supported()`` is false
and callers keep their process-group cleanup alone.
"""

from __future__ import annotations

import asyncio
import functools
import os
import signal
from pathlib import Path
from uuid import uuid4

from .process_identity import (
    ProcessIdentityUnavailable,
    process_birth,
    same_process,
)

OWNER_ENV = "NEBULA_PROCESS_OWNER"
_PROC = Path("/proc")


@functools.cache
def _launcher() -> str:
    pid = os.getpid()
    try:
        birth = process_birth(pid)
    except ProcessIdentityUnavailable:
        # diagnostic-expected: without a birth identity no orphan is ever claimed.
        birth = None
    return f"{pid}:{birth or 'unknown'}"


def new_owner_token(kind: str, identity: str | None = None) -> str:
    """A unique marker: ``<kind>:<identity>@<launcher pid>:<launcher birth>``.

    ``kind`` names the owner in a process listing; the launcher identity lets a
    later Core tell a process whose launching worker has ended from one a live
    worker still owns.
    """

    return f"{kind}:{identity or uuid4().hex}@{_launcher()}"


def _parse_token(value: str) -> tuple[str, str, int, str] | None:
    head, separator, launcher = value.rpartition("@")
    kind, colon, identity = head.partition(":")
    pid, colon2, birth = launcher.partition(":")
    if not (separator and colon and colon2 and pid.isdigit() and identity):
        return None
    return kind, identity, int(pid), birth


def orphaned_marked(kind: str) -> dict[str, list[int]]:
    """Marked processes of ``kind`` whose launching Core process has ended.

    Keyed by the owner identity in the marker. A process whose launcher is
    still alive (another worker, another Core on this host) is never listed.
    """

    if not marker_supported():
        return {}
    prefix = f"{OWNER_ENV}={kind}:".encode()
    found: dict[str, list[int]] = {}
    own = os.getpid()
    for pid in _pids():
        if pid == own:
            continue
        try:
            environ = (_PROC / str(pid) / "environ").read_bytes()
        except OSError:
            # diagnostic-expected: an exited process, or one of another user.
            continue
        for entry in environ.split(b"\0"):
            if not entry.startswith(prefix):
                continue
            parsed = _parse_token(entry[len(OWNER_ENV) + 1 :].decode(errors="replace"))
            if parsed is None or parsed[0] != kind:
                break
            _kind, identity, launcher_pid, birth = parsed
            if birth == "unknown" or same_process(launcher_pid, birth):
                break
            found.setdefault(f"{kind}:{identity}@{launcher_pid}:{birth}", []).append(
                pid
            )
            break
    return found


def owner_environment(token: str) -> dict[str, str]:
    return {OWNER_ENV: token}


def marker_supported() -> bool:
    return os.name == "posix" and (_PROC / "self" / "environ").is_file()


def _has_marker(pid: int, needle: bytes) -> bool:
    try:
        environ = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        # diagnostic-expected: an exited process, or one of another user.
        return False
    return needle in environ.split(b"\0")


def _parent_of(pid: int) -> int | None:
    try:
        fields = (_PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        # diagnostic-expected: an exited or unreadable process has no parent.
        return None
    if fields[0] == "Z":
        return None
    return int(fields[1])


def _pids() -> list[int]:
    try:
        return [int(entry) for entry in os.listdir(_PROC) if entry.isdigit()]
    except OSError:
        # diagnostic-expected: no /proc on this host.
        return []


def marked_processes(token: str) -> list[int]:
    """Live processes carrying ``token``, never this process."""

    if not marker_supported():
        return []
    needle = f"{OWNER_ENV}={token}".encode()
    own = os.getpid()
    return [pid for pid in _pids() if pid != own and _has_marker(pid, needle)]


def descendant_processes(root: int) -> list[int]:
    """Every live descendant of ``root`` (not ``root`` itself), parents first."""

    if not marker_supported():
        return []
    children: dict[int, list[int]] = {}
    for pid in _pids():
        parent = _parent_of(pid)
        if parent is not None:
            children.setdefault(parent, []).append(pid)
    found: list[int] = []
    pending = list(children.get(root, ()))
    while pending:
        pid = pending.pop(0)
        if pid in found or pid == os.getpid():
            continue
        found.append(pid)
        pending.extend(children.get(pid, ()))
    return found


def _signal_verified(pid: int, value: int, still_owned) -> bool:
    """Signal ``pid`` only while ``still_owned(pid)`` holds for that process."""

    pidfd_open = getattr(os, "pidfd_open", None)
    send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or send is None:
        if not still_owned(pid):
            return False
        try:
            os.kill(pid, value)
        except ProcessLookupError:
            # diagnostic-expected: the process exited first.
            return False
        return True
    try:
        descriptor = pidfd_open(pid)
    except (ProcessLookupError, OSError):
        # diagnostic-expected: the process exited before it was pinned.
        return False
    try:
        # Re-read after pinning: the PID cannot be reused while it is held.
        if not still_owned(pid):
            return False
        send(descriptor, value)
        return True
    except ProcessLookupError:
        # diagnostic-expected: the process exited first.
        return False
    finally:
        os.close(descriptor)


def kill_marked(token: str, value: int = signal.SIGKILL) -> int:
    """Signal every process still carrying ``token``; returns how many."""

    needle = f"{OWNER_ENV}={token}".encode()
    return sum(
        _signal_verified(pid, value, lambda pid: _has_marker(pid, needle))
        for pid in marked_processes(token)
    )


async def stop_process_tree(
    process: asyncio.subprocess.Process,
    *,
    token: str | None,
    grace_seconds: float = 3.0,
) -> int:
    """Stop a long-lived CLI and every process it started.

    Descendants are collected while the root is alive (parent links are
    intact). TERM goes to the root (its whole group when it leads one), the
    descendants and the marked processes; after ``grace_seconds`` everything
    still there is killed. Returns how many processes other than the root were
    signalled.
    """

    root = getattr(process, "pid", None)
    tree: dict[int, int] = {}
    own_group = False
    if isinstance(root, int) and process.returncode is None:
        try:
            own_group = os.getpgid(root) == root and root != os.getpgrp()
        except ProcessLookupError:
            # diagnostic-expected: the root exited before it was inspected.
            own_group = False
        for pid in await asyncio.to_thread(descendant_processes, root):
            parent = await asyncio.to_thread(_parent_of, pid)
            if parent is not None:
                tree[pid] = parent
    signalled: set[int] = set()

    def _signal_all(value: int) -> None:
        if own_group and isinstance(root, int):
            try:
                os.killpg(root, value)
            except (ProcessLookupError, PermissionError):
                # diagnostic-expected: the group already exited.
                pass
        for pid in tree:
            # A child whose parent exited was reparented; the marker scan
            # below still finds it, the parent check here does not.
            if _signal_verified(pid, value, lambda pid: _parent_of(pid) == tree[pid]):
                signalled.add(pid)
        if token is not None:
            needle = f"{OWNER_ENV}={token}".encode()
            for pid in marked_processes(token):
                if _signal_verified(pid, value, lambda pid: _has_marker(pid, needle)):
                    signalled.add(pid)

    def _root(value: int) -> None:
        if process.returncode is None:
            try:
                if value == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                # diagnostic-expected: the root exited between checks.
                pass

    _root(signal.SIGTERM)
    await asyncio.to_thread(_signal_all, signal.SIGTERM)
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            # diagnostic-expected: bounded TERM grace before KILL.
            pass
    _root(signal.SIGKILL)
    await asyncio.to_thread(_signal_all, signal.SIGKILL)
    if process.returncode is None:
        await process.wait()
    if isinstance(root, int):
        signalled.discard(root)
    return len(signalled)
