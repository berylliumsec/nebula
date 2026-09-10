"""Serve the exact candidate's embedded UI/Core for bounded model acceptance.

Only disposable data is used. Unlike the source fixture, this entry point adds
no routes or adapters to Core. It supports the dense-graph and reset journeys,
which create their synthetic records through the normal API.
"""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not args.core.is_file() or not 1024 <= args.port <= 65535:
        parser.error("An existing candidate Core and non-privileged port are required")
    if args.auth_file.exists() or not args.auth_file.parent.is_dir():
        parser.error(
            "Use a new auth file inside an existing temporary evidence directory"
        )
    environment = dict(os.environ)
    environment.pop("NEBULA_V3_UI_DIR", None)
    environment.pop("NEBULA_V3_API_TOKEN", None)

    def interrupted(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="nebula-candidate-model-") as directory:
        with subprocess.Popen(
            [
                str(args.core.resolve()),
                "ui",
                "--no-browser",
                "--lan",
                "--allow-insecure-lan",
                "--host",
                "0.0.0.0",
                "--port",
                str(args.port),
                "--data-dir",
                directory,
            ],
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                output = ""
                assert child.stdout is not None
                for line in child.stdout:
                    output += line
                    token = re.search(r'"token"\s*:\s*"([^\"]+)"', output)
                    if '"nebula-api-ready"' not in output or not token:
                        continue
                    descriptor = os.open(
                        args.auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "w") as stream:
                        json.dump(
                            {"token": token[1], "core": str(args.core.resolve())},
                            stream,
                        )
                    print(
                        "Candidate Core ready; embedded assets and disposable model data only",
                        flush=True,
                    )
                    break
                else:
                    raise RuntimeError(
                        "Candidate exited without an authenticated ready response"
                    )
                if child.wait() != 0:
                    raise RuntimeError("Candidate Core exited unexpectedly")
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)


if __name__ == "__main__":
    main()
