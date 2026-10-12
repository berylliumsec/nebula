#!/usr/bin/env python3
"""Stage the locked full Playwright Chromium runtime for native installers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


MANIFEST_NAME = "nebula-playwright-runtime.json"
SBOM_NAME = "nebula-playwright-sbom.spdx.json"
BROWSER_EXECUTABLE_NAMES = {
    "chrome",
    "chrome.exe",
    "google chrome for testing",
}


class PlaywrightRuntimeStageError(RuntimeError):
    """The browser payload could not be staged safely."""


def _clear_generated_payload(destination: Path) -> None:
    if destination.name != "playwright-browsers":
        raise PlaywrightRuntimeStageError(
            "the Playwright runtime destination must be named playwright-browsers"
        )
    if destination.is_symlink():
        raise PlaywrightRuntimeStageError(
            "the Playwright runtime destination cannot be a symbolic link"
        )
    destination.mkdir(parents=True, exist_ok=True)
    for child in destination.iterdir():
        if child.name == ".gitignore":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)


def _browser_executables(destination: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in destination.rglob("*"):
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        relative = path.relative_to(destination)
        if path.name.casefold() in BROWSER_EXECUTABLE_NAMES:
            candidates.append(relative)
    return sorted(candidates)


def _browser_licenses(destination: Path) -> list[Path]:
    return sorted(
        path.relative_to(destination)
        for path in destination.rglob("*")
        if path.is_file() and path.name.casefold().startswith(("license", "notice"))
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_target(
    *, platform_name: str | None = None, machine: str | None = None
) -> str:
    platform_name = platform_name or sys.platform
    machine = (machine or platform.machine()).lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else machine
    if platform_name == "darwin":
        return f"{architecture}-apple-darwin"
    if platform_name.startswith("linux"):
        return f"{architecture}-unknown-linux-gnu"
    if platform_name == "win32":
        return f"{architecture}-pc-windows-msvc"
    raise PlaywrightRuntimeStageError("the host platform is unsupported")


def verified_existing_runtime(
    destination: Path, *, target: str
) -> dict[str, object] | None:
    """Reuse a generated runtime only when its pinned executable still matches."""
    if destination.is_symlink():
        raise PlaywrightRuntimeStageError(
            "the Playwright runtime destination cannot be a symbolic link"
        )
    manifest_path = destination / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != 1
            or manifest.get("browser") != "chromium"
            or manifest.get("target") != target
            or manifest.get("playwright_version")
            != importlib.metadata.version("playwright")
            or not isinstance(manifest.get("executables"), list)
            or not manifest["executables"]
            or not isinstance(manifest.get("executable_sha256"), dict)
            or not isinstance(manifest.get("licenses"), list)
            or not manifest["licenses"]
            or not isinstance(manifest.get("sbom"), str)
        ):
            return None
        root = destination.resolve()
        for relative in [manifest["sbom"], *manifest["licenses"]]:
            payload = (destination / relative).resolve()
            if (
                not payload.is_relative_to(root)
                or not payload.is_file()
                or payload.stat().st_size == 0
            ):
                return None
        for relative in manifest["executables"]:
            executable = (destination / relative).resolve()
            if (
                not executable.is_relative_to(root)
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
                or _sha256(executable) != manifest["executable_sha256"].get(relative)
            ):
                return None
        return manifest
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _normalize_linux_executable_rpath(path: Path) -> None:
    """Make linuxdeploy's later rpath pass byte-for-byte idempotent."""

    with path.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            return
    try:
        subprocess.run(
            ["patchelf", "--set-rpath", "$ORIGIN", str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PlaywrightRuntimeStageError(
            "Playwright Chromium rpath could not be normalized for Linux packaging"
        ) from exc


def stage_playwright_runtime(
    destination: Path,
    *,
    target: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Download and verify full headed Chromium for browserd and headless capture."""

    destination = destination.absolute()
    _clear_generated_payload(destination)
    destination = destination.resolve()
    environment = os.environ.copy()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(destination)
    command = [
        sys.executable,
        "-m",
        "playwright",
        "install",
        "--no-shell",
        "chromium",
    ]
    try:
        run(command, env=environment, check=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PlaywrightRuntimeStageError(
            "Playwright Chromium could not be downloaded"
        ) from exc

    executables = _browser_executables(destination)
    if not executables:
        raise PlaywrightRuntimeStageError(
            "Playwright did not install an executable full Chromium runtime"
        )
    if "linux" in target:
        for executable in executables:
            _normalize_linux_executable_rpath(destination / executable)
    licenses = _browser_licenses(destination)
    if not licenses:
        raise PlaywrightRuntimeStageError(
            "Playwright Chromium did not include its required license payload"
        )
    playwright_version = importlib.metadata.version("playwright")
    executable_sha256 = {
        path.as_posix(): _sha256(destination / path) for path in executables
    }
    primary_sha256 = executable_sha256[executables[0].as_posix()]
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"Nebula Playwright Chromium {target}",
        "documentNamespace": (
            "https://nebula.security/sbom/playwright/"
            f"{playwright_version}/{target}/{primary_sha256}"
        ),
        "creationInfo": {
            "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "creators": ["Tool: scripts/stage_playwright_runtime.py"],
        },
        "packages": [
            {
                "name": "Chrome for Testing",
                "SPDXID": "SPDXRef-Package-Chromium",
                "versionInfo": executables[0].parts[0],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": "BSD-3-Clause",
                "licenseDeclared": "BSD-3-Clause",
                "checksums": [{"algorithm": "SHA256", "checksumValue": primary_sha256}],
            },
            {
                "name": "Playwright Python",
                "SPDXID": "SPDXRef-Package-Playwright",
                "versionInfo": playwright_version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "Apache-2.0",
                "licenseDeclared": "Apache-2.0",
            },
        ],
    }
    (destination / SBOM_NAME).write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files = [path for path in destination.rglob("*") if path.is_file()]
    payload_bytes = sum(path.stat().st_size for path in files)
    manifest: dict[str, object] = {
        "schema": 1,
        "browser": "chromium",
        "playwright_version": playwright_version,
        "target": target,
        "payload_bytes": payload_bytes,
        "executables": [path.as_posix() for path in executables],
        "executable_sha256": executable_sha256,
        "licenses": [path.as_posix() for path in licenses],
        "sbom": SBOM_NAME,
        "provenance": {
            "installer": "python -m playwright install chromium",
            "browser_revision": executables[0].parts[0],
            "target": target,
        },
    }
    (destination / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--if-needed", action="store_true")
    arguments = parser.parse_args()
    target = _host_target() if arguments.target == "auto" else arguments.target
    manifest = (
        verified_existing_runtime(arguments.destination, target=target)
        if arguments.if_needed
        else None
    ) or stage_playwright_runtime(arguments.destination, target=target)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
