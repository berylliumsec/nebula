#!/usr/bin/env python3
"""Send one saved Nebula storage report with a stable ID and bounded retries.

Usage: NEBULA_STORAGE_TOKEN=... python examples/storage_sender.py \
  --url https://nebula.example/api/v1/storage/events --event report.json
Keep report.json unchanged for retries. A new observation needs a new observed_at.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def sender_url(value: str) -> str:
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/api/v1/storage/events"):
        raise ValueError("URL must be the Nebula /api/v1/storage/events endpoint")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("HTTPS is required except for loopback HTTP")
    return value


def prepare(path: Path) -> bytes:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != 1 or not report.get("observed_at"):
        raise ValueError("report needs schema_version 1 and a saved observed_at")
    if not report.get("event_id"):
        identity = {key: value for key, value in report.items() if key != "event_id"}
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        report["event_id"] = hashlib.sha256(raw).hexdigest()[:32]
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode()


def send(url: str, token: str, payload: bytes, *, attempts: int = 6) -> dict:
    for attempt in range(attempts):
        request = Request(url, data=payload, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "nebula-storage-reference-sender/1",
        }, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                if response.status != 202:
                    raise ValueError(f"unexpected Nebula status {response.status}")
                result = json.loads(response.read(65536))
                if not isinstance(result, dict) or result.get("accepted") is not True:
                    raise ValueError("Nebula did not acknowledge the storage event")
                return result
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            last_error = exc
        except URLError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(min(30, 2 ** attempt) + random.random())
    raise RuntimeError(f"delivery failed after {attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--event", type=Path, required=True)
    args = parser.parse_args()
    token = os.getenv("NEBULA_STORAGE_TOKEN")
    if not token:
        parser.error("set NEBULA_STORAGE_TOKEN")
    result = send(sender_url(args.url), token, prepare(args.event))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, HTTPError, RuntimeError) as error:
        print(f"storage sender: {error}", file=sys.stderr)
        sys.exit(1)
