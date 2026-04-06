import os
from pathlib import Path

from nebula import constants


def test_resource_path_uses_module_directory(monkeypatch):
    monkeypatch.setattr(constants.sys, "frozen", False, raising=False)
    monkeypatch.delattr(constants.sys, "_MEIPASS", raising=False)

    resolved = constants.resource_path("sample.txt")

    expected = os.path.join(
        os.path.dirname(os.path.abspath(constants.__file__)), "sample.txt"
    )
    assert resolved == expected


def test_resource_path_uses_meipass_when_frozen(monkeypatch):
    monkeypatch.setattr(constants.sys, "frozen", True, raising=False)
    monkeypatch.setattr(constants.sys, "_MEIPASS", "/tmp/bundle", raising=False)

    assert constants.resource_path("asset.png") == "/tmp/bundle/asset.png"


def test_resource_path_logs_and_returns_none_on_error(monkeypatch):
    messages = []

    monkeypatch.setattr(constants.logger, "debug", messages.append)
    monkeypatch.setattr(constants.os.path, "join", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))

    assert constants.resource_path("asset.png") is None
    assert messages and "boom" in messages[0]


def test_is_run_as_package_respects_docker_override(monkeypatch):
    monkeypatch.setenv("IN_DOCKER", "1")

    assert constants.is_run_as_package() is False


def test_is_run_as_package_detects_site_packages(monkeypatch):
    monkeypatch.delenv("IN_DOCKER", raising=False)
    monkeypatch.setattr(
        constants.os.path, "abspath", lambda _: "/tmp/site-packages/nebula/constants.py"
    )

    assert constants.is_run_as_package() is True


def test_return_path_passthrough_when_running_as_package(monkeypatch):
    monkeypatch.setattr(constants, "is_run_as_package", lambda: True)

    assert constants.return_path("config.css") == "config.css"


def test_return_path_uses_resource_path_when_not_running_as_package(monkeypatch):
    monkeypatch.setattr(constants, "is_run_as_package", lambda: False)
    monkeypatch.setattr(constants, "resource_path", lambda path: str(Path("/tmp") / path))

    assert constants.return_path("config.css") == "/tmp/config.css"


def test_return_path_logs_and_returns_none_on_error(monkeypatch):
    messages = []

    monkeypatch.setattr(constants.logger, "debug", messages.append)
    monkeypatch.setattr(
        constants, "is_run_as_package", lambda: (_ for _ in ()).throw(RuntimeError("failed"))
    )

    assert constants.return_path("config.css") is None
    assert messages and "failed" in messages[0]
