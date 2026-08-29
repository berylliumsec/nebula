#!/usr/bin/env python3
"""Stage the locked full Playwright Chromium runtime for native installers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
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
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": primary_sha256}
                ],
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
    arguments = parser.parse_args()
    manifest = stage_playwright_runtime(
        arguments.destination,
        target=arguments.target,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
