#!/usr/bin/env python3
"""Own a deny-by-default TCP namespace and its policy-aware DNS resolver."""

from __future__ import annotations

import argparse
import ipaddress
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


ENABLE_REQUEST = Path("/run/nebula-egress-enable")
ENABLE_ACK = Path("/run/nebula-egress-enabled")
POLICY_RESOLVER = "127.0.0.53"
DNS_PORT = 53


def _rule(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]:
    parsed = urlsplit(value)
    if parsed.scheme != "tcp" or parsed.hostname is None or parsed.port is None:
        raise ValueError("allow rules must use tcp://IP:port")
    address = ipaddress.ip_address(parsed.hostname)
    if not 1 <= parsed.port <= 65_535:
        raise ValueError("allow-rule port is outside 1..65535")
    return ipaddress.ip_network(address), str(parsed.port)


def _cidr_rule(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]:
    try:
        address, port = value.rsplit(",", 1)
        network = ipaddress.ip_network(address, strict=False)
    except ValueError as exc:
        raise ValueError("CIDR allow rules must use CIDR,PORT") from exc
    if port != "1:65535":
        parsed_port = int(port)
        if not 1 <= parsed_port <= 65_535:
            raise ValueError("allow-rule port is outside 1..65535")
        port = str(parsed_port)
    return network, port


def _run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL)


def _base(binary: str) -> None:
    _run(binary, "-F", "OUTPUT")
    _run(binary, "-P", "OUTPUT", "DROP")
    if binary == "iptables":
        # The worker can reach only this resolver. The root-owned helper may
        # reach the container engine's upstream resolver; the worker is
        # permanently non-root with every capability dropped.
        for protocol in ("udp", "tcp"):
            _run(
                binary,
                "-A",
                "OUTPUT",
                "-p",
                protocol,
                "-d",
                POLICY_RESOLVER,
                "--dport",
                str(DNS_PORT),
                "-j",
                "ACCEPT",
            )
            _run(
                binary,
                "-A",
                "OUTPUT",
                "-p",
                protocol,
                "--dport",
                str(DNS_PORT),
                "-m",
                "owner",
                "--uid-owner",
                "0",
                "-j",
                "ACCEPT",
            )
            _run(
                binary,
                "-A",
                "OUTPUT",
                "-p",
                protocol,
                "--dport",
                str(DNS_PORT),
                "-j",
                "DROP",
            )
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


def _install_rule(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, port: str
) -> None:
    binary = "iptables" if network.version == 4 else "ip6tables"
    _run(
        binary,
        "-A",
        "OUTPUT",
        "-p",
        "tcp",
        "-d",
        str(network),
        "--dport",
        port,
        "-j",
        "ACCEPT",
    )


def _install_rules(
    rules: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str]],
) -> None:
    for network, port in rules:
        _install_rule(network, port)


def _read_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    cursor = offset
    end = offset
    jumped = False
    seen: set[int] = set()
    for _ in range(128):
        if cursor >= len(packet):
            raise ValueError("truncated DNS name")
        length = packet[cursor]
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[cursor + 1]
            if pointer in seen or pointer >= len(packet):
                raise ValueError("invalid DNS pointer")
            seen.add(pointer)
            if not jumped:
                end = cursor + 2
                jumped = True
            cursor = pointer
            continue
        if length & 0xC0 or length > 63:
            raise ValueError("invalid DNS label")
        cursor += 1
        if length == 0:
            if not jumped:
                end = cursor
            return ".".join(labels).lower(), end
        if cursor + length > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[cursor : cursor + length].decode("ascii"))
        cursor += length
    raise ValueError("DNS name exceeds compression limit")


def _question(packet: bytes) -> tuple[str, int]:
    if len(packet) < 12:
        raise ValueError("truncated DNS header")
    _identifier, flags, questions, _answers, _authority, _additional = struct.unpack(
        "!HHHHHH", packet[:12]
    )
    if flags & 0x8000 or questions != 1:
        raise ValueError("DNS request must contain exactly one question")
    name, offset = _read_name(packet, 12)
    if offset + 4 > len(packet):
        raise ValueError("truncated DNS question")
    query_type, query_class = struct.unpack("!HH", packet[offset : offset + 4])
    if query_class != 1 or query_type == 252:
        raise ValueError("unsupported DNS question")
    return name, offset + 4


def _answers(
    packet: bytes, expected_name: str
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    if len(packet) < 12:
        raise ValueError("truncated DNS response")
    _identifier, flags, questions, answer_count, _authority, _additional = struct.unpack(
        "!HHHHHH", packet[:12]
    )
    if not flags & 0x8000 or questions != 1:
        raise ValueError("invalid DNS response")
    name, offset = _read_name(packet, 12)
    if name != expected_name or offset + 4 > len(packet):
        raise ValueError("DNS response question mismatch")
    offset += 4
    records: list[tuple[str, int, bytes | str]] = []
    for _ in range(answer_count):
        owner, offset = _read_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS answer")
        record_type, record_class, _ttl, length = struct.unpack(
            "!HHIH", packet[offset : offset + 10]
        )
        offset += 10
        end = offset + length
        if end > len(packet):
            raise ValueError("truncated DNS answer data")
        if record_class == 1 and record_type == 5:
            target, _unused = _read_name(packet, offset)
            records.append((owner, record_type, target))
        elif record_class == 1 and record_type in {1, 28}:
            records.append((owner, record_type, packet[offset:end]))
        offset = end
    permitted_names = {expected_name}
    for _ in range(len(records) + 1):
        changed = False
        for owner, record_type, data in records:
            if record_type == 5 and owner in permitted_names and data not in permitted_names:
                assert isinstance(data, str)
                permitted_names.add(data)
                changed = True
        if not changed:
            break
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for owner, record_type, data in records:
        if owner not in permitted_names or record_type not in {1, 28}:
            continue
        assert isinstance(data, bytes)
        if len(data) != (4 if record_type == 1 else 16):
            raise ValueError("invalid DNS address answer")
        addresses.append(ipaddress.ip_address(data))
    return addresses


def _error_response(request: bytes, question_end: int, code: int) -> bytes:
    identifier, flags = struct.unpack("!HH", request[:4])
    response_flags = 0x8000 | (flags & 0x0100) | 0x0080 | code
    return struct.pack("!HHHHHH", identifier, response_flags, 1, 0, 0, 0) + request[
        12:question_end
    ]


def _upstream_resolvers() -> list[str]:
    result: list[str] = []
    for line in Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "nameserver":
            address = str(ipaddress.ip_address(fields[1].split("%", 1)[0]))
            if address != POLICY_RESOLVER:
                result.append(address)
    if not result:
        raise ValueError("the egress namespace has no upstream DNS resolver")
    return result


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise OSError("DNS TCP stream ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class PolicyResolver:
    def __init__(
        self,
        *,
        domains: list[str],
        ports: list[int],
        explicit_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
        enabled: bool,
    ) -> None:
        self.domains = domains
        self.ports = [str(port) for port in ports] or ["1:65535"]
        self.explicit_networks = explicit_networks
        self.upstream = _upstream_resolvers()
        self.installed: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.enabled = threading.Event()
        if enabled:
            self.enabled.set()
        self.sockets: list[socket.socket] = []

    def _domain_allowed(self, name: str) -> bool:
        for policy in self.domains:
            if policy.startswith("*."):
                suffix = policy[1:]
                if name.endswith(suffix) and name != policy[2:]:
                    return True
            elif name == policy:
                return True
        return False

    def _address_allowed(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> bool:
        if any(address in network for network in self.explicit_networks):
            return True
        return bool(address.is_global and not address.is_multicast)

    def _forward(self, request: bytes) -> bytes:
        last_error: OSError | None = None
        for upstream in self.upstream:
            family = socket.AF_INET6 if ":" in upstream else socket.AF_INET
            destination = (upstream, DNS_PORT, 0, 0) if family == socket.AF_INET6 else (
                upstream,
                DNS_PORT,
            )
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as client:
                    client.settimeout(3)
                    client.sendto(request, destination)
                    response = client.recv(65_535)
                if len(response) >= 4 and response[:2] == request[:2]:
                    return response
            except OSError as exc:
                last_error = exc
        raise OSError("all upstream DNS resolvers failed") from last_error

    def resolve(self, request: bytes) -> bytes:
        try:
            name, question_end = _question(request)
        except (UnicodeError, ValueError):
            return b""
        if not self._domain_allowed(name):
            return _error_response(request, question_end, 5)
        if not self.enabled.is_set():
            return _error_response(request, question_end, 5)
        try:
            response = self._forward(request)
            if response[:2] != request[:2]:
                raise ValueError("DNS transaction mismatch")
            addresses = _answers(response, name)
            if any(not self._address_allowed(address) for address in addresses):
                return _error_response(request, question_end, 2)
            with self.lock:
                for address in addresses:
                    network = ipaddress.ip_network(address)
                    for port in self.ports:
                        key = (str(network), port)
                        if key not in self.installed:
                            _install_rule(network, port)
                            self.installed.add(key)
            return response
        except (OSError, subprocess.SubprocessError, ValueError):
            return _error_response(request, question_end, 2)

    def _udp(self, server: socket.socket) -> None:
        while not self.stopped.is_set():
            try:
                request, peer = server.recvfrom(65_535)
                response = self.resolve(request)
                if response:
                    server.sendto(response, peer)
            except TimeoutError:
                continue
            except OSError:
                if not self.stopped.is_set():
                    continue

    def _tcp_client(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(5)
            length = struct.unpack("!H", _recv_exact(connection, 2))[0]
            request = _recv_exact(connection, length)
            response = self.resolve(request)
            if response:
                connection.sendall(struct.pack("!H", len(response)) + response)

    def _tcp(self, server: socket.socket) -> None:
        while not self.stopped.is_set():
            try:
                connection, _peer = server.accept()
                threading.Thread(
                    target=self._tcp_client, args=(connection,), daemon=True
                ).start()
            except TimeoutError:
                continue
            except OSError:
                if not self.stopped.is_set():
                    continue

    def start(self) -> None:
        for socket_type, target in (
            (socket.SOCK_DGRAM, self._udp),
            (socket.SOCK_STREAM, self._tcp),
        ):
            server = socket.socket(socket.AF_INET, socket_type)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((POLICY_RESOLVER, DNS_PORT))
            server.settimeout(0.5)
            if socket_type == socket.SOCK_STREAM:
                server.listen(32)
            self.sockets.append(server)
            threading.Thread(target=target, args=(server,), daemon=True).start()

    def close(self) -> None:
        self.stopped.set()
        for server in self.sockets:
            server.close()

    def enable(self) -> None:
        self.enabled.set()


def serve(
    values: list[str],
    cidr_values: list[str],
    domains: list[str],
    domain_ports: list[int],
    *,
    disabled: bool,
) -> int:
    legacy = list(map(_rule, values))
    rules = [*legacy, *[_cidr_rule(value) for value in cidr_values]]
    if not rules and not domains:
        raise ValueError("at least one allow rule or domain is required")
    _base("iptables")
    _base("ip6tables")
    resolver = (
        PolicyResolver(
            domains=domains,
            ports=domain_ports,
            explicit_networks=[network for network, _port in rules],
            enabled=not disabled,
        )
        if domains
        else None
    )
    if resolver is not None:
        resolver.start()
    if not disabled:
        _install_rules(rules)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    print("READY", flush=True)
    try:
        while not stopped.wait(0.1):
            if disabled and ENABLE_REQUEST.exists():
                _install_rules(rules)
                if resolver is not None:
                    resolver.enable()
                disabled = False
                ENABLE_ACK.touch(mode=0o600, exist_ok=True)
    finally:
        if resolver is not None:
            resolver.close()
    return 0


def enable() -> int:
    ENABLE_REQUEST.touch(mode=0o600, exist_ok=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ENABLE_ACK.exists():
            print("ENABLED", flush=True)
            return 0
        time.sleep(0.05)
    raise RuntimeError("egress enable acknowledgement timed out")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nebula-egress")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--allow", action="append", default=[])
    serve_parser.add_argument("--allow-cidr", action="append", default=[])
    serve_parser.add_argument("--domain", action="append", default=[])
    serve_parser.add_argument("--domain-port", action="append", type=int, default=[])
    serve_parser.add_argument("--disabled", action="store_true")
    subparsers.add_parser("enable")
    options = parser.parse_args(argv)
    try:
        if options.operation == "enable":
            return enable()
        if any(not 1 <= port <= 65_535 for port in options.domain_port):
            raise ValueError("domain ports must be between 1 and 65535")
        return serve(
            options.allow,
            options.allow_cidr,
            options.domain,
            options.domain_port,
            disabled=options.disabled,
        )
    except Exception as exc:
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
