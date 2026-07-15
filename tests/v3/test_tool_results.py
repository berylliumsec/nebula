from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.domain import (
    Artifact,
    ChatTurn,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.mcp import build_mcp_tool_plugins, mcp_tool_runtime_name
from nebula.v3.sandbox import SandboxRequest, SandboxResult, SandboxRunner
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import (
    ToolOutputAccessError,
    ToolOutputQueryError,
    ToolOutputService,
    WorkspaceOutputService,
    sanitize_model_history_result,
)
from nebula.v3.tools import (
    SandboxCommandTool,
    StoreToolEvidenceRecorder,
    StoreToolLedger,
    ToolBroker,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
)


OBJECT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class StreamingFixtureRunner(SandboxRunner):
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int | None = 0,
        timed_out: bool = False,
        generated: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.generated = generated
        self.request: SandboxRequest | None = None

    async def available(self) -> tuple[bool, str]:
        return True, "fixture"

    async def run(self, request: SandboxRequest) -> SandboxResult:
        raise AssertionError("the command tool must use streaming execution")

    async def run_stream(
        self,
        request: SandboxRequest,
        *,
        input_bytes: bytes = b"",
        on_chunk=None,
        container_name: str | None = None,
    ) -> SandboxResult:
        del input_bytes, container_name
        self.request = request
        if self.generated:
            assert request.output_directory is not None
            (request.output_directory / "scan.xml").write_text(
                "<port protocol='tcp' portid='443'/>", encoding="utf-8"
            )
            (request.output_directory / "binary.bin").write_bytes(b"\x00\x01\x02")
            (request.output_directory / "report.txt").write_text(
                "bounded generated report", encoding="utf-8"
            )
            (request.output_directory / "outside-link").symlink_to(
                request.workspace / "outside.txt"
            )
        if on_chunk is not None:
            for stream, payload in (("stdout", self.stdout), ("stderr", self.stderr)):
                for offset in range(0, len(payload), 7):
                    await on_chunk(stream, payload[offset : offset + 7])
        started = utc_now()
        return SandboxResult(
            command=request.command,
            image=request.image,
            runtime="fixture",
            started_at=started,
            completed_at=utc_now(),
            duration_seconds=0,
            exit_code=self.exit_code,
            stdout=self.stdout.decode("utf-8", errors="replace"),
            stderr=self.stderr.decode("utf-8", errors="replace"),
            timed_out=self.timed_out,
            observed_stdout_bytes=len(self.stdout),
            observed_stderr_bytes=len(self.stderr),
        )


class BlockingFixtureRunner(StreamingFixtureRunner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run_stream(self, request: SandboxRequest, **kwargs) -> SandboxResult:
        on_chunk = kwargs.get("on_chunk")
        assert on_chunk is not None
        await on_chunk("stdout", b"partial 22/tcp open ssh\n")
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


def _broker(
    tmp_path: Path,
    runner: SandboxRunner,
    *,
    parser=None,
) -> tuple[ToolBroker, NebulaStore, ArtifactStore, ToolSpec]:
    spec = ToolSpec(
        name="nmap.scan",
        description="Run a fixture scan",
        input_schema=OBJECT_SCHEMA,
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    registry = ToolRegistry()
    registry.register(
        SandboxCommandTool(
            spec,
            image="example.invalid/nmap@sha256:" + "a" * 64,
            command_builder=lambda _: ["/usr/bin/nmap", "127.0.0.1"],
            output_parser=parser,
        )
    )
    store = NebulaStore(Database(tmp_path / "nebula.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    broker = ToolBroker(
        registry=registry,
        policy_engine=__import__(
            "nebula.v3.policy", fromlist=["PolicyEngine"]
        ).PolicyEngine(),
        runner=runner,
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda _: tmp_path,
        evidence_recorder=StoreToolEvidenceRecorder(store, artifacts),
    )
    return broker, store, artifacts, spec


def _execute(broker: ToolBroker, tmp_path: Path, *, call_id: str = "call-nmap"):
    return asyncio.run(
        broker.execute(
            ToolInvocation(
                id=call_id,
                engagement_id="eng-a",
                run_id="run-a",
                tool_name="nmap.scan",
                workspace=tmp_path,
            ),
            ScopePolicy(engagement_id="eng-a"),
        )
    )


def test_raw_nmap_output_is_artifact_first_and_searchable(tmp_path: Path) -> None:
    raw = (
        b"Starting Nmap 7.95\n"
        b"PORT    STATE SERVICE\n"
        b"22/tcp  open  ssh\n"
        b"443/tcp open  https\n"
        b"Nmap done\n"
    )
    broker, store, artifacts, _ = _broker(
        tmp_path, StreamingFixtureRunner(stdout=raw, generated=True)
    )

    result = _execute(broker, tmp_path)

    assert result.output["schema"] == "nebula.tool-result/v2"
    assert "443/tcp" not in json.dumps(result.output)
    assert result.stdout == ""
    assert result.receipt is not None
    assert result.receipt.status.value == "completed"
    assert {item.kind for item in result.receipt.artifacts} >= {
        "stdout",
        "stderr",
        "generated_file",
    }
    search = ToolOutputService(store, artifacts).search(
        engagement_id="eng-a",
        owner_id="run-a",
        tool_call_id="call-nmap",
        query="443/tcp",
        context_lines=1,
    )
    assert search["matches"][0]["line"] == 4
    assert "443/tcp open  https" in json.dumps(search)
    assert search["untrusted_data"] is True
    binary = next(
        item
        for item in store.list_entities(Artifact, engagement_id="eng-a")
        if item.filename == "binary.bin"
    )
    assert (
        ToolOutputService(store, artifacts).read(
            engagement_id="eng-a",
            owner_id="run-a",
            artifact_id=binary.id,
        )["searchable"]
        is False
    )
    assert not any(
        item.filename == "outside-link"
        for item in store.list_entities(Artifact, engagement_id="eng-a")
    )


def test_receipt_includes_safe_nmap_port_observations(tmp_path: Path) -> None:
    raw = b"PORT STATE SERVICE\n18080/tcp open unknown\n"

    def parser(stdout, stderr, exit_code):
        return {
            "protocol": "nebula.toolbox/v1",
            "tool": "nmap",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }

    broker, _, _, _ = _broker(
        tmp_path,
        StreamingFixtureRunner(stdout=raw),
        parser=parser,
    )

    result = _execute(broker, tmp_path)

    assert result.receipt is not None
    assert result.receipt.observations[0].model_dump() == {
        "kind": "network_port",
        "protocol": "tcp",
        "port": 18080,
        "state": "open",
        "service": "unknown",
    }


@pytest.mark.parametrize(
    ("is_error", "expected"), [(False, "completed"), (True, "failed")]
)
def test_native_mcp_execution_is_captured_as_a_receipt(
    tmp_path: Path, is_error: bool, expected: str
) -> None:
    snapshot = McpToolSnapshot(
        name="nmap_result",
        input_schema=OBJECT_SCHEMA,
        read_only=True,
        open_world=False,
        credentialed=False,
        annotations_complete=True,
    )
    profile = McpServerProfile(
        id="mcp-native",
        name="native-fixture",
        transport=McpTransport.STREAMABLE_HTTP,
        url="https://mcp.invalid/api",
        enabled=True,
        capabilities=McpCapabilitySnapshot(checked_at=utc_now(), tools=[snapshot]),
    )

    class Service:
        async def call_tool(self, profile, **kwargs):
            del profile, kwargs
            return {
                "content": [{"type": "text", "text": "443/tcp open https\n"}],
                "structuredContent": {"ports": [443]},
                "isError": is_error,
            }

    registry = ToolRegistry()
    for plugin in build_mcp_tool_plugins(Service(), (profile,)):
        registry.register(plugin)
    store = NebulaStore(Database(tmp_path / "native-mcp.db"))
    artifacts = ArtifactStore(tmp_path / "native-mcp-artifacts")
    broker = ToolBroker(
        registry=registry,
        policy_engine=__import__(
            "nebula.v3.policy", fromlist=["PolicyEngine"]
        ).PolicyEngine(),
        runner=object(),
        ledger=StoreToolLedger(store, enforce_run_budget=False),
        workspace_resolver=lambda _: tmp_path,
        evidence_recorder=StoreToolEvidenceRecorder(store, artifacts),
    )
    tool_name = mcp_tool_runtime_name(profile.id, snapshot.name)
    result = asyncio.run(
        broker.execute(
            ToolInvocation(
                id=f"native-mcp-{expected}",
                engagement_id="eng-a",
                run_id="run-a",
                tool_name=tool_name,
                workspace=tmp_path,
            ),
            ScopePolicy(engagement_id="eng-a"),
        )
    )

    assert result.receipt is not None
    assert result.receipt.status.value == expected
    assert "443/tcp" not in json.dumps(result.receipt.as_model_result())
    matches = ToolOutputService(store, artifacts).search(
        engagement_id="eng-a",
        owner_id="run-a",
        tool_call_id=f"native-mcp-{expected}",
        query="443/tcp",
    )
    assert matches["matches"]


def test_tool_artifact_api_bounds_search_and_requires_raw_acknowledgement(
    tmp_path: Path,
) -> None:
    raw = b"22/tcp open ssh\n443/tcp open https\n"
    broker, store, artifacts, _ = _broker(tmp_path, StreamingFixtureRunner(stdout=raw))
    result = _execute(broker, tmp_path, call_id="call-api")
    assert result.receipt is not None
    stdout = next(item for item in result.receipt.artifacts if item.kind == "stdout")
    client = TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    )
    auth = {"Authorization": "Bearer test-token"}

    search = client.post(
        "/api/v1/tool-calls/call-api/output/search",
        headers=auth,
        json={"query": "443/tcp", "context_lines": 1},
    )
    assert search.status_code == 200
    assert search.json()["matches"][0]["line"] == 2
    denied = client.get(f"/api/v1/artifacts/{stdout.artifact_id}/content", headers=auth)
    assert denied.status_code == 428
    downloaded = client.get(
        f"/api/v1/artifacts/{stdout.artifact_id}/content",
        headers={**auth, "X-Nebula-Sensitive-Data-Acknowledged": "true"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == raw
    assert downloaded.headers["X-Nebula-Artifact-SHA256"] == stdout.sha256
    assert downloaded.headers["X-Nebula-Artifact-Truncated"] == "false"


def test_parser_failure_is_warning_not_execution_failure(tmp_path: Path) -> None:
    raw_output = "443/tcp open https secret=do-not-copy-into-receipt"

    def broken_parser(stdout: str, stderr: str, exit_code: int | None):
        del stderr, exit_code
        raise ValueError(f"nmap XML was malformed: {stdout}")

    broker, store, _, _ = _broker(
        tmp_path,
        StreamingFixtureRunner(stdout=raw_output.encode()),
        parser=broken_parser,
    )
    result = _execute(broker, tmp_path, call_id="call-parser")

    assert result.receipt is not None
    assert result.receipt.status.value == "completed"
    assert result.receipt.parser.state.value == "failed"
    assert "optional parser failed" in result.receipt.warnings[0]
    assert "ValueError" in result.receipt.warnings[0]
    assert raw_output not in json.dumps(result.receipt.as_model_result())
    assert store.get(ToolCall, "call-parser").status == ToolCallStatus.COMPLETE


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [(2, False, "failed"), (None, True, "timed_out")],
)
def test_failed_and_timed_out_execution_preserve_partial_output(
    tmp_path: Path, exit_code: int | None, timed_out: bool, expected: str
) -> None:
    broker, store, artifacts, _ = _broker(
        tmp_path,
        StreamingFixtureRunner(
            stdout=b"partial 80/tcp open http\n",
            stderr=b"fixture failure\n",
            exit_code=exit_code,
            timed_out=timed_out,
        ),
    )
    result = _execute(broker, tmp_path, call_id=f"call-{expected}")

    assert result.receipt is not None
    assert result.receipt.status.value == expected
    assert result.receipt.summary is None
    found = ToolOutputService(store, artifacts).search(
        engagement_id="eng-a",
        owner_id="run-a",
        tool_call_id=f"call-{expected}",
        query="80/tcp",
    )
    assert found["matches"]
    assert store.get(ToolCall, f"call-{expected}").status == ToolCallStatus.FAILED


def test_receipt_promotes_only_trusted_wrapper_validation_error(tmp_path: Path) -> None:
    def parser(stdout, stderr, exit_code):
        del stdout
        return {
            "protocol": "nebula.toolbox/v1",
            "operation": "error",
            "command": [],
            "stderr": stderr,
            "exit_code": exit_code,
        }

    broker, _, _, _ = _broker(
        tmp_path,
        StreamingFixtureRunner(
            stderr=b"ValueError: p must be an integer\n", exit_code=2
        ),
        parser=parser,
    )

    result = _execute(broker, tmp_path, call_id="call-wrapper-error")

    assert result.receipt is not None
    assert result.receipt.summary == "ValueError: p must be an integer"


def test_cancellation_preserves_searchable_partial_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = BlockingFixtureRunner()
        broker, store, artifacts, _ = _broker(tmp_path, runner)
        task = asyncio.create_task(
            broker.execute(
                ToolInvocation(
                    id="call-cancel",
                    engagement_id="eng-a",
                    run_id="run-a",
                    tool_name="nmap.scan",
                    workspace=tmp_path,
                ),
                ScopePolicy(engagement_id="eng-a"),
            )
        )
        await runner.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        call = store.get(ToolCall, "call-cancel")
        assert call.status == ToolCallStatus.CANCELLED
        assert call.result is not None and call.result["status"] == "cancelled"
        found = ToolOutputService(store, artifacts).search(
            engagement_id="eng-a",
            owner_id="run-a",
            tool_call_id=call.id,
            query="ssh",
        )
        assert found["matches"]

    asyncio.run(scenario())


def test_capture_limit_counts_beyond_retention_and_keeps_utf8_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nebula.v3.tools.MAX_CAPTURE_BYTES", 32)
    payload = b"A" * 30 + b"\xff\xfe" + b"B" * 50
    broker, store, artifacts, _ = _broker(
        tmp_path, StreamingFixtureRunner(stdout=payload)
    )
    result = _execute(broker, tmp_path, call_id="call-truncated")

    assert result.receipt is not None and result.receipt.truncated is True
    stdout = next(item for item in result.receipt.artifacts if item.kind == "stdout")
    assert stdout.byte_count == 32
    assert stdout.observed_byte_count == len(payload)
    read = ToolOutputService(store, artifacts).read(
        engagement_id="eng-a",
        owner_id="run-a",
        artifact_id=stdout.artifact_id,
    )
    assert "�" in read["lines"][0]["text"]


def test_generated_file_count_limit_is_reported_without_walking_unboundedly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nebula.v3.tools.MAX_GENERATED_FILES", 2)
    broker, _, _, _ = _broker(
        tmp_path, StreamingFixtureRunner(stdout=b"done\n", generated=True)
    )

    result = _execute(broker, tmp_path, call_id="call-generated-limit")

    assert result.receipt is not None
    generated = [
        item for item in result.receipt.artifacts if item.kind == "generated_file"
    ]
    assert len(generated) == 2
    assert result.receipt.incomplete is True
    assert any("generated file limit" in item for item in result.receipt.warnings)


def test_output_access_is_owner_scoped_and_regex_is_deadlined(tmp_path: Path) -> None:
    broker, store, artifacts, _ = _broker(
        tmp_path, StreamingFixtureRunner(stdout=b"aaaaab\n")
    )
    _execute(broker, tmp_path, call_id="call-owner")
    service = ToolOutputService(store, artifacts)
    with pytest.raises(ToolOutputAccessError):
        service.search(
            engagement_id="eng-b",
            owner_id="run-a",
            tool_call_id="call-owner",
            query="a",
        )
    with pytest.raises(ToolOutputAccessError):
        service.search(
            engagement_id="eng-a",
            owner_id="run-b",
            tool_call_id="call-owner",
            query="a",
        )
    with pytest.raises(ToolOutputQueryError, match="deadline"):
        ToolOutputService(store, artifacts, regex_deadline_seconds=0).search(
            engagement_id="eng-a",
            owner_id="run-a",
            tool_call_id="call-owner",
            query="(a+)+$",
            mode="regex",
        )


def test_chat_output_access_is_authorized_across_one_session(tmp_path: Path) -> None:
    broker, store, artifacts, _ = _broker(
        tmp_path, StreamingFixtureRunner(stdout=b"443/tcp open https\n")
    )
    earlier = store.create(
        ChatTurn(
            id="turn-earlier",
            engagement_id="eng-a",
            session_id="session-a",
            provider_profile_id="provider-a",
            model="model-a",
        )
    )
    current = store.create(
        ChatTurn(
            id="turn-current",
            engagement_id="eng-a",
            session_id="session-a",
            provider_profile_id="provider-a",
            model="model-a",
        )
    )
    other = store.create(
        ChatTurn(
            id="turn-other",
            engagement_id="eng-a",
            session_id="session-b",
            provider_profile_id="provider-a",
            model="model-a",
        )
    )
    asyncio.run(
        broker.execute(
            ToolInvocation(
                id="chat-call",
                engagement_id="eng-a",
                run_id=earlier.id,
                origin=ToolCallOrigin.CHAT,
                chat_session_id=earlier.session_id,
                chat_turn_id=earlier.id,
                tool_name="nmap.scan",
                workspace=tmp_path,
            ),
            ScopePolicy(engagement_id="eng-a"),
        )
    )
    service = ToolOutputService(store, artifacts)

    assert service.search(
        engagement_id="eng-a",
        owner_id=current.id,
        tool_call_id="chat-call",
        query="443/tcp",
    )["matches"]
    with pytest.raises(ToolOutputAccessError):
        service.search(
            engagement_id="eng-a",
            owner_id=other.id,
            tool_call_id="chat-call",
            query="443/tcp",
        )


def test_workspace_retrieval_rejects_symlinks_and_bounds_huge_lines(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
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


def test_historical_model_history_fails_closed_to_a_receipt() -> None:
    result = sanitize_model_history_result(
        {
            "stdout": "raw historical scan output must stay out of context",
            "stderr": "",
            "exit_code": 0,
        },
        tool_call_id="legacy-call",
        tool_name="nmap.scan",
    )

    assert result["schema"] == "nebula.tool-result/v2"
    assert result["incomplete"] is True
    assert "raw historical scan" not in json.dumps(result)
