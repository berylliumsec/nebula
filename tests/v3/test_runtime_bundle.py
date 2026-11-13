"""Retained packaged and development payloads survive their original tree."""

import shutil
import subprocess

import pytest

from nebula.v3.runtime_bundle import build_frozen_bundle, build_source_bundle
from nebula.v3.release_store import ReleaseIntegrityError, ReleaseStore
from nebula.v3 import runtime_bundle


def test_frozen_bundle_contains_and_checks_every_runtime_resource(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "_internal").mkdir(parents=True)
    executable = runtime / "nebula-runtime"
    executable.write_text('#!/bin/sh\ncat "$(dirname "$0")/_internal/message"\n')
    executable.chmod(0o755)
    (runtime / "_internal/message").write_text("retained\n")
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
        runtime_directory=runtime,
        resources={"playwright-browsers": resources},
    )
    assert {
        "bin/nebula-core",
        "bin/_internal/message",
        "ui/index.html",
        "resources/playwright-browsers/chrome",
    } <= manifest.files.keys()
    store = ReleaseStore(tmp_path / "retained")
    _, installed = store.install(tmp_path / "bundle", expected_manifest_sha256=digest)
    shutil.rmtree(runtime)
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
    runtime = tmp_path / "runtime"
    (runtime / "_internal").mkdir(parents=True)
    core = runtime / "nebula-runtime"
    core.write_bytes(b"core")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("UI")
    _, digest = build_frozen_bundle(
        core, ui, tmp_path / "bundle", runtime_directory=runtime
    )
    (tmp_path / "bundle/bin/nebula-core").write_bytes(b"changed")
    with pytest.raises(ReleaseIntegrityError):
        ReleaseStore.verify(tmp_path / "bundle", expected_manifest_sha256=digest)
    (ui / "alias").symlink_to(core)
    with pytest.raises(ValueError, match="symlinks"):
        build_frozen_bundle(core, ui, tmp_path / "other", runtime_directory=runtime)


@pytest.mark.parametrize(
    "link_kind", ["internal_file", "external_file", "internal_directory"]
)
def test_onedir_links_are_materialized_only_for_internal_files(tmp_path, link_kind):
    runtime = tmp_path / "runtime"
    (runtime / "_internal").mkdir(parents=True)
    executable = runtime / "nebula-runtime"
    executable.write_bytes(b"inert executable")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html/>")
    library = runtime / "_internal/library.so.1"
    library.write_bytes(b"pinned dependency")
    alias = runtime / "_internal/library.so"
    if link_kind == "internal_file":
        alias.symlink_to(library.name)
        manifest, digest = build_frozen_bundle(
            executable, ui, tmp_path / "bundle", runtime_directory=runtime
        )
        materialized = tmp_path / "bundle/bin/_internal/library.so"
        assert not materialized.is_symlink()
        assert materialized.read_bytes() == library.read_bytes()
        assert (
            manifest.files["bin/_internal/library.so"]
            == manifest.files["bin/_internal/library.so.1"]
        )
        assert (
            ReleaseStore.verify(tmp_path / "bundle", expected_manifest_sha256=digest)
            == manifest
        )
    else:
        if link_kind == "external_file":
            outside = tmp_path / "outside.so"
            outside.write_bytes(b"outside must not be copied")
            alias.symlink_to(outside)
        else:
            alias.symlink_to(runtime / "_internal", target_is_directory=True)
        with pytest.raises(ValueError, match="escapes|directory symlinks"):
            build_frozen_bundle(
                executable, ui, tmp_path / "bundle", runtime_directory=runtime
            )
        assert not (tmp_path / "bundle/bin/_internal/library.so").exists()


def test_single_file_runtime_is_not_accepted_as_retained_onedir(tmp_path):
    executable = tmp_path / "nebula-runtime"
    executable.write_bytes(b"onefile")
    with pytest.raises(ValueError, match="complete onedir"):
        build_frozen_bundle(
            executable, tmp_path, tmp_path / "bundle", runtime_directory=tmp_path
        )


def test_compatibility_requires_review_of_exact_migration_tree(tmp_path, monkeypatch):
    source = runtime_bundle.Path(runtime_bundle.__file__).parent
    shutil.copy2(
        source / "runtime-compatibility.json", tmp_path / "runtime-compatibility.json"
    )
    shutil.copytree(source / "migrations/versions", tmp_path / "migrations/versions")
    monkeypatch.setattr(runtime_bundle, "__file__", str(tmp_path / "runtime_bundle.py"))
    assert runtime_bundle.load_compatibility()["additive_migrations"] is True
    (tmp_path / "migrations/versions/9999_unreviewed.py").write_text(
        "def upgrade(): pass\n"
    )
    with pytest.raises(ValueError, match="review runtime-compatibility"):
        runtime_bundle.load_compatibility()


def test_compatibility_reviews_chat_scoped_admission_schema():
    compatibility = runtime_bundle.load_compatibility()

    assert compatibility["migration_head"] == "0048_chat_scoped_admission"
    assert compatibility["schema_family"] == 5
    assert compatibility["schema_min"] == 5
    assert compatibility["schema_max"] == 5
    assert compatibility["additive_migrations"] is True
