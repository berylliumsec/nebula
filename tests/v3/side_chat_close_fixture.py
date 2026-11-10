"""Disposable authenticated Core with execution SQL fencing and inert side chats."""

import argparse
import asyncio
import json
import socket
from contextlib import suppress
from pathlib import Path

import uvicorn
from keyring.backends.null import Keyring

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatService
from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database
from nebula.v3.domain import ChatMessage, ChatSession, Engagement, ProviderProfile
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.storage import NebulaStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--static-dir", type=Path)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--allow-insecure-device-pairing", action="store_true")
    parser.add_argument("--allow-browser-diagnostics", action="store_true")
    args = parser.parse_args()
    control = NebulaStore(args.data_dir / "core.db")
    runtime = None
    heartbeat = None
    try:
        await control.create(Engagement(id="project", name="Side chat cleanup"))
        coordinator = RuntimeCoordination(control.database)
        await coordinator.register_runtime("execution", "side-chat-fixture")
        await coordinator.claim_project("project", "execution")
        await control.create(
            ProviderProfile(
                id="provider", name="Inert", provider_type="vllm", enabled=False
            )
        )
        await control.create(
            ChatSession(
                id="parent",
                engagement_id="project",
                title="Preserved parent",
                provider_profile_id="provider",
                model="inert",
            )
        )
        await control.create(
            ChatMessage(
                id="question",
                engagement_id="project",
                session_id="parent",
                sequence=1,
                role="user",
                content="Preserve this parent history",
            )
        )
        await ChatService(control).fork_session(
            "parent", through_message_id="question", side_chat=True
        )
        runtime = NebulaStore(
            Database(
                args.data_dir / "core.db",
                bootstrap=False,
                execution_runtime_id="execution",
            )
        )

        async def renew():
            while True:
                await coordinator.heartbeat("execution")
                await asyncio.sleep(5)

        heartbeat = asyncio.create_task(renew())
        app = create_app(
            runtime,
            artifact_store=ArtifactStore(args.data_dir / "artifacts"),
            credential_store=CredentialStore(Keyring()),
            auth_token=args.token,
            allow_insecure_device_pairing=args.allow_insecure_device_pairing,
            static_dir=args.static_dir,
            enable_executable_missions=False,
        )
        with socket.socket() as listener:
            listener.bind((args.host, args.port))
            listener.listen()
            print(
                json.dumps({"url": f"http://{args.host}:{listener.getsockname()[1]}"}),
                flush=True,
            )
            await uvicorn.Server(uvicorn.Config(app, log_level="warning")).serve(
                sockets=[listener]
            )
    finally:
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        for store in (runtime, control):
            if store:
                await store.event_history.close()
                await store.database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
