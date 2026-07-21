import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3 import automation_runtime
from nebula.v3.automation_runtime import (
    AutomationRuntimeManager,
    AutomationPolicyDenied,
    CommandApprovalRequired,
    ContainerRuntimeSession,
    ProcessIORequest,
    RunCommandRequest,
    RuntimeBackendProcess,
    RuntimeBackendSession,
)
from nebula.v3.automation_tools import AutomationBroker, PROCESS_IO_NAME
from nebula.v3.domain import (
    Approval,
    ApprovalStatus,
    AutomationApprovalPolicy,
    AutomationNetworkMode,
    AutomationSession,
    AutomationSessionStatus,
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    RiskClass,
    utc_now,
)
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import (
    ToolOutputAccessError,
    ToolOutputQueryError,
    ToolOutputService,
    WorkspaceOutputService,
    sanitize_model_history_result,
)


IMAGE = "registry.invalid/nebula-automation@sha256:" + "a" * 64


class FakeProcess(RuntimeBackendProcess):
    def __init__(self, output: bytes, *, stays_running: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.writes: list[bytes] = []
        self._done: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.stdout.feed_data(output)
        if not stays_running:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._done.set_result(0)

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def wait(self) -> int:
        return await self._done

    async def terminate(self) -> None:
        if not self._done.done():
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._done.set_result(-15)


class FakeSession(RuntimeBackendSession):
    def __init__(self, *, network_granted: bool, workspace: Path) -> None:
        self._network_enabled = network_granted
        self.workspace = workspace
        self.enable_count = 0
        self.closed = False
        self.processes: list[FakeProcess] = []

    @property
    def network_enabled(self) -> bool:
        return self._network_enabled

    async def enable_network(self) -> None:
        self._network_enabled = True
        self.enable_count += 1

    async def run(self, process_id: str, command: str, cwd: str) -> FakeProcess:
        del process_id
        if command == "write-generated-file":
            (self.workspace / cwd / "generated.txt").write_text(
                "generated", encoding="utf-8"
            )
        output = (
            b"\x00\xffbinary-output-continues"
            if command == "binary-output"
            else f"cwd={cwd} command={command}\n".encode()
        )
        process = FakeProcess(
            output,
            stays_running=command == "wait-forever",
        )
        self.processes.append(process)
        return process

    async def close(self) -> None:
        self.closed = True
        for process in self.processes:
            await process.terminate()


def runtime(tmp_path: Path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Runtime test"))
    scope = store.create(
        ScopePolicy(
            id=f"scope:{engagement.id}",
            engagement_id=engagement.id,
            allowed_cidrs=["203.0.113.0/24"],
            allowed_ports=[443],
        )
    )
    engagement = store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": scope.id},
        expected_revision=engagement.revision,
    )
    store.create(
        RunnerProfile(
            id="runner",
            name="Docker",
            runtime=RunnerRuntime.DOCKER,
            executable="/usr/bin/docker",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )
    workspace = tmp_path / "workspaces" / engagement.id
    workspace.mkdir(parents=True)
    sessions: list[FakeSession] = []

    async def launch(configuration):
        session = FakeSession(
            network_granted=configuration.network_granted,
            workspace=configuration.workspace,
        )
        sessions.append(session)
        return session

    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=artifacts,
        data_root=tmp_path,
        workspace_resolver=lambda _engagement_id: workspace,
        runtime_image=IMAGE,
        session_factory=launch,
    )
    return manager, store, artifacts, engagement, sessions


def test_general_command_reuses_session_and_persists_artifacts(tmp_path):
    async def scenario():
        manager, store, artifacts, engagement, sessions = runtime(tmp_path)

        first = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id="chat-1",
            request=RunCommandRequest(command="rg needle .", cwd="."),
        )
        second = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="chat",
            owner_id="chat-1",
            request=RunCommandRequest(command="python -V", cwd="."),
        )

        assert len(sessions) == 1
        assert first.session_id == second.session_id
        assert first.status == CommandExecutionStatus.COMPLETED
        assert "rg needle" in first.stdout
        execution = store.get(CommandExecution, first.execution_id)
        assert execution.command_sha256
        assert execution.stdout_artifact_id
        assert execution.redacted_stdout_artifact_id
        assert artifacts.read(
            store.get_by_kind("artifacts", execution.stdout_artifact_id)
        )

        closed = await manager.close_session(first.session_id)
        assert closed.status == AutomationSessionStatus.CLOSED
        assert sessions[0].closed is True

    asyncio.run(scenario())


def test_process_wrapper_accepts_an_existing_process_group(tmp_path):
    async def scenario():
        pid_file = tmp_path / "process.pid"
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/python3",
            "-c",
            ContainerRuntimeSession._PROCESS_WRAPPER,
            str(pid_file),
            "printf wrapper-ok",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await process.communicate()

        assert process.returncode == 0, stderr.decode()
        assert stdout == b"wrapper-ok"
        assert int(pid_file.read_text(encoding="ascii")) > 1

    asyncio.run(scenario())


def test_communicate_accepts_discarded_stderr():
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/python3",
            "-c",
            "import sys; print('output'); print('discarded', file=sys.stderr)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, stderr = await automation_runtime._communicate(process, timeout=5)

        assert stdout == b"output\n"
        assert stderr == b""

    asyncio.run(scenario())


def test_startup_removes_exact_orphan_runtime_and_gateway_names(tmp_path, monkeypatch):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        policy = manager.project_policy(engagement.id)
        session = store.create(
            AutomationSession(
                id="A4D6F5D0-BC17-4A90-8878-21A93F805EB9",
                engagement_id=engagement.id,
                owner_kind="api",
                owner_id="orphan-owner",
                runtime_image=IMAGE,
                runtime_digest="sha256:" + "a" * 64,
                runner_profile_id="runner",
                runner_profile_revision=1,
                policy_id=policy.id,
                policy_revision=policy.revision,
                status=AutomationSessionStatus.READY,
            )
        )
        store.create(
            CommandExecution(
                id=manager._execution_id("orphan-process"),
                engagement_id=engagement.id,
                session_id=session.id,
                process_id="orphan-process",
                command="sleep 3600",
                command_sha256="0" * 64,
                runtime_digest="sha256:" + "a" * 64,
                policy_revision=policy.revision,
            )
        )

        class CleanupRunner:
            def __init__(self):
                self.removed = []

            async def available(self):
                return True, "verified"

            async def _force_remove(self, name):
                self.removed.append(name)

        cleanup_runner = CleanupRunner()
        monkeypatch.setattr(
            manager, "_runner", lambda *_args, **_kwargs: cleanup_runner
        )

        await manager.startup()

        expected = "nebula-runtime-a4d6f5d0bc174a90887821a93f805eb9"
        assert cleanup_runner.removed == [expected, f"{expected}-egress"]
        interrupted_session = store.get(AutomationSession, session.id)
        assert interrupted_session.status == AutomationSessionStatus.INTERRUPTED
        assert interrupted_session.failure_detail == (
            "Core restarted; detached runtime teardown requested"
        )
        [execution] = store.list_entities(CommandExecution)
        assert execution.status == CommandExecutionStatus.INTERRUPTED

    asyncio.run(scenario())


def test_historical_process_io_preserves_agent_ownership(tmp_path):
    async def scenario():
        manager, _store, _artifacts, engagement, _sessions = runtime(tmp_path)
        result = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="mission",
            owner_id="owning-mission",
            request=RunCommandRequest(command="printf done"),
        )
        await manager.close_session(result.session_id)

        with pytest.raises(AutomationPolicyDenied, match="unavailable"):
            await manager.process_io(
                result.process_id,
                ProcessIORequest(action="poll"),
                engagement_id=engagement.id,
                owner_id="another-mission",
            )
        historical = await manager.process_io(
            result.process_id,
            ProcessIORequest(action="poll"),
            engagement_id=engagement.id,
            owner_id="owning-mission",
        )
        assert historical.status == CommandExecutionStatus.COMPLETED

    asyncio.run(scenario())


def test_command_receipt_records_workspace_changes(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)

        result = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="api-workspace",
            request=RunCommandRequest(command="write-generated-file"),
        )

        assert [item.model_dump() for item in result.workspace_changes] == [
            {"path": "generated.txt", "change": "added", "size": 9}
        ]
        execution = store.get(CommandExecution, result.execution_id)
        assert execution.workspace_changes == result.workspace_changes

    asyncio.run(scenario())


def test_command_request_rejects_workspace_escape_and_nul():
    with pytest.raises(ValidationError):
        RunCommandRequest(command="pwd", cwd="../outside")
    with pytest.raises(ValidationError):
        RunCommandRequest(command="printf bad\0command")


def test_binary_output_is_truncated_for_models_and_retained_as_raw_artifact(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setattr(automation_runtime, "MAX_CAPTURE_BYTES", 8)
        manager, store, artifacts, engagement, _sessions = runtime(tmp_path)

        result = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="binary-output",
            request=RunCommandRequest(command="binary-output"),
        )

        execution = store.get(CommandExecution, result.execution_id)
        raw = store.get_by_kind("artifacts", execution.stdout_artifact_id)
        assert result.output_truncated is True
        assert execution.observed_stdout_bytes > raw.size == 8
        assert raw.media_type == "application/octet-stream"
        assert artifacts.read(raw) == b"\x00\xffbinary"

    asyncio.run(scenario())


def test_network_boundary_prompts_once_and_enables_whole_project_scope(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, sessions = runtime(tmp_path)
        request = RunCommandRequest(
            command="curl https://example.test",
            network=AutomationNetworkMode.PROJECT_SCOPE,
        )
        with pytest.raises(CommandApprovalRequired) as required:
            await manager.run_command(
                engagement_id=engagement.id,
                owner_kind="mission",
                owner_id="mission-1",
                request=request,
            )
        approval = store.update(
            Approval,
            required.value.approval.id,
            {
                "status": ApprovalStatus.APPROVED,
                "decided_by": "operator",
                "decided_at": utc_now(),
            },
            expected_revision=required.value.approval.revision,
        )
        result = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="mission",
            owner_id="mission-1",
            request=request,
            approval=approval,
        )
        again = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="mission",
            owner_id="mission-1",
            request=RunCommandRequest(
                command="curl https://second.example",
                network=AutomationNetworkMode.PROJECT_SCOPE,
            ),
        )

        assert result.network_granted is True
        assert again.network_granted is True
        assert sessions[0].enable_count == 1

    asyncio.run(scenario())


def test_never_policy_is_automatic_and_background_process_can_be_cancelled(tmp_path):
    async def scenario():
        manager, _store, _artifacts, engagement, sessions = runtime(tmp_path)
        policy = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=True,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=policy.revision,
        )
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="api-1",
            request=RunCommandRequest(
                command="wait-forever",
                background=True,
                network=AutomationNetworkMode.PROJECT_SCOPE,
            ),
        )
        assert started.status == CommandExecutionStatus.RUNNING
        assert started.network_granted is True
        assert sessions[0].enable_count == 1

        polled = await manager.process_io(
            started.process_id,
            ProcessIORequest(action="write", input="continue\n"),
        )
        assert polled.status == CommandExecutionStatus.RUNNING
        assert sessions[0].processes[0].writes == [b"continue\n"]

        stopped = await manager.process_io(
            started.process_id, ProcessIORequest(action="terminate")
        )
        assert stopped.status == CommandExecutionStatus.CANCELLED

    asyncio.run(scenario())


def test_always_policy_consumes_one_exact_approval_per_command(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        policy = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.ALWAYS,
            network_enabled=False,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=policy.revision,
        )
        request = RunCommandRequest(command="git status")
        with pytest.raises(CommandApprovalRequired) as required:
            await manager.run_command(
                engagement_id=engagement.id,
                owner_kind="api",
                owner_id="api-2",
                request=request,
            )
        approval = store.update(
            Approval,
            required.value.approval.id,
            {
                "status": ApprovalStatus.APPROVED,
                "decided_by": "operator",
                "decided_at": utc_now(),
            },
            expected_revision=required.value.approval.revision,
        )
        await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="api-2",
            request=request,
            approval=approval,
        )
        with pytest.raises(CommandApprovalRequired):
            await manager.run_command(
                engagement_id=engagement.id,
                owner_kind="api",
                owner_id="api-2",
                request=request,
            )

    asyncio.run(scenario())


def test_command_timeout_terminates_the_process_group(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, sessions = runtime(tmp_path)
        started = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="timeout-command",
            request=RunCommandRequest(
                command="wait-forever", background=True, timeout_ms=1_000
            ),
        )

        await asyncio.sleep(1)
        for _ in range(100):
            result = await manager.process_io(
                started.process_id, ProcessIORequest(action="poll")
            )
            if result.status == CommandExecutionStatus.TIMED_OUT:
                break
            await asyncio.sleep(0.01)
        assert result.status == CommandExecutionStatus.TIMED_OUT
        execution = store.get(CommandExecution, result.execution_id)
        assert execution.error == "command timed out"
        assert sessions[0].processes[0]._done.result() == -15

    asyncio.run(scenario())


def test_network_request_fails_closed_when_disabled_or_scope_is_url_only(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        policy = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=False,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=policy.revision,
        )
        request = RunCommandRequest(
            command="curl https://example.test",
            network=AutomationNetworkMode.PROJECT_SCOPE,
        )
        with pytest.raises(AutomationPolicyDenied, match="networking is disabled"):
            await manager.run_command(
                engagement_id=engagement.id,
                owner_kind="api",
                owner_id="disabled-network",
                request=request,
            )

        current = manager.project_policy(engagement.id)
        manager.update_project_policy(
            engagement.id,
            approval_policy=AutomationApprovalPolicy.NEVER,
            network_enabled=True,
            runner_profile_id="runner",
            max_timeout_ms=30_000,
            expected_revision=current.revision,
        )
        scope = store.get(ScopePolicy, engagement.scope_policy_id)
        store.update(
            ScopePolicy,
            scope.id,
            {
                "allowed_cidrs": [],
                "allowed_domains": [],
                "allowed_urls": ["https://example.test/only-this-path"],
            },
            expected_revision=scope.revision,
        )
        with pytest.raises(AutomationPolicyDenied, match="URL-only scope"):
            await manager.run_command(
                engagement_id=engagement.id,
                owner_kind="api",
                owner_id="url-only-network",
                request=request,
            )

    asyncio.run(scenario())


def test_cidr_only_scope_keeps_ports_on_cidr_rules(tmp_path):
    async def scenario():
        manager, _store, _artifacts, engagement, _sessions = runtime(tmp_path)
        launches = []

        async def launch(configuration):
            launches.append(configuration)
            return FakeSession(
                network_granted=configuration.network_granted,
                workspace=configuration.workspace,
            )

        manager.session_factory = launch
        await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="cidr-only",
            request=RunCommandRequest(command="pwd", network="none"),
        )

        assert len(launches) == 1
        assert launches[0].egress_domains == ()
        assert launches[0].egress_ports == ()
        assert [(rule.address, rule.ports) for rule in launches[0].egress_rules] == [
            ("203.0.113.0/24", [443])
        ]

    asyncio.run(scenario())


def test_domain_scope_resolver_is_readable_by_the_non_root_runtime(tmp_path):
    async def scenario():
        manager, store, _artifacts, engagement, _sessions = runtime(tmp_path)
        scope = store.get(ScopePolicy, engagement.scope_policy_id)
        store.update(
            ScopePolicy,
            scope.id,
            {"allowed_cidrs": [], "allowed_domains": ["example.test"]},
            expected_revision=scope.revision,
        )
        launches = []

        async def launch(configuration):
            launches.append(configuration)
            return FakeSession(
                network_granted=configuration.network_granted,
                workspace=configuration.workspace,
            )

        manager.session_factory = launch
        await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="api",
            owner_id="domain-scope",
            request=RunCommandRequest(command="pwd", network="none"),
        )

        resolver = launches[0].resolv_conf
        assert resolver is not None
        assert resolver.stat().st_mode & 0o777 == 0o644
        assert resolver.read_text(encoding="ascii").startswith(
            "nameserver 127.0.0.53\n"
        )

    asyncio.run(scenario())


def test_api_exposes_only_the_fixed_runtime_command_surface(tmp_path):
    manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="runtime-test-token",
        automation_runtime=manager,
    )
    headers = {"Authorization": "Bearer runtime-test-token"}

    with TestClient(app) as client:
        policy = client.get(
            f"/api/v1/engagements/{engagement.id}/automation-policy",
            headers=headers,
        )
        assert policy.status_code == 200
        assert policy.json()["approval_policy"] == "on_boundary"

        command = client.post(
            f"/api/v1/engagements/{engagement.id}/automation-sessions/api/"
            "api-session/commands",
            headers=headers,
            json={"command": "rg needle .", "network": "none"},
        )
        assert command.status_code == 200, command.text
        result = command.json()
        assert result["status"] == "completed"
        assert "rg needle" in result["stdout"]

        processes = client.get(
            f"/api/v1/automation-sessions/{result['session_id']}/processes",
            headers=headers,
        )
        assert processes.status_code == 200
        assert [item["process_id"] for item in processes.json()] == [
            result["process_id"]
        ]

        paths = client.get("/openapi.json").json()["paths"]
        assert not any(
            marker in path
            for path in paths
            for marker in ("tool-packs", "tool-catalog", "tool-assignment")
        )


def test_api_runtime_lifecycle_uses_registered_diagnostics_feature(tmp_path):
    manager, store, artifacts, _engagement, _sessions = runtime(tmp_path)
    diagnostics = DiagnosticManager(
        tmp_path / "diagnostics", level_override="debug", watch_settings=False
    )
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="runtime-test-token",
        automation_runtime=manager,
        diagnostic_manager=diagnostics,
    )

    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/automation/runtime",
                headers={"Authorization": "Bearer runtime-test-token"},
            )
            assert response.status_code == 200
    finally:
        diagnostics.close()

    runtime_events = (diagnostics.log_dir / "runtime.log").read_text(encoding="utf-8")
    assert "runtime.runtime.started" in runtime_events
    assert "runtime.runtime.stopped" in runtime_events


def test_runtime_info_is_not_ready_before_kali_image_preparation(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store.create(
        RunnerProfile(
            id="runner",
            name="Docker",
            runtime=RunnerRuntime.DOCKER,
            executable="/usr/bin/docker",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )

    async def resolve_runtime(_engagement_id: str):
        raise AssertionError("runtime_info must not prepare the image")

    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=artifacts,
        data_root=tmp_path,
        workspace_resolver=lambda _engagement_id: tmp_path / "workspace",
        runtime_resolver=resolve_runtime,
        cached_runtime_provider=lambda: None,
    )

    info = asyncio.run(manager.runtime_info())

    assert info.configured is True
    assert info.ready is False
    assert info.image is None
    assert info.digest is None
    assert info.runner_profile_id == "runner"
    assert info.detail == "the existing Kali headless runtime has not been prepared"


def test_runtime_output_artifacts_are_owner_scoped_searchable_and_bounded(tmp_path):
    async def scenario():
        manager, store, artifacts, engagement, _sessions = runtime(tmp_path)
        call = store.create(
            ToolCall(
                id="command-call",
                engagement_id=engagement.id,
                run_id="mission-owner",
                origin=ToolCallOrigin.MISSION,
                tool_name="run_command",
                status=ToolCallStatus.RUNNING,
                risk_class=RiskClass.WORKSPACE_WRITE,
            )
        )
        result = await manager.run_command(
            engagement_id=engagement.id,
            owner_kind="mission",
            owner_id="mission-owner",
            request=RunCommandRequest(command="rg unique-needle ."),
            tool_call_id=call.id,
        )
        service = ToolOutputService(store, artifacts)
        process_receipt = AutomationBroker(
            manager=manager,
            store=store,
            output_service=service,
        )._receipt("process-io-call", PROCESS_IO_NAME, result)
        assert process_receipt.tool_call_id == call.id

        search = service.search(
            engagement_id=engagement.id,
            owner_id="mission-owner",
            tool_call_id=call.id,
            query="unique-needle",
        )
        assert search["matches"]
        assert search["untrusted_data"] is True
        read = service.read(
            engagement_id=engagement.id,
            owner_id="mission-owner",
            artifact_id=result.stdout_artifact_id,
        )
        assert "unique-needle" in json.dumps(read)
        assert len(json.dumps(read).encode()) <= 8 * 1024
        with pytest.raises(ToolOutputAccessError):
            service.search(
                engagement_id=engagement.id,
                owner_id="another-mission",
                tool_call_id=call.id,
                query="unique-needle",
            )
        with pytest.raises(ToolOutputQueryError, match="deadline"):
            ToolOutputService(store, artifacts, regex_deadline_seconds=0).search(
                engagement_id=engagement.id,
                owner_id="mission-owner",
                tool_call_id=call.id,
                query="(a+)+$",
                mode="regex",
            )

    asyncio.run(scenario())


def test_workspace_retrieval_is_bounded_and_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "retrieval-workspace"
    workspace.mkdir()
    (workspace / "huge.txt").write_text("X" * 100_000 + "\nnext\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)
    service = WorkspaceOutputService(workspace)

    result = service.read(path="huge.txt", line_count=200)
    assert len(json.dumps(result).encode()) <= 8 * 1024
    assert result["lines"][0]["line_truncated"] is True
    with pytest.raises(ToolOutputAccessError):
        service.read(path="link")
    with pytest.raises(ToolOutputAccessError):
        service.read(path="../outside.txt")


def test_historical_raw_tool_output_fails_closed_to_a_receipt():
    result = sanitize_model_history_result(
        {
            "stdout": "raw historical output must stay out of context",
            "stderr": "",
            "exit_code": 0,
        },
        tool_call_id="historical-call",
        tool_name="run_command",
    )

    assert result["schema"] == "nebula.tool-result/v2"
    assert result["incomplete"] is True
    assert "raw historical output" not in json.dumps(result)
