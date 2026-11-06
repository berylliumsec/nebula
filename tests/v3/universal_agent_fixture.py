"""Disposable authenticated Core and production UI, with inert agent transports."""

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import HTTPException, Request
from keyring.backends.null import Keyring

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nebula.v3 import chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.chat_snapshot_parts import resolve_request_snapshot
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatSession,
    ChatSubagent,
    ChatTurn,
    Engagement,
    HarnessProfile,
    McpServerProfile,
    NativeHookExecution,
    ProviderProfile,
    utc_now,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.providers import ModelResponse, ToolCall, ToolChoice
from nebula.v3.storage import NebulaStore
from tests.v3.test_harness_provider_subagents import ChildProvider, ScriptedAdapter
from tests.v3.test_universal_agent_tools import hook


class Provider(ChildProvider):
    def __init__(self):
        super().__init__()
        self.routing = 0

    async def complete(self, request):
        if request.metadata.get("operation"):
            return self._response("Synthetic guarded conversation")
        if request.tool_choice == ToolChoice.NONE:
            return self._response("Guarded child finished.")
        if "You are a subagent." in (request.instructions or ""):
            return self._response("Guarded child finished.")
        self.routing += 1
        calls = [
            ToolCall(
                id="start",
                name="start_subagent",
                arguments={
                    "task": "Return the synthetic guarded result",
                    "name": "Guarded child",
                    "context": None,
                    "reasoning_effort": None,
                    "capabilities": {"hook_ids": ["guard"]},
                },
            ),
            ToolCall(
                id="wait",
                name="wait_subagents",
                arguments={"subagent_ids": None, "mode": "all"},
            ),
        ]
        if self.routing <= len(calls):
            return ModelResponse(
                provider_id="provider",
                model="model-a",
                tool_calls=[calls[self.routing - 1]],
                finish_reason="tool_calls",
            )
        return self._response("Guarded child finished.")


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-universal-ui-") as directory:
        root = Path(directory)
        hook(root)
        store = NebulaStore(root / "core.db")
        (
            await store.create(
                ProviderProfile(
                    id="provider",
                    name="Synthetic provider",
                    provider_type="vllm",
                    is_local=True,
                    model_allowlist=["model-a"],
                    capabilities={"streaming": True, "tool_calling": True},
                    capability_verifications={
                        "model-a": {"model": "model-a", "status": "verified"}
                    },
                    privacy={"permits_sensitive_data": True},
                )
            )
        )
        harness = await store.create(
            HarnessProfile(
                id="codex",
                name="Synthetic harness",
                kind="codex_app_server",
                executable="/bin/true",
                default_model="gpt-test",
                capabilities={
                    "models": ["gpt-test"],
                    "mcp": True,
                    "interruption": True,
                },
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
        adapter = ScriptedAdapter()
        artifacts = ArtifactStore(root / "artifacts")
        credentials = CredentialStore(keyring_backend=Keyring())
        runtime = HarnessRuntimeService(
            store,
            credential_store=credentials,
            workspace_resolver=lambda _: root,
            artifact_store=artifacts,
            adapter_factory=lambda _: adapter,
        )
        adapter.runtime = runtime

        async def script(connection, prompt):
            if "MCP continuity probe" in prompt:
                return "MCP continuity confirmed."
            if "You are a subagent." not in prompt:
                started = await connection.call(
                    "subagent.start",
                    task="Return the synthetic guarded result",
                    capabilities={"hook_ids": ["guard"]},
                )
                if started["isError"]:
                    raise AssertionError(started)
                result = await connection.call("subagent.wait")
                if result["isError"]:
                    raise AssertionError(result)
            return "Guarded child finished."

        adapter.script = script
        chat_module.provider_from_profile = lambda _: Provider()
        bundle = Path(__file__).resolve().parents[2] / "ui/dist"
        digest = hashlib.sha256()
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                digest.update(
                    str(path.relative_to(bundle)).encode() + b"\0" + path.read_bytes()
                )
        app = create_app(
            store,
            harness_runtime_service=runtime,
            artifact_store=artifacts,
            auth_token="universal-test-token",
            execution_data_root=root,
            static_dir=bundle,
            credential_store=credentials,
            allow_insecure_device_pairing=True,
        )

        def authorize(request):
            if request.headers.get("authorization") != "Bearer universal-test-token":
                raise HTTPException(401)

        @app.post("/fixture/seed")
        async def seed(request: Request):
            authorize(request)
            project = await store.create(
                Engagement(
                    id=str(uuid4()),
                    name="Synthetic mixed agents",
                    workspace_path=str(root),
                )
            )
            provider = await store.create(
                ChatSession(
                    engagement_id=project.id,
                    title="Provider parent",
                    provider_profile_id="provider",
                    model="model-a",
                    metadata={
                        "initial_title_state": "operator",
                        "tools_enabled": False,
                    },
                )
            )
            session = await runtime.create_session(
                engagement_id=project.id,
                profile_id=harness.id,
                model="gpt-test",
                tools_enabled=False,
            )
            parent = await store.create(
                ChatSession(
                    engagement_id=project.id,
                    title="Harness parent",
                    backend="harness",
                    harness_profile_id=harness.id,
                    harness_session_id=session.id,
                    model="gpt-test",
                    metadata={"initial_title_state": "operator"},
                )
            )
            return {
                "project": project.id,
                "provider": provider.id,
                "harness": parent.id,
                "bundle_sha256": digest.hexdigest(),
            }

        @app.post("/fixture/mcp-seed")
        async def mcp_seed(request: Request):
            fixture = await seed(request)
            server = await store.create(
                McpServerProfile(
                    name=f"continuity-{fixture['project']}",
                    transport="streamable_http",
                    url="https://mcp.invalid/api",
                    enabled=True,
                    metadata={"engagement_id": fixture["project"]},
                    capabilities={
                        "checked_at": utc_now(),
                        "tools": [
                            {
                                "name": "read_marker",
                                "input_schema": {"type": "object", "properties": {}},
                                "read_only": True,
                                "destructive": False,
                                "idempotent": True,
                                "open_world": False,
                                "credentialed": False,
                                "annotations_complete": True,
                            }
                        ],
                    },
                )
            )
            return {**fixture, "mcp": server.id}

        @app.get("/fixture/evidence/{identity}")
        async def evidence(identity: str, request: Request):
            authorize(request)
            parent = await store.get(ChatSession, identity)
            records = await store.find_entities(
                ChatSubagent, {"parent_session_id": parent.id}
            )
            turns = await store.list_session_entities(
                ChatTurn, parent.id, newest_first=True, limit=1
            )
            snapshot = (
                (
                    await resolve_request_snapshot(
                        store, turns[0].request_snapshot, session_id=parent.id
                    )
                )
                if turns
                else {}
            )
            return {
                "harness": {
                    "session_id": parent.harness_session_id,
                    "mcp_server_ids": snapshot.get("mcp_server_ids", []),
                    "catalog_ids": [
                        item["id"] for item in snapshot.get("mcp_catalog_snapshot", [])
                    ],
                    "connection_opens": sum(
                        opened.session.id == parent.harness_session_id
                        for opened in adapter.opens
                    ),
                },
                "children": [
                    {
                        "backend": (
                            await store.get(ChatSession, child.child_session_id)
                        ).backend.value,
                        "status": child.status.value,
                        "hooks": [
                            {"event": hook.event_name, "status": hook.status}
                            for hook in (
                                await store.list_session_entities(
                                    NativeHookExecution, child.child_session_id
                                )
                            )
                        ],
                    }
                    for child in records
                ],
            }

        controls = [
            route
            for route in app.router.routes
            if getattr(route, "path", "").startswith("/fixture/")
        ]
        app.router.routes[:] = controls + [
            route for route in app.router.routes if route not in controls
        ]
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19486")),
        )


if __name__ == "__main__":
    main()
