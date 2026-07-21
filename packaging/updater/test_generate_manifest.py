from pathlib import Path

import pytest

from packaging.updater.generate_manifest import (
    ManifestError,
    assert_monotonic,
    generate_manifest,
)


def release_assets(root: Path, version: str) -> None:
    names = [
        f"Nebula-{version}-macOS-arm64.updater.tar.gz",
        f"Nebula-{version}-linux-x86_64.AppImage",
    ]
    for name in names:
        (root / name).write_bytes(b"artifact")
        (root / f"{name}.sig").write_text("trusted-signature", encoding="utf-8")


def test_manifest_maps_native_tauri_targets(tmp_path: Path) -> None:
    release_assets(tmp_path, "3.1.0")

    manifest = generate_manifest(
        assets=tmp_path,
        version="3.1.0",
        tag="nebula-v3.1.0",
        repository="BerylliumSec/nebula",
        published_at="2026-07-12T12:00:00Z",
        notes="Release notes",
    )

    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "linux-x86_64",
    }


def test_manifest_refuses_an_incomplete_release(tmp_path: Path) -> None:
    release_assets(tmp_path, "3.1.0")
    (tmp_path / "Nebula-3.1.0-macOS-arm64.updater.tar.gz.sig").unlink()

    with pytest.raises(ManifestError, match="missing updater signature"):
        generate_manifest(
            assets=tmp_path,
            version="3.1.0",
            tag="nebula-v3.1.0",
            repository="BerylliumSec/nebula",
            published_at="2026-07-12T12:00:00Z",
            notes="Release notes",
        )


def test_manifest_refuses_channel_rollback_or_equal_version_replacement():
    previous = {"version": "3.1.0", "platforms": {"linux": "old"}}
    with pytest.raises(ManifestError, match="newer update"):
        assert_monotonic(previous, {"version": "3.0.9", "platforms": {}})
    with pytest.raises(ManifestError, match="equal-version"):
        assert_monotonic(
            previous, {"version": "3.1.0+rebuilt", "platforms": {"linux": "new"}}
        )
    assert_monotonic(previous, previous.copy())
    assert_monotonic(previous, {"version": "3.2.0-alpha.1", "platforms": {}})
