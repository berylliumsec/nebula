"""Strict admission and runtime materialization for OpenVPN client profiles."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass


MAX_PROFILE_CHARACTERS = 15_000
_INLINE_BLOCKS = {
    "ca",
    "cert",
    "key",
    "tls-auth",
    "tls-crypt",
    "tls-crypt-v2",
    "extra-certs",
    "pkcs12",
}
_SAFE_OPTIONS = {
    "auth",
    "auth-nocache",
    "cipher",
    "client",
    "connect-retry",
    "connect-retry-max",
    "connect-timeout",
    "data-ciphers",
    "data-ciphers-fallback",
    "explicit-exit-notify",
    "float",
    "key-direction",
    "mssfix",
    "mute",
    "nobind",
    "persist-key",
    "persist-tun",
    "ping",
    "ping-restart",
    "proto",
    "pull",
    "remote",
    "remote-cert-tls",
    "resolv-retry",
    "sndbuf",
    "rcvbuf",
    "tls-client",
    "tls-version-min",
    "tun-mtu",
    "verb",
    "verify-x509-name",
}


class VpnProfileError(ValueError):
    """A profile cannot be admitted without weakening the runtime boundary."""


@dataclass(frozen=True)
class ParsedVpnProfile:
    config: str
    remote_host: str
    remote_port: int
    protocol: str
    fingerprint: str
    requires_credentials: bool


def parse_openvpn_profile(
    config: str, *, username: str | None = None, password: str | None = None
) -> ParsedVpnProfile:
    if not config or len(config) > MAX_PROFILE_CHARACTERS or "\x00" in config:
        raise VpnProfileError("profile must contain 1 to 15000 text characters")
    if (username is None) != (password is None):
        raise VpnProfileError("username and password must be supplied together")
    normalized = config.replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    active_block: str | None = None
    block_names: set[str] = set()
    remotes: list[tuple[str, int]] = []
    protocol = "udp"
    has_client = False
    has_tun = False
    has_server_verification = False
    auth_user_pass = False
    for number, raw in enumerate(normalized.splitlines(), start=1):
        stripped = raw.strip()
        if active_block is not None:
            output.append(raw)
            if stripped.lower() == f"</{active_block}>":
                active_block = None
            continue
        if not stripped or stripped.startswith(("#", ";")):
            output.append(raw)
            continue
        if stripped.startswith("<") and stripped.endswith(">"):
            name = stripped[1:-1].lower()
            if name not in _INLINE_BLOCKS or name in block_names:
                raise VpnProfileError(
                    f"unsupported inline block on line {number}: {name}"
                )
            active_block = name
            block_names.add(name)
            output.append(f"<{name}>")
            continue
        try:
            fields = shlex.split(stripped, comments=False, posix=True)
        except ValueError as exc:
            raise VpnProfileError(f"invalid quoting on line {number}") from exc
        option = fields[0].lstrip("-").lower()
        if option == "auth-user-pass":
            if len(fields) != 1:
                raise VpnProfileError("auth-user-pass file paths are not allowed")
            auth_user_pass = True
            continue
        if option == "dev":
            if len(fields) != 2 or fields[1].lower() not in {"tun", "tun0"}:
                raise VpnProfileError("only a TUN client profile is supported")
            has_tun = True
            output.append("dev tun0")
            continue
        if option == "proto":
            if len(fields) != 2 or fields[1].lower() not in {
                "udp",
                "udp4",
                "tcp",
                "tcp4",
                "tcp-client",
            }:
                raise VpnProfileError("only UDP or TCP client transports are supported")
            protocol = "tcp" if fields[1].lower().startswith("tcp") else "udp"
        elif option == "remote":
            if len(fields) not in {2, 3}:
                raise VpnProfileError("remote must contain one host and optional port")
            try:
                port = int(fields[2]) if len(fields) == 3 else 1194
            except ValueError as exc:
                raise VpnProfileError("remote port must be a number") from exc
            if not 1 <= port <= 65535:
                raise VpnProfileError("remote port is outside 1..65535")
            remotes.append((fields[1], port))
        elif option == "client":
            has_client = True
        elif option in {"remote-cert-tls", "verify-x509-name"}:
            has_server_verification = True
        elif option == "setenv" and fields[1:] == ["opt", "block-outside-dns"]:
            continue
        elif option not in _SAFE_OPTIONS:
            raise VpnProfileError(
                f"unsupported or unsafe option on line {number}: {option}"
            )
        output.append(" ".join(shlex.quote(field) for field in fields))
    if active_block is not None:
        raise VpnProfileError(f"inline {active_block} block is not closed")
    if not has_client or not has_tun or len(remotes) != 1:
        raise VpnProfileError("profile must be a TUN client with exactly one remote")
    if not has_server_verification:
        raise VpnProfileError("profile must verify the VPN server certificate")
    if auth_user_pass:
        if username is None or password is None:
            raise VpnProfileError("this profile requires a username and password")
        if any(
            "\n" in value or "\r" in value or "\x00" in value or "<" in value
            for value in (username, password)
        ):
            raise VpnProfileError("VPN credentials cannot contain line breaks")
        output.extend(["<auth-user-pass>", username, password, "</auth-user-pass>"])
    elif username is not None:
        raise VpnProfileError(
            "credentials were supplied but the profile does not request them"
        )
    output.extend(
        [
            "auth-nocache",
            "script-security 1",
            "redirect-gateway def1",
            "redirect-gateway ipv6",
        ]
    )
    admitted = "\n".join(output).rstrip() + "\n"
    if len(admitted) > 16_384:
        raise VpnProfileError("admitted profile exceeds secure storage capacity")
    remote_host, remote_port = remotes[0]
    return ParsedVpnProfile(
        config=admitted,
        remote_host=remote_host,
        remote_port=remote_port,
        protocol=protocol,
        fingerprint=hashlib.sha256(admitted.encode()).hexdigest(),
        requires_credentials=auth_user_pass,
    )
