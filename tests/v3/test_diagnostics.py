from __future__ import annotations

import json
import logging
import os
import stat
import asyncio
import threading
import time
import warnings
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nebula.v3 import diagnostics
from nebula.v3.cli import app
from nebula.v3.diagnostics import (
    DIAGNOSTIC_SCHEMA,
    FEATURE_FILES,
    SETTINGS_SCHEMA,
    DiagnosticManager,
    create_diagnostic_task,
    current_operation_id,
    diagnostic_context,
    record_caught_exception,
    sanitize_metadata,
)


CONTRACT_PATH = Path(__file__).parent / "fixtures" / "diagnostics_contract.json"


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_shared_diagnostics_contract_matches_core() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["record_schema"] == DIAGNOSTIC_SCHEMA
    assert contract["settings"] == diagnostics.DiagnosticSettings().as_dict()
    assert contract["features"] == list(FEATURE_FILES)
    assert (
        sanitize_metadata(contract["metadata_input"]) == contract["metadata_expected"]
    )


@pytest.fixture
def manager(tmp_path: Path):
    value = DiagnosticManager(tmp_path, watch_settings=False)
    try:
        yield value
    finally:
        value.close()


def test_error_default_routes_to_feature_and_aggregate_with_secure_files(
    manager: DiagnosticManager,
) -> None:
    assert (
        manager.record(
            "info",
            "projects",
            "projects.lifecycle.started",
            "A project operation started.",
        )
        is None
    )

    error_id = manager.record(
        "error",
        "projects",
        "projects.persistence.failed",
        "The requested project operation could not be saved.",
        stage="persistence",
        outcome="failure",
        retryable=True,
    )

    assert error_id
    feature_record = _records(manager.log_dir / "projects.log")[-1]
    aggregate_record = _records(manager.log_dir / "errors.log")[-1]
    assert feature_record == aggregate_record
    assert feature_record == {
        **feature_record,
        "schema": DIAGNOSTIC_SCHEMA,
        "level": "ERROR",
        "feature": "projects",
        "error_id": error_id,
        "stage": "persistence",
    }
    assert not (manager.log_dir / "projects.log").read_text().startswith("A project")
    assert set(FEATURE_FILES.values()).issubset(
        {path.name for path in manager.log_dir.iterdir()}
    )
    for path in [manager.settings_path, *manager.log_dir.iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manager.log_dir.stat().st_mode) == 0o700


def test_every_feature_error_routes_to_its_domain_and_the_same_aggregate_record(
    manager: DiagnosticManager,
) -> None:
    expected: dict[str, str] = {}
    for feature in FEATURE_FILES:
        error_id = manager.record(
            "error",
            feature,
            f"{feature}.contract.failed",
            "A feature contract fault was injected.",
            outcome="failure",
            stage="fault-injection",
        )
        assert error_id is not None
        expected[feature] = error_id

    aggregate = {
        str(record["error_id"]): record
        for record in _records(manager.log_dir / "errors.log")
    }
    for feature, filename in FEATURE_FILES.items():
        feature_record = _records(manager.log_dir / filename)[-1]
        assert feature_record["feature"] == feature
        assert feature_record["error_id"] == expected[feature]
        assert aggregate[expected[feature]] == feature_record


def test_global_and_per_feature_levels_are_atomic_and_process_override_wins(
    tmp_path: Path,
) -> None:
    manager = DiagnosticManager(
        tmp_path, level_override="warning", watch_settings=False
    )
    try:
        updated = manager.update_settings(
            {
                "schema": SETTINGS_SCHEMA,
                "global_level": "debug",
                "feature_levels": {"projects": "info"},
            }
        )
        assert updated.global_level == "debug"
        assert manager.effective_level("projects") == "warning"
        assert manager.effective_level("storage") == "warning"
        saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
        assert saved == updated.as_dict()
        assert not list(manager.settings_path.parent.glob(".diagnostics-settings*.tmp"))
    finally:
        manager.close()

    feature_override = DiagnosticManager(
        tmp_path,
        feature_level_overrides={"projects": "debug"},
        watch_settings=False,
    )
    try:
        assert feature_override.effective_level("projects") == "debug"
        assert feature_override.effective_level("storage") == "debug"
    finally:
        feature_override.close()


def test_invalid_process_override_fails_closed_and_is_recorded(tmp_path: Path) -> None:
    manager = DiagnosticManager(tmp_path, level_override="trace", watch_settings=False)
    try:
        assert manager.effective_level("projects") == "error"
        records = _records(manager.log_dir / "errors.log")
        assert records[-1]["event_code"] == "diagnostics.process_override_invalid"
        assert records[-1]["level"] == "ERROR"
    finally:
        manager.close()


def test_corrupt_settings_fail_closed_and_create_a_diagnostic(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    settings = tmp_path / "diagnostics-settings.json"
    settings.write_text('{"global_level":"debug","feature_levels":', encoding="utf-8")
    os.chmod(settings, 0o600)

    manager = DiagnosticManager(tmp_path, watch_settings=False)
    try:
        assert manager.settings.global_level == "error"
        record = _records(manager.log_dir / "errors.log")[-1]
        assert record["event_code"] == "diagnostics.settings_invalid"
        assert record["level"] == "ERROR"
    finally:
        manager.close()


def test_recursive_sanitization_excludes_payloads_and_exception_values(
    manager: DiagnosticManager,
) -> None:
    try:
        raise RuntimeError("Bearer top-secret-token-value command=canary-command")
    except RuntimeError as exc:
        manager.record(
            "error",
            "sandbox",
            "sandbox.boundary.failed",
            "Sandbox setup failed for Bearer top-secret-token-value.",
            exception=exc,
            metadata={
                "component": "runner",
                "command": "canary-command",
                "nested": {"password": "canary-password"},
                "count": 3,
            },
        )

    encoded = (manager.log_dir / "sandbox.log").read_text(encoding="utf-8")
    assert "top-secret-token-value" not in encoded
    assert "canary-command" not in encoded
    assert "canary-password" not in encoded
    record = json.loads(encoded)
    assert record["exception_type"] == "RuntimeError"
    assert "exception_message" not in record
    assert record["metadata"] == {"component": "runner", "count": 3}
    assert all("locals" not in frame for frame in record["stack_frames"])


def test_runtime_warning_adapter_drops_message_and_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DiagnosticManager(tmp_path, watch_settings=False)
    manager.update_settings(
        {
            "schema": SETTINGS_SCHEMA,
            "global_level": "warning",
            "feature_levels": {},
        }
    )
    original = warnings.showwarning
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    monkeypatch.setattr(diagnostics, "_manager", manager)
    try:
        diagnostics._install_warning_hook()
        warnings.showwarning(
            UserWarning("Bearer top-secret-token-value command=canary-command"),
            UserWarning,
            "/sensitive/canary-source.py",
            42,
        )
        handler = diagnostics._SanitizingDiagnosticHandler(level=logging.WARNING)
        handler.handle(
            logging.LogRecord(
                name="canary.third.party",
                level=logging.WARNING,
                pathname="/sensitive/canary-library.py",
                lineno=51,
                msg="Bearer canary-logging-token-value",
                args=(),
                exc_info=None,
            )
        )
        manager.flush()
    finally:
        warnings.showwarning = original
        for installed in list(root_logger.handlers):
            if installed not in original_handlers:
                root_logger.removeHandler(installed)
        manager.close()

    encoded = (manager.log_dir / "diagnostics.log").read_text(encoding="utf-8")
    assert "diagnostics.runtime.warning" in encoded
    assert '"category":"UserWarning"' in encoded
    assert "top-secret-token-value" not in encoded
    assert "canary-command" not in encoded
    assert "canary-source.py" not in encoded
    assert "diagnostics.runtime.log_warning" in encoded
    assert "canary-logging-token-value" not in encoded
    assert "canary.third.party" not in encoded
    assert "canary-library.py" not in encoded


def test_invalid_durations_are_omitted_from_valid_json_lines(
    manager: DiagnosticManager,
) -> None:
    manager.update_settings(
        {"schema": SETTINGS_SCHEMA, "global_level": "debug", "feature_levels": {}}
    )
    manager.record(
        "debug",
        "api",
        "api.duration.invalid",
        "An invalid duration was safely omitted.",
        duration_ms=float("nan"),
    )
    manager.record(
        "debug",
        "api",
        "api.duration.negative",
        "A negative duration was safely omitted.",
        duration_ms=-1,
    )
    assert manager.flush()
    records = _records(manager.log_dir / "api.log")
    assert all("duration_ms" not in record for record in records[-2:])


def test_context_ids_and_multithreaded_sequence_are_stable(
    manager: DiagnosticManager,
) -> None:
    manager.update_settings(
        {"schema": SETTINGS_SCHEMA, "global_level": "debug", "feature_levels": {}}
    )
    with diagnostic_context(
        request_id="request_1",
        operation_id="operation_1",
        parent_operation_id="operation_parent",
        project_id="project_1",
    ):
        manager.record(
            "error", "api", "api.correlated.failed", "A correlated operation failed."
        )

    threads = [
        threading.Thread(
            target=lambda index=index: manager.record(
                "error",
                "storage",
                "storage.concurrent.failed",
                "A concurrent test operation failed.",
                metadata={"count": index},
            )
        )
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    correlated = _records(manager.log_dir / "api.log")[-1]
    assert correlated["request_id"] == "request_1"
    assert correlated["operation_id"] == "operation_1"
    assert correlated["parent_operation_id"] == "operation_parent"
    assert correlated["project_id"] == "project_1"
    sequences = [
        int(item["sequence"]) for item in _records(manager.log_dir / "errors.log")
    ]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


def test_rotation_retention_and_age_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_FILE_BYTES", 700)
    manager = DiagnosticManager(tmp_path, watch_settings=False)
    try:
        for index in range(12):
            manager.record(
                "error",
                "projects",
                "projects.rotation.failed",
                "A bounded failure record triggered rotation.",
                metadata={"count": index},
            )
        assert (manager.log_dir / "projects.log.1").is_file()
        assert not (manager.log_dir / "projects.log.3").exists()
        assert (
            stat.S_IMODE((manager.log_dir / "projects.log.1").stat().st_mode) == 0o600
        )

        old = manager.log_dir / "projects.log.2"
        old.touch(exist_ok=True)
        old_time = (datetime.now(UTC) - timedelta(days=15)).timestamp()
        os.utime(old, (old_time, old_time))
        manager._prune(force=True)
        assert not old.exists()
    finally:
        manager.close()


def test_queue_pressure_drops_only_lower_levels_and_synchronously_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DiagnosticManager(tmp_path, queue_capacity=1, watch_settings=False)
    manager.update_settings(
        {"schema": SETTINGS_SCHEMA, "global_level": "debug", "feature_levels": {}}
    )
    release = threading.Event()
    entered = threading.Event()
    original = manager._write_pending

    def blocked(pending):
        entered.set()
        release.wait(timeout=5)
        original(pending)

    monkeypatch.setattr(manager, "_write_pending", blocked)
    try:
        manager.record("debug", "api", "api.queue.first", "First queued record.")
        assert entered.wait(timeout=2)
        producer = threading.Thread(
            target=lambda: [
                manager.record(
                    "debug",
                    "api",
                    "api.queue.pressure",
                    "A lower-level queue record.",
                    metadata={"count": index},
                )
                for index in range(20)
            ]
        )
        producer.start()
        time.sleep(0.05)
        release.set()
        producer.join(timeout=5)
        assert not producer.is_alive()
        assert manager.flush(timeout=5)
        assert manager.status()["dropped_record_count"] > 0
        notices = [
            item
            for item in _records(manager.log_dir / "errors.log")
            if item["event_code"] == "diagnostics.records_dropped"
        ]
        assert len(notices) == 1
    finally:
        release.set()
        manager.close()


def test_live_reload_applies_atomic_external_changes(tmp_path: Path) -> None:
    manager = DiagnosticManager(tmp_path, watch_settings=True)
    try:
        replacement = manager.settings_path.with_suffix(".replacement")
        replacement.write_text(
            json.dumps(
                {
                    "schema": SETTINGS_SCHEMA,
                    "global_level": "warning",
                    "feature_levels": {"chat": "debug"},
                }
            ),
            encoding="utf-8",
        )
        os.chmod(replacement, 0o600)
        os.replace(replacement, manager.settings_path)
        deadline = time.monotonic() + 3
        while (
            manager.effective_level("chat") != "debug" and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert manager.effective_level("chat") == "debug"
        assert manager.effective_level("projects") == "warning"
    finally:
        manager.close()


def test_background_tasks_receive_child_correlation_and_persist_failures(
    manager: DiagnosticManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_manager", manager)
    observed: dict[str, str | None] = {}
    durable = threading.Event()

    async def scenario() -> None:
        async def failing_work() -> None:
            observed["operation_id"] = current_operation_id()
            raise RuntimeError("canary background exception")

        def persist_failure(_exception: BaseException) -> None:
            durable.set()

        with diagnostic_context(operation_id="operation_parent"):
            task = create_diagnostic_task(
                failing_work(),
                feature="missions",
                event_code="missions.background.contract",
                failure_message="A supervised mission task failed.",
                durable_failure=persist_failure,
            )
            with pytest.raises(RuntimeError):
                await task
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert observed["operation_id"] not in {None, "operation_parent"}
    assert durable.wait(timeout=1)
    failures = [
        record
        for record in _records(manager.log_dir / "missions.log")
        if record["event_code"] == "missions.background.contract.failed"
    ]
    assert len(failures) == 1
    assert failures[0]["operation_id"] == observed["operation_id"]
    assert failures[0]["parent_operation_id"] == "operation_parent"
    assert "canary background exception" not in json.dumps(failures[0])


def test_caught_exception_severity_uses_http_and_integrity_semantics(
    manager: DiagnosticManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_manager", manager)
    manager.update_settings(
        {"schema": SETTINGS_SCHEMA, "global_level": "debug", "feature_levels": {}}
    )

    class ExpectedDenial(RuntimeError):
        status_code = 409
        code = "revision_conflict"

    class StorageIntegrityError(RuntimeError):
        pass

    denial = ExpectedDenial("canary expected detail")
    assert (
        record_caught_exception(
            "projects",
            "projects.test.denied",
            "A project update was rejected safely.",
            denial,
            stage="validation",
        )
        is None
    )
    integrity = StorageIntegrityError("canary integrity detail")
    integrity_id = record_caught_exception(
        "storage",
        "storage.test.integrity_failed",
        "A storage integrity check failed.",
        integrity,
        stage="integrity",
    )
    assert integrity_id
    assert manager.flush()

    denial_record = _records(manager.log_dir / "projects.log")[-1]
    integrity_record = _records(manager.log_dir / "storage.log")[-1]
    assert denial_record["level"] == "WARNING"
    assert "error_id" not in denial_record
    assert integrity_record["level"] == "CRITICAL"
    assert integrity_record["error_id"] == integrity_id
    assert diagnostics.diagnostic_error_id(integrity) == integrity_id
    assert "canary" not in json.dumps([denial_record, integrity_record])


def test_export_is_redacted_bounded_and_manifested(
    manager: DiagnosticManager, tmp_path: Path
) -> None:
    manager.record(
        "error",
        "providers",
        "providers.credentials.failed",
        "Provider authentication failed for sk-abcdefghijklmnopqrstuvwxyz123456.",
        metadata={"credential": "canary-credential", "provider": "compatible"},
    )
    (manager.data_dir / "nebula-v3.db").write_text("canary-database", encoding="utf-8")
    (manager.data_dir / "workspace-secret.txt").write_text(
        "canary-workspace", encoding="utf-8"
    )
    startup_log = manager.log_dir / "nebula-core-startup.log"
    startup_log.write_text(
        "RuntimeError: canary-prompt-command-output\n"
        "-----BEGIN PRIVATE KEY-----canary-key-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    os.chmod(startup_log, 0o600)

    output = manager.export(tmp_path / "support.zip")

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "metadata.json" in names
        assert "SHA256SUMS.json" in names
        assert "nebula-v3.db" not in names
        assert "workspace-secret.txt" not in names
        contents = b"".join(archive.read(name) for name in names)
        assert b"canary-credential" not in contents
        assert b"canary-database" not in contents
        assert b"canary-workspace" not in contents
        assert b"canary-prompt-command-output" not in contents
        assert b"canary-key" not in contents
        assert b"sk-abcdefghijklmnopqrstuvwxyz123456" not in contents
        metadata = json.loads(archive.read("metadata.json"))
        assert "log_directory" not in metadata["health"]
        assert "settings_path" not in metadata["health"]
        emergency = archive.read("logs/nebula-core-startup.log")
        assert emergency.count(b"Core stderr line redacted") == 2
        manifest = json.loads(archive.read("SHA256SUMS.json"))
        assert manifest["schema"] == "nebula.diagnostics-manifest/v1"
        assert set(manifest["sha256"]) == names - {"SHA256SUMS.json"}


def test_unwritable_initialization_retains_errors_and_logger_failure_in_memory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    manager = DiagnosticManager(blocked, watch_settings=False)
    try:
        error_id = manager.record(
            "error",
            "storage",
            "storage.test.unwritable",
            "A storage operation failed while diagnostics were unavailable.",
        )
        assert error_id
        assert manager.status()["degraded"] is True
        recent = manager.recent_errors(limit=20)
        by_code = {str(record["event_code"]): record for record in recent}
        assert by_code["storage.test.unwritable"]["error_id"] == error_id
        assert by_code["diagnostics.logging_unavailable"]["level"] == "CRITICAL"
        emergency = capsys.readouterr().err
        assert "NEBULA_DIAGNOSTICS_UNAVAILABLE" in emergency
        assert "not a directory" not in emergency
    finally:
        manager.close()


def test_desktop_child_mirrors_sanitized_error_without_owning_aggregate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = DiagnosticManager(tmp_path, desktop_parent=True, watch_settings=False)
    try:
        error_id = manager.record(
            "error",
            "storage",
            "storage.desktop_child.failed",
            "Desktop child storage failed.",
        )
        captured = capsys.readouterr().err
        assert diagnostics.ERROR_MIRROR_PREFIX in captured
        assert error_id in captured
        assert not (manager.log_dir / "errors.log").exists()
        assert not (manager.log_dir / "desktop.log").exists()
        assert not (manager.log_dir / "interface.log").exists()
    finally:
        manager.close()


def test_diagnostics_cli_status_levels_reset_and_export(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / "cli-data"

    status = runner.invoke(app, ["diagnostics", "status", "--data-dir", str(data_dir)])
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["global_level"] == "error"

    global_level = runner.invoke(
        app,
        ["diagnostics", "set-level", "info", "--data-dir", str(data_dir)],
    )
    assert global_level.exit_code == 0, global_level.stdout
    assert json.loads(global_level.stdout)["settings"]["global_level"] == "info"

    feature_level = runner.invoke(
        app,
        [
            "diagnostics",
            "set-level",
            "debug",
            "--feature",
            "chat",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert feature_level.exit_code == 0, feature_level.stdout
    assert json.loads(feature_level.stdout)["settings"]["feature_levels"] == {
        "chat": "debug"
    }

    invalid = runner.invoke(
        app,
        ["diagnostics", "set-level", "trace", "--data-dir", str(data_dir)],
    )
    assert invalid.exit_code != 0
    assert "unsupported diagnostics level" in invalid.output

    destination = tmp_path / "cli-diagnostics.zip"
    exported = runner.invoke(
        app,
        [
            "diagnostics",
            "export",
            str(destination),
            "--data-dir",
            str(data_dir),
        ],
    )
    assert exported.exit_code == 0, exported.stdout
    assert destination.is_file()
    with zipfile.ZipFile(destination) as archive:
        assert "SHA256SUMS.json" in archive.namelist()

    reset = runner.invoke(
        app, ["diagnostics", "reset-levels", "--data-dir", str(data_dir)]
    )
    assert reset.exit_code == 0, reset.stdout
    assert json.loads(reset.stdout)["settings"] == {
        "schema": SETTINGS_SCHEMA,
        "global_level": "error",
        "feature_levels": {},
        "sensitive_detail_capture": False,
    }

    diagnostics.shutdown_diagnostics()
