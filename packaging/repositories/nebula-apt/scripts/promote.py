#!/usr/bin/env python3
"""Record a validated Nebula release in one APT channel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from channel_policy import (
    ChannelPolicyError,
    compare_versions,
    load_channels,
    validate_payload,
)


def main() -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", type=Path, default=Path("channels.json"))
    parser.add_argument("--channel", choices=("stable", "prerelease"), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha256", required=True)
    arguments = parser.parse_args()

    version = arguments.tag.removeprefix("nebula-v")
    entry = {
        "tag": arguments.tag,
        "version": version,
        "asset": f"Nebula-{version}-linux-x86_64.deb",
        "sha256": arguments.sha256,
    }
    try:
        payload = load_channels(arguments.channels)
        validate_payload(
            {
                "schema": 1,
                "channels": {
                    arguments.channel: [entry],
                    "prerelease" if arguments.channel == "stable" else "stable": [],
                },
            }
        )
    except ChannelPolicyError as error:
        parser.error(str(error))

    entries = payload["channels"][arguments.channel]
    for existing in entries:
        if existing["tag"] == arguments.tag or existing["version"] == version:
            if existing == entry:
                parser.error(f"{arguments.tag} is already recorded")
            parser.error(f"{arguments.tag} conflicts with an immutable channel entry")
    if entries and compare_versions(version, entries[0]["version"]) <= 0:
        parser.error(
            f"promotion must advance {arguments.channel} beyond "
            f"{entries[0]['version']}"
        )

    payload["channels"][arguments.channel] = [
        entry,
        *entries,
    ][:2]
    try:
        validate_payload(payload)
    except ChannelPolicyError as error:
        parser.error(str(error))
    arguments.channels.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
