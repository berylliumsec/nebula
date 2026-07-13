import hashlib
import json

import httpx
from typer.testing import CliRunner

from nebula.v3.cli import _is_loopback, app
from nebula.v3.domain import AgentRun, Artifact, Engagement, ProviderProfile
from nebula.v3.providers import (
    ModelCapabilities,
    OpenAICompatibleProvider,
    ProviderFlavor,
    config_from_catalog,
)
from nebula.v3.storage import NebulaStore
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


def test_worker_rejects_unimplemented_daemon_mode():
    result = CliRunner().invoke(app, ["worker", "--no-once"])

    assert result.exit_code != 0
    assert "durable worker mode is release-gated" in result.output


def test_cli_run_rejects_tool_budget_without_selected_tools():
    result = CliRunner().invoke(
        app,
        ["run", "engagement", "objective", "--max-tool-calls", "1"],
    )

    assert result.exit_code != 0
    normalized_output = " ".join(result.output.split())
    assert "positive --max-tool-calls budget requires at least one" in normalized_output
    assert "--tool" in normalized_output


def test_cli_tool_missions_are_not_release_gated():
    result = CliRunner().invoke(
        app,
        [
            "run",
            "engagement",
            "objective",
            "--tool",
            "nmap.connect_scan",
            "--max-tool-calls",
            "1",
            "--provider",
            "provider",
        ],
    )

    assert result.exit_code != 0
    assert "release-gated" not in result.output
    assert result.exception is not None
    assert "engagements entity not found" in str(result.exception)


def test_tools_cli_accepts_stable_json_output_flag(tmp_path):
    result = CliRunner().invoke(
        app,
        ["tools", "--json", "list", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"installations": [], "tools": []}


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
