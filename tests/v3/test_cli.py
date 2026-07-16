import hashlib
import json
from datetime import datetime, timezone

import httpx
from typer.testing import CliRunner

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.cli import _is_loopback, app
from nebula.v3.domain import (
    AgentRun,
    Artifact,
    Engagement,
    ProviderProfile,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
)
from nebula.v3.providers import (
    ModelCapabilities,
    OpenAICompatibleProvider,
    ProviderFlavor,
    config_from_catalog,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.terminal_history import CapturedTerminalCommand, TerminalCommandHistory
from nebula.v3.version import __version__


def _manifest(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_loopback_detection_supports_ipv6_and_rejects_mixed_dns(monkeypatch):
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("::1") is True
    assert _is_loopback("192.0.2.1") is False

    monkeypatch.setattr(
        "nebula.v3.cli.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("192.0.2.1", 0)),
        ],
    )
    assert _is_loopback("mixed.example") is False


def test_hidden_mcp_gateway_command_uses_environment_token(tmp_path, monkeypatch):
    observed = {}

    async def serve_gateway(socket_path, token):
        observed.update(socket_path=socket_path, token=token)
        return 0

    monkeypatch.setattr("nebula.v3.cli.serve_mcp_gateway", serve_gateway)
    monkeypatch.setenv("NEBULA_MCP_GATEWAY_TOKEN", "single-use-token")

    result = CliRunner().invoke(
        app,
        ["mcp-gateway", "--socket", str(tmp_path / "core.sock")],
    )

    assert result.exit_code == 0, result.stdout
    assert observed == {
        "socket_path": tmp_path / "core.sock",
        "token": "single-use-token",
    }


def test_hidden_mcp_gateway_command_requires_environment_token(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_MCP_GATEWAY_TOKEN", raising=False)

    result = CliRunner().invoke(
        app,
        ["mcp-gateway", "--socket", str(tmp_path / "core.sock")],
    )

    assert result.exit_code == 2
    assert "gateway token environment is required" in result.output


def test_doctor_reports_analysis_only_without_host_fallback(tmp_path, monkeypatch):
    async def unavailable(_self):
        return False, "rootless runner is not configured"

    monkeypatch.setattr("nebula.v3.cli.ContainerSandboxRunner.available", unavailable)
    monkeypatch.setenv("NEBULA_BUILD_COMMIT", "doctor-commit")
    monkeypatch.setenv("NEBULA_BUILD_TARGET", "doctor-target")
    monkeypatch.setenv("NEBULA_BUILD_TIMESTAMP", "2026-07-12T12:00:00Z")
    monkeypatch.setenv("NEBULA_DISTRIBUTION_CHANNEL", "qa")
    result = CliRunner().invoke(app, ["doctor", "--json", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    assert report["version"] == __version__
    assert report["commit"] == "doctor-commit"
    assert report["target"] == "doctor-target"
    assert report["build_timestamp"] == "2026-07-12T12:00:00Z"
    assert report["distribution_channel"] == "qa"
    assert report["database"]["journal_mode"] == "wal"
    assert report["artifacts"]["writable"] is True
    assert report["artifacts"]["checked"] == 0
    assert report["artifacts"]["corrupt"] == 0
    assert report["artifacts"]["orphan_blobs"] == 0
    assert report["api"]["version"] == "v1"
    assert report["sandbox"] == {
        "available": False,
        "detail": "rootless runner is not configured",
        "host_fallback": False,
        "mode": "analysis-only",
    }


def test_doctor_reports_unprepared_kali_runtime_without_crashing(tmp_path):
    data_dir = tmp_path / "doctor-unprepared-runtime"
    store = NebulaStore(data_dir / "nebula.db")
    store.create(
        RunnerProfile(
            id="local",
            name="Local Docker",
            runtime=RunnerRuntime.DOCKER,
            executable="/usr/bin/docker",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )

    result = CliRunner().invoke(
        app, ["doctor", "--json", "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.stdout
    runtime = json.loads(result.stdout)["automation_runtime"]
    assert runtime == {
        "configured": True,
        "ready": False,
        "image": None,
        "digest": None,
        "runner_profile_id": "local",
        "detail": "the existing Kali headless runtime has not been prepared",
        "inventory": [],
    }


def test_doctor_reports_corrupt_persisted_artifacts(tmp_path, monkeypatch):
    async def unavailable(_self):
        return False, "not configured"

    monkeypatch.setattr("nebula.v3.cli.ContainerSandboxRunner.available", unavailable)
    data_dir = tmp_path / "doctor-data"
    store = NebulaStore(data_dir / "nebula.db")
    artifact = store.create(
        Artifact(
            engagement_id="eng-a",
            sha256="a" * 64,
            size=10,
            storage_path=f"sha256/aa/aa/{'a' * 64}",
        )
    )

    result = CliRunner().invoke(
        app,
        ["doctor", "--json", "--data-dir", str(data_dir)],
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["status"] == "error"
    assert report["artifacts"]["checked"] == 1
    assert report["artifacts"]["corrupt"] == 1
    assert report["artifacts"]["corrupt_ids"] == [artifact.id]


def test_doctor_verifies_terminal_audit_hashes_artifacts_and_events(
    tmp_path, monkeypatch
):
    async def unavailable(_self):
        return False, "not configured"

    monkeypatch.setattr("nebula.v3.cli.ContainerSandboxRunner.available", unavailable)
    data_dir = tmp_path / "terminal-doctor-data"
    store = NebulaStore(data_dir / "nebula.db")
    artifacts = ArtifactStore(data_dir / "artifacts")
    project = store.create(Engagement(name="Terminal doctor"))
    now = datetime.now(timezone.utc)
    output = b"verified terminal result"
    TerminalCommandHistory(
        store.database, store=store, artifact_store=artifacts
    ).record_capture(
        engagement_id=project.id,
        session_id="doctor-terminal-session",
        operator_id="operator-1",
        capture=CapturedTerminalCommand(
            shell_sequence="1",
            command="printf verified",
            cwd="/workspace",
            status="completed",
            exit_code=0,
            started_at=now,
            completed_at=now,
            output=output,
            observed_output_bytes=len(output),
            output_sha256=hashlib.sha256(output).hexdigest(),
            output_truncated=False,
        ),
    )

    healthy = CliRunner().invoke(app, ["doctor", "--json", "--data-dir", str(data_dir)])
    assert healthy.exit_code == 0, healthy.stdout
    assert json.loads(healthy.stdout)["terminal_audit"] == {
        "checked": 1,
        "errors": 0,
        "error_records": [],
    }

    with store.database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE terminal_command_records SET command_sha256 = ?",
            ("0" * 64,),
        )
    corrupted = CliRunner().invoke(
        app, ["doctor", "--json", "--data-dir", str(data_dir)]
    )
    assert corrupted.exit_code == 1
    audit = json.loads(corrupted.stdout)["terminal_audit"]
    assert audit["errors"] >= 1
    assert any("command_hash" in item for item in audit["error_records"])


def test_worker_rejects_unimplemented_daemon_mode():
    result = CliRunner().invoke(app, ["worker", "--no-once"])

    assert result.exit_code != 0
    assert "durable worker mode is release-gated" in result.output


def test_runtime_status_reports_the_prepared_kali_runtime(tmp_path, monkeypatch):
    class Info:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "ready": True,
                "image": "localhost/nebula-kali-headless@sha256:" + "a" * 64,
                "digest": "sha256:" + "a" * 64,
                "binary_inventory": [{"name": "rg", "version": "14.1.1"}],
            }

    class Manager:
        async def runtime_info(self):
            return Info()

    monkeypatch.setattr("nebula.v3.cli._automation_services", lambda _: Manager())
    result = CliRunner().invoke(
        app,
        ["runtime", "status", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ready"] is True
    assert "nebula-kali-headless" in payload["image"]
    assert payload["binary_inventory"][0]["name"] == "rg"


def test_cli_imports_legacy_side_by_side_then_exports_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    monkeypatch.delenv("NEBULA_V3_ARTIFACT_DIR", raising=False)
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "engagement_details.json").write_text(
        json.dumps(
            {
                "engagement_name": "Legacy consultancy engagement",
                "ip_addresses": ["192.0.2.10"],
                "urls": ["https://app.example.test"],
            }
        ),
        encoding="utf-8",
    )
    (source / "notes.txt").write_text("Analyst note", encoding="utf-8")
    before = _manifest(source)
    data_dir = tmp_path / "v3-data"
    runner = CliRunner()

    imported = runner.invoke(
        app,
        ["import-2x", str(source), "--data-dir", str(data_dir)],
    )

    assert imported.exit_code == 0, imported.stdout
    report = json.loads(imported.stdout)
    assert report["status"] == "completed"
    assert report["source_unchanged"] is True
    assert _manifest(source) == before
    store = NebulaStore(data_dir / "nebula.db")
    engagement = store.get(Engagement, report["target_engagement_id"])
    assert engagement.name == "Legacy consultancy engagement"

    destination = tmp_path / "legacy-export.zip"
    exported = runner.invoke(
        app,
        [
            "export",
            engagement.id,
            str(destination),
            "--data-dir",
            str(data_dir),
        ],
    )
    assert exported.exit_code == 0, exported.stdout
    export_report = json.loads(exported.stdout)
    assert export_report["engagement_id"] == engagement.id
    assert export_report["entity_counts"]["engagements"] == 1
    assert destination.is_file()


def test_cli_run_uses_an_explicit_vllm_profile_without_tools(tmp_path, monkeypatch):
    data_dir = tmp_path / "v3-data"
    store = NebulaStore(data_dir / "nebula.db")
    engagement = store.create(Engagement(name="vLLM mission"))
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=["security-model"],
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == "security-model"
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "id": "vllm-request-1",
                "model": "security-model",
                "choices": [
                    {
                        "message": {"content": "Scope is bounded and reviewable."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 5,
                    "total_tokens": 13,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        config_from_catalog(
            provider_id=profile.id,
            flavor=ProviderFlavor.VLLM,
            default_model="security-model",
            capabilities=ModelCapabilities(),
        ),
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(
        "nebula.v3.cli.provider_from_profile", lambda selected: provider
    )

    result = CliRunner().invoke(
        app,
        [
            "run",
            engagement.id,
            "Review external scope",
            "--provider",
            profile.id,
            "--data-dir",
            str(data_dir),
            "--max-tool-calls",
            "0",
            "--max-tokens",
            "100",
        ],
    )

    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert "Scope is bounded and reviewable." in report["summary"]
    run = store.list_entities(AgentRun)[0]
    assert run.supervisor_provider_id == profile.id
    assert run.supervisor_model == "security-model"
