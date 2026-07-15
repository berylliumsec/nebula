#!/usr/bin/env python3
"""Configure a deny-by-default network namespace for one Toolbox call."""

from __future__ import annotations

import argparse
import ipaddress
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit


def _rule(value: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    parsed = urlsplit(value)
    if parsed.scheme != "tcp" or parsed.hostname is None or parsed.port is None:
        raise ValueError("allow rules must use tcp://IP:port")
    address = ipaddress.ip_address(parsed.hostname)
    if not 1 <= parsed.port <= 65535:
        raise ValueError("allow-rule port is outside 1..65535")
    return address, parsed.port


def _run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL)


def _base(binary: str) -> None:
    _run(binary, "-F", "OUTPUT")
    _run(binary, "-P", "OUTPUT", "DROP")
    _run(binary, "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    _run(
        binary,
        "-A",
        "OUTPUT",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )


def serve(values: list[str]) -> int:
    rules = [_rule(value) for value in values]
    if not rules:
        raise ValueError("at least one allow rule is required")
    _base("iptables")
    _base("ip6tables")
    for address, port in rules:
        binary = "iptables" if address.version == 4 else "ip6tables"
        _run(
            binary,
            "-A",
            "OUTPUT",
            "-p",
            "tcp",
            "-d",
            str(address),
            "--dport",
            str(port),
            "-j",
            "ACCEPT",
        )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    print("READY", flush=True)
    stopped.wait()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nebula-egress")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--allow", action="append", default=[])
    options = parser.parse_args(argv)
    try:
        return serve(options.allow)
    except Exception as exc:
        # diagnostic-expected: the supervising Core records the helper failure;
        # this emergency stderr line is bounded and contains no request payload.
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
