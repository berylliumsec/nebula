"""Disposable real Core; fake provider, real runtime policy, no research commands."""

import asyncio
import os
import tempfile
from pathlib import Path
import uvicorn
from test_automation_runtime import runtime as make_runtime
from test_harnesses import FakeAdapter, FakeConnection
from nebula.v3.api import create_app
from nebula.v3.domain import HarnessProfile
from nebula.v3.harnesses import HarnessRuntimeService, HarnessEvent, HarnessPlanEntry
from nebula.v3.credentials import CredentialStore

with tempfile.TemporaryDirectory(prefix="nebula-host-rollover-") as directory:
    root = Path(directory)
    manager, store, artifacts, project, _ = make_runtime(root)
    profile = store.create(
        HarnessProfile(
            id="host-fixture",
            name="Fixture agent",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    finish = asyncio.Event()

    class HeldConnection(FakeConnection):
        async def run_turn(self, prompt, *, model, images=None):
            async for event in super().run_turn(prompt, model=model, images=images):
                if event.type == "completed" and "Continue with host tools" in prompt:
                    yield HarnessEvent(
                        type="item_upsert",
                        item_id="fixture-plan",
                        item_kind="plan",
                        item_status="streaming",
                        title="Plan",
                        plan=[
                            HarnessPlanEntry(
                                id="read",
                                title="Read the project context",
                                status="completed",
                            ),
                            HarnessPlanEntry(
                                id="check",
                                title="Verify the current workspace and saved runtime policy",
                                status="in_progress",
                            ),
                            HarnessPlanEntry(
                                id="finish", title="Report the result", status="pending"
                            ),
                        ],
                    )
                    await finish.wait()
                yield event

    class HeldAdapter(FakeAdapter):
        async def open(self, request):
            connection = HeldConnection(request)
            self.connections.append(connection)
            return connection

    adapter = HeldAdapter()
    harness = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=manager.workspace_resolver,
        adapter_factory=lambda _: adapter,
        artifact_store=artifacts,
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        automation_runtime=manager,
        harness_runtime_service=harness,
        auth_token="model-test-token",
        allow_insecure_device_pairing=True,
        static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
    )

    @app.post("/fixture/start")
    async def start():
        finish.clear()
        policy = manager.project_policy(project.id)
        manager.update_project_policy(
            project.id,
            approval_policy="never",
            execution_mode="docker",
            network_enabled=False,
            runner_profile_id="runner",
            max_timeout_ms=60000,
            expected_revision=policy.revision,
        )
        chat, _, turn = harness.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Remember this fixture",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        async for _ in harness.stream_turn(turn.id):
            pass
        return {
            "project": project.id,
            "chat": chat.id,
            "session": turn.harness_session_id,
        }

    app.router.routes.insert(0, app.router.routes.pop())

    @app.post("/fixture/finish")
    async def release():
        finish.set()
        return {"released": True}

    app.router.routes.insert(0, app.router.routes.pop())

    @app.post("/fixture/catch-up")
    async def catch_up():
        chat, _, turn = harness.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Catch-up fixture update",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        async for _ in harness.stream_turn(turn.id):
            pass
        return {"chat": chat.id}

    app.router.routes.insert(0, app.router.routes.pop())
    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19441"))
    )
