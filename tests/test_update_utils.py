from contextlib import contextmanager
from pathlib import Path

import pytest

from nebula import update_utils


def test_resource_path_uses_importlib_resources(monkeypatch):
    seen = {}

    class FakeFiles:
        def joinpath(self, relative_path):
            seen["relative_path"] = relative_path
            return relative_path

    @contextmanager
    def fake_as_file(resource):
        seen["resource"] = resource
        yield Path("/package") / resource

    monkeypatch.setattr(update_utils, "files", lambda package: FakeFiles())
    monkeypatch.setattr(update_utils, "as_file", fake_as_file)

    resolved = update_utils.resource_path("config/dark-stylesheet.css")

    assert resolved == "/package/config/dark-stylesheet.css"
    assert seen == {
        "relative_path": "config/dark-stylesheet.css",
        "resource": "config/dark-stylesheet.css",
    }


def test_resource_path_fails_closed_when_package_data_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        update_utils,
        "files",
        lambda package: (_ for _ in ()).throw(RuntimeError("missing package data")),
    )

    with pytest.raises(RuntimeError, match="missing package data"):
        update_utils.resource_path("config/dark-stylesheet.css")


def test_is_run_as_package_respects_docker_override(monkeypatch):
    monkeypatch.setenv("IN_DOCKER", "1")

    assert update_utils.is_run_as_package() is False


def test_is_run_as_package_detects_site_packages(monkeypatch):
    monkeypatch.delenv("IN_DOCKER", raising=False)
    monkeypatch.setattr(
        update_utils.os.path,
        "abspath",
        lambda _: "/tmp/site-packages/nebula/update_utils.py",
    )

    assert update_utils.is_run_as_package() is True


def test_return_path_delegates_to_resource_path(monkeypatch):
    monkeypatch.setattr(
        update_utils, "resource_path", lambda path: str(Path("/resolved") / path)
    )

    assert update_utils.return_path("config.css") == "/resolved/config.css"
