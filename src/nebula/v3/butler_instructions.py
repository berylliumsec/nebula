"""Read the operator-owned Nebula-wide Butler mandate from the data directory."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


MAX_INSTRUCTIONS_BYTES = 32_768


class ButlerInstructionsError(ValueError):
    """The configured instructions exist but cannot be used safely."""


@dataclass(frozen=True)
class ButlerInstructions:
    text: str
    sha256: str


def read_butler_instructions(data_root: Path) -> ButlerInstructions | None:
    """Read ``.butler/BUTLER.md`` without following file or directory symlinks.

    The operator creates and owns this file. A missing file disables the
    Nebula-wide scheduled mandate; Core never creates a default copy.
    """

    root_fd: int | None = None
    folder_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = os.open(
            data_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            folder_fd = os.open(
                ".butler",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return None
        try:
            file_fd = os.open(
                "BUTLER.md",
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=folder_fd,
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ButlerInstructionsError("BUTLER.md must be a regular file")
        content = os.read(file_fd, MAX_INSTRUCTIONS_BYTES + 1)
        if len(content) > MAX_INSTRUCTIONS_BYTES:
            raise ButlerInstructionsError("BUTLER.md exceeds the 32 KiB limit")
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ButlerInstructionsError("BUTLER.md must be UTF-8") from exc
        if not decoded.strip():
            raise ButlerInstructionsError("BUTLER.md is empty")
        return ButlerInstructions(
            text=decoded, sha256=hashlib.sha256(content).hexdigest()
        )
    except ButlerInstructionsError:
        raise
    except OSError as exc:
        raise ButlerInstructionsError(
            "Cannot safely read .butler/BUTLER.md in the Nebula data directory"
        ) from exc
    finally:
        for descriptor in (file_fd, folder_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)
