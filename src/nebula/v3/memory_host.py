"""Resolve visible cgroup-v2 constraints for Core's current host process."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re


def _proc_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def cgroup_memory_capacity(
    *, proc: Path = Path("/proc"), fallback: Path = Path("/sys/fs/cgroup")
) -> int | None:
    locations = [fallback]
    try:
        membership = next(
            line[3:]
            for line in (proc / "self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        for line in (proc / "self/mountinfo").read_text().splitlines():
            before, separator, after = line.partition(" - ")
            if not separator or after.split()[0] != "cgroup2":
                continue
            fields = before.split()
            root, mount = (
                PurePosixPath(_proc_path(fields[3])),
                Path(_proc_path(fields[4])),
            )
            member = PurePosixPath(membership)
            if ".." in member.parts:
                raise ValueError("invalid cgroup membership")
            if member.is_relative_to(root):
                current = mount.joinpath(*member.relative_to(root).parts)
            elif str(member) == "/":
                # A cgroup namespace reports / for its root even when the
                # mounted hierarchy exposes that namespace via another root.
                current = mount
            else:
                continue
            while current.is_relative_to(mount):
                locations.append(current)
                if current == mount:
                    break
                current = current.parent
    except (OSError, StopIteration, ValueError, IndexError):
        # diagnostic-expected: non-Linux/hidden proc metadata still permits the
        # directly visible root constraint; no missing value invents a quota.
        pass
    finite = []
    for directory in set(locations):
        try:
            value = (directory / "memory.max").read_text().strip()
            if value != "max" and int(value) > 0:
                finite.append(int(value))
        except (OSError, ValueError):
            # diagnostic-expected: unavailable ancestor controls are not measurements.
            continue
    return min(finite) if finite else None
