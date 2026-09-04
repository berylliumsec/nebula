#!/usr/bin/python3
"""Refresh the terminal container's public egress address atomically."""

from __future__ import annotations

import ipaddress
import os
import pathlib
import time
import urllib.request


STATUS_DIR = pathlib.Path("/run/nebula")
STATUS_PATH = STATUS_DIR / "public-ip"
TEMP_PATH = STATUS_DIR / "public-ip.next"


def main() -> None:
    request = urllib.request.Request(
        "https://api.ipify.org",
        headers={"Accept": "text/plain", "User-Agent": "Nebula-Terminal/1"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        address = str(ipaddress.ip_address(response.read(128).decode().strip()))
    STATUS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    TEMP_PATH.write_text(f"{address}\n{int(time.time())}\n", encoding="ascii")
    os.chmod(TEMP_PATH, 0o600)
    TEMP_PATH.replace(STATUS_PATH)


if __name__ == "__main__":
    main()
