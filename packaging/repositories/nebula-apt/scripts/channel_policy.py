#!/usr/bin/env python3
"""Validate the immutable Nebula APT channel manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


VERSION = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
TAG = re.compile(r"^nebula-v(?P<version>3\..+)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHANNELS = ("stable", "prerelease")
ENTRY_KEYS = {"tag", "version", "asset", "sha256"}


class ChannelPolicyError(ValueError):
    """Raised when channels.json violates the release policy."""


def parse_version(version: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = VERSION.fullmatch(version)
    if match is None:
        raise ChannelPolicyError(f"invalid semantic version: {version!r}")
    prerelease = match.group("prerelease")
    identifiers = None if prerelease is None else tuple(prerelease.split("."))
    if identifiers is not None:
        for identifier in identifiers:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise ChannelPolicyError(
                    f"numeric prerelease identifier has a leading zero: {version!r}"
                )
    return (
        (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
        ),
        identifiers,
    )


def compare_versions(left: str, right: str) -> int:
    left_core, left_prerelease = parse_version(left)
    right_core, right_prerelease = parse_version(right)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_prerelease is None or right_prerelease is None:
        if left_prerelease is right_prerelease:
            return 0
        return 1 if left_prerelease is None else -1
    for left_identifier, right_identifier in zip(
        left_prerelease, right_prerelease, strict=False
    ):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_identifier) > int(right_identifier) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_identifier > right_identifier else -1
    if len(left_prerelease) == len(right_prerelease):
        return 0
    return 1 if len(left_prerelease) > len(right_prerelease) else -1


def validate_entry(channel: str, entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        raise ChannelPolicyError(f"{channel} entry has unsupported fields")
    if not all(isinstance(entry[key], str) for key in ENTRY_KEYS):
        raise ChannelPolicyError(f"{channel} entry fields must be strings")

    tag_match = TAG.fullmatch(entry["tag"])
    if tag_match is None:
        raise ChannelPolicyError(f"invalid Nebula 3 tag: {entry['tag']!r}")
    version = entry["version"]
    parse_version(version)
    if tag_match.group("version") != version:
        raise ChannelPolicyError(f"tag and version disagree: {entry['tag']!r}")
    if ("-" in version) != (channel == "prerelease"):
        raise ChannelPolicyError(f"{version!r} does not belong in {channel}")
    expected_asset = f"Nebula-{version}-linux-x86_64.deb"
    if entry["asset"] != expected_asset:
        raise ChannelPolicyError(f"unexpected asset for {version!r}")
    if SHA256.fullmatch(entry["sha256"]) is None:
        raise ChannelPolicyError(f"invalid SHA-256 for {version!r}")
    return entry


def validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "channels"}:
        raise ChannelPolicyError("channels.json has unsupported top-level fields")
    if payload["schema"] != 1:
        raise ChannelPolicyError("unsupported channels.json schema")
    channels = payload["channels"]
    if not isinstance(channels, dict) or set(channels) != set(CHANNELS):
        raise ChannelPolicyError("channels.json must contain stable and prerelease")

    seen_tags: set[str] = set()
    seen_versions: set[str] = set()
    for channel in CHANNELS:
        entries = channels[channel]
        if not isinstance(entries, list) or len(entries) > 2:
            raise ChannelPolicyError(f"{channel} must retain at most two releases")
        previous_version: str | None = None
        for raw_entry in entries:
            entry = validate_entry(channel, raw_entry)
            if entry["tag"] in seen_tags or entry["version"] in seen_versions:
                raise ChannelPolicyError("release entries must be unique")
            seen_tags.add(entry["tag"])
            seen_versions.add(entry["version"])
            if (
                previous_version is not None
                and compare_versions(previous_version, entry["version"]) <= 0
            ):
                raise ChannelPolicyError(
                    f"{channel} releases must be strictly newest-first"
                )
            previous_version = entry["version"]
    return payload


def load_channels(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChannelPolicyError(f"could not read {path}: {error}") from error
    return validate_payload(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channels", type=Path, nargs="?", default=Path("channels.json"))
    arguments = parser.parse_args()
    try:
        load_channels(arguments.channels)
    except ChannelPolicyError as error:
        parser.error(str(error))
    print(f"{arguments.channels}: channel policy valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
