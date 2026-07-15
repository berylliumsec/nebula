from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import zipfile
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatMessage,
    ChatTokenUsage,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTurn,
    HarnessTurnStatus,
    KnowledgeSource,
    McpApprovalMode,
    McpAuthMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    RunBudget,
    RunStatus,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    AdapterOpenRequest,
    HarnessAdapter,
    HarnessConfigurationError,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessPermissionRequest,
    HarnessRuntimeService,
    HarnessStateError,
    HarnessTransportError,
)
from nebula.v3.exporter import export_engagement
from nebula.v3.mcp import McpProbeService
from nebula.v3.storage import NebulaStore


class FakeConnection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/fake"

    def __init__(self, request: AdapterOpenRequest, *, fail: bool = False) -> None:
        self.request = request
        self.external_session_id = request.session.external_session_id
        self.fail = fail
        self.interrupted = False
        self.closed = False
        self.steering: list[str] = []
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str, *, model: str) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.external_session_id = self.external_session_id or "vendor-session-1"
        yield HarnessEvent(
            type="started",
            external_session_id=self.external_session_id,
            external_turn_id=f"vendor-turn-{len(self.prompts)}",
        )
        if self.fail:
            raise HarnessTransportError("uncertain transport loss token=do-not-store")
        answer = f"Harness answer for {prompt}"
        yield HarnessEvent(type="message_delta", delta=answer[:8])
        yield HarnessEvent(type="message_delta", delta=answer[8:])
        yield HarnessEvent(
            type="usage",
            usage=ChatTokenUsage(input_tokens=4, output_tokens=5, total_tokens=9),
        )
        yield HarnessEvent(type="completed", message=answer)

    async def steer(self, text: str) -> None:
        self.steering.append(text)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def close(self) -> None:
        self.closed = True


class FakeAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.opens: list[AdapterOpenRequest] = []
        self.connections: list[FakeConnection] = []

    async def probe(
        self, profile: HarnessProfile, credential_store: CredentialStore
    ) -> HarnessHealth:
        del credential_store
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            harness_version="fixture-1",
            capabilities=HarnessCapabilities(
                steering=True,
                adapter_version=ADAPTER_CONTRACT_VERSION + "/fake",
                checked_at=utc_now(),
            ),
        )

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        self.opens.append(request)
        connection = FakeConnection(request, fail=self.fail)
        self.connections.append(connection)
        return connection


def _runtime(tmp_path: Path, *, fail: bool = False):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        HarnessProfile(
            id="harness-a",
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    mcp = store.create(
        McpServerProfile(
            id="mcp-a",
            name="workspace",
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://mcp.invalid/api",
            enabled=True,
            capabilities=McpCapabilitySnapshot(
                checked_at=utc_now(),
                tools=[
                    McpToolSnapshot(
                        name="read_file",
                        read_only=True,
                        destructive=False,
                        idempotent=True,
                        open_world=False,
                        credentialed=False,
                        annotations_complete=True,
                    ),
                    McpToolSnapshot(
                        name="delete_file",
                        read_only=False,
                        destructive=True,
                        idempotent=True,
                        open_world=False,
                        credentialed=False,
                        annotations_complete=True,
                    ),
                ],
            ),
        )
    )
    adapter = FakeAdapter(fail=fail)
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    return store, engagement, profile, mcp, adapter, runtime


def test_shared_session_handoff_streaming_and_frozen_mcp_snapshot(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, mcp, adapter, runtime = _runtime(tmp_path)
        chat, chat_turn, harness_turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Inspect the target",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[mcp.id],
        )

        with pytest.raises(HarnessStateError, match="active work"):
            await runtime.start_mission(
                engagement_id=engagement.id,
                objective="Cannot overlap",
                profile_id=profile.id,
                model=None,
                budget=RunBudget(),
                harness_session_id=chat.harness_session_id,
            )

        events = [event async for event in runtime.stream_turn(harness_turn.id)]
        assert [event.type for event in events] == [
            "started",
            "message_delta",
            "message_delta",
            "usage",
            "completed",
        ]
        assert (
            store.get(HarnessTurn, harness_turn.id).status == HarnessTurnStatus.COMPLETE
        )
        assert store.get(ChatTurn, chat_turn.id).status.value == "complete"
        session = store.get(HarnessSession, chat.harness_session_id or "")
        assert session.external_session_id == "vendor-session-1"
        assert session.status == HarnessSessionStatus.IDLE

        # Editing a profile cannot mutate the immutable session snapshot.
        store.update(
            McpServerProfile,
            mcp.id,
            {"url": "https://changed.invalid/api"},
            expected_revision=mcp.revision,
        )
        run = await runtime.start_mission(
            engagement_id=engagement.id,
            objective="Continue autonomously",
            profile_id=profile.id,
            model="test-model",
            budget=RunBudget(max_duration_seconds=5),
            harness_session_id=session.id,
        )
        await runtime._mission_tasks[run.id]
        finished = store.get(AgentRun, run.id)
        assert finished.status == RunStatus.COMPLETE
        assert finished.harness_session_id == session.id
        assert (
            finished.runtime_snapshot["mcp_snapshot"][0]["url"]
            == "https://mcp.invalid/api"
        )
        assert len(adapter.opens) == 1
        assert adapter.opens[0].mcp_profiles[0].url == "https://mcp.invalid/api"

        attached = runtime.attach_run_to_chat(run.id)
        assert attached.id == chat.id
        messages = [
            item
            for item in store.list_entities(ChatMessage, engagement_id=engagement.id)
            if item.session_id == chat.id
        ]
        assert [item.content for item in messages][-2:] == [
            "Continue autonomously",
            "Harness answer for Continue autonomously",
        ]
        await runtime.shutdown()
        assert adapter.connections[0].closed is True

    asyncio.run(scenario())


def test_mcp_policy_fails_closed_and_routes_exact_approval(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, mcp, _, runtime = _runtime(tmp_path)
        chat, _, turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Policy test",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[mcp.id],
        )
        safe = await runtime._request_permission(
            turn.id,
            HarnessPermissionRequest(
                vendor_request_id="safe-1",
                category="mcp",
                vendor_name="mcp__workspace__read_file",
                server_name="workspace",
                tool_name="read_file",
                arguments={"path": "README.md"},
            ),
        )
        assert (await safe.decision).allowed is True
        assert safe.approval_id is None
        assert (
            store.get(ToolCall, safe.tool_call_id or "").status
            == ToolCallStatus.APPROVED
        )

        destructive = await runtime._request_permission(
            turn.id,
            HarnessPermissionRequest(
                vendor_request_id="delete-1",
                category="mcp",
                vendor_name="mcp__workspace__delete_file",
                server_name="workspace",
                tool_name="delete_file",
                arguments={"path": "evidence.txt"},
            ),
        )
        approval = store.get(Approval, destructive.approval_id or "")
        assert approval.exact_request["argument_editing"] is False
        assert approval.status == ApprovalStatus.PENDING
        decided = store.update(
            Approval,
            approval.id,
            {"status": ApprovalStatus.APPROVED, "decided_by": "operator"},
            expected_revision=approval.revision,
        )
        await runtime.resolve_approval(decided)
        assert (await destructive.decision).allowed is True
        await asyncio.sleep(0)

        unknown = await runtime._request_permission(
            turn.id,
            HarnessPermissionRequest(
                vendor_request_id="unknown-1",
                category="mcp",
                vendor_name="mcp__ambient__leak",
                server_name="ambient",
                tool_name="leak",
            ),
        )
        denied = await unknown.decision
        assert denied.allowed is False
        assert "Unknown MCP server" in (denied.reason or "")
        assert unknown.approval_id is None

        # Exact deny takes precedence even for an otherwise safe tool.
        session = store.get(HarnessSession, chat.harness_session_id or "")
        snapshot = McpServerProfile.model_validate(session.mcp_snapshot[0])
        snapshot.tool_overrides = {"read_file": McpApprovalMode.DENY}
        store.update(
            HarnessSession,
            session.id,
            {"mcp_snapshot": [snapshot.model_dump(mode="json")]},
            expected_revision=session.revision,
        )
        exact_deny = await runtime._request_permission(
            turn.id,
            HarnessPermissionRequest(
                vendor_request_id="safe-denied",
                category="mcp",
                vendor_name="mcp__workspace__read_file",
                server_name="workspace",
                tool_name="read_file",
            ),
        )
        assert (await exact_deny.decision).allowed is False

    asyncio.run(scenario())


def test_transport_loss_and_restart_interrupt_without_replay(tmp_path):
    async def scenario() -> None:
        store, engagement, profile, _, adapter, runtime = _runtime(tmp_path, fail=True)
        _, chat_turn, turn = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Do not replay me",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        events = [event async for event in runtime.stream_turn(turn.id)]
        assert events[-1].type == "error"
        failed = store.get(HarnessTurn, turn.id)
        assert failed.status == HarnessTurnStatus.INTERRUPTED
        assert store.get(ChatTurn, chat_turn.id).status.value == "interrupted"
        assert adapter.connections[0].prompts == ["Do not replay me"]

        # A later Core instance reconciles uncertain durable state but never opens
        # a connection or reissues the objective.
        store.update(
            HarnessTurn,
            failed.id,
            {"status": HarnessTurnStatus.RUNNING, "completed_at": None},
            expected_revision=failed.revision,
        )
        second_adapter = FakeAdapter()
        restarted = HarnessRuntimeService(
            store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: tmp_path,
            adapter_factory=lambda _: second_adapter,
        )
        await restarted.startup()
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.INTERRUPTED
        assert second_adapter.opens == []

    asyncio.run(scenario())


def test_explicit_stdio_mcp_probe_validates_schema_and_closes_process(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    store = NebulaStore(tmp_path / "mcp.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        McpServerProfile(
            id="mcp-stdio",
            name="fixture",
            transport=McpTransport.STDIO,
            command=sys.executable,
            arguments=[str(fixture)],
            enabled=True,
            trusted_stdio=True,
        )
    )
    service = McpProbeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )

    report = asyncio.run(service.probe(profile.id, engagement_id=engagement.id))

    assert report.compatible is True
    assert report.capabilities.resources is True
    assert report.capabilities.prompts is True
    tool = report.capabilities.tools[0]
    assert tool.name == "read_file"
    assert tool.annotations_complete is True
    assert tool.read_only is True

    bad = store.create(
        profile.model_copy(
            update={
                "id": "mcp-bad-schema",
                "arguments": [str(fixture), "--bad-schema"],
                "revision": 1,
            }
        )
    )
    failed = asyncio.run(service.probe(bad.id, engagement_id=engagement.id))
    assert failed.compatible is False
    assert "invalid schema" in (failed.detail or "")


def test_streamable_http_mcp_probe_injects_bearer_and_closes_session(tmp_path):
    observed: list[tuple[str, str | None]] = []
    deleted = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return None

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            observed.append(
                (request.get("method", ""), self.headers.get("Authorization"))
            )
            method = request.get("method")
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                }
            elif method == "tools/list":
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"tools": []},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"event: message\ndata: " + payload + b"\n\n")
                return
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            else:
                result = {"prompts": []}
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if method == "initialize":
                self.send_header("MCP-Session-Id", "http-session-1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_DELETE(self):
            if self.headers.get("MCP-Session-Id") == "http-session-1":
                deleted.set()
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    credentials = CredentialStore()
    secret = credentials.create(
        CredentialCreateRequest(
            secret=SecretStr("mcp-bearer-fixture"), persistence="session"
        )
    )
    store = NebulaStore(tmp_path / "mcp-http.db")
    profile = store.create(
        McpServerProfile(
            id="mcp-http",
            name="http-fixture",
            transport=McpTransport.STREAMABLE_HTTP,
            url=f"http://127.0.0.1:{server.server_port}/mcp",
            auth_mode=McpAuthMode.BEARER,
            bearer_secret_ref=secret.reference,
            enabled=True,
        )
    )
    service = McpProbeService(
        store,
        credential_store=credentials,
        workspace_resolver=lambda _: tmp_path,
    )
    try:
        report = asyncio.run(service.probe(profile.id))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert report.compatible is True
    assert all(auth == "Bearer mcp-bearer-fixture" for _, auth in observed)
    assert deleted.is_set()


def test_harness_api_chat_mission_handoff_and_catalog(tmp_path):
    store, engagement, profile, mcp, adapter, runtime = _runtime(tmp_path)
    app = create_app(
        store,
        auth_token="test-token",
        harness_runtime_service=runtime,
    )
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        catalog = client.get("/api/v1/harness-catalog", headers=headers)
        assert catalog.status_code == 200
        assert {item["kind"] for item in catalog.json()} == {
            "codex_app_server",
            "claude_agent_sdk",
        }
        health = client.post(f"/api/v1/harnesses/{profile.id}/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["harness_version"] == "fixture-1"
        assert (
            store.get(HarnessProfile, profile.id).capabilities.harness_version
            == "fixture-1"
        )

        completion = client.post(
            "/api/v1/chat/completions",
            headers=headers,
            json={
                "backend": "harness",
                "engagement_id": engagement.id,
                "harness_profile_id": profile.id,
                "model": "test-model",
                "mcp_server_ids": [mcp.id],
                "messages": [{"role": "user", "content": "API inspection"}],
            },
        )
        assert completion.status_code == 200, completion.text
        body = completion.json()
        assert body["backend"] == "harness"
        assert body["message"]["content"] == "Harness answer for API inspection"
        assert body["harness_session_id"]
        assert body["harness_turn_id"]

        sessions = client.get(
            "/api/v1/harness-sessions",
            headers=headers,
            params={"engagement_id": engagement.id},
        )
        assert sessions.status_code == 200
        assert sessions.json()[0]["external_session_id"] == "vendor-session-1"

        handoff = client.post(
            f"/api/v1/chat/sessions/{body['session_id']}/continue-as-mission",
            headers=headers,
            json={"objective": "API mission", "max_duration_seconds": 5},
        )
        assert handoff.status_code == 202, handoff.text
        run_id = handoff.json()["id"]
        for _ in range(100):
            if store.get(AgentRun, run_id).status == RunStatus.COMPLETE:
                break
            time.sleep(0.01)
        assert store.get(AgentRun, run_id).status == RunStatus.COMPLETE

        discussed = client.post(f"/api/v1/runs/{run_id}/discuss", headers=headers)
        assert discussed.status_code == 200
        assert discussed.json()["id"] == body["session_id"]
        assert len(adapter.opens) == 1


def test_harness_export_closes_references_without_machine_credentials(tmp_path):
    store, engagement, profile, mcp, _, runtime = _runtime(tmp_path)
    profile = store.update(
        HarnessProfile,
        profile.id,
        {"auth_mode": "secret_ref", "secret_ref": "env:CODEX_AUTH"},
        expected_revision=profile.revision,
    )
    mcp = store.update(
        McpServerProfile,
        mcp.id,
        {"auth_mode": McpAuthMode.BEARER, "bearer_secret_ref": "env:MCP_TOKEN"},
        expected_revision=mcp.revision,
    )
    session = runtime.create_session(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        mcp_server_ids=[mcp.id],
    )
    destination = tmp_path / "harness-export.nebula.zip"

    manifest = export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )

    assert manifest.entity_counts["harnesses"] == 1
    assert manifest.entity_counts["harness_sessions"] == 1
    assert manifest.entity_counts["mcp_servers"] == 1
    with zipfile.ZipFile(destination) as archive:
        harness = json.loads(archive.read("entities/harnesses.json"))[0]
        server = json.loads(archive.read("entities/mcp_servers.json"))[0]
        exported_session = json.loads(archive.read("entities/harness_sessions.json"))[0]
    assert harness["secret_ref"] is None
    assert server["bearer_secret_ref"] is None
    assert exported_session["id"] == session.id
    assert exported_session["mcp_snapshot"][0]["bearer_secret_ref"] is None


def test_harness_chat_reuses_bounded_knowledge_with_privacy_confirmation(tmp_path):
    store, engagement, profile, _, adapter, runtime = _runtime(tmp_path)
    store.create(
        KnowledgeSource(
            id="knowledge-a",
            engagement_id=engagement.id,
            name="target.txt",
            source_type="text/plain",
            citation="Target notes",
            metadata={
                "chunks": [
                    {
                        "id": "chunk-a",
                        "text": "The relevant harness marker is HARNESS_KNOWLEDGE_443.",
                    }
                ]
            },
        )
    )
    profile = store.update(
        HarnessProfile,
        profile.id,
        {"privacy": {"local_only": False, "permits_sensitive_data": True}},
        expected_revision=profile.revision,
    )
    app = create_app(
        store,
        auth_token="test-token",
        harness_runtime_service=runtime,
    )
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "backend": "harness",
        "engagement_id": engagement.id,
        "harness_profile_id": profile.id,
        "model": "test-model",
        "include_knowledge": True,
        "messages": [{"role": "user", "content": "What harness marker is relevant?"}],
    }

    with TestClient(app) as client:
        blocked = client.post("/api/v1/chat/completions", headers=headers, json=payload)
        assert blocked.status_code == 409
        assert "explicit operator confirmation" in blocked.json()["detail"]

        allowed = client.post(
            "/api/v1/chat/completions",
            headers=headers,
            json={**payload, "allow_cloud_knowledge": True},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["citations"][0]["source_id"] == "knowledge-a"
        assert "HARNESS_KNOWLEDGE_443" in adapter.connections[0].prompts[0]
        messages = [
            item
            for item in store.list_entities(ChatMessage, engagement_id=engagement.id)
            if item.session_id == allowed.json()["session_id"]
        ]
        assert messages[0].content == "What harness marker is relevant?"
        assert messages[-1].citations[0].source_id == "knowledge-a"


def test_remote_harness_mcp_requires_profile_policy_and_turn_confirmation(tmp_path):
    store, engagement, profile, mcp, _, runtime = _runtime(tmp_path)
    profile = store.update(
        HarnessProfile,
        profile.id,
        {"privacy": {"local_only": False, "permits_sensitive_data": True}},
        expected_revision=profile.revision,
    )

    with pytest.raises(HarnessConfigurationError, match="explicit operator"):
        runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Use MCP",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[mcp.id],
        )
    assert store.list_entities(HarnessSession, engagement_id=engagement.id) == []

    chat, _, _ = runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Use MCP",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[mcp.id],
        allow_remote_mcp=True,
    )
    assert chat.harness_session_id
