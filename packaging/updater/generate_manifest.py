#!/usr/bin/env python3
"""Generate a fail-closed Tauri v2 static update manifest."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ManifestError(RuntimeError):
    """Raised when a release asset set is incomplete or malformed."""


def _compare_semver(left: str, right: str) -> int:
    def parts(value: str) -> tuple[tuple[int, int, int], list[str] | None]:
        match = SEMVER.fullmatch(value)
        if match is None:
            raise ManifestError(f"invalid semantic version: {value!r}")
        release = tuple(int(match.group(index)) for index in (1, 2, 3))
        without_build = value.split("+", 1)[0]
        prerelease = (
            without_build.split("-", 1)[1].split(".") if "-" in without_build else None
        )
        return release, prerelease

    left_release, left_pre = parts(left)
    right_release, right_pre = parts(right)
    if left_release != right_release:
        return (left_release > right_release) - (left_release < right_release)
    if left_pre is None or right_pre is None:
        return (left_pre is None) - (right_pre is None)
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return (int(left_item) > int(right_item)) - (
                int(left_item) < int(right_item)
            )
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return (left_item > right_item) - (left_item < right_item)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def assert_monotonic(previous: dict[str, object], current: dict[str, object]) -> None:
    old = previous.get("version")
    new = current.get("version")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ManifestError("update manifests require semantic versions")
    comparison = _compare_semver(new, old)
    if comparison < 0:
        raise ManifestError(f"refusing to replace newer update {old} with {new}")
    if comparison == 0 and previous != current:
        raise ManifestError(
            f"refusing to replace update {old} with different equal-version content"
        )


def _signature(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ManifestError(f"missing updater signature: {path.name}") from exc
    if not value or any(character.isspace() for character in value):
        raise ManifestError(f"invalid updater signature: {path.name}")
    return value


def generate_manifest(
    *,
    assets: Path,
    version: str,
    tag: str,
    repository: str,
    published_at: str,
    notes: str,
) -> dict[str, object]:
    if not SEMVER.fullmatch(version):
        raise ManifestError(f"invalid semantic version: {version!r}")
    if tag != f"nebula-v{version}":
        raise ManifestError(f"tag {tag!r} does not match version {version!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ManifestError(f"invalid GitHub repository: {repository!r}")
    try:
        datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(
            f"invalid RFC 3339 publication date: {published_at!r}"
        ) from exc

    mac_arm = f"Nebula-{version}-macOS-arm64.updater.tar.gz"
    linux = f"Nebula-{version}-linux-x86_64.AppImage"
    files = {
        "darwin-aarch64": mac_arm,
        "linux-x86_64": linux,
    }
    base_url = (
        f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}"
    )
    platforms: dict[str, dict[str, str]] = {}
    for target, filename in files.items():
        artifact = assets / filename
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise ManifestError(f"missing updater artifact: {filename}")
        platforms[target] = {
            "url": f"{base_url}/{quote(filename)}",
            "signature": _signature(assets / f"{filename}.sig"),
        }

    return {
        "version": version,
        "notes": notes,
        "pub_date": published_at,
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", default="BerylliumSec/nebula")
    parser.add_argument(
        "--published-at",
        default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    parser.add_argument("--notes", default="Nebula desktop update")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    arguments = parser.parse_args()
    try:
        manifest = generate_manifest(
            assets=arguments.assets,
            version=arguments.version,
            tag=arguments.tag,
            repository=arguments.repository,
            published_at=arguments.published_at,
            notes=arguments.notes,
        )
    except ManifestError as exc:
        parser.error(str(exc))
    if arguments.previous is not None and arguments.previous.is_file():
        try:
            previous = json.loads(arguments.previous.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise ManifestError("previous update manifest is not an object")
            assert_monotonic(previous, manifest)
        except (json.JSONDecodeError, OSError, ManifestError) as exc:
            parser.error(str(exc))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
