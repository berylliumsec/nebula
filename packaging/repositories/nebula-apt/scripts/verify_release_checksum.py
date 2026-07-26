#!/usr/bin/env python3
"""Require the release checksum, GitHub digest, and asset bytes to agree."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def checksum_entries(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if len(line) < 67 or line[64:66] not in {"  ", " *"}:
            raise ValueError(f"{path}:{line_number}: invalid SHA-256 line")
        digest = line[:64]
        filename = line[66:]
        if SHA256.fullmatch(digest) is None or not filename:
            raise ValueError(f"{path}:{line_number}: invalid SHA-256 entry")
        entries.append((filename, digest))
    return entries


def verify(checksums: Path, asset: Path, expected: str) -> None:
    if SHA256.fullmatch(expected) is None:
        raise ValueError("expected digest must be 64 lowercase hexadecimal characters")
    matches = [
        digest
        for filename, digest in checksum_entries(checksums)
        if filename == asset.name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{checksums} must contain exactly one entry for {asset.name}"
        )
    actual = hashlib.sha256(asset.read_bytes()).hexdigest()
    if matches[0] != expected or actual != expected:
        raise ValueError(
            f"digest disagreement for {asset.name}: "
            f"release={matches[0]} github={expected} actual={actual}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checksums", type=Path, required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    arguments = parser.parse_args()
    try:
        verify(arguments.checksums, arguments.asset, arguments.sha256)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{arguments.asset}: release checksum and GitHub digest verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
