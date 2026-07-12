#!/usr/bin/env python3
"""Rewrite a Tauri DEB with Debian-correct Nebula prerelease ordering."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

from scripts.nebula3_version import SEMVER


class DebPackagingError(RuntimeError):
    """The generated package could not be safely normalized."""


def debian_version(version: str) -> str:
    if not SEMVER.fullmatch(version):
        raise DebPackagingError(f"invalid Nebula semantic version: {version!r}")
    release, separator, prerelease = version.partition("-")
    return f"{release}~{prerelease}" if separator else release


def repack(deb: Path, version: str) -> str:
    deb = deb.resolve()
    if not deb.is_file():
        raise DebPackagingError(f"DEB does not exist: {deb}")
    expected = debian_version(version)
    current = subprocess.check_output(
        ["dpkg-deb", "--field", str(deb), "Version"], text=True
    ).strip()
    if current not in {version, expected}:
        raise DebPackagingError(
            f"generated DEB version {current!r} does not match {version!r}"
        )
    if current == expected:
        return expected

    with tempfile.TemporaryDirectory(prefix="nebula-deb-") as temporary:
        root = Path(temporary) / "root"
        output = Path(temporary) / deb.name
        subprocess.run(["dpkg-deb", "--raw-extract", str(deb), str(root)], check=True)
        control = root / "DEBIAN" / "control"
        text = control.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"^Version: .+$", f"Version: {expected}", text, count=1, flags=re.MULTILINE
        )
        if count != 1:
            raise DebPackagingError("generated DEB control file has no unique Version")
        control.write_text(updated, encoding="utf-8")
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(root), str(output)],
            check=True,
        )
        output.replace(deb)

    rewritten = subprocess.check_output(
        ["dpkg-deb", "--field", str(deb), "Version"], text=True
    ).strip()
    if rewritten != expected:
        raise DebPackagingError("rebuilt DEB did not retain the normalized version")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deb", type=Path)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    print(repack(arguments.deb, arguments.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
