"""Retained runtime bytes do not relax the installer toolchain boundary."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from scripts.package_audit import (
    ArtifactAuditError,
    inspect_installer_tree,
)


def _write(path: Path, payload: bytes = b"inert executable") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def installer(tmp_path):
    _write(tmp_path / "usr/bin/nebula-ui")
    _write(tmp_path / "usr/bin/nebula-core")
    _write(tmp_path / "usr/share/doc/nebula/LICENSE", b"BSD")
    _write(tmp_path / "usr/share/doc/nebula/THIRD_PARTY_NOTICES.txt", b"Notices")
    resources = tmp_path / "usr/lib/nebula"
    browser = resources / "playwright-browsers"
    browser_digest = _write(browser / "chromium/chrome")
    _write(browser / "LICENSE.chromium", b"Chromium license")
    _write(browser / "sbom.json", b'{"spdxVersion":"SPDX-2.3"}')
    (browser / "nebula-playwright-runtime.json").write_text(
        json.dumps(
            dict(
                schema=1,
                browser="chromium",
                playwright_version="1.61.0",
                target="x86_64-unknown-linux-gnu",
                payload_bytes=16,
                executables=["chromium/chrome"],
                executable_sha256={"chromium/chrome": browser_digest},
                licenses=["LICENSE.chromium"],
                sbom="sbom.json",
                provenance={
                    "installer": "python -m playwright install chromium",
                    "target": "x86_64-unknown-linux-gnu",
                },
            )
        )
    )
    notebook = resources / "notebook-runtime"
    executable_hashes = {
        relative: _write(notebook / relative)
        for relative in ("python/bin/python3", "uv")
    }
    worker_hash = _write(notebook / "worker.py", b"print('inert worker')")
    (notebook / "nebula-notebook-runtime.json").write_text(
        json.dumps(
            dict(
                schema=1,
                target="x86_64-unknown-linux-gnu",
                executables=executable_hashes,
                worker_sha256=worker_hash,
            )
        )
    )
    return tmp_path


def _seal(bundle: Path):
    files = {
        path.relative_to(bundle).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in bundle.rglob("*")
        if path.is_file()
        and path.name not in {"nebula-runtime.json", "nebula-runtime.sha256"}
    }
    manifest = bundle / "nebula-runtime.json"
    manifest.write_text(
        json.dumps(
            dict(
                format="nebula.runtime-bundle/v1",
                core_executable="bin/nebula-core",
                files=files,
            )
        )
    )
    (bundle / "nebula-runtime.sha256").write_text(
        hashlib.sha256(manifest.read_bytes()).hexdigest() + "\n"
    )


def _retain(installer: Path, *, resources: bool = False) -> Path:
    bundle = installer / "usr/lib/nebula/nebula-runtime"
    _write(bundle / "bin/nebula-core")
    _write(bundle / "bin/_internal/playwright/driver/node")
    _write(bundle / "bin/_internal/libdependency.so", b"retained dependency")
    if resources:
        for name in ("playwright-browsers", "notebook-runtime"):
            shutil.copytree(bundle.parent / name, bundle / "resources" / name)
    _seal(bundle)
    return bundle


def test_retained_driver_is_allowed_but_unrelated_node_is_rejected(installer):
    _retain(installer)
    assert inspect_installer_tree(installer)["retained_bundle_count"] == 1
    _write(installer / "usr/bin/node")
    with pytest.raises(ArtifactAuditError, match="forbidden installer content"):
        inspect_installer_tree(installer)


@pytest.mark.parametrize(
    "corruption", ["driver", "dependency", "marker", "unlisted", "symlink"]
)
def test_retained_driver_permission_requires_complete_verified_bundle(
    installer, corruption
):
    bundle = _retain(installer)
    if corruption == "driver":
        (bundle / "bin/_internal/playwright/driver/node").write_bytes(b"tampered")
    elif corruption == "dependency":
        (bundle / "bin/_internal/libdependency.so").write_bytes(b"tampered")
    elif corruption == "marker":
        (bundle / "nebula-runtime.sha256").write_text("0" * 64)
    elif corruption == "unlisted":
        _write(bundle / "bin/_internal/unlisted.so")
    else:
        selected = bundle / "bin/_internal/libdependency.so"
        selected.unlink()
        selected.symlink_to(installer / "usr/bin/nebula-core")
    with pytest.raises(ArtifactAuditError, match="retained runtime"):
        inspect_installer_tree(installer)


def test_another_hashed_node_location_remains_forbidden(installer):
    bundle = _retain(installer)
    _write(bundle / "bin/_internal/another-driver/node")
    _seal(bundle)
    with pytest.raises(ArtifactAuditError, match="forbidden installer content"):
        inspect_installer_tree(installer)


@pytest.mark.parametrize("legacy_copy", [True, False])
def test_verified_resources_allow_existing_paths_and_retained_only_layout(
    installer, legacy_copy
):
    bundle = _retain(installer, resources=True)
    if not legacy_copy:
        for name in ("playwright-browsers", "notebook-runtime"):
            shutil.rmtree(bundle.parent / name)
    report = inspect_installer_tree(installer)
    assert report["playwright_executable_count"] == 1
    assert report["notebook_executable_count"] == 2


@pytest.mark.parametrize("change", ["changed", "extra", "removed"])
def test_legacy_resource_copy_must_match_every_retained_asset(installer, change):
    bundle = _retain(installer, resources=True)
    browser = bundle.parent / "playwright-browsers"
    if change == "changed":
        (browser / "LICENSE.chromium").write_bytes(b"different nonempty license")
    elif change == "extra":
        _write(browser / "unexpected-data")
    else:
        (browser / "LICENSE.chromium").unlink()
    with pytest.raises(ArtifactAuditError, match="differs from retained bytes"):
        inspect_installer_tree(installer)
