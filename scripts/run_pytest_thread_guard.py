"""Run selected pytest arguments; bound exit after a thread leak and bound total time."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--termination-grace", type=float, default=5)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0,
        help="Stop pytest's process group after this many seconds (0 disables).",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    targets = args.pytest_args
    if targets[:1] == ["--"]:
        targets = targets[1:]
    if not targets or args.termination_grace <= 0 or args.max_seconds < 0:
        parser.error("Selected pytest arguments and a positive exit grace are required")
    with tempfile.TemporaryDirectory(prefix="nebula-test-thread-guard-") as directory:
        marker = Path(directory) / "leaked"
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", *targets],
            env={**os.environ, "NEBULA_TEST_THREAD_LEAK_MARKER": str(marker)},
            start_new_session=True,
        )
        detected = None
        started = time.monotonic()
        while process.poll() is None:
            if args.max_seconds and time.monotonic() - started >= args.max_seconds:
                print(
                    f"pytest ran longer than {args.max_seconds:g}s; stopping its owned process group",
                    file=sys.stderr,
                    flush=True,
                )
                _stop_group(process)
                return 1
            if marker.exists():
                detected = detected or time.monotonic()
                if time.monotonic() - detected >= args.termination_grace:
                    print(
                        "pytest reported leaked threads and failed to exit; stopping its owned process group",
                        file=sys.stderr,
                        flush=True,
                    )
                    _stop_group(process)
                    return 1
            time.sleep(0.05)
        return 1 if marker.exists() else process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
