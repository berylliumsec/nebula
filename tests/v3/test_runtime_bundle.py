"""Retained packaged and development payloads survive their original tree."""

import shutil
import subprocess

import pytest

from nebula.v3.runtime_bundle import build_frozen_bundle, build_source_bundle
from nebula.v3.release_store import ReleaseIntegrityError, ReleaseStore


def test_frozen_bundle_contains_and_checks_every_runtime_resource(tmp_path):
    executable = tmp_path / "core"
    executable.write_text("#!/bin/sh\necho retained\n")
    executable.chmod(0o755)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>retained UI</html>")
    resources = tmp_path / "browser"
    resources.mkdir()
    (resources / "chrome").write_bytes(b"browser")
    manifest, digest = build_frozen_bundle(
        executable,
        ui,
        tmp_path / "bundle",
        resources={"playwright-browsers": resources},
    )
    assert {
        "bin/nebula-core",
        "ui/index.html",
        "resources/playwright-browsers/chrome",
    } <= manifest.files.keys()
    store = ReleaseStore(tmp_path / "retained")
    _, installed = store.install(tmp_path / "bundle", expected_manifest_sha256=digest)
    executable.unlink()
    shutil.rmtree(ui)
    shutil.rmtree(resources)
    assert store.verify(installed, expected_manifest_sha256=digest) == manifest
    assert (
        subprocess.check_output(
            [str(installed / manifest.core_executable)], text=True
        ).strip()
        == "retained"
    )


def test_source_worker_dispatch_does_not_import_cli_or_original_source(tmp_path):
    package = tmp_path / "source/nebula"
    (package / "v3").mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "v3/__init__.py").touch()
    (package / "v3/cli.py").write_text("raise RuntimeError('worker imported CLI')")
    (package / "v3/runtime_worker.py").write_text(
        "def main():\n    print('retained-worker')\n"
    )
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html/>")
    manifest, digest = build_source_bundle(package, ui, tmp_path / "bundle")
    _, installed = ReleaseStore(tmp_path / "retained").install(
        tmp_path / "bundle", expected_manifest_sha256=digest
    )
    shutil.rmtree(tmp_path / "source")
    output = subprocess.check_output(
        [
            str(installed / manifest.core_executable),
            "runtime-worker",
            "--bootstrap-stdin",
        ],
        text=True,
        cwd=tmp_path,
        env={},
    )
    assert output.strip() == "retained-worker"


def test_runtime_bundle_rejects_tampering_and_symlink_resources(tmp_path):
    core = tmp_path / "core"
    core.write_bytes(b"core")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("UI")
    _, digest = build_frozen_bundle(core, ui, tmp_path / "bundle")
    (tmp_path / "bundle/bin/nebula-core").write_bytes(b"changed")
    with pytest.raises(ReleaseIntegrityError):
        ReleaseStore.verify(tmp_path / "bundle", expected_manifest_sha256=digest)
    (ui / "alias").symlink_to(core)
    with pytest.raises(ValueError, match="symlinks"):
        build_frozen_bundle(core, ui, tmp_path / "other")
