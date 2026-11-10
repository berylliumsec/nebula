"""Published releases include the runnable bundle, not just wrapper metadata."""

import importlib.util
from pathlib import Path
import sys

import pytest

from nebula.v3.runtime_launcher import packaged_bundle
from tests.v3.test_release_store import bundle

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_runtime_bundle_deployer", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)


def built_bundle(tmp_path):
    candidate = tmp_path / "candidate"
    source = candidate / "ui/src-tauri/resources/nebula-runtime"
    digest = bundle(source)
    (source / "nebula-runtime.sha256").write_text(digest + "\n")
    return candidate, source, digest


def test_published_bundle_bootstraps_from_the_configured_release_path(
    tmp_path, monkeypatch
):
    candidate, source, digest = built_bundle(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    (release / "nebula-runtime.json").write_text('{"format":"deployment-wrapper"}')
    destination = release / "dist/nebula-runtime"
    monkeypatch.setenv("NEBULA_RUNTIME_BUNDLE", str(destination))
    monkeypatch.setattr(sys, "executable", str(release / "dist/nebula-core"))
    with pytest.raises(FileNotFoundError):
        packaged_bundle()
    deployer.copy_canonical_runtime_bundle(candidate, release)
    selected, observed_digest = packaged_bundle()
    assert selected == destination and observed_digest == digest
    assert (destination / "nebula-core").read_bytes() == (
        source / "nebula-core"
    ).read_bytes()
    assert (destination / "ui/index.html").read_bytes() == (
        source / "ui/index.html"
    ).read_bytes()


@pytest.mark.parametrize("missing", ["nebula-runtime.json", "nebula-runtime.sha256"])
def test_missing_manifest_pair_refuses_publication(tmp_path, missing):
    candidate, source, _digest = built_bundle(tmp_path)
    (source / missing).unlink()
    with pytest.raises(RuntimeError, match="manifest and digest"):
        deployer.copy_canonical_runtime_bundle(candidate, tmp_path / "release")
    assert not (tmp_path / "release/dist/nebula-runtime").exists()


def test_mismatched_runtime_digest_refuses_publication(tmp_path):
    candidate, source, _digest = built_bundle(tmp_path)
    (source / "nebula-runtime.sha256").write_text("0" * 64)
    with pytest.raises(RuntimeError, match="does not match"):
        deployer.copy_canonical_runtime_bundle(candidate, tmp_path / "release")


def test_linked_runtime_asset_refuses_publication(tmp_path):
    candidate, source, _digest = built_bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic outside bytes")
    (source / "linked").symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlinks"):
        deployer.copy_canonical_runtime_bundle(candidate, tmp_path / "release")
