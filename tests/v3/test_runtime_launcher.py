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
