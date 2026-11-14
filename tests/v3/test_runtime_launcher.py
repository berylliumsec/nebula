"""Private viewer attachment never owns an existing Core's lifetime."""

import json
import os

import pytest
from typer.testing import CliRunner

from nebula.v3 import runtime_launcher as launcher
from nebula.v3.cli import app


def test_token_is_durable_and_rejects_replacement(tmp_path):
    first = launcher.operator_token(tmp_path)
    assert launcher.operator_token(tmp_path) == first
    assert len(first) >= 32
    assert (tmp_path / "runtime/operator-token").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="different administration credential"):
        launcher.operator_token(tmp_path, "changed" * 8)


def test_discovery_rejects_foreign_root_and_unsafe_files(tmp_path):
    root = launcher.runtime_root(tmp_path)
    file = root / launcher.CONNECTION_NAME
    launcher.write_private(file, json.dumps({"data_root": "/another-root"}).encode())
    with pytest.raises(ValueError, match="discovery is invalid"):
        launcher.discovery(tmp_path)
    file.chmod(0o644)
    with pytest.raises(ValueError, match="private regular files"):
        launcher.discovery(tmp_path)


def test_private_state_refuses_symlinks(tmp_path):
    destination = tmp_path / "target"
    destination.mkdir()
    (tmp_path / "runtime").symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        launcher.operator_token(tmp_path)


def test_attaching_reuses_credentials_without_staging_or_spawning(
    tmp_path, monkeypatch
):
    token = launcher.operator_token(tmp_path)
    value = {
        "protocol": launcher.DESKTOP_PROTOCOL,
        "data_root": str(tmp_path),
        "pid": os.getpid(),
        "process_birth": "incarnation",
        "endpoint": "http://127.0.0.1:54321/api/v1",
    }
    launcher.write_private(
        tmp_path / "runtime/connection.json", json.dumps(value).encode()
    )
    monkeypatch.setattr(launcher, "same_process", lambda *_args: True)
    monkeypatch.setattr(
        launcher,
        "health",
        lambda connection, supplied: {"status": "ok"} if supplied == token else None,
    )
    monkeypatch.setattr(
        launcher,
        "retain_bundle",
        lambda *_a, **_kw: pytest.fail("attachment staged a bundle"),
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("attachment spawned a process"),
    )
    assert launcher.desktop_connection(tmp_path) == {
        "protocol": launcher.DESKTOP_PROTOCOL,
        "host": "127.0.0.1",
        "port": 54321,
        "ipc_token": token,
    }


def test_live_unreachable_core_is_not_replaced(tmp_path, monkeypatch):
    launcher.operator_token(tmp_path)
    value = {
        "protocol": launcher.DESKTOP_PROTOCOL,
        "data_root": str(tmp_path),
        "pid": os.getpid(),
        "process_birth": "incarnation",
        "endpoint": "http://127.0.0.1:54321/api/v1",
    }
    launcher.write_private(
        tmp_path / "runtime/connection.json", json.dumps(value).encode()
    )
    monkeypatch.setattr(launcher, "same_process", lambda *_a: True)

    def failed(*_a):
        raise ValueError("health rejected")

    monkeypatch.setattr(launcher, "health", failed)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("replaced live Core"),
    )
    with pytest.raises(ValueError, match="health rejected"):
        launcher.desktop_connection(tmp_path)


def test_desktop_discovery_never_accepts_remote_transport():
    with pytest.raises(ValueError, match="loopback"):
        launcher.connection_frame({"endpoint": "http://example.test/api/v1"}, "a" * 40)


def test_internal_worker_command_requires_private_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr("nebula.v3.runtime_worker.main", lambda: calls.append("worker"))
    assert CliRunner().invoke(app, ["runtime-worker"]).exit_code != 0
    result = CliRunner().invoke(app, ["runtime-worker", "--bootstrap-stdin"])
    assert result.exit_code == 0, result.output
    assert calls == ["worker"]


def test_desktop_cli_uses_explicit_root(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        launcher,
        "desktop_connection",
        lambda root, **_kw: (
            calls.append(root) or {"protocol": launcher.DESKTOP_PROTOCOL}
        ),
    )
    result = CliRunner().invoke(app, ["desktop-connect", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls == [tmp_path]
    assert json.loads(result.output)["protocol"] == launcher.DESKTOP_PROTOCOL


def test_unsupported_local_platform_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="configure a remote Core"):
        launcher.desktop_connection(tmp_path)


def test_legacy_database_requires_explicit_adoption_and_is_preserved(tmp_path):
    database = tmp_path / "nebula.db"
    database.write_bytes(b"unchanged database")
    with pytest.raises(ValueError, match="runtime-adopt"):
        launcher.operator_token(tmp_path)
    token = launcher.operator_token(tmp_path, adopt_legacy=True)
    assert launcher.operator_token(tmp_path) == token
    assert database.read_bytes() == b"unchanged database"


def test_startup_nonce_accepts_the_actual_frozen_service_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        launcher, "retain_bundle", lambda *_a, **_kw: (tmp_path, "a" * 64)
    )
    monkeypatch.setattr(launcher, "same_process", lambda *_a: True)
    monkeypatch.setattr(launcher, "health", lambda *_a: {"status": "ok"})

    def spawn(command, **kwargs):
        nonce = command[command.index("--startup-id") + 1]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == launcher.subprocess.DEVNULL
        launcher.write_private(
            tmp_path / "runtime/connection.json",
            json.dumps(
                {
                    "protocol": launcher.DESKTOP_PROTOCOL,
                    "data_root": str(tmp_path),
                    "pid": 10001,
                    "process_birth": "child",
                    "startup_id": nonce,
                    "endpoint": "http://127.0.0.1:54321/api/v1",
                }
            ).encode(),
        )
        from types import SimpleNamespace

        return SimpleNamespace(pid=10000, poll=lambda: None)

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    assert launcher.desktop_connection(tmp_path)["port"] == 54321


def test_shutdown_requires_success_receipt_and_exact_service_exit(
    tmp_path, monkeypatch
):
    launcher.operator_token(tmp_path)
    launcher.write_private(
        tmp_path / "runtime/connection.json",
        json.dumps(
            {
                "protocol": launcher.DESKTOP_PROTOCOL,
                "data_root": str(tmp_path),
                "pid": 12345,
                "process_birth": "original",
                "endpoint": "http://127.0.0.1:54321/api/v1",
            }
        ).encode(),
    )
    states = iter([True, False])
    monkeypatch.setattr(launcher, "same_process", lambda *_a: next(states))
    from types import SimpleNamespace

    def response(payload):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, *_args, **_kwargs):
            return response({"revision": 7})

        def post(self, _url, **kwargs):
            assert kwargs["json"]["parameters"] == {"expected_revision": 7}
            return response(
                {"state": "succeeded", "operation_id": "owned", "result": {}}
            )

    monkeypatch.setattr(launcher.httpx, "Client", Client)
    assert launcher.shutdown_runtime(tmp_path)["observed_state"] == "stopped"


def test_shutdown_failure_leads_with_the_refusal_reason(tmp_path, monkeypatch):
    launcher.operator_token(tmp_path)
    launcher.write_private(
        tmp_path / "runtime/connection.json",
        json.dumps(
            {
                "protocol": launcher.DESKTOP_PROTOCOL,
                "data_root": str(tmp_path),
                "pid": 12345,
                "process_birth": "original",
                "endpoint": "http://127.0.0.1:54321/api/v1",
            }
        ).encode(),
    )
    monkeypatch.setattr(launcher, "same_process", lambda *_a: True)
    from types import SimpleNamespace

    reason = 'Project "Alpha" (alpha): Project cleanup is unverified'
    result = {
        "detail": "unconfirmed",
        "error_type": "RuntimeIpcError",
        "reason": reason,
    }

    def response(payload):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, *_args, **_kwargs):
            return response({"revision": 7})

        def post(self, _url, **_kwargs):
            return response(
                {"state": "uncertain", "operation_id": "refused", "result": result}
            )

    monkeypatch.setattr(launcher.httpx, "Client", Client)
    with pytest.raises(RuntimeError) as failed:
        launcher.shutdown_runtime(tmp_path)
    assert str(failed.value) == (
        f"Core shutdown needs attention: {reason} Receipt refused: "
        + json.dumps(result)
    )


def test_shutdown_rereads_status_while_workers_move_the_revision(tmp_path, monkeypatch):
    launcher.operator_token(tmp_path)
    launcher.write_private(
        tmp_path / "runtime/connection.json",
        json.dumps(
            {
                "protocol": launcher.DESKTOP_PROTOCOL,
                "data_root": str(tmp_path),
                "pid": 12345,
                "process_birth": "original",
                "endpoint": "http://127.0.0.1:54321/api/v1",
            }
        ).encode(),
    )
    states = iter([True, False])
    monkeypatch.setattr(launcher, "same_process", lambda *_a: next(states))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    from types import SimpleNamespace

    revisions, posted = iter([7, 9]), []

    def response(payload):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, *_args, **_kwargs):
            return response({"revision": next(revisions)})

        def post(self, _url, **kwargs):
            posted.append(kwargs["json"])
            if len(posted) == 1:
                return response(
                    {
                        "state": "failed",
                        "operation_id": kwargs["json"]["operation_id"],
                        "result": {
                            "detail": "deployment changed before shutdown",
                            "error_type": "DeploymentChanged",
                        },
                    }
                )
            return response(
                {"state": "succeeded", "operation_id": "second", "result": {}}
            )

    monkeypatch.setattr(launcher.httpx, "Client", Client)
    assert launcher.shutdown_runtime(tmp_path)["observed_state"] == "stopped"
    assert [item["parameters"] for item in posted] == [
        {"expected_revision": 7},
        {"expected_revision": 9},
    ]
    assert posted[0]["operation_id"] != posted[1]["operation_id"]


def test_shutdown_reports_a_deployment_that_never_settles(tmp_path, monkeypatch):
    launcher.operator_token(tmp_path)
    launcher.write_private(
        tmp_path / "runtime/connection.json",
        json.dumps(
            {
                "protocol": launcher.DESKTOP_PROTOCOL,
                "data_root": str(tmp_path),
                "pid": 12345,
                "process_birth": "original",
                "endpoint": "http://127.0.0.1:54321/api/v1",
            }
        ).encode(),
    )
    monkeypatch.setattr(launcher, "same_process", lambda *_a: True)
    from types import SimpleNamespace

    result = {
        "detail": "deployment changed before shutdown",
        "error_type": "DeploymentChanged",
    }

    def response(payload):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get(self, *_args, **_kwargs):
            return response({"revision": 7})

        def post(self, _url, **_kwargs):
            return response(
                {"state": "failed", "operation_id": "changed", "result": result}
            )

    monkeypatch.setattr(launcher.httpx, "Client", Client)
    with pytest.raises(RuntimeError) as failed:
        launcher.shutdown_runtime(tmp_path, settle_timeout=0)
    assert str(failed.value) == (
        "Core shutdown needs attention: deployment changed before shutdown "
        "(Core workers kept starting or stopping for 0 s; retry once they "
        "settle.) Receipt changed: " + json.dumps(result)
    )
