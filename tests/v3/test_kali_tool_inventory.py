from __future__ import annotations

import json
import subprocess
import sys

from nebula.v3 import kali_tool_inventory as inventory


def test_inventory_script_runs_standalone_for_container_image_build():
    result = subprocess.run(
        [sys.executable, inventory.__file__, "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--output" in result.stdout


def test_inventory_uses_installed_security_dependencies_and_path_executables(
    tmp_path, monkeypatch
):
    bin_dir = tmp_path / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    fixture_binaries = {
        "hashcat",
        "nmap",
        "vim",
        *inventory.REQUIRED_AUTOMATION_BINARIES,
    }
    for name in fixture_binaries:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nprintf 'fixture 1.0\\n'\n", encoding="utf-8")
        path.chmod(0o755)
    status = """
Package: kali-linux-headless
Status: install ok installed
Depends: kali-linux-core, hashcat, nmap, git, vim | vim-nox, plocate | mlocate, absent | replacement

Package: kali-linux-core
Status: install ok installed

Package: hashcat
Status: install ok installed

Package: nmap
Status: install ok installed

Package: git
Status: install ok installed

Package: vim
Status: install ok installed

Package: mlocate
Status: install ok installed

Package: replacement
Status: install ok installed
"""
    paths = {
        "hashcat": [str(bin_dir / "hashcat")],
        "nmap": [str(bin_dir / "nmap")],
        "replacement": [str(tmp_path / "usr" / "share" / "replacement")],
    }
    monkeypatch.setattr(inventory, "PATH_DIRECTORIES", frozenset({str(bin_dir)}))
    monkeypatch.setattr(
        inventory.shutil,
        "which",
        lambda name: str(bin_dir / name),
    )

    manifest = inventory.build_manifest(
        status,
        paths_for_package=lambda package: tuple(paths.get(package, ())),
    )

    assert manifest["schema"] == inventory.MANIFEST_SCHEMA
    assert manifest["packages"] == ["hashcat", "nmap", "replacement"]
    assert manifest["tools"] == ["hashcat", "nmap"]
    assert manifest["provenance"] == {
        "hashcat": ["hashcat"],
        "nmap": ["nmap"],
    }
    runtime_binaries = {item["name"]: item for item in manifest["runtime_binaries"]}
    assert set(inventory.REQUIRED_AUTOMATION_BINARIES).issubset(runtime_binaries)
    assert runtime_binaries["rg"]["path"].endswith("/rg")
    assert runtime_binaries["python3"]["version"]
    assert json.loads(json.dumps(manifest)) == manifest


def test_inventory_rejects_missing_or_empty_kali_security_set(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "PATH_DIRECTORIES", frozenset({str(tmp_path)}))
    try:
        inventory.build_manifest("Package: nmap\nStatus: install ok installed\n")
    except ValueError as exc:
        assert "kali-linux-headless" in str(exc)
    else:
        raise AssertionError("missing metapackage should fail")

    status = """
Package: kali-linux-headless
Status: install ok installed
Depends: git, vim

Package: git
Status: install ok installed

Package: vim
Status: install ok installed
"""
    try:
        inventory.build_manifest(status, paths_for_package=lambda _package: ())
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty inventory should fail")
