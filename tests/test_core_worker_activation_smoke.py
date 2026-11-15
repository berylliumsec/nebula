"""The frozen CPU smoke carries the complete verified runtime to its child."""

import pytest

from scripts.benchmarks.core_worker_activation_smoke import verified_runtime_environment
from nebula.v3.release_store import ReleaseIntegrityError
from tests.v3.test_release_store import bundle


def test_verified_bundle_is_explicit_in_child_environment(tmp_path):
    directory = tmp_path / "runtime"
    digest = bundle(directory)
    (directory / "nebula-runtime.sha256").write_text(digest)
    environment, manifest, observed = verified_runtime_environment(directory)
    assert environment == {"NEBULA_RUNTIME_BUNDLE": str(directory.resolve())}
    assert manifest.version == "v1"
    assert observed == digest


def test_tampered_bundle_fails_before_any_child_launch(tmp_path):
    directory = tmp_path / "runtime"
    digest = bundle(directory)
    (directory / "nebula-runtime.sha256").write_text(digest)
    (directory / "nebula-core").write_bytes(b"changed")
    with pytest.raises(ReleaseIntegrityError):
        verified_runtime_environment(directory)


def test_missing_bundle_fails_before_any_child_launch(tmp_path):
    with pytest.raises(FileNotFoundError):
        verified_runtime_environment(tmp_path / "missing")
