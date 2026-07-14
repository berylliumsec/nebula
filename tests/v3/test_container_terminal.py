from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from functools import wraps

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.container_terminal import (
    ContainerTerminalError,
    ContainerTerminalExit,
    ContainerTerminalOutput,
    ContainerTerminalPreflightRequest,
    ContainerTerminalService,
    ContainerTerminalStartRequest,
    TERMINAL_PROMPT_COMMAND,
)
from nebula.v3.domain import (
    Engagement,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
)
from nebula.v3.exporter import export_engagement
from nebula.v3.sandbox import (
    PreparedContainerImage,
    SandboxContainerUser,
    SandboxExecutionKind,
    SandboxNetwork,
    SandboxRootFilesystem,
    SandboxWorkspaceAccess,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.terminal_history import Osc633CommandParser, TerminalCommandHistory
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

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.closed = 0
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._chunks = list(chunks or [b"container-only\r\n", b""])

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
    def __init__(self, *, chunks: list[bytes] | None = None) -> None:
        self.requests: list[tuple[object, str, int, int]] = []
        self.processes: list[RecordingTerminalProcess] = []
        self.chunks = chunks

    async def open_terminal(
        self, request, *, container_name: str, columns: int, rows: int
    ) -> RecordingTerminalProcess:
        self.requests.append((request, container_name, columns, rows))
        process = RecordingTerminalProcess(self.chunks)
        process.container_name = container_name
        self.processes.append(process)
        return process


class ControllableTerminalProcess:
    container_name = "nebula-terminal-controllable"

    def __init__(self) -> None:
        self.closed = 0
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._exited = asyncio.Event()
        self._exit_code = 0
        self._loop = asyncio.get_running_loop()

    async def read(self, maximum_bytes: int = 32_768) -> bytes:
        assert maximum_bytes <= 32_768
        value = await self._chunks.get()
        return b"" if value is None else value

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    def resize(self, columns: int, rows: int) -> None:
        if columns < 1 or rows < 1:
            raise ValueError("terminal dimensions must be positive")
        self.resizes.append((columns, rows))

    async def wait(self) -> int:
        await self._exited.wait()
        return self._exit_code

    async def emit(self, data: bytes) -> None:
        await self._chunks.put(data)

    def emit_from_thread(self, data: bytes) -> None:
        asyncio.run_coroutine_threadsafe(self.emit(data), self._loop).result(timeout=1)

    async def exit(self, exit_code: int) -> None:
        self._exit_code = exit_code
        await self._chunks.put(None)
        self._exited.set()

    async def close(self) -> None:
        self.closed += 1
        if not self._exited.is_set():
            self._exit_code = -15
            await self._chunks.put(None)
            self._exited.set()


class ControllableTerminalRunner:
    def __init__(self) -> None:
        self.requests: list[tuple[object, str, int, int]] = []
        self.processes: list[ControllableTerminalProcess] = []

    async def open_terminal(
        self, request, *, container_name: str, columns: int, rows: int
    ) -> ControllableTerminalProcess:
        self.requests.append((request, container_name, columns, rows))
        process = ControllableTerminalProcess()
        process.container_name = container_name
        self.processes.append(process)
        return process


class RecordingExecutionLocks:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.locks: dict[str, asyncio.Lock] = {}

    def engagement_lock(self, engagement_id: str) -> asyncio.Lock:
        return self.locks.setdefault(engagement_id, asyncio.Lock())


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


def fixture(
    tmp_path, *, fail_image: bool = False, chunks: list[bytes] | None = None
):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Container Terminal Lab"))
    runner = RecordingTerminalRunner(chunks=chunks)
    platform = StubTerminalPlatform(
        tmp_path / "workspace", runner, fail_image=fail_image
    )
    service = ContainerTerminalService(
        store=store,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    return store, engagement, None, runner, platform, service


def continuity_fixture(tmp_path, **service_options):
    store = NebulaStore(tmp_path / "continuity.db")
    engagement = store.create(Engagement(name="Terminal Continuity Lab"))
    runner = ControllableTerminalRunner()
    platform = StubTerminalPlatform(
        tmp_path / "continuity-workspace",
        runner,  # type: ignore[arg-type]
    )
    service = ContainerTerminalService(
        store=store,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
        **service_options,
    )
    return store, engagement, runner, platform, service


async def start_controllable_terminal(
    service: ContainerTerminalService,
    engagement: Engagement,
):
    request = ContainerTerminalPreflightRequest(engagement_id=engagement.id)
    preview = await service.preflight(request)
    assert preview.preview_token is not None
    assert preview.preview_fingerprint is not None
    started = await service.start(
        ContainerTerminalStartRequest(
            **request.model_dump(),
            preview_token=preview.preview_token,
            preview_fingerprint=preview.preview_fingerprint,
            client_idempotency_key="continuity-terminal",
        )
    )
    attachment = await service.attach(
        started.session_id,
        started.websocket_ticket,
    )
    return started, attachment


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
    assert service.workspace_lock(engagement.id).locked() is False
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
        "HISTFILE": "/dev/null",
        "LANG": "C.UTF-8",
        "PROMPT_COMMAND": TERMINAL_PROMPT_COMMAND,
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


@async_test
async def test_terminal_process_survives_detach_and_replays_only_missed_output(
    tmp_path,
):
    _store, engagement, runner, _platform, service = continuity_fixture(tmp_path)
    started, first = await start_controllable_terminal(service, engagement)
    process = runner.processes[0]

    with pytest.raises(ContainerTerminalError, match="active WebSocket"):
        await service.attach(
            started.session_id,
            first.reconnect_ticket,
        )

    await process.emit(b"before disconnect\r\n")
    initial = await asyncio.wait_for(service.next_event(first), timeout=1)
    assert initial == ContainerTerminalOutput(1, b"before disconnect\r\n")
    reconnect_ticket = first.reconnect_ticket
    await service.detach(first)
    assert process.closed == 0
    assert service.workspace_lock(engagement.id).locked() is False

    await process.emit(b"while detached\r\n")
    second = await service.attach(
        started.session_id,
        reconnect_ticket,
        after_sequence=initial.sequence,
    )
    replayed = await asyncio.wait_for(service.next_event(second), timeout=1)
    assert replayed == ContainerTerminalOutput(2, b"while detached\r\n")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(service.next_event(second), timeout=0.01)

    await process.emit(b"live again\r\n")
    live = await asyncio.wait_for(service.next_event(second), timeout=1)
    assert live == ContainerTerminalOutput(3, b"live again\r\n")
    assert len(runner.processes) == 1

    await service.close_attachment(second)
    assert process.closed == 1
    assert service.workspace_lock(engagement.id).locked() is False


@async_test
async def test_active_terminal_does_not_hold_the_reviewed_execution_lock(tmp_path):
    store = NebulaStore(tmp_path / "shared-workspace.db")
    engagement = store.create(Engagement(name="Concurrent Reviewed Execution Lab"))
    runner = ControllableTerminalRunner()
    platform = StubTerminalPlatform(
        tmp_path / "shared-workspace",
        runner,  # type: ignore[arg-type]
    )
    execution_locks = RecordingExecutionLocks(store)
    service = ContainerTerminalService(
        store=store,
        tool_platform=platform,  # type: ignore[arg-type]
        execution_service=execution_locks,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    _started, attachment = await start_controllable_terminal(service, engagement)

    async def reviewed_execution() -> str:
        async with execution_locks.engagement_lock(engagement.id):
            return "executed"

    assert await asyncio.wait_for(reviewed_execution(), timeout=0.1) == "executed"
    assert runner.processes[0].closed == 0
    with pytest.raises(ContainerTerminalError, match="workspace cannot be changed"):
        async with service.guard_workspace_operation(engagement.id):
            pytest.fail("destructive workspace guard admitted an active terminal")

    await service.close_attachment(attachment)


@async_test
async def test_terminal_recovery_rotates_one_use_tickets_without_starting_a_process(
    tmp_path,
):
    _store, engagement, runner, platform, service = continuity_fixture(tmp_path)
    assert (await service.recover(engagement.id)).active is False

    started, attachment = await start_controllable_terminal(service, engagement)
    original_reconnect_ticket = attachment.reconnect_ticket
    first_recovery = await service.recover(engagement.id)
    second_recovery = await service.recover(engagement.id)
    assert first_recovery.active is True
    assert first_recovery.session is not None
    assert first_recovery.runtime is not None
    assert first_recovery.runtime.image_digest == platform.image.digest
    assert first_recovery.session.session_id == started.session_id
    assert first_recovery.session.last_sequence == 0
    assert second_recovery.session is not None
    assert first_recovery.session.websocket_ticket != (
        second_recovery.session.websocket_ticket
    )
    assert len(runner.processes) == 1
    with pytest.raises(ContainerTerminalError, match="active WebSocket"):
        await service.attach(
            started.session_id,
            second_recovery.session.websocket_ticket,
        )
    assert len(runner.processes) == 1

    await service.detach(attachment)
    for stale_ticket in (
        original_reconnect_ticket,
        first_recovery.session.websocket_ticket,
    ):
        with pytest.raises(ContainerTerminalError, match="invalid"):
            await service.attach(started.session_id, stale_ticket)

    recovered = await service.attach(
        started.session_id,
        second_recovery.session.websocket_ticket,
    )
    assert len(runner.processes) == 1
    await service.close_attachment(recovered)
    assert (await service.recover(engagement.id)).active is False


@async_test
async def test_terminal_replay_is_memory_bounded_and_marks_truncation(tmp_path):
    _store, engagement, runner, _platform, service = continuity_fixture(
        tmp_path,
        replay_max_bytes=8,
    )
    started, first = await start_controllable_terminal(service, engagement)
    process = runner.processes[0]

    await process.emit(b"12345678")
    assert await asyncio.wait_for(service.next_event(first), timeout=1) == (
        ContainerTerminalOutput(1, b"12345678")
    )
    await process.emit(b"abcdefgh")
    assert await asyncio.wait_for(service.next_event(first), timeout=1) == (
        ContainerTerminalOutput(2, b"abcdefgh")
    )
    reconnect_ticket = first.reconnect_ticket
    await service.detach(first)

    second = await service.attach(
        started.session_id,
        reconnect_ticket,
        after_sequence=0,
    )
    assert second.replay_truncated is True
    assert second.oldest_sequence == 2
    assert second.latest_sequence == 2
    assert await asyncio.wait_for(service.next_event(second), timeout=1) == (
        ContainerTerminalOutput(2, b"abcdefgh")
    )
    await service.close_attachment(second)


@async_test
async def test_terminal_disconnect_grace_expires_and_releases_workspace(tmp_path):
    store, engagement, runner, _platform, service = continuity_fixture(
        tmp_path,
        reconnect_grace_seconds=0.03,
    )
    started, attachment = await start_controllable_terminal(service, engagement)
    await service.detach(attachment)
    await asyncio.sleep(0.08)

    assert runner.processes[0].closed == 1
    assert service.workspace_lock(engagement.id).locked() is False
    assert await service.engagement_active(engagement.id) is False
    events = store.replay_operation_events(started.session_id)
    assert events[-1].payload["status"] == "reconnect_timeout"


@async_test
async def test_terminal_idle_timeout_is_based_on_input_and_output_activity(tmp_path):
    _store, engagement, runner, _platform, service = continuity_fixture(
        tmp_path,
        idle_timeout_seconds=0.03,
        watchdog_interval_seconds=0.005,
    )
    _started, attachment = await start_controllable_terminal(service, engagement)
    event = await asyncio.wait_for(service.next_event(attachment), timeout=1)

    assert event == ContainerTerminalExit(
        outcome="idle_timeout",
        error_code="idle_timeout",
        detail="terminal closed after 30 minutes without input or output",
    )
    await asyncio.sleep(0)
    assert runner.processes[0].closed == 1
    assert service.workspace_lock(engagement.id).locked() is False


@async_test
async def test_terminal_reconnects_one_hundred_times_without_new_processes_or_tasks(
    tmp_path,
):
    _store, engagement, runner, _platform, service = continuity_fixture(
        tmp_path,
        reconnect_grace_seconds=1,
    )
    started, attachment = await start_controllable_terminal(service, engagement)
    for _index in range(100):
        reconnect_ticket = attachment.reconnect_ticket
        await service.detach(attachment)
        attachment = await service.attach(
            started.session_id,
            reconnect_ticket,
            after_sequence=0,
        )

    assert len(runner.processes) == 1
    assert runner.processes[0].closed == 0
    await service.close_attachment(attachment)
    await asyncio.sleep(0)
    assert runner.processes[0].closed == 1
    assert service.workspace_lock(engagement.id).locked() is False
    leaked = [
        task.get_name()
        for task in asyncio.all_tasks()
        if started.session_id in task.get_name() and not task.done()
    ]
    assert leaked == []


@async_test
async def test_terminal_process_exit_is_delivered_and_cleans_up(tmp_path):
    _store, engagement, runner, _platform, service = continuity_fixture(tmp_path)
    _started, attachment = await start_controllable_terminal(service, engagement)
    process = runner.processes[0]
    await process.emit(b"last output")
    await process.emit(b"still buffered")
    await process.exit(23)
    assert await asyncio.wait_for(service.next_event(attachment), timeout=1) == (
        ContainerTerminalOutput(1, b"last output")
    )
    assert await asyncio.wait_for(service.next_event(attachment), timeout=1) == (
        ContainerTerminalOutput(2, b"still buffered")
    )
    terminal = await asyncio.wait_for(service.next_event(attachment), timeout=1)
    assert terminal == ContainerTerminalExit(outcome="completed", exit_code=23)
    await asyncio.sleep(0)
    assert process.closed == 1
    assert service.workspace_lock(engagement.id).locked() is False


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
        uploaded = client.put(
            f"/api/v1/engagements/{engagement.id}/workspace/file",
            headers={**headers, "Content-Type": "application/octet-stream"},
            params={"path": "uploaded-while-terminal-runs.txt"},
            content=b"available immediately\n",
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["sha256"] == hashlib.sha256(
            b"available immediately\n"
        ).hexdigest()
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


def test_container_terminal_websocket_reconnects_to_the_same_process(tmp_path):
    store, engagement, runner, _platform, service = continuity_fixture(tmp_path)
    app = create_app(
        store,
        auth_token="test-token",
        container_terminal_service=service,
    )
    headers = {"Authorization": "Bearer test-token"}
    encoded_token = base64.urlsafe_b64encode(b"test-token").decode().rstrip("=")

    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/container-terminal/preflight",
            headers=headers,
            json={"engagement_id": engagement.id},
        ).json()
        session = client.post(
            "/api/v1/container-terminal/sessions",
            headers=headers,
            json={
                "engagement_id": engagement.id,
                "preview_token": preview["preview_token"],
                "preview_fingerprint": preview["preview_fingerprint"],
                "client_idempotency_key": "api-reconnect",
            },
        ).json()
        initial_protocols = [
            "nebula.container-terminal.v1",
            f"nebula.auth.{encoded_token}",
            f"nebula.ticket.{session['websocket_ticket']}",
        ]
        with client.websocket_connect(
            session["websocket_path"], subprotocols=initial_protocols
        ) as socket:
            ready = socket.receive_json()
            assert ready["type"] == "ready"
            original_reconnect_ticket = ready["reconnect_ticket"]
            runner.processes[0].emit_from_thread(b"visible before navigation\r\n")
            displayed = socket.receive_json()
            assert displayed["sequence"] == 1
            assert base64.b64decode(displayed["data"]) == (
                b"visible before navigation\r\n"
            )

        time.sleep(0.05)
        assert len(runner.processes) == 1
        assert runner.processes[0].closed == 0
        unauthenticated = client.post(
            f"/api/v1/engagements/{engagement.id}/container-terminal/recover"
        )
        assert unauthenticated.status_code == 401
        recovery_response = client.post(
            f"/api/v1/engagements/{engagement.id}/container-terminal/recover",
            headers=headers,
        )
        assert recovery_response.status_code == 200
        assert recovery_response.headers["cache-control"] == "private, no-store"
        recovery = recovery_response.json()
        assert recovery["active"] is True
        assert recovery["session"]["session_id"] == session["session_id"]
        assert recovery["session"]["last_sequence"] == 0
        assert recovery["session"]["websocket_ticket"] != (
            original_reconnect_ticket
        )
        assert recovery["runtime"]["image_digest"] == (
            "sha256:" + "c" * 64
        )
        runner.processes[0].emit_from_thread(b"missed while view changed\r\n")
        reconnect_protocols = [
            "nebula.container-terminal.v1",
            f"nebula.auth.{encoded_token}",
            f"nebula.ticket.{recovery['session']['websocket_ticket']}",
        ]
        with client.websocket_connect(
            session["websocket_path"] + "?after_sequence=0",
            subprotocols=reconnect_protocols,
        ) as socket:
            reconnect_ready = socket.receive_json()
            assert reconnect_ready["type"] == "ready"
            replayed = socket.receive_json()
            assert replayed["type"] == "output"
            assert replayed["sequence"] == 1
            assert base64.b64decode(replayed["data"]) == (
                b"visible before navigation\r\n"
            )
            missed = socket.receive_json()
            assert missed["type"] == "output"
            assert missed["sequence"] == 2
            assert base64.b64decode(missed["data"]) == (
                b"missed while view changed\r\n"
            )
            socket.send_json({"type": "close"})
            assert socket.receive_json() == {
                "type": "exit",
                "exit_code": None,
                "outcome": "closed",
            }

        inactive = client.post(
            f"/api/v1/engagements/{engagement.id}/container-terminal/recover",
            headers=headers,
        )
        assert inactive.json() == {
            "active": False,
            "session": None,
            "runtime": None,
        }

    assert len(runner.processes) == 1
    assert runner.processes[0].closed == 1


def _command_marker(
    command: str, *, cwd: str = "/workspace", exit_code: int = 0
) -> bytes:
    return (
        b"\x1b]633;NebulaCommand;"
        + str(exit_code).encode()
        + b";"
        + base64.b64encode(cwd.encode())
        + b";"
        + base64.b64encode(command.encode())
        + b"\x07"
    )


@async_test
async def test_command_parser_and_history_span_websocket_reconnects(tmp_path):
    database = tmp_path / "continuity.db"
    history_store = NebulaStore(database)
    engagement = history_store.create(Engagement(name="History Continuity Lab"))
    runner = ControllableTerminalRunner()
    platform = StubTerminalPlatform(
        tmp_path / "continuity-workspace",
        runner,  # type: ignore[arg-type]
    )
    history = TerminalCommandHistory(history_store.database)
    service = ContainerTerminalService(
        store=history_store,
        tool_platform=platform,  # type: ignore[arg-type]
        command_history=history,
        operator_id=lambda: "operator-1",
    )
    started, first = await start_controllable_terminal(service, engagement)
    process = runner.processes[0]
    marker = _command_marker("printf reconnect", exit_code=4)
    output_secret = b"continuity-output-must-stay-in-memory"

    await process.emit(output_secret + marker[:19])
    assert await asyncio.wait_for(service.next_event(first), timeout=1) == (
        ContainerTerminalOutput(1, output_secret)
    )
    reconnect_ticket = first.reconnect_ticket
    await service.detach(first)
    second = await service.attach(
        started.session_id,
        reconnect_ticket,
        after_sequence=1,
    )
    await process.emit(marker[19:] + b"prompt$ ")
    assert await asyncio.wait_for(service.next_event(second), timeout=1) == (
        ContainerTerminalOutput(2, b"prompt$ ")
    )

    for _attempt in range(20):
        records = history.list(engagement.id).records
        if records:
            break
        await asyncio.sleep(0.01)
    assert [(item.command, item.cwd, item.exit_code) for item in records] == [
        ("printf reconnect", "/workspace", 4)
    ]
    assert all(
        output_secret not in path.read_bytes()
        for path in database.parent.glob(database.name + "*")
        if path.is_file()
    )
    await service.close_attachment(second)


def test_terminal_stream_strips_split_markers_and_persists_only_command_metadata(
    tmp_path,
):
    command = "  printf 'history only'"
    marker = _command_marker(command, exit_code=9)
    malformed = b"\x1b]633;NebulaCommand;invalid;@@@;@@@\x07"
    output_secret = b"terminal-output-must-not-persist"
    chunks = [
        output_secret + b"\r\n" + marker[:8],
        marker[8:31],
        marker[31:] + malformed[:12],
        malformed[12:] + b"\r\nprompt$ ",
        b"",
    ]
    store, engagement, _policy, _runner, platform, service = fixture(
        tmp_path,
        chunks=chunks,
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = WorkspaceService(
        store=store,
        artifact_store=artifacts,
        tool_platform=platform,  # type: ignore[arg-type]
        operator_id=lambda: "operator-1",
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="test-token",
        container_terminal_service=service,
        workspace_service=workspace,
    )
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        preview = client.post(
            "/api/v1/container-terminal/preflight",
            headers=headers,
            json={"engagement_id": engagement.id},
        ).json()
        started = client.post(
            "/api/v1/container-terminal/sessions",
            headers=headers,
            json={
                "engagement_id": engagement.id,
                "preview_token": preview["preview_token"],
                "preview_fingerprint": preview["preview_fingerprint"],
                "client_idempotency_key": "history-stream",
            },
        ).json()
        encoded_token = base64.urlsafe_b64encode(b"test-token").decode().rstrip("=")
        protocols = [
            "nebula.container-terminal.v1",
            f"nebula.auth.{encoded_token}",
            f"nebula.ticket.{started['websocket_ticket']}",
        ]
        displayed = bytearray()
        with client.websocket_connect(
            started["websocket_path"], subprotocols=protocols
        ) as socket:
            assert socket.receive_json()["type"] == "ready"
            while True:
                message = socket.receive_json()
                if message["type"] == "output":
                    displayed.extend(base64.b64decode(message["data"]))
                    continue
                assert message == {
                    "type": "exit",
                    "exit_code": 7,
                    "outcome": "completed",
                }
                break

        assert bytes(displayed) == (
            output_secret + b"\r\n" + malformed + b"\r\nprompt$ "
        )
        history = client.get(
            f"/api/v1/engagements/{engagement.id}/terminal/commands",
            headers=headers,
        )
        assert history.status_code == 200
        assert history.json()["total"] == 1
        assert history.json()["records"][0] == {
            "id": history.json()["records"][0]["id"],
            "engagement_id": engagement.id,
            "session_id": started["session_id"],
            "command": command,
            "cwd": "/workspace",
            "exit_code": 9,
            "occurred_at": history.json()["records"][0]["occurred_at"],
        }

    destination = tmp_path / "stream-project.nebula.zip"
    export_engagement(
        engagement_id=engagement.id,
        destination=destination,
        store=store,
        artifact_store=artifacts,
    )
    with zipfile.ZipFile(destination) as archive:
        archived_payloads = [archive.read(name) for name in archive.namelist()]
    assert all(output_secret not in payload for payload in archived_payloads)
    assert all(command.encode() not in payload for payload in archived_payloads)


@pytest.mark.skipif(
    any(shutil.which(program) is None for program in ("bash", "base64", "tr")),
    reason="bash, base64, and tr are required for shell integration coverage",
)
def test_fixed_bash_prompt_hook_emits_completed_command_markers_only():
    environment = {
        **os.environ,
        "HISTFILE": "/dev/null",
        "PROMPT_COMMAND": TERMINAL_PROMPT_COMMAND,
        "PS1": "",
    }
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-i"],
        input=b"  printf 'shell-hook\\n'\nfalse\nexit\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        check=False,
        timeout=10,
    )
    parser = Osc633CommandParser()
    parsed = parser.feed(completed.stdout)
    tail = parser.flush()

    assert [(record.command, record.exit_code) for record in parsed.records] == [
        ("  printf 'shell-hook\\n'", 0),
        ("false", 1),
    ]
    assert b"NebulaCommand" not in parsed.passthrough + tail.passthrough
