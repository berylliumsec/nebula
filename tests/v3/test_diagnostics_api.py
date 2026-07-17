from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.diagnostics import DiagnosticManager, SETTINGS_SCHEMA
from nebula.v3.diagnostic_sensitive import SensitiveDiagnosticStore
from nebula.v3.harnesses import HarnessTransportError
from nebula.v3.storage import NebulaStore


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_diagnostics_api_settings_correlation_fault_and_export(tmp_path: Path) -> None:
    manager = DiagnosticManager(tmp_path / "data", watch_settings=False)
    store = NebulaStore(tmp_path / "nebula.db")
    app = create_app(
        store,
        auth_token="test-token",
        diagnostic_manager=manager,
        allow_browser_diagnostic_events=True,
    )

    @app.get("/api/v1/test/diagnostic-fault", tags=["administration"])
    async def diagnostic_fault() -> None:
        raise OSError("Bearer canary-secret-token should never be logged")

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            unauthorized = client.get("/api/v1/diagnostics/settings")
            assert unauthorized.status_code == 401
            assert unauthorized.headers["x-request-id"]
            assert unauthorized.json()["detail"] == "valid bearer token required"

            settings = client.get("/api/v1/diagnostics/settings", headers=_auth())
            assert settings.status_code == 200
            assert settings.json() == {
                "schema": SETTINGS_SCHEMA,
                "global_level": "error",
                "feature_levels": {},
                "sensitive_detail_capture": False,
            }
            updated = client.put(
                "/api/v1/diagnostics/settings",
                headers=_auth(),
                json={
                    "schema": SETTINGS_SCHEMA,
                    "global_level": "debug",
                    "feature_levels": {"storage": "debug"},
                    "sensitive_detail_capture": False,
                },
            )
            assert updated.status_code == 200
            assert updated.json()["feature_levels"] == {"storage": "debug"}

            operation_id = "op_frontend_123"
            failure = client.get(
                "/api/v1/test/diagnostic-fault",
                headers={**_auth(), "X-Nebula-Operation-ID": operation_id},
            )
            assert failure.status_code == 500
            body = failure.json()
            assert body == {
                "detail": "The operation failed unexpectedly. No verified recovery procedure is available.",
                "code": "api.unhandled_exception",
                "feature": "storage",
                "request_id": failure.headers["x-request-id"],
                "error_id": body["error_id"],
                "retryable": False,
                "help_article": None,
                "reason_code": "unknown_internal_fault",
                "operator_detail": "Nebula recorded an internal failure but the available sanitized evidence does not identify a verified root cause.",
                "impact": "The affected operation did not complete; no additional impact can be claimed from the available evidence.",
                "remediation_id": "storage.unknown_internal_fault",
                "recovery_action": "Review recovery guidance",
                "recovery_destination": "/settings#diagnostics-settings",
            }

            matching = [
                record
                for record in _records(manager.log_dir / "storage.log")
                if record["error_id"] == body["error_id"]
            ]
            aggregate = [
                record
                for record in _records(manager.log_dir / "errors.log")
                if record["error_id"] == body["error_id"]
            ]
            assert len(matching) == 1
            assert {record["feature"] for record in aggregate} == {"storage", "api"}
            assert matching[0] in aggregate
            assert matching[0]["request_id"] == body["request_id"]
            assert matching[0]["operation_id"] == operation_id
            assert matching[0]["stage"] == "dispatch"
            assert matching[0]["exception_type"] == "OSError"
            api_failure = [
                record
                for record in _records(manager.log_dir / "api.log")
                if record.get("error_id") == body["error_id"]
            ]
            assert len(api_failure) == 1
            assert api_failure[0]["event_code"] == "api.request.failed"
            assert api_failure[0]["metadata"] == {
                "http_status": 500,
                "method": "GET",
                "route": "/api/v1/test/diagnostic-fault",
            }

            browser_error_id = "err_browser_shared_123"
            browser = client.post(
                "/api/v1/diagnostics/events",
                headers=_auth(),
                json={
                    "events": [
                        {
                            "schema": "nebula.diagnostic/v1",
                            "level": "error",
                            "feature": "interface",
                            "event_code": "interface.test.failed",
                            "message": "The interface test failed.",
                            "error_id": browser_error_id,
                            "exception_type": "Error",
                            "metadata": {
                                "component": "test",
                                "authorization": "canary-authorization",
                            },
                        }
                    ]
                },
            )
            assert browser.status_code == 200
            assert browser.json() == {
                "accepted": 1,
                "error_ids": [browser_error_id],
            }
            interface_record = _records(manager.log_dir / "interface.log")[-1]
            assert interface_record["error_id"] == browser_error_id
            assert interface_record["source"] == "browser"
            assert interface_record["metadata"] == {"component": "test"}

            handled_error_id = "err_core_handled_123"
            handled = client.post(
                "/api/v1/diagnostics/events",
                headers=_auth(),
                json={
                    "events": [
                        {
                            "schema": "nebula.diagnostic/v1",
                            "level": "debug",
                            "feature": "interface",
                            "event_code": "interface.api.handled_failure",
                            "message": "A previously recorded Core error was shown.",
                            "request_id": "req_core_handled_123",
                            "error_id": handled_error_id,
                            "metadata": {"kind": "core-error-handled"},
                        }
                    ]
                },
            )
            assert handled.status_code == 200
            assert handled.json() == {
                "accepted": 1,
                "error_ids": [handled_error_id],
            }
            assert manager.flush()
            interface_record = _records(manager.log_dir / "interface.log")[-1]
            assert interface_record["level"] == "DEBUG"
            assert interface_record["error_id"] == handled_error_id
            assert not any(
                record.get("error_id") == handled_error_id
                for record in _records(manager.log_dir / "errors.log")
            )

            recent = client.get(
                "/api/v1/diagnostics/errors?feature=storage&limit=10",
                headers=_auth(),
            )
            assert recent.status_code == 200
            assert [item["error_id"] for item in recent.json()["errors"]] == [
                body["error_id"]
            ]

            files = client.get("/api/v1/diagnostics/files", headers=_auth())
            assert files.status_code == 200
            assert files.json()["health"]["global_level"] == "debug"
            assert all(
                set(item) == {"name", "size_bytes", "modified_at"}
                for item in files.json()["files"]
            )

            exported = client.post("/api/v1/diagnostics/export", headers=_auth())
            assert exported.status_code == 200
            assert exported.headers["content-type"] == "application/zip"
            with zipfile.ZipFile(BytesIO(exported.content)) as archive:
                names = set(archive.namelist())
                assert "metadata.json" in names
                assert "SHA256SUMS.json" in names
                assert all("nebula.db" not in name for name in names)

        encoded_logs = b"".join(
            path.read_bytes() for path in manager.log_dir.iterdir() if path.is_file()
        )
        assert b"canary-secret-token" not in encoded_logs
        assert b"canary-authorization" not in encoded_logs
    finally:
        manager.close()


def test_browser_ingress_is_disabled_outside_development_mode(tmp_path: Path) -> None:
    manager = DiagnosticManager(tmp_path / "data", watch_settings=False)
    store = NebulaStore(tmp_path / "nebula.db")
    app = create_app(store, auth_token="test-token", diagnostic_manager=manager)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/diagnostics/events",
                headers=_auth(),
                json={
                    "events": [
                        {
                            "schema": "nebula.diagnostic/v1",
                            "level": "error",
                            "feature": "interface",
                            "event_code": "interface.test.failed",
                            "message": "The interface test failed.",
                        }
                    ]
                },
            )
        assert response.status_code == 403
        assert response.json()["detail"] == (
            "browser diagnostic ingress is disabled outside development mode"
        )
    finally:
        manager.close()


def test_actionable_incident_resolution_and_guarded_sensitive_detail(
    tmp_path: Path,
) -> None:
    detail_store = SensitiveDiagnosticStore(
        tmp_path / "protected",
        enabled=True,
        keyring_backend=None,
    )
    manager = DiagnosticManager(
        tmp_path / "data",
        watch_settings=False,
        sensitive_detail_store=detail_store,
    )
    store = NebulaStore(tmp_path / "nebula.db")
    app = create_app(
        store,
        auth_token="test-token",
        diagnostic_manager=manager,
        allow_browser_diagnostic_events=True,
    )
    failure = HarnessTransportError(
        "Codex app-server closed stdout before turn completion"
    )
    error_id = manager.record(
        "error",
        "harnesses",
        "harnesses.turn.runtime_failed",
        "The harness runtime reported a turn failure.",
        request_id="req_shared_harness",
        operation_id="op_shared_harness",
        outcome="failure",
        stage="turn-runtime",
        retryable=True,
        reason_code="transport_closed",
        operator_detail=str(failure),
        exception=failure,
        metadata={"transport": "stdio", "adapter": "codex"},
    )
    assert error_id

    try:
        with TestClient(app) as client:
            wrapper = {
                "schema": "nebula.diagnostic/v1",
                "level": "ERROR",
                "feature": "interface",
                "event_code": "interface.sessions_page.caught_failure_13",
                "message": "A handled interface operation failed.",
                "error_id": error_id,
                "request_id": "req_shared_harness",
                "operation_id": "op_shared_harness",
                "reason_code": "transport_closed",
            }
            resolved = client.post(
                "/api/v1/diagnostics/incidents/resolve",
                headers=_auth(),
                json={"records": [wrapper]},
            )
            assert resolved.status_code == 200, resolved.text
            [incident] = resolved.json()
            assert incident["error_id"] == error_id
            assert incident["primary"]["source"] == "core"
            assert incident["guidance"]["cause"] == str(failure)
            assert incident["guidance"]["verification"]
            assert len(incident["related_records"]) == 1

            fetched = client.get(
                f"/api/v1/diagnostics/incidents/{error_id}", headers=_auth()
            )
            assert fetched.status_code == 200
            assert fetched.json()["sensitive_detail_available"] is True

            unconfirmed = client.post(
                f"/api/v1/diagnostics/incidents/{error_id}/sensitive-detail",
                headers=_auth(),
                json={"confirmed": False, "action": "reveal"},
            )
            assert unconfirmed.status_code == 409

            revealed = client.post(
                f"/api/v1/diagnostics/incidents/{error_id}/sensitive-detail",
                headers=_auth(),
                json={"confirmed": True, "action": "reveal"},
            )
            assert revealed.status_code == 200
            assert revealed.headers["cache-control"] == "no-store"
            assert revealed.json()["detail"] == f"HarnessTransportError: {failure}"

            rejected = client.post(
                f"/api/v1/diagnostics/incidents/{error_id}/actions/arbitrary_command",
                headers=_auth(),
                json={"confirmed": True},
            )
            assert rejected.status_code == 404

        assert manager.flush()
        audit = [
            record
            for record in _records(manager.log_dir / "diagnostics.log")
            if record["event_code"] == "diagnostics.sensitive_detail.accessed"
        ]
        assert audit[-1]["metadata"] == {
            "action": "reveal",
            "operator_id": "local-operator",
        }
        assert str(failure) not in (manager.log_dir / "diagnostics.log").read_text(
            encoding="utf-8"
        )
    finally:
        manager.close()
