import importlib.resources
import os
from contextlib import contextmanager
from pathlib import Path

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

    monkeypatch.setattr(importlib.resources, "files", lambda package: FakeFiles())
    monkeypatch.setattr(importlib.resources, "as_file", fake_as_file)

    resolved = update_utils.resource_path("config/dark-stylesheet.css")

    assert resolved == "/package/config/dark-stylesheet.css"
    assert seen == {
        "relative_path": "config/dark-stylesheet.css",
        "resource": "config/dark-stylesheet.css",
    }


def test_resource_path_falls_back_to_module_directory(monkeypatch):
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda package: (_ for _ in ()).throw(RuntimeError("missing package data")),
    )
    monkeypatch.setattr(update_utils.sys, "frozen", False, raising=False)
    monkeypatch.delattr(update_utils.sys, "_MEIPASS", raising=False)

    resolved = update_utils.resource_path("config/dark-stylesheet.css")

    expected = os.path.join(
        os.path.dirname(os.path.abspath(update_utils.__file__)),
        "config/dark-stylesheet.css",
    )
    assert resolved == expected


def test_resource_path_falls_back_to_meipass_when_frozen(monkeypatch):
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda package: (_ for _ in ()).throw(RuntimeError("missing package data")),
    )
    monkeypatch.setattr(update_utils.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update_utils.sys, "_MEIPASS", "/tmp/bundle", raising=False)

    assert (
        update_utils.resource_path("Images/logo.png")
        == "/tmp/bundle/Images/logo.png"
    )


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
