from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.diagnostics import DiagnosticManager, SETTINGS_SCHEMA
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
            }
            updated = client.put(
                "/api/v1/diagnostics/settings",
                headers=_auth(),
                json={
                    "schema": SETTINGS_SCHEMA,
                    "global_level": "debug",
                    "feature_levels": {"storage": "debug"},
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
