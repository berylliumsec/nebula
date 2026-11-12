"""Authenticated disposable Core for sidebar ordering; no provider work is submitted."""

import asyncio
import os
import tempfile
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import ChatSession, ChatTurn, Engagement, ProviderProfile
from nebula.v3.storage import NebulaStore
from keyring.backends.null import Keyring


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-conversation-order-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        app = create_app(
            store,
            artifact_store=ArtifactStore(root / "artifacts"),
            credential_store=CredentialStore(Keyring()),
            auth_token="conversation-order-test-token",
            allow_insecure_device_pairing=True,
            static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
        )

        def authorize(request):
            if (
                request.headers.get("authorization")
                != "Bearer conversation-order-test-token"
            ):
                raise HTTPException(401)

        @app.post("/fixture/conversations")
        async def seed(request: Request):
            authorize(request)
            project = await store.create(Engagement(name="Conversation ordering"))
            provider = await store.create(
                ProviderProfile(
                    name="Inert fixture",
                    provider_type="vllm",
                    is_local=True,
                    endpoint="http://127.0.0.1:1",
                    model_allowlist=["fixture"],
                )
            )
            ids = {}
            for name, status, pinned in [
                ("Paused chat", None, False),
                ("Pinned idle chat", None, True),
                ("Blocked chat", "waiting_approval", False),
                ("Running chat", "waiting_callback", False),
                ("Pinned running chat", "waiting_callback", True),
            ]:
                chat = await store.create(
                    ChatSession(
                        engagement_id=project.id,
                        title=name,
                        provider_profile_id=provider.id,
                        model="fixture",
                        metadata={"pinned": pinned, "initial_title_state": "operator"},
                    )
                )
                ids[name] = chat.id
                if status:
                    await store.create(
                        ChatTurn(
                            id="turn-" + chat.id,
                            engagement_id=project.id,
                            session_id=chat.id,
                            provider_profile_id=provider.id,
                            model="fixture",
                            status=status,
                        )
                    )
            return {"project": project.id, "ids": ids}

        @app.post("/fixture/conversations/{session_id}/finish")
        async def finish(session_id: str, request: Request):
            authorize(request)
            await store.update(ChatTurn, "turn-" + session_id, {"status": "complete"})
            return {"finished": session_id}

        @app.post("/fixture/child-project-conversations")
        async def seed_child_project(request: Request):
            authorize(request)
            parent = await store.create(Engagement(name="Parent project"))
            child = await store.create(
                Engagement(
                    name="Child project",
                    parent_engagement_id=parent.id,
                )
            )
            provider = await store.create(
                ProviderProfile(
                    name="Inert fixture",
                    provider_type="vllm",
                    is_local=True,
                    endpoint="http://127.0.0.1:1",
                    model_allowlist=["fixture"],
                )
            )
            chat = await store.create(
                ChatSession(
                    engagement_id=child.id,
                    title="Example child chat",
                    provider_profile_id=provider.id,
                    model="fixture",
                    metadata={"initial_title_state": "operator"},
                )
            )
            return {"parent": parent.id, "child": child.id, "chat": chat.id}

        controls = [
            route
            for route in app.router.routes
            if getattr(route, "path", "").startswith("/fixture/")
        ]
        app.router.routes[:] = controls + [
            route for route in app.router.routes if route not in controls
        ]
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19489")),
            )
        ).serve()


if __name__ == "__main__":
    asyncio.run(main())
