"""Exact Core-owned instructions, independent of lossy native conversation history."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from uuid import uuid4


def instruction_snapshot(text: str) -> dict[str, str]:
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def snapshot_text(value: Any) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError(
            "The saved standing instructions are unavailable; retry this message."
        )
    text = value["text"]
    if instruction_snapshot(text)["sha256"] != value.get("sha256"):
        raise ValueError("The saved standing instructions changed; retry this message.")
    return text


def installed_channel_matches(value: Any, external_id: str, text: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("external_session_id") == external_id
        and value.get("sha256") == instruction_snapshot(text)["sha256"]
    )


def grok_channel_barrier(home: Path) -> tuple[int, int]:
    """Return an advanced filesystem timestamp and its device before the RPC.

    The owned probe is removed before returning. Failed setup, clock advance
    or cleanup fails the open rather than falling back to a wall clock.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    # The configured account root may be a symlink; its sessions and evidence
    # remain no-follow, as in the existing native readback contract.
    account = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir("sessions", mode=0o700, dir_fd=account)
        except FileExistsError:
            # diagnostic-expected: sessions may already exist; the no-follow
            # O_DIRECTORY open below verifies that exact existing entry.
            pass
        sessions = os.open("sessions", flags, dir_fd=account)
        try:
            return _grok_barrier_probe(sessions)
        finally:
            os.close(sessions)
    finally:
        os.close(account)


def _grok_barrier_probe(sessions: int) -> tuple[int, int]:
    # Kernel-assigned inode timestamps may lag time.time_ns(). Advance the
    # filesystem clock past its first tick before any native session RPC.
    name = ".nebula-instruction-barrier-" + uuid4().hex
    probe = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=sessions,
    )
    owned = None
    try:
        owned = os.fstat(probe)
        if not stat.S_ISREG(owned.st_mode):
            raise OSError("The native instruction probe is not regular.")
        if os.write(probe, b"0") != 1:
            raise OSError("The native instruction probe write was incomplete.")
        first = os.fstat(probe)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.001)
            if time.monotonic() >= deadline:
                break
            os.lseek(probe, 0, os.SEEK_SET)
            if os.write(probe, b"1") != 1:
                raise OSError("The native instruction probe write was incomplete.")
            current = os.fstat(probe)
            if current.st_mtime_ns > first.st_mtime_ns:
                return current.st_mtime_ns, current.st_dev
        raise TimeoutError("The native instruction filesystem clock did not advance.")
    finally:
        try:
            if owned is None:
                owned = os.fstat(probe)
            entry = os.stat(name, dir_fd=sessions, follow_symlinks=False)
            if (entry.st_dev, entry.st_ino) != (owned.st_dev, owned.st_ino):
                raise OSError("The owned native instruction probe changed.")
            os.unlink(name, dir_fd=sessions)
        finally:
            os.close(probe)


def grok_channel_is_confirmed(
    home: Path,
    external_id: str,
    text: str,
    *,
    not_before_ns: int,
    expected_device: int | None = None,
) -> bool:
    """Read only this native session's fresh system-head artifact, bounded.

    Unknown ACP metadata may be silently ignored. A version string alone is
    insufficient proof that the installed runtime accepted our exact rules.
    Grok writes system_prompt.txt synchronously before a cold session opens.
    No symlink, special file, stale artifact or ambiguous session is a receipt.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", external_id):
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root = os.open(home / "sessions", flags)
        try:
            if expected_device is not None and os.fstat(root).st_dev != expected_device:
                return False
            matches = []
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 4_096:
                        return False
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    folder = os.open(entry.name, flags, dir_fd=root)
                    try:
                        try:
                            session = os.open(external_id, flags, dir_fd=folder)
                        except FileNotFoundError:
                            # diagnostic-expected: this account folder has no matching native session; inspect the next bounded candidate.
                            continue
                        try:
                            file = os.open(
                                "system_prompt.txt",
                                os.O_RDONLY
                                | os.O_NONBLOCK
                                | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=session,
                            )
                            try:
                                info = os.fstat(file)
                                if (
                                    not stat.S_ISREG(info.st_mode)
                                    or info.st_mtime_ns < not_before_ns
                                    or (
                                        expected_device is not None
                                        and info.st_dev != expected_device
                                    )
                                ):
                                    return False
                                payload = os.read(file, 2_000_001)
                                if len(payload) > 2_000_000:
                                    return False
                                matches.append(payload.decode("utf-8"))
                            finally:
                                os.close(file)
                        finally:
                            os.close(session)
                    finally:
                        os.close(folder)
            return len(matches) == 1 and matches[0].count(text) == 1
        finally:
            os.close(root)
    except (OSError, UnicodeError):
        # diagnostic-expected: the adapter reports unverified readiness without
        # logging any native instruction content or account data.
        return False
