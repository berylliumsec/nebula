"""Serve a disposable production Core for the explicit release browser checks."""

import argparse
import os
from pathlib import Path
import secrets

import uvicorn

from approval_core import InertHostLoadMonitor
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.host_load import HostLoadGate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    # Refuse existing data or credentials: this process never opens a deployment.
    args.root.mkdir(mode=0o700)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(token)
    app = create_app(
        database=args.root / "nebula.db",
        artifact_store=ArtifactStore(args.root / "artifacts"),
        auth_token=token,
        execution_data_root=args.root,
        static_dir=args.static_dir,
        bootstrap_workspace=True,
        allow_insecure_device_pairing=True,
        host_load_gate=HostLoadGate(InertHostLoadMonitor()),
    )
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="error")


if __name__ == "__main__":
    main()
