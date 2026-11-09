"""Exact Core-owned instructions, independent of lossy native conversation history."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any


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


def grok_channel_is_confirmed(
    home: Path, external_id: str, text: str, *, not_before_ns: int
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
