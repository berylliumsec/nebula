"""Disposable authenticated Core for the browser-primitives LAN journeys.

Run the candidate source directly. No retained runtime installation or external
provider is needed for these tests.
"""

import argparse
import asyncio
import json
from pathlib import Path
import socket

import uvicorn
from keyring.backends.null import Keyring

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    args, _ = parser.parse_known_args()
    store = NebulaStore(args.data_dir / "core.db")
    project = await store.create(Engagement(name="Browser harness fixture"))
    scope = await store.create(ScopePolicy(engagement_id=project.id))
    await store.update(Engagement, project.id, {"scope_policy_id": scope.id})
    app = create_app(
        store,
        auth_token=args.token,
        artifact_store=ArtifactStore(args.data_dir / "artifacts"),
        credential_store=CredentialStore(Keyring()),
        static_dir=args.static_dir,
        allow_insecure_device_pairing=True,
        allow_browser_diagnostic_events=True,
        enable_executable_missions=False,
    )
    with socket.socket() as listener:
        listener.bind((args.host, args.port))
        listener.listen(128)
        print(
            json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}"}),
            flush=True,
        )
        await uvicorn.Server(uvicorn.Config(app, log_level="warning")).serve(
            sockets=[listener]
        )


if __name__ == "__main__":
    asyncio.run(main())
