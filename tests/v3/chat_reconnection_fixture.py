"""Disposable real Core; deterministic harness text, no tools or provider traffic."""

import asyncio
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import HTTPException, Request
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    HarnessCapabilities,
    HarnessProfile,
    HarnessSession,
    utc_now,
)
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessRuntimeService,
)
from nebula.v3.storage import NebulaStore

releases = {}
executions = {}


class Connection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/reconnect-fixture"

    def __init__(self, request):
        self.request = request
        self.external_session_id = "fixture-" + request.session.id

    async def run_turn(self, prompt, *, model, **options):
        key = self.request.session.id
        executions[key] = executions.get(key, 0) + 1
        release = releases.setdefault(key, asyncio.Event())
        yield HarnessEvent(type="started", external_session_id=self.external_session_id)
        yield HarnessEvent(type="message_delta", delta="Before disconnect. ")
        yield HarnessEvent(
            type="output_delta",
            vendor=self.request.profile.kind,
            item_kind="reasoning",
            item_id="thinking",
            stream="reasoning_summary",
            delta="Saved thinking before disconnection.",
        )
        await release.wait()
        yield HarnessEvent(type="message_delta", delta="After reconnect.")
        yield HarnessEvent(
            type="completed", message="Before disconnect. After reconnect."
        )

    async def steer(self, text):
        pass

    async def interrupt(self):
        releases.setdefault(self.request.session.id, asyncio.Event()).set()

    async def close(self):
        pass


class Adapter(HarnessAdapter):
    async def open(self, request):
        return Connection(request)

    async def probe(self, profile, credential_store):
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            harness_version="fixture",
            capabilities=HarnessCapabilities(
                models=["test-model"], interruption=True, checked_at=utc_now()
            ),
        )


with tempfile.TemporaryDirectory(prefix="nebula-reconnect-") as directory:
    root = Path(directory)
    store = NebulaStore(root / "core.db")
    project = store.create(
        Engagement(id="reconnect-project", name="Connection recovery")
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: root,
        adapter_factory=lambda _: Adapter(),
    )
    app = create_app(
        store,
        harness_runtime_service=runtime,
        artifact_store=ArtifactStore(root / "artifacts"),
        auth_token="reconnect-test-token",
        allow_insecure_device_pairing=True,
        static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
    )

    def auth(request):
        if request.headers.get("authorization") != "Bearer reconnect-test-token":
            raise HTTPException(401)

    @app.post("/fixture/chat/{vendor}")
    async def create_chat(vendor: str, request: Request):
        auth(request)
        identity = str(uuid4())
        profile = store.create(
            HarnessProfile(
                id="profile-" + identity,
                name="Test harness",
                kind=vendor,
                executable="/nonexistent/fixture",
                default_model="test-model",
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
        session = store.create(
            HarnessSession(
                id="session-" + identity,
                engagement_id=project.id,
                harness_profile_id=profile.id,
                model="test-model",
                status="idle",
            )
        )
        chat = store.create(
            ChatSession(
                id=identity,
                engagement_id=project.id,
                title="Connection test",
                backend="harness",
                harness_profile_id=profile.id,
                harness_session_id=session.id,
                model="test-model",
                metadata={"initial_title_state": "operator"},
            )
        )
        return {"id": chat.id}

    @app.post("/fixture/release/{identity}")
    async def release(identity: str, request: Request):
        auth(request)
        key = store.get(ChatSession, identity).harness_session_id
        releases.setdefault(key, asyncio.Event()).set()
        return {"executions": executions.get(key, 0)}

    @app.get("/fixture/executions/{identity}")
    async def count(identity: str, request: Request):
        auth(request)
        key = store.get(ChatSession, identity).harness_session_id
        return {"executions": executions.get(key, 0)}

    # Fixture controls precede the production SPA's catch-all static mount.
    controls = [
        route
        for route in app.router.routes
        if getattr(route, "path", "").startswith("/fixture/")
    ]
    app.router.routes[:] = controls + [
        route for route in app.router.routes if route not in controls
    ]

    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19448"))
    )
