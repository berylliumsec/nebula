from __future__ import annotations

import asyncio
import hashlib
from functools import wraps

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.assistant_code import parse_fenced_code_blocks
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    Engagement,
    ExecutionOrigin,
    ExecutionNetworkMode,
    OperatorExecution,
    OperatorExecutionStatus,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    ScopePolicy,
    utc_now,
)
from nebula.v3.executions import (
    ExecutionNetworkRequest,
    ExecutionPreflightRequest,
    ExecutionService,
    ExecutionServiceError,
    ExecutionStartRequest,
)
from nebula.v3.sandbox import SandboxNetwork, SandboxResult
from nebula.v3.storage import NebulaStore
from nebula.v3.runtime_platform import (
    OperatorRuntimeCommand,
    OperatorRuntimeResolution,
    RuntimePlatformError,
)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class RecordingRunner:
    def __init__(self) -> None:
        self.requests: list[tuple[object, bytes, str]] = []
        self.removed: list[str] = []

    async def run_stream(
        self,
        request,
        *,
        input_bytes: bytes,
        on_chunk,
        container_name: str,
    ) -> SandboxResult:
        self.requests.append((request, input_bytes, container_name))
        await on_chunk("stdout", b"before sk-test-token-")
        await on_chunk("stderr", b"warning\x1b[31m\n")
        await on_chunk("stdout", b"12345678901234567890 after\n")
        now = utc_now()
        return SandboxResult(
            command=request.command,
            image=request.image,
            runtime="stub",
            started_at=now,
            completed_at=now,
            duration_seconds=0,
            exit_code=7,
            stdout="",
            stderr="",
        )

    async def _force_remove(self, container_name: str) -> None:
        self.removed.append(container_name)


class StubExecutionPlatform:
    execution_enabled = True

    def __init__(self, workspace, runner: RecordingRunner) -> None:
        self.workspace = workspace
        self.workspace.mkdir(parents=True)
        self.runner = runner
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
        self.image = "sha256:" + "b" * 64

    def workspace_for(self, engagement_id: str):
        del engagement_id
        return self.workspace

    def resolve_operator_runtime(
        self, engagement_id: str, language: str, *, network: bool
    ) -> OperatorRuntimeResolution:
        del engagement_id, network
        canonical = {"python3": "python", "py": "python", "shell": "bash"}.get(
            language, language
        )
        runtime = OperatorRuntimeCommand(
            interpreter=("/usr/bin/python3" if canonical == "python" else "/bin/bash"),
            arguments=(["-I", "-B"] if canonical == "python" else ["--noprofile"]),
        )
        return OperatorRuntimeResolution(
            canonical_language=canonical,
            runtime=runtime,
            profile=self.profile,
            image=self.image,
            runtime_digest=self.image,
            runner=self.runner,  # type: ignore[arg-type]
            workspace=self.workspace,
        )


def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Execution Lab"))
    policy = store.create(ScopePolicy(engagement_id=engagement.id))
    engagement = store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": policy.id},
        expected_revision=engagement.revision,
    )
    markdown = "Result:\n```python\n  print('λ')\n```\n"
    message = store.create(
        ChatMessage(
            engagement_id=engagement.id,
            session_id="session-1",
            sequence=1,
            role=ChatRole.ASSISTANT,
            content=markdown,
        )
    )
    block = parse_fenced_code_blocks(message.content)[0]
    runner = RecordingRunner()
    platform = StubExecutionPlatform(tmp_path / "workspace", runner)
    service = ExecutionService(
        store=store,
        artifact_store=artifacts,
        tool_platform=platform,  # type: ignore[arg-type]
        data_root=tmp_path / "core",
        operator_id=lambda: "operator-1",
    )
    request = ExecutionPreflightRequest(
        engagement_id=engagement.id,
        language="python3",
        source=block.source,
        origin=ExecutionOrigin(
            kind="assistant_message",
            message_id=message.id,
            block_ordinal=0,
            block_sha256=block.sha256,
        ),
    )
    return store, artifacts, engagement, policy, runner, service, request


def test_run_capability_is_hidden_until_offline_and_scoped_paths_are_ready(tmp_path):
    _store, _artifacts, engagement, _policy, _runner, service, _request = _fixture(
        tmp_path
    )
    assert service.tool_platform is not None
    original = service.tool_platform.resolve_operator_runtime

    def offline_only(engagement_id: str, language: str, *, network: bool):
        if network:
            raise RuntimePlatformError("egress helper unavailable")
        return original(engagement_id, language, network=network)

    service.tool_platform.resolve_operator_runtime = offline_only  # type: ignore[method-assign]
    capabilities = service.capabilities(engagement.id)

    assert any(runtime.offline for runtime in capabilities.runtimes)
    assert all(not runtime.scoped_network for runtime in capabilities.runtimes)
    assert capabilities.ready is False


@async_test
async def test_empty_starter_scope_allows_offline_review_but_denies_network(tmp_path):
    _store, _artifacts, _engagement, policy, _runner, service, request = _fixture(
        tmp_path
    )
    assert policy.allowed_cidrs == []
    assert policy.allowed_domains == []
    assert policy.allowed_urls == []
    assert policy.allowed_ports == []

    offline = await service.preflight(request)
    networked = await service.preflight(
        request.model_copy(
            update={
                "network": ExecutionNetworkRequest(
                    mode=ExecutionNetworkMode.SCOPED,
                    target="192.0.2.10",
                    ports=[443],
                )
            }
        )
    )

    assert offline.allowed is True
    assert networked.allowed is False
    assert networked.error_code == "policy_denied"


async def _await_terminal(
    service: ExecutionService, execution_id: str
) -> OperatorExecution:
    task = service._tasks.get(execution_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    return service.store.get(OperatorExecution, execution_id)


@async_test
async def test_terminal_selection_enters_the_same_mandatory_review_path(tmp_path):
    _store, _artifacts, engagement, _policy, _runner, service, request = _fixture(
        tmp_path
    )
    source = "printf 'selected command\\n'\n"
    selected = request.model_copy(
        update={
            "engagement_id": engagement.id,
            "language": "bash",
            "source": source,
            "origin": ExecutionOrigin(
                kind="selection",
                source_kind="terminal",
                source_id="terminal-session-1",
                source_label="Terminal selection",
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            ),
        }
    )

    preview = await service.preflight(selected)
    assert preview.allowed is True
    assert preview.canonical_language == "bash"
    assert preview.source_sha256 == selected.origin.source_sha256

    tampered = selected.model_copy(
        update={
            "origin": selected.origin.model_copy(update={"source_sha256": "0" * 64})
        }
    )
    denied = await service.preflight(tampered)
    assert denied.allowed is False
    assert denied.error_code == "origin_mismatch"
    assert "reviewed SHA-256" in denied.detail


@async_test
async def test_reviewed_execution_is_exact_isolated_redacted_and_idempotent(tmp_path):
    store, _artifacts, _engagement, _policy, runner, service, request = _fixture(
        tmp_path
    )
    assert request.source.startswith("  ")
    assert request.source.endswith("\n")
    await service.startup()
    preview = await service.preflight(request)
    assert preview.allowed is True
    assert preview.canonical_language == "python"
    assert preview.runtime is not None
    assert preview.runtime.arguments == ["-I", "-B"]
    assert preview.preview_token is not None
    assert preview.preview_fingerprint is not None

    start = ExecutionStartRequest(
        **request.model_dump(),
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        client_idempotency_key="client-attempt-1",
    )
    execution = await service.start(start)
    retry = await service.start(start)
    assert retry.id == execution.id
    terminal = await _await_terminal(service, execution.id)
    assert terminal.status == OperatorExecutionStatus.COMPLETED
    assert terminal.exit_code == 7
    assert terminal.evidence_id is not None
    assert terminal.manifest_artifact_id is not None

    assert len(runner.requests) == 1
    sandbox_request, source_bytes, container_name = runner.requests[0]
    assert source_bytes == request.source.encode("utf-8")
    assert container_name == "nebula-exec-" + execution.id.replace("-", "")
    assert sandbox_request.network == SandboxNetwork.NONE
    assert sandbox_request.command == ["/usr/bin/python3", "-I", "-B"]
    assert sandbox_request.limits.timeout_seconds == 300
    assert sandbox_request.limits.output_bytes == 2_000_000

    raw_stdout, _ = service.output_bytes(execution.id, "stdout", raw=True)
    safe_stdout, _ = service.output_bytes(execution.id, "stdout", raw=False)
    assert raw_stdout == b"before sk-test-token-12345678901234567890 after\n"
    assert b"sk-test-token-12345678901234567890" not in safe_stdout
    assert b"[REDACTED TOKEN]" in safe_stdout
    safe_stderr, _ = service.output_bytes(execution.id, "stderr", raw=False)
    assert b"<0x1B>[31m" in safe_stderr

    events = store.replay_operation_events(execution.id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    serialized = "\n".join(str(event.payload) for event in events)
    assert "sk-test-token-12345678901234567890" not in serialized
    await service.shutdown()


@async_test
async def test_confirmed_policy_race_creates_a_durable_denied_record(tmp_path):
    store, _artifacts, _engagement, policy, runner, service, request = _fixture(
        tmp_path
    )
    await service.startup()
    preview = await service.preflight(request)
    assert preview.allowed and preview.preview_token and preview.preview_fingerprint
    store.update(
        ScopePolicy,
        policy.id,
        {"prohibited_actions": ["operator_code"]},
        expected_revision=policy.revision,
    )
    start = ExecutionStartRequest(
        **request.model_dump(),
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        client_idempotency_key="policy-race",
    )
    denied = await service.start(start)
    assert denied.status == OperatorExecutionStatus.DENIED
    assert denied.error_code == "policy_denied"
    assert denied.completed_at is not None
    assert denied.evidence_id is None
    assert runner.requests == []
    assert (await service.start(start)).id == denied.id
    events = store.replay_operation_events(denied.id)
    assert [event.event_type for event in events] == ["execution.denied"]
    await service.shutdown()


@async_test
async def test_idempotency_key_conflict_fails_closed(tmp_path):
    _store, _artifacts, _engagement, _policy, _runner, service, request = _fixture(
        tmp_path
    )
    await service.startup()
    preview = await service.preflight(request)
    assert preview.allowed and preview.preview_token and preview.preview_fingerprint
    start = ExecutionStartRequest(
        **request.model_dump(),
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        client_idempotency_key="same-key",
    )
    execution = await service.start(start)
    changed = start.model_copy(update={"language": "py"})
    with pytest.raises(ExecutionServiceError, match="different execution input") as exc:
        await service.start(changed)
    assert exc.value.code == "idempotency_conflict"
    await _await_terminal(service, execution.id)
    await service.shutdown()
