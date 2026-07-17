#!/usr/bin/env python3
"""Run the Nebula 3 suite without importing legacy Qt test infrastructure."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    environment = dict(os.environ)
    # A developer may have pytest-qt installed globally even when the Nebula 3
    # dependency boundary intentionally contains no Qt binding.
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--confcutdir=tests/v3",
        "tests/v3",
        *sys.argv[1:],
    ]
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
