#!/usr/bin/env python3
"""Stage the locked Playwright Chromium headless shell for native installers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


MANIFEST_NAME = "nebula-playwright-runtime.json"
BROWSER_EXECUTABLE_NAMES = {
    "chrome",
    "chrome.exe",
    "chrome-headless-shell",
    "headless_shell",
    "headless_shell.exe",
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


def stage_playwright_runtime(
    destination: Path,
    *,
    target: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Download and verify the headless-only Chromium payload."""

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
        "--only-shell",
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
            "Playwright did not install an executable Chromium headless shell"
        )
    licenses = _browser_licenses(destination)
    if not licenses:
        raise PlaywrightRuntimeStageError(
            "Playwright Chromium did not include its required license payload"
        )
    files = [path for path in destination.rglob("*") if path.is_file()]
    payload_bytes = sum(path.stat().st_size for path in files)
    manifest: dict[str, object] = {
        "schema": 1,
        "browser": "chromium-headless-shell",
        "playwright_version": importlib.metadata.version("playwright"),
        "target": target,
        "payload_bytes": payload_bytes,
        "executables": [path.as_posix() for path in executables],
        "licenses": [path.as_posix() for path in licenses],
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
