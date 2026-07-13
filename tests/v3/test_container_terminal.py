from __future__ import annotations

import asyncio
import base64
from functools import wraps

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.container_terminal import (
    ContainerTerminalError,
    ContainerTerminalPreflightRequest,
    ContainerTerminalService,
    ContainerTerminalStartRequest,
)
from nebula.v3.domain import (
    Engagement,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
)
from nebula.v3.sandbox import (
    PreparedContainerImage,
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxNetwork,
    SandboxRootFilesystem,
    SandboxWorkspaceAccess,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_platform import (
    HumanTerminalRuntimeResolution,
    ToolPlatformError,
)
from nebula.v3.workspace import WorkspaceService


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class RecordingTerminalProcess:
    container_name = "nebula-terminal-recording"

    def __init__(self) -> None:
        self.closed = 0
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._chunks = [b"container-only\r\n", b""]

    async def read(self, maximum_bytes: int = 32_768) -> bytes:
        assert maximum_bytes <= 32_768
        await asyncio.sleep(0)
        return self._chunks.pop(0)

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    def resize(self, columns: int, rows: int) -> None:
        self.resizes.append((columns, rows))

    async def wait(self) -> int:
        await asyncio.sleep(0.02)
        return 7

    async def close(self) -> None:
        self.closed += 1


class RecordingTerminalRunner:
    def __init__(self) -> None:
        self.requests: list[tuple[object, str, int, int]] = []
        self.processes: list[RecordingTerminalProcess] = []

    async def open_terminal(
        self, request, *, container_name: str, columns: int, rows: int
    ) -> RecordingTerminalProcess:
        self.requests.append((request, container_name, columns, rows))
        process = RecordingTerminalProcess()
        process.container_name = container_name
        self.processes.append(process)
        return process


class StubTerminalPlatform:
    execution_enabled = True

    def __init__(
        self, workspace, runner: RecordingTerminalRunner, *, fail_image: bool = False
    ) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True)
        self.runner = runner
        self.fail_image = fail_image
        self.cleanup_calls = 0
        self.profile = RunnerProfile(
            id="runner-1",
            name="Rootless Podman",
            runtime=RunnerRuntime.PODMAN,
            executable="/usr/bin/podman",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            enabled=True,
            healthy=True,
        )
        self.image = PreparedContainerImage(
            source_reference="docker.io/kalilinux/kali-rolling:latest",
            base_resolved_reference=(
                "docker.io/kalilinux/kali-rolling@sha256:" + "b" * 64
            ),
            base_digest="sha256:" + "b" * 64,
            resolved_reference="sha256:" + "c" * 64,
            digest="sha256:" + "c" * 64,
            platform="linux/amd64",
            configured_user="",
            installed_packages=("kali-linux-headless", "iputils-ping"),
            refreshed=True,
            detail="pulled and verified the latest official Kali image",
        )

    async def cleanup_operator_terminals(self) -> None:
        self.cleanup_calls += 1

    def workspace_for(self, engagement_id: str):
        del engagement_id
        return self.workspace

    def resolve_human_terminal_profile(self, engagement_id: str) -> RunnerProfile:
        del engagement_id
        return self.profile

    async def resolve_human_terminal_runtime(
        self, engagement_id: str
    ) -> HumanTerminalRuntimeResolution:
        del engagement_id
        if self.fail_image:
            raise ToolPlatformError("registry unavailable and no cached Kali image")
        return HumanTerminalRuntimeResolution(
            profile=self.profile,
            runner=self.runner,  # type: ignore[arg-type]
            workspace=self.workspace,
            image=self.image,
        )


def fixture(tmp_path, *, fail_image: bool = False):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Container Terminal Lab"))
    runner = RecordingTerminalRunner()
    platform = StubTerminalPlatform(
        tmp_path / "workspace", runner, fail_image=fail_image
    )
    service = ContainerTerminalService(
        store=store,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    return store, engagement, None, runner, platform, service


@async_test
async def test_reviewed_terminal_uses_only_the_fixed_container_shell(tmp_path):
    store, engagement, _policy, runner, platform, service = fixture(tmp_path)
    store.append_operation_event(
        "orphaned-terminal",
        "container_terminal",
        engagement.id,
        "container_terminal.running",
        {"status": "running"},
        actor_id="operator-1",
    )
    await service.startup()
    assert platform.cleanup_calls == 1
    recovered = store.replay_operation_events("orphaned-terminal")
    assert recovered[-1].event_type == "container_terminal.terminal"
    assert recovered[-1].payload["status"] == "interrupted"
    capabilities = service.capabilities(engagement.id)
    assert capabilities.ready is True
    assert capabilities.source_image == "docker.io/kalilinux/kali-rolling:latest"
    assert capabilities.installed_packages == [
        "kali-linux-headless",
        "iputils-ping",
    ]
    assert capabilities.network.mode == "unrestricted"
    assert capabilities.network.runtime_network == "bridge"
    assert capabilities.security.container_user == "root"
    assert capabilities.security.linux_capabilities == []
    assert capabilities.security.host_network is False
    assert capabilities.security.runtime_socket is False

    request = ContainerTerminalPreflightRequest(engagement_id=engagement.id)
    preview = await service.preflight(request)
    assert preview.allowed is True
    assert preview.runtime is not None
    assert preview.runtime.source_image == capabilities.source_image
    assert preview.runtime.base_image == platform.image.base_resolved_reference
    assert preview.runtime.base_image_digest == platform.image.base_digest
    assert preview.runtime.image == platform.image.resolved_reference
    assert preview.runtime.image_digest == platform.image.digest
    assert preview.runtime.installed_packages == list(platform.image.installed_packages)
    assert preview.runtime.interpreter == "/bin/bash"
    assert preview.runtime.arguments == ["--noprofile", "--norc", "-i"]
    assert preview.network.mode == "unrestricted"
    assert preview.network.published_ports == []
    assert preview.security.root_filesystem == "writable"
    assert preview.security.no_new_privileges is True
    assert preview.security.host_shell is False
    assert preview.fresh_container is True
    assert preview.preview_token is not None
    assert preview.preview_fingerprint is not None

    start = ContainerTerminalStartRequest(
        **request.model_dump(),
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        client_idempotency_key="terminal-attempt-1",
    )
    created = await service.start(start)
    retry = await service.start(start)
    assert retry.session_id == created.session_id
    assert service.workspace_lock(engagement.id).locked() is True
    with pytest.raises(ContainerTerminalError, match="invalid"):
        await service.claim(created.session_id, "wrong-ticket")
    await service.claim(created.session_id, created.websocket_ticket)
    with pytest.raises(ContainerTerminalError, match="already been used"):
        await service.claim(created.session_id, created.websocket_ticket)

    process = await service.launch(created.session_id)
    assert process is runner.processes[0]
    sandbox_request, name, columns, rows = runner.requests[0]
    assert name.startswith("nebula-terminal-")
    assert (columns, rows) == (100, 30)
    assert sandbox_request.command == [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-i",
    ]
    assert sandbox_request.workspace == platform.workspace
    assert sandbox_request.workspace_access == SandboxWorkspaceAccess.WRITE
    assert sandbox_request.image == platform.image.resolved_reference
    assert sandbox_request.network == SandboxNetwork.UNRESTRICTED
    assert sandbox_request.execution_kind == SandboxExecutionKind.HUMAN_TERMINAL
    assert sandbox_request.container_user == SandboxContainerUser.ROOT
    assert sandbox_request.root_filesystem == SandboxRootFilesystem.WRITABLE
    assert sandbox_request.environment == {
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
    }
    assert sandbox_request.limits.cpu_count == 1
    assert sandbox_request.limits.memory_mb == 512
    assert sandbox_request.limits.pids == 128

    await service.finish(created.session_id, outcome="completed", exit_code=0)
    assert service.workspace_lock(engagement.id).locked() is False
    assert runner.processes[0].closed == 1
    events = store.replay_operation_events(created.session_id)
    assert [event.event_type for event in events] == [
        "container_terminal.pending",
        "container_terminal.claimed",
        "container_terminal.running",
        "container_terminal.terminal",
    ]
    assert all(event.operation_kind == "container_terminal" for event in events)
    pending = events[0].payload
    assert pending["runtime"]["image"] == platform.image.resolved_reference
    assert pending["runtime"]["base_image"] == platform.image.base_resolved_reference
    assert pending["runtime"]["installed_packages"] == [
        "kali-linux-headless",
        "iputils-ping",
    ]
    assert pending["network"] == {
        "mode": "unrestricted",
        "published_ports": [],
        "runtime_network": "bridge",
    }
    assert pending["security"]["container_user"] == "root"
    await service.shutdown()


@async_test
async def test_unrestricted_kali_terminal_needs_no_toolbox_or_scope_policy(tmp_path):
    store = NebulaStore(tmp_path / "default-terminal.db")
    engagement = store.create(Engagement(name="Default Terminal Lab"))
    runner = RecordingTerminalRunner()
    platform = StubTerminalPlatform(tmp_path / "workspace", runner)
    service = ContainerTerminalService(
        store=store,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    request = ContainerTerminalPreflightRequest(engagement_id=engagement.id)

    preview = await service.preflight(request)

    assert preview.allowed is True
    assert preview.policy_rule == "human_terminal_unrestricted"
    assert preview.network.mode == "unrestricted"
    start = ContainerTerminalStartRequest(
        **request.model_dump(),
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        client_idempotency_key="default-terminal",
    )
    created = await service.start(start)
    await service.claim(created.session_id, created.websocket_ticket)
    await service.launch(created.session_id)
    assert runner.requests[0][0].network == SandboxNetwork.UNRESTRICTED
    await service.finish(created.session_id, outcome="closed")


def test_terminal_request_rejects_client_selected_network_boundary():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContainerTerminalPreflightRequest(
            engagement_id="engagement-1",
            network={"mode": "none", "ports": []},  # type: ignore[call-arg]
        )


@async_test
async def test_terminal_reports_image_unavailable_without_verified_cache(tmp_path):
    _store, engagement, _unused, _runner, _platform, service = fixture(
        tmp_path, fail_image=True
    )
    preview = await service.preflight(
        ContainerTerminalPreflightRequest(engagement_id=engagement.id)
    )
    assert preview.allowed is False
    assert preview.error_code == "image_unavailable"
    assert "no cached Kali image" in preview.detail


def test_container_terminal_api_streams_container_output_with_one_use_ticket(tmp_path):
    store, engagement, _policy, runner, platform, service = fixture(tmp_path)
    workspace = WorkspaceService(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    app = create_app(
        store,
        auth_token="test-token",
        container_terminal_service=service,
        workspace_service=workspace,
    )
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        health = client.get("/api/v1/health", headers=headers)
        assert health.json()["human_pty"] == "unavailable"
        assert health.json()["container_terminal"] == "configured"
        preview = client.post(
            "/api/v1/container-terminal/preflight",
            headers=headers,
            json={
                "engagement_id": engagement.id,
                "columns": 90,
                "rows": 24,
            },
        )
        assert preview.status_code == 200
        reviewed = preview.json()
        assert reviewed["runtime"]["image"] == platform.image.resolved_reference
        assert reviewed["network"] == {
            "mode": "unrestricted",
            "runtime_network": "bridge",
            "published_ports": [],
        }
        assert reviewed["security"]["container_user"] == "root"
        started = client.post(
            "/api/v1/container-terminal/sessions",
            headers=headers,
            json={
                "engagement_id": engagement.id,
                "columns": 90,
                "rows": 24,
                "preview_token": reviewed["preview_token"],
                "preview_fingerprint": reviewed["preview_fingerprint"],
                "client_idempotency_key": "api-terminal",
            },
        )
        assert started.status_code == 201
        session = started.json()
        blocked_reset = client.post(
            f"/api/v1/engagements/{engagement.id}/workspace/reset",
            headers=headers,
            json={"engagement_name": engagement.name},
        )
        assert blocked_reset.status_code == 409
        assert blocked_reset.json()["code"] == "workspace_busy"
        encoded_token = base64.urlsafe_b64encode(b"test-token").decode().rstrip("=")
        protocols = [
            "nebula.container-terminal.v1",
            f"nebula.auth.{encoded_token}",
            f"nebula.ticket.{session['websocket_ticket']}",
        ]
        with client.websocket_connect(
            session["websocket_path"], subprotocols=protocols
        ) as socket:
            assert socket.receive_json()["type"] == "ready"
            output = socket.receive_json()
            assert output["type"] == "output"
            assert base64.b64decode(output["data"]) == b"container-only\r\n"
            terminal = socket.receive_json()
            assert terminal == {
                "type": "exit",
                "exit_code": 7,
                "outcome": "completed",
            }
    assert runner.processes[0].closed == 1
