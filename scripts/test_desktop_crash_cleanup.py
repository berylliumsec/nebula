#!/usr/bin/env python3
"""Assert that a force-killed desktop cannot orphan its packaged Core."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path


def matching_core_processes(core: Path) -> list[int]:
    expected = core.resolve()
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            executable = (entry / "exe").resolve()
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if executable == expected and b"serve" in arguments:
            matches.append(int(entry.name))
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desktop", type=Path)
    arguments = parser.parse_args()
    desktop = arguments.desktop.resolve()
    core = desktop.with_name("nebula-core")
    if not desktop.is_file() or not core.is_file():
        parser.error("desktop and sibling nebula-core are required")

    process = subprocess.Popen(
        [str(desktop), "--self-test"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    observed = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        observed = matching_core_processes(core)
        if observed:
            break
        time.sleep(0.02)
    if not observed:
        process.kill()
        process.wait()
        raise RuntimeError("desktop exited before the Core lifetime test could attach")

    os.kill(process.pid, signal.SIGKILL)
    process.wait()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        remaining = matching_core_processes(core)
        if not remaining:
            return 0
        time.sleep(0.05)

    remaining = matching_core_processes(core)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise RuntimeError(f"desktop crash orphaned Nebula Core processes: {remaining}")


if __name__ == "__main__":
    raise SystemExit(main())
