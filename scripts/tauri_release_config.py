#!/usr/bin/env python3
"""Generate the non-secret Tauri updater config for a direct release build."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


class ReleaseConfigError(RuntimeError):
    """The protected release environment is incomplete or unsafe."""


def updater_config(public_key: str, endpoint: str) -> dict[str, object]:
    key = public_key.strip()
    if not key:
        raise ReleaseConfigError("NEBULA_UPDATER_PUBLIC_KEY is required")
    parsed = urlsplit(endpoint.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseConfigError(
            "NEBULA_UPDATE_ENDPOINT must be an HTTPS URL without credentials, query, or fragment"
        )
    return {
        "plugins": {
            "updater": {
                "pubkey": key,
                "endpoints": [endpoint.strip()],
            }
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    config = updater_config(
        os.environ.get("NEBULA_UPDATER_PUBLIC_KEY", ""),
        os.environ.get("NEBULA_UPDATE_ENDPOINT", ""),
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
