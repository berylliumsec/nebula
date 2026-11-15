"""Run selected pytest arguments and bound exit after a reported thread leak."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--termination-grace", type=float, default=5)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    targets = args.pytest_args
    if targets[:1] == ["--"]:
        targets = targets[1:]
    if not targets or args.termination_grace <= 0:
        parser.error("Selected pytest arguments and a positive exit grace are required")
    with tempfile.TemporaryDirectory(prefix="nebula-test-thread-guard-") as directory:
        marker = Path(directory) / "leaked"
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", *targets],
            env={**os.environ, "NEBULA_TEST_THREAD_LEAK_MARKER": str(marker)},
            start_new_session=True,
        )
        detected = None
        while process.poll() is None:
            if marker.exists():
                detected = detected or time.monotonic()
                if time.monotonic() - detected >= args.termination_grace:
                    print(
                        "pytest reported leaked threads and failed to exit; stopping its owned process group",
                        file=sys.stderr,
                        flush=True,
                    )
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    return 1
            time.sleep(0.05)
        return 1 if marker.exists() else process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
