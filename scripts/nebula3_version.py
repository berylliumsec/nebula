#!/usr/bin/env python3
"""Synchronize Nebula 3's release version without touching Nebula 2."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class VersionSyncError(RuntimeError):
    """Raised when a manifest is absent, malformed, or out of sync."""


def _validate(version: str) -> str:
    if not SEMVER.fullmatch(version):
        raise VersionSyncError(f"invalid semantic version: {version!r}")
    return version


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionSyncError(f"cannot read {path}") from exc


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.nebula-version.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise VersionSyncError(f"cannot update {path}") from exc


def _match_version(text: str, pattern: str, label: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        raise VersionSyncError(f"cannot locate {label} version")
    return _validate(match.group("version"))


def _cargo_manifest_version(path: Path) -> str:
    return _match_version(
        _read_text(path),
        r'^\[package\]\s*$.*?^version\s*=\s*"(?P<version>[^"]+)"',
        "Cargo package",
    )


def _cargo_lock_version(path: Path) -> str:
    text = _read_text(path)
    for block in re.findall(
        r"^\[\[package\]\]\s*$.*?(?=^\[\[package\]\]|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    ):
        name = re.search(r'^name\s*=\s*"([^"]+)"', block, re.MULTILINE)
        if name is not None and name.group(1) == "nebula-ui":
            return _match_version(
                block,
                r'^version\s*=\s*"(?P<version>[^"]+)"',
                "Cargo lock nebula-ui",
            )
    raise VersionSyncError("cannot locate nebula-ui in Cargo.lock")


def _json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise VersionSyncError(f"invalid JSON in {path}") from exc
    if not isinstance(payload, dict):
        raise VersionSyncError(f"expected an object in {path}")
    return payload


def read_versions(root: Path) -> dict[str, str]:
    """Read every canonical Nebula 3 version-bearing file."""

    root = root.resolve()
    package = _json(root / "ui/package.json")
    package_lock = _json(root / "ui/package-lock.json")
    lock_root = package_lock.get("packages")
    tauri = _json(root / "ui/src-tauri/tauri.conf.json")
    if not isinstance(lock_root, dict) or not isinstance(lock_root.get(""), dict):
        raise VersionSyncError("package-lock.json is missing its root package")

    def json_version(payload: dict[str, object], label: str) -> str:
        value = payload.get("version")
        if not isinstance(value, str):
            raise VersionSyncError(f"{label} is missing a string version")
        return _validate(value)

    return {
        "NEBULA3_VERSION": _validate(
            _read_text(root / "NEBULA3_VERSION").strip()
        ),
        "python module": _match_version(
            _read_text(root / "src/nebula/v3/version.py"),
            r'^__version__\s*=\s*"(?P<version>[^"]+)"',
            "Python module",
        ),
        "Tauri config": json_version(tauri, "Tauri config"),
        "Cargo.toml": _cargo_manifest_version(root / "ui/src-tauri/Cargo.toml"),
        "Cargo.lock": _cargo_lock_version(root / "ui/src-tauri/Cargo.lock"),
        "package.json": json_version(package, "package.json"),
        "package-lock.json": json_version(package_lock, "package-lock.json"),
        "package-lock root": json_version(
            lock_root[""],  # type: ignore[arg-type]
            "package-lock root",
        ),
    }


def check_versions(root: Path, *, expected: str | None = None) -> str:
    """Return the synchronized version or raise with every mismatch."""

    versions = read_versions(root)
    canonical = versions["NEBULA3_VERSION"]
    wanted = _validate(expected) if expected is not None else canonical
    mismatches = {
        label: value for label, value in versions.items() if value != wanted
    }
    if mismatches:
        detail = ", ".join(
            f"{label}={value!r}" for label, value in sorted(mismatches.items())
        )
        raise VersionSyncError(f"expected {wanted!r}; version mismatch: {detail}")
    return wanted


def _replace_once(path: Path, pattern: str, replacement: Callable[[re.Match[str]], str]) -> None:
    text = _read_text(path)
    updated, count = re.subn(
        pattern,
        replacement,
        text,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count != 1:
        raise VersionSyncError(f"could not update exactly one version in {path}")
    _write_text(path, updated)


def set_version(root: Path, version: str) -> None:
    """Atomically update all version sources as one best-effort operation."""

    root = root.resolve()
    version = _validate(version)
    paths = {
        "canonical": root / "NEBULA3_VERSION",
        "python": root / "src/nebula/v3/version.py",
        "tauri": root / "ui/src-tauri/tauri.conf.json",
        "cargo": root / "ui/src-tauri/Cargo.toml",
        "cargo_lock": root / "ui/src-tauri/Cargo.lock",
        "package": root / "ui/package.json",
        "package_lock": root / "ui/package-lock.json",
    }
    originals = {name: _read_text(path) for name, path in paths.items()}
    try:
        _write_text(paths["canonical"], f"{version}\n")
        _replace_once(
            paths["python"],
            r'^(?P<prefix>__version__\s*=\s*")[^"]+(?P<suffix>")',
            lambda match: f'{match.group("prefix")}{version}{match.group("suffix")}',
        )

        tauri = _json(paths["tauri"])
        tauri["version"] = version
        _write_text(paths["tauri"], json.dumps(tauri, indent=2) + "\n")

        _replace_once(
            paths["cargo"],
            r'^(?P<head>\[package\]\s*$.*?^version\s*=\s*")[^"]+(?P<tail>")',
            lambda match: f'{match.group("head")}{version}{match.group("tail")}',
        )
        _replace_once(
            paths["cargo_lock"],
            r'^(?P<head>\[\[package\]\]\s*$\s*name\s*=\s*"nebula-ui"\s*\nversion\s*=\s*")[^"]+(?P<tail>")',
            lambda match: f'{match.group("head")}{version}{match.group("tail")}',
        )

        package = _json(paths["package"])
        package["version"] = version
        _write_text(paths["package"], json.dumps(package, indent=2) + "\n")

        package_lock = _json(paths["package_lock"])
        package_lock["version"] = version
        packages = package_lock.get("packages")
        if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
            raise VersionSyncError("package-lock.json is missing its root package")
        packages[""]["version"] = version
        _write_text(paths["package_lock"], json.dumps(package_lock, indent=2) + "\n")
        check_versions(root, expected=version)
    except Exception:
        for name, path in paths.items():
            _write_text(path, originals[name])
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="fail unless every version matches")
    check.add_argument("--expected")
    update = commands.add_parser("set", help="synchronize a semantic version")
    update.add_argument("version")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "check":
            version = check_versions(arguments.root, expected=arguments.expected)
            print(version)
        else:
            set_version(arguments.root, arguments.version)
            print(arguments.version)
    except VersionSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
